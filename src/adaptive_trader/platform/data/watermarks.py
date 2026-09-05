"""Calendar-aware gap lifecycle and deterministic contiguous-readiness persistence."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adaptive_trader.platform.constants import MAX_SIGNED_64_BIT_INTEGER
from adaptive_trader.platform.data.calendar import ExchangeCalendar, TradingInterval
from adaptive_trader.platform.domain import AuditPayload, AuditWriter, require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    MarketDataIntegrityError,
    MarketDataPersistenceError,
    MarketDataRepository,
    MarketDataValidationError,
    StoredBarEvent,
    SymbolWatermark,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_bar_latest,
    aqa_basket_watermarks,
    aqa_data_gaps,
    aqa_symbol_watermarks,
)
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
    TransactionBoundaryError,
)
from adaptive_trader.platform.universe import UniverseSpec

_SUPPORTED_DIALECTS = frozenset({"postgresql", "sqlite"})
_HASH = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]*$", re.ASCII)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", re.ASCII)
_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", re.ASCII)
_QUALITY_FLAG = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", re.ASCII)
_GAP_ID = re.compile(r"^gap_[0-9a-f]{64}$", re.ASCII)
_BASKET_ID = re.compile(r"^basket_watermark_[0-9a-f]{64}$", re.ASCII)
_TIMEFRAME_DURATIONS = {
    "1Min": timedelta(minutes=1),
    "15Min": timedelta(minutes=15),
}
_SUPPORTED_PROVIDERS = frozenset({"alpaca", "fixture"})
_SUPPORTED_FEED = "iex"
_SUPPORTED_ADJUSTMENT = "raw"
_MAX_SIGNED_32_BIT_INTEGER = (2**31) - 1


class ReadinessValidationError(DomainValidationError):
    """Raised when gap or watermark input violates a public contract."""


class ReadinessIntegrityError(RuntimeError):
    """Raised when persisted gap or watermark state cannot be trusted."""


class ReadinessPersistenceError(RuntimeError):
    """Raised when a gap or watermark transaction fails safely."""


class GapStatus(StrEnum):
    """Closed durable lifecycle for one calendar-valid missing interval range."""

    OPEN = "open"
    REPAIRING = "repairing"
    RESOLVED = "resolved"
    WAIVED = "waived"


class BasketStatus(StrEnum):
    """Closed durable readiness states for the active-symbol basket."""

    READY = "ready"
    BLOCKED = "blocked"


def _require_token(value: object, *, field_name: str, maximum_length: int) -> str:
    if (
        type(value) is not str
        or len(value) > maximum_length
        or _LOWER_TOKEN.fullmatch(value) is None
    ):
        raise ReadinessValidationError(f"data-series {field_name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DataSeries:
    """Exact provider identity used for gaps and contiguous readiness."""

    provider: str
    feed: str
    adjustment: str
    symbol: str
    timeframe: str

    def __post_init__(self) -> None:
        _require_token(self.provider, field_name="provider", maximum_length=32)
        _require_token(self.feed, field_name="feed", maximum_length=16)
        _require_token(self.adjustment, field_name="adjustment", maximum_length=16)
        if self.provider not in _SUPPORTED_PROVIDERS:
            raise ReadinessValidationError("data-series provider is unsupported")
        if self.feed != _SUPPORTED_FEED:
            raise ReadinessValidationError("data-series feed must be exact IEX without fallback")
        if self.adjustment != _SUPPORTED_ADJUSTMENT:
            raise ReadinessValidationError("data-series adjustment must be raw")
        if type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None:
            raise ReadinessValidationError("data-series symbol is invalid")
        _timeframe_duration(self.timeframe)

    @property
    def hash_input(self) -> tuple[str, str, str, str, str]:
        return (self.provider, self.feed, self.adjustment, self.symbol, self.timeframe)


@dataclass(frozen=True, slots=True)
class GapDetection:
    """A contiguous, same-session group of expected intervals missing from storage.

    ``gap_id`` is exactly ``gap_`` plus SHA-256 of ``(experiment_hash, provider, feed,
    adjustment, symbol, timeframe, start_at, end_at)``.
    """

    experiment_hash: str
    series: DataSeries
    start_at: datetime
    end_at: datetime
    reason_code: str
    detected_at: datetime

    def __post_init__(self) -> None:
        _require_hash(self.experiment_hash, field_name="experiment hash")
        if type(self.series) is not DataSeries:
            raise ReadinessValidationError("gap series is invalid")
        start_at, end_at = _require_utc_range(self.start_at, self.end_at)
        if type(self.reason_code) is not str or _REASON.fullmatch(self.reason_code) is None:
            raise ReadinessValidationError("gap reason code is invalid")
        detected_at = _require_utc(self.detected_at, field_name="detected_at")
        if detected_at < end_at:
            raise ReadinessValidationError("gap cannot be detected before its interval ends")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)
        object.__setattr__(self, "detected_at", detected_at)

    @property
    def gap_id(self) -> str:
        return _gap_id(self.experiment_hash, self.series, self.start_at, self.end_at)


@dataclass(frozen=True, slots=True)
class DataGap:
    """Validated durable state for a missing range and its repair attempts."""

    gap_id: str
    experiment_hash: str
    series: DataSeries
    start_at: datetime
    end_at: datetime
    status: GapStatus
    reason_code: str
    attempt_count: int
    detected_at: datetime
    last_attempt_at: datetime | None
    resolved_at: datetime | None
    content_hash: str
    version: int

    def __post_init__(self) -> None:
        _require_hash(self.experiment_hash, field_name="experiment hash")
        if type(self.series) is not DataSeries:
            raise ReadinessIntegrityError("persisted gap series is invalid")
        start_at, end_at = _require_utc_range(self.start_at, self.end_at)
        detected_at = _require_utc(self.detected_at, field_name="detected_at")
        last_attempt_at = _optional_utc(self.last_attempt_at, field_name="last_attempt_at")
        resolved_at = _optional_utc(self.resolved_at, field_name="resolved_at")
        if type(self.status) is not GapStatus:
            raise ReadinessIntegrityError("persisted gap status is invalid")
        if type(self.reason_code) is not str or _REASON.fullmatch(self.reason_code) is None:
            raise ReadinessIntegrityError("persisted gap reason is invalid")
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise ReadinessIntegrityError("persisted gap attempt count is invalid")
        if self.attempt_count > _MAX_SIGNED_32_BIT_INTEGER:
            raise ReadinessIntegrityError("persisted gap attempt count is invalid")
        if (
            type(self.version) is not int
            or self.version < 1
            or self.version > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise ReadinessIntegrityError("persisted gap version is invalid")
        if detected_at < end_at:
            raise ReadinessIntegrityError("persisted gap predates its missing interval")
        if self.attempt_count == 0 and last_attempt_at is not None:
            raise ReadinessIntegrityError("unattempted gap has a repair timestamp")
        if self.attempt_count > 0 and last_attempt_at is None:
            raise ReadinessIntegrityError("attempted gap is missing its repair timestamp")
        if last_attempt_at is not None and last_attempt_at < detected_at:
            raise ReadinessIntegrityError("gap repair timestamp precedes detection")
        if self.status is GapStatus.RESOLVED:
            if resolved_at is None or last_attempt_at is None or resolved_at < last_attempt_at:
                raise ReadinessIntegrityError("resolved gap timestamps are inconsistent")
        elif resolved_at is not None:
            raise ReadinessIntegrityError("unresolved gap has a resolution timestamp")
        expected_id = _gap_id(self.experiment_hash, self.series, start_at, end_at)
        if type(self.gap_id) is not str or self.gap_id != expected_id:
            raise ReadinessIntegrityError("persisted gap identity is invalid")
        _require_integrity_hash(self.content_hash, field_name="gap content hash")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)
        object.__setattr__(self, "detected_at", detected_at)
        object.__setattr__(self, "last_attempt_at", last_attempt_at)
        object.__setattr__(self, "resolved_at", resolved_at)
        if self.content_hash != _gap_content_hash(self):
            raise ReadinessIntegrityError("persisted gap content hash is invalid")

    @property
    def unresolved(self) -> bool:
        return self.status in {GapStatus.OPEN, GapStatus.REPAIRING}


@dataclass(frozen=True, slots=True)
class GapRepairCoverage:
    """Bounded repair result; exact identity prevents feed or adjustment drift."""

    series: DataSeries
    start_at: datetime
    end_at: datetime
    observed_intervals: tuple[TradingInterval, ...]
    completed_at: datetime
    unambiguous: bool = True

    def __post_init__(self) -> None:
        if type(self.series) is not DataSeries:
            raise ReadinessValidationError("repair series is invalid")
        start_at, end_at = _require_utc_range(self.start_at, self.end_at)
        intervals = _require_intervals(self.observed_intervals, self.series.timeframe)
        completed_at = _require_utc(self.completed_at, field_name="completed_at")
        if type(self.unambiguous) is not bool:
            raise ReadinessValidationError("repair ambiguity flag is invalid")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)
        object.__setattr__(self, "observed_intervals", intervals)
        object.__setattr__(self, "completed_at", completed_at)


@dataclass(frozen=True, slots=True)
class BarQualityPolicy:
    """Versioned exact-match quality gate whose identity is included in watermark hashes."""

    policy_id: str
    eligible_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_token(self.policy_id, field_name="quality policy", maximum_length=64)
        if (
            type(self.eligible_flags) is not tuple
            or not self.eligible_flags
            or len(self.eligible_flags) > 64
            or any(
                type(flag) is not str or _QUALITY_FLAG.fullmatch(flag) is None
                for flag in self.eligible_flags
            )
            or tuple(sorted(set(self.eligible_flags))) != self.eligible_flags
        ):
            raise ReadinessValidationError("eligible quality flags are invalid")

    @property
    def policy_hash(self) -> str:
        return sha256_hex(("bar_quality_policy_v1", self.policy_id, self.eligible_flags))

    def approves(self, event: StoredBarEvent) -> bool:
        if type(event) is not StoredBarEvent:
            raise ReadinessValidationError("quality input must be a stored bar event")
        return event.bar.quality_flags == self.eligible_flags


STRICT_COMPLETE_QUALITY = BarQualityPolicy(
    policy_id="strict_complete_v1",
    eligible_flags=("complete",),
)


@dataclass(frozen=True, slots=True)
class SymbolReadiness:
    """Pure recomputation result for one exact market-data series."""

    series: DataSeries
    range_start_at: datetime
    range_end_at: datetime
    contiguous_through: datetime | None
    latest_contiguous_event: StoredBarEvent | None
    quality_hash: str
    blocking_interval: TradingInterval | None
    blocking_gap_ids: tuple[str, ...]

    @property
    def ready_through_range(self) -> bool:
        return self.contiguous_through == self.range_end_at


@dataclass(frozen=True, slots=True)
class BasketWatermark:
    """Validated current active-basket projection."""

    basket_watermark_id: str
    experiment_hash: str
    timeframe: str
    status: BasketStatus
    contiguous_through: datetime | None
    component_hash: str
    content_hash: str
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_hash(self.experiment_hash, field_name="experiment hash")
        _timeframe_duration(self.timeframe)
        if type(self.status) is not BasketStatus:
            raise ReadinessIntegrityError("persisted basket watermark status is invalid")
        contiguous = _optional_utc(self.contiguous_through, field_name="contiguous_through")
        if (self.status is BasketStatus.READY) != (contiguous is not None):
            raise ReadinessIntegrityError("persisted basket watermark readiness is inconsistent")
        updated = _require_utc(self.updated_at, field_name="updated_at")
        _require_integrity_hash(self.component_hash, field_name="component hash")
        _require_integrity_hash(self.content_hash, field_name="basket content hash")
        expected_id = _basket_watermark_id(self.experiment_hash, self.timeframe)
        if type(self.basket_watermark_id) is not str or self.basket_watermark_id != expected_id:
            raise ReadinessIntegrityError("persisted basket watermark identity is invalid")
        if (
            type(self.version) is not int
            or self.version < 1
            or self.version > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise ReadinessIntegrityError("persisted basket watermark version is invalid")
        object.__setattr__(self, "contiguous_through", contiguous)
        object.__setattr__(self, "updated_at", updated)
        if self.content_hash != _basket_content_hash(self):
            raise ReadinessIntegrityError("persisted basket watermark content hash is invalid")

    @property
    def is_ready(self) -> bool:
        return self.status is BasketStatus.READY


@dataclass(frozen=True, slots=True)
class BasketReadiness:
    """Active-only basket result; non-active roles are deliberately absent."""

    watermark: BasketWatermark | None
    missing_active_symbols: tuple[str, ...]
    blocked_active_symbols: tuple[str, ...]
    required_through: datetime | None

    @property
    def is_ready(self) -> bool:
        return (
            self.watermark is not None
            and self.watermark.is_ready
            and not self.missing_active_symbols
            and not self.blocked_active_symbols
            and (
                self.required_through is None
                or (
                    self.watermark.contiguous_through is not None
                    and self.watermark.contiguous_through >= self.required_through
                )
            )
        )


def detect_data_gaps(
    *,
    calendar: ExchangeCalendar,
    experiment_hash: str,
    series: DataSeries,
    start_at: datetime,
    end_at: datetime,
    observed_intervals: Sequence[TradingInterval],
    detected_at: datetime,
    reason_code: str = "missing_expected_bar",
) -> tuple[GapDetection, ...]:
    """Return same-session missing ranges; scheduled closed time never becomes a gap."""

    _require_calendar(calendar)
    _require_hash(experiment_hash, field_name="experiment hash")
    if type(series) is not DataSeries:
        raise ReadinessValidationError("gap-detection series is invalid")
    start_at, end_at = _require_utc_range(start_at, end_at)
    detected_at = _require_utc(detected_at, field_name="detected_at")
    if detected_at < end_at:
        raise ReadinessValidationError("gap detection cannot precede the inspected range")
    if type(reason_code) is not str or _REASON.fullmatch(reason_code) is None:
        raise ReadinessValidationError("gap reason code is invalid")
    expected = calendar.expected_intervals(
        start_at=start_at,
        end_at=end_at,
        timeframe=series.timeframe,
    )
    observed = _require_interval_sequence(observed_intervals, series.timeframe)
    expected_set = set(expected)
    if any(interval not in expected_set for interval in observed):
        raise ReadinessValidationError("observed interval is not expected by the exchange calendar")
    missing = tuple(interval for interval in expected if interval not in set(observed))
    groups = _contiguous_groups(missing)
    return tuple(
        GapDetection(
            experiment_hash=experiment_hash,
            series=series,
            start_at=group[0].start_at,
            end_at=group[-1].end_at,
            reason_code=reason_code,
            detected_at=detected_at,
        )
        for group in groups
    )


def compute_symbol_readiness(
    *,
    series: DataSeries,
    expected_intervals: Sequence[TradingInterval],
    effective_events: Sequence[StoredBarEvent],
    unresolved_gaps: Sequence[DataGap],
    quality_policy: BarQualityPolicy = STRICT_COMPLETE_QUALITY,
) -> SymbolReadiness:
    """Compute the highest contiguous approved end from current effective revisions."""

    if type(series) is not DataSeries:
        raise ReadinessValidationError("watermark series is invalid")
    expected = _require_interval_sequence(expected_intervals, series.timeframe)
    if not expected:
        raise ReadinessValidationError("watermark computation requires expected intervals")
    if type(quality_policy) is not BarQualityPolicy:
        raise ReadinessValidationError("watermark quality policy is invalid")
    events = _require_events(effective_events, series)
    gaps = _require_gaps(unresolved_gaps, series)
    by_start = {event.identity.start_at: event for event in events}

    contiguous_through: datetime | None = None
    latest_event: StoredBarEvent | None = None
    blocking_interval: TradingInterval | None = None
    blocking_gap_ids: tuple[str, ...] = ()
    quality_components: list[tuple[Any, ...]] = []
    for interval in expected:
        interval_gaps = tuple(
            gap.gap_id
            for gap in gaps
            if gap.start_at < interval.end_at and gap.end_at > interval.start_at
        )
        event = by_start.get(interval.start_at)
        approved = bool(
            event is not None
            and event.identity.end_at == interval.end_at
            and event.identity.timeframe == series.timeframe
            and quality_policy.approves(event)
        )
        quality_components.append(
            (
                interval.start_at,
                interval.end_at,
                None if event is None else event.bar_event_id,
                None if event is None else event.revision,
                None if event is None else event.normalized_payload_hash,
                None if event is None else event.bar.quality_flags,
                approved,
                interval_gaps,
            )
        )
        if blocking_interval is None and (interval_gaps or not approved):
            blocking_interval = interval
            blocking_gap_ids = interval_gaps
        if blocking_interval is None:
            contiguous_through = interval.end_at
            latest_event = event

    quality_hash = sha256_hex(
        (
            "symbol_readiness_v1",
            series.hash_input,
            quality_policy.policy_hash,
            tuple(quality_components),
        )
    )
    return SymbolReadiness(
        series=series,
        range_start_at=expected[0].start_at,
        range_end_at=expected[-1].end_at,
        contiguous_through=contiguous_through,
        latest_contiguous_event=latest_event,
        quality_hash=quality_hash,
        blocking_interval=blocking_interval,
        blocking_gap_ids=blocking_gap_ids,
    )


class GapRepository:
    """Persist gap state before bounded repair work and enforce idempotent transitions."""

    def __init__(self, engine: Engine, *, calendar: ExchangeCalendar) -> None:
        self._engine = _require_engine(engine)
        self._calendar = _require_calendar(calendar)
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.COLLECTOR)

    def transaction(self) -> AbstractContextManager[Connection]:
        return self._transactions.transaction()

    def record(self, detection: GapDetection) -> DataGap:
        """Durably record a calendar-valid gap; exact retries return the original row."""

        if type(detection) is not GapDetection:
            raise ReadinessValidationError("gap detection is invalid")
        _require_contiguous_calendar_range(
            self._calendar, detection.series, detection.start_at, detection.end_at
        )
        try:
            with self.transaction() as connection:
                self._lock_gap(
                    connection,
                    detection.gap_id,
                    experiment_hash=detection.experiment_hash,
                    series=detection.series,
                )
                current = self._select(connection, detection.gap_id, lock=True)
                if current is not None:
                    if (
                        current.experiment_hash != detection.experiment_hash
                        or current.series != detection.series
                        or current.start_at != detection.start_at
                        or current.end_at != detection.end_at
                        or current.reason_code != detection.reason_code
                    ):
                        raise ReadinessIntegrityError(
                            "gap identity collides with different content"
                        )
                    return current
                gap = _new_gap(detection)
                connection.execute(insert(aqa_data_gaps).values(**_gap_values(gap)))
                self._audit_gap(connection, gap, event_type="gap.detected")
                return gap
        except (ReadinessIntegrityError, ReadinessPersistenceError, ReadinessValidationError):
            raise
        except IntegrityError:
            raise ReadinessPersistenceError("gap write lost its idempotency race") from None
        except SQLAlchemyError:
            raise ReadinessPersistenceError("gap could not be persisted") from None

    def get(self, gap_id: str) -> DataGap | None:
        _require_gap_id(gap_id)
        try:
            with self._engine.begin() as connection:
                return self._select(connection, gap_id, lock=False)
        except (ReadinessIntegrityError, ReadinessValidationError):
            raise
        except SQLAlchemyError:
            raise ReadinessPersistenceError("gap could not be read") from None

    def begin_repair(self, gap_id: str, *, attempted_at: datetime) -> DataGap:
        """Claim a persisted gap before external repair activity begins."""

        _require_gap_id(gap_id)
        attempted_at = _require_utc(attempted_at, field_name="attempted_at")
        try:
            with self.transaction() as connection:
                unlocked = self._required(connection, gap_id, lock=False)
                self._lock_gap(
                    connection,
                    gap_id,
                    experiment_hash=unlocked.experiment_hash,
                    series=unlocked.series,
                )
                current = self._required(connection, gap_id, lock=True)
                if current.status is GapStatus.RESOLVED:
                    return current
                if current.status is GapStatus.WAIVED:
                    raise ReadinessValidationError("waived gap cannot be repaired")
                if current.status is GapStatus.REPAIRING:
                    if current.last_attempt_at == attempted_at:
                        return current
                    raise ReadinessValidationError("gap already has an active repair attempt")
                if attempted_at < current.detected_at:
                    raise ReadinessValidationError("repair attempt precedes gap detection")
                if current.attempt_count >= _MAX_SIGNED_32_BIT_INTEGER:
                    raise ReadinessIntegrityError("gap repair attempt capacity is exhausted")
                if current.version >= MAX_SIGNED_64_BIT_INTEGER:
                    raise ReadinessIntegrityError("gap version capacity is exhausted")
                changed = _replace_gap(
                    current,
                    status=GapStatus.REPAIRING,
                    attempt_count=current.attempt_count + 1,
                    last_attempt_at=attempted_at,
                    resolved_at=None,
                )
                self._update(connection, current, changed)
                self._audit_gap(connection, changed, event_type="gap.repair_started")
                return changed
        except (ReadinessIntegrityError, ReadinessPersistenceError, ReadinessValidationError):
            raise
        except SQLAlchemyError:
            raise ReadinessPersistenceError("gap repair claim could not be persisted") from None

    def complete_repair(self, gap_id: str, *, coverage: GapRepairCoverage) -> DataGap:
        """Resolve only exact, unambiguous calendar coverage; partial results reopen the gap."""

        _require_gap_id(gap_id)
        if type(coverage) is not GapRepairCoverage:
            raise ReadinessValidationError("gap repair coverage is invalid")
        try:
            with self.transaction() as connection:
                unlocked = self._required(connection, gap_id, lock=False)
                self._lock_gap(
                    connection,
                    gap_id,
                    experiment_hash=unlocked.experiment_hash,
                    series=unlocked.series,
                )
                current = self._required(connection, gap_id, lock=True)
                _require_matching_repair(current, coverage)
                expected = self._calendar.expected_intervals(
                    start_at=current.start_at,
                    end_at=current.end_at,
                    timeframe=current.series.timeframe,
                )
                complete = coverage.unambiguous and coverage.observed_intervals == expected
                if current.status is GapStatus.RESOLVED:
                    if complete:
                        return current
                    raise ReadinessValidationError("resolved gap cannot accept incomplete coverage")
                if current.status is not GapStatus.REPAIRING:
                    raise ReadinessValidationError("gap repair must be claimed before completion")
                if (
                    current.last_attempt_at is None
                    or coverage.completed_at < current.last_attempt_at
                ):
                    raise ReadinessValidationError("repair completion precedes its attempt")
                if current.version >= MAX_SIGNED_64_BIT_INTEGER:
                    raise ReadinessIntegrityError("gap version capacity is exhausted")
                changed = _replace_gap(
                    current,
                    status=GapStatus.RESOLVED if complete else GapStatus.OPEN,
                    attempt_count=current.attempt_count,
                    last_attempt_at=current.last_attempt_at,
                    resolved_at=coverage.completed_at if complete else None,
                )
                self._update(connection, current, changed)
                self._audit_gap(
                    connection,
                    changed,
                    event_type="gap.resolved" if complete else "gap.reopened",
                )
                return changed
        except (ReadinessIntegrityError, ReadinessPersistenceError, ReadinessValidationError):
            raise
        except SQLAlchemyError:
            raise ReadinessPersistenceError("gap repair result could not be persisted") from None

    def reopen_interrupted(self, gap_id: str, *, reopened_at: datetime) -> DataGap:
        """Return an interrupted durable claim to ``OPEN`` so restart can retry safely."""

        _require_gap_id(gap_id)
        reopened_at = _require_utc(reopened_at, field_name="reopened_at")
        try:
            with self.transaction() as connection:
                unlocked = self._required(connection, gap_id, lock=False)
                self._lock_gap(
                    connection,
                    gap_id,
                    experiment_hash=unlocked.experiment_hash,
                    series=unlocked.series,
                )
                current = self._required(connection, gap_id, lock=True)
                if current.status is GapStatus.OPEN:
                    return current
                if current.status is not GapStatus.REPAIRING:
                    raise ReadinessValidationError("only an active repair claim can be reopened")
                if current.last_attempt_at is None or reopened_at < current.last_attempt_at:
                    raise ReadinessValidationError("gap reopen timestamp precedes its attempt")
                if current.version >= MAX_SIGNED_64_BIT_INTEGER:
                    raise ReadinessIntegrityError("gap version capacity is exhausted")
                changed = _replace_gap(
                    current,
                    status=GapStatus.OPEN,
                    attempt_count=current.attempt_count,
                    last_attempt_at=current.last_attempt_at,
                    resolved_at=None,
                )
                self._update(connection, current, changed)
                self._audit_gap(
                    connection,
                    changed,
                    event_type="gap.reopened",
                    occurred_at=reopened_at,
                )
                return changed
        except (ReadinessIntegrityError, ReadinessPersistenceError, ReadinessValidationError):
            raise
        except SQLAlchemyError:
            raise ReadinessPersistenceError("interrupted gap could not be reopened") from None

    def repair(
        self,
        detection: GapDetection,
        *,
        attempted_at: datetime,
        repairer: Callable[[DataGap], GapRepairCoverage],
    ) -> DataGap:
        """Run repair only after the gap and attempt are durably visible."""

        if not callable(repairer):
            raise ReadinessValidationError("gap repairer must be callable")
        persisted = self.record(detection)
        if persisted.status is GapStatus.RESOLVED:
            return persisted
        claimed = self.begin_repair(persisted.gap_id, attempted_at=attempted_at)
        try:
            coverage = repairer(claimed)
        except Exception:
            self.reopen_interrupted(claimed.gap_id, reopened_at=attempted_at)
            raise
        return self.complete_repair(claimed.gap_id, coverage=coverage)

    def list_unresolved(
        self,
        *,
        experiment_hash: str,
        series: DataSeries | None = None,
        connection: Connection | None = None,
    ) -> tuple[DataGap, ...]:
        _require_hash(experiment_hash, field_name="experiment hash")
        if series is not None and type(series) is not DataSeries:
            raise ReadinessValidationError("gap series is invalid")
        statement = select(aqa_data_gaps).where(
            aqa_data_gaps.c.experiment_hash == experiment_hash,
            aqa_data_gaps.c.status.in_((GapStatus.OPEN.value, GapStatus.REPAIRING.value)),
        )
        if series is not None:
            statement = statement.where(*_series_predicates(aqa_data_gaps, series))
        statement = statement.order_by(aqa_data_gaps.c.gap_start_at, aqa_data_gaps.c.gap_id)
        try:
            if connection is not None:
                self._transactions.validate_connection(
                    connection,
                    require_serialized_sqlite=False,
                )
                rows = connection.execute(statement).mappings().all()
            else:
                with self._engine.begin() as owned:
                    rows = owned.execute(statement).mappings().all()
            return tuple(_gap_from_row(row) for row in rows)
        except (ReadinessIntegrityError, ReadinessValidationError):
            raise
        except TransactionBoundaryError:
            raise ReadinessPersistenceError("gap read requires a repository transaction") from None
        except SQLAlchemyError:
            raise ReadinessPersistenceError("gaps could not be read") from None

    def _required(self, connection: Connection, gap_id: str, *, lock: bool) -> DataGap:
        gap = self._select(connection, gap_id, lock=lock)
        if gap is None:
            raise ReadinessValidationError("gap does not exist")
        return gap

    def _select(self, connection: Connection, gap_id: str, *, lock: bool) -> DataGap | None:
        statement = select(aqa_data_gaps).where(aqa_data_gaps.c.gap_id == gap_id)
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _gap_from_row(row)

    def _lock_gap(
        self,
        connection: Connection,
        gap_id: str,
        *,
        experiment_hash: str,
        series: DataSeries,
    ) -> None:
        try:
            self._transactions.acquire_postgres_advisory_locks(
                connection,
                (
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
                        gap_id,
                    ),
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
                        _symbol_watermark_id(experiment_hash, series),
                    ),
                ),
            )
        except TransactionBoundaryError:
            raise ReadinessPersistenceError("gap lock could not be acquired") from None

    def _audit_gap(
        self,
        connection: Connection,
        gap: DataGap,
        *,
        event_type: str,
        occurred_at: datetime | None = None,
    ) -> None:
        self._audit.append(
            stream_id=f"aqa_collector:gap:{gap.gap_id}",
            event_type=event_type,
            occurred_at=(occurred_at or gap.resolved_at or gap.last_attempt_at or gap.detected_at),
            payload=AuditPayload.from_mapping(
                {
                    "adjustment": gap.series.adjustment,
                    "content_hash": gap.content_hash,
                    "feed": gap.series.feed,
                    "gap_id": gap.gap_id,
                    "idempotency_key": f"gap_state_{gap.content_hash}",
                    "provider": gap.series.provider,
                    "reason_code": gap.reason_code,
                    "status": gap.status.value,
                    "symbol": gap.series.symbol,
                    "version": gap.version,
                }
            ),
            connection=connection,
        )

    @staticmethod
    def _update(connection: Connection, current: DataGap, changed: DataGap) -> None:
        result = connection.execute(
            update(aqa_data_gaps)
            .where(
                aqa_data_gaps.c.gap_id == current.gap_id, aqa_data_gaps.c.version == current.version
            )
            .values(**_gap_values(changed, include_identity=False))
        )
        if result.rowcount != 1:
            raise ReadinessPersistenceError("gap update lost its concurrency fence")


class WatermarkRepository:
    """Rebuild symbol and active-basket readiness from durable effective state."""

    def __init__(self, engine: Engine, *, calendar: ExchangeCalendar) -> None:
        self._engine = _require_engine(engine)
        self._calendar = _require_calendar(calendar)
        self._transactions = SerializedTransactionCoordinator(engine)
        self._market_data = MarketDataRepository(engine)
        self._gaps = GapRepository(engine, calendar=calendar)
        self._audit = AuditRepository(engine, writer=AuditWriter.COLLECTOR)

    def transaction(self) -> AbstractContextManager[Connection]:
        return self._transactions.transaction()

    def active_basket_watermark(
        self,
        *,
        experiment_hash: str,
        timeframe: str,
    ) -> BasketWatermark | None:
        """Load and verify the durable active-basket state, including blocked state."""

        _require_hash(experiment_hash, field_name="experiment hash")
        _timeframe_duration(timeframe)
        try:
            with self._engine.begin() as connection:
                return _select_basket_watermark(
                    connection,
                    experiment_hash=experiment_hash,
                    timeframe=timeframe,
                    lock=False,
                )
        except (ReadinessIntegrityError, ReadinessValidationError):
            raise
        except SQLAlchemyError:
            raise ReadinessPersistenceError("basket watermark could not be read") from None

    def recompute_symbol(
        self,
        *,
        experiment_hash: str,
        series: DataSeries,
        start_at: datetime,
        end_at: datetime,
        updated_at: datetime,
        quality_policy: BarQualityPolicy = STRICT_COMPLETE_QUALITY,
    ) -> tuple[SymbolReadiness, SymbolWatermark | None]:
        """Atomically recompute and persist the highest current contiguous eligible end."""

        _require_hash(experiment_hash, field_name="experiment hash")
        if type(series) is not DataSeries:
            raise ReadinessValidationError("watermark series is invalid")
        start_at, end_at = _require_utc_range(start_at, end_at)
        updated_at = _require_utc(updated_at, field_name="updated_at")
        if updated_at < end_at:
            raise ReadinessValidationError("watermark recomputation precedes its inspected range")
        expected = self._calendar.expected_intervals(
            start_at=start_at,
            end_at=end_at,
            timeframe=series.timeframe,
        )
        if not expected:
            raise ReadinessValidationError("watermark range contains no expected trading intervals")
        try:
            with self.transaction() as connection:
                self._lock_series_and_bars(connection, experiment_hash, series, expected)
                events = tuple(
                    event
                    for interval in expected
                    if (
                        event := self._market_data.latest(
                            _bar_identity(series, interval),
                            connection=connection,
                        )
                    )
                    is not None
                )
                gaps = self._gaps.list_unresolved(
                    experiment_hash=experiment_hash,
                    series=series,
                    connection=connection,
                )
                readiness = compute_symbol_readiness(
                    series=series,
                    expected_intervals=expected,
                    effective_events=events,
                    unresolved_gaps=gaps,
                    quality_policy=quality_policy,
                )
                current = _select_symbol_watermark(
                    connection,
                    experiment_hash=experiment_hash,
                    series=series,
                    lock=True,
                )
                if readiness.latest_contiguous_event is None:
                    if current is not None and (
                        current.contiguous_through == readiness.range_start_at
                        and current.quality_hash == readiness.quality_hash
                        and current.latest_bar_event_id is None
                    ):
                        return readiness, current
                    if current is not None and current.version >= MAX_SIGNED_64_BIT_INTEGER:
                        raise ReadinessIntegrityError(
                            "symbol watermark version capacity is exhausted"
                        )
                    candidate = _new_blocked_symbol_watermark(
                        experiment_hash=experiment_hash,
                        readiness=readiness,
                        version=1 if current is None else current.version + 1,
                        updated_at=updated_at,
                    )
                    if current is not None and updated_at <= current.updated_at:
                        raise ReadinessValidationError(
                            "changed watermark recomputation must follow durable state time"
                        )
                    if current is None:
                        connection.execute(
                            insert(aqa_symbol_watermarks).values(
                                **_symbol_watermark_values(candidate)
                            )
                        )
                    else:
                        result = connection.execute(
                            update(aqa_symbol_watermarks)
                            .where(
                                aqa_symbol_watermarks.c.symbol_watermark_id
                                == current.symbol_watermark_id,
                                aqa_symbol_watermarks.c.version == current.version,
                            )
                            .values(**_symbol_watermark_values(candidate, include_identity=False))
                        )
                        if result.rowcount != 1:
                            raise ReadinessPersistenceError(
                                "symbol watermark update lost its concurrency fence"
                            )
                    self._audit_symbol_watermark(connection, candidate)
                    return readiness, candidate
                if current is not None and (
                    current.contiguous_through == readiness.contiguous_through
                    and current.quality_hash == readiness.quality_hash
                    and current.latest_bar_event_id
                    == readiness.latest_contiguous_event.bar_event_id
                ):
                    return readiness, current
                if current is not None and current.version >= MAX_SIGNED_64_BIT_INTEGER:
                    raise ReadinessIntegrityError("symbol watermark version capacity is exhausted")
                candidate = _new_symbol_watermark(
                    experiment_hash=experiment_hash,
                    readiness=readiness,
                    version=1 if current is None else current.version + 1,
                    updated_at=updated_at,
                )
                if current is not None and updated_at <= current.updated_at:
                    raise ReadinessValidationError(
                        "changed watermark recomputation must follow durable state time"
                    )
                if current is None:
                    connection.execute(
                        insert(aqa_symbol_watermarks).values(**_symbol_watermark_values(candidate))
                    )
                else:
                    result = connection.execute(
                        update(aqa_symbol_watermarks)
                        .where(
                            aqa_symbol_watermarks.c.symbol_watermark_id
                            == current.symbol_watermark_id,
                            aqa_symbol_watermarks.c.version == current.version,
                        )
                        .values(**_symbol_watermark_values(candidate, include_identity=False))
                    )
                    if result.rowcount != 1:
                        raise ReadinessPersistenceError(
                            "symbol watermark update lost its concurrency fence"
                        )
                self._audit_symbol_watermark(connection, candidate)
                return readiness, candidate
        except (ReadinessIntegrityError, ReadinessPersistenceError, ReadinessValidationError):
            raise
        except SQLAlchemyError:
            raise ReadinessPersistenceError("symbol watermark could not be persisted") from None

    def recompute_active_basket(
        self,
        *,
        experiment_hash: str,
        universe: UniverseSpec,
        provider: str,
        feed: str,
        adjustment: str,
        timeframe: str,
        updated_at: datetime,
        required_through: datetime | None = None,
    ) -> BasketReadiness:
        """Persist the minimum across every active symbol; other roles never participate."""

        _require_hash(experiment_hash, field_name="experiment hash")
        if not isinstance(universe, UniverseSpec):
            raise ReadinessValidationError("basket universe is invalid")
        template = DataSeries(provider, feed, adjustment, universe.active_tradable[0], timeframe)
        del template
        updated_at = _require_utc(updated_at, field_name="updated_at")
        if required_through is not None:
            required_through = _require_utc(required_through, field_name="required_through")
        active = tuple(sorted(universe.active_tradable))
        try:
            with self.transaction() as connection:
                requests = (
                    *(
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
                            _symbol_watermark_id(
                                experiment_hash,
                                DataSeries(provider, feed, adjustment, symbol, timeframe),
                            ),
                        )
                        for symbol in active
                    ),
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
                        _basket_watermark_id(experiment_hash, timeframe),
                    ),
                )
                self._transactions.acquire_postgres_advisory_locks(connection, requests)
                selected: list[tuple[DataSeries, SymbolWatermark, BarIdentity]] = []
                evidence: list[tuple[Any, ...]] = []
                observed_updates: list[datetime] = []
                missing: list[str] = []
                blocked: list[str] = []
                for symbol in active:
                    series = DataSeries(provider, feed, adjustment, symbol, timeframe)
                    watermark = _select_symbol_watermark(
                        connection,
                        experiment_hash=experiment_hash,
                        series=series,
                        lock=True,
                    )
                    if watermark is None:
                        missing.append(symbol)
                        evidence.append((symbol, "missing"))
                        continue
                    observed_updates.append(watermark.updated_at)
                    if watermark.latest_bar_event_id is None:
                        blocked.append(symbol)
                        evidence.append(
                            (
                                symbol,
                                "symbol_blocked",
                                watermark.contiguous_through,
                                watermark.quality_hash,
                                watermark.version,
                            )
                        )
                        continue
                    identity = _bar_identity(
                        series,
                        TradingInterval(
                            watermark.contiguous_through - _timeframe_duration(timeframe),
                            watermark.contiguous_through,
                        ),
                    )
                    referenced_identity_id = connection.scalar(
                        select(aqa_bar_events.c.bar_identity_id).where(
                            aqa_bar_events.c.bar_event_id == watermark.latest_bar_event_id
                        )
                    )
                    if referenced_identity_id != identity.bar_identity_id:
                        raise ReadinessIntegrityError(
                            "symbol watermark references another series or interval"
                        )
                    selected.append((series, watermark, identity))
                self._transactions.acquire_postgres_advisory_locks(
                    connection,
                    tuple(
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
                            identity.bar_identity_id,
                        )
                        for _, _, identity in selected
                    ),
                )
                components: list[SymbolWatermark] = []
                for series, watermark, identity in selected:
                    effective_event_id = connection.scalar(
                        select(aqa_bar_latest.c.bar_event_id).where(
                            aqa_bar_latest.c.bar_identity_id == identity.bar_identity_id
                        )
                    )
                    if effective_event_id != watermark.latest_bar_event_id:
                        blocked.append(series.symbol)
                        evidence.append(
                            (
                                series.symbol,
                                "effective_event_mismatch",
                                watermark.contiguous_through,
                                watermark.quality_hash,
                                watermark.latest_bar_event_id,
                                effective_event_id,
                                watermark.version,
                            )
                        )
                        continue
                    verified = self._market_data.watermark(
                        experiment_hash=experiment_hash,
                        identity=identity,
                        connection=connection,
                    )
                    if verified != watermark:
                        raise ReadinessIntegrityError(
                            "symbol watermark changed during basket recomputation"
                        )
                    gaps = self._gaps.list_unresolved(
                        experiment_hash=experiment_hash,
                        series=series,
                        connection=connection,
                    )
                    blocking_gaps = tuple(
                        sorted(
                            (gap for gap in gaps if gap.start_at < watermark.contiguous_through),
                            key=lambda gap: gap.gap_id,
                        )
                    )
                    if blocking_gaps:
                        blocked.append(series.symbol)
                        evidence.append(
                            (
                                series.symbol,
                                "unresolved_gaps",
                                watermark.contiguous_through,
                                watermark.quality_hash,
                                watermark.latest_bar_event_id,
                                watermark.version,
                                tuple((gap.gap_id, gap.content_hash) for gap in blocking_gaps),
                            )
                        )
                        continue
                    components.append(watermark)
                    evidence.append(
                        (
                            series.symbol,
                            "ready",
                            watermark.contiguous_through,
                            watermark.quality_hash,
                            watermark.latest_bar_event_id,
                            watermark.version,
                        )
                    )
                current = _select_basket_watermark(
                    connection,
                    experiment_hash=experiment_hash,
                    timeframe=timeframe,
                    lock=True,
                )
                if missing or blocked:
                    if observed_updates and updated_at < max(observed_updates):
                        raise ReadinessValidationError(
                            "basket timestamp precedes its component readiness state"
                        )
                    component_hash = sha256_hex(
                        (
                            "active_basket_blocked_components_v1",
                            provider,
                            feed,
                            adjustment,
                            timeframe,
                            active,
                            tuple(evidence),
                        )
                    )
                    if (
                        current is not None
                        and current.status is BasketStatus.BLOCKED
                        and current.component_hash == component_hash
                    ):
                        candidate = current
                    else:
                        if current is not None and current.version >= MAX_SIGNED_64_BIT_INTEGER:
                            raise ReadinessIntegrityError(
                                "basket watermark version capacity is exhausted"
                            )
                        candidate = _new_basket_watermark(
                            experiment_hash=experiment_hash,
                            timeframe=timeframe,
                            status=BasketStatus.BLOCKED,
                            contiguous_through=None,
                            component_hash=component_hash,
                            version=1 if current is None else current.version + 1,
                            updated_at=updated_at,
                        )
                        self._persist_basket_watermark(connection, current, candidate)
                    return BasketReadiness(
                        watermark=candidate,
                        missing_active_symbols=tuple(missing),
                        blocked_active_symbols=tuple(blocked),
                        required_through=required_through,
                    )
                contiguous = min(item.contiguous_through for item in components)
                latest_component_update = max(item.updated_at for item in components)
                if updated_at < max(contiguous, latest_component_update):
                    raise ReadinessValidationError(
                        "basket timestamp precedes its component readiness state"
                    )
                component_hash = sha256_hex(
                    (
                        "active_basket_components_v1",
                        provider,
                        feed,
                        adjustment,
                        timeframe,
                        tuple(
                            (
                                item.symbol,
                                item.contiguous_through,
                                item.quality_hash,
                                item.latest_bar_event_id,
                                item.version,
                            )
                            for item in sorted(components, key=lambda value: value.symbol)
                        ),
                    )
                )
                if current is not None and (
                    current.status is BasketStatus.READY
                    and current.contiguous_through == contiguous
                    and current.component_hash == component_hash
                ):
                    return BasketReadiness(
                        watermark=current,
                        missing_active_symbols=(),
                        blocked_active_symbols=(),
                        required_through=required_through,
                    )
                if current is not None and current.version >= MAX_SIGNED_64_BIT_INTEGER:
                    raise ReadinessIntegrityError("basket watermark version capacity is exhausted")
                candidate = _new_basket_watermark(
                    experiment_hash=experiment_hash,
                    timeframe=timeframe,
                    status=BasketStatus.READY,
                    contiguous_through=contiguous,
                    component_hash=component_hash,
                    version=1 if current is None else current.version + 1,
                    updated_at=updated_at,
                )
                self._persist_basket_watermark(connection, current, candidate)
                return BasketReadiness(
                    watermark=candidate,
                    missing_active_symbols=(),
                    blocked_active_symbols=(),
                    required_through=required_through,
                )
        except (ReadinessIntegrityError, ReadinessPersistenceError, ReadinessValidationError):
            raise
        except (MarketDataIntegrityError, MarketDataValidationError):
            raise ReadinessIntegrityError("symbol watermark effective-event proof failed") from None
        except MarketDataPersistenceError:
            raise ReadinessPersistenceError(
                "symbol watermark effective-event proof failed"
            ) from None
        except (TransactionBoundaryError, SQLAlchemyError):
            raise ReadinessPersistenceError("basket watermark could not be persisted") from None

    def _persist_basket_watermark(
        self,
        connection: Connection,
        current: BasketWatermark | None,
        candidate: BasketWatermark,
    ) -> None:
        if current is None:
            connection.execute(insert(aqa_basket_watermarks).values(**_basket_values(candidate)))
        else:
            if candidate.updated_at <= current.updated_at:
                raise ReadinessValidationError(
                    "changed basket recomputation must follow durable state time"
                )
            result = connection.execute(
                update(aqa_basket_watermarks)
                .where(
                    aqa_basket_watermarks.c.basket_watermark_id == current.basket_watermark_id,
                    aqa_basket_watermarks.c.version == current.version,
                )
                .values(**_basket_values(candidate, include_identity=False))
            )
            if result.rowcount != 1:
                raise ReadinessPersistenceError(
                    "basket watermark update lost its concurrency fence"
                )
        self._audit_basket_watermark(connection, candidate)

    def _audit_symbol_watermark(
        self,
        connection: Connection,
        watermark: SymbolWatermark,
    ) -> None:
        payload: dict[str, object] = {
            "adjustment": watermark.adjustment,
            "content_hash": watermark.content_hash,
            "feed": watermark.feed,
            "idempotency_key": f"watermark_state_{watermark.content_hash}",
            "provider": watermark.provider,
            "status": "ready" if watermark.is_ready else "blocked",
            "symbol": watermark.symbol,
            "version": watermark.version,
        }
        if watermark.latest_bar_event_id is not None:
            payload["bar_id"] = watermark.latest_bar_event_id
        self._audit.append(
            stream_id=f"aqa_collector:data:{watermark.symbol_watermark_id}",
            event_type="data.symbol_watermark_updated",
            occurred_at=watermark.updated_at,
            payload=AuditPayload.from_mapping(payload),
            connection=connection,
        )

    def _audit_basket_watermark(
        self,
        connection: Connection,
        watermark: BasketWatermark,
    ) -> None:
        self._audit.append(
            stream_id=f"aqa_collector:data:{watermark.basket_watermark_id}",
            event_type="data.basket_watermark_updated",
            occurred_at=watermark.updated_at,
            payload=AuditPayload.from_mapping(
                {
                    "content_hash": watermark.content_hash,
                    "experiment_hash": watermark.experiment_hash,
                    "idempotency_key": f"watermark_state_{watermark.content_hash}",
                    "status": watermark.status.value,
                    "version": watermark.version,
                }
            ),
            connection=connection,
        )

    def _lock_series_and_bars(
        self,
        connection: Connection,
        experiment_hash: str,
        series: DataSeries,
        intervals: Sequence[TradingInterval],
    ) -> None:
        requests = [
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
                _symbol_watermark_id(experiment_hash, series),
            )
        ]
        requests.extend(
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
                _bar_identity(series, interval).bar_identity_id,
            )
            for interval in intervals
        )
        try:
            self._transactions.acquire_postgres_advisory_locks(connection, requests)
        except TransactionBoundaryError:
            raise ReadinessPersistenceError("watermark locks could not be acquired") from None


def _new_gap(detection: GapDetection) -> DataGap:
    values: dict[str, Any] = {
        "gap_id": detection.gap_id,
        "experiment_hash": detection.experiment_hash,
        "series": detection.series,
        "start_at": detection.start_at,
        "end_at": detection.end_at,
        "status": GapStatus.OPEN,
        "reason_code": detection.reason_code,
        "attempt_count": 0,
        "detected_at": detection.detected_at,
        "last_attempt_at": None,
        "resolved_at": None,
        "version": 1,
    }
    values["content_hash"] = _gap_hash_for_fields(**values)
    return DataGap(**values)


def _replace_gap(
    gap: DataGap,
    *,
    status: GapStatus,
    attempt_count: int,
    last_attempt_at: datetime | None,
    resolved_at: datetime | None,
) -> DataGap:
    values: dict[str, Any] = {
        "gap_id": gap.gap_id,
        "experiment_hash": gap.experiment_hash,
        "series": gap.series,
        "start_at": gap.start_at,
        "end_at": gap.end_at,
        "status": status,
        "reason_code": gap.reason_code,
        "attempt_count": attempt_count,
        "detected_at": gap.detected_at,
        "last_attempt_at": last_attempt_at,
        "resolved_at": resolved_at,
        "version": gap.version + 1,
    }
    values["content_hash"] = _gap_hash_for_fields(**values)
    return DataGap(**values)


def _gap_content_hash(gap: DataGap) -> str:
    return _gap_hash_for_fields(
        gap_id=gap.gap_id,
        experiment_hash=gap.experiment_hash,
        series=gap.series,
        start_at=gap.start_at,
        end_at=gap.end_at,
        status=gap.status,
        reason_code=gap.reason_code,
        attempt_count=gap.attempt_count,
        detected_at=gap.detected_at,
        last_attempt_at=gap.last_attempt_at,
        resolved_at=gap.resolved_at,
        version=gap.version,
    )


def _gap_hash_for_fields(
    *,
    gap_id: str,
    experiment_hash: str,
    series: DataSeries,
    start_at: datetime,
    end_at: datetime,
    status: GapStatus,
    reason_code: str,
    attempt_count: int,
    detected_at: datetime,
    last_attempt_at: datetime | None,
    resolved_at: datetime | None,
    version: int,
) -> str:
    return sha256_hex(
        (
            "data_gap_state_v1",
            gap_id,
            experiment_hash,
            series.hash_input,
            start_at,
            end_at,
            status,
            reason_code,
            attempt_count,
            detected_at,
            last_attempt_at,
            resolved_at,
            version,
        )
    )


def _gap_values(gap: DataGap, *, include_identity: bool = True) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": gap.status.value,
        "reason_code": gap.reason_code,
        "attempt_count": gap.attempt_count,
        "detected_at": gap.detected_at,
        "last_attempt_at": gap.last_attempt_at,
        "resolved_at": gap.resolved_at,
        "content_hash": gap.content_hash,
        "version": gap.version,
    }
    if include_identity:
        values.update(
            {
                "gap_id": gap.gap_id,
                "experiment_hash": gap.experiment_hash,
                "provider": gap.series.provider,
                "feed": gap.series.feed,
                "adjustment": gap.series.adjustment,
                "symbol": gap.series.symbol,
                "timeframe": gap.series.timeframe,
                "gap_start_at": gap.start_at,
                "gap_end_at": gap.end_at,
            }
        )
    return values


def _gap_from_row(row: RowMapping) -> DataGap:
    try:
        return DataGap(
            gap_id=row["gap_id"],
            experiment_hash=row["experiment_hash"],
            series=DataSeries(
                provider=row["provider"],
                feed=row["feed"],
                adjustment=row["adjustment"],
                symbol=row["symbol"],
                timeframe=row["timeframe"],
            ),
            start_at=row["gap_start_at"],
            end_at=row["gap_end_at"],
            status=GapStatus(row["status"]),
            reason_code=row["reason_code"],
            attempt_count=row["attempt_count"],
            detected_at=row["detected_at"],
            last_attempt_at=row["last_attempt_at"],
            resolved_at=row["resolved_at"],
            content_hash=row["content_hash"],
            version=row["version"],
        )
    except (DomainValidationError, KeyError, TypeError, ValueError):
        raise ReadinessIntegrityError("persisted gap is malformed") from None


def _new_symbol_watermark(
    *,
    experiment_hash: str,
    readiness: SymbolReadiness,
    version: int,
    updated_at: datetime,
) -> SymbolWatermark:
    event = readiness.latest_contiguous_event
    if event is None or readiness.contiguous_through is None:
        raise ReadinessValidationError("blocked readiness cannot produce a symbol watermark")
    series = readiness.series
    watermark_id = _symbol_watermark_id(experiment_hash, series)
    values: dict[str, Any] = {
        "symbol_watermark_id": watermark_id,
        "experiment_hash": experiment_hash,
        "provider": series.provider,
        "feed": series.feed,
        "adjustment": series.adjustment,
        "symbol": series.symbol,
        "timeframe": series.timeframe,
        "contiguous_through": readiness.contiguous_through,
        "quality_hash": readiness.quality_hash,
        "latest_bar_event_id": event.bar_event_id,
        "version": version,
        "updated_at": updated_at,
    }
    values["content_hash"] = sha256_hex(
        (
            watermark_id,
            experiment_hash,
            series.provider,
            series.feed,
            series.adjustment,
            series.symbol,
            series.timeframe,
            readiness.contiguous_through,
            readiness.quality_hash,
            event.bar_event_id,
            version,
            updated_at,
        )
    )
    return SymbolWatermark(**values)


def _new_blocked_symbol_watermark(
    *,
    experiment_hash: str,
    readiness: SymbolReadiness,
    version: int,
    updated_at: datetime,
) -> SymbolWatermark:
    """Represent no eligible prefix durably at the inspected range floor."""

    if readiness.latest_contiguous_event is not None or readiness.contiguous_through is not None:
        raise ReadinessValidationError("ready state cannot produce a blocked watermark floor")
    series = readiness.series
    watermark_id = _symbol_watermark_id(experiment_hash, series)
    values: dict[str, Any] = {
        "symbol_watermark_id": watermark_id,
        "experiment_hash": experiment_hash,
        "provider": series.provider,
        "feed": series.feed,
        "adjustment": series.adjustment,
        "symbol": series.symbol,
        "timeframe": series.timeframe,
        "contiguous_through": readiness.range_start_at,
        "quality_hash": readiness.quality_hash,
        "latest_bar_event_id": None,
        "version": version,
        "updated_at": updated_at,
    }
    values["content_hash"] = sha256_hex(
        (
            watermark_id,
            experiment_hash,
            series.provider,
            series.feed,
            series.adjustment,
            series.symbol,
            series.timeframe,
            readiness.range_start_at,
            readiness.quality_hash,
            None,
            version,
            updated_at,
        )
    )
    return SymbolWatermark(**values)


def _select_symbol_watermark(
    connection: Connection,
    *,
    experiment_hash: str,
    series: DataSeries,
    lock: bool,
) -> SymbolWatermark | None:
    statement = select(aqa_symbol_watermarks).where(
        aqa_symbol_watermarks.c.experiment_hash == experiment_hash,
        *_series_predicates(aqa_symbol_watermarks, series),
    )
    if lock and connection.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        return None
    try:
        return SymbolWatermark(**dict(row))
    except (DomainValidationError, TypeError, ValueError):
        raise ReadinessIntegrityError("persisted symbol watermark is malformed") from None


def _symbol_watermark_values(
    watermark: SymbolWatermark,
    *,
    include_identity: bool = True,
) -> dict[str, Any]:
    values = {
        "contiguous_through": watermark.contiguous_through,
        "quality_hash": watermark.quality_hash,
        "latest_bar_event_id": watermark.latest_bar_event_id,
        "content_hash": watermark.content_hash,
        "version": watermark.version,
        "updated_at": watermark.updated_at,
    }
    if include_identity:
        values.update(
            {
                "symbol_watermark_id": watermark.symbol_watermark_id,
                "experiment_hash": watermark.experiment_hash,
                "provider": watermark.provider,
                "feed": watermark.feed,
                "adjustment": watermark.adjustment,
                "symbol": watermark.symbol,
                "timeframe": watermark.timeframe,
            }
        )
    return values


def _new_basket_watermark(
    *,
    experiment_hash: str,
    timeframe: str,
    status: BasketStatus,
    contiguous_through: datetime | None,
    component_hash: str,
    version: int,
    updated_at: datetime,
) -> BasketWatermark:
    values: dict[str, Any] = {
        "basket_watermark_id": _basket_watermark_id(experiment_hash, timeframe),
        "experiment_hash": experiment_hash,
        "timeframe": timeframe,
        "status": status,
        "contiguous_through": contiguous_through,
        "component_hash": component_hash,
        "version": version,
        "updated_at": updated_at,
    }
    values["content_hash"] = _basket_hash_for_fields(
        basket_watermark_id=values["basket_watermark_id"],
        experiment_hash=experiment_hash,
        timeframe=timeframe,
        contiguous_through=contiguous_through,
        component_hash=component_hash,
        version=version,
        updated_at=updated_at,
    )
    return BasketWatermark(**values)


def _basket_content_hash(watermark: BasketWatermark) -> str:
    return _basket_hash_for_fields(
        basket_watermark_id=watermark.basket_watermark_id,
        experiment_hash=watermark.experiment_hash,
        timeframe=watermark.timeframe,
        contiguous_through=watermark.contiguous_through,
        component_hash=watermark.component_hash,
        version=watermark.version,
        updated_at=watermark.updated_at,
    )


def _basket_hash_for_fields(
    *,
    basket_watermark_id: str,
    experiment_hash: str,
    timeframe: str,
    contiguous_through: datetime | None,
    component_hash: str,
    version: int,
    updated_at: datetime,
) -> str:
    return sha256_hex(
        (
            "active_basket_watermark_v1",
            basket_watermark_id,
            experiment_hash,
            timeframe,
            contiguous_through,
            component_hash,
            version,
            updated_at,
        )
    )


def _select_basket_watermark(
    connection: Connection,
    *,
    experiment_hash: str,
    timeframe: str,
    lock: bool,
) -> BasketWatermark | None:
    statement = select(aqa_basket_watermarks).where(
        aqa_basket_watermarks.c.experiment_hash == experiment_hash,
        aqa_basket_watermarks.c.role == "active",
        aqa_basket_watermarks.c.timeframe == timeframe,
    )
    if lock and connection.dialect.name == "postgresql":
        statement = statement.with_for_update()
    row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        return None
    try:
        return BasketWatermark(
            basket_watermark_id=row["basket_watermark_id"],
            experiment_hash=row["experiment_hash"],
            timeframe=row["timeframe"],
            status=BasketStatus(row["status"]),
            contiguous_through=row["contiguous_through"],
            component_hash=row["component_hash"],
            content_hash=row["content_hash"],
            version=row["version"],
            updated_at=row["updated_at"],
        )
    except (DomainValidationError, KeyError, TypeError, ValueError):
        raise ReadinessIntegrityError("persisted basket watermark is malformed") from None


def _basket_values(
    watermark: BasketWatermark,
    *,
    include_identity: bool = True,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": watermark.status.value,
        "contiguous_through": watermark.contiguous_through,
        "component_hash": watermark.component_hash,
        "content_hash": watermark.content_hash,
        "version": watermark.version,
        "updated_at": watermark.updated_at,
    }
    if include_identity:
        values.update(
            {
                "basket_watermark_id": watermark.basket_watermark_id,
                "experiment_hash": watermark.experiment_hash,
                "role": "active",
                "timeframe": watermark.timeframe,
            }
        )
    return values


def _require_matching_repair(gap: DataGap, coverage: GapRepairCoverage) -> None:
    if (
        coverage.series != gap.series
        or coverage.start_at != gap.start_at
        or coverage.end_at != gap.end_at
    ):
        raise ReadinessValidationError(
            "repair must preserve provider, feed, adjustment, symbol, timeframe, and interval"
        )


def _require_contiguous_calendar_range(
    calendar: ExchangeCalendar,
    series: DataSeries,
    start_at: datetime,
    end_at: datetime,
) -> tuple[TradingInterval, ...]:
    expected = calendar.expected_intervals(
        start_at=start_at,
        end_at=end_at,
        timeframe=series.timeframe,
    )
    if (
        not expected
        or expected[0].start_at != start_at
        or expected[-1].end_at != end_at
        or any(left.end_at != right.start_at for left, right in pairwise(expected))
    ):
        raise ReadinessValidationError(
            "gap must contain only contiguous expected intervals from one market session"
        )
    return expected


def _require_events(
    values: Sequence[StoredBarEvent],
    series: DataSeries,
) -> tuple[StoredBarEvent, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ReadinessValidationError("effective events must be a sequence")
    events = tuple(values)
    if any(type(event) is not StoredBarEvent for event in events):
        raise ReadinessValidationError("effective events are invalid")
    if any(
        (
            event.identity.provider,
            event.identity.feed,
            event.identity.adjustment,
            event.identity.symbol,
            event.identity.timeframe,
        )
        != series.hash_input
        for event in events
    ):
        raise ReadinessValidationError("effective event belongs to another data series")
    starts = tuple(event.identity.start_at for event in events)
    if starts != tuple(sorted(set(starts))):
        raise ReadinessValidationError("effective events must be uniquely ordered by interval")
    return events


def _require_gaps(values: Sequence[DataGap], series: DataSeries) -> tuple[DataGap, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ReadinessValidationError("unresolved gaps must be a sequence")
    gaps = tuple(values)
    if any(type(gap) is not DataGap or gap.series != series or not gap.unresolved for gap in gaps):
        raise ReadinessValidationError("unresolved gap input is invalid")
    if gaps != tuple(sorted(gaps, key=lambda gap: (gap.start_at, gap.gap_id))):
        raise ReadinessValidationError("unresolved gaps must be deterministically ordered")
    return gaps


def _require_intervals(
    values: tuple[TradingInterval, ...],
    timeframe: str,
) -> tuple[TradingInterval, ...]:
    return _require_interval_sequence(values, timeframe)


def _require_interval_sequence(
    values: Sequence[TradingInterval],
    timeframe: str,
) -> tuple[TradingInterval, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ReadinessValidationError("trading intervals must be a sequence")
    duration = _timeframe_duration(timeframe)
    intervals = tuple(values)
    if any(
        type(interval) is not TradingInterval or interval.end_at - interval.start_at != duration
        for interval in intervals
    ):
        raise ReadinessValidationError("trading intervals do not match their timeframe")
    if intervals != tuple(sorted(set(intervals))):
        raise ReadinessValidationError("trading intervals must be uniquely ordered")
    return intervals


def _contiguous_groups(
    intervals: Sequence[TradingInterval],
) -> tuple[tuple[TradingInterval, ...], ...]:
    groups: list[list[TradingInterval]] = []
    for interval in intervals:
        if not groups or groups[-1][-1].end_at != interval.start_at:
            groups.append([interval])
        else:
            groups[-1].append(interval)
    return tuple(tuple(group) for group in groups)


def _series_predicates(table: Any, series: DataSeries) -> tuple[Any, ...]:
    return (
        table.c.provider == series.provider,
        table.c.feed == series.feed,
        table.c.adjustment == series.adjustment,
        table.c.symbol == series.symbol,
        table.c.timeframe == series.timeframe,
    )


def _gap_id(
    experiment_hash: str,
    series: DataSeries,
    start_at: datetime,
    end_at: datetime,
) -> str:
    return f"gap_{sha256_hex((experiment_hash, *series.hash_input, start_at, end_at))}"


def _symbol_watermark_id(experiment_hash: str, series: DataSeries) -> str:
    return f"symbol_watermark_{sha256_hex((experiment_hash, *series.hash_input))}"


def _basket_watermark_id(experiment_hash: str, timeframe: str) -> str:
    return f"basket_watermark_{sha256_hex((experiment_hash, 'active', timeframe))}"


def _bar_identity(series: DataSeries, interval: TradingInterval) -> BarIdentity:
    return BarIdentity(
        provider=series.provider,
        feed=series.feed,
        adjustment=series.adjustment,
        symbol=series.symbol,
        timeframe=series.timeframe,
        start_at=interval.start_at,
        end_at=interval.end_at,
    )


def _require_engine(engine: object) -> Engine:
    if not isinstance(engine, Engine):
        raise TypeError("readiness repository requires a concrete SQLAlchemy Engine")
    if engine.dialect.name not in _SUPPORTED_DIALECTS:
        raise ValueError("readiness repository requires PostgreSQL or SQLite")
    if engine.dialect.name == "sqlite":
        schema_map = engine.get_execution_options().get("schema_translate_map")
        if (
            not isinstance(schema_map, dict)
            or schema_map.get(PLATFORM_SCHEMA, object()) is not None
        ):
            raise ValueError("SQLite readiness repository requires the platform schema map")
    return engine


def _require_calendar(calendar: object) -> ExchangeCalendar:
    if not isinstance(calendar, ExchangeCalendar):
        raise TypeError("readiness service requires the exchange-calendar contract")
    if calendar.name != "XNAS":
        raise ReadinessValidationError("only the configured XNAS calendar is supported")
    return calendar


def _require_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise ReadinessValidationError(f"{field_name} is invalid")
    return value


def _require_integrity_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise ReadinessIntegrityError(f"persisted {field_name} is invalid")
    return value


def _require_gap_id(value: object) -> str:
    if type(value) is not str or _GAP_ID.fullmatch(value) is None:
        raise ReadinessValidationError("gap ID is invalid")
    return value


def _timeframe_duration(value: object) -> timedelta:
    if type(value) is not str or value not in _TIMEFRAME_DURATIONS:
        raise ReadinessValidationError("data-series timeframe is unsupported")
    return _TIMEFRAME_DURATIONS[value]


def _require_utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise ReadinessValidationError(f"{field_name} must be a UTC instant") from None


def _optional_utc(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise ReadinessIntegrityError(f"persisted {field_name} is invalid") from None


def _require_utc_range(start_at: object, end_at: object) -> tuple[datetime, datetime]:
    start = _require_utc(start_at, field_name="start_at")
    end = _require_utc(end_at, field_name="end_at")
    if start >= end:
        raise ReadinessValidationError("data interval must have positive duration")
    return start, end


__all__ = [
    "STRICT_COMPLETE_QUALITY",
    "BarQualityPolicy",
    "BasketReadiness",
    "BasketWatermark",
    "DataGap",
    "DataSeries",
    "GapDetection",
    "GapRepairCoverage",
    "GapRepository",
    "GapStatus",
    "ReadinessIntegrityError",
    "ReadinessPersistenceError",
    "ReadinessValidationError",
    "SymbolReadiness",
    "WatermarkRepository",
    "compute_symbol_readiness",
    "detect_data_gaps",
]
