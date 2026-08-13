from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import sin
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from adaptive_trader.broker import FakePaperBroker
from adaptive_trader.clock import FakeClock
from adaptive_trader.config import (
    AppConfig,
    DataConfig,
    MarketDataConfig,
    MeanReversionConfig,
    MomentumConfig,
    RegimeConfig,
    RiskConfig,
)
from adaptive_trader.constants import (
    NEW_YORK,
    PAPER_FLATTEN_ACKNOWLEDGEMENT,
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    PAPER_RESUME_ACKNOWLEDGEMENT,
    UTC,
)
from adaptive_trader.decision_engine import ForwardDecisionEngine
from adaptive_trader.exceptions import (
    BrokerConnectionError,
    ReconciliationBlocked,
    SafetyViolation,
)
from adaptive_trader.live import LiveService
from adaptive_trader.live_models import (
    AccountState,
    AssetInfo,
    BrokerOrderState,
    LocalOrderState,
    MarketBar,
    MarketClockState,
    OrderIntent,
    PaperCredentials,
    RunMode,
    Side,
)
from adaptive_trader.market_data_live import (
    AlpacaMarketDataProvider,
    BarStore,
    ReplayMarketDataProvider,
)
from adaptive_trader.persistence import (
    AuditRepository,
    Database,
    decision_receipts,
    fill_events,
    risk_decisions,
    stream_events,
)

NOW = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)


def _config(
    *,
    run_name: str = "p1-test",
    enabled: bool = True,
    tickers: tuple[str, ...] = ("SPY", "QQQ"),
) -> SimpleNamespace:
    return SimpleNamespace(
        project=SimpleNamespace(
            run_name=run_name,
            output_directory="outputs/p1-test",
        ),
        data=SimpleNamespace(tickers=list(tickers), benchmark="SPY"),
        market_data=SimpleNamespace(stale_after_seconds=180),
        schedule=SimpleNamespace(
            evaluation_time_et="10:05",
            catch_up_cutoff_et="14:30",
            heartbeat_interval_seconds=10,
            risk_monitor_interval_seconds=10,
            reconciliation_interval_seconds=10,
            open_order_monitor_interval_seconds=10,
            order_fill_timeout_seconds=30,
            one_shot_readiness_timeout_seconds=0,
        ),
        execution=SimpleNamespace(
            paper_only=True,
            paper_order_submission_enabled=enabled,
            minimum_order_notional=25,
            max_single_order_fraction_of_equity=1.0,
            required_cash_buffer=0.0,
            maximum_orders_per_rebalance=10,
        ),
        risk=SimpleNamespace(
            daily_loss_limit=0.03,
            drawdown_soft_limit=0.10,
            drawdown_hard_limit=0.15,
            soft_limit_max_gross_exposure=0.50,
        ),
    )


def _bar(symbol: str, when: datetime = NOW, close: str = "100") -> MarketBar:
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        start=when,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=100,
        feed="REPLAY",
        received_at=when,
        source="p1-test",
    )


def _completed_daily_bars() -> tuple[MarketBar, ...]:
    sessions: list[date] = []
    cursor = NOW.date() - timedelta(days=220)
    while cursor < NOW.date():
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor += timedelta(days=1)
    bars: list[MarketBar] = []
    for index, session_date in enumerate(sessions):
        for symbol, phase in (("SPY", 0.0), ("QQQ", 0.7)):
            trend = 100.0 + index * (0.11 if symbol == "SPY" else 0.14)
            close = trend * (1.0 + 0.012 * sin(index / 4.0 + phase))
            started = datetime.combine(
                session_date,
                time(16),
                tzinfo=NEW_YORK,
            ).astimezone(UTC)
            bars.append(
                MarketBar(
                    symbol=symbol,
                    start=started,
                    end=started + timedelta(minutes=1),
                    open=Decimal(str(close * 0.998)),
                    high=Decimal(str(close * 1.002)),
                    low=Decimal(str(close * 0.996)),
                    close=Decimal(str(close)),
                    volume=1_000_000 + index,
                    feed="REPLAY",
                    received_at=NOW,
                    source="live-risk-context-test",
                )
            )
    return tuple(bars)


def _service(
    *,
    repository: AuditRepository | None = None,
    broker: FakePaperBroker | None = None,
    mode: RunMode = RunMode.PAPER_ONCE,
    config: SimpleNamespace | None = None,
    now: datetime = NOW,
    dry_run: bool = False,
    market_data: ReplayMarketDataProvider | None = None,
) -> tuple[LiveService, AuditRepository, FakePaperBroker]:
    selected_config = config or _config()
    selected_repository = repository or AuditRepository(Database(":memory:"))
    selected_broker = broker or FakePaperBroker(now=now)
    provider = market_data or ReplayMarketDataProvider(
        tuple(_bar(symbol, now) for symbol in selected_config.data.tickers)
    )
    # Stateful execution tests use only the explicitly isolated replay
    # authority.  A fake broker in PAPER_* mode must never become a production
    # service-level authority merely because a fixture omitted provider facts.
    isolated_replay = bool(
        mode in {RunMode.PAPER_ONCE, RunMode.PAPER_RUN}
        and isinstance(selected_broker, FakePaperBroker)
        and getattr(selected_config.market_data, "provider", None) is None
    )
    service = LiveService(
        selected_config,
        repository=selected_repository,
        broker=selected_broker,
        market_data=provider,
        mode=RunMode.REPLAY if isolated_replay else mode,
        clock=FakeClock(now),
        environment={PAPER_ORDER_ENABLEMENT_ENV: PAPER_ORDER_ACKNOWLEDGEMENT},
        dry_run=dry_run,
        allow_simulated_replay_orders=isolated_replay,
    )
    return service, selected_repository, selected_broker


def test_observer_risk_and_halt_never_mutate_broker() -> None:
    service, repository, broker = _service(mode=RunMode.OBSERVE)
    cancellations = 0

    def count_cancel() -> None:
        nonlocal cancellations
        cancellations += 1

    broker.cancel_all_orders = count_cancel  # type: ignore[method-assign]
    losing_account = AccountState(
        timestamp=NOW,
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("80000"),
        cash=Decimal("80000"),
        buying_power=Decimal("80000"),
        last_equity=Decimal("100000"),
    )

    service._apply_account_risk_latches(
        losing_account,
        date(2026, 1, 2),
        cancellation_authorized=True,
    )
    service.halt("observer safety test")

    assert cancellations == 0
    assert "manual" in repository.active_halts(NOW)


def test_service_level_dry_run_and_halt_mode_never_cancel() -> None:
    for mode, dry_run in (
        (RunMode.PAPER_ONCE, True),
        (RunMode.HALT, False),
    ):
        service, _, broker = _service(mode=mode, dry_run=dry_run)
        cancellations = 0

        def count_cancel() -> None:
            nonlocal cancellations
            cancellations += 1

        broker.cancel_all_orders = count_cancel  # type: ignore[method-assign]
        service.halt("non-mutating halt context")
        assert cancellations == 0


def _losing_account() -> AccountState:
    return AccountState(
        timestamp=NOW,
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("80000"),
        cash=Decimal("80000"),
        buying_power=Decimal("80000"),
        last_equity=Decimal("100000"),
    )


def _accepted_order(client_order_id: str, side: Side) -> BrokerOrderState:
    return BrokerOrderState(
        client_order_id=client_order_id,
        broker_order_id=f"broker-{client_order_id}",
        symbol="SPY" if side is Side.SELL else "QQQ",
        side=side,
        status="accepted",
        submitted_at=NOW,
        updated_at=NOW,
        requested_notional=Decimal("100"),
    )


def test_daily_loss_cancel_all_ignores_stale_market_data() -> None:
    service, repository, broker = _service(mode=RunMode.PAPER_RUN)
    cancellations = 0

    def count_cancel() -> None:
        nonlocal cancellations
        cancellations += 1

    broker.cancel_all_orders = count_cancel  # type: ignore[method-assign]
    broker.get_account = _losing_account  # type: ignore[method-assign]
    assert service.bar_store.freshness(NOW).fresh is False

    service._monitor_risk_snapshot(NOW)

    assert cancellations == 1
    assert "daily_loss" in repository.active_halts(NOW)
    assert repository.latest_reconciliation()["clean"] is True


def test_daily_loss_cancel_all_ignores_blocking_reconciliation() -> None:
    service, repository, broker = _service(mode=RunMode.PAPER_RUN)
    broker.get_account = _losing_account  # type: ignore[method-assign]
    broker.inject_order(_accepted_order("apa-risk-buy", Side.BUY))
    broker.inject_order(_accepted_order("apa-risk-sell", Side.SELL))

    service._monitor_risk_snapshot(NOW)

    assert {order.status for order in broker.get_orders()} == {"canceled"}
    assert repository.latest_reconciliation()["blocking"] is True
    assert "daily_loss" in repository.active_halts(NOW)


