"""Signed reconciliation, latch, restart, and forced-flat tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from adaptive_trader.platform.execution import (
    BrokerOrder,
    DeterministicFakePaperBroker,
    DiscrepancyCode,
    ExecutionService,
    FakeBrokerScenario,
    ForcedFlattenRequest,
    ForcedFlattenService,
    MemoryExecutionRepository,
    Position,
    ReconciliationRequest,
    ReconciliationStatus,
    SubmissionSafetySnapshot,
    plan_signed_orders,
    reconcile,
    reconcile_and_persist,
    reconstruct_signed_positions,
)
from adaptive_trader.platform.risk.latches import RiskLatchKind, RiskLatchState
from adaptive_trader.platform.risk.models import RiskExecutionScope
from adaptive_trader.platform.storage.repositories import verify_audit_chain
from tests.unit.test_platform_execution_planner import planning_request

NOW = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
EXPERIMENT_HASH = "b" * 64
ACCOUNT_HASH = (
    DeterministicFakePaperBroker(initial_time=NOW).account(observed_at=NOW).account_id_hash
)


def _safety(symbols: tuple[str, ...], *, entry_disabled: bool = False):
    return SubmissionSafetySnapshot(
        evaluated_at=NOW,
        session_open=True,
        data_complete=True,
        account_observed_at=NOW,
        security_observed_at=NOW,
        reconciliation_observed_at=NOW,
        price_observed_at=NOW,
        reconciliation_clean=True,
        ambiguous_order_exists=False,
        blocking_latch_exists=False,
        entry_disabled=entry_disabled,
        active_symbols=symbols,
        shortable_symbols=symbols,
    )


def _reconciliation_request(
    repository: MemoryExecutionRepository,
    broker: DeterministicFakePaperBroker,
    *,
    symbols: tuple[str, ...],
    marks: tuple[tuple[str, Decimal], ...],
    execution_plan_id: str | None,
    broker_positions: tuple[Position, ...] | None = None,
    broker_orders: tuple[BrokerOrder, ...] | None = None,
    require_flat: bool = False,
    completed_at: datetime = NOW,
) -> ReconciliationRequest:
    orders = repository.all_orders()
    return ReconciliationRequest(
        experiment_hash=EXPERIMENT_HASH,
        slot_id=f"slot_{'1' * 64}",
        execution_plan_id=execution_plan_id,
        correlation_id=f"correlation_{'d' * 64}",
        active_symbols=symbols,
        short_eligible_symbols=symbols,
        baseline_positions=(),
        baseline_cash=Decimal("100000.00"),
        fills=repository.fills(),
        intents=repository.all_intents(),
        durable_orders=orders,
        broker_orders=orders if broker_orders is None else broker_orders,
        broker_positions=broker.positions() if broker_positions is None else broker_positions,
        broker_account=broker.account(observed_at=completed_at),
        expected_account_id_hash=ACCOUNT_HASH,
        mark_prices=marks,
        started_at=completed_at - timedelta(seconds=1),
        completed_at=completed_at,
        require_flat=require_flat,
        required_flat_at=NOW + timedelta(minutes=2) if require_flat else None,
    )


def _filled_portfolio(
    targets: tuple[tuple[str, Decimal], ...],
):
    symbols = tuple(symbol for symbol, _ in targets)
    current = tuple((symbol, Decimal(0)) for symbol in symbols)
    request = planning_request(current=current, target_weights=targets)
    result = plan_signed_orders(request)
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW)
    marks = tuple((symbol, Decimal("100")) for symbol in symbols)
    broker.set_mark_prices(marks)
    service = ExecutionService(repository=repository, broker=broker)
    outcomes = service.submit_plan(result, safety_provider=lambda _intent: _safety(symbols))
    assert all(outcome.state.value == "FILLED" for outcome in outcomes)
    return result, repository, broker, marks


def test_buy_and_sell_fills_reconstruct_signed_positions_idempotently() -> None:
    result, repository, _broker, _marks = _filled_portfolio(
        (("AAA", Decimal("0.10")), ("BBB", Decimal("-0.10")))
    )
    fills = repository.fills()

    reconstructed = reconstruct_signed_positions(
        baseline_positions=(),
        fills=(*fills, fills[0]),
    )

    assert reconstructed == (Position("AAA", Decimal(1)), Position("BBB", Decimal(-1)))
    assert result.plan.target_quantities == reconstructed


def test_clean_reconciliation_binds_fills_orders_cash_and_equity() -> None:
    result, repository, broker, marks = _filled_portfolio(
        (("AAA", Decimal("0.10")), ("BBB", Decimal("-0.10")))
    )
    request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA", "BBB"),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )

    receipt = reconcile(request)

    assert receipt.status is ReconciliationStatus.CLEAN
    assert receipt.discrepancies == ()
    assert receipt.expected_positions == broker.positions()
    assert receipt.expected_cash == receipt.observed_cash
    assert receipt.expected_equity == receipt.observed_equity


def test_position_mismatch_engages_append_only_latch_and_incident() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
        broker_positions=(Position("AAA", Decimal("2")),),
    )
    latch_state = RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH)

    outcome = reconcile_and_persist(
        repository=repository,
        request=request,
        latch_state=latch_state,
    )

    assert outcome.receipt.status is ReconciliationStatus.BLOCKING
    assert DiscrepancyCode.POSITION_QUANTITY_MISMATCH in {
        item.code for item in outcome.receipt.discrepancies
    }
    assert outcome.latch_event is not None
    assert outcome.latch_event.latch_type is RiskLatchKind.RECONCILIATION
    assert outcome.incident is not None
    assert len(repository.latch_events()) == 1
    assert len(repository.incidents()) == 1
    assert verify_audit_chain(repository.audit_events()).event_count == 4


def test_submission_unknown_is_a_blocking_reconciliation_discrepancy() -> None:
    result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal("0.10")),),
        )
    )
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(
        initial_time=NOW,
        default_scenario=FakeBrokerScenario.TIMEOUT_BEFORE_ACCEPTANCE,
    )
    broker.set_mark_prices((("AAA", Decimal("100")),))
    service = ExecutionService(repository=repository, broker=broker)
    service.persist(result)
    service.submit_one(result.intents[0].client_order_id, safety=_safety(("AAA",)))

    receipt = reconcile(
        _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=(("AAA", Decimal("100")),),
            execution_plan_id=result.plan.execution_plan_id,
            broker_orders=(),
        )
    )

    codes = {item.code for item in receipt.discrepancies}
    assert DiscrepancyCode.SUBMISSION_UNKNOWN in codes
    assert DiscrepancyCode.MISSING_BROKER_ORDER in codes


def test_duplicate_execution_id_is_critical_even_when_fill_is_identical() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    fill = repository.fills()[0]
    base = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )
    duplicate_request = replace(base, fills=(*base.fills, fill))

    receipt = reconcile(duplicate_request)

    discrepancy = next(
        item
        for item in receipt.discrepancies
        if item.code is DiscrepancyCode.DUPLICATE_EXECUTION_ID
    )
    assert discrepancy.code.severity.value == "CRITICAL"


def test_negative_position_requires_complete_intent_order_fill_chain() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("-0.10")),))
    base = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )
    missing_local_order = replace(base, durable_orders=())

    receipt = reconcile(missing_local_order)

    assert DiscrepancyCode.UNTRACEABLE_SHORT_POSITION in {
        item.code for item in receipt.discrepancies
    }


def test_persisted_fill_total_must_match_durable_order_projection() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    base = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )
    stale_projection = BrokerOrder.committed(result.intents[0])
    stale_request = replace(base, durable_orders=(stale_projection,))

    receipt = reconcile(stale_request)

    assert DiscrepancyCode.FILLED_QUANTITY_MISMATCH in {item.code for item in receipt.discrepancies}


def test_forced_flat_from_long_and_short_proves_zero_before_success() -> None:
    _opening, repository, broker, marks = _filled_portfolio(
        (("AAA", Decimal("0.10")), ("BBB", Decimal("-0.10")))
    )
    pre_request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA", "BBB"),
        marks=marks,
        execution_plan_id=None,
    )
    pre = reconcile(pre_request)
    forced_plan = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)), ("BBB", Decimal(-1))),
            target_weights=(("AAA", Decimal(0)), ("BBB", Decimal(0))),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
        )
    )
    request = ForcedFlattenRequest(
        planning_result=forced_plan,
        pre_reconciliation=pre,
        latch_state=RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
        conflicting_opening_order_ids=(),
        target_at=NOW,
        submission_cutoff_at=NOW + timedelta(minutes=1),
        required_flat_at=NOW + timedelta(minutes=2),
        attempted_at=NOW,
        safety_provider=lambda _intent: _safety(("AAA", "BBB"), entry_disabled=True),
        final_reconciliation=lambda: _reconciliation_request(
            repository,
            broker,
            symbols=("AAA", "BBB"),
            marks=marks,
            execution_plan_id=forced_plan.plan.execution_plan_id,
            require_flat=True,
        ),
    )

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(request)

    assert outcome.success
    assert broker.positions() == ()
    assert outcome.reconciliation.receipt.status is ReconciliationStatus.CLEAN
    assert all(item.state.value == "FILLED" for item in outcome.submissions)


def test_forced_flat_partial_fill_creates_incident_instead_of_success() -> None:
    _opening, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    pre = reconcile(
        _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=marks,
            execution_plan_id=None,
        )
    )
    forced_plan = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)),),
            target_weights=(("AAA", Decimal(0)),),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
        )
    )
    broker.set_scenario(
        forced_plan.intents[0].client_order_id,
        FakeBrokerScenario.PARTIAL_FILL,
    )
    request = ForcedFlattenRequest(
        planning_result=forced_plan,
        pre_reconciliation=pre,
        latch_state=RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
        conflicting_opening_order_ids=(),
        target_at=NOW,
        submission_cutoff_at=NOW + timedelta(minutes=1),
        required_flat_at=NOW + timedelta(minutes=2),
        attempted_at=NOW,
        safety_provider=lambda _intent: _safety(("AAA",), entry_disabled=True),
        final_reconciliation=lambda: _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=marks,
            execution_plan_id=forced_plan.plan.execution_plan_id,
            require_flat=True,
        ),
    )

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(request)

    assert not outcome.success
    assert outcome.reconciliation.incident is not None
    assert DiscrepancyCode.REQUIRED_FLAT_NOT_PROVEN in {
        item.code for item in outcome.reconciliation.receipt.discrepancies
    }
    assert repository.incidents()
