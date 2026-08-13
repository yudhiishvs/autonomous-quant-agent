from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adaptive_trader.broker import FakePaperBroker
from adaptive_trader.clock import FakeClock, as_utc
from adaptive_trader.constants import (
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    UTC,
)
from adaptive_trader.exceptions import InvalidOrderTransition, SafetyViolation
from adaptive_trader.execution import (
    OrderManager,
    OrderPlanner,
    OrderStateMachine,
    deterministic_client_order_id,
    evaluate_order_enablement,
)
from adaptive_trader.live import LiveService
from adaptive_trader.live_models import (
    AccountState,
    BrokerOrderState,
    DataFreshnessState,
    DiscrepancySeverity,
    LocalOrderState,
    MarketBar,
    MarketClockState,
    OrderIntent,
    PositionState,
    RunMode,
    Side,
)
from adaptive_trader.market_data_live import BarStore, ReplayMarketDataProvider
from adaptive_trader.persistence import AuditRepository, Database, configuration_hash
from adaptive_trader.reconciliation import reconcile

NOW = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)


def _enabled_config() -> SimpleNamespace:
    return SimpleNamespace(
        execution=SimpleNamespace(
            paper_only=True,
            paper_order_submission_enabled=True,
        )
    )


def _live_config(run_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        project=SimpleNamespace(run_name=run_name),
        data=SimpleNamespace(tickers=["SPY"], benchmark="SPY"),
        market_data=SimpleNamespace(stale_after_seconds=180),
        schedule=SimpleNamespace(
            catch_up_cutoff_et="14:30",
            one_shot_readiness_timeout_seconds=0,
        ),
        execution=SimpleNamespace(
            paper_only=True,
            paper_order_submission_enabled=False,
            minimum_order_notional=25,
            max_single_order_fraction_of_equity=0.2,
            required_cash_buffer=0.02,
            maximum_orders_per_rebalance=20,
        ),
        risk=SimpleNamespace(
            daily_loss_limit=0.03,
            drawdown_soft_limit=0.10,
            drawdown_hard_limit=0.15,
            soft_limit_max_gross_exposure=0.50,
        ),
    )


def _account() -> AccountState:
    return AccountState(
        timestamp=NOW,
        account_id="paper-account",
        status="ACTIVE",
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("50000"),
        last_equity=Decimal("100000"),
    )


def _market_clock(*, is_open: bool = True) -> MarketClockState:
    return MarketClockState(
        timestamp=NOW,
        is_open=is_open,
        next_open=NOW + timedelta(days=1),
        next_close=NOW + timedelta(hours=6),
    )


def _freshness(*, stale: bool = False) -> DataFreshnessState:
    return DataFreshnessState(
        checked_at=NOW,
        stream_healthy=True,
        stale_after_seconds=180,
        last_bar_by_symbol={"SPY": NOW},
        stale_symbols=("SPY",) if stale else (),
    )


def _missing_symbol_freshness() -> DataFreshnessState:
    return DataFreshnessState(
        checked_at=NOW,
        stream_healthy=True,
        stale_after_seconds=180,
        last_bar_by_symbol={"SPY": NOW},
        missing_symbols=("QQQ",),
    )


def _gate_arguments() -> dict[str, object]:
    return {
        "mode": RunMode.PAPER_ONCE,
        "environment": {PAPER_ORDER_ENABLEMENT_ENV: PAPER_ORDER_ACKNOWLEDGEMENT},
        "broker_paper_only": True,
        "credentials_verified": True,
        "provider_authority_verified": True,
        "startup_preflight_verified": True,
        "account": _account(),
        "market_clock": _market_clock(),
        "freshness": _freshness(),
        "reconciliation_clean": True,
        "halt_active": False,
        "before_cutoff": True,
        "dry_run": False,
    }