@pytest.mark.parametrize(
    ("mode", "dry_run"),
    [
        (RunMode.OBSERVE, False),
        (RunMode.PAPER_ONCE, True),
        (RunMode.HALT, False),
    ],
)
def test_risk_monitor_never_cancels_without_paper_mutation_authority(
    mode: RunMode,
    dry_run: bool,
) -> None:
    service, _, broker = _service(mode=mode, dry_run=dry_run)
    cancellations = 0

    def count_cancel() -> None:
        nonlocal cancellations
        cancellations += 1

    broker.cancel_all_orders = count_cancel  # type: ignore[method-assign]
    broker.get_account = _losing_account  # type: ignore[method-assign]

    service._monitor_risk_snapshot(NOW)

    assert cancellations == 0


def test_operator_latch_survives_reopen_and_blocks_submission(tmp_path) -> None:
    path = tmp_path / "operator-latch.sqlite3"
    first_db = Database(path)
    first = AuditRepository(first_db)
    first.record_halt(
        run_id=None,
        action="halt",
        latch_type="operator",
        initiator="cli",
        reason="operator requested",
        created_at=NOW,
    )
    first_db.close()

    second_db = Database(path)
    repository = AuditRepository(second_db)
    service, _, broker = _service(
        repository=repository,
        config=_config(run_name="operator-restart"),
    )
    try:
        result = service.run_once({"SPY": 0.5})
    finally:
        service.shutdown()
        second_db.close()

    assert result.status == "observed"
    assert any("halt latch" in reason for reason in result.gate.reasons)
    assert broker.submit_calls == 0


def test_observer_records_hypothetical_intents_without_broker_orders() -> None:
    service, repository, broker = _service(mode=RunMode.OBSERVE)
    try:
        result = service.run_once({"SPY": 0.5})
    finally:
        service.shutdown()

    assert result.status == "observed"
    assert result.hypothetical_client_order_ids
    assert repository.count("order_intents") == 1
    assert repository.count("broker_orders") == 0
    assert broker.submit_calls == 0


def test_direct_production_run_once_requires_real_paper_authority_and_preflights() -> None:
    config = _config(run_name="direct-production-authority", tickers=("SPY",))
    config.market_data.provider = "alpaca"
    config.market_data.feed = "IEX"
    service, repository, broker = _service(config=config, mode=RunMode.PAPER_ONCE)

    with pytest.raises(SafetyViolation, match="concrete Alpaca paper broker"):
        service.run_once({"SPY": 0.5})

    cancel_calls = 0

    def count_cancel() -> None:
        nonlocal cancel_calls
        cancel_calls += 1

    broker.cancel_all_orders = count_cancel  # type: ignore[method-assign]
    with pytest.raises(SafetyViolation, match="complete paper/replay authority"):
        service._request_operator_halt_cancellation(
            halt_event_id="direct-private-cancel-regression",
            flatten_requested=False,
        )
    with pytest.raises(SafetyViolation, match="cancellation authority is incomplete"):
        service.halt(
            "direct production cancellation authority regression",
            flatten=True,
            acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
        )

    assert broker.submit_calls == 0
    assert cancel_calls == 0
    assert repository.count("broker_orders") == 0
    service.shutdown()


def test_decision_receipt_hashes_broker_account_identity() -> None:
    sentinel = "FULL-PAPER-ACCOUNT-ID-MUST-NOT-PERSIST"
    service, repository, broker = _service(mode=RunMode.OBSERVE)
    original_account = broker.get_account()
    broker.get_account = lambda: AccountState(  # type: ignore[method-assign]
        timestamp=original_account.timestamp,
        account_id=sentinel,
        status=original_account.status,
        equity=original_account.equity,
        cash=original_account.cash,
        buying_power=original_account.buying_power,
        last_equity=original_account.last_equity,
        trading_blocked=original_account.trading_blocked,
    )

    result = service.run_once({"SPY": 0.5})
    receipt = repository.get_rebalance(result.decision_id)
    service.shutdown()

    assert receipt is not None
    serialized = str(receipt["payload"])
    assert sentinel not in serialized
    assert "account_id_hash" in serialized


def test_external_cash_flow_starts_new_unlinked_return_segment() -> None:
    service, repository, _ = _service(mode=RunMode.OBSERVE)
    prior = AccountState(
        timestamp=NOW - timedelta(minutes=1),
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
    )
    current = AccountState(
        timestamp=NOW,
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("110000"),
        cash=Decimal("110000"),
        buying_power=Decimal("110000"),
    )
    repository.record_account_state(prior, (), run_id=service.run_id)

    metrics = service._record_forward_performance(
        account=current,
        positions=(),
        session_date=date(2026, 1, 2),
        now=NOW,
    )

    assert metrics["continuity_flag"] == "external_cash_flow_discontinuity"
    assert metrics["external_cash_flow"] == Decimal("10000")
    assert metrics["daily_return"] is None
    assert metrics["cumulative_return"] == Decimal("0")
    assert metrics["return_unavailable_reason"] == "external_cash_flow_timing_unknown"


def test_performance_series_survives_restart_without_recounting_same_snapshot() -> None:
    repository = AuditRepository(Database(":memory:"))
    config = _config(run_name="performance-restart")
    first, _, _ = _service(repository=repository, config=config, mode=RunMode.OBSERVE)
    prior = AccountState(
        timestamp=NOW - timedelta(minutes=1),
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
    )
    current = AccountState(
        timestamp=NOW,
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("110000"),
        cash=Decimal("110000"),
        buying_power=Decimal("110000"),
    )
    repository.record_account_state(prior, (), run_id=first.run_id)
    first_metrics = first._record_forward_performance(
        account=current,
        positions=(),
        session_date=date(2026, 1, 2),
        now=NOW,
    )
    repository.record_account_state(current, (), run_id=first.run_id)

    restarted, _, _ = _service(
        repository=repository,
        config=config,
        mode=RunMode.OBSERVE,
    )
    restarted_metrics = restarted._record_forward_performance(
        account=current,
        positions=(),
        session_date=date(2026, 1, 2),
        now=NOW,
    )

    assert restarted_metrics["series_id"] == first_metrics["series_id"]
    assert restarted_metrics["segment_id"] == first_metrics["segment_id"]
    assert restarted_metrics["daily_return"] is None
    assert restarted_metrics["cumulative_return"] == "0"
    assert restarted_metrics["external_cash_flow"] == "10000"


class AmbiguousEveryOrderBroker(FakePaperBroker):
    def submit_order(self, intent):
        self.timeout_after_accept.add(intent.client_order_id)
        return super().submit_order(intent)


def test_ambiguous_buy_is_submission_unknown_and_immediately_reconciled() -> None:
    broker = AmbiguousEveryOrderBroker(now=NOW)
    service, repository, _ = _service(broker=broker)
    try:
        result = service.run_once({"SPY": 0.5})
    finally:
        service.shutdown()

    assert result.status == "submission_unknown"
    assert repository.count("reconciliation_runs") >= 2
    assert broker.submit_calls == 1
    outcomes = repository.get_rebalance(result.decision_id)["payload"]["order_outcomes"]
    assert outcomes[0]["broker_order_id"]
    assert outcomes[0]["local_state"] == "accepted"
    assert outcomes[0]["broker_status"] == "accepted"


def test_pending_sell_resumes_buys_once_without_duplicate_sell() -> None:
    broker = FakePaperBroker(now=NOW)
    broker.set_position("SPY", "500", "100")
    service, repository, _ = _service(broker=broker)
    service.start_streams()
    result = service.run_once({"SPY": 0.0, "QQQ": 0.5})
    assert result.status == "execution_pending"
    sell = next(order for order in broker.get_orders() if order.side is Side.SELL)
    broker.emit_trade_update(
        sell.client_order_id,
        "fill",
        fill_quantity=sell.requested_quantity,
        fill_price="100",
        event_id="sell-filled",
    )

    service._monitor_open_orders(NOW)
    service._monitor_open_orders(NOW)
    updated = repository.get_rebalance(result.decision_id)
    service.shutdown()

    assert updated is not None and updated["status"] == "submitted"
    assert broker.submit_calls == 2
    assert len([order for order in broker.get_orders() if order.side is Side.SELL]) == 1
    assert len([order for order in broker.get_orders() if order.side is Side.BUY]) == 1


def test_scheduler_records_missed_receipt_and_monitors_once() -> None:
    after_cutoff = datetime(2026, 1, 2, 21, 30, tzinfo=UTC)
    config = _config(run_name="missed-test")
    service, repository, _ = _service(
        config=config,
        mode=RunMode.OBSERVE,
        now=after_cutoff,
    )

    service.run(max_iterations=1)
    service.run(max_iterations=1)

    decisions = repository.list_rebalances()
    assert len(decisions) == 1
    assert decisions[0]["status"] == "missed_after_cutoff"
    assert repository.count("decision_receipts") == 1
    assert repository.count("heartbeats") == 1
    # One durable EOD-finalization marker plus one report marker, each once.
    assert repository.count("generated_reports") == 2
    assert repository.count("reconciliation_runs") >= 2
    payload = decisions[0]["payload"]
    assert payload["account"]["equity"] == "100000"
    assert payload["positions"] == []
    assert payload["current_cash_weight"] == "1"
    assert payload["final_target"] is None
    assert payload["target_cash_weight"] is None
    assert payload["turnover"] is None
    assert payload["estimated_volatility"] is None
    assert payload["planning"]["intents"] == []
    assert payload["planning"]["skipped"][0]["reason"] == "missed_after_cutoff"
    assert payload["risk_decision"]["not_evaluated_reason"] == payload["skip_reason"]
    assert payload["non_evaluated_fields"]["order_intents"] == payload["skip_reason"]
    assert payload["operational_risk_state"]["evaluation_status"] == "missed_after_cutoff"
    assert payload["operational_risk_state"]["open_orders"] == []


