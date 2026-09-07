"""Signed reconciliation, latch, restart, and forced-flat tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from adaptive_trader.platform.execution import (
    AccountState,
    BrokerOrder,
    BrokerUpdate,
    DeterministicFakePaperBroker,
    DiscrepancyCode,
    ExecutionPlanningResult,
    ExecutionService,
    ExecutionValidationError,
    FakeBrokerScenario,
    ForcedFlattenRequest,
    ForcedFlattenService,
    MemoryExecutionRepository,
    OrderIntent,
    OrderState,
    Position,
    ReconciliationReceipt,
    ReconciliationRequest,
    ReconciliationStatus,
    SubmissionSafetySnapshot,
    plan_signed_orders,
    reconcile,
    reconcile_and_persist,
    reconciliation_input_hash,
    reconstruct_signed_positions,
)
from adaptive_trader.platform.risk.latches import RiskLatchKind, RiskLatchState
from adaptive_trader.platform.risk.models import RiskExecutionScope
from adaptive_trader.platform.scheduling import (
    ClaimResult,
    ClaimStatus,
    DecisionSlot,
    DecisionType,
    SlotState,
)
from adaptive_trader.platform.storage.repositories import verify_audit_chain
from tests.unit.test_platform_execution_planner import planning_request

NOW = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
EXPERIMENT_HASH = "b" * 64
ACCOUNT_HASH = (
    DeterministicFakePaperBroker(initial_time=NOW).account(observed_at=NOW).account_id_hash
)


def _safety(
    symbols: tuple[str, ...],
    *,
    entry_disabled: bool = False,
    evaluated_at: datetime = NOW,
    source_observed_at: datetime | None = None,
) -> SubmissionSafetySnapshot:
    source_time = evaluated_at if source_observed_at is None else source_observed_at
    return SubmissionSafetySnapshot(
        evaluated_at=evaluated_at,
        session_open=True,
        data_complete=True,
        account_observed_at=evaluated_at,
        security_observed_at=source_time,
        reconciliation_observed_at=source_time,
        price_observed_at=source_time,
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
    slot_id: str = f"slot_{'1' * 64}",
) -> ReconciliationRequest:
    all_orders = repository.all_orders()
    plan_intents = (
        repository.intents_for_plan(execution_plan_id)
        if execution_plan_id is not None
        else repository.all_intents()
    )
    plan_client_ids = {intent.client_order_id for intent in plan_intents}
    orders = all_orders
    intents = tuple(
        intent
        for intent in repository.all_intents()
        if intent.client_order_id in {order.client_order_id for order in orders}
    )
    fills = tuple(
        fill
        for fill in repository.fills()
        if execution_plan_id is None or fill.client_order_id in plan_client_ids
    )
    return ReconciliationRequest(
        experiment_hash=EXPERIMENT_HASH,
        slot_id=slot_id,
        execution_plan_id=execution_plan_id,
        correlation_id=(
            repository.get_plan(execution_plan_id).correlation_id
            if execution_plan_id is not None
            else f"correlation_{'d' * 64}"
        ),
        active_symbols=symbols,
        short_eligible_symbols=symbols,
        baseline_positions=(
            repository.get_plan(execution_plan_id).current_positions
            if execution_plan_id is not None
            else ()
        ),
        baseline_cash=(
            repository.get_plan(execution_plan_id).equity
            - sum(
                (
                    position.quantity * dict(marks)[position.symbol]
                    for position in repository.get_plan(execution_plan_id).current_positions
                ),
                start=Decimal(0),
            )
            if execution_plan_id is not None
            else Decimal("1000.00")
        ),
        fills=fills,
        order_fills=tuple(
            fill
            for fill in repository.fills()
            if fill.client_order_id in {order.client_order_id for order in orders}
        ),
        intents=intents,
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


def _claimed_forced_flat_slot() -> DecisionSlot:
    pending = DecisionSlot.create(
        experiment_id="execution_test",
        experiment_version=1,
        experiment_hash=EXPERIMENT_HASH,
        signal_provider_id="always_flat",
        signal_provider_version="1",
        session_date=NOW.date(),
        source_interval_start=NOW - timedelta(minutes=1),
        source_interval_end=NOW,
        ready_at=NOW,
        deadline_at=NOW + timedelta(minutes=1),
        required_completion_at=NOW + timedelta(minutes=2),
        decision_type=DecisionType.FORCED_FLAT,
        initial_state=SlotState.FLATTEN_REQUIRED,
    )
    return pending.evolve(
        state=SlotState.CLAIMED,
        claim_owner="forced_flat_worker",
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
        attempt_count=1,
        completed_at=None,
        reason_code=None,
    )


class _SlotLifecycle:
    def __init__(self, slot: DecisionSlot, events: list[str] | None = None) -> None:
        self.slot = slot
        self._events = events

    def claim(self, slot_id: str, *, owner: str, now: datetime) -> ClaimResult:
        if self._events is not None:
            self._events.append("claim")
        assert (slot_id, owner, now) == (
            self.slot.slot_id,
            self.slot.claim_owner,
            self.slot.claimed_at,
        )
        return ClaimResult(status=ClaimStatus.CLAIMED, slot=self.slot)

    def renew(self, slot_id: str, *, owner: str, now: datetime) -> DecisionSlot:
        if self._events is not None:
            self._events.append("renew")
        assert slot_id == self.slot.slot_id
        assert owner == self.slot.claim_owner
        self.slot = self.slot.evolve(
            state=SlotState.CLAIMED,
            claim_owner=owner,
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=30),
            attempt_count=self.slot.attempt_count,
            completed_at=None,
            reason_code=None,
        )
        return self.slot

    def complete(self, slot_id: str, *, owner: str, now: datetime) -> DecisionSlot:
        if self._events is not None:
            self._events.append("complete")
        assert slot_id == self.slot.slot_id
        assert owner == self.slot.claim_owner
        self.slot = self.slot.evolve(
            state=SlotState.COMPLETED,
            claim_owner=None,
            claimed_at=None,
            lease_expires_at=None,
            attempt_count=self.slot.attempt_count,
            completed_at=now,
            reason_code="decision_materialized",
        )
        return self.slot

    def fail(
        self,
        slot_id: str,
        *,
        owner: str,
        reason_code: str,
        now: datetime,
    ) -> DecisionSlot:
        if self._events is not None:
            self._events.append("fail")
        assert slot_id == self.slot.slot_id
        assert owner == self.slot.claim_owner
        self.slot = self.slot.evolve(
            state=SlotState.FAILED,
            claim_owner=None,
            claimed_at=None,
            lease_expires_at=None,
            attempt_count=self.slot.attempt_count,
            completed_at=now,
            reason_code=reason_code,
        )
        return self.slot


def _filled_portfolio(
    targets: tuple[tuple[str, Decimal], ...],
):
    symbols = tuple(symbol for symbol, _ in targets)
    current = tuple((symbol, Decimal(0)) for symbol in symbols)
    request = planning_request(current=current, target_weights=targets)
    result = plan_signed_orders(request)
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW, initial_cash=Decimal("1000"))
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


def test_reconciliation_rejects_account_observation_outside_its_window() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )

    with pytest.raises(
        ExecutionValidationError,
        match="broker account observation must occur during reconciliation",
    ):
        replace(
            request,
            broker_account=replace(
                request.broker_account,
                observed_at=request.started_at - timedelta(microseconds=1),
            ),
        )


def test_reconciliation_hashes_bind_flat_deadline_account_time_and_marks() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )
    required_flat = replace(
        request,
        require_flat=True,
        required_flat_at=NOW + timedelta(minutes=2),
    )
    variants = (
        request,
        required_flat,
        replace(required_flat, required_flat_at=NOW + timedelta(minutes=3)),
        replace(
            request,
            broker_account=replace(
                request.broker_account,
                observed_at=NOW - timedelta(microseconds=1),
            ),
        ),
        replace(request, mark_prices=(("AAA", Decimal("101")),)),
    )

    assert len({reconciliation_input_hash(item) for item in variants}) == len(variants)
    assert len({reconcile(item).content_hash for item in variants}) == len(variants)


def test_current_plan_baseline_excludes_prior_active_order_fills() -> None:
    opening = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal("0.10")),),
        )
    )
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(
        initial_time=NOW,
        initial_cash=Decimal("1000"),
        default_scenario=FakeBrokerScenario.PARTIAL_FILL,
    )
    broker.set_mark_prices((("AAA", Decimal("100")),))
    service = ExecutionService(repository=repository, broker=broker)
    service.submit_plan(opening, safety_provider=lambda _intent: _safety(("AAA",)))
    prior_order = repository.get_order(opening.intents[0].client_order_id)
    assert prior_order.state.value == "PARTIALLY_FILLED"

    current = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal("0.5")),),
            target_weights=(("AAA", Decimal("0.10")),),
        )
    )
    service.persist(current)
    request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=(("AAA", Decimal("100")),),
        execution_plan_id=current.plan.execution_plan_id,
        broker_orders=(prior_order,),
    )

    outcome = reconcile_and_persist(
        repository=repository,
        request=request,
        latch_state=RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
    )

    assert request.fills == ()
    assert request.order_fills == repository.fills()
    assert outcome.receipt.status is ReconciliationStatus.CLEAN
    assert outcome.receipt.expected_positions == (Position("AAA", Decimal("0.5")),)
    assert outcome.receipt.expected_cash == Decimal("950")


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
    assert verify_audit_chain(repository.audit_events()).event_count == 5


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
        initial_cash=Decimal("1000"),
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


def test_short_provenance_inventory_must_cover_the_full_expected_quantity() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("-0.10")),))
    base = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=result.plan.execution_plan_id,
    )
    larger_short = replace(
        base,
        baseline_positions=(Position("AAA", Decimal(-1)),),
        broker_positions=(Position("AAA", Decimal(-2)),),
    )

    receipt = reconcile(larger_short)

    assert DiscrepancyCode.UNTRACEABLE_SHORT_POSITION in {
        item.code for item in receipt.discrepancies
    }


def test_persisted_reconciliation_rejects_marks_that_differ_from_signed_plan() -> None:
    result, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    request = replace(
        _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=marks,
            execution_plan_id=result.plan.execution_plan_id,
        ),
        mark_prices=(("AAA", Decimal("101")),),
    )

    with pytest.raises(
        ExecutionValidationError,
        match="reconciliation request does not match its execution plan",
    ):
        reconcile_and_persist(
            repository=repository,
            request=request,
            latch_state=RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
        )

    assert repository.reconciliations() == ()


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
    opening, repository, broker, marks = _filled_portfolio(
        (("AAA", Decimal("0.10")), ("BBB", Decimal("-0.10")))
    )
    pre_completed_at = NOW + timedelta(seconds=2)
    pre_request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA", "BBB"),
        marks=marks,
        execution_plan_id=opening.plan.execution_plan_id,
        completed_at=pre_completed_at,
    )
    slot = _claimed_forced_flat_slot()
    forced_plan = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)), ("BBB", Decimal(-1))),
            target_weights=(("AAA", Decimal(0)), ("BBB", Decimal(0))),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
            slot_id=slot.slot_id,
            correlation_id=slot.correlation_id,
            buying_power=broker.account(observed_at=pre_completed_at).buying_power,
            created_at=pre_completed_at,
            deadline_at=slot.deadline_at,
        )
    )
    lifecycle = _SlotLifecycle(slot)
    request = ForcedFlattenRequest(
        slot_id=slot.slot_id,
        experiment_hash=EXPERIMENT_HASH,
        claim_owner="forced_flat_worker",
        attempted_at=NOW,
        claim_slot=lifecycle.claim,
        renew_slot=lifecycle.renew,
        complete_slot=lifecycle.complete,
        fail_slot=lifecycle.fail,
        latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
        pre_reconciliation=lambda: pre_request,
        planning_provider=lambda _receipt, _account, _positions: forced_plan,
        safety_provider=lambda _intent: _safety(
            ("AAA", "BBB"),
            entry_disabled=True,
            evaluated_at=NOW + timedelta(seconds=3),
            source_observed_at=pre_completed_at,
        ),
        final_reconciliation=lambda: _reconciliation_request(
            repository,
            broker,
            symbols=("AAA", "BBB"),
            marks=marks,
            execution_plan_id=forced_plan.plan.execution_plan_id,
            require_flat=True,
            slot_id=slot.slot_id,
            completed_at=NOW + timedelta(seconds=4),
        ),
    )

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(request)

    assert outcome.success
    assert outcome.slot.state is SlotState.COMPLETED
    assert broker.positions() == ()
    assert outcome.reconciliation.receipt.status is ReconciliationStatus.CLEAN
    assert all(item.state.value == "FILLED" for item in outcome.submissions)


def test_already_flat_path_engages_slot_control_before_planning() -> None:
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW, initial_cash=Decimal("1000"))
    marks = (("AAA", Decimal("100")),)
    broker.set_mark_prices(marks)
    baseline = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal(0)),),
        )
    )
    ExecutionService(repository=repository, broker=broker).persist(baseline)
    slot = _claimed_forced_flat_slot()
    events: list[str] = []
    lifecycle = _SlotLifecycle(slot, events)
    pre_completed_at = NOW + timedelta(seconds=2)
    planned: dict[str, ExecutionPlanningResult] = {}

    def pre_reconciliation() -> ReconciliationRequest:
        events.append("pre_reconciliation")
        return _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=marks,
            execution_plan_id=baseline.plan.execution_plan_id,
            completed_at=pre_completed_at,
        )

    def planning_provider(
        receipt: ReconciliationReceipt,
        account: AccountState,
        positions: tuple[Position, ...],
    ) -> ExecutionPlanningResult:
        events.append("planning")
        assert positions == ()
        result = plan_signed_orders(
            planning_request(
                current=(("AAA", Decimal(0)),),
                target_weights=(("AAA", Decimal(0)),),
                forced_flat=True,
                scope=RiskExecutionScope.RISK_REDUCING_ONLY,
                slot_id=slot.slot_id,
                correlation_id=slot.correlation_id,
                buying_power=account.buying_power,
                created_at=receipt.completed_at,
                deadline_at=slot.deadline_at,
            )
        )
        planned["result"] = result
        return result

    def final_reconciliation() -> ReconciliationRequest:
        events.append("final_reconciliation")
        return _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=marks,
            execution_plan_id=planned["result"].plan.execution_plan_id,
            require_flat=True,
            slot_id=slot.slot_id,
            completed_at=slot.required_completion_at,
        )

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(
        ForcedFlattenRequest(
            slot_id=slot.slot_id,
            experiment_hash=EXPERIMENT_HASH,
            claim_owner="forced_flat_worker",
            attempted_at=NOW,
            claim_slot=lifecycle.claim,
            renew_slot=lifecycle.renew,
            complete_slot=lifecycle.complete,
            fail_slot=lifecycle.fail,
            latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
            pre_reconciliation=pre_reconciliation,
            planning_provider=planning_provider,
            safety_provider=lambda _intent: pytest.fail(
                "already-flat plan must not submit an order"
            ),
            final_reconciliation=final_reconciliation,
        )
    )

    assert outcome.success
    assert outcome.submissions == ()
    assert lifecycle.slot.state is SlotState.COMPLETED
    assert events == [
        "claim",
        "renew",
        "pre_reconciliation",
        "renew",
        "planning",
        "renew",
        "final_reconciliation",
        "renew",
        "complete",
    ]
    assert lifecycle.slot.version == slot.version + 5


def test_flat_proof_after_required_time_fails_and_persists_incident() -> None:
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW, initial_cash=Decimal("1000"))
    marks = (("AAA", Decimal("100")),)
    broker.set_mark_prices(marks)
    baseline = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal(0)),),
        )
    )
    ExecutionService(repository=repository, broker=broker).persist(baseline)
    slot = _claimed_forced_flat_slot()
    lifecycle = _SlotLifecycle(slot)
    pre_completed_at = NOW + timedelta(seconds=2)
    forced_plan = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal(0)),),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
            slot_id=slot.slot_id,
            correlation_id=slot.correlation_id,
            buying_power=broker.account(observed_at=pre_completed_at).buying_power,
            created_at=pre_completed_at,
            deadline_at=slot.deadline_at,
        )
    )

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(
        ForcedFlattenRequest(
            slot_id=slot.slot_id,
            experiment_hash=EXPERIMENT_HASH,
            claim_owner="forced_flat_worker",
            attempted_at=NOW,
            claim_slot=lifecycle.claim,
            renew_slot=lifecycle.renew,
            complete_slot=lifecycle.complete,
            fail_slot=lifecycle.fail,
            latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
            pre_reconciliation=lambda: _reconciliation_request(
                repository,
                broker,
                symbols=("AAA",),
                marks=marks,
                execution_plan_id=baseline.plan.execution_plan_id,
                completed_at=pre_completed_at,
            ),
            planning_provider=lambda _receipt, _account, _positions: forced_plan,
            safety_provider=lambda _intent: pytest.fail(
                "already-flat plan must not submit an order"
            ),
            final_reconciliation=lambda: _reconciliation_request(
                repository,
                broker,
                symbols=("AAA",),
                marks=marks,
                execution_plan_id=forced_plan.plan.execution_plan_id,
                require_flat=True,
                slot_id=slot.slot_id,
                completed_at=slot.required_completion_at + timedelta(microseconds=1),
            ),
        )
    )

    assert not outcome.success
    assert outcome.slot.state is SlotState.FAILED
    assert outcome.reconciliation.incident is not None
    assert DiscrepancyCode.FORCED_FLAT_DEADLINE_MISSED in {
        item.code for item in outcome.reconciliation.receipt.discrepancies
    }
    assert repository.incidents() == (outcome.reconciliation.incident,)


def test_forced_flat_partial_fill_creates_incident_instead_of_success() -> None:
    opening, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    pre_completed_at = NOW + timedelta(seconds=2)
    pre_request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=opening.plan.execution_plan_id,
        completed_at=pre_completed_at,
    )
    slot = _claimed_forced_flat_slot()
    forced_plan = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)),),
            target_weights=(("AAA", Decimal(0)),),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
            slot_id=slot.slot_id,
            correlation_id=slot.correlation_id,
            buying_power=broker.account(observed_at=pre_completed_at).buying_power,
            created_at=pre_completed_at,
            deadline_at=slot.deadline_at,
        )
    )
    broker.set_scenario(
        forced_plan.intents[0].client_order_id,
        FakeBrokerScenario.PARTIAL_FILL,
    )
    lifecycle = _SlotLifecycle(slot)
    request = ForcedFlattenRequest(
        slot_id=slot.slot_id,
        experiment_hash=EXPERIMENT_HASH,
        claim_owner="forced_flat_worker",
        attempted_at=NOW,
        claim_slot=lifecycle.claim,
        renew_slot=lifecycle.renew,
        complete_slot=lifecycle.complete,
        fail_slot=lifecycle.fail,
        latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
        pre_reconciliation=lambda: pre_request,
        planning_provider=lambda _receipt, _account, _positions: forced_plan,
        safety_provider=lambda _intent: _safety(
            ("AAA",),
            entry_disabled=True,
            evaluated_at=NOW + timedelta(seconds=3),
            source_observed_at=pre_completed_at,
        ),
        final_reconciliation=lambda: _reconciliation_request(
            repository,
            broker,
            symbols=("AAA",),
            marks=marks,
            execution_plan_id=forced_plan.plan.execution_plan_id,
            require_flat=True,
            slot_id=slot.slot_id,
            completed_at=NOW + timedelta(seconds=4),
        ),
    )

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(request)

    assert not outcome.success
    assert outcome.slot.state is SlotState.FAILED
    assert outcome.reconciliation.incident is not None
    assert DiscrepancyCode.REQUIRED_FLAT_NOT_PROVEN in {
        item.code for item in outcome.reconciliation.receipt.discrepancies
    }
    assert repository.incidents()


def test_forced_flat_submission_is_denied_at_exact_submission_cutoff() -> None:
    opening, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    pre_completed_at = NOW + timedelta(seconds=2)
    slot = _claimed_forced_flat_slot()
    forced_plan = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)),),
            target_weights=(("AAA", Decimal(0)),),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
            slot_id=slot.slot_id,
            correlation_id=slot.correlation_id,
            buying_power=broker.account(observed_at=pre_completed_at).buying_power,
            created_at=pre_completed_at,
            deadline_at=slot.deadline_at,
        )
    )
    lifecycle = _SlotLifecycle(slot)

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(
        ForcedFlattenRequest(
            slot_id=slot.slot_id,
            experiment_hash=EXPERIMENT_HASH,
            claim_owner="forced_flat_worker",
            attempted_at=NOW,
            claim_slot=lifecycle.claim,
            renew_slot=lifecycle.renew,
            complete_slot=lifecycle.complete,
            fail_slot=lifecycle.fail,
            latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
            pre_reconciliation=lambda: _reconciliation_request(
                repository,
                broker,
                symbols=("AAA",),
                marks=marks,
                execution_plan_id=opening.plan.execution_plan_id,
                completed_at=pre_completed_at,
            ),
            planning_provider=lambda _receipt, _account, _positions: forced_plan,
            safety_provider=lambda _intent: _safety(
                ("AAA",),
                entry_disabled=True,
                evaluated_at=slot.deadline_at,
                source_observed_at=pre_completed_at,
            ),
            final_reconciliation=lambda: _reconciliation_request(
                repository,
                broker,
                symbols=("AAA",),
                marks=marks,
                execution_plan_id=forced_plan.plan.execution_plan_id,
                require_flat=True,
                slot_id=slot.slot_id,
                completed_at=slot.deadline_at + timedelta(seconds=1),
            ),
        )
    )

    assert not outcome.success
    assert outcome.submissions[0].submitted is False
    assert outcome.submissions[0].reason_codes == ("intent_expired",)
    assert repository.get_order(forced_plan.intents[0].client_order_id).state is (
        OrderState.INTENT_COMMITTED
    )
    assert broker.positions() == (Position("AAA", Decimal(1)),)
    assert outcome.reconciliation.incident is not None


def test_post_claim_failure_persists_incident_and_fails_owned_slot() -> None:
    opening, repository, broker, marks = _filled_portfolio((("AAA", Decimal("0.10")),))
    pre_completed_at = NOW + timedelta(seconds=2)
    pre_request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=marks,
        execution_plan_id=opening.plan.execution_plan_id,
        completed_at=pre_completed_at,
    )
    slot = _claimed_forced_flat_slot()
    lifecycle = _SlotLifecycle(slot)

    def fail_planning(
        _receipt: ReconciliationReceipt,
        _account: AccountState,
        _positions: tuple[Position, ...],
    ) -> ExecutionPlanningResult:
        raise RuntimeError("bounded planning failure")

    with pytest.raises(RuntimeError, match="bounded planning failure"):
        ForcedFlattenService(repository=repository, broker=broker).run(
            ForcedFlattenRequest(
                slot_id=slot.slot_id,
                experiment_hash=EXPERIMENT_HASH,
                claim_owner="forced_flat_worker",
                attempted_at=NOW,
                claim_slot=lifecycle.claim,
                renew_slot=lifecycle.renew,
                complete_slot=lifecycle.complete,
                fail_slot=lifecycle.fail,
                latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
                pre_reconciliation=lambda: pre_request,
                planning_provider=fail_planning,
                safety_provider=lambda _intent: _safety(("AAA",), entry_disabled=True),
                final_reconciliation=lambda: pytest.fail(
                    "final reconciliation must not run after planning failure"
                ),
            )
        )

    assert lifecycle.slot.state is SlotState.FAILED
    assert lifecycle.slot.reason_code == "forced_flat_lifecycle_failed"
    assert len(repository.incidents()) == 1
    assert repository.incidents()[0].reason_code == "forced_flat_planning_failed"
    assert repository.audit_events()[-1].event_type == "incident.opened"


def test_forced_flat_orders_cancel_reconcile_refresh_plan_submit_and_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW, initial_cash=Decimal("1000"))
    marks = (("AAA", Decimal("100")), ("BBB", Decimal("100")))
    broker.set_mark_prices(marks)
    execution = ExecutionService(repository=repository, broker=broker)

    filled = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)), ("BBB", Decimal(0))),
            target_weights=(("AAA", Decimal("0.10")), ("BBB", Decimal(0))),
        )
    )
    execution.submit_plan(filled, safety_provider=lambda _intent: _safety(("AAA", "BBB")))
    pending = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)), ("BBB", Decimal(0))),
            target_weights=(("AAA", Decimal("0.10")), ("BBB", Decimal("0.10"))),
        )
    )
    broker.set_scenario(pending.intents[0].client_order_id, FakeBrokerScenario.DELAYED_UPDATE)
    pending_submission = execution.submit_plan(
        pending,
        safety_provider=lambda _intent: _safety(("AAA", "BBB")),
    )
    assert pending_submission[0].state is OrderState.PENDING

    slot = _claimed_forced_flat_slot()
    pre_completed_at = NOW + timedelta(seconds=2)
    events: list[str] = []
    recording_enabled = True
    original_submit = broker.submit
    original_cancel = broker.cancel
    original_account = broker.account
    original_positions = broker.positions
    original_open_orders = broker.open_client_order_ids

    def recorded_submit(intent: OrderIntent, *, submitted_at: datetime) -> BrokerUpdate:
        if recording_enabled:
            events.append("submit")
        return original_submit(intent, submitted_at=submitted_at)

    def recorded_cancel(client_order_id: str, *, canceled_at: datetime) -> BrokerUpdate:
        if recording_enabled:
            events.append("cancel")
        return original_cancel(client_order_id, canceled_at=canceled_at)

    def recorded_account(*, observed_at: datetime) -> AccountState:
        if recording_enabled:
            events.append("account")
        return original_account(observed_at=observed_at)

    def recorded_positions() -> tuple[Position, ...]:
        if recording_enabled:
            events.append("positions")
        return original_positions()

    def recorded_open_orders() -> tuple[str, ...]:
        if recording_enabled:
            events.append("open_orders")
        return original_open_orders()

    monkeypatch.setattr(broker, "submit", recorded_submit)
    monkeypatch.setattr(broker, "cancel", recorded_cancel)
    monkeypatch.setattr(broker, "account", recorded_account)
    monkeypatch.setattr(broker, "positions", recorded_positions)
    monkeypatch.setattr(broker, "open_client_order_ids", recorded_open_orders)
    planned: dict[str, ExecutionPlanningResult] = {}
    lifecycle = _SlotLifecycle(slot, events)

    def pre_reconciliation() -> ReconciliationRequest:
        nonlocal recording_enabled
        events.append("pre_reconciliation")
        recording_enabled = False
        try:
            return _reconciliation_request(
                repository,
                broker,
                symbols=("AAA", "BBB"),
                marks=marks,
                execution_plan_id=pending.plan.execution_plan_id,
                completed_at=pre_completed_at,
            )
        finally:
            recording_enabled = True

    def planning_provider(
        receipt: ReconciliationReceipt,
        account: AccountState,
        positions: tuple[Position, ...],
    ) -> ExecutionPlanningResult:
        events.append("plan")
        assert receipt.status is ReconciliationStatus.CLEAN
        assert positions == (Position("AAA", Decimal(1)),)
        result = plan_signed_orders(
            planning_request(
                current=(("AAA", Decimal(1)), ("BBB", Decimal(0))),
                target_weights=(("AAA", Decimal(0)), ("BBB", Decimal(0))),
                forced_flat=True,
                scope=RiskExecutionScope.RISK_REDUCING_ONLY,
                slot_id=slot.slot_id,
                correlation_id=slot.correlation_id,
                buying_power=account.buying_power,
                created_at=receipt.completed_at,
                deadline_at=slot.deadline_at,
            )
        )
        planned["result"] = result
        return result

    def safety_provider(_intent: OrderIntent) -> SubmissionSafetySnapshot:
        events.append("safety")
        return _safety(
            ("AAA", "BBB"),
            entry_disabled=True,
            evaluated_at=NOW + timedelta(seconds=3),
            source_observed_at=pre_completed_at,
        )

    def final_reconciliation() -> ReconciliationRequest:
        nonlocal recording_enabled
        events.append("final_reconciliation")
        result = planned["result"]
        recording_enabled = False
        try:
            return _reconciliation_request(
                repository,
                broker,
                symbols=("AAA", "BBB"),
                marks=marks,
                execution_plan_id=result.plan.execution_plan_id,
                require_flat=True,
                slot_id=slot.slot_id,
                completed_at=NOW + timedelta(seconds=4),
            )
        finally:
            recording_enabled = True

    outcome = ForcedFlattenService(repository=repository, broker=broker).run(
        ForcedFlattenRequest(
            slot_id=slot.slot_id,
            experiment_hash=EXPERIMENT_HASH,
            claim_owner="forced_flat_worker",
            attempted_at=NOW,
            claim_slot=lifecycle.claim,
            renew_slot=lifecycle.renew,
            complete_slot=lifecycle.complete,
            fail_slot=lifecycle.fail,
            latch_state_provider=lambda: RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
            pre_reconciliation=pre_reconciliation,
            planning_provider=planning_provider,
            safety_provider=safety_provider,
            final_reconciliation=final_reconciliation,
        )
    )

    assert outcome.success
    assert outcome.canceled_client_order_ids == (pending.intents[0].client_order_id,)
    assert events == [
        "claim",
        "renew",
        "cancel",
        "pre_reconciliation",
        "renew",
        "account",
        "positions",
        "plan",
        "renew",
        "safety",
        "renew",
        "account",
        "positions",
        "open_orders",
        "submit",
        "final_reconciliation",
        "renew",
        "complete",
    ]


def test_required_flat_reconciliation_blocks_nonterminal_opening_order() -> None:
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(
        initial_time=NOW,
        initial_cash=Decimal("1000"),
        default_scenario=FakeBrokerScenario.DELAYED_UPDATE,
    )
    broker.set_mark_prices((("AAA", Decimal("100")),))
    opening = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal("0.10")),),
        )
    )
    ExecutionService(repository=repository, broker=broker).submit_plan(
        opening,
        safety_provider=lambda _intent: _safety(("AAA",)),
    )
    request = _reconciliation_request(
        repository,
        broker,
        symbols=("AAA",),
        marks=(("AAA", Decimal("100")),),
        execution_plan_id=opening.plan.execution_plan_id,
        require_flat=True,
    )

    outcome = reconcile_and_persist(
        repository=repository,
        request=request,
        latch_state=RiskLatchState.empty(experiment_hash=EXPERIMENT_HASH),
    )

    assert outcome.receipt.observed_positions == ()
    assert outcome.receipt.status is ReconciliationStatus.BLOCKING
    assert outcome.incident is not None
    assert DiscrepancyCode.REQUIRED_FLAT_NOT_PROVEN in {
        item.code for item in outcome.receipt.discrepancies
    }
