"""Replayable canonical aggregates, exact gaps, and calendar-aware readiness.

Dirty work is keyed by symbol/session, never by receipt time. One item reads at most
one regular session of minute identities and is acknowledged only after all derived
transactions commit. A concurrent intake increments its generation, keeping the item
pending for replay. The caller must include pending work in its readiness gate.

Symbol readiness verifies the complete history on cold start, then reuses a process-local
hash prefix when queued work and gap fingerprints exclude older changes. Historical
corrections invalidate that prefix. Bulk reads verify bounded immutable revision chains;
hashing happens outside leased database transactions. Publication checks the work/configuration/gap
fence in the same leased transaction as the watermark write. Concurrent intake invalidates
that snapshot and leaves work queued. No missing history or revision is skipped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from threading import Lock

from sqlalchemy import Connection, Engine, delete, event, func, select
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.collection.schema import canonical_work, collector_checkpoints
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.data.aggregation import EffectiveBar, SessionWindow
from adaptive_trader.platform.data.calendar import TradingInterval, XnasExchangeCalendar
from adaptive_trader.platform.data.materialization import FifteenMinuteMaterializer
from adaptive_trader.platform.data.normalization import CanonicalBar
from adaptive_trader.platform.data.watermarks import (
    DataGap,
    DataSeries,
    GapRepairCoverage,
    GapRepository,
    GapStatus,
    ReadinessPrefix,
    WatermarkRepository,
    compute_symbol_readiness_from_batches,
    detect_data_gaps,
)
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    MarketDataRepository,
    StoredBarEvent,
    read_effective_bars,
)
from adaptive_trader.platform.storage.tables import (
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_experiments,
)

_MAX_DRAIN_SESSIONS = 1_024
_MINUTE = timedelta(minutes=1)
_SNAPSHOT_BATCH_SIZE = 390
# PostgreSQL's one-bigint advisory key space is separate from the platform's
# two-integer transaction lock space. This identifies AQA's sole derived drain.
_DERIVED_ADVISORY_KEY = 0x4151415F44455249


class DerivedProcessingError(RuntimeError):
    """Derived state could not be verified; durable work remains pending."""


class DerivedSnapshotChanged(DerivedProcessingError):
    """Concurrent intake invalidated a snapshot; its work must be retried."""


@dataclass(frozen=True, slots=True)
class _SessionWork:
    symbol: str
    session_date: date
    generation: int
    through_at: datetime


class DerivedDataProcessor:
    """Converge data-only work without changing the immutable research universe.

    PostgreSQL drains hold an independent session advisory lock on a dedicated AUTOCOMMIT
    connection, avoiding both overlapping consumers and idle-in-transaction timeouts. The
    injected lease validator and that same backend connection are checked before each write.
    A private OptionEngine also calls ``transaction_guard(connection)`` before each data
    transaction. That callback must verify and lock the collector lease row on the supplied
    connection until commit, fencing takeover during the write. The parent engine and its
    lease-renewal thread are unaffected. No claim is made about arbitrary callback behavior
    or database failover outside these PostgreSQL transaction/connection boundaries. Offline
    SQLite callers must serialize distinct instances; one instance rejects overlapping drains.

    The processor receives no provider or trading authority. The eighteen collection-only instruments
    remain raw data; only the experiment's active, benchmark, and context instruments gain
    experiment-scoped aggregates and readiness records.
    """

    def __init__(
        self,
        engine: Engine,
        experiment: ExperimentDefinition,
        history_start: datetime,
        clock: Callable[[], datetime],
        *,
        lease_validator: Callable[[], None] | None = None,
        transaction_guard: Callable[[Connection], None] | None = None,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("derived processor requires a database engine")
        if type(experiment) is not ExperimentDefinition:
            raise TypeError("derived processor requires an immutable experiment")
        if not callable(clock):
            raise TypeError("derived processor requires an injected clock")
        if lease_validator is not None and not callable(lease_validator):
            raise TypeError("derived lease validator must be callable")
        if engine.dialect.name == "postgresql" and lease_validator is None:
            raise ValueError("PostgreSQL derived processing requires a current lease validator")
        if transaction_guard is not None and not callable(transaction_guard):
            raise TypeError("derived transaction guard must be callable")
        if engine.dialect.name == "postgresql" and transaction_guard is None:
            raise ValueError("PostgreSQL derived processing requires transaction fencing")
        market = experiment.market_data
        if (
            market.provider,
            market.feed,
            market.adjustment,
            market.source_timeframe,
            market.decision_timeframe,
            market.exchange_calendar,
            market.regular_hours_only,
        ) != ("alpaca", "iex", "raw", "1Min", "15Min", "XNAS", True):
            raise ValueError("derived processor requires the existing Alpaca IEX raw contract")
        self._input_engine = engine
        self._engine = engine.execution_options(aqa_derived_transaction=True)
        self._experiment = experiment
        self._history_start = require_utc_instant(history_start, field_name="history_start")
        self._clock = clock
        self._lease_validator = lease_validator
        self._transaction_guard = transaction_guard
        self._local_drain_lock = Lock()
        # Bounded by the immutable experiment's series inventory. Restart always
        # rebuilds these process-local prefixes from verified database events.
        self._readiness_prefixes: dict[
            DataSeries, tuple[ReadinessPrefix, tuple[tuple[str, int, str], ...]]
        ] = {}
        self._drain_connection: Connection | None = None
        self._drain_backend_pid: int | None = None
        if transaction_guard is not None:
            event.listen(self._engine, "begin", self._begin_derived_transaction)
        self._calendar = XnasExchangeCalendar()
        self._market = MarketDataRepository(self._engine)
        self._materializer = FifteenMinuteMaterializer.from_repository(self._market)
        self._gaps = GapRepository(self._engine, calendar=self._calendar)
        self._watermarks = WatermarkRepository(self._engine, calendar=self._calendar)

    def drain(self, limit: int = 29) -> int:
        """Process at most ``limit`` dirty sessions, oldest first, preserving failed work."""

        if type(limit) is not int or not 1 <= limit <= _MAX_DRAIN_SESSIONS:
            raise ValueError("derived session limit must be between 1 and 1024")
        self._validate_lease()
        if not self._local_drain_lock.acquire(blocking=False):
            return 0
        try:
            with self._exclusive_drain() as acquired:
                if not acquired:
                    return 0
                return self._drain_sessions(limit)
        except SQLAlchemyError:
            raise DerivedProcessingError("derived database operation failed") from None
        finally:
            self._local_drain_lock.release()

    def _drain_sessions(self, limit: int) -> int:
        self._guard()
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(canonical_work)
                        .order_by(canonical_work.c.session_date, canonical_work.c.symbol)
                        .limit(limit)
                    )
                    .mappings()
                    .all()
                )
            by_symbol: dict[str, list[_SessionWork]] = {}
            for row in rows:
                work = _SessionWork(
                    symbol=row["symbol"],
                    session_date=row["session_date"],
                    generation=row["generation"],
                    through_at=self._stored_timestamp(row["through_at"]),
                )
                if (
                    work.symbol not in COLLECTION_UNIVERSE_V1.symbols
                    or type(work.session_date) is not date
                    or type(work.generation) is not int
                    or work.generation < 1
                ):
                    raise DerivedProcessingError("dirty session identity is invalid")
                if work.symbol in self._experiment.collection_allowlist:
                    self._process_session(work)
                by_symbol.setdefault(work.symbol, []).append(work)
            completed = 0
            for symbol, work_items in by_symbol.items():
                if symbol in self._experiment.collection_allowlist:
                    try:
                        self._refresh_readiness(work_items[-1])
                    except DerivedSnapshotChanged:
                        continue
                for work in work_items:
                    self._acknowledge(work)
                    completed += 1
            return completed
        except SQLAlchemyError:
            raise DerivedProcessingError("derived database operation failed") from None

    def drain_backfill(self, limit: int = 29) -> int:
        """Keep bounded drain counts, but report finite completion only for an empty queue.

        Live drains deliberately return zero when another drain holds the advisory lock
        or intake invalidates every selected snapshot. A finite backfill must not treat
        either state as completed. On zero progress, check pending work under the current
        collector lease using the parent engine, without requiring our own advisory lock.
        """

        completed = self.drain(limit)
        if completed:
            return completed
        self._validate_lease()
        try:
            with self._input_engine.begin() as connection:
                if self._transaction_guard is not None:
                    self._transaction_guard(connection)
                pending = connection.scalar(select(canonical_work.c.symbol).limit(1))
                if pending is not None:
                    raise DerivedProcessingError("canonical backfill has pending derived work")
            self._validate_lease()
            return 0
        except SQLAlchemyError:
            raise DerivedProcessingError(
                "canonical backfill completion could not be verified"
            ) from None

    def _validate_lease(self) -> None:
        if self._lease_validator is not None:
            self._lease_validator()

    def _guard(self) -> None:
        self._validate_lease()
        self._check_lock_connection()

    def _check_lock_connection(self) -> None:
        connection = self._drain_connection
        if connection is not None:
            if connection.invalidated or connection.closed:
                raise DerivedProcessingError("derived lock connection was lost")
            if connection.scalar(select(func.pg_backend_pid())) != self._drain_backend_pid:
                raise DerivedProcessingError("derived lock backend changed")
        elif self._engine.dialect.name == "postgresql":
            raise DerivedProcessingError("derived processing has no session lock")

    def _begin_derived_transaction(self, connection: Connection) -> None:
        if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
            return
        self._check_lock_connection()
        if self._transaction_guard is not None:
            self._transaction_guard(connection)

    @contextmanager
    def _exclusive_drain(self) -> Iterator[bool]:
        if self._engine.dialect.name != "postgresql":
            yield True
            return
        with self._engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            try:
                acquired = connection.scalar(
                    select(func.pg_try_advisory_lock(_DERIVED_ADVISORY_KEY))
                )
            except SQLAlchemyError:
                connection.invalidate()
                raise
            if acquired is not True:
                self._validate_lease()
                yield False
                return
            completed_normally = False
            try:
                self._drain_connection = connection
                self._drain_backend_pid = connection.scalar(select(func.pg_backend_pid()))
                self._guard()
                yield True
                completed_normally = True
            finally:
                self._drain_connection = None
                self._drain_backend_pid = None
                try:
                    if connection.invalidated or connection.closed:
                        raise DerivedProcessingError("derived lock connection was lost")
                    released = connection.scalar(
                        select(func.pg_advisory_unlock(_DERIVED_ADVISORY_KEY))
                    )
                    if released is not True:
                        raise DerivedProcessingError("derived session lock could not be released")
                except (SQLAlchemyError, DerivedProcessingError):
                    # Never return an uncertain session lock to the connection pool.
                    # Preserve an existing phase failure; otherwise surface cleanup failure.
                    connection.invalidate()
                    if completed_normally:
                        raise DerivedProcessingError(
                            "derived session lock cleanup failed"
                        ) from None

    def _now(self) -> datetime:
        return require_utc_instant(self._clock(), field_name="derived_clock")

    def _stored_timestamp(self, value: object) -> datetime:
        if type(value) is not datetime:
            raise DerivedProcessingError("stored inspection timestamp is invalid")
        # SQLite drops DateTime(timezone=True)'s marker; only this known storage
        # representation is restored. Public inputs and PostgreSQL remain strict.
        if self._engine.dialect.name == "sqlite" and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return require_utc_instant(value, field_name="stored_inspection_timestamp")

    def _series(self, symbol: str, timeframe: str) -> DataSeries:
        return DataSeries("alpaca", "iex", "raw", symbol, timeframe)

    def _read_intervals(
        self, series: DataSeries, intervals: tuple[TradingInterval, ...]
    ) -> tuple[StoredBarEvent, ...]:
        self._guard()
        if not intervals:
            return ()
        if len(intervals) > _SNAPSHOT_BATCH_SIZE:
            raise DerivedProcessingError("canonical read exceeds its bounded identity batch")
        identities = tuple(
            BarIdentity(
                series.provider,
                series.feed,
                series.adjustment,
                series.symbol,
                series.timeframe,
                interval.start_at,
                interval.end_at,
            )
            for interval in intervals
        )
        with self._engine.begin() as connection:
            return read_effective_bars(connection, identities)

    def _process_session(self, work: _SessionWork) -> None:
        self._guard()
        session = self._calendar.session(work.session_date)
        if session is None:
            return
        # A late repair can be the earliest minute of a previously wider gap.
        # Revisit the full known portion of that session so the original exact
        # gap can resolve even when this delivery's own end is earlier.
        end = min(session.close_at, self._inspected_through(work))
        start = max(session.open_at, self._history_start)
        if end <= start:
            return
        minutes = self._calendar.expected_intervals(start_at=start, end_at=end, timeframe="1Min")
        minute_series = self._series(work.symbol, "1Min")
        events = self._read_intervals(minute_series, minutes)
        self._synchronize_gaps(minute_series, start, end, events)
        by_start = {event.identity.start_at: event for event in events}
        buckets = self._calendar.expected_intervals(start_at=start, end_at=end, timeframe="15Min")
        for bucket in buckets:
            constituents = tuple(
                by_start[bucket.start_at + offset * _MINUTE]
                for offset in range(15)
                if bucket.start_at + offset * _MINUTE in by_start
            )
            if len(constituents) == 15:
                self._guard()
                self._materializer.materialize(
                    tuple(stored_as_effective(event) for event in constituents),
                    session=SessionWindow(session.open_at, session.close_at),
                )
        aggregate_series = self._series(work.symbol, "15Min")
        aggregates = self._read_intervals(aggregate_series, buckets)
        self._synchronize_gaps(aggregate_series, start, end, aggregates)

    def _synchronize_gaps(
        self,
        series: DataSeries,
        start: datetime,
        end: datetime,
        events: tuple[StoredBarEvent, ...],
    ) -> None:
        observed = tuple(
            TradingInterval(event.identity.start_at, event.identity.end_at) for event in events
        )
        for detection in detect_data_gaps(
            calendar=self._calendar,
            experiment_hash=self._experiment.content_hash,
            series=series,
            start_at=start,
            end_at=end,
            observed_intervals=observed,
            detected_at=self._now(),
        ):
            self._guard()
            self._gaps.record(detection)
        for gap in self._gaps.list_unresolved(
            experiment_hash=self._experiment.content_hash, series=series
        ):
            if gap.start_at < start or gap.end_at > end:
                continue
            expected = self._calendar.expected_intervals(
                start_at=gap.start_at, end_at=gap.end_at, timeframe=series.timeframe
            )
            actual = tuple(
                interval
                for interval in observed
                if gap.start_at <= interval.start_at and interval.end_at <= gap.end_at
            )
            if actual != expected:
                continue
            if gap.status is GapStatus.OPEN:
                self._guard()
                self._gaps.begin_repair(gap.gap_id, attempted_at=self._now())
            self._guard()
            self._gaps.complete_repair(
                gap.gap_id,
                coverage=GapRepairCoverage(
                    series=series,
                    start_at=gap.start_at,
                    end_at=gap.end_at,
                    observed_intervals=actual,
                    completed_at=self._now(),
                ),
            )

    def _inspected_through(self, work: _SessionWork) -> datetime:
        self._guard()
        # Old corrections must re-evaluate later known coverage rather than
        # resetting readiness to the corrected session's end.
        with self._engine.connect() as connection:
            checkpoint = connection.scalar(
                select(func.max(collector_checkpoints.c.committed_through_utc)).where(
                    collector_checkpoints.c.checkpoint_name == "rest_coverage",
                    collector_checkpoints.c.provider == "alpaca",
                    collector_checkpoints.c.feed == "IEX",
                    collector_checkpoints.c.adjustment == "raw",
                    collector_checkpoints.c.symbol == work.symbol,
                    collector_checkpoints.c.timeframe == "1m",
                )
            )
            latest_end = connection.scalar(
                select(func.max(aqa_bar_identities.c.end_at))
                .select_from(aqa_bar_identities.join(aqa_bar_latest))
                .where(
                    aqa_bar_identities.c.provider == "alpaca",
                    aqa_bar_identities.c.feed == "iex",
                    aqa_bar_identities.c.adjustment == "raw",
                    aqa_bar_identities.c.symbol == work.symbol,
                    aqa_bar_identities.c.timeframe == "1Min",
                )
            )
        bounds = [work.through_at]
        bounds.extend(
            self._stored_timestamp(value) for value in (checkpoint, latest_end) if value is not None
        )
        return min(max(bounds), self._now())

    def _refresh_readiness(self, work: _SessionWork) -> None:
        for timeframe in ("1Min", "15Min"):
            series = self._series(work.symbol, timeframe)
            self._guard()
            # Capture before reading any history, not after it. Only this advisory-
            # locked consumer can delete queue rows, preventing a generation ABA.
            with self._engine.begin() as connection:
                source_fence, gaps = self._snapshot_fence(connection, series)
                earliest_dirty = connection.scalar(
                    select(func.min(canonical_work.c.session_date)).where(
                        canonical_work.c.symbol == series.symbol
                    )
                )
            end = self._inspected_through(work)
            if end <= self._history_start:
                continue

            def prefix_gaps(
                before: datetime, selected_gaps: tuple[DataGap, ...] = gaps
            ) -> tuple[tuple[str, int, str], ...]:
                return tuple(
                    (gap.gap_id, gap.version, gap.content_hash)
                    for gap in selected_gaps
                    if gap.start_at < before
                )

            cached = self._readiness_prefixes.pop(series, None)
            prefix = None
            if cached is not None:
                candidate, previous_gaps = cached
                if (
                    earliest_dirty is not None
                    and earliest_dirty >= candidate.resume_at.date()
                    and candidate.resume_at <= end
                    and previous_gaps == prefix_gaps(candidate.resume_at)
                ):
                    prefix = candidate
            scan_start = self._history_start if prefix is None else prefix.resume_at
            checkpoint_at = end.replace(hour=0, minute=0, second=0, microsecond=0)
            checkpoints: list[ReadinessPrefix] = []

            def interval_batches(
                _end: datetime = end,
                _timeframe: str = timeframe,
                _start: datetime = scan_start,
            ) -> Iterator[tuple[TradingInterval, ...]]:
                cursor = _start
                while cursor < _end:
                    midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
                    boundary = min(midnight + timedelta(days=1), _end)
                    yield self._calendar.expected_intervals(
                        start_at=cursor, end_at=boundary, timeframe=_timeframe
                    )
                    cursor = boundary

            # Recreate bounded calendar batches for the event reader; neither traversal
            # retains the full archive's expected-minute inventory in memory.
            expected = (interval for batch in interval_batches() for interval in batch)

            first = next(expected, None)
            if first is None and prefix is None:
                continue
            readiness = compute_symbol_readiness_from_batches(
                series=series,
                expected_intervals=chain(() if first is None else (first,), expected),
                effective_event_batches=(
                    events
                    for batch in interval_batches()
                    for events in self._snapshot_batches(series, batch)
                ),
                unresolved_gaps=gaps,
                prefix=prefix,
                checkpoint_at=checkpoint_at,
                checkpoint_sink=checkpoints.append,
            )

            def verify_snapshot(
                connection: Connection,
                *,
                selected_series: DataSeries = series,
                expected_fence: str = source_fence,
            ) -> None:
                if self._snapshot_fence(connection, selected_series)[0] != expected_fence:
                    raise DerivedSnapshotChanged("canonical input changed during readiness scan")

            self._guard()
            self._watermarks.publish_symbol_snapshot(
                experiment_hash=self._experiment.content_hash,
                readiness=readiness,
                updated_at=self._now(),
                verify_snapshot=verify_snapshot,
            )
            self._guard()
            self._watermarks.recompute_active_basket(
                experiment_hash=self._experiment.content_hash,
                universe=self._experiment,
                provider="alpaca",
                feed="iex",
                adjustment="raw",
                timeframe=timeframe,
                updated_at=self._now(),
                required_through=readiness.range_end_at,
            )
            if checkpoints:
                checkpoint = checkpoints[-1]
                self._readiness_prefixes[series] = (checkpoint, prefix_gaps(checkpoint.resume_at))

    def _snapshot_batches(
        self, series: DataSeries, expected: tuple[TradingInterval, ...]
    ) -> Iterator[tuple[StoredBarEvent, ...]]:
        for offset in range(0, len(expected), _SNAPSHOT_BATCH_SIZE):
            yield self._read_intervals(series, expected[offset : offset + _SNAPSHOT_BATCH_SIZE])

    def _snapshot_fence(
        self, connection: Connection, series: DataSeries
    ) -> tuple[str, tuple[DataGap, ...]]:
        experiment = (
            connection.execute(
                select(aqa_experiments).where(
                    aqa_experiments.c.experiment_hash == self._experiment.content_hash
                )
            )
            .mappings()
            .one_or_none()
        )
        if experiment is None or (
            experiment["content_hash"] != self._experiment.content_hash
            or sha256_hex(experiment["configuration"]) != self._experiment.content_hash
        ):
            raise DerivedProcessingError("registered experiment configuration is inconsistent")
        work_rows = (
            connection.execute(
                select(canonical_work)
                .where(canonical_work.c.symbol == series.symbol)
                .order_by(canonical_work.c.session_date)
            )
            .mappings()
            .all()
        )
        checkpoint = connection.scalar(
            select(func.max(collector_checkpoints.c.committed_through_utc)).where(
                collector_checkpoints.c.checkpoint_name == "rest_coverage",
                collector_checkpoints.c.provider == series.provider,
                collector_checkpoints.c.feed == "IEX",
                collector_checkpoints.c.adjustment == series.adjustment,
                collector_checkpoints.c.symbol == series.symbol,
                collector_checkpoints.c.timeframe == "1m",
            )
        )
        gaps = self._gaps.list_unresolved(
            experiment_hash=self._experiment.content_hash, series=series, connection=connection
        )
        digest = hashlib.sha256(
            canonical_json_bytes(
                (
                    "canonical_readiness_source_v1",
                    self._experiment.content_hash,
                    series.hash_input,
                    self._history_start,
                    None if checkpoint is None else self._stored_timestamp(checkpoint),
                )
            )
        )
        for row in work_rows:
            digest.update(b"\x00work\x00")
            digest.update(
                canonical_json_bytes(
                    (
                        row["session_date"].isoformat(),
                        row["generation"],
                        self._stored_timestamp(row["through_at"]),
                    )
                )
            )
        for gap in gaps:
            digest.update(b"\x00gap\x00")
            digest.update(canonical_json_bytes((gap.gap_id, gap.version, gap.content_hash)))
        return digest.hexdigest(), gaps

    def _acknowledge(self, work: _SessionWork) -> None:
        self._guard()
        with self._engine.begin() as connection:
            connection.execute(
                delete(canonical_work).where(
                    canonical_work.c.symbol == work.symbol,
                    canonical_work.c.session_date == work.session_date,
                    canonical_work.c.generation == work.generation,
                )
            )


def stored_as_effective(event: StoredBarEvent) -> EffectiveBar:
    """Convert a verified canonical event without crossing research orchestration."""
    stored = event.bar
    if stored.source_event_id is None:
        raise DerivedProcessingError("canonical minute source identity is missing")
    return EffectiveBar(
        bar_event_id=event.bar_event_id,
        revision=event.revision,
        bar=CanonicalBar(
            provider=stored.identity.provider,
            feed=stored.identity.feed,
            adjustment=stored.identity.adjustment,
            symbol=stored.identity.symbol,
            timeframe=stored.identity.timeframe,
            source_mode=stored.source_mode,
            interval_start_utc=stored.identity.start_at,
            interval_end_utc=stored.identity.end_at,
            receipt_timestamp_utc=stored.received_at,
            provider_event_timestamp_utc=stored.provider_timestamp,
            open=stored.open,
            high=stored.high,
            low=stored.low,
            close=stored.close,
            volume=stored.volume,
            trade_count=stored.trade_count,
            vwap=stored.vwap,
            schema_version=stored.schema_version,
            source_event_id=stored.source_event_id,
            quality_flags=stored.quality_flags,
            is_correction=stored.is_correction,
            correction_of_source_event_id=stored.correction_of_source_event_id,
        ),
    )
