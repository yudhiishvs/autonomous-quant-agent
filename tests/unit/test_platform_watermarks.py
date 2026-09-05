"""Calendar-aware durable gap and watermark tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, insert, select, update

from adaptive_trader.platform.data.calendar import TradingInterval, XnasExchangeCalendar
from adaptive_trader.platform.data.watermarks import (
    STRICT_COMPLETE_QUALITY,
    BarQualityPolicy,
    BasketReadiness,
    BasketStatus,
    DataGap,
    DataSeries,
    GapDetection,
    GapRepairCoverage,
    GapRepository,
    GapStatus,
    ReadinessIntegrityError,
    ReadinessValidationError,
    WatermarkRepository,
    compute_symbol_readiness,
    detect_data_gaps,
)
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    MarketDataRepository,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_basket_watermarks,
    aqa_data_gaps,
    aqa_experiments,
    aqa_symbol_watermarks,
    metadata,
)
from adaptive_trader.platform.universe import UniverseSpec

_EXPERIMENT_HASH = "a" * 64
_START = datetime(2024, 7, 2, 13, 30, tzinfo=UTC)
_END = _START + timedelta(minutes=3)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'readiness.sqlite3'}",
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
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="readiness_test",
                experiment_version=1,
                schema_version=1,
                configuration={"fixture": True},
                content_hash="b" * 64,
                registered_at=_START,
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def calendar() -> XnasExchangeCalendar:
    return XnasExchangeCalendar()


def _series(symbol: str = "NVDA") -> DataSeries:
    return DataSeries(
        provider="fixture",
        feed="iex",
        adjustment="raw",
        symbol=symbol,
        timeframe="1Min",
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"provider": "polygon"}, "provider is unsupported"),
        ({"feed": "sip"}, "exact IEX"),
        ({"feed": "fallback"}, "exact IEX"),
        ({"adjustment": "all"}, "must be raw"),
    ],
)
def test_data_series_rejects_unsupported_provider_feed_and_adjustment(
    changes: dict[str, str],
    message: str,
) -> None:
    values = {
        "provider": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "symbol": "NVDA",
        "timeframe": "1Min",
        **changes,
    }
    with pytest.raises(ReadinessValidationError, match=message):
        DataSeries(**values)


def _interval(minute: int, *, day_start: datetime = _START) -> TradingInterval:
    start = day_start + timedelta(minutes=minute)
    return TradingInterval(start, start + timedelta(minutes=1))


def _bar(
    series: DataSeries,
    interval: TradingInterval,
    *,
    close: str = "10.25",
    flags: tuple[str, ...] = ("complete",),
    receipt_offset: int = 1,
) -> BarWrite:
    return BarWrite(
        identity=BarIdentity(
            provider=series.provider,
            feed=series.feed,
            adjustment=series.adjustment,
            symbol=series.symbol,
            timeframe=series.timeframe,
            start_at=interval.start_at,
            end_at=interval.end_at,
        ),
        received_at=interval.end_at + timedelta(seconds=receipt_offset),
        provider_timestamp=interval.end_at,
        open=Decimal("10.00"),
        high=Decimal("11.00"),
        low=Decimal("9.00"),
        close=Decimal(close),
        volume=Decimal("1000"),
        trade_count=10,
        vwap=Decimal("10.20"),
        quality_flags=flags,
        source="fixture",
        source_event_id=f"{series.symbol}-{interval.start_at.isoformat()}-{close}",
        source_payload_hash=sha256_hex((series.symbol, interval.start_at, close, flags)),
    )


def _detect(
    calendar: XnasExchangeCalendar,
    series: DataSeries,
    *,
    observed: tuple[TradingInterval, ...],
    start: datetime = _START,
    end: datetime = _END,
) -> tuple[GapDetection, ...]:
    return detect_data_gaps(
        calendar=calendar,
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=start,
        end_at=end,
        observed_intervals=observed,
        detected_at=end + timedelta(minutes=1),
    )


def test_gap_detection_splits_sessions_and_never_models_closed_time(
    calendar: XnasExchangeCalendar,
) -> None:
    friday_start = datetime(2024, 7, 5, 19, 59, tzinfo=UTC)
    monday_start = datetime(2024, 7, 8, 13, 30, tzinfo=UTC)
    detections = _detect(
        calendar,
        _series(),
        observed=(),
        start=friday_start,
        end=monday_start + timedelta(minutes=1),
    )

    assert [(gap.start_at, gap.end_at) for gap in detections] == [
        (friday_start, friday_start + timedelta(minutes=1)),
        (monday_start, monday_start + timedelta(minutes=1)),
    ]
    assert (
        _detect(
            calendar,
            _series(),
            observed=(),
            start=datetime(2024, 7, 6, 0, 0, tzinfo=UTC),
            end=datetime(2024, 7, 8, 0, 0, tzinfo=UTC),
        )
        == ()
    )


def test_gap_detection_rejects_observations_outside_expected_calendar(
    calendar: XnasExchangeCalendar,
) -> None:
    overnight = TradingInterval(
        datetime(2024, 7, 2, 1, 0, tzinfo=UTC),
        datetime(2024, 7, 2, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ReadinessValidationError, match="not expected"):
        _detect(calendar, _series(), observed=(overnight,))


def test_gap_is_committed_before_repair_and_exact_retry_is_idempotent(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    repository = GapRepository(sqlite_engine, calendar=calendar)
    detection = _detect(calendar, _series(), observed=(_interval(0), _interval(2)))[0]
    callback_states: list[GapStatus] = []

    def repairer(claimed: DataGap) -> GapRepairCoverage:
        visible = repository.get(claimed.gap_id)
        assert visible is not None
        assert claimed.last_attempt_at is not None
        callback_states.append(visible.status)
        return GapRepairCoverage(
            series=claimed.series,
            start_at=claimed.start_at,
            end_at=claimed.end_at,
            observed_intervals=(_interval(1),),
            completed_at=claimed.last_attempt_at + timedelta(seconds=1),
        )

    result = repository.repair(
        detection,
        attempted_at=detection.detected_at + timedelta(seconds=1),
        repairer=repairer,
    )
    repeated = repository.record(
        replace(detection, detected_at=detection.detected_at + timedelta(1))
    )
    called = False

    def should_not_run(claimed: DataGap) -> GapRepairCoverage:
        nonlocal called
        called = True
        raise AssertionError(claimed)

    replay = repository.repair(
        replace(detection, detected_at=detection.detected_at + timedelta(2)),
        attempted_at=detection.detected_at + timedelta(seconds=3),
        repairer=should_not_run,
    )

    assert callback_states == [GapStatus.REPAIRING]
    assert result.status is GapStatus.RESOLVED
    assert result.attempt_count == 1
    assert repeated == result
    assert replay == result
    assert called is False


def test_partial_and_ambiguous_repairs_remain_unresolved_and_can_retry(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    repository = GapRepository(sqlite_engine, calendar=calendar)
    detection = _detect(calendar, _series("AMD"), observed=())[0]
    first_claim = repository.begin_repair(
        repository.record(detection).gap_id,
        attempted_at=detection.detected_at + timedelta(seconds=1),
    )
    partial = repository.complete_repair(
        first_claim.gap_id,
        coverage=GapRepairCoverage(
            series=first_claim.series,
            start_at=first_claim.start_at,
            end_at=first_claim.end_at,
            observed_intervals=(_interval(0), _interval(1)),
            completed_at=first_claim.last_attempt_at + timedelta(seconds=1),
        ),
    )
    assert partial.last_attempt_at is not None
    second_claim = repository.begin_repair(
        partial.gap_id,
        attempted_at=partial.last_attempt_at + timedelta(seconds=2),
    )
    assert second_claim.last_attempt_at is not None
    ambiguous = repository.complete_repair(
        second_claim.gap_id,
        coverage=GapRepairCoverage(
            series=second_claim.series,
            start_at=second_claim.start_at,
            end_at=second_claim.end_at,
            observed_intervals=(_interval(0), _interval(1), _interval(2)),
            completed_at=second_claim.last_attempt_at + timedelta(seconds=1),
            unambiguous=False,
        ),
    )

    assert partial.status is GapStatus.OPEN
    assert partial.attempt_count == 1
    assert ambiguous.status is GapStatus.OPEN
    assert ambiguous.attempt_count == 2
    assert ambiguous.resolved_at is None


def test_interrupted_repair_is_durably_reopened_for_restart(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    repository = GapRepository(sqlite_engine, calendar=calendar)
    detection = _detect(calendar, _series("INSG"), observed=())[0]

    class RepairFailure(RuntimeError):
        pass

    def fail_after_claim(claimed: DataGap) -> GapRepairCoverage:
        visible = repository.get(claimed.gap_id)
        assert visible is not None
        assert visible.status is GapStatus.REPAIRING
        raise RepairFailure("fixture repair failure")

    with pytest.raises(RepairFailure, match="fixture"):
        repository.repair(
            detection,
            attempted_at=detection.detected_at + timedelta(seconds=1),
            repairer=fail_after_claim,
        )

    reopened = repository.get(detection.gap_id)
    assert reopened is not None
    assert reopened.status is GapStatus.OPEN
    assert reopened.attempt_count == 1
    assert reopened.version == 3

    claimed_again = repository.begin_repair(
        reopened.gap_id,
        attempted_at=detection.detected_at + timedelta(seconds=2),
    )
    assert claimed_again.status is GapStatus.REPAIRING
    assert claimed_again.attempt_count == 2


def test_repair_identity_drift_is_rejected_and_gap_remains_unresolved(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    repository = GapRepository(sqlite_engine, calendar=calendar)
    detection = _detect(calendar, _series("CSCO"), observed=())[0]
    claimed = repository.begin_repair(
        repository.record(detection).gap_id,
        attempted_at=detection.detected_at + timedelta(seconds=1),
    )
    assert claimed.last_attempt_at is not None

    with pytest.raises(ReadinessValidationError, match="preserve provider"):
        repository.complete_repair(
            claimed.gap_id,
            coverage=GapRepairCoverage(
                series=replace(claimed.series, provider="alpaca"),
                start_at=claimed.start_at,
                end_at=claimed.end_at,
                observed_intervals=(_interval(0), _interval(1), _interval(2)),
                completed_at=claimed.last_attempt_at + timedelta(seconds=1),
            ),
        )

    persisted = repository.get(claimed.gap_id)
    assert persisted is not None
    assert persisted.status is GapStatus.REPAIRING


def test_persisted_gap_tampering_fails_closed(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    repository = GapRepository(sqlite_engine, calendar=calendar)
    detection = _detect(calendar, _series("AXTI"), observed=())[0]
    persisted = repository.record(detection)
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_data_gaps)
            .where(aqa_data_gaps.c.gap_id == persisted.gap_id)
            .values(attempt_count=99)
        )

    with pytest.raises(ReadinessIntegrityError, match=r"persisted gap|attempted gap"):
        repository.get(persisted.gap_id)


def test_symbol_recomputation_is_contiguous_revision_aware_and_restart_stable(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    series = _series()
    market = MarketDataRepository(sqlite_engine)
    gap_repository = GapRepository(sqlite_engine, calendar=calendar)
    repository = WatermarkRepository(sqlite_engine, calendar=calendar)
    first = market.append(_bar(series, _interval(0))).event
    third = market.append(_bar(series, _interval(2))).event
    gap = gap_repository.record(_detect(calendar, series, observed=(_interval(0), _interval(2)))[0])

    initial_readiness, initial_watermark = repository.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_END,
        updated_at=_END + timedelta(minutes=1),
    )
    market.append(_bar(series, _interval(1)))
    claimed = gap_repository.begin_repair(
        gap.gap_id,
        attempted_at=_END + timedelta(minutes=2),
    )
    assert claimed.last_attempt_at is not None
    gap_repository.complete_repair(
        gap.gap_id,
        coverage=GapRepairCoverage(
            series=series,
            start_at=gap.start_at,
            end_at=gap.end_at,
            observed_intervals=(_interval(1),),
            completed_at=claimed.last_attempt_at + timedelta(seconds=1),
        ),
    )
    complete_readiness, complete_watermark = repository.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_END,
        updated_at=_END + timedelta(minutes=3),
    )
    restarted = WatermarkRepository(sqlite_engine, calendar=calendar)
    replay_readiness, replay_watermark = restarted.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_END,
        updated_at=_END + timedelta(minutes=4),
    )

    assert initial_readiness.contiguous_through == _interval(0).end_at
    assert initial_readiness.blocking_gap_ids == (gap.gap_id,)
    assert initial_watermark is not None
    assert initial_watermark.latest_bar_event_id == first.bar_event_id
    assert complete_readiness.ready_through_range is True
    assert complete_watermark is not None
    assert complete_watermark.contiguous_through == _END
    assert complete_watermark.latest_bar_event_id == third.bar_event_id
    assert replay_readiness == complete_readiness
    assert replay_watermark == complete_watermark

    corrected = market.append(_bar(series, _interval(2), close="10.75", receipt_offset=20)).event
    corrected_readiness, corrected_watermark = restarted.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_END,
        updated_at=_END + timedelta(minutes=5),
    )
    assert corrected_readiness.ready_through_range is True
    assert corrected_watermark is not None
    assert corrected_watermark.version == complete_watermark.version + 1
    assert corrected_watermark.latest_bar_event_id == corrected.bar_event_id
    assert corrected_watermark.quality_hash != complete_watermark.quality_hash

    audit = AuditRepository(sqlite_engine, writer=AuditWriter.COLLECTOR)
    gap_history = audit.list_events(stream_id=f"aqa_collector:gap:{gap.gap_id}")
    watermark_history = audit.list_events(
        stream_id=f"aqa_collector:data:{corrected_watermark.symbol_watermark_id}"
    )
    assert [item.event_type for item in gap_history] == [
        "gap.detected",
        "gap.repair_started",
        "gap.resolved",
    ]
    assert [item.payload["version"] for item in watermark_history] == [1, 2, 3]


def test_quality_gate_and_pure_recomputation_fail_closed_on_unknown_flags(
    sqlite_engine: Engine,
) -> None:
    market = MarketDataRepository(sqlite_engine)
    series = _series("HLIT")
    first = market.append(_bar(series, _interval(0))).event
    second = market.append(_bar(series, _interval(1), flags=("suspect",))).event
    policy = BarQualityPolicy("strict_complete_v1", ("complete",))
    result = compute_symbol_readiness(
        series=series,
        expected_intervals=(_interval(0), _interval(1)),
        effective_events=(first, second),
        unresolved_gaps=(),
        quality_policy=policy,
    )

    assert policy == STRICT_COMPLETE_QUALITY
    assert result.contiguous_through == _interval(0).end_at
    assert result.blocking_interval == _interval(1)
    assert result.latest_contiguous_event == first


def test_active_basket_uses_all_and_only_active_symbols(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    universe = UniverseSpec(
        active_tradable=("AMD", "NVDA"),
        benchmark_only=("SOXX",),
        context_only=("QQQ", "SPY"),
        excluded=("TSLA",),
    )
    market = MarketDataRepository(sqlite_engine)
    repository = WatermarkRepository(sqlite_engine, calendar=calendar)
    for minute in range(3):
        market.append(_bar(_series("NVDA"), _interval(minute)))
    for minute in range(2):
        market.append(_bar(_series("AMD"), _interval(minute)))
    for symbol, minutes in (("NVDA", 3), ("AMD", 2)):
        repository.recompute_symbol(
            experiment_hash=_EXPERIMENT_HASH,
            series=_series(symbol),
            start_at=_START,
            end_at=_START + timedelta(minutes=minutes),
            updated_at=_END + timedelta(minutes=minute_index(symbol)),
        )

    readiness = repository.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=10),
        required_through=_START + timedelta(minutes=2),
    )
    replay = WatermarkRepository(sqlite_engine, calendar=calendar).recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=11),
        required_through=_START + timedelta(minutes=2),
    )

    assert readiness.is_ready is True
    assert readiness.watermark is not None
    assert readiness.watermark.status is BasketStatus.READY
    assert readiness.watermark.contiguous_through == _START + timedelta(minutes=2)
    assert readiness.missing_active_symbols == ()
    assert readiness.blocked_active_symbols == ()
    assert replay.watermark == readiness.watermark
    with sqlite_engine.connect() as connection:
        row = connection.execute(select(aqa_basket_watermarks)).mappings().one()
    assert row["version"] == 1
    assert row["status"] == "ready"
    audit = AuditRepository(sqlite_engine, writer=AuditWriter.COLLECTOR)
    history = audit.list_events(
        stream_id=f"aqa_collector:data:{readiness.watermark.basket_watermark_id}"
    )
    assert len(history) == 1
    assert history[0].event_type == "data.basket_watermark_updated"


def test_missing_active_symbol_blocks_while_missing_context_does_not_participate(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    universe = UniverseSpec(
        active_tradable=("AMD", "NVDA"),
        benchmark_only=("SOXX",),
        context_only=("SPY",),
        excluded=(),
    )
    market = MarketDataRepository(sqlite_engine)
    repository = WatermarkRepository(sqlite_engine, calendar=calendar)
    market.append(_bar(_series("NVDA"), _interval(0)))
    repository.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=_series("NVDA"),
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
        updated_at=_END + timedelta(minutes=1),
    )

    result = repository.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=2),
    )

    assert result.is_ready is False
    assert result.watermark is not None
    assert result.watermark.status is BasketStatus.BLOCKED
    assert result.watermark.contiguous_through is None
    assert result.missing_active_symbols == ("AMD",)
    assert "SPY" not in result.missing_active_symbols
    assert "SOXX" not in result.missing_active_symbols
    restarted = WatermarkRepository(sqlite_engine, calendar=calendar)
    persisted = restarted.active_basket_watermark(
        experiment_hash=_EXPERIMENT_HASH,
        timeframe="1Min",
    )
    assert persisted == result.watermark
    assert (
        BasketReadiness(
            watermark=persisted,
            missing_active_symbols=(),
            blocked_active_symbols=(),
            required_through=None,
        ).is_ready
        is False
    )


def test_gap_on_non_last_active_symbol_is_attributed_to_that_symbol(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    universe = UniverseSpec(
        active_tradable=("AMD", "NVDA"),
        benchmark_only=(),
        context_only=(),
        excluded=(),
    )
    market = MarketDataRepository(sqlite_engine)
    watermarks = WatermarkRepository(sqlite_engine, calendar=calendar)
    for symbol in universe.active_tradable:
        series = _series(symbol)
        market.append(_bar(series, _interval(0)))
        watermarks.recompute_symbol(
            experiment_hash=_EXPERIMENT_HASH,
            series=series,
            start_at=_START,
            end_at=_START + timedelta(minutes=1),
            updated_at=_END + timedelta(minutes=1),
        )
    GapRepository(sqlite_engine, calendar=calendar).record(
        _detect(
            calendar,
            _series("AMD"),
            observed=(),
            start=_START,
            end=_START + timedelta(minutes=1),
        )[0]
    )

    result = watermarks.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=2),
    )

    assert result.blocked_active_symbols == ("AMD",)
    assert result.missing_active_symbols == ()
    assert result.is_ready is False


def test_basket_timestamp_cannot_precede_component_state(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    universe = UniverseSpec(
        active_tradable=("NVDA",),
        benchmark_only=(),
        context_only=(),
        excluded=(),
    )
    series = _series()
    market = MarketDataRepository(sqlite_engine)
    watermarks = WatermarkRepository(sqlite_engine, calendar=calendar)
    market.append(_bar(series, _interval(0)))
    component_time = _END + timedelta(minutes=5)
    watermarks.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
        updated_at=component_time,
    )

    with pytest.raises(ReadinessValidationError, match="precedes its component"):
        watermarks.recompute_active_basket(
            experiment_hash=_EXPERIMENT_HASH,
            universe=universe,
            provider="fixture",
            feed="iex",
            adjustment="raw",
            timeframe="1Min",
            updated_at=component_time - timedelta(microseconds=1),
        )


def test_unresolved_active_gap_and_endpoint_correction_block_basket(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    universe = UniverseSpec(
        active_tradable=("NVDA",),
        benchmark_only=(),
        context_only=(),
        excluded=(),
    )
    series = _series()
    market = MarketDataRepository(sqlite_engine)
    watermarks = WatermarkRepository(sqlite_engine, calendar=calendar)
    gap_repository = GapRepository(sqlite_engine, calendar=calendar)
    market.append(_bar(series, _interval(0)))
    watermarks.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
        updated_at=_END + timedelta(minutes=1),
    )
    ready = watermarks.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=2),
    )
    assert ready.is_ready is True
    assert ready.watermark is not None
    assert ready.watermark.version == 1
    gap = gap_repository.record(
        _detect(
            calendar,
            series,
            observed=(),
            start=_START,
            end=_START + timedelta(minutes=1),
        )[0]
    )

    blocked = watermarks.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=3),
    )
    assert blocked.blocked_active_symbols == ("NVDA",)
    assert blocked.is_ready is False
    assert blocked.watermark is not None
    assert blocked.watermark.status is BasketStatus.BLOCKED
    assert blocked.watermark.contiguous_through is None
    assert blocked.watermark.version == 2

    claim_time = _END + timedelta(minutes=4)
    claimed = gap_repository.begin_repair(gap.gap_id, attempted_at=claim_time)
    assert claimed.last_attempt_at is not None
    gap_repository.complete_repair(
        gap.gap_id,
        coverage=GapRepairCoverage(
            series=series,
            start_at=gap.start_at,
            end_at=gap.end_at,
            observed_intervals=(_interval(0),),
            completed_at=claim_time + timedelta(seconds=1),
        ),
    )
    recovered = watermarks.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=5),
    )
    assert recovered.is_ready is True
    assert recovered.watermark is not None
    assert recovered.watermark.status is BasketStatus.READY
    assert recovered.watermark.version == 3

    # A current-endpoint correction invalidates the repaired component until its symbol
    # watermark is deterministically recomputed.
    market.append(_bar(series, _interval(0), close="10.75", receipt_offset=20))
    corrected = watermarks.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=6),
    )
    assert corrected.blocked_active_symbols == ("NVDA",)
    assert corrected.is_ready is False
    assert corrected.watermark is not None
    assert corrected.watermark.status is BasketStatus.BLOCKED
    assert corrected.watermark.version == 4

    restarted = WatermarkRepository(sqlite_engine, calendar=calendar)
    assert (
        restarted.active_basket_watermark(
            experiment_hash=_EXPERIMENT_HASH,
            timeframe="1Min",
        )
        == corrected.watermark
    )
    replay = restarted.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=7),
    )
    assert replay.watermark == corrected.watermark
    audit = AuditRepository(sqlite_engine, writer=AuditWriter.COLLECTOR).list_events(
        stream_id=f"aqa_collector:data:{corrected.watermark.basket_watermark_id}"
    )
    assert tuple(event.payload["status"] for event in audit) == (
        "ready",
        "blocked",
        "ready",
        "blocked",
    )


def test_earlier_invalid_correction_durably_floors_symbol_and_blocks_basket_after_restart(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    universe = UniverseSpec(
        active_tradable=("NVDA",),
        benchmark_only=(),
        context_only=(),
        excluded=(),
    )
    series = _series()
    market = MarketDataRepository(sqlite_engine)
    watermarks = WatermarkRepository(sqlite_engine, calendar=calendar)
    for minute in range(3):
        market.append(_bar(series, _interval(minute)))
    _readiness, ready = watermarks.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_END,
        updated_at=_END + timedelta(minutes=1),
    )
    assert ready is not None and ready.is_ready
    basket = watermarks.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=2),
    )
    assert basket.is_ready

    market.append(
        _bar(
            series,
            _interval(0),
            close="10.75",
            flags=("suspect",),
            receipt_offset=20,
        )
    )
    corrected_readiness, blocked_floor = watermarks.recompute_symbol(
        experiment_hash=_EXPERIMENT_HASH,
        series=series,
        start_at=_START,
        end_at=_END,
        updated_at=_END + timedelta(minutes=3),
    )

    assert corrected_readiness.contiguous_through is None
    assert blocked_floor is not None
    assert blocked_floor.is_ready is False
    assert blocked_floor.contiguous_through == _START
    assert blocked_floor.latest_bar_event_id is None
    assert blocked_floor.version == ready.version + 1
    persisted = MarketDataRepository(sqlite_engine).watermark(
        experiment_hash=_EXPERIMENT_HASH,
        identity=_bar(series, _interval(0)).identity,
    )
    assert persisted == blocked_floor

    restarted = WatermarkRepository(sqlite_engine, calendar=calendar)
    after_restart = restarted.recompute_active_basket(
        experiment_hash=_EXPERIMENT_HASH,
        universe=universe,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=_END + timedelta(minutes=4),
    )
    assert after_restart.is_ready is False
    assert after_restart.blocked_active_symbols == ("NVDA",)
    audit = AuditRepository(sqlite_engine, writer=AuditWriter.COLLECTOR).list_events(
        stream_id=f"aqa_collector:data:{blocked_floor.symbol_watermark_id}"
    )
    assert audit[-1].payload["status"] == "blocked"


def test_audit_failure_rolls_back_symbol_watermark_atomically(
    sqlite_engine: Engine,
    calendar: XnasExchangeCalendar,
) -> None:
    series = _series("RBLX")
    MarketDataRepository(sqlite_engine).append(_bar(series, _interval(0)))
    repository = WatermarkRepository(sqlite_engine, calendar=calendar)

    class InjectedAuditFailure(RuntimeError):
        pass

    def fail_audit_insert(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if statement.lstrip().upper().startswith("INSERT") and "aqa_audit_events" in statement:
            raise InjectedAuditFailure("injected audit failure")

    event.listen(sqlite_engine, "before_cursor_execute", fail_audit_insert)
    try:
        with pytest.raises(InjectedAuditFailure, match="injected"):
            repository.recompute_symbol(
                experiment_hash=_EXPERIMENT_HASH,
                series=series,
                start_at=_START,
                end_at=_START + timedelta(minutes=1),
                updated_at=_END + timedelta(minutes=1),
            )
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", fail_audit_insert)

    with sqlite_engine.connect() as connection:
        assert (
            connection.scalar(
                select(aqa_symbol_watermarks.c.symbol_watermark_id).where(
                    aqa_symbol_watermarks.c.symbol == series.symbol
                )
            )
            is None
        )


def minute_index(symbol: str) -> int:
    return {"AMD": 5, "NVDA": 4}[symbol]