@pytest.mark.parametrize(
    ("case", "reason_fragment"),
    [
        ("missing_token", PAPER_ORDER_ENABLEMENT_ENV),
        ("wrong_token", PAPER_ORDER_ENABLEMENT_ENV),
        ("observer", "not a paper-order command"),
        ("dry_run", "dry-run"),
        ("market_closed", "market is closed"),
        ("after_cutoff", "cutoff has passed"),
        ("stale", "not fresh"),
        ("missing_symbol", "not fresh"),
        ("halt", "halt latch"),
        ("reconciliation", "reconciliation"),
        ("credentials", "credentials"),
        ("provider_authority", "provider authority"),
        ("startup_preflight", "startup preflights"),
    ],
)
def test_central_gate_denies_each_missing_safety_condition(case: str, reason_fragment: str) -> None:
    arguments = _gate_arguments()
    assert evaluate_order_enablement(_enabled_config(), **arguments).allowed is True

    if case == "missing_token":
        arguments["environment"] = {}
    elif case == "wrong_token":
        arguments["environment"] = {PAPER_ORDER_ENABLEMENT_ENV: "yes"}
    elif case == "observer":
        arguments["mode"] = RunMode.OBSERVE
    elif case == "dry_run":
        arguments["dry_run"] = True
    elif case == "market_closed":
        arguments["market_clock"] = _market_clock(is_open=False)
    elif case == "after_cutoff":
        arguments["before_cutoff"] = False
    elif case == "stale":
        arguments["freshness"] = _freshness(stale=True)
    elif case == "missing_symbol":
        arguments["freshness"] = _missing_symbol_freshness()
    elif case == "halt":
        arguments["halt_active"] = True
    elif case == "reconciliation":
        arguments["reconciliation_clean"] = False
    elif case == "credentials":
        arguments["credentials_verified"] = False
    elif case == "provider_authority":
        arguments["provider_authority_verified"] = False
    elif case == "startup_preflight":
        arguments["startup_preflight_verified"] = False

    result = evaluate_order_enablement(_enabled_config(), **arguments)

    assert result.allowed is False
    assert any(reason_fragment in reason for reason in result.reasons)
    assert result.effective_mode in {"observer", "dry_run"}


def test_state_machine_rejects_skipping_persist_before_submit_states() -> None:
    with pytest.raises(InvalidOrderTransition):
        OrderStateMachine.validate(LocalOrderState.PLANNED, LocalOrderState.FILLED)


def _planner(
    *,
    minimum: str = "25",
    cash_buffer: str = "0",
    max_order_fraction: str = "1",
) -> OrderPlanner:
    return OrderPlanner(
        minimum_order_notional=Decimal(minimum),
        max_single_order_fraction_of_equity=Decimal(max_order_fraction),
        required_cash_buffer=Decimal(cash_buffer),
        maximum_orders=10,
        stale_after_seconds=180,
    )


def _position(
    symbol: str,
    quantity: str,
    price: str = "100",
) -> PositionState:
    qty = Decimal(quantity)
    px = Decimal(price)
    return PositionState(
        timestamp=NOW,
        symbol=symbol,
        quantity=qty,
        market_value=qty * px,
        average_entry_price=px,
        current_price=px,
    )


def _planning_account(*, equity: str = "100000", cash: str = "50000") -> AccountState:
    return AccountState(
        timestamp=NOW,
        account_id="paper",
        status="ACTIVE",
        equity=Decimal(equity),
        cash=Decimal(cash),
        buying_power=Decimal(cash),
    )


def _plan(
    planner: OrderPlanner,
    *,
    targets: dict[str, float],
    account: AccountState,
    positions: tuple[PositionState, ...] = (),
    open_orders: tuple[BrokerOrderState, ...] = (),
):
    return planner.plan(
        decision_id="decision-plan",
        session_date=date(2026, 1, 2),
        created_at=NOW,
        target_weights=targets,
        account=account,
        positions=positions,
        latest_prices={"SPY": (Decimal("100"), NOW), "QQQ": (Decimal("100"), NOW)},
        universe=("SPY", "QQQ"),
        open_orders=open_orders,
    )


def test_planner_orders_sells_before_buys() -> None:
    result = _plan(
        _planner(),
        targets={"SPY": 0.0, "QQQ": 0.5},
        account=_planning_account(),
        positions=(_position("SPY", "500"),),
    )

    assert [intent.side for intent in result.intents] == [Side.SELL, Side.BUY]