def test_scheduler_persists_configured_evaluation_as_scheduled_time() -> None:
    catch_up = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
    service, repository, _ = _service(
        config=_config(run_name="scheduled-time-test"),
        mode=RunMode.OBSERVE,
        now=catch_up,
    )

    service.run(max_iterations=1)

    decision = repository.list_rebalances()[0]
    expected = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)
    assert decision["scheduled_at"] == expected
    assert decision["payload"]["scheduled_at"] == expected.isoformat()
    assert decision["payload"]["actual_at"] == catch_up.isoformat()


def test_scheduler_recovers_from_transient_calendar_failure_and_caches_session() -> None:
    before_evaluation = NOW - timedelta(hours=2)
    config = _config(run_name="calendar-recovery")
    config.schedule.calendar_retry_interval_seconds = 0
    service, repository, broker = _service(
        config=config,
        mode=RunMode.OBSERVE,
        now=before_evaluation,
    )
    service._last_risk_snapshot_at = before_evaluation
    original_calendar = broker.get_calendar
    calendar_calls = 0

    def transient_calendar(start, end):
        nonlocal calendar_calls
        calendar_calls += 1
        if calendar_calls == 1:
            raise BrokerConnectionError("synthetic transient calendar outage")
        return original_calendar(start, end)

    broker.get_calendar = transient_calendar  # type: ignore[method-assign]

    service.run(max_iterations=3, poll_seconds=0)

    assert calendar_calls == 2
    assert repository.count("rebalance_decisions") == 0
    assert repository.count("system_incidents") == 1
    assert repository.active_incidents(run_id=service.run_id) == []
    assert repository.count("heartbeats") == 2
    heartbeat = repository.latest_heartbeat()
    assert heartbeat is not None
    assert heartbeat["components"]["active_incidents"] == []
    assert "unresolved system incident" not in heartbeat["components"]["health_reasons"]


def test_scheduler_recovers_from_transient_scheduled_cycle_failure() -> None:
    config = _config(run_name="scheduled-cycle-recovery")
    service, repository, _ = _service(
        config=config,
        mode=RunMode.OBSERVE,
        now=NOW,
    )
    service._last_risk_snapshot_at = NOW
    original_run_once = service.run_once
    cycle_calls = 0

    def transient_cycle(*args, **kwargs):
        nonlocal cycle_calls
        cycle_calls += 1
        if cycle_calls == 1:
            assert repository.count("rebalance_decisions") == 0
            raise BrokerConnectionError("synthetic scheduled-cycle outage")
        return original_run_once(*args, **kwargs)

    service.run_once = transient_cycle  # type: ignore[method-assign]

    service.run(max_iterations=2, poll_seconds=0)

    assert cycle_calls == 2
    assert repository.count("rebalance_decisions") == 1
    assert repository.count("decision_receipts") == 1
    assert repository.count("system_incidents") == 1
    assert repository.active_incidents(run_id=service.run_id) == []
    assert repository.count("heartbeats") == 2
    heartbeat = repository.latest_heartbeat()
    assert heartbeat is not None
    assert heartbeat["components"]["active_incidents"] == []
    assert "unresolved system incident" not in heartbeat["components"]["health_reasons"]


def test_missed_receipt_survives_market_clock_failure_after_calendar() -> None:
    after_cutoff = datetime(2026, 1, 2, 21, 30, tzinfo=UTC)
    service, repository, broker = _service(
        config=_config(run_name="missed-clock-fallback"),
        mode=RunMode.OBSERVE,
        now=after_cutoff,
    )
    service._last_risk_snapshot_at = after_cutoff
    original_clock = broker.get_clock
    broker.get_account = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BrokerConnectionError("synthetic account snapshot outage")
    )
    broker.get_positions = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BrokerConnectionError("synthetic position snapshot outage")
    )
    clock_calls = 0

    def fail_once_after_calendar():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            raise BrokerConnectionError("synthetic post-calendar clock outage")
        return original_clock()

    broker.get_clock = fail_once_after_calendar  # type: ignore[method-assign]

    service.run(max_iterations=1)

    assert repository.count("rebalance_decisions") == 1
    assert repository.count("decision_receipts") == 1
    decision = repository.list_rebalances()[0]
    assert decision["status"] == "missed_after_cutoff"
    assert decision["payload"]["market"]["source"] == "cached_market_calendar"
    snapshot_errors = decision["payload"]["operational_risk_state"]["snapshot_errors"]
    assert set(snapshot_errors) >= {"account", "positions", "market_clock"}
    assert decision["payload"]["account"] is None
    assert decision["payload"]["planning"]["intents"] == []


def test_heartbeat_market_clock_outage_is_persisted_unhealthy_without_stopping() -> None:
    before_evaluation = NOW - timedelta(hours=2)
    service, repository, broker = _service(
        config=_config(run_name="heartbeat-clock-outage"),
        mode=RunMode.OBSERVE,
        now=before_evaluation,
    )
    service._last_risk_snapshot_at = before_evaluation

    def unavailable_clock():
        raise BrokerConnectionError("synthetic heartbeat clock outage")

    broker.get_clock = unavailable_clock  # type: ignore[method-assign]

    service.run(max_iterations=1)

    heartbeat = repository.latest_heartbeat()
    assert heartbeat is not None
    assert heartbeat["healthy"] is False
    assert heartbeat["components"]["market_clock_error"] == ("synthetic heartbeat clock outage")
    assert "paper market clock is unavailable" in heartbeat["components"]["health_reasons"]


def test_entitlement_requires_data_and_recovery_requires_coverage(monkeypatch) -> None:
    provider = object.__new__(AlpacaMarketDataProvider)
    provider._feed = "IEX"
    monkeypatch.setattr(provider, "get_bars", lambda *args, **kwargs: ())

    with pytest.raises(BrokerConnectionError):
        provider.check_feed_entitlement(now=NOW)

    repository = AuditRepository(Database(":memory:"))
    store = BarStore(repository, universe=("SPY",), clock=FakeClock(NOW))
    store.mark_health(True, "connected")
    store.ingest(_bar("SPY", NOW))
    assert store.freshness().unresolved_gap is False
    store.mark_health(True, "reconnect_backfill_incomplete")
    assert store.freshness().unresolved_gap is True
    assert store.freshness().stream_healthy is False
    store.mark_health(True, "reconnect_backfill_complete")
    assert store.freshness().unresolved_gap is False
    assert store.freshness().stream_healthy is True


class FailingEntitlementProvider(ReplayMarketDataProvider):
    def __init__(self) -> None:
        super().__init__((_bar("SPY"), _bar("QQQ")), feed="SIP")
        self.probe_calls = 0

    def check_feed_entitlement(self, symbol: str, *, now: datetime) -> bool:
        del symbol, now
        self.probe_calls += 1
        raise BrokerConnectionError("synthetic SIP entitlement rejection")


def test_live_startup_fails_closed_and_audits_feed_entitlement() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    provider = FailingEntitlementProvider()
    service = LiveService(
        _config(run_name="entitlement-failure"),
        repository=repository,
        broker=broker,
        market_data=provider,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
    )

    with pytest.raises(SafetyViolation, match="SIP feed entitlement"):
        service.start_streams()

    assert provider.probe_calls == 1
    assert provider.connected is False
    assert repository.count("system_incidents") == 1
    assert repository.count("rebalance_decisions") == 0


def test_live_startup_validates_complete_asset_universe_before_streams() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    broker.add_asset(
        AssetInfo(
            symbol="SPY",
            asset_class="us_equity",
            exchange="NYSE",
            active=False,
            tradable=True,
            fractionable=True,
        )
    )
    provider = ReplayMarketDataProvider((_bar("SPY"), _bar("QQQ")))
    service = LiveService(
        _config(run_name="invalid-asset-startup"),
        repository=repository,
        broker=broker,
        market_data=provider,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
    )

    with pytest.raises(SafetyViolation, match="asset validation"):
        service.start_streams()

    assert provider.connected is False
    assert broker.trade_updates_healthy is False
    assert service.status()["assets_verified"] is False
    assert repository.count("system_incidents") == 1
    assert repository.count("rebalance_decisions") == 0


def test_restart_rehydrates_unresolved_incident_into_heartbeat_health() -> None:
    repository = AuditRepository(Database(":memory:"))
    config = _config(run_name="durable-incident")
    first, _, _ = _service(repository=repository, config=config, mode=RunMode.OBSERVE)
    incident_id = first._record_incident(
        "durable_unknown_incident",
        "survives restart",
    )
    first.shutdown()

    restarted, _, _ = _service(
        repository=repository,
        config=config,
        mode=RunMode.OBSERVE,
    )
    restarted.run(max_iterations=1)

    assert incident_id in restarted._incident_ids
    assert repository.latest_heartbeat()["healthy"] is False
    restarted.shutdown()


