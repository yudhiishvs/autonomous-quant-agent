"""Retained stream retry, cancellation, and durable-state recovery invariants."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from adaptive_trader.platform.data.collector import MarketDataCollector, MarketDataCollectorError
from adaptive_trader.platform.data.provider import (
    FixtureMarketDataProvider,
    MarketDataProviderError,
    StreamEvent,
    StreamEventType,
)
from adaptive_trader.platform.data.watermarks import GapRepository, WatermarkRepository
from adaptive_trader.platform.storage.market_data import MarketDataRepository
from adaptive_trader.platform.storage.tables import aqa_bar_events
from tests.unit.test_platform_data_collector import (
    _START,
    _envelope,
    _MinuteCalendar,
    engine,
    experiment,
)

__all__ = ["engine", "experiment"]


class Stream:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = 0

    def receive(self):
        result = next(self.events)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed += 1


class Provider(FixtureMarketDataProvider):
    def __init__(self, streams):
        super().__init__()
        self.streams = iter(streams)
        self.opens = 0

    def open_stream(self, subscription):
        assert subscription.symbols == ("AMD",)
        self.opens += 1
        selected = next(self.streams)
        if isinstance(selected, Exception):
            raise selected
        return selected


def collector(engine, experiment, provider, clock):
    calendar = _MinuteCalendar()
    return MarketDataCollector(
        experiment=experiment,
        provider=provider,
        calendar=calendar,
        market_data_repository=MarketDataRepository(engine),
        gap_repository=GapRepository(engine, calendar=calendar),
        watermark_repository=WatermarkRepository(engine, calendar=calendar),
        readiness_start_at=_START,
        clock=lambda: clock[0],
        sleep=lambda _: pytest.fail("retained cycles must not sleep"),
    )


def bar(minute):
    return StreamEvent(
        StreamEventType.BAR, _START + timedelta(minutes=20), bar=_envelope(minute=minute)
    )


def test_retained_frame_events_survive_cycle_bound_and_shutdown_is_idempotent(engine, experiment):
    stream = Stream(
        [StreamEvent(StreamEventType.CONNECTED, _START + timedelta(minutes=20)), bar(0), bar(1)]
    )
    provider = Provider([stream])
    clock = [_START + timedelta(minutes=20)]
    cycle = collector(engine, experiment, provider, clock)
    assert cycle.collect_stream_cycle(max_bars=1).inserted == 1
    clock[0] += timedelta(seconds=1)
    assert cycle.collect_stream_cycle(max_bars=1).inserted == 1
    assert provider.opens == 1
    assert cycle.collect_stream_cycle(max_bars=1).received == 0
    cycle.close_stream()
    cycle.close_stream()
    assert stream.closed == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 2


def test_retryable_open_error_honors_cooldown_and_then_recovers(engine, experiment):
    provider = Provider(
        [MarketDataProviderError("offline_transport", retryable=True), Stream([bar(0)])]
    )
    clock = [_START + timedelta(minutes=20)]
    cycle = collector(engine, experiment, provider, clock)
    assert cycle.collect_stream_cycle(max_bars=1).received == 0
    assert cycle.collect_stream_cycle(max_bars=1).received == 0
    assert provider.opens == 1
    clock[0] += timedelta(seconds=1)
    assert cycle.collect_stream_cycle(max_bars=1).inserted == 1
    assert provider.opens == 2


@pytest.mark.parametrize("event_type", [StreamEventType.RATE_LIMITED, StreamEventType.DISCONNECTED])
def test_control_disconnect_closes_connection_and_reopens_after_cooldown(
    engine, experiment, event_type
):
    clock = [_START + timedelta(minutes=20)]
    first = Stream([StreamEvent(event_type, clock[0])])
    provider = Provider([first, Stream([bar(0)])])
    cycle = collector(engine, experiment, provider, clock)
    result = cycle.collect_stream_cycle(max_bars=1)
    assert result.rate_limits == int(event_type is StreamEventType.RATE_LIMITED)
    assert first.closed == 1
    assert cycle.collect_stream_cycle(max_bars=1).received == 0
    clock[0] += timedelta(seconds=1)
    assert cycle.collect_stream_cycle(max_bars=1).inserted == 1


@pytest.mark.parametrize("retryable", [True, False])
def test_receive_error_closes_stream_before_propagation_or_retry(engine, experiment, retryable):
    clock = [_START + timedelta(minutes=20)]
    stream = Stream([MarketDataProviderError("fixture_receive_failure", retryable=retryable)])
    cycle = collector(engine, experiment, Provider([stream]), clock)
    if retryable:
        assert cycle.collect_stream_cycle(max_bars=1).received == 0
    else:
        with pytest.raises(MarketDataProviderError, match="fixture_receive_failure"):
            cycle.collect_stream_cycle(max_bars=1)
    assert stream.closed == 1


def test_retry_exhaustion_is_bounded_without_busy_reconnect(engine, experiment):
    provider = Provider(
        [MarketDataProviderError("fixture_failure", retryable=True) for _ in range(6)]
    )
    clock = [_START + timedelta(minutes=20)]
    cycle = collector(engine, experiment, provider, clock)
    for delay in (1, 2, 4, 8, 16):
        assert cycle.collect_stream_cycle(max_bars=1).received == 0
        clock[0] += timedelta(seconds=delay)
    with pytest.raises(MarketDataCollectorError, match="exhausted"):
        cycle.collect_stream_cycle(max_bars=1)
    assert provider.opens == 6


def test_control_flood_is_bounded_without_fabricated_bars(engine, experiment):
    now = _START + timedelta(minutes=20)
    stream = Stream(
        [StreamEvent(StreamEventType.CONNECTED, now + timedelta(microseconds=i)) for i in range(21)]
    )
    cycle = collector(engine, experiment, Provider([stream]), [now])
    with pytest.raises(MarketDataCollectorError, match="control-event bound"):
        cycle.collect_stream_cycle(max_bars=1)
    cycle.close_stream()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 0


@pytest.mark.parametrize("bound", [0, -1, True, 1.5])
def test_invalid_cycle_limit_has_no_provider_side_effect(engine, experiment, bound):
    provider = Provider([])
    cycle = collector(engine, experiment, provider, [_START])
    with pytest.raises(MarketDataCollectorError, match="positive integer"):
        cycle.collect_stream_cycle(max_bars=bound)
    assert provider.opens == 0
