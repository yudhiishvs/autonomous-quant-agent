"""Known-answer and invariant tests for pure fifteen-minute aggregation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from random import Random

import pytest

from adaptive_trader.platform.data.aggregation import (
    AggregatedBar,
    AggregationError,
    EffectiveBar,
    SessionWindow,
    aggregate_one_minute_bars,
)
from adaptive_trader.platform.data.normalization import CanonicalBar

_SUMMER_OPEN = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_SUMMER_CLOSE = datetime(2026, 7, 6, 20, 0, tzinfo=UTC)


class _Unset:
    pass


_UNSET = _Unset()


def _bar(
    minute: int,
    *,
    start: datetime = _SUMMER_OPEN,
    symbol: str = "AAA",
    provider: str = "alpaca",
    source_mode: str = "external_provider",
    volume: Decimal | _Unset = _UNSET,
    trade_count: int | _Unset | None = _UNSET,
    vwap: Decimal | _Unset | None = _UNSET,
    quality_flags: tuple[str, ...] = ("complete",),
) -> CanonicalBar:
    interval_start = start + timedelta(minutes=minute)
    selected_volume = Decimal(100 + minute) if volume is _UNSET else volume
    selected_trade_count = 10 + minute if trade_count is _UNSET else trade_count
    selected_vwap = Decimal("100.25") + minute if vwap is _UNSET else vwap
    return CanonicalBar(
        provider=provider,
        feed="iex",
        adjustment="raw",
        symbol=symbol,
        timeframe="1Min",
        source_mode=source_mode,
        interval_start_utc=interval_start,
        interval_end_utc=interval_start + timedelta(minutes=1),
        receipt_timestamp_utc=interval_start + timedelta(minutes=1, seconds=30),
        provider_event_timestamp_utc=interval_start,
        open=Decimal(100 + minute),
        high=Decimal(101 + minute),
        low=Decimal(99 + minute),
        close=Decimal("100.5") + minute,
        volume=selected_volume,  # type: ignore[arg-type]
        trade_count=selected_trade_count,  # type: ignore[arg-type]
        vwap=selected_vwap,  # type: ignore[arg-type]
        schema_version=1,
        source_event_id=f"{provider}_fixture_{minute:02d}",
        quality_flags=quality_flags,
        is_correction=False,
        correction_of_source_event_id=None,
    )


def _effective(
    minute: int,
    *,
    start: datetime = _SUMMER_OPEN,
    revision: int = 1,
    **bar_overrides: object,
) -> EffectiveBar:
    return EffectiveBar(
        bar_event_id=f"bar_event_{minute + 1:064x}",
        revision=revision,
        bar=_bar(minute, start=start, **bar_overrides),  # type: ignore[arg-type]
    )


def _constituents(*, start: datetime = _SUMMER_OPEN) -> tuple[EffectiveBar, ...]:
    return tuple(_effective(minute, start=start) for minute in range(15))


def _summer_session() -> SessionWindow:
    return SessionWindow(_SUMMER_OPEN, _SUMMER_CLOSE)


def test_independently_calculated_known_answer() -> None:
    result = aggregate_one_minute_bars(_constituents(), session=_summer_session())

    assert result.bar.open == Decimal("100")
    assert result.bar.high == Decimal("115")
    assert result.bar.low == Decimal("99")
    assert result.bar.close == Decimal("114.5")
    assert result.bar.volume == Decimal("1605")
    assert result.bar.trade_count == 255
    assert result.bar.vwap == Decimal(
        "107.4244548286604361370716510903426791277258566978193146417445483"
    )
    assert result.bar.interval_start_utc == _SUMMER_OPEN
    assert result.bar.interval_end_utc == _SUMMER_OPEN + timedelta(minutes=15)
    assert result.bar.receipt_timestamp_utc == datetime(2026, 7, 6, 13, 45, 30, tzinfo=UTC)
    assert result.lineage_hash == "e186911d00ed70e8579d90a4897f322b125c0568dcf57e403ab5e56d51cc9ded"
    assert (
        result.bar.payload_hash
        == "335aaebb48eda7f04e5566838f161ba1b694cdb6341c03d00231f5ea3715987b"
    )
    assert result.result_hash == "680463ffe786f8a6dde3b8ff23fbaa0becf45fb0bd57b66cefe1e5543b0d6e84"
    assert result.bar.source_event_id == f"aggregate_{result.lineage_hash}"
    assert result.bar.canonical_bytes == (
        b'{"adjustment":"raw","close":"114.5","correction":{"correction_of_source_event_id"'
        b':null,"is_correction":false},"feed":"iex","high":"115","interval_end_utc":'
        b'"2026-07-06T13:45:00.000000Z","interval_start_utc":"2026-07-06T13:30:00.000000Z"'
        b',"low":"99","open":"100","payload_hash":"335aaebb48eda7f04e5566838f161ba1b694c'
        b'db6341c03d00231f5ea3715987b","provider":"alpaca","provider_event_timestamp_utc":null'
        b',"quality_flags":["complete"],"receipt_timestamp_utc":"2026-07-06T13:45:30.000000Z"'
        b',"schema_version":1,"source_event_id":"aggregate_e186911d00ed70e8579d90a4897f322b1'
        b'25c0568dcf57e403ab5e56d51cc9ded","source_mode":"external_provider","symbol":"AAA"'
        b',"timeframe":"15Min","trade_count"'
        b':255,"volume":"1605","vwap":"107.4244548286604361370716510903426791277258566978193146417445483"}'
    )


def test_out_of_order_arrival_converges_to_identical_result() -> None:
    ordered = _constituents()
    shuffled = list(ordered)
    Random(20260905).shuffle(shuffled)

    expected = aggregate_one_minute_bars(ordered, session=_summer_session())
    reversed_result = aggregate_one_minute_bars(tuple(reversed(ordered)), session=_summer_session())
    shuffled_result = aggregate_one_minute_bars(shuffled, session=_summer_session())

    assert reversed_result == expected
    assert shuffled_result == expected
    assert shuffled_result.bar.canonical_bytes == expected.bar.canonical_bytes


def test_offline_fixture_aggregate_retains_non_promotable_provenance() -> None:
    fixture = tuple(
        _effective(
            minute,
            provider="fixture",
            source_mode="offline_fixture",
        )
        for minute in range(15)
    )

    result = aggregate_one_minute_bars(fixture, session=_summer_session())

    assert result.bar.provider == "fixture"
    assert result.bar.source_mode == "offline_fixture"
    assert result.bar.has_promotable_provenance is False
    assert (
        result.bar.payload_hash
        != aggregate_one_minute_bars(
            _constituents(),
            session=_summer_session(),
        ).bar.payload_hash
    )


def test_effective_constituent_revision_changes_lineage_and_result() -> None:
    original = _constituents()
    corrected = list(original)
    corrected[7] = replace(corrected[7], revision=2)

    first = aggregate_one_minute_bars(original, session=_summer_session())
    second = aggregate_one_minute_bars(corrected, session=_summer_session())

    assert second.bar.open == first.bar.open
    assert second.bar.close == first.bar.close
    assert second.lineage_hash != first.lineage_hash
    assert second.bar.payload_hash != first.bar.payload_hash
    assert second.result_hash != first.result_hash


@pytest.mark.parametrize("count", [0, 1, 14, 16])
def test_incomplete_or_oversized_buckets_are_never_materialized(count: int) -> None:
    with pytest.raises(AggregationError, match="exactly 15"):
        aggregate_one_minute_bars(
            tuple(_effective(minute) for minute in range(count)),
            session=_summer_session(),
        )


def test_missing_and_duplicate_minutes_are_not_manufactured() -> None:
    bars = list(_constituents())
    bars[7] = replace(_effective(8), bar_event_id=f"bar_event_{'f' * 64}")

    with pytest.raises(AggregationError, match="exact and contiguous"):
        aggregate_one_minute_bars(bars, session=_summer_session())

    bars = list(_constituents())
    bars[7] = bars[6]
    with pytest.raises(AggregationError, match="duplicate effective event"):
        aggregate_one_minute_bars(bars, session=_summer_session())


def test_mixed_symbol_series_is_rejected() -> None:
    bars = list(_constituents())
    bars[4] = _effective(4, symbol="BBB")

    with pytest.raises(AggregationError, match="share one canonical series"):
        aggregate_one_minute_bars(bars, session=_summer_session())


def test_trade_count_is_null_when_any_constituent_is_null() -> None:
    bars = list(_constituents())
    bars[3] = _effective(3, trade_count=None)

    result = aggregate_one_minute_bars(bars, session=_summer_session())

    assert result.bar.trade_count is None


def test_vwap_is_null_when_any_value_is_missing_or_total_volume_is_zero() -> None:
    missing = list(_constituents())
    missing[3] = _effective(3, vwap=None)
    no_vwap = aggregate_one_minute_bars(missing, session=_summer_session())

    zero_volume = tuple(_effective(index, volume=Decimal(0)) for index in range(15))
    zero_denominator = aggregate_one_minute_bars(zero_volume, session=_summer_session())

    assert no_vwap.bar.vwap is None
    assert zero_denominator.bar.vwap is None
    assert zero_denominator.bar.volume == 0


def test_quality_flags_are_the_deterministic_union() -> None:
    bars = list(_constituents())
    bars[0] = _effective(0, quality_flags=("complete", "repaired"))
    bars[14] = _effective(14, quality_flags=("complete", "late"))

    result = aggregate_one_minute_bars(tuple(reversed(bars)), session=_summer_session())

    assert result.bar.quality_flags == ("complete", "late", "repaired")


def test_alignment_is_derived_from_injected_session_open() -> None:
    misaligned_start = _SUMMER_OPEN + timedelta(minutes=1)

    with pytest.raises(AggregationError, match="aligned from session open"):
        aggregate_one_minute_bars(
            _constituents(start=misaligned_start),
            session=_summer_session(),
        )


def test_dst_specific_utc_session_window_is_injected_not_guessed() -> None:
    winter_open = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    winter_close = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)

    winter = aggregate_one_minute_bars(
        _constituents(start=winter_open),
        session=SessionWindow(winter_open, winter_close),
    )

    assert winter.bar.interval_start_utc.hour == 14
    assert winter.bar.interval_end_utc.hour == 14
    assert winter.bar.interval_end_utc.minute == 45


def test_early_close_window_blocks_a_bucket_that_would_cross_close() -> None:
    early_open = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    early_close = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    crossing_start = early_close - timedelta(minutes=10)

    with pytest.raises(AggregationError, match="crosses its injected session"):
        aggregate_one_minute_bars(
            _constituents(start=crossing_start),
            session=SessionWindow(early_open, early_close),
        )


def test_aggregation_is_independent_of_process_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 6
        low_precision = aggregate_one_minute_bars(_constituents(), session=_summer_session())
    with localcontext() as context:
        context.prec = 50
        high_precision = aggregate_one_minute_bars(_constituents(), session=_summer_session())

    assert low_precision == high_precision


def test_aggregate_and_lineage_contracts_are_immutable() -> None:
    result = aggregate_one_minute_bars(_constituents(), session=_summer_session())

    with pytest.raises(FrozenInstanceError):
        result.lineage_hash = "0" * 64  # type: ignore[misc]
    assert not hasattr(result, "__dict__")
    assert isinstance(result, AggregatedBar)


@pytest.mark.parametrize(
    "bounds",
    [
        (datetime(2026, 7, 6, 9, 30), _SUMMER_CLOSE),
        (_SUMMER_OPEN, _SUMMER_OPEN),
        (_SUMMER_OPEN + timedelta(seconds=1), _SUMMER_CLOSE),
    ],
)
def test_invalid_injected_session_bounds_are_rejected(
    bounds: tuple[datetime, datetime],
) -> None:
    with pytest.raises(AggregationError):
        SessionWindow(*bounds)


def test_constituent_identity_and_revision_are_strict() -> None:
    valid = _effective(0)
    with pytest.raises(AggregationError, match="event ID"):
        replace(valid, bar_event_id="bar_event_bad")
    with pytest.raises(AggregationError, match="revision"):
        replace(valid, revision=True)