def test_heartbeat_and_status_fail_on_open_market_stream_outage() -> None:
    provider = ReplayMarketDataProvider((_bar("SPY"), _bar("QQQ")))
    service, repository, broker = _service(
        mode=RunMode.OBSERVE,
        market_data=provider,
    )
    service.start_streams()
    provider.set_connected(False, "synthetic_disconnect")
    broker.set_trade_updates_healthy(False)

    service.run(max_iterations=1)

    heartbeat = repository.latest_heartbeat()
    assert heartbeat is not None and heartbeat["healthy"] is False
    assert (
        "market-data stream is disconnected during market hours"
        in heartbeat["components"]["health_reasons"]
    )
    assert (
        "trade-update stream is unhealthy while required"
        in heartbeat["components"]["health_reasons"]
    )
    status = service.status()
    assert status["healthy"] is False
    assert status["stream_connected"] is False
    assert status["trade_updates_healthy"] is False


def test_closed_market_ignores_expected_staleness_but_not_unresolved_gap() -> None:
    provider = ReplayMarketDataProvider((_bar("SPY"), _bar("QQQ")))
    service, _, broker = _service(
        mode=RunMode.OBSERVE,
        market_data=provider,
    )
    service.start_streams()
    broker.set_clock(
        MarketClockState(
            timestamp=NOW,
            is_open=False,
            next_open=NOW + timedelta(days=1),
            next_close=NOW + timedelta(days=1, hours=6),
        )
    )
    provider.set_connected(False, "market_closed")
    broker.set_trade_updates_healthy(False)

    assert service.status()["healthy"] is True

    service.bar_store.ingest(_bar("SPY", NOW))
    service.bar_store.ingest(_bar("SPY", NOW + timedelta(minutes=3)))
    status = service.status()
    assert status["healthy"] is False
    assert "market-data gap is unresolved" in status["health_reasons"]