def test_planner_never_spends_pending_sale_proceeds_or_cash_buffer() -> None:
    result = _plan(
        _planner(cash_buffer="0.10"),
        targets={"QQQ": 0.5},
        account=_planning_account(equity="1000", cash="150"),
    )

    assert len(result.buys) == 1
    assert result.buys[0].notional == Decimal("50")


def test_planner_skips_minimum_notional_and_conflicting_open_order() -> None:
    below_minimum = _plan(
        _planner(minimum="25"),
        targets={"QQQ": 0.01},
        account=_planning_account(equity="1000", cash="1000"),
    )
    conflicting_order = BrokerOrderState(
        client_order_id="apa-existing-qqq-buy",
        broker_order_id="broker-existing",
        symbol="QQQ",
        side=Side.BUY,
        status="accepted",
        submitted_at=NOW,
        updated_at=NOW,
        requested_notional=Decimal("100"),
    )
    conflict = _plan(
        _planner(),
        targets={"QQQ": 0.5},
        account=_planning_account(),
        open_orders=(conflicting_order,),
    )

    assert below_minimum.intents == ()
    assert any(item["reason"] == "below_minimum_notional" for item in below_minimum.skipped)
    assert conflict.intents == ()
    assert any(item["reason"] == "conflicting_open_order" for item in conflict.skipped)


def test_planner_rejects_negative_position() -> None:
    with pytest.raises(SafetyViolation, match="Negative broker position"):
        _plan(
            _planner(),
            targets={"SPY": 0.0},
            account=_planning_account(),
            positions=(_position("SPY", "-1"),),
        )


def test_planner_rejects_negative_target_and_never_oversells_holdings() -> None:
    with pytest.raises(SafetyViolation, match="nonnegative"):
        _plan(
            _planner(),
            targets={"SPY": -0.1},
            account=_planning_account(),
            positions=(_position("SPY", "10"),),
        )

    result = _plan(
        _planner(),
        targets={"SPY": 0.0},
        account=_planning_account(),
        positions=(_position("SPY", "10"),),
    )
    assert len(result.sells) == 1
    assert result.sells[0].quantity == Decimal("10.000000000")


def test_planner_clips_single_order_and_client_ids_are_deterministic() -> None:
    result = _plan(
        _planner(max_order_fraction="0.10"),
        targets={"QQQ": 1.0},
        account=_planning_account(equity="100000", cash="100000"),
    )
    assert len(result.buys) == 1
    assert result.buys[0].notional == Decimal("10000.00")

    arguments = {
        "session_date": date(2026, 1, 2),
        "decision_id": "stable-decision",
        "symbol": "SPY",
        "side": Side.BUY,
        "sequence": 0,
    }
    first = deterministic_client_order_id(**arguments)
    assert deterministic_client_order_id(**arguments) == first
    assert len(first) <= 48
    assert deterministic_client_order_id(**{**arguments, "decision_id": "other"}) != first


def test_extended_hours_order_state_is_unrepresentable() -> None:
    with pytest.raises(SafetyViolation, match="Extended-hours"):
        BrokerOrderState(
            client_order_id="apa-no-extended-hours",
            broker_order_id="broker-no-extended-hours",
            symbol="SPY",
            side=Side.BUY,
            status="accepted",
            submitted_at=NOW,
            updated_at=NOW,
            requested_notional=Decimal("100"),
            extended_hours=True,
        )


def _bar(start: datetime) -> MarketBar:
    return MarketBar(
        symbol="SPY",
        start=start,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=100,
        feed="REPLAY",
        received_at=NOW,
        source="safety-test",
    )


