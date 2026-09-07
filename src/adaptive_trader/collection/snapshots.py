"""Bounded, consistent canonical PostgreSQL snapshots with explicit metadata uncertainty."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.collection.derived import stored_as_effective
from adaptive_trader.collection.schema import (
    canonical_work,
    collector_checkpoints,
    collector_configuration,
)
from adaptive_trader.collection.service import completed_bar_cutoff
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.data.aggregation import EffectiveBar
from adaptive_trader.platform.data.calendar import TradingInterval, XnasExchangeCalendar
from adaptive_trader.platform.data.datasets import (
    CollectionDatasetProvenance,
    DatasetFreezeRequest,
    DatasetGapSummary,
    DatasetValidationError,
    FrozenDataset,
    LocalFilesystemArtifactStore,
    SnapshotMetadataEvidence,
)
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.storage.datasets import (
    DatasetManifestRegistration,
    DatasetManifestRepository,
    freeze_and_register_dataset,
)
from adaptive_trader.platform.storage.market_data import BarIdentity, read_effective_bars
from adaptive_trader.platform.storage.tables import aqa_data_gaps

_MAX_DAYS = 31
_MAX_ROWS = 250_000
_READ_BATCH = 390


def parse_snapshot_metadata(payload: object) -> SnapshotMetadataEvidence:
    """Validate an operator-supplied metadata document; never infer provider verification."""

    return SnapshotMetadataEvidence.from_payload(payload)


def freeze_collection_snapshot(
    engine: Engine,
    *,
    experiment: ExperimentDefinition,
    artifact_root: Path,
    range_start: datetime,
    range_end: datetime,
    source_git_commit: str,
    dirty_worktree: bool,
    uv_lock_hash: str,
    created_at: datetime,
    diagnostic: bool = False,
    metadata_evidence: SnapshotMetadataEvidence | None = None,
) -> tuple[FrozenDataset, DatasetManifestRegistration]:
    """Freeze the exact research/context minute selection from one consistent data view.

    Reads are bounded to 31 calendar days and 250,000 expected rows, using PostgreSQL
    REPEATABLE READ READ ONLY. SQLite is supported only for isolated fixture tests. Publication
    and registration follow the existing immutable orphan/retry contract after releasing the
    read transaction. Unknown metadata, missing minutes, dirty work, unresolved gaps and lagging
    coverage require explicit diagnostic export and cannot produce promotable artifacts.
    """

    if not isinstance(engine, Engine) or engine.dialect.name not in {"postgresql", "sqlite"}:
        raise DatasetValidationError("collection snapshot requires PostgreSQL or isolated SQLite")
    if type(experiment) is not ExperimentDefinition or type(diagnostic) is not bool:
        raise DatasetValidationError("collection snapshot configuration is invalid")
    market = experiment.market_data
    if (
        market.provider,
        market.feed,
        market.adjustment,
        market.source_timeframe,
        market.exchange_calendar,
        market.regular_hours_only,
    ) != ("alpaca", "iex", "raw", "1Min", "XNAS", True):
        raise DatasetValidationError(
            "collection snapshot requires canonical Alpaca IEX raw minutes"
        )
    if set(experiment.collection_allowlist) | set(experiment.excluded) != set(
        COLLECTION_UNIVERSE_V1.symbols
    ):
        raise DatasetValidationError("snapshot collection and research universes disagree")
    start = require_utc_instant(range_start, field_name="snapshot_start")
    end = require_utc_instant(range_end, field_name="snapshot_end")
    created = require_utc_instant(created_at, field_name="snapshot_created_at")
    if (
        not timedelta(0) < end - start <= timedelta(days=_MAX_DAYS)
        or start.second
        or start.microsecond
        or end.second
        or end.microsecond
        or end > completed_bar_cutoff(created, lag_minutes=2)
    ):
        raise DatasetValidationError("snapshot range must contain completed minutes within 31 days")
    symbols = tuple(sorted(experiment.collection_allowlist))
    calendar = XnasExchangeCalendar()
    intervals = calendar.expected_intervals(start_at=start, end_at=end, timeframe="1Min")
    expected_rows = len(intervals) * len(symbols)
    if not 0 < expected_rows <= _MAX_ROWS:
        raise DatasetValidationError("snapshot expected row count is outside its bounded range")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
            with connection.begin():
                if engine.dialect.name == "sqlite":
                    # pysqlite otherwise defers BEGIN until the first write, allowing torn reads.
                    connection.exec_driver_sql("BEGIN")
                bars, gaps, provenance = _read_snapshot(
                    connection,
                    experiment=experiment,
                    symbols=symbols,
                    intervals=intervals,
                    start=start,
                    end=end,
                    metadata_evidence=metadata_evidence,
                )
    except SQLAlchemyError:
        raise DatasetValidationError("collection snapshot database read failed") from None
    if not bars:
        raise DatasetValidationError("collection snapshot has no canonical observations")
    if not diagnostic and (
        gaps.missing_rows
        or not provenance.permits_promotion
        or dirty_worktree
        or any(
            not bar.bar.has_promotable_provenance or "complete" not in bar.bar.quality_flags
            for bar in bars
        )
    ):
        raise DatasetValidationError(
            "collection snapshot is not promotable; explicit diagnostic export is required"
        )
    request = DatasetFreezeRequest(
        experiment=experiment,
        symbols=symbols,
        effective_bars=bars,
        range_start_utc=start,
        range_end_utc=end,
        gap_summary=gaps,
        source_git_commit=source_git_commit,
        dirty_worktree=dirty_worktree,
        uv_lock_hash=uv_lock_hash,
        created_at=created,
        collection_provenance=provenance,
        diagnostic_only=diagnostic,
    )
    with LocalFilesystemArtifactStore(trusted_artifact_root=artifact_root) as store:
        return freeze_and_register_dataset(
            request, store=store, repository=DatasetManifestRepository(engine), calendar=calendar
        )


def _read_snapshot(
    connection: Connection,
    *,
    experiment: ExperimentDefinition,
    symbols: tuple[str, ...],
    intervals: tuple[TradingInterval, ...],
    start: datetime,
    end: datetime,
    metadata_evidence: SnapshotMetadataEvidence | None,
) -> tuple[tuple[EffectiveBar, ...], DatasetGapSummary, CollectionDatasetProvenance]:
    configuration = (
        connection.execute(
            select(collector_configuration).where(collector_configuration.c.name == "canonical")
        )
        .mappings()
        .one_or_none()
    )
    if (
        configuration is None
        or configuration["universe_hash"] != COLLECTION_UNIVERSE_V1.universe_hash
        or configuration["experiment_hash"] != experiment.content_hash
    ):
        raise DatasetValidationError(
            "snapshot durable collection configuration is missing or inconsistent"
        )
    history_start = _stored_utc(configuration["history_start"])
    if history_start > start:
        raise DatasetValidationError("snapshot range precedes the configured collection history")
    bars: list[EffectiveBar] = []
    for symbol in symbols:
        for offset in range(0, len(intervals), _READ_BATCH):
            identities = tuple(
                BarIdentity(
                    "alpaca", "iex", "raw", symbol, "1Min", interval.start_at, interval.end_at
                )
                for interval in intervals[offset : offset + _READ_BATCH]
            )
            bars.extend(
                stored_as_effective(row) for row in read_effective_bars(connection, identities)
            )
    pending = connection.scalar(
        select(func.count())
        .select_from(canonical_work)
        .where(
            canonical_work.c.symbol.in_(symbols),
            canonical_work.c.session_date >= start.date(),
            canonical_work.c.session_date <= end.date(),
        )
    )
    checkpoints = {
        str(row[0]): _stored_utc(row[1])
        for row in connection.execute(
            select(
                collector_checkpoints.c.symbol, collector_checkpoints.c.committed_through_utc
            ).where(
                collector_checkpoints.c.checkpoint_name == "rest_coverage",
                collector_checkpoints.c.provider == "alpaca",
                collector_checkpoints.c.feed == "IEX",
                collector_checkpoints.c.adjustment == "raw",
                collector_checkpoints.c.timeframe == "1m",
                collector_checkpoints.c.symbol.in_(symbols),
            )
        ).all()
    }
    lagging = tuple(
        symbol
        for symbol in symbols
        if symbol not in checkpoints or _stored_utc(checkpoints[symbol]) < intervals[-1].end_at
    )
    gap_rows = (
        connection.execute(
            select(aqa_data_gaps)
            .where(
                aqa_data_gaps.c.experiment_hash == experiment.content_hash,
                aqa_data_gaps.c.provider == "alpaca",
                aqa_data_gaps.c.feed == "iex",
                aqa_data_gaps.c.adjustment == "raw",
                aqa_data_gaps.c.timeframe == "1Min",
                aqa_data_gaps.c.symbol.in_(symbols),
                aqa_data_gaps.c.gap_start_at < end,
                aqa_data_gaps.c.gap_end_at > start,
            )
            .order_by(aqa_data_gaps.c.gap_id)
            .limit(_MAX_ROWS + 1)
        )
        .mappings()
        .all()
    )
    if len(gap_rows) > _MAX_ROWS:
        raise DatasetValidationError("snapshot persisted gaps exceed its bounded range")
    missing = len(intervals) * len(symbols) - len(bars)
    observed = {(bar.bar.symbol, bar.bar.interval_start_utc) for bar in bars}
    missing_runs = 0
    for symbol in symbols:
        previous_missing_end = None
        for interval in intervals:
            if (symbol, interval.start_at) not in observed:
                if previous_missing_end != interval.start_at:
                    missing_runs += 1
                previous_missing_end = interval.end_at
            else:
                previous_missing_end = None
    gaps = DatasetGapSummary(
        expected_rows=len(intervals) * len(symbols),
        missing_rows=missing,
        unresolved_gap_count=missing_runs,
        repaired_gap_count=sum(row["status"] == "resolved" for row in gap_rows),
    )
    roles = {symbol: "collection_only" for symbol in COLLECTION_UNIVERSE_V1.symbols}
    for label, members in (
        ("active", experiment.active_tradable),
        ("benchmark", experiment.benchmark_only),
        ("context", experiment.context_only),
    ):
        roles.update({symbol: label for symbol in members})
    gap_digest = hashlib.sha256(b"aqa.collection.snapshot.gaps.v1\x00")
    for row in gap_rows:
        payload = canonical_json_bytes((row["gap_id"], row["status"], row["content_hash"]))
        gap_digest.update(len(payload).to_bytes(8, "big"))
        gap_digest.update(payload)
    provenance = CollectionDatasetProvenance(
        universe_version=COLLECTION_UNIVERSE_V1.SCHEMA_VERSION,
        universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
        members=tuple(
            (member.symbol, member.role.value, roles[member.symbol])
            for member in sorted(COLLECTION_UNIVERSE_V1.members, key=lambda member: member.symbol)
        ),
        calendar_name="XNAS",
        calendar_version=version("exchange-calendars"),
        history_start_utc=history_start,
        pending_derived_sessions=int(pending or 0),
        persisted_gap_state_hash=gap_digest.hexdigest(),
        unresolved_persisted_gaps=sum(row["status"] != "resolved" for row in gap_rows),
        lagging_checkpoint_symbols=lagging,
        metadata_evidence=metadata_evidence,
    )
    return tuple(bars), gaps, provenance


def _stored_utc(value: Any) -> datetime:
    if type(value) is not datetime:
        raise DatasetValidationError("snapshot stored timestamp is invalid")
    return require_utc_instant(
        value.replace(tzinfo=UTC) if value.tzinfo is None else value, field_name="stored_timestamp"
    )