def test_market_stream_is_not_healthy_before_authenticated_message(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    class NoMessageStream:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def subscribe_bars(self, handler, *symbols) -> None:
            del handler, symbols

        def subscribe_updated_bars(self, handler, *symbols) -> None:
            del handler, symbols

        def run(self) -> None:
            started.set()
            release.wait(1)

        def stop(self) -> None:
            release.set()

    monkeypatch.setattr("adaptive_trader.market_data_live.StockDataStream", NoMessageStream)
    provider = AlpacaMarketDataProvider(
        PaperCredentials("paper-key", "paper-secret"),
        max_reconnect_attempts=1,
    )
    health: list[tuple[bool, str]] = []
    provider.start_stream(("SPY",), lambda bar: bar, lambda ok, reason: health.append((ok, reason)))
    assert started.wait(1)

    assert provider.connected is False
    assert not any(ok for ok, _ in health)
    provider.stop_stream()


def test_unhealthy_trade_stream_prevents_claim_and_submission() -> None:
    service, repository, broker = _service()
    service.start_streams()
    broker.set_trade_updates_healthy(False)

    result = service.run_once({"SPY": 0.5})

    assert result.status == "waiting_for_fresh_data"
    assert result.claimed is False
    assert "trade-update stream" in str(result.skip_reason)
    assert repository.count("rebalance_decisions") == 0
    assert broker.submit_calls == 0


def test_trade_stream_recovery_resolves_incident_and_restores_health_projection() -> None:
    service, repository, _ = _service()
    incident_id = service._record_incident(
        "trade_update_stream_unhealthy_with_open_orders",
        "synthetic transient outage",
        severity="warning",
    )
    service._trade_stream_incident_active = True
    service.start_streams()

    service._monitor_open_orders(NOW)

    assert repository.active_incidents(run_id=service.run_id) == []
    assert incident_id not in service._incident_ids
    assert service._heartbeat_components(NOW)["trade_updates_healthy"] is True


def test_post_decision_hard_stop_retries_canceled_residual_after_cutoff() -> None:
    after_cutoff = datetime(2026, 1, 2, 20, 0, tzinfo=UTC)
    broker = FakePaperBroker(now=after_cutoff, initial_cash="50000")
    broker.set_position("SPY", "300", "100")
    service, repository, _ = _service(broker=broker, now=after_cutoff)
    prior_high = AccountState(
        timestamp=after_cutoff - timedelta(days=1),
        account_id="paper",
        status="ACTIVE",
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
    )
    repository.record_account_state(prior_high, (), run_id=service.run_id)
    service.ensure_ready(require_trade_updates=True)

    service._monitor_risk_snapshot(after_cutoff)
    service._monitor_risk_snapshot(after_cutoff)

    reductions = [order for order in broker.get_orders() if order.side is Side.SELL]
    decisions = repository.list_rebalances()
    assert broker.submit_calls == 2
    assert len(reductions) == 2
    assert {order.status for order in reductions} == {"accepted", "canceled"}
    assert [row["status"] for row in decisions] == [
        "hard_stop_submitted",
        "hard_stop_submitted",
    ]
    assert repository.count("decision_receipts") == 2


def test_pending_buy_waits_when_current_cash_no_longer_covers_plan() -> None:
    broker = FakePaperBroker(now=NOW)
    broker.set_position("SPY", "500", "100")
    service, repository, _ = _service(broker=broker)
    service.start_streams()
    result = service.run_once({"SPY": 0.0, "QQQ": 0.5})
    sell = next(order for order in broker.get_orders() if order.side is Side.SELL)
    broker.emit_trade_update(
        sell.client_order_id,
        "fill",
        fill_quantity=sell.requested_quantity,
        fill_price="100",
        event_id="cash-recheck-sell-filled",
    )
    broker.set_cash("10")

    service._monitor_open_orders(NOW)
    service._monitor_open_orders(NOW)

    assert repository.get_rebalance(result.decision_id)["status"] == "execution_pending"
    assert broker.submit_calls == 1
    assert repository.count("system_incidents") == 1

    broker.set_cash("100000")
    service._monitor_open_orders(NOW)
    service._monitor_open_orders(NOW)
    assert repository.get_rebalance(result.decision_id)["status"] == "submitted"
    assert broker.submit_calls == 2


def test_pending_buy_is_terminally_abandoned_after_original_session() -> None:
    broker = FakePaperBroker(now=NOW)
    broker.set_position("SPY", "500", "100")
    service, repository, _ = _service(broker=broker)
    service.start_streams()
    result = service.run_once({"SPY": 0.0, "QQQ": 0.5})
    sell = next(order for order in broker.get_orders() if order.side is Side.SELL)
    broker.emit_trade_update(
        sell.client_order_id,
        "fill",
        fill_quantity=sell.requested_quantity,
        fill_price="100",
        event_id="stale-session-sell-filled",
    )

    service._monitor_open_orders(NOW + timedelta(days=1))

    decision = repository.get_rebalance(result.decision_id)
    assert decision["status"] == "buy_leg_abandoned"
    assert "original market session" in decision["skip_reason"]
    assert broker.submit_calls == 1
    assert not any(order.side is Side.BUY for order in broker.get_orders())


def test_reconciliation_recovers_missed_fill_without_false_external_cash_flow() -> None:
    broker = FakePaperBroker(now=NOW)
    broker.set_position("SPY", "500", "100")
    service, repository, _ = _service(broker=broker)
    service.start_streams()
    result = service.run_once({"SPY": 0.0})
    sell = next(order for order in broker.get_orders() if order.side is Side.SELL)
    broker.stop_trade_updates()
    later = NOW + timedelta(minutes=1)
    broker.set_clock(
        MarketClockState(
            timestamp=later,
            is_open=True,
            next_open=later + timedelta(days=1),
            next_close=later + timedelta(hours=5),
        )
    )
    delayed_update = broker.emit_trade_update(
        sell.client_order_id,
        "fill",
        fill_quantity=sell.requested_quantity,
        fill_price="100",
        event_id="missed-websocket-fill",
    )

    service.reconcile()
    service.reconcile()
    service.process_trade_update(delayed_update)
    metrics = service._record_forward_performance(
        account=broker.get_account(),
        positions=broker.get_positions(),
        session_date=result.session_date,
        now=later,
    )

    assert repository.count("fill_events") == 1
    assert repository.fill_cash_effect(after=NOW, through=later) == Decimal("50000")
    assert metrics["external_cash_flow"] == Decimal("0")
    assert metrics["continuity_flag"] == "continuous"


def test_resume_preserves_daily_loss_latch() -> None:
    service, repository, _ = _service(mode=RunMode.OBSERVE)
    service.ensure_ready()
    for latch_type in ("operator", "manual", "hard_stop", "daily_loss"):
        repository.record_halt(
            run_id=service.run_id,
            action="halt",
            latch_type=latch_type,
            initiator="test",
            reason="resume filter test",
            session_date=date(2026, 1, 2),
            created_at=NOW,
        )

    service.resume(acknowledgement=PAPER_RESUME_ACKNOWLEDGEMENT)

    assert set(repository.active_halts(NOW)) == {"daily_loss"}


def _record_prior_daily_loss_latch(repository: AuditRepository, run_id: str) -> None:
    repository.record_halt(
        run_id=run_id,
        action="daily_loss",
        latch_type="daily_loss",
        initiator="risk_monitor",
        reason="prior session loss",
        session_date=date(2026, 1, 1),
        created_at=NOW - timedelta(days=1),
    )


def test_daily_loss_expires_only_after_persisted_clean_current_reconciliation() -> None:
    service, repository, _ = _service(mode=RunMode.OBSERVE)
    _record_prior_daily_loss_latch(repository, service.run_id)

    service._monitor_risk_snapshot(NOW)

    evidence = repository.latest_reconciliation()
    assert evidence is not None and evidence["clean"] is True
    assert "daily_loss" not in repository.active_halts(NOW)
    assert repository.count("halt_events") == 2


def test_blocking_daemon_reconciliation_retains_prior_daily_loss_latch() -> None:
    service, repository, broker = _service(mode=RunMode.OBSERVE)
    _record_prior_daily_loss_latch(repository, service.run_id)
    repository.record_account_state(
        AccountState(
            timestamp=NOW - timedelta(minutes=1),
            account_id="paper",
            status="ACTIVE",
            equity=Decimal("100000"),
            cash=Decimal("99600"),
            buying_power=Decimal("99600"),
            last_equity=Decimal("100000"),
        ),
        (),
        run_id=service.run_id,
    )
    broker.set_position("SPY", "4", "100")

    service._monitor_risk_snapshot(NOW)

    evidence = repository.latest_reconciliation()
    assert evidence is not None and evidence["blocking"] is True
    assert "daily_loss" in repository.active_halts(NOW)
    assert repository.count("halt_events") == 1


def test_stale_one_shot_retains_prior_daily_loss_without_reconciliation() -> None:
    provider = ReplayMarketDataProvider(())
    service, repository, _ = _service(
        mode=RunMode.PAPER_ONCE,
        market_data=provider,
    )
    _record_prior_daily_loss_latch(repository, service.run_id)

    result = service.run_once({"SPY": 0.0})

    assert result.status == "waiting_for_fresh_data"
    assert repository.count("reconciliation_runs") == 0
    assert "daily_loss" in repository.active_halts(NOW)


def test_shutdown_stops_both_streams_before_final_account_and_reconciliation() -> None:
    events: list[str] = []

    class TrackingProvider(ReplayMarketDataProvider):
        def stop_stream(self) -> None:
            events.append("market_stream_stopped")
            super().stop_stream()

    class TrackingBroker(FakePaperBroker):
        def stop_trade_updates(self) -> None:
            events.append("trade_stream_stopped")
            super().stop_trade_updates()

        def get_account(self) -> AccountState:
            events.append("account_snapshot")
            return super().get_account()

    broker = TrackingBroker(now=NOW)
    provider = TrackingProvider((_bar("SPY"), _bar("QQQ")))
    service, repository, _ = _service(
        broker=broker,
        market_data=provider,
        mode=RunMode.OBSERVE,
    )
    service.start_streams()
    events.clear()

    service.shutdown("ordering_test")
    service.shutdown("duplicate_shutdown_is_noop")

    assert events[:2] == ["market_stream_stopped", "trade_stream_stopped"]
    assert events.index("account_snapshot") > events.index("trade_stream_stopped")
    assert repository.count("reconciliation_runs") == 1


class FlakyHistoricalProvider(ReplayMarketDataProvider):
    def __init__(self, bars: tuple[MarketBar, ...]) -> None:
        super().__init__(bars)
        self.fail_next = False

    def get_bars(self, *args, **kwargs):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic transient history failure")
        return super().get_bars(*args, **kwargs)


class CapturingRiskEngine:
    def __init__(self) -> None:
        self.context: dict[str, object] = {}

    def evaluate(self, **kwargs):
        self.context = dict(kwargs)
        return kwargs


class RiskContextTargetProvider:
    def __init__(self) -> None:
        self._risk = CapturingRiskEngine()
        self.last_metadata: dict[str, object] | None = None

    def __call__(self, now, account, positions):
        del now, account, positions
        self._risk.evaluate(
            current_drawdown=0.0,
            current_daily_loss=0.0,
            hard_stop_latched=False,
            daily_loss_latched=False,
            halt_latched=False,
        )
        target = {"SPY": 0.5}
        self.last_metadata = {
            "status": "approved",
            "risk_actions": ({"control": "soft_drawdown"},),
            "risk_decision": {
                "proposed_weights": {"SPY": 1.0},
                "final_weights": target,
                "final_turnover": 0.5,
                "final_estimated_volatility": 0.1,
            },
            "final_target": target,
        }
        return target


class OperationalContextTargetProvider:
    def __init__(self) -> None:
        self.context: dict[str, object] = {}
        self.last_metadata: dict[str, object] | None = None

    def set_live_risk_context(self, **context) -> None:
        self.context = dict(context)

    def __call__(self, now, account, positions):
        del now, account, positions
        self.last_metadata = {
            "status": "approved",
            "risk_decision": {
                "proposed_weights": {"SPY": 0.1},
                "final_weights": {"SPY": 0.1},
                "final_turnover": 0.1,
                "final_estimated_volatility": 0.1,
                "evaluation_context": dict(self.context),
            },
            "final_target": {"SPY": 0.1},
        }
        return {"SPY": 0.1}


def test_target_provider_receives_refreshed_operational_risk_facts() -> None:
    service, repository, broker = _service(mode=RunMode.PAPER_ONCE)
    service.start_streams()
    setup_intent = OrderIntent(
        decision_id="context-setup",
        client_order_id="apa-context-open",
        session_date=NOW.date(),
        symbol="SPY",
        side=Side.BUY,
        sequence=0,
        reference_price=Decimal("100"),
        created_at=NOW,
        notional=Decimal("100"),
        reason="test_context",
    )
    assert service.order_manager.submit_intent(setup_intent) is not None
    target_provider = OperationalContextTargetProvider()
    service.target_provider = target_provider

    result = service.run_once()

    assert target_provider.context["open_order_symbols"] == ("SPY",)
    assert target_provider.context["current_price_timestamps"] == {
        "QQQ": NOW.isoformat(),
        "SPY": NOW.isoformat(),
    }
    assert target_provider.context["asset_eligibility"] == {
        "QQQ": True,
        "SPY": True,
    }
    assert target_provider.context["data_freshness_state"] == "fresh"
    assert target_provider.context["market_state"] == "open"
    assert target_provider.context["halt_state"] == "clear"
    assert target_provider.context["account_timestamp"] == NOW.isoformat()
    assert target_provider.context["positions"] == ()
    assert target_provider.context["current_prices"] == {"QQQ": 100.0, "SPY": 100.0}
    assert target_provider.context["asset_metadata"]["SPY"] == {
        "valid": True,
        "reasons": (),
        "asset_class": "us_equity",
        "exchange": "NYSE",
        "active": True,
        "tradable": True,
        "fractionable": True,
    }
    assert target_provider.context["market_timestamp"] == NOW.isoformat()
    assert target_provider.context["next_market_open"] == (NOW + timedelta(days=1)).isoformat()
    assert target_provider.context["evaluation_cutoff"].endswith("19:30:00+00:00")
    receipt = repository.get_rebalance(result.decision_id)["payload"]
    evaluation = receipt["decision_metadata"]["risk_decision"]["evaluation_context"]
    assert evaluation["open_order_symbols"] == ["SPY"]
    assert evaluation["open_orders"][0]["client_order_id"] == "apa-context-open"
    assert evaluation["current_price_timestamps"]["SPY"] == NOW.isoformat()
    assert evaluation["asset_eligibility"] == {"QQQ": True, "SPY": True}
    assert evaluation["market_state"] == "open"
    assert broker.submit_calls == 1


def test_forward_engine_evaluation_context_contains_live_service_facts() -> None:
    config = AppConfig(
        data=DataConfig(tickers=["SPY", "QQQ"], benchmark="SPY"),
        market_data=MarketDataConfig(
            historical_calendar_days=600,
            minimum_completed_sessions=80,
            stale_after_seconds=180,
        ),
        momentum=MomentumConfig(
            lookback_days=20,
            volatility_lookback_days=10,
            top_n=1,
        ),
        mean_reversion=MeanReversionConfig(
            zscore_lookback_days=10,
            long_term_trend_days=30,
            volatility_lookback_days=10,
            top_n=1,
        ),
        regime=RegimeConfig(
            benchmark="SPY",
            fast_moving_average_days=10,
            slow_moving_average_days=30,
            volatility_lookback_days=10,
            volatility_threshold_lookback_days=40,
        ),
        risk=RiskConfig(covariance_lookback_days=20),
    )
    config.market_data.provider = "replay"
    provider = ReplayMarketDataProvider((*_completed_daily_bars(), _bar("SPY"), _bar("QQQ")))
    broker = FakePaperBroker(now=NOW, initial_cash="90000")
    broker.set_position("SPY", "100", "100")
    repository = AuditRepository(Database(":memory:"))
    engine = ForwardDecisionEngine(config, provider)
    service = LiveService(
        config,
        repository=repository,
        broker=broker,
        market_data=provider,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
        target_provider=engine,
    )
    setup_intent = OrderIntent(
        decision_id="engine-context-setup",
        client_order_id="apa-engine-context-open",
        session_date=NOW.date(),
        symbol="QQQ",
        side=Side.BUY,
        sequence=0,
        reference_price=Decimal("100"),
        created_at=NOW,
        notional=Decimal("100"),
        reason="test_context",
    )
    assert service.order_manager.submit_intent(setup_intent) is not None

    result = service.run_once()

    assert result.decision_metadata is not None
    evaluation = result.decision_metadata["risk_decision"]["evaluation_context"]
    assert evaluation["account_equity"] == 100_000.0
    assert evaluation["account_cash"] == 90_000.0
    assert evaluation["account_timestamp"] == NOW.isoformat()
    assert evaluation["position_weights"] == {"QQQ": 0.0, "SPY": 0.1}
    assert [dict(position) for position in evaluation["positions"]] == [
        {
            "symbol": "SPY",
            "quantity": 100.0,
            "market_value": 10_000.0,
            "current_price": 100.0,
            "timestamp": NOW.isoformat(),
        }
    ]
    assert evaluation["open_order_symbols"] == ("QQQ",)
    assert evaluation["open_orders"][0]["client_order_id"] == "apa-engine-context-open"
    assert evaluation["current_prices"] == {"QQQ": 100.0, "SPY": 100.0}
    assert evaluation["current_price_timestamps"] == {
        "QQQ": NOW.isoformat(),
        "SPY": NOW.isoformat(),
    }
    assert evaluation["asset_eligibility"] == {"QQQ": True, "SPY": True}
    assert evaluation["asset_metadata"]["SPY"]["fractionable"] is True
    assert evaluation["market_timestamp"] == NOW.isoformat()
    assert evaluation["next_market_open"] == (NOW + timedelta(days=1)).isoformat()
    assert evaluation["next_market_close"] == (NOW + timedelta(hours=6)).isoformat()
    assert evaluation["evaluation_cutoff"].endswith("19:30:00+00:00")
    assert evaluation["data_freshness_state"] == "fresh"
    assert evaluation["market_state"] == "open"
    assert evaluation["halt_state"] == "clear"
    receipt = repository.get_rebalance(result.decision_id)["payload"]
    persisted_evaluation = receipt["decision_metadata"]["risk_decision"]["evaluation_context"]
    assert persisted_evaluation["positions"] == [dict(evaluation["positions"][0])]
    assert persisted_evaluation["open_orders"][0]["client_order_id"] == ("apa-engine-context-open")
    assert persisted_evaluation["asset_metadata"]["SPY"]["valid"] is True


def test_forward_history_preflight_fails_before_stream_connections() -> None:
    config = AppConfig(
        data=DataConfig(tickers=["SPY", "QQQ"], benchmark="SPY"),
        market_data=MarketDataConfig(
            historical_calendar_days=600,
            minimum_completed_sessions=80,
        ),
        momentum=MomentumConfig(
            lookback_days=20,
            volatility_lookback_days=10,
            top_n=1,
        ),
        mean_reversion=MeanReversionConfig(
            zscore_lookback_days=10,
            long_term_trend_days=30,
            volatility_lookback_days=10,
            top_n=1,
        ),
        regime=RegimeConfig(
            benchmark="SPY",
            fast_moving_average_days=10,
            slow_moving_average_days=30,
            volatility_lookback_days=10,
            volatility_threshold_lookback_days=40,
        ),
        risk=RiskConfig(covariance_lookback_days=20),
    )
    config.market_data.provider = "replay"
    provider = ReplayMarketDataProvider((_bar("SPY"), _bar("QQQ")))
    broker = FakePaperBroker(now=NOW)
    repository = AuditRepository(Database(":memory:"))
    service = LiveService(
        config,
        repository=repository,
        broker=broker,
        market_data=provider,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
        target_provider=ForwardDecisionEngine(config, provider),
    )

    with pytest.raises(SafetyViolation, match="history"):
        service.start_streams()

    assert provider.connected is False
    assert broker.trade_updates_healthy is False
    assert service.status()["history_preflight_verified"] is False
    assert "completed strategy history is unverified" in service.status()["health_reasons"]
    assert repository.active_incidents(run_id=service.run_id)[0]["incident_type"] == (
        "strategy_history_preflight_failed"
    )


def test_target_provider_receives_durable_high_water_drawdown_context() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW, initial_cash="89000")
    service, _, _ = _service(
        repository=repository,
        broker=broker,
        mode=RunMode.OBSERVE,
    )
    repository.record_account_state(
        AccountState(
            timestamp=NOW - timedelta(days=1),
            account_id="paper",
            status="ACTIVE",
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("100000"),
            last_equity=Decimal("100000"),
        ),
        (),
        run_id=service.run_id,
    )
    target_provider = RiskContextTargetProvider()
    service.target_provider = target_provider

    result = service.run_once()

    assert target_provider._risk.context["current_drawdown"] == pytest.approx(-0.11)
    assert result.decision_context["current_drawdown"] == Decimal("0.11")
    assert result.decision_context["final_target"] == {"SPY": 0.5}
    receipt = repository.get_rebalance(result.decision_id)["payload"]
    assert receipt["decision_metadata"]["risk_decision"]["current_drawdown"] == pytest.approx(-0.11)
    operational = receipt["operational_risk_state"]
    assert operational["account"]["equity"] == "89000"
    assert operational["positions"] == []
    assert operational["open_orders"] == []
    assert operational["freshness"]["stream_healthy"] is True
    assert operational["market_clock"]["is_open"] is True
    assert operational["before_strategy_cutoff"] is True
    assert operational["asset_validation"]["SPY"]["valid"] is True
    assert operational["reconciliation"]["clean"] is False
    assert operational["reconciliation"]["blocking"] is True
    assert operational["reconciliation"]["discrepancies"]
    assert operational["halt_state"]["manual_or_operator_active"] is False
    assert operational["limits"]["hard_drawdown"] == 0.15
    with repository.database.engine.connect() as connection:
        fact = connection.execute(select(risk_decisions.c.payload)).scalar_one()
    assert fact["operational_risk_state"] == operational
    assert repository.count("risk_decisions") == 1
    assert repository.count("risk_actions") == 1
    service.shutdown()