def test_bar_store_deduplicates_tracks_out_of_order_staleness_and_disconnect() -> None:
    repository = AuditRepository(Database(":memory:"))
    clock = FakeClock(NOW)
    store = BarStore(
        repository,
        universe=("SPY",),
        stale_after_seconds=60,
        clock=clock,
    )
    store.mark_health(True, "test_connected")

    assert store.ingest(_bar(NOW)) == "inserted"
    assert store.ingest(_bar(NOW)) == "duplicate"
    assert store.ingest(_bar(NOW - timedelta(seconds=30))) == "inserted"
    assert repository.count("market_bars") == 2
    assert repository.latest_bar_times(("SPY",))["SPY"] == NOW
    assert repository.count("stream_events") == 5
    assert store.freshness().fresh is True

    clock.advance(61)
    assert store.freshness().stale_symbols == ("SPY",)
    store.mark_health(False, "test_disconnect")
    disconnected = store.freshness()
    assert disconnected.stream_healthy is False
    assert disconnected.fresh is False


def test_backfill_then_stream_duplicate_is_stored_once() -> None:
    repository = AuditRepository(Database(":memory:"))
    provider = ReplayMarketDataProvider((_bar(NOW),))
    store = BarStore(
        repository,
        universe=("SPY",),
        stale_after_seconds=60,
        clock=FakeClock(NOW),
    )
    backfill = provider.get_bars(
        ("SPY",),
        start=NOW - timedelta(minutes=1),
        end=NOW,
    )
    assert store.ingest(backfill[0]) == "inserted"
    provider.start_stream(("SPY",), store.ingest, store.mark_health)

    assert provider.emit(_bar(NOW)) == "duplicate"
    assert repository.count("market_bars") == 1


def test_market_timestamps_reject_naive_values_and_round_trip_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        as_utc(datetime(2026, 1, 2, 15, 5))

    repository = AuditRepository(Database(":memory:"))
    store = BarStore(
        repository,
        universe=("SPY",),
        stale_after_seconds=60,
        clock=FakeClock(NOW),
    )
    store.ingest(_bar(NOW))
    restored = repository.latest_bar_times(("SPY",))["SPY"]
    assert restored == NOW
    assert restored.utcoffset() == timedelta(0)


def test_unresolved_gap_survives_restart_until_verified_backfill(tmp_path) -> None:
    path = tmp_path / "durable-gap.sqlite3"
    first_database = Database(path)
    first_repository = AuditRepository(first_database)
    first_store = BarStore(
        first_repository,
        universe=("SPY",),
        stale_after_seconds=300,
        clock=FakeClock(NOW),
    )
    first_store.ingest(_bar(NOW - timedelta(minutes=3)))
    first_store.ingest(_bar(NOW))
    assert first_store.freshness().unresolved_gap is True
    assert len(first_repository.unresolved_gaps(("SPY",))) == 1
    first_database.close()

    second_database = Database(path)
    second_repository = AuditRepository(second_database)
    restarted = BarStore(
        second_repository,
        universe=("SPY",),
        stale_after_seconds=300,
        clock=FakeClock(NOW),
    )
    assert restarted.freshness().unresolved_gap is True
    restarted.mark_health(True, "reconnect_backfill_incomplete")
    assert restarted.freshness().unresolved_gap is True
    assert restarted.freshness().stream_healthy is False

    restarted.mark_health(True, "reconnect_backfill_complete")
    assert restarted.freshness().unresolved_gap is True
    assert len(second_repository.unresolved_gaps(("SPY",))) == 1

    # Only explicit durable coverage of every missing minute may clear the
    # pre-restart gap; a process-local reconnect event alone is insufficient.
    restarted.ingest(_bar(NOW - timedelta(minutes=2)))
    restarted.ingest(_bar(NOW - timedelta(minutes=1)))
    restarted.mark_health(True, "reconnect_backfill_complete")
    assert restarted.freshness().unresolved_gap is False
    assert restarted.freshness().stream_healthy is True
    assert second_repository.unresolved_gaps(("SPY",)) == []
    assert second_repository.count("market_data_gaps") == 1
    second_database.close()

    third_database = Database(path)
    try:
        final_store = BarStore(
            AuditRepository(third_database),
            universe=("SPY",),
            stale_after_seconds=300,
            clock=FakeClock(NOW),
        )
        assert final_store.freshness().unresolved_gap is False
    finally:
        third_database.close()


