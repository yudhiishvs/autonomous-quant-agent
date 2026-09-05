"""Pure deterministic one-minute to fifteen-minute market-bar aggregation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, localcontext

from adaptive_trader.platform.constants import (
    MAX_DOMAIN_DECIMAL_DIGITS,
    MAX_SIGNED_64_BIT_INTEGER,
)
from adaptive_trader.platform.data.normalization import CanonicalBar
from adaptive_trader.platform.domain import require_finite_decimal, require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

_EVENT_ID_PATTERN = re.compile(r"^bar_event_[0-9a-f]{64}$", flags=re.ASCII)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_SOURCE_COUNT = 15
_SOURCE_DURATION = timedelta(minutes=1)
_TARGET_DURATION = timedelta(minutes=15)


class AggregationError(DomainValidationError):
    """Raised when minute bars cannot form one complete session-aligned bucket."""


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """Calendar-injected regular-session bounds used by the pure aggregator.

    A calendar adapter owns timezone, holiday, DST, and early-close decisions. This narrow value
    gives aggregation only the resolved UTC bounds, so the transformation has no wall-clock or
    global-calendar dependency.
    """

    session_open_utc: datetime
    session_close_utc: datetime

    def __post_init__(self) -> None:
        start = _require_utc(self.session_open_utc, field_name="session_open_utc")
        end = _require_utc(self.session_close_utc, field_name="session_close_utc")
        if start.second != 0 or start.microsecond != 0 or end.second != 0 or end.microsecond != 0:
            raise AggregationError("session bounds must be minute-aligned")
        if end <= start:
            raise AggregationError("session close must follow session open")
        object.__setattr__(self, "session_open_utc", start)
        object.__setattr__(self, "session_close_utc", end)


@dataclass(frozen=True, slots=True)
class EffectiveBar:
    """One current immutable event revision selected by the latest projection."""

    bar_event_id: str
    revision: int
    bar: CanonicalBar

    def __post_init__(self) -> None:
        if (
            type(self.bar_event_id) is not str
            or _EVENT_ID_PATTERN.fullmatch(self.bar_event_id) is None
        ):
            raise AggregationError("constituent bar event ID is invalid")
        if type(self.revision) is not int or not 1 <= self.revision <= MAX_SIGNED_64_BIT_INTEGER:
            raise AggregationError("constituent bar revision is invalid")
        if type(self.bar) is not CanonicalBar:
            raise AggregationError("constituent canonical bar is invalid")

    @property
    def lineage_item(self) -> tuple[str, int]:
        """Return the exact ordered lineage preimage for this effective event."""

        return (self.bar_event_id, self.revision)


@dataclass(frozen=True, slots=True)
class AggregatedBar:
    """One deterministic aggregate plus ordered effective-revision lineage."""

    bar: CanonicalBar
    constituents: tuple[EffectiveBar, ...]
    lineage_hash: str
    result_hash: str

    def __post_init__(self) -> None:
        if type(self.bar) is not CanonicalBar or self.bar.timeframe != "15Min":
            raise AggregationError("aggregate canonical bar is invalid")
        if type(self.constituents) is not tuple or len(self.constituents) != _SOURCE_COUNT:
            raise AggregationError("aggregate must retain exactly 15 constituents")
        if any(type(item) is not EffectiveBar for item in self.constituents):
            raise AggregationError("aggregate constituent is invalid")
        ordered = tuple(sorted(self.constituents, key=lambda item: item.bar.interval_start_utc))
        if self.constituents != ordered:
            raise AggregationError("aggregate constituents must be ordered")
        expected_lineage = _lineage_hash(self.constituents)
        if (
            type(self.lineage_hash) is not str
            or _HASH_PATTERN.fullmatch(self.lineage_hash) is None
            or self.lineage_hash != expected_lineage
        ):
            raise AggregationError("aggregate lineage hash is invalid")
        expected_result = sha256_hex(
            ("canonical_aggregate_result_v1", self.bar.payload_hash, self.lineage_hash)
        )
        if (
            type(self.result_hash) is not str
            or _HASH_PATTERN.fullmatch(self.result_hash) is None
            or self.result_hash != expected_result
        ):
            raise AggregationError("aggregate result hash is invalid")


def aggregate_one_minute_bars(
    constituents: Sequence[EffectiveBar],
    *,
    session: SessionWindow,
) -> AggregatedBar:
    """Return one complete, calendar-resolved 15-minute aggregate.

    Input order is irrelevant. Missing or duplicate minutes, mixed identities, cross-session
    buckets, and partial early-close buckets fail closed; no minute is manufactured.
    """

    if type(session) is not SessionWindow:
        raise AggregationError("aggregation session is invalid")
    try:
        frozen = tuple(constituents)
    except TypeError:
        raise AggregationError("aggregate constituents must be a finite sequence") from None
    if len(frozen) != _SOURCE_COUNT or any(type(item) is not EffectiveBar for item in frozen):
        raise AggregationError("a complete aggregate requires exactly 15 effective bars")
    ordered = tuple(sorted(frozen, key=lambda item: item.bar.interval_start_utc))
    if len({item.bar_event_id for item in ordered}) != _SOURCE_COUNT:
        raise AggregationError("aggregate contains a duplicate effective event")

    first = ordered[0].bar
    if first.timeframe != "1Min":
        raise AggregationError("aggregate source timeframe must be 1Min")
    expected_series = (
        first.provider,
        first.feed,
        first.adjustment,
        first.symbol,
        first.schema_version,
        first.source_mode,
    )
    for index, item in enumerate(ordered):
        bar = item.bar
        if (
            bar.timeframe != "1Min"
            or (
                bar.provider,
                bar.feed,
                bar.adjustment,
                bar.symbol,
                bar.schema_version,
                bar.source_mode,
            )
            != expected_series
        ):
            raise AggregationError("aggregate source bars must share one canonical series")
        expected_start = first.interval_start_utc + (index * _SOURCE_DURATION)
        if bar.interval_start_utc != expected_start or bar.interval_end_utc != (
            expected_start + _SOURCE_DURATION
        ):
            raise AggregationError("aggregate source minutes must be exact and contiguous")

    start = first.interval_start_utc
    end = ordered[-1].bar.interval_end_utc
    if start < session.session_open_utc or end > session.session_close_utc:
        raise AggregationError("aggregate bucket crosses its injected session boundary")
    offset = start - session.session_open_utc
    if offset % _TARGET_DURATION != timedelta(0) or end - start != _TARGET_DURATION:
        raise AggregationError("aggregate bucket is not aligned from session open")

    try:
        arithmetic = Context(prec=MAX_DOMAIN_DECIMAL_DIGITS, rounding=ROUND_HALF_EVEN)
        with localcontext(arithmetic):
            volume = sum((item.bar.volume for item in ordered), Decimal(0))
            weighted_vwap = sum(
                (item.bar.vwap * item.bar.volume for item in ordered if item.bar.vwap is not None),
                Decimal(0),
            )
            all_vwap = all(item.bar.vwap is not None for item in ordered)
            vwap = weighted_vwap / volume if all_vwap and volume > 0 else None
    except DecimalException:
        raise AggregationError("aggregate decimal arithmetic failed") from None
    try:
        volume = require_finite_decimal(volume, field_name="aggregate.volume")
        if vwap is not None:
            vwap = require_finite_decimal(vwap, field_name="aggregate.vwap")
    except DomainValidationError:
        raise AggregationError("aggregate numeric result exceeds its limits") from None

    trade_count: int | None
    if all(item.bar.trade_count is not None for item in ordered):
        trade_count = sum(
            item.bar.trade_count for item in ordered if item.bar.trade_count is not None
        )
        if trade_count > MAX_SIGNED_64_BIT_INTEGER:
            raise AggregationError("aggregate trade count exceeds its limit")
    else:
        trade_count = None

    lineage_hash = _lineage_hash(ordered)
    quality_flags = tuple(sorted({flag for item in ordered for flag in item.bar.quality_flags}))
    aggregate = CanonicalBar(
        provider=first.provider,
        feed=first.feed,
        adjustment=first.adjustment,
        symbol=first.symbol,
        timeframe="15Min",
        source_mode=first.source_mode,
        interval_start_utc=start,
        interval_end_utc=end,
        receipt_timestamp_utc=max(item.bar.receipt_timestamp_utc for item in ordered),
        provider_event_timestamp_utc=None,
        open=first.open,
        high=max(item.bar.high for item in ordered),
        low=min(item.bar.low for item in ordered),
        close=ordered[-1].bar.close,
        volume=volume,
        trade_count=trade_count,
        vwap=vwap,
        schema_version=1,
        source_event_id=f"aggregate_{lineage_hash}",
        quality_flags=quality_flags,
        is_correction=False,
        correction_of_source_event_id=None,
    )
    result_hash = sha256_hex(
        ("canonical_aggregate_result_v1", aggregate.payload_hash, lineage_hash)
    )
    return AggregatedBar(
        bar=aggregate,
        constituents=ordered,
        lineage_hash=lineage_hash,
        result_hash=result_hash,
    )


def _lineage_hash(constituents: tuple[EffectiveBar, ...]) -> str:
    return sha256_hex(
        (
            "canonical_bar_lineage_v1",
            tuple(item.lineage_item for item in constituents),
        )
    )


def _require_utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise AggregationError(f"{field_name} must be timezone-aware UTC") from None