def test_hard_stop_retries_pre_submission_failure_and_canceled_residual() -> None:
    after_cutoff = datetime(2026, 1, 2, 20, 0, tzinfo=UTC)
    provider = FlakyHistoricalProvider((_bar("SPY", after_cutoff), _bar("QQQ", after_cutoff)))
    broker = FakePaperBroker(now=after_cutoff, initial_cash="50000")
    broker.set_position("SPY", "300", "100")
    repository = AuditRepository(Database(":memory:"))
    config = _config(run_name="hard-stop-retry")
    service, _, _ = _service(
        broker=broker,
        repository=repository,
        config=config,
        now=after_cutoff,
        market_data=provider,
    )
    repository.record_account_state(
        AccountState(
            timestamp=after_cutoff - timedelta(days=1),
            account_id="paper",
            status="ACTIVE",
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("100000"),
        ),
        (),
        run_id=service.run_id,
    )
    service.ensure_ready(require_trade_updates=True)
    provider.fail_next = True

    service._monitor_risk_snapshot(after_cutoff)
    assert broker.submit_calls == 0
    assert repository.list_rebalances()[0]["status"] == "rejected"

    service._monitor_risk_snapshot(after_cutoff)
    assert broker.submit_calls == 1
    assert [row["status"] for row in repository.list_rebalances()] == [
        "rejected",
        "hard_stop_submitted",
    ]
    service.shutdown()

    restarted, _, _ = _service(
        broker=broker,
        repository=repository,
        config=config,
        now=after_cutoff,
        market_data=provider,
    )
    restarted.ensure_ready(require_trade_updates=True)
    restarted._monitor_risk_snapshot(after_cutoff)
    # The active hard-stop first cancels the prior accepted order, reconciles
    # its terminal state, and then submits one distinct residual attempt.
    assert broker.submit_calls == 2
    orders = broker.get_orders(include_closed=True)
    assert len({order.client_order_id for order in orders}) == 2
    assert {order.status for order in orders} == {"accepted", "canceled"}


def test_hard_stop_replans_filled_capped_residual_until_flat() -> None:
    config = _config(run_name="hard-stop-filled-residual", tickers=("SPY",))
    config.execution.max_single_order_fraction_of_equity = 0.20
    broker = FakePaperBroker(now=NOW, initial_cash="0", auto_fill=True)
    broker.set_position("SPY", "1000", "100")
    service, repository, _ = _service(broker=broker, config=config, mode=RunMode.PAPER_RUN)
    repository.record_account_state(
        AccountState(
            timestamp=NOW - timedelta(days=1),
            account_id="paper",
            status="ACTIVE",
            equity=Decimal("125000"),
            cash=Decimal("125000"),
            buying_power=Decimal("125000"),
        ),
        (),
        run_id=service.run_id,
    )
    service.ensure_ready(require_trade_updates=True)

    for _ in range(5):
        service._monitor_risk_snapshot(NOW)

    assert broker.submit_calls == 5
    assert broker.get_positions() == []
    orders = broker.get_orders(include_closed=True)
    assert len({order.client_order_id for order in orders}) == 5
    assert {order.status for order in orders} == {"filled"}

    service._monitor_risk_snapshot(NOW)
    assert broker.submit_calls == 5


