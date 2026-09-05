"""Offline orchestration tests for durable platform market-data collection."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, func, insert, select

from adaptive_trader.platform.config import ExperimentDefinition, load_experiment
from adaptive_trader.platform.data.calendar import TradingInterval, TradingSession
from adaptive_trader.platform.data.collector import RETRY_DELAYS_SECONDS, MarketDataCollector
from adaptive_trader.platform.data.normalization import MarketDataNormalizationError
from adaptive_trader.platform.data.provider import (
    FixtureMarketDataProvider,
    HistoricalRequest,
    HistoricalResult,
    HistoricalStatus,
    RawBarEnvelope,
    StreamEvent,
    StreamEventType,
    StreamSubscription,
)
from adaptive_trader.platform.data.watermarks import (
    DataSeries,
    GapDetection,
    GapRepository,
    GapStatus,
    WatermarkRepository,
)
from adaptive_trader.platform.storage.market_data import BarIdentity, MarketDataRepository
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_experiments,
    metadata,
)

_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"


class _MinuteCalendar:
    @property
    def name(self) -> str:
        return "XNAS"

    def session(self, session_date: date) -> TradingSession | None:
        del session_date
        return None

    def require_entry_session(self, session_date: date) -> TradingSession:
        del session_date
        raise AssertionError("collector does not request strategy-entry authority")

    def expected_intervals(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        timeframe: str,
    ) -> tuple[TradingInterval, ...]:
        assert timeframe == "1Min"
        intervals: list[TradingInterval] = []
        cursor = start_at
        while cursor + timedelta(minutes=1) <= end_at:
            intervals.append(TradingInterval(cursor, cursor + timedelta(minutes=1)))
            cursor += timedelta(minutes=1)
        return tuple(intervals)


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    shipped = load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_CONFIG_ROOT,
    )
    payload = shipped.model_dump(mode="python")
    payload.update(
        {
            "active_tradable": ("AMD",),
            "benchmark_only": (),
            "context_only": (),
            "excluded": ("TSLA",),
            "risk_groups": (
                {
                    "id": "semiconductors",
                    "symbols": ("AMD",),
                    "max_gross_weight": shipped.risk_groups[0].max_gross_weight,
                },
            ),
        }
    )
    return ExperimentDefinition.model_validate(payload)


@pytest.fixture
def engine(tmp_path: Path, experiment: ExperimentDefinition) -> Iterator[Engine]:
    selected = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'collector.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(selected, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(selected)
    with selected.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=experiment.content_hash,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.experiment_version,
                schema_version=experiment.schema_version,
                configuration={"fixture": True},
                content_hash=experiment.content_hash,
                registered_at=_START,
            )
        )
    try:
        yield selected
    finally:
        selected.dispose()


def _payload(*, minute: int, close: int = 101, symbol: str = "AMD") -> dict[str, object]:
    return {
        "S": symbol,
        "t": (_START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "o": 100,
        "h": max(102, close),
        "l": 99,
        "c": close,
        "v": 1_000,
        "n": 20,
        "vw": 100.5,
    }


def _envelope(*, minute: int, close: int = 101, symbol: str = "AMD") -> RawBarEnvelope:
    return RawBarEnvelope(
        payload=_payload(minute=minute, close=close, symbol=symbol),
        received_at=_START + timedelta(minutes=10),
    )


def _collector(
    *,
    engine: Engine,
    experiment: ExperimentDefinition,
    provider: object,
    sleeps: list[int] | None = None,
    clock_start: datetime | None = None,
) -> MarketDataCollector:
    calendar = _MinuteCalendar()
    return MarketDataCollector(
        experiment=experiment,
        provider=provider,  # type: ignore[arg-type]
        calendar=calendar,
        market_data_repository=MarketDataRepository(engine),
        gap_repository=GapRepository(engine, calendar=calendar),
        watermark_repository=WatermarkRepository(engine, calendar=calendar),
        readiness_start_at=_START,
        clock=_Clock(clock_start or _START + timedelta(minutes=20)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_historical_collection_persists_duplicates_corrections_out_of_order_and_gaps(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    provider = FixtureMarketDataProvider(bars=(_envelope(minute=2), _envelope(minute=0)))
    collector = _collector(engine=engine, experiment=experiment, provider=provider)

    first = collector.collect_historical(start_at=_START, end_at=_START + timedelta(minutes=3))
    duplicate = collector._persist_envelope(_envelope(minute=0))
    correction = collector._persist_envelope(_envelope(minute=0, close=102))

    assert first.received == first.inserted == 2
    assert first.gaps_recorded == 1
    assert duplicate.duplicates == 1
    assert correction.corrected == 1
    assert collector._persist_envelope(_envelope(minute=0, close=102)).out_of_order == 1
    gaps = GapRepository(engine, calendar=_MinuteCalendar()).list_unresolved(
        experiment_hash=experiment.content_hash,
        series=DataSeries("fixture", "iex", "raw", "AMD", "1Min"),
    )
    assert len(gaps) == 1
    assert (gaps[0].start_at, gaps[0].end_at) == (
        _START + timedelta(minutes=1),
        _START + timedelta(minutes=2),
    )


class _RecordingProvider:
    name = "fixture"

    def __init__(self, results: list[HistoricalResult]) -> None:
        self.results = results
        self.requests: list[HistoricalRequest] = []

    def fetch_historical(self, request: HistoricalRequest) -> HistoricalResult:
        self.requests.append(request)
        return self.results.pop(0)

    def open_stream(self, subscription: StreamSubscription) -> object:
        del subscription
        raise AssertionError("stream not requested")


def test_restart_resumes_from_durable_contiguous_watermark(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    initial = FixtureMarketDataProvider(bars=(_envelope(minute=0), _envelope(minute=1)))
    _collector(engine=engine, experiment=experiment, provider=initial).collect_historical(
        start_at=_START,
        end_at=_START + timedelta(minutes=2),
    )
    restarted_provider = _RecordingProvider(
        [HistoricalResult(HistoricalStatus.OK, bars=(_envelope(minute=2),))]
    )

    _collector(
        engine=engine,
        experiment=experiment,
        provider=restarted_provider,
        clock_start=_START + timedelta(minutes=21),
    ).collect_historical(start_at=_START, end_at=_START + timedelta(minutes=3))

    assert restarted_provider.requests[0].start_at == _START + timedelta(minutes=2)
    assert restarted_provider.requests[0].symbols == ("AMD",)


def test_rate_limits_use_exact_bounded_retry_schedule(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    provider = _RecordingProvider(
        [
            HistoricalResult(HistoricalStatus.RATE_LIMITED, retry_after_seconds=99)
            for _ in RETRY_DELAYS_SECONDS
        ]
        + [HistoricalResult(HistoricalStatus.OK, bars=(_envelope(minute=0),))]
    )
    sleeps: list[int] = []
    collector = _collector(
        engine=engine,
        experiment=experiment,
        provider=provider,
        sleeps=sleeps,
    )

    result = collector.collect_historical(
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
    )

    assert sleeps == [1, 2, 4, 8, 16]
    assert result.rate_limits == 5
    assert len(provider.requests) == 6


class _RepairProvider(_RecordingProvider):
    def __init__(self, repository: GapRepository, detection: GapDetection) -> None:
        super().__init__([HistoricalResult(HistoricalStatus.OK, bars=(_envelope(minute=0),))])
        self.repository = repository
        self.detection = detection
        self.observed_status: GapStatus | None = None

    def fetch_historical(self, request: HistoricalRequest) -> HistoricalResult:
        persisted = self.repository.get(self.detection.gap_id)
        self.observed_status = None if persisted is None else persisted.status
        return super().fetch_historical(request)


def test_gap_is_durable_and_claimed_before_repair_provider_call(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    calendar = _MinuteCalendar()
    repository = GapRepository(engine, calendar=calendar)
    detection = GapDetection(
        experiment_hash=experiment.content_hash,
        series=DataSeries("fixture", "iex", "raw", "AMD", "1Min"),
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
        reason_code="missing_expected_bar",
        detected_at=_START + timedelta(minutes=10),
    )
    provider = _RepairProvider(repository, detection)

    result = _collector(engine=engine, experiment=experiment, provider=provider).repair_gap(
        detection
    )

    assert provider.observed_status is GapStatus.REPAIRING
    assert result.status is GapStatus.RESOLVED


def test_gap_repair_consumes_bounded_pagination(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    detection = GapDetection(
        experiment_hash=experiment.content_hash,
        series=DataSeries("fixture", "iex", "raw", "AMD", "1Min"),
        start_at=_START,
        end_at=_START + timedelta(minutes=2),
        reason_code="missing_expected_bar",
        detected_at=_START + timedelta(minutes=10),
    )
    provider = _RecordingProvider(
        [
            HistoricalResult(
                HistoricalStatus.OK,
                bars=(_envelope(minute=0),),
                next_page_token="page-2",
            ),
            HistoricalResult(HistoricalStatus.OK, bars=(_envelope(minute=1),)),
        ]
    )

    repaired = _collector(
        engine=engine,
        experiment=experiment,
        provider=provider,
    ).repair_gap(detection)

    assert repaired.status is GapStatus.RESOLVED
    assert [request.page_token for request in provider.requests] == [None, "page-2"]


def test_gap_repair_repeated_page_token_fails_closed_and_reopens_claim(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    calendar = _MinuteCalendar()
    repository = GapRepository(engine, calendar=calendar)
    detection = GapDetection(
        experiment_hash=experiment.content_hash,
        series=DataSeries("fixture", "iex", "raw", "AMD", "1Min"),
        start_at=_START,
        end_at=_START + timedelta(minutes=2),
        reason_code="missing_expected_bar",
        detected_at=_START + timedelta(minutes=10),
    )
    provider = _RecordingProvider(
        [
            HistoricalResult(HistoricalStatus.OK, next_page_token="repeated"),
            HistoricalResult(HistoricalStatus.OK, next_page_token="repeated"),
        ]
    )

    with pytest.raises(RuntimeError, match="pagination token repeated"):
        _collector(engine=engine, experiment=experiment, provider=provider).repair_gap(detection)

    persisted = repository.get(detection.gap_id)
    assert persisted is not None
    assert persisted.status is GapStatus.OPEN
    assert persisted.attempt_count == 1


def test_repair_recovers_a_durable_interrupted_claim_after_restart(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    calendar = _MinuteCalendar()
    repository = GapRepository(engine, calendar=calendar)
    detection = GapDetection(
        experiment_hash=experiment.content_hash,
        series=DataSeries("fixture", "iex", "raw", "AMD", "1Min"),
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
        reason_code="missing_expected_bar",
        detected_at=_START + timedelta(minutes=10),
    )
    repository.record(detection)
    repository.begin_repair(
        detection.gap_id,
        attempted_at=_START + timedelta(minutes=11),
    )
    provider = _RepairProvider(repository, detection)

    recovered = _collector(
        engine=engine,
        experiment=experiment,
        provider=provider,
        clock_start=_START + timedelta(minutes=20),
    ).repair_gap(detection)

    assert provider.observed_status is GapStatus.REPAIRING
    assert recovered.status is GapStatus.RESOLVED
    assert recovered.attempt_count == 2


def test_invalid_or_excluded_provider_bar_is_rejected_before_persistence(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    provider = _RecordingProvider(
        [HistoricalResult(HistoricalStatus.OK, bars=(_envelope(minute=0, symbol="TSLA"),))]
    )

    with pytest.raises(MarketDataNormalizationError, match="excluded"):
        _collector(engine=engine, experiment=experiment, provider=provider).collect_historical(
            start_at=_START,
            end_at=_START + timedelta(minutes=1),
        )
    with engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 0


def test_fixture_stream_uses_exact_allowlist_without_network(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    event = StreamEvent(
        event_type=StreamEventType.BAR,
        occurred_at=_START + timedelta(minutes=10),
        bar=_envelope(minute=0),
    )
    provider = FixtureMarketDataProvider(stream_events=(event,))

    result = _collector(engine=engine, experiment=experiment, provider=provider).collect_stream(
        max_events=1
    )

    assert result.inserted == 1


def test_stream_jump_persists_calendar_gap_before_readiness_advances(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    events = tuple(
        StreamEvent(
            event_type=StreamEventType.BAR,
            occurred_at=_START + timedelta(minutes=10, seconds=minute),
            bar=_envelope(minute=minute),
        )
        for minute in (0, 2)
    )
    provider = FixtureMarketDataProvider(stream_events=events)

    result = _collector(engine=engine, experiment=experiment, provider=provider).collect_stream(
        max_events=2
    )

    gaps = GapRepository(engine, calendar=_MinuteCalendar()).list_unresolved(
        experiment_hash=experiment.content_hash,
        series=DataSeries("fixture", "iex", "raw", "AMD", "1Min"),
    )
    watermark = MarketDataRepository(engine).watermark(
        experiment_hash=experiment.content_hash,
        identity=BarIdentity(
            provider="fixture",
            feed="iex",
            adjustment="raw",
            symbol="AMD",
            timeframe="1Min",
            start_at=_START,
            end_at=_START + timedelta(minutes=1),
        ),
    )

    assert result.gaps_recorded == 1
    assert len(gaps) == 1
    assert (gaps[0].start_at, gaps[0].end_at) == (
        _START + timedelta(minutes=1),
        _START + timedelta(minutes=2),
    )
    assert watermark is not None
    assert watermark.contiguous_through == _START + timedelta(minutes=1)


class _OfflineAlpacaProvider:
    name = "alpaca"

    def fetch_historical(self, request: HistoricalRequest) -> HistoricalResult:
        del request
        raise AssertionError("direct persistence test does not fetch")

    def open_stream(self, subscription: StreamSubscription) -> object:
        del subscription
        raise AssertionError("direct persistence test does not connect")


def test_same_valued_alpaca_update_is_a_canonical_correction(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    collector = _collector(
        engine=engine,
        experiment=experiment,
        provider=_OfflineAlpacaProvider(),
    )
    original = RawBarEnvelope(
        payload={**_payload(minute=0), "T": "b"},
        received_at=_START + timedelta(minutes=10),
    )
    updated = RawBarEnvelope(
        payload={**_payload(minute=0), "T": "u"},
        received_at=_START + timedelta(minutes=10, seconds=1),
        is_correction=True,
    )

    inserted = collector._persist_envelope(original)
    corrected = collector._persist_envelope(updated)
    identity = collector._anchor_identity("AMD")
    events = MarketDataRepository(engine).list_events(identity)

    assert inserted.inserted == 1
    assert corrected.corrected == 1
    assert len(events) == 2
    assert events[0].bar.is_correction is False
    assert events[1].bar.is_correction is True
    assert events[0].normalized_payload_hash != events[1].normalized_payload_hash


class _FiniteStream:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.events = events

    def receive(self) -> StreamEvent:
        return self.events.pop(0)

    def close(self) -> None:
        pass


class _DisconnectingProvider(_RecordingProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.subscriptions: list[StreamSubscription] = []

    def open_stream(self, subscription: StreamSubscription) -> _FiniteStream:
        self.subscriptions.append(subscription)
        attempt = len(self.subscriptions) - 1
        occurred_at = _START + timedelta(minutes=10, seconds=attempt)
        return _FiniteStream(
            [
                StreamEvent(StreamEventType.CONNECTED, occurred_at),
                StreamEvent(
                    StreamEventType.DISCONNECTED,
                    occurred_at,
                    reason_code="remote_closed",
                ),
            ]
        )


def test_stream_reconnects_are_audited_with_exact_bounded_delays(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    provider = _DisconnectingProvider()
    sleeps: list[int] = []
    collector = _collector(
        engine=engine,
        experiment=experiment,
        provider=provider,
        sleeps=sleeps,
    )

    collector.collect_stream(max_events=12)

    assert sleeps == [1, 2, 4, 8, 16]
    assert len(provider.subscriptions) == 6
    assert all(item.symbols == ("AMD",) for item in provider.subscriptions)
    events = AuditRepository(engine).list_events(
        stream_id=f"aqa_collector:market_data:{experiment.content_hash}"
    )
    assert events[0].event_type == "collector.connected"
    assert [item.event_type for item in events].count("collector.reconnected") == 5
    assert [item.event_type for item in events].count("collector.disconnected") == 6


def test_bar_is_committed_before_watermark_failure(
    engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    collector = _collector(
        engine=engine,
        experiment=experiment,
        provider=FixtureMarketDataProvider(),
    )

    class InjectedWatermarkFailure(RuntimeError):
        pass

    class FailingWatermarks:
        def recompute_symbol(self, **values: object) -> None:
            del values
            with engine.begin() as connection:
                count = connection.scalar(select(func.count()).select_from(aqa_bar_events))
            assert count == 1
            raise InjectedWatermarkFailure

    collector._watermarks = FailingWatermarks()  # type: ignore[assignment]
    with pytest.raises(InjectedWatermarkFailure):
        collector._persist_envelope(_envelope(minute=0))

    with engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 1
