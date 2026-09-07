"""Canonical snapshot readback, metadata uncertainty, correction and consistency evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    insert,
    select,
    update,
)

from adaptive_trader.collection.operations import collection_experiment
from adaptive_trader.collection.schema import (
    SCHEMA_NAME,
    canonical_work,
    collector_checkpoints,
    collector_configuration,
)
from adaptive_trader.collection.snapshots import freeze_collection_snapshot, parse_snapshot_metadata
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.data.datasets import (
    ArtifactIntegrityError,
    DatasetStatus,
    DatasetValidationError,
    SnapshotMetadataEvidence,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.datasets import DatasetManifestRepository
from adaptive_trader.platform.storage.market_data import BarIdentity, BarWrite, MarketDataRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_dataset_manifests,
    aqa_experiments,
    metadata,
)

_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_END = _START + timedelta(minutes=1)
_CREATED = datetime(2026, 9, 5, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return collection_experiment(Path(__file__).resolve().parents[1] / "configs")


@pytest.fixture
def engine(tmp_path: Path, experiment: ExperimentDefinition) -> Iterator[Engine]:
    selected = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'snapshot.sqlite3'}"
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None, SCHEMA_NAME: None})

    @event.listens_for(selected, "connect")
    def configure(connection: Any, record: object) -> None:
        del record
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    metadata.create_all(selected)
    canonical_work.create(selected)
    collector_configuration.create(selected)
    Table(
        collector_checkpoints.name,
        MetaData(),
        *(
            Column(name, String)
            for name in ("checkpoint_name", "provider", "feed", "adjustment", "symbol", "timeframe")
        ),
        Column("committed_through_utc", DateTime(timezone=True)),
    ).create(selected)
    with selected.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=experiment.content_hash,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.experiment_version,
                schema_version=1,
                configuration=experiment.model_dump(mode="json"),
                content_hash=experiment.content_hash,
                registered_at=_START,
            )
        )
        connection.execute(
            insert(collector_configuration).values(
                name="canonical",
                universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
                experiment_hash=experiment.content_hash,
                history_start=_START,
            )
        )
        for symbol in experiment.collection_allowlist:
            connection.exec_driver_sql(
                "INSERT INTO collector_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("rest_coverage", "alpaca", "IEX", "raw", symbol, "1m", _END.replace(tzinfo=None)),
            )
    try:
        yield selected
    finally:
        selected.dispose()


def _bar(symbol: str, *, close: int = 101) -> BarWrite:
    return BarWrite(
        identity=BarIdentity("alpaca", "iex", "raw", symbol, "1Min", _START, _END),
        received_at=_END + timedelta(seconds=1 if close == 101 else 2),
        provider_timestamp=_START,
        open=Decimal(100),
        high=Decimal(max(102, close)),
        low=Decimal(99),
        close=Decimal(close),
        volume=Decimal(100),
        trade_count=10,
        vwap=Decimal(100),
        quality_flags=("complete",),
        source="collection_projection",
        source_mode="external_provider",
        source_event_id=f"minute_{symbol}_{close}",
        source_payload_hash=sha256_hex((symbol, _START, close)),
    )


def _seed(engine: Engine, experiment: ExperimentDefinition, *, missing: str | None = None) -> None:
    repository = MarketDataRepository(engine)
    for symbol in experiment.collection_allowlist:
        if symbol != missing:
            repository.append(_bar(symbol))


def _evidence(experiment: ExperimentDefinition) -> SnapshotMetadataEvidence:
    return SnapshotMetadataEvidence(
        source="reviewed-fixture",
        source_document_sha256="e" * 64,
        observed_at=_CREATED,
        range_start_utc=_START,
        range_end_utc=_END,
        symbols=tuple(sorted(experiment.collection_allowlist)),
        listing_status="active",
        corporate_action_status="clear",
    )


def _freeze(engine: Engine, experiment: ExperimentDefinition, root: Path, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "experiment": experiment,
        "artifact_root": root.resolve(),
        "range_start": _START,
        "range_end": _END,
        "source_git_commit": "a" * 40,
        "dirty_worktree": False,
        "uv_lock_hash": "b" * 64,
        "created_at": _CREATED,
    }
    arguments.update(overrides)
    return freeze_collection_snapshot(engine, **arguments)


def _parquet(root: Path, artifact_id: str) -> Path:
    return (
        root
        / hashlib.sha256(artifact_id.encode("ascii")).hexdigest()[:2]
        / artifact_id
        / "bars.parquet"
    )


def test_unknown_metadata_requires_explicit_diagnostic_and_preserves_roles(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
) -> None:
    _seed(engine, experiment)
    root = tmp_path / "artifacts"
    with pytest.raises(DatasetValidationError, match="explicit diagnostic"):
        _freeze(engine, experiment, root)
    assert not root.exists()
    frozen, registration = _freeze(engine, experiment, root, diagnostic=True)
    assert frozen.promotable is False
    assert registration.promotable is False
    document = frozen.manifest
    assert document["manifest_schema_version"] == document["dataset_identity_version"] == 2
    assert document["symbols"] == sorted(experiment.collection_allowlist)
    provenance = document["collection_provenance"]
    assert provenance["universe_hash"] == COLLECTION_UNIVERSE_V1.universe_hash
    assert len(provenance["members"]) == 29
    assert (
        sum(member["research_role"] == "collection_only" for member in provenance["members"]) == 18
    )
    assert all(member["execution_authorized"] is False for member in provenance["members"])
    assert provenance["metadata_evidence"] == {
        "listing_status": "unknown",
        "corporate_action_status": "unknown",
    }
    assert provenance["calendar_name"] == "XNAS"
    table = pq.read_table(_parquet(root, frozen.artifact_id))
    assert table.num_rows == frozen.row_count == 11
    assert table.column("symbol").to_pylist() == sorted(experiment.collection_allowlist)
    repeated, second_registration = _freeze(engine, experiment, root, diagnostic=True)
    assert not repeated.created and not second_registration.created
    assert repeated.manifest_bytes == frozen.manifest_bytes
    with engine.connect() as connection:
        assert (
            connection.scalar(select(aqa_dataset_manifests.c.manifest_hash)) == frozen.manifest_hash
        )


def test_explicit_scoped_metadata_is_usable_and_changes_dataset_identity(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
) -> None:
    _seed(engine, experiment)
    metadata_evidence = _evidence(experiment)
    first, _ = _freeze(
        engine, experiment, tmp_path / "artifacts", metadata_evidence=metadata_evidence
    )
    changed, _ = _freeze(
        engine,
        experiment,
        tmp_path / "artifacts",
        metadata_evidence=replace(metadata_evidence, source_document_sha256="f" * 64),
    )
    assert first.promotable and changed.promotable
    assert first.logical_hash != changed.logical_hash
    assert first.physical_hash == changed.physical_hash


@pytest.mark.parametrize(
    "status_field,status",
    [
        ("listing_status", "unknown"),
        ("listing_status", "invalidated"),
        ("corporate_action_status", "unknown"),
        ("corporate_action_status", "invalidated"),
    ],
)
def test_uncertain_or_invalidated_metadata_cannot_promote(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
    status_field: str,
    status: str,
) -> None:
    _seed(engine, experiment)
    evidence = (
        replace(_evidence(experiment), listing_status=status)
        if status_field == "listing_status"
        else replace(_evidence(experiment), corporate_action_status=status)
    )
    with pytest.raises(DatasetValidationError, match="explicit diagnostic"):
        _freeze(engine, experiment, tmp_path / "rejected", metadata_evidence=evidence)
    result, _ = _freeze(
        engine, experiment, tmp_path / "diagnostic", metadata_evidence=evidence, diagnostic=True
    )
    assert not result.promotable
    assert result.manifest["collection_provenance"]["metadata_evidence"][status_field] == status


def test_missing_required_symbol_is_retained_in_gap_and_row_summary(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
) -> None:
    _seed(engine, experiment, missing="AMD")
    with pytest.raises(DatasetValidationError, match="explicit diagnostic"):
        _freeze(engine, experiment, tmp_path / "rejected", metadata_evidence=_evidence(experiment))
    frozen, _ = _freeze(engine, experiment, tmp_path / "diagnostic", diagnostic=True)
    assert frozen.manifest["gap_summary"] == {
        "expected_rows": 11,
        "missing_rows": 1,
        "unresolved_gap_count": 1,
        "repaired_gap_count": 0,
    }
    assert {"symbol": "AMD", "timeframe": "1Min", "row_count": 0} in frozen.manifest["row_counts"]


@pytest.mark.parametrize("boundary", ["pending", "checkpoint", "history", "universe"])
def test_durable_state_cannot_be_silently_ignored(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
    boundary: str,
) -> None:
    _seed(engine, experiment)
    with engine.begin() as connection:
        if boundary == "pending":
            connection.execute(
                insert(canonical_work).values(
                    symbol="AMD", session_date=_START.date(), generation=1, through_at=_END
                )
            )
        elif boundary == "checkpoint":
            connection.execute(
                update(collector_checkpoints)
                .where(collector_checkpoints.c.symbol == "AMD")
                .values(committed_through_utc=_START)
            )
        elif boundary == "history":
            connection.execute(update(collector_configuration).values(history_start=_END))
        else:
            connection.execute(update(collector_configuration).values(universe_hash="c" * 64))
    with pytest.raises(DatasetValidationError):
        _freeze(engine, experiment, tmp_path / "rejected", metadata_evidence=_evidence(experiment))


def test_correction_gets_new_identity_and_leaves_previous_parquet_untouched(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
) -> None:
    _seed(engine, experiment)
    root = tmp_path / "artifacts"
    before, _ = _freeze(engine, experiment, root, diagnostic=True)
    before_bytes = _parquet(root, before.artifact_id).read_bytes()
    MarketDataRepository(engine).append(_bar("AMD", close=103))
    after, _ = _freeze(engine, experiment, root, diagnostic=True)
    assert after.dataset_id != before.dataset_id and after.physical_hash != before.physical_hash
    assert _parquet(root, before.artifact_id).read_bytes() == before_bytes
    assert after.manifest["correction_summary"] == {
        "effective_corrected_rows": 1,
        "superseded_revision_count": 1,
    }
    amd = [
        row
        for row in pq.read_table(_parquet(root, after.artifact_id)).to_pylist()
        if row["symbol"] == "AMD"
    ]
    assert amd[0]["close"] == "103" and amd[0]["revision"] == 2


@pytest.mark.parametrize("change", ["unknown", "version", "naive", "symbols", "future", "coverage"])
def test_metadata_parser_and_coverage_fail_closed(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
    change: str,
) -> None:
    _seed(engine, experiment)
    document = json.loads(
        json.dumps(_evidence(experiment).payload(), default=lambda value: value.isoformat())
    )
    if change == "unknown":
        document["approved"] = True
    elif change == "version":
        document["schema_version"] = True
    elif change == "naive":
        document["observed_at"] = "2026-09-05T12:00:00"
    elif change == "symbols":
        document["symbols"] = ["AMD"]
    elif change == "future":
        document["observed_at"] = (_CREATED + timedelta(days=1)).isoformat()
    else:
        document["range_start_utc"] = _END.isoformat()
        document["range_end_utc"] = (_END + timedelta(minutes=1)).isoformat()
    with pytest.raises(DatasetValidationError):
        parsed = parse_snapshot_metadata(document)
        _freeze(engine, experiment, tmp_path / "rejected", metadata_evidence=parsed)


def test_metadata_parser_accepts_exact_scoped_payload(experiment: ExperimentDefinition) -> None:
    document = json.loads(
        json.dumps(_evidence(experiment).payload(), default=lambda value: value.isoformat())
    )
    assert parse_snapshot_metadata(document) == _evidence(experiment)


def test_snapshot_consistent_read_does_not_mix_in_concurrent_correction(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
) -> None:
    _seed(engine, experiment)
    changed = False

    def after_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: Any,
    ) -> None:
        nonlocal changed
        del connection, cursor, parameters, context, executemany
        if (
            not changed
            and statement.startswith("SELECT")
            and "FROM main.aqa_bar_identities" in statement
        ):
            changed = True
            MarketDataRepository(engine).append(_bar("AMD", close=103))

    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        frozen, _ = _freeze(engine, experiment, tmp_path / "artifacts", diagnostic=True)
    finally:
        event.remove(engine, "after_cursor_execute", after_execute)
    assert changed
    assert frozen.manifest["correction_summary"]["effective_corrected_rows"] == 0
    rows = pq.read_table(_parquet(tmp_path / "artifacts", frozen.artifact_id)).to_pylist()
    assert next(row["close"] for row in rows if row["symbol"] == "AMD") == "101"
    current, _ = _freeze(engine, experiment, tmp_path / "artifacts", diagnostic=True)
    assert current.manifest["correction_summary"]["effective_corrected_rows"] == 1


def test_parquet_readback_rejects_serialized_values_that_differ_from_canonical(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(engine, experiment)
    writer = pq.write_table

    def write_corrupt_table(table: Any, stream: Any, **options: Any) -> None:
        writer(table.slice(1), stream, **options)

    monkeypatch.setattr(pq, "write_table", write_corrupt_table)
    with pytest.raises(ArtifactIntegrityError, match="readback differs"):
        _freeze(engine, experiment, tmp_path / "artifacts", diagnostic=True)
    with engine.connect() as connection:
        assert connection.scalar(select(aqa_dataset_manifests.c.dataset_id)) is None


def test_manifest_registration_cannot_promote_unknown_collection_metadata(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
) -> None:
    _seed(engine, experiment)
    frozen, _ = _freeze(engine, experiment, tmp_path / "artifacts", diagnostic=True)
    document = frozen.manifest
    document.update(promotable=True, status="promotable", diagnostic_only=False)
    document.pop("manifest_hash")
    manifest_hash = sha256_hex(document)
    document["manifest_hash"] = manifest_hash
    tampered = replace(
        frozen,
        promotable=True,
        status=DatasetStatus.PROMOTABLE,
        manifest_hash=manifest_hash,
        manifest_bytes=canonical_json_bytes(document),
    )
    with pytest.raises(DatasetValidationError, match="cannot authorize promotion"):
        DatasetManifestRepository(engine).register(tampered)


@pytest.mark.parametrize(
    "end", [_START + timedelta(days=32), _CREATED, _END + timedelta(seconds=1)]
)
def test_snapshot_refuses_unbounded_future_or_unaligned_range(
    engine: Engine,
    experiment: ExperimentDefinition,
    tmp_path: Path,
    end: datetime,
) -> None:
    with pytest.raises(DatasetValidationError, match="completed minutes within 31 days"):
        _freeze(engine, experiment, tmp_path / "artifacts", range_end=end, diagnostic=True)