def test_hard_stop_partial_then_canceled_retries_only_current_residual() -> None:
    config = _config(run_name="hard-stop-partial-residual", tickers=("SPY",))
    config.execution.max_single_order_fraction_of_equity = 0.20
    broker = FakePaperBroker(now=NOW, initial_cash="0")
    broker.set_position("SPY", "1000", "100")
    service, repository, _ = _service(broker=broker, config=config, mode=RunMode.PAPER_RUN)
    repository.record_account_state(
        AccountState(
            timestamp=NOW - timedelta(days=1),
            account_id="paper",
            status="ACTIVE",
            equity=Decimal("125000"),
            cash=Decimal("125000"),
            buying_power=Decimal("125000"),
        ),
        (),
        run_id=service.run_id,
    )
    service.ensure_ready(require_trade_updates=True)
    service._monitor_risk_snapshot(NOW)
    first = broker.get_orders(include_closed=True)[0]
    broker.emit_trade_update(
        first.client_order_id,
        "partial_fill",
        fill_quantity="50",
        fill_price="100",
    )

    service._monitor_risk_snapshot(NOW)

    orders = broker.get_orders(include_closed=True)
    assert broker.submit_calls == 2
    assert len({order.client_order_id for order in orders}) == 2
    assert broker.get_positions()[0].quantity == Decimal("950")
    retry = next(order for order in orders if order.client_order_id != first.client_order_id)
    assert retry.requested_quantity == Decimal("200")
    assert retry.requested_quantity <= broker.get_positions()[0].quantity


def test_run_once_plans_from_post_reconciliation_position_snapshot() -> None:
    config = _config(run_name="post-reconcile-refresh", tickers=("SPY",))
    broker = FakePaperBroker(now=NOW, initial_cash="90000")
    broker.set_position("SPY", "100", "100")
    service, _, _ = _service(broker=broker, config=config)
    original_reconcile = service.reconcile
    changed = False

    def reconcile_then_apply_fill():
        nonlocal changed
        result = original_reconcile()
        if not changed:
            broker.set_position("SPY", "40", "100")
            broker.set_cash("96000")
            changed = True
        return result

    service.reconcile = reconcile_then_apply_fill  # type: ignore[method-assign]

    result = service.run_once({})

    assert result.planning is not None
    assert len(result.planning.sells) == 1
    assert result.planning.sells[0].quantity == Decimal("40")
    submitted = broker.get_orders(include_closed=True)[0]
    assert submitted.requested_quantity == Decimal("40")


def test_reconciliation_repairs_crash_lost_fill_with_implied_delta_price() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    run_id = repository.start_run(mode="paper_once", configuration={"safe": True})
    service, _, _ = _service(repository=repository, broker=broker)
    intent = OrderIntent(
        decision_id="fill-crash-decision",
        client_order_id="apa-fill-crash",
        session_date=NOW.date(),
        symbol="SPY",
        side=Side.BUY,
        sequence=0,
        reference_price=Decimal("100"),
        created_at=NOW,
        quantity=Decimal("5"),
        reason="fill_crash_test",
    )
    manager = service.order_manager
    manager.run_id = run_id
    assert manager.submit_intent(intent) is not None
    first = broker.emit_trade_update(
        intent.client_order_id,
        "partial_fill",
        fill_quantity="2",
        fill_price="100",
        event_id="first-partial",
    )
    assert manager.process_trade_update(first) is True
    assert repository.cumulative_fill_quantity(intent.client_order_id) == Decimal("2")

    second = broker.emit_trade_update(
        intent.client_order_id,
        "partial_fill",
        fill_quantity="3",
        fill_price="120",
        event_id="second-partial",
    )
    cumulative = BrokerOrderState(
        client_order_id=second.order.client_order_id,
        broker_order_id=second.order.broker_order_id,
        symbol=second.order.symbol,
        side=second.order.side,
        status="partial_fill",
        submitted_at=second.order.submitted_at,
        updated_at=second.order.updated_at,
        requested_quantity=second.order.requested_quantity,
        filled_quantity=Decimal("5"),
        average_fill_price=Decimal("112"),
    )
    broker.inject_order(cumulative)
    # Simulate a crash after the mutable projection commit but before the
    # immutable fill insert for the second partial fill.
    assert repository.transition_order(
        client_order_id=intent.client_order_id,
        to_state=LocalOrderState.PARTIALLY_FILLED,
        event_type="trade_update",
        event_key="simulated-crash-after-transition",
        allowed_from={LocalOrderState.PARTIALLY_FILLED},
        created_at=NOW,
        broker_order_id=cumulative.broker_order_id,
        raw_status=cumulative.status,
        filled_quantity=cumulative.filled_quantity,
        average_fill_price=cumulative.average_fill_price,
    )
    assert repository.cumulative_fill_quantity(intent.client_order_id) == Decimal("2")

    repaired = service.reconcile()

    assert repaired.blocking is False
    assert any(item.kind == "missing_fill_event" for item in repaired.discrepancies)
    assert repository.cumulative_fill_quantity(intent.client_order_id) == Decimal("5")
    with repository.database.engine.connect() as connection:
        rows = connection.execute(
            select(fill_events.c.quantity, fill_events.c.price, fill_events.c.payload).where(
                fill_events.c.client_order_id == intent.client_order_id
            )
        ).all()
    assert sorted((Decimal(row.quantity), Decimal(row.price)) for row in rows) == [
        (Decimal("2"), Decimal("100")),
        (Decimal("3"), Decimal("120")),
    ]
    assert any(row.payload["source"] == "reconciliation" for row in rows)

    second_reconciliation = service.reconcile()
    assert second_reconciliation.clean is True
    assert repository.count("fill_events") == 2


def test_pre_submission_receipt_survives_accept_then_crash_without_duplicate() -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    config = _config(run_name="receipt-before-submit", tickers=("SPY",))
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    provider = ReplayMarketDataProvider((_bar("SPY"),))
    service, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        market_data=provider,
    )
    original_submit = broker.submit_order
    receipts_seen_at_submit: list[int] = []

    def accept_then_crash(intent):
        receipts_seen_at_submit.append(repository.count("decision_receipts"))
        original_submit(intent)
        raise SimulatedProcessCrash

    broker.submit_order = accept_then_crash  # type: ignore[method-assign]
    with pytest.raises(SimulatedProcessCrash):
        service.run_once({"SPY": 0.10})

    assert broker.submit_calls == 1
    assert receipts_seen_at_submit == [1]
    assert repository.count("decision_receipts") == 1
    decision = repository.list_rebalances()[0]
    assert decision["status"] == "submission_authorized"
    with repository.database.engine.connect() as connection:
        receipt = connection.execute(select(decision_receipts.c.payload)).scalar_one()
    assert receipt["status"] == "submission_authorized"
    assert receipt["execution_phase"] == "authorized_not_yet_submitted"
    assert receipt["planning"]["intents"][0]["symbol"] == "SPY"

    broker.submit_order = original_submit  # type: ignore[method-assign]
    restarted, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        market_data=ReplayMarketDataProvider((_bar("SPY"),)),
    )
    duplicate = restarted.run_once({"SPY": 0.10})

    assert duplicate.status == "duplicate"
    assert broker.submit_calls == 1
    assert repository.count("decision_receipts") == 1
    assert len(broker.get_orders(include_closed=True)) == 1


def test_observer_receipt_precedes_hypothetical_order_intent_facts() -> None:
    service, repository, broker = _service(mode=RunMode.OBSERVE)
    service.start_streams()
    original_record = repository.record_hypothetical_order_intents
    receipts_seen: list[int] = []

    def record_after_receipt(*, run_id, intents, mode):
        receipts_seen.append(repository.count("decision_receipts"))
        return original_record(run_id=run_id, intents=intents, mode=mode)

    repository.record_hypothetical_order_intents = record_after_receipt  # type: ignore[method-assign]

    result = service.run_once({"SPY": 0.10})

    assert result.status == "observed"
    assert receipts_seen == [1]
    assert repository.count("order_intents") == 1
    assert repository.count("broker_orders") == 0
    assert broker.submit_calls == 0
    with repository.database.engine.connect() as connection:
        receipt = connection.execute(select(decision_receipts.c.payload)).scalar_one()
    assert receipt["execution_phase"] == "hypothetical_not_submitted"