def test_live_startup_automatically_recovers_exact_durable_gap_interval(tmp_path) -> None:
    path = tmp_path / "automatic-durable-gap.sqlite3"
    first_database = Database(path)
    first_repository = AuditRepository(first_database)
    first_store = BarStore(
        first_repository,
        universe=("SPY",),
        stale_after_seconds=300,
        clock=FakeClock(NOW),
    )
    first_store.ingest(_bar(NOW - timedelta(minutes=3)))
    first_store.ingest(_bar(NOW))
    gap_id = str(first_repository.unresolved_gaps(("SPY",))[0]["gap_id"])
    first_database.close()

    config = _live_config("automatic-durable-gap")
    second_database = Database(path)
    second_repository = AuditRepository(second_database)
    unavailable = ReplayMarketDataProvider(())
    blocked_service = LiveService(
        config,
        repository=second_repository,
        broker=FakePaperBroker(now=NOW),
        market_data=unavailable,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
    )
    blocked_service.start_streams()
    assert [str(row["gap_id"]) for row in second_repository.unresolved_gaps(("SPY",))] == [gap_id]
    assert blocked_service.bar_store.freshness(NOW).unresolved_gap is True
    assert second_repository.active_incidents(configuration_hash=configuration_hash(config))
    blocked_service.shutdown()
    second_database.close()

    third_database = Database(path)
    third_repository = AuditRepository(third_database)
    exact_backfill = ReplayMarketDataProvider(
        (
            _bar(NOW - timedelta(minutes=2)),
            _bar(NOW - timedelta(minutes=1)),
        )
    )
    recovered_service = LiveService(
        config,
        repository=third_repository,
        broker=FakePaperBroker(now=NOW),
        market_data=exact_backfill,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
    )
    try:
        recovered_service.start_streams()
        assert third_repository.unresolved_gaps(("SPY",)) == []
        assert recovered_service.bar_store.freshness(NOW).unresolved_gap is False
        assert (
            third_repository.active_incidents(configuration_hash=configuration_hash(config)) == []
        )
        gaps = third_repository.unresolved_gaps(("SPY",))
        assert gaps == []
    finally:
        recovered_service.shutdown()
        third_database.close()


def _broker_order(symbol: str = "SPY") -> BrokerOrderState:
    return BrokerOrderState(
        client_order_id=f"apa-unknown-{symbol.lower()}",
        broker_order_id=f"broker-unknown-{symbol.lower()}",
        symbol=symbol,
        side=Side.BUY,
        status="accepted",
        submitted_at=NOW,
        updated_at=NOW,
        requested_notional=Decimal("100"),
    )


def test_reconciliation_blocks_unknown_broker_order() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    broker.inject_order(_broker_order())

    result = reconcile(
        repository,
        broker,
        universe=("SPY",),
        clock=FakeClock(NOW),
    )

    unknown = next(item for item in result.discrepancies if item.kind == "unknown_broker_order")
    assert result.blocking is True
    assert unknown.severity is DiscrepancySeverity.BLOCKING


def test_reconciliation_marks_negative_unexpected_position_critical() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    broker.set_position("QQQ", "-1", "100")

    result = reconcile(
        repository,
        broker,
        universe=("SPY",),
        clock=FakeClock(NOW),
    )

    by_kind = {item.kind: item for item in result.discrepancies}
    assert result.blocking is True
    assert by_kind["negative_position"].severity is DiscrepancySeverity.CRITICAL
    assert by_kind["unexpected_symbol"].severity is DiscrepancySeverity.CRITICAL


def test_reconciliation_detects_ordinary_position_quantity_mismatch() -> None:
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    repository.record_account_state(
        _planning_account(),
        (_position("SPY", "5"),),
        run_id=None,
    )
    broker.set_position("SPY", "4", "100")

    result = reconcile(
        repository,
        broker,
        universe=("SPY",),
        clock=FakeClock(NOW),
    )

    mismatch = next(
        item for item in result.discrepancies if item.kind == "position_quantity_mismatch"
    )
    assert result.blocking is True
    assert mismatch.details == {"local": "5", "broker": "4"}


