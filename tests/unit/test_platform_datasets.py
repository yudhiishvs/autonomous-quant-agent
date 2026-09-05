"""Determinism, immutability, and path-security tests for Parquet datasets."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine, create_engine, event, insert, select

import adaptive_trader.platform.data.datasets as datasets_module
from adaptive_trader.platform.config import ExperimentDefinition, load_experiment
from adaptive_trader.platform.data.aggregation import EffectiveBar
from adaptive_trader.platform.data.datasets import (
    DATASET_PARQUET_SCHEMA,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactSecurityError,
    DatasetFreezeRequest,
    DatasetGapSummary,
    DatasetStatus,
    DatasetValidationError,
    LocalFilesystemArtifactStore,
    freeze_dataset,
)
from adaptive_trader.platform.data.normalization import CanonicalBar
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError
from adaptive_trader.platform.storage.datasets import (
    DatasetManifestPersistenceError,
    DatasetManifestRepository,
    freeze_and_register_dataset,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_dataset_manifests,
    aqa_experiments,
    metadata,
)

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"
_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_CREATED = datetime(2026, 9, 5, 12, 30, 45, 123456, tzinfo=UTC)
_GIT_COMMIT = "a" * 40
_UV_LOCK_HASH = "b" * 64


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_CONFIG_ROOT,
    )


@pytest.fixture
def dataset_engine(tmp_path: Path, experiment: ExperimentDefinition) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'datasets.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=experiment.content_hash,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.experiment_version,
                schema_version=experiment.schema_version,
                configuration=experiment.model_dump(mode="json"),
                content_hash=experiment.content_hash,
                registered_at=_CREATED,
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _bar(
    symbol: str,
    minute: int,
    *,
    provider: str = "alpaca",
    source_mode: str = "external_provider",
    close: Decimal | None = None,
    quality_flags: tuple[str, ...] = ("complete",),
    is_correction: bool = False,
) -> CanonicalBar:
    start = _START + timedelta(minutes=minute)
    selected_close = Decimal("100.5") + minute if close is None else close
    source_event_id = f"{provider}_{symbol.lower()}_{minute}_v{2 if is_correction else 1}"
    return CanonicalBar(
        provider=provider,
        feed="iex",
        adjustment="raw",
        symbol=symbol,
        timeframe="1Min",
        source_mode=source_mode,
        interval_start_utc=start,
        interval_end_utc=start + timedelta(minutes=1),
        receipt_timestamp_utc=start + timedelta(minutes=1, seconds=1),
        provider_event_timestamp_utc=start,
        open=Decimal(100 + minute),
        high=Decimal(101 + minute),
        low=Decimal(99 + minute),
        close=selected_close,
        volume=Decimal(1_000 + minute),
        trade_count=10 + minute,
        vwap=Decimal("100.4") + minute,
        schema_version=1,
        source_event_id=source_event_id,
        quality_flags=quality_flags,
        is_correction=is_correction,
        correction_of_source_event_id=(
            f"{provider}_{symbol.lower()}_{minute}_v1" if is_correction else None
        ),
    )


def _effective(
    symbol: str,
    minute: int,
    *,
    revision: int = 1,
    event_digit: str | None = None,
    **bar_overrides: object,
) -> EffectiveBar:
    digit = event_digit or f"{minute + 1:x}"
    return EffectiveBar(
        bar_event_id=f"bar_event_{digit * 64}",
        revision=revision,
        bar=_bar(symbol, minute, **bar_overrides),  # type: ignore[arg-type]
    )


def _request(
    experiment: ExperimentDefinition,
    *,
    bars: tuple[EffectiveBar, ...] | None = None,
    symbols: tuple[str, ...] = ("AMD",),
    gap_summary: DatasetGapSummary | None = None,
    dirty_worktree: bool = False,
    created_at: datetime = _CREATED,
) -> DatasetFreezeRequest:
    selected = bars or (_effective("AMD", 0),)
    gaps = gap_summary or DatasetGapSummary(
        expected_rows=len(selected),
        missing_rows=0,
        unresolved_gap_count=0,
        repaired_gap_count=0,
    )
    represented_minutes = max(
        int((item.bar.interval_end_utc - _START).total_seconds() // 60) for item in selected
    )
    declared_minutes = math.ceil(gaps.expected_rows / len(symbols))
    return DatasetFreezeRequest(
        experiment=experiment,
        symbols=symbols,
        effective_bars=selected,
        range_start_utc=_START,
        range_end_utc=_START + timedelta(minutes=max(represented_minutes, declared_minutes)),
        gap_summary=gaps,
        source_git_commit=_GIT_COMMIT,
        dirty_worktree=dirty_worktree,
        uv_lock_hash=_UV_LOCK_HASH,
        created_at=created_at,
    )


def _artifact_directory(root: Path, artifact_id: str) -> Path:
    bucket = hashlib.sha256(artifact_id.encode("ascii")).hexdigest()[:2]
    return root / bucket / artifact_id


def test_freeze_has_known_logical_identity_and_explicit_parquet_schema(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    root = tmp_path.resolve() / "artifacts"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        frozen = freeze_dataset(_request(experiment), store=store)

    assert frozen.logical_hash == "bfdc24e625a42ded4c450475c8f182f098fd58ab048b098e11e15c8108079c1d"
    assert frozen.dataset_id == f"dataset_{frozen.logical_hash}"
    assert frozen.artifact_id == frozen.dataset_id
    assert frozen.promotable is True
    assert frozen.status is DatasetStatus.PROMOTABLE
    assert frozen.created is True

    directory = _artifact_directory(root, frozen.artifact_id)
    table = pq.read_table(directory / "bars.parquet")
    assert table.schema == DATASET_PARQUET_SCHEMA
    assert table.column_names == DATASET_PARQUET_SCHEMA.names
    assert table.to_pylist()[0]["close"] == "100.5"
    assert table.to_pylist()[0]["quality_flags"] == ["complete"]
    assert hashlib.sha256((directory / "bars.parquet").read_bytes()).hexdigest() == (
        frozen.physical_hash
    )

    manifest = frozen.manifest
    assert manifest["experiment_id"] == experiment.experiment_id
    assert manifest["experiment_version"] == experiment.experiment_version
    assert manifest["experiment_hash"] == experiment.content_hash
    assert manifest["symbols"] == ["AMD"]
    assert manifest["row_counts"] == [{"row_count": 1, "symbol": "AMD", "timeframe": "1Min"}]
    assert manifest["roles"] == [
        {"role": "ACTIVE_TRADABLE", "symbols": ["AMD"]},
        {"role": "BENCHMARK_ONLY", "symbols": []},
        {"role": "CONTEXT_ONLY", "symbols": []},
        {"role": "EXCLUDED", "symbols": []},
    ]
    assert manifest["source_git_commit"] == _GIT_COMMIT
    assert manifest["dirty_worktree"] is False
    assert manifest["uv_lock_hash"] == _UV_LOCK_HASH
    assert manifest["created_at"] == "2026-09-05T12:30:45.123456Z"
    assert manifest["manifest_hash"] == frozen.manifest_hash


def test_input_order_produces_identical_logical_and_physical_content(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    bars = (
        _effective("NVDA", 1, event_digit="4"),
        _effective("NVDA", 0, event_digit="3"),
        _effective("AMD", 1, event_digit="2"),
        _effective("AMD", 0, event_digit="1"),
    )
    request = _request(experiment, bars=bars, symbols=("AMD", "NVDA"))
    reverse_request = replace(request, effective_bars=tuple(reversed(bars)))

    with LocalFilesystemArtifactStore(trusted_artifact_root=tmp_path.resolve() / "first") as first:
        first_result = freeze_dataset(request, store=first)
    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "second"
    ) as second:
        second_result = freeze_dataset(reverse_request, store=second)

    assert first_result.dataset_id == second_result.dataset_id
    assert first_result.logical_hash == second_result.logical_hash
    assert first_result.physical_hash == second_result.physical_hash
    assert first_result.manifest_bytes == second_result.manifest_bytes


def test_provenance_and_promotion_are_metadata_not_logical_or_physical_identity(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    first_request = _request(experiment)
    later_request = replace(
        first_request,
        source_git_commit="c" * 40,
        dirty_worktree=True,
        uv_lock_hash="d" * 64,
        created_at=_CREATED + timedelta(hours=1),
    )

    with LocalFilesystemArtifactStore(trusted_artifact_root=tmp_path.resolve() / "first") as first:
        first_result = freeze_dataset(first_request, store=first)
    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "second"
    ) as second:
        later_result = freeze_dataset(later_request, store=second)

    assert first_result.dataset_id == later_result.dataset_id
    assert first_result.logical_hash == later_result.logical_hash
    assert first_result.physical_hash == later_result.physical_hash
    assert first_result.manifest_hash != later_result.manifest_hash
    assert first_result.status is DatasetStatus.PROMOTABLE
    assert later_result.status is DatasetStatus.DIAGNOSTIC
    assert later_result.manifest["source_git_commit"] == "c" * 40
    assert later_result.manifest["uv_lock_hash"] == "d" * 64


def test_repeated_freeze_preserves_first_immutable_manifest(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    later = replace(
        request,
        source_git_commit="c" * 40,
        dirty_worktree=True,
        uv_lock_hash="d" * 64,
        created_at=request.created_at + timedelta(days=1),
    )
    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "artifacts"
    ) as store:
        first = freeze_dataset(request, store=store)
        repeated = freeze_dataset(later, store=store)

    assert first.created is True
    assert repeated.created is False
    assert repeated.dataset_id == first.dataset_id
    assert repeated.manifest_hash == first.manifest_hash
    assert repeated.manifest_bytes == first.manifest_bytes
    assert repeated.created_at == first.created_at
    assert repeated.status is DatasetStatus.PROMOTABLE
    assert repeated.manifest["source_git_commit"] == _GIT_COMMIT


def test_freeze_registration_and_audit_are_retry_stable(
    tmp_path: Path,
    experiment: ExperimentDefinition,
    dataset_engine: Engine,
) -> None:
    request = _request(experiment)
    repository = DatasetManifestRepository(dataset_engine)
    root = tmp_path.resolve() / "registered-artifacts"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        first_frozen, first_registration = freeze_and_register_dataset(
            request,
            store=store,
            repository=repository,
        )
        replay_frozen, replay_registration = freeze_and_register_dataset(
            request,
            store=store,
            repository=repository,
        )

    assert first_frozen.dataset_id == replay_frozen.dataset_id
    assert first_registration.created is True
    assert replay_registration == replace(first_registration, created=False)
    with dataset_engine.connect() as connection:
        manifests = connection.execute(select(aqa_dataset_manifests)).mappings().all()
    assert len(manifests) == 1
    assert manifests[0]["dataset_id"] == first_frozen.dataset_id
    audit = AuditRepository(dataset_engine, writer=AuditWriter.COLLECTOR)
    events = audit.list_events(stream_id=f"aqa_collector:data:{first_frozen.dataset_id}")
    assert len(events) == 1
    assert events[0].event_type == "dataset.frozen"
    assert events[0].audit_payload.value["manifest_hash"] == first_frozen.manifest_hash


def test_audit_failure_rolls_back_manifest_registration(
    tmp_path: Path,
    experiment: ExperimentDefinition,
    dataset_engine: Engine,
) -> None:
    request = _request(experiment)
    root = tmp_path.resolve() / "orphan-recovery"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        frozen = freeze_dataset(request, store=store)

    class FailingAudit:
        def append(self, **kwargs: object) -> None:
            del kwargs
            raise AuditPersistenceError("injected audit failure")

    repository = DatasetManifestRepository(dataset_engine)
    repository._audit = FailingAudit()  # type: ignore[assignment]
    with pytest.raises(DatasetManifestPersistenceError, match="audit evidence"):
        repository.register(frozen)

    with dataset_engine.connect() as connection:
        assert connection.execute(select(aqa_dataset_manifests)).all() == []

    recovered = DatasetManifestRepository(dataset_engine).register(frozen)
    assert recovered.created is True


def test_registration_rejects_a_manifest_mutated_after_artifact_verification(
    tmp_path: Path,
    experiment: ExperimentDefinition,
    dataset_engine: Engine,
) -> None:
    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "tampered-registration"
    ) as store:
        frozen = freeze_dataset(_request(experiment), store=store)
    document = frozen.manifest
    document["provider"] = "tampered"
    tampered = replace(
        frozen,
        manifest_bytes=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
    )

    with pytest.raises(DatasetValidationError, match="hash"):
        DatasetManifestRepository(dataset_engine).register(tampered)


def test_effective_correction_changes_dataset_identity_and_summary(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    original = _request(experiment)
    correction = _request(
        experiment,
        bars=(
            _effective(
                "AMD",
                0,
                revision=2,
                event_digit="f",
                close=Decimal("100.75"),
                is_correction=True,
            ),
        ),
    )
    root = tmp_path.resolve() / "artifacts"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        first = freeze_dataset(original, store=store)
        corrected = freeze_dataset(correction, store=store)

    assert corrected.dataset_id != first.dataset_id
    assert corrected.logical_hash != first.logical_hash
    assert corrected.manifest["correction_summary"] == {
        "effective_corrected_rows": 1,
        "superseded_revision_count": 1,
    }
    assert _artifact_directory(root, first.artifact_id).is_dir()
    assert _artifact_directory(root, corrected.artifact_id).is_dir()


def test_return_to_same_values_at_later_revision_still_changes_identity(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    original_bar = _bar("AMD", 0)
    first = _request(
        experiment,
        bars=(EffectiveBar(bar_event_id=f"bar_event_{'1' * 64}", revision=1, bar=original_bar),),
    )
    returned = _request(
        experiment,
        bars=(EffectiveBar(bar_event_id=f"bar_event_{'3' * 64}", revision=3, bar=original_bar),),
    )
    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "artifacts"
    ) as store:
        first_result = freeze_dataset(first, store=store)
        returned_result = freeze_dataset(returned, store=store)

    assert returned_result.logical_hash != first_result.logical_hash
    assert returned_result.manifest["correction_summary"] == {
        "effective_corrected_rows": 1,
        "superseded_revision_count": 2,
    }


@pytest.mark.parametrize("diagnostic_reason", ["fixture", "dirty", "gap", "quality"])
def test_promotability_is_derived_fail_closed(
    diagnostic_reason: str,
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    bars = (_effective("AMD", 0),)
    gaps = DatasetGapSummary(1, 0, 0, 0)
    dirty = False
    if diagnostic_reason == "fixture":
        bars = (
            _effective(
                "AMD",
                0,
                provider="fixture",
                source_mode="offline_fixture",
            ),
        )
    elif diagnostic_reason == "dirty":
        dirty = True
    elif diagnostic_reason == "gap":
        gaps = DatasetGapSummary(2, 1, 1, 0)
    else:
        bars = (_effective("AMD", 0, quality_flags=("late",)),)
    request = _request(experiment, bars=bars, gap_summary=gaps, dirty_worktree=dirty)

    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / diagnostic_reason
    ) as store:
        frozen = freeze_dataset(request, store=store)

    assert frozen.promotable is False
    assert frozen.status is DatasetStatus.DIAGNOSTIC
    assert frozen.manifest["promotable"] is False
    assert frozen.manifest["status"] == "diagnostic"


def test_missing_middle_interval_cannot_be_claimed_as_complete(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    request = _request(
        experiment,
        bars=(
            _effective("AMD", 0, event_digit="1"),
            _effective("AMD", 2, event_digit="3"),
        ),
        gap_summary=DatasetGapSummary(
            expected_rows=3,
            missing_rows=1,
            unresolved_gap_count=0,
            repaired_gap_count=0,
        ),
    )

    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "missing-middle"
    ) as store:
        frozen = freeze_dataset(request, store=store)

    assert frozen.status is DatasetStatus.DIAGNOSTIC
    assert frozen.manifest["gap_summary"]["missing_rows"] == 1


def test_selected_symbol_with_no_rows_cannot_be_claimed_as_complete(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    request = _request(
        experiment,
        bars=(_effective("AMD", 0, event_digit="1"),),
        symbols=("AMD", "NVDA"),
        gap_summary=DatasetGapSummary(
            expected_rows=2,
            missing_rows=1,
            unresolved_gap_count=0,
            repaired_gap_count=0,
        ),
    )

    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "missing-symbol"
    ) as store:
        frozen = freeze_dataset(request, store=store)

    assert frozen.status is DatasetStatus.DIAGNOSTIC
    assert frozen.manifest["row_counts"] == [
        {"row_count": 1, "symbol": "AMD", "timeframe": "1Min"},
        {"row_count": 0, "symbol": "NVDA", "timeframe": "1Min"},
    ]


def test_freeze_rejects_count_identity_series_and_range_mismatches(
    experiment: ExperimentDefinition,
) -> None:
    base = _request(experiment)
    invalid_requests = (
        replace(base, gap_summary=DatasetGapSummary(2, 0, 0, 0)),
        replace(
            base,
            effective_bars=(base.effective_bars[0], replace(base.effective_bars[0], revision=2)),
            gap_summary=DatasetGapSummary(2, 0, 0, 0),
        ),
        replace(base, symbols=("NVDA",)),
        replace(base, range_start_utc=_START + timedelta(seconds=30)),
        replace(
            base,
            effective_bars=(
                base.effective_bars[0],
                _effective("AMD", 1, provider="fixture", source_mode="offline_fixture"),
            ),
            gap_summary=DatasetGapSummary(2, 0, 0, 0),
        ),
    )

    class UnusedStore:
        def load_dataset(self, artifact_id: str) -> None:
            raise AssertionError(artifact_id)

        def publish_dataset(self, artifact_id: str, **kwargs: object) -> None:
            raise AssertionError((artifact_id, kwargs))

    for request in invalid_requests:
        with pytest.raises(DatasetValidationError):
            freeze_dataset(request, store=UnusedStore())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "artifact_id",
    ["", "../escape", "a..b", "/absolute", "nested/path", "UPPER", "nul\x00id", "x" * 129],
)
def test_artifact_ids_cannot_select_paths(artifact_id: str, tmp_path: Path) -> None:
    with (
        LocalFilesystemArtifactStore(
            trusted_artifact_root=tmp_path.resolve() / "artifacts"
        ) as store,
        pytest.raises(ArtifactSecurityError, match="artifact ID"),
    ):
        store.load_dataset(artifact_id)


def test_symbolic_links_are_rejected_at_root_bucket_artifact_and_member(
    tmp_path: Path,
) -> None:
    canonical_root = tmp_path.resolve()
    outside = canonical_root / "outside"
    outside.mkdir()
    linked_root = canonical_root / "linked"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactSecurityError):
        LocalFilesystemArtifactStore(trusted_artifact_root=linked_root / "artifacts")

    root = canonical_root / "artifacts"
    artifact_id = "artifact_safe"
    bucket_name = hashlib.sha256(artifact_id.encode("ascii")).hexdigest()[:2]
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        (root / bucket_name).symlink_to(outside, target_is_directory=True)
        with pytest.raises(ArtifactSecurityError):
            store.load_dataset(artifact_id)
    (root / bucket_name).unlink()

    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        bucket = root / bucket_name
        bucket.mkdir()
        (bucket / artifact_id).symlink_to(outside, target_is_directory=True)
        with pytest.raises(ArtifactSecurityError):
            store.load_dataset(artifact_id)
    (bucket / artifact_id).unlink()

    payload = b"parquet"
    manifest = b"{}"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        store.publish_dataset(
            artifact_id,
            parquet_stream=io.BytesIO(payload),
            physical_hash=hashlib.sha256(payload).hexdigest(),
            parquet_size_bytes=len(payload),
            manifest_bytes=manifest,
        )
        member = _artifact_directory(root, artifact_id) / "bars.parquet"
        member.unlink()
        member.symlink_to(outside / "stolen")
        with pytest.raises(ArtifactSecurityError):
            store.load_dataset(artifact_id)


def test_existing_artifact_id_never_overwrites_different_content(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "artifacts"
    first = b"first parquet bytes"
    second = b"different parquet bytes"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        created = store.publish_dataset(
            "artifact_immutable",
            parquet_stream=io.BytesIO(first),
            physical_hash=hashlib.sha256(first).hexdigest(),
            parquet_size_bytes=len(first),
            manifest_bytes=b'{"version":1}',
        )
        repeated = store.publish_dataset(
            "artifact_immutable",
            parquet_stream=io.BytesIO(first),
            physical_hash=hashlib.sha256(first).hexdigest(),
            parquet_size_bytes=len(first),
            manifest_bytes=b'{"version":1}',
        )
        with pytest.raises(ArtifactConflictError):
            store.publish_dataset(
                "artifact_immutable",
                parquet_stream=io.BytesIO(second),
                physical_hash=hashlib.sha256(second).hexdigest(),
                parquet_size_bytes=len(second),
                manifest_bytes=b'{"version":2}',
            )

    assert created.created is True
    assert repeated.created is False
    assert (_artifact_directory(root, "artifact_immutable") / "bars.parquet").read_bytes() == first


def test_failed_atomic_rename_rolls_back_staging_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve() / "artifacts"
    payload = b"parquet"

    def fail_rename(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("injected publication failure")

    monkeypatch.setattr(datasets_module.os, "rename", fail_rename)
    with (
        LocalFilesystemArtifactStore(trusted_artifact_root=root) as store,
        pytest.raises(ArtifactSecurityError, match="published safely"),
    ):
        store.publish_dataset(
            "artifact_rollback",
            parquet_stream=io.BytesIO(payload),
            physical_hash=hashlib.sha256(payload).hexdigest(),
            parquet_size_bytes=len(payload),
            manifest_bytes=b"{}",
        )

    bucket = _artifact_directory(root, "artifact_rollback").parent
    assert sorted(path.name for path in bucket.iterdir()) == []


def test_physical_or_manifest_tampering_is_detected(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    root = tmp_path.resolve() / "artifacts"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        frozen = freeze_dataset(request, store=store)
        directory = _artifact_directory(root, frozen.artifact_id)
        (directory / "bars.parquet").write_bytes(b"tampered")
        with pytest.raises(ArtifactIntegrityError, match="Parquet content"):
            freeze_dataset(request, store=store)

    second_root = tmp_path.resolve() / "second"
    with LocalFilesystemArtifactStore(trusted_artifact_root=second_root) as store:
        frozen = freeze_dataset(request, store=store)
        directory = _artifact_directory(second_root, frozen.artifact_id)
        document = json.loads((directory / "manifest.json").read_bytes())
        document["row_count"] = 999
        (directory / "manifest.json").write_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        )
        with pytest.raises(ArtifactIntegrityError, match="manifest hash"):
            freeze_dataset(request, store=store)


def test_manifest_view_and_request_are_immutable(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    with pytest.raises(FrozenInstanceError):
        request.symbols = ("NVDA",)  # type: ignore[misc]

    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "artifacts"
    ) as store:
        frozen = freeze_dataset(request, store=store)

    first = frozen.manifest
    first["status"] = "tampered"
    assert frozen.manifest["status"] == "promotable"


def test_store_rejects_noncanonical_root_and_use_after_close(tmp_path: Path) -> None:
    with pytest.raises(ArtifactSecurityError):
        LocalFilesystemArtifactStore(trusted_artifact_root=Path("relative"))

    store = LocalFilesystemArtifactStore(trusted_artifact_root=tmp_path.resolve() / "artifacts")
    store.close()
    store.close()
    with pytest.raises(ArtifactSecurityError, match="closed"):
        store.load_dataset("artifact_closed")


def test_staged_source_hash_mismatch_publishes_nothing(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "artifacts"
    payload = b"actual"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        with pytest.raises(ArtifactIntegrityError, match="changed during publication"):
            store.publish_dataset(
                "artifact_hash_mismatch",
                parquet_stream=io.BytesIO(payload),
                physical_hash="0" * 64,
                parquet_size_bytes=len(payload),
                manifest_bytes=b"{}",
            )
        assert store.load_dataset("artifact_hash_mismatch") is None


def test_gap_summary_and_freeze_request_reject_malformed_metadata(
    experiment: ExperimentDefinition,
) -> None:
    with pytest.raises(DatasetValidationError):
        DatasetGapSummary(1, 2, 0, 0)
    with pytest.raises(DatasetValidationError):
        DatasetGapSummary(2, 1, 2, 0)
    with pytest.raises(DatasetValidationError):
        DatasetGapSummary(True, 0, 0, 0)

    base = _request(experiment)
    invalid = (
        {"symbols": ("AMD", "AMD")},
        {"symbols": ("AAPL",)},
        {"source_git_commit": "not-a-commit"},
        {"uv_lock_hash": "not-a-hash"},
        {"dirty_worktree": 0},
        {"created_at": datetime(2026, 9, 5)},
    )
    for changes in invalid:
        with pytest.raises(DatasetValidationError):
            replace(base, **changes)


def test_artifact_store_protocol_rejects_unrelated_objects(
    experiment: ExperimentDefinition,
) -> None:
    with pytest.raises(DatasetValidationError, match="artifact store"):
        freeze_dataset(_request(experiment), store=object())  # type: ignore[arg-type]


def test_artifact_permissions_are_not_world_accessible(
    tmp_path: Path,
    experiment: ExperimentDefinition,
) -> None:
    root = tmp_path.resolve() / "artifacts"
    with LocalFilesystemArtifactStore(trusted_artifact_root=root) as store:
        frozen = freeze_dataset(_request(experiment), store=store)
    directory = _artifact_directory(root, frozen.artifact_id)

    assert os.stat(directory).st_mode & 0o007 == 0
    assert os.stat(directory / "bars.parquet").st_mode & 0o007 == 0
    assert os.stat(directory / "manifest.json").st_mode & 0o007 == 0