def test_halt_flatten_is_ready_audited_and_completes_capped_terminal_fills() -> None:
    config = _config(run_name="operator-flatten-terminal", tickers=("SPY",))
    config.execution.max_single_order_fraction_of_equity = 0.20
    broker = FakePaperBroker(now=NOW, initial_cash="0", auto_fill=True)
    broker.set_position("SPY", "1000", "100")
    repository = AuditRepository(Database(":memory:"))
    provider = ReplayMarketDataProvider((_bar("SPY"),))
    service, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        market_data=provider,
    )
    original_submit = broker.submit_order
    receipts_seen_at_submit: list[int] = []

    def submit_after_receipt(intent):
        receipts_seen_at_submit.append(repository.count("decision_receipts"))
        return original_submit(intent)

    broker.submit_order = submit_after_receipt  # type: ignore[method-assign]

    halt_id = service.halt(
        "operator requested complete simulated liquidation",
        flatten=True,
        acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
    )

    assert provider.connected is True
    assert broker.get_positions() == []
    assert broker.submit_calls == 5
    assert receipts_seen_at_submit == [1, 2, 3, 4, 5]
    assert repository.count("decision_receipts") == 5
    assert {row["status"] for row in repository.list_rebalances()} == {"liquidation_submitted"}
    with repository.database.engine.connect() as connection:
        cancellation = connection.execute(
            select(stream_events.c.payload).where(
                stream_events.c.event_type == "operator_halt_cancel_all_requested"
            )
        ).scalar_one()
        receipts = connection.execute(select(decision_receipts.c.payload)).scalars().all()
    assert cancellation["halt_event_id"] == halt_id
    assert all(receipt["execution_phase"] == "authorized_not_yet_submitted" for receipt in receipts)


def test_halt_flatten_reconciles_then_refreshes_before_planning() -> None:
    config = _config(run_name="operator-flatten-refresh", tickers=("SPY",))
    broker = FakePaperBroker(now=NOW, initial_cash="90000")
    broker.set_position("SPY", "100", "100")
    service, _, _ = _service(broker=broker, config=config)
    original_reconcile = service.reconcile
    changed = False

    def reconcile_then_change_holding():
        nonlocal changed
        result = original_reconcile()
        if not changed:
            broker.set_position("SPY", "40", "100")
            broker.set_cash("96000")
            changed = True
        return result

    service.reconcile = reconcile_then_change_holding  # type: ignore[method-assign]

    service.halt(
        "refresh before simulated liquidation",
        flatten=True,
        acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
    )

    submitted = broker.get_orders(include_closed=True)
    assert len(submitted) == 1
    assert submitted[0].requested_quantity == Decimal("40")


def test_operator_flatten_retries_partial_canceled_residual_after_restart() -> None:
    config = _config(run_name="operator-flatten-restart", tickers=("SPY",))
    config.execution.max_single_order_fraction_of_equity = 0.20
    broker = FakePaperBroker(now=NOW, initial_cash="0")
    broker.set_position("SPY", "1000", "100")
    repository = AuditRepository(Database(":memory:"))
    service, _, _ = _service(
        broker=broker,
        repository=repository,
        config=config,
        mode=RunMode.PAPER_RUN,
    )

    halt_id = service.halt(
        "durable simulated liquidation",
        flatten=True,
        acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
    )
    first = broker.get_orders(include_closed=True)[0]
    assert first.requested_quantity == Decimal("200")
    service.shutdown()

    broker.emit_trade_update(
        first.client_order_id,
        "partial_fill",
        fill_quantity="50",
        fill_price="100",
    )
    broker.cancel_order(first.broker_order_id)
    restarted, _, _ = _service(
        broker=broker,
        repository=repository,
        config=config,
        mode=RunMode.PAPER_RUN,
    )
    restarted.ensure_ready(require_trade_updates=True)

    restarted._monitor_risk_snapshot(NOW)

    orders = broker.get_orders(include_closed=True)
    assert broker.submit_calls == 2
    assert len({order.client_order_id for order in orders}) == 2
    retry = next(order for order in orders if order.client_order_id != first.client_order_id)
    assert retry.requested_quantity == Decimal("200")
    assert retry.requested_quantity <= broker.get_positions()[0].quantity
    assert restarted._active_operator_flatten_request(NOW) == halt_id
    assert repository.count("decision_receipts") == 2


@pytest.mark.parametrize(
    ("mode", "dry_run"),
    ((RunMode.OBSERVE, False), (RunMode.PAPER_ONCE, True)),
)
def test_refused_service_mode_flatten_never_becomes_restart_executable(
    mode: RunMode,
    dry_run: bool,
) -> None:
    config = _config(run_name=f"refused-flatten-{mode.value}-{dry_run}", tickers=("SPY",))
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW, initial_cash="90000")
    broker.set_position("SPY", "100", "100")
    original_cancel = broker.cancel_all_orders
    cancel_calls = 0

    def count_cancel() -> None:
        nonlocal cancel_calls
        cancel_calls += 1
        original_cancel()

    broker.cancel_all_orders = count_cancel  # type: ignore[method-assign]
    refused, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        mode=mode,
        dry_run=dry_run,
    )

    with pytest.raises(SafetyViolation):
        refused.halt(
            "refused flatten must remain a manual halt only",
            flatten=True,
            acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
        )

    manual = repository.active_halts(NOW)["manual"]
    assert manual["details"]["flatten_requested"] is False
    assert manual["details"]["flatten_refused_by_service_mode"] is True
    assert refused._active_operator_flatten_request(NOW) is None
    assert refused._pending_operator_flatten_request(NOW) is None

    restarted, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        mode=RunMode.PAPER_RUN,
    )
    restarted.ensure_ready(require_trade_updates=True)
    restarted._monitor_risk_snapshot(NOW)

    assert cancel_calls == 0
    assert broker.submit_calls == 0
    assert repository.count("decision_receipts") == 0


def test_failed_flatten_cancel_is_retried_before_restart_submission() -> None:
    config = _config(run_name="failed-cancel-restart", tickers=("SPY",))
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW, initial_cash="90000")
    broker.set_position("SPY", "100", "100")
    original_cancel = broker.cancel_all_orders
    cancel_failures = 0

    def fail_cancel() -> None:
        nonlocal cancel_failures
        cancel_failures += 1
        raise RuntimeError("synthetic cancel outage")

    broker.cancel_all_orders = fail_cancel  # type: ignore[method-assign]
    service, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        mode=RunMode.PAPER_RUN,
    )
    with pytest.raises(ReconciliationBlocked):
        service.halt(
            "cancel must succeed before flatten",
            flatten=True,
            acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
        )
    halt_id = str(repository.active_halts(NOW)["manual"]["halt_event_id"])
    assert repository.has_operator_halt_cancel_success(halt_id) is False
    assert service._active_operator_flatten_request(NOW) is None
    assert service._pending_operator_flatten_request(NOW) == halt_id

    restarted, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        mode=RunMode.PAPER_RUN,
    )
    restarted.ensure_ready(require_trade_updates=True)
    restarted._monitor_risk_snapshot(NOW)
    assert cancel_failures == 2
    assert broker.submit_calls == 0
    assert repository.has_operator_halt_cancel_success(halt_id) is False

    broker.cancel_all_orders = original_cancel  # type: ignore[method-assign]
    evidence_at_submit: list[bool] = []
    original_submit = broker.submit_order

    def submit_after_cancel_evidence(intent):
        evidence_at_submit.append(repository.has_operator_halt_cancel_success(halt_id))
        return original_submit(intent)

    broker.submit_order = submit_after_cancel_evidence  # type: ignore[method-assign]
    restarted._monitor_risk_snapshot(NOW)

    assert evidence_at_submit == [True]
    assert broker.submit_calls == 1
    assert restarted._active_operator_flatten_request(NOW) == halt_id


def test_crash_before_cancel_evidence_retries_cancel_before_restart_submission() -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    config = _config(run_name="cancel-stage-crash", tickers=("SPY",))
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW, initial_cash="90000")
    broker.set_position("SPY", "100", "100")
    original_cancel = broker.cancel_all_orders

    def crash_before_cancel() -> None:
        raise SimulatedProcessCrash

    broker.cancel_all_orders = crash_before_cancel  # type: ignore[method-assign]
    service, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        mode=RunMode.PAPER_RUN,
    )
    with pytest.raises(SimulatedProcessCrash):
        service.halt(
            "simulate crash before durable cancel evidence",
            flatten=True,
            acknowledgement=PAPER_FLATTEN_ACKNOWLEDGEMENT,
        )
    halt_id = str(repository.active_halts(NOW)["manual"]["halt_event_id"])
    assert repository.has_operator_halt_cancel_success(halt_id) is False
    assert repository.count("stream_events") == 0
    assert broker.submit_calls == 0

    broker.cancel_all_orders = original_cancel  # type: ignore[method-assign]
    evidence_at_submit: list[bool] = []
    original_submit = broker.submit_order

    def submit_after_cancel_evidence(intent):
        evidence_at_submit.append(repository.has_operator_halt_cancel_success(halt_id))
        return original_submit(intent)

    broker.submit_order = submit_after_cancel_evidence  # type: ignore[method-assign]
    restarted, _, _ = _service(
        repository=repository,
        broker=broker,
        config=config,
        mode=RunMode.PAPER_RUN,
    )
    restarted.ensure_ready(require_trade_updates=True)
    restarted._monitor_risk_snapshot(NOW)

    assert evidence_at_submit == [True]
    assert broker.submit_calls == 1
    assert repository.has_operator_halt_cancel_success(halt_id) is True