def test_halt_latch_persists_across_database_reopen(tmp_path) -> None:
    path = tmp_path / "live-state.sqlite3"
    first_database = Database(path)
    first = AuditRepository(first_database)
    first.record_halt(
        run_id=None,
        action="halt",
        latch_type="manual",
        initiator="test",
        reason="safety matrix",
        created_at=NOW,
    )
    first_database.close()

    second_database = Database(path)
    second = AuditRepository(second_database)
    try:
        assert "manual" in second.active_halts(NOW)
    finally:
        second_database.close()


def test_hard_stop_latch_survives_restart_after_equity_recovers(tmp_path) -> None:
    path = tmp_path / "hard-stop-restart.sqlite3"
    config = _live_config("hard-stop-restart")
    first_database = Database(path)
    first_repository = AuditRepository(first_database)
    first_broker = FakePaperBroker(now=NOW, initial_cash="80000")
    first_service = LiveService(
        config,
        repository=first_repository,
        broker=first_broker,
        market_data=ReplayMarketDataProvider((_bar(NOW),)),
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
    )
    first_repository.record_account_state(
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
        run_id=first_service.run_id,
    )
    triggered = first_service._apply_account_risk_latches(
        first_broker.get_account(),
        NOW.date(),
        mutations_authorized=False,
    )
    assert triggered["hard_stop_active"] is True
    first_service.shutdown()
    first_database.close()

    second_database = Database(path)
    second_repository = AuditRepository(second_database)
    recovered_broker = FakePaperBroker(now=NOW, initial_cash="100000")
    restarted = LiveService(
        config,
        repository=second_repository,
        broker=recovered_broker,
        market_data=ReplayMarketDataProvider((_bar(NOW),)),
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
    )
    recovered = restarted._apply_account_risk_latches(
        recovered_broker.get_account(),
        NOW.date(),
        mutations_authorized=False,
    )
    try:
        assert recovered["drawdown"] == Decimal("0")
        assert recovered["hard_stop_active"] is True
        assert "hard_stop" in second_repository.active_halts(NOW)
    finally:
        restarted.shutdown()
        second_database.close()


def _intent(
    *,
    decision_id: str,
    client_order_id: str,
    symbol: str,
    sequence: int,
) -> OrderIntent:
    return OrderIntent(
        decision_id=decision_id,
        client_order_id=client_order_id,
        session_date=date(2026, 1, 2),
        symbol=symbol,
        side=Side.BUY,
        sequence=sequence,
        reference_price=Decimal("100"),
        created_at=NOW,
        notional=Decimal("1000"),
    )


def test_order_updates_handle_partial_fill_rejection_and_duplicate_event() -> None:
    repository = AuditRepository(Database(":memory:"))
    run_id = repository.start_run(mode="paper_once", configuration={"safe": True})
    broker = FakePaperBroker(now=NOW)
    manager = OrderManager(repository=repository, broker=broker, run_id=run_id)
    partial_intent = _intent(
        decision_id="partial-decision",
        client_order_id="apa-partial-spy-buy-00",
        symbol="SPY",
        sequence=0,
    )
    assert manager.submit_intent(partial_intent).status == "accepted"

    partial = broker.emit_trade_update(
        partial_intent.client_order_id,
        "partial_fill",
        fill_quantity="2",
        fill_price="100",
        event_id="paper-event-partial-1",
    )
    assert manager.process_trade_update(partial) is True
    assert manager.process_trade_update(partial) is False
    assert repository.get_order(partial_intent.client_order_id)["state"] == (
        LocalOrderState.PARTIALLY_FILLED
    )
    partial_row = repository.get_order(partial_intent.client_order_id)
    assert Decimal(partial_row["filled_quantity"]) == Decimal("2")
    requested_quantity = partial_intent.estimated_notional / partial_intent.reference_price
    assert requested_quantity - Decimal(partial_row["filled_quantity"]) == Decimal("8")
    assert repository.count("fill_events") == 1

    filled = broker.emit_trade_update(
        partial_intent.client_order_id,
        "fill",
        fill_quantity="8",
        fill_price="100",
        event_id="paper-event-fill-1",
    )
    assert manager.process_trade_update(filled) is True
    assert repository.get_order(partial_intent.client_order_id)["state"] == LocalOrderState.FILLED

    rejected_intent = _intent(
        decision_id="rejected-decision",
        client_order_id="apa-rejected-qqq-buy-00",
        symbol="QQQ",
        sequence=0,
    )
    broker.reject_client_ids.add(rejected_intent.client_order_id)
    rejected = manager.submit_intent(rejected_intent)
    assert rejected is not None and rejected.status == "rejected"
    assert repository.get_order(rejected_intent.client_order_id)["state"] == (
        LocalOrderState.REJECTED
    )


def test_target_provider_rejection_completes_receipt_and_metadata_facts() -> None:
    metadata = {
        "status": "rejected",
        "evaluated_at": NOW,
        "cutoff": date(2026, 1, 1),
        "strategy_outputs": {
            "momentum": {"weights": {"SPY": 0.6}},
            "mean_reversion": {"weights": {"SPY": 0.4}},
        },
        "regime": {"name": "bull_low_vol"},
        "allocation": {"pre_risk_weights": {"SPY": 1.0}},
        "risk_actions": ({"control": "reject"},),
        "risk_decision": {
            "proposed_weights": {"SPY": 1.0},
            "final_weights": {"SPY": 0.5},
        },
        "final_target": {"SPY": 0.5},
        "error": "insufficient completed history",
    }

    class RejectingTargetProvider:
        last_metadata = metadata

        def __call__(self, now, account, positions):
            del now, account, positions
            raise ValueError("insufficient completed history")

    config = SimpleNamespace(
        project=SimpleNamespace(run_name="metadata-test"),
        data=SimpleNamespace(tickers=["SPY"]),
        market_data=SimpleNamespace(stale_after_seconds=180),
        schedule=SimpleNamespace(catch_up_cutoff_et="14:30"),
        execution=SimpleNamespace(
            paper_only=True,
            paper_order_submission_enabled=False,
            minimum_order_notional=25,
            max_single_order_fraction_of_equity=0.2,
            required_cash_buffer=0.02,
            maximum_orders_per_rebalance=20,
        ),
        risk=SimpleNamespace(daily_loss_limit=0.03, drawdown_hard_limit=0.15),
    )
    repository = AuditRepository(Database(":memory:"))
    broker = FakePaperBroker(now=NOW)
    provider = ReplayMarketDataProvider((_bar(NOW),))
    service = LiveService(
        config,
        repository=repository,
        broker=broker,
        market_data=provider,
        mode=RunMode.OBSERVE,
        clock=FakeClock(NOW),
        target_provider=RejectingTargetProvider(),
    )
    try:
        service.start_streams()
        result = service.run_once()
    finally:
        service.shutdown()

    assert result.status == "rejected"
    assert result.planning is None
    assert broker.submit_calls == 0
    assert repository.count("decision_receipts") == 1
    assert repository.count("strategy_signals") == 1
    assert repository.count("regime_states") == 1
    assert repository.count("allocation_results") == 1
    assert repository.count("risk_decisions") == 1
    persisted = repository.get_rebalance(result.decision_id)
    assert persisted is not None and persisted["status"] == "rejected"
    receipt = persisted["payload"]
    assert receipt["signal_cutoff"] == "2026-01-01"
    assert receipt["momentum"] == metadata["strategy_outputs"]["momentum"]
    assert receipt["mean_reversion"] == metadata["strategy_outputs"]["mean_reversion"]
    assert receipt["regime"] == metadata["regime"]
    assert receipt["allocation"] == metadata["allocation"]
    assert receipt["proposed_target"] == {"SPY": 1.0}
    assert receipt["final_target"] is None
    assert receipt["decision_metadata"]["final_target"] == {"SPY": 0.5}
    assert receipt["risk_actions"] == [{"control": "reject"}]
