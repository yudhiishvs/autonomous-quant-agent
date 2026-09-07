"""Operational database outages and broker uncertainty preserve durable retry boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from functools import partial

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from adaptive_trader.platform.execution import (
    ExecutionValidationError,
    FakeBrokerScenario,
    Incident,
    OrderState,
    Position,
)
from adaptive_trader.platform.storage.execution import (
    ExecutionPersistenceError,
    SignedExecutionRepository,
)
from adaptive_trader.platform.storage.repositories import AuditPersistenceError
from adaptive_trader.platform.storage.tables import aqa_audit_events
from tests.unit.test_platform_execution_broker import NOW, _persisted_service, _safety
from tests.unit.test_platform_execution_evidence_boundaries import _submitted
from tests.unit.test_platform_execution_persistence import signed_plan, sqlite_engine

__all__ = ["signed_plan", "sqlite_engine"]


@pytest.fixture
def persisted(sqlite_engine, signed_plan):
    repo = SignedExecutionRepository(sqlite_engine)
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    return repo


def _read(repo, plan, operation):
    arguments = {
        "get_plan": (plan.plan.execution_plan_id,),
        "get_intent": (plan.intents[0].client_order_id,),
        "get_order": (plan.intents[0].client_order_id,),
        "get_risk_decision": (plan.risk_decision.risk_decision_id,),
        "intents_for_plan": (plan.plan.execution_plan_id,),
        "fills_for_order": (plan.intents[0].client_order_id,),
        "submission_ledger_snapshot": (plan.intents[0].client_order_id,),
    }
    return getattr(repo, operation)(*arguments.get(operation, ()))


@pytest.mark.parametrize(
    "operation",
    [
        "get_plan",
        "get_intent",
        "get_order",
        "get_risk_decision",
        "intents_for_plan",
        "all_intents",
        "all_orders",
        "order_events",
        "fills",
        "fills_for_order",
        "submission_ledger_snapshot",
        "reconciliations",
        "incidents",
        "has_ambiguous_order",
    ],
)
def test_sql_read_outage_is_sanitized_and_recovery_returns_same_authority(
    persisted, sqlite_engine, signed_plan, operation
):
    expected = _read(persisted, signed_plan, operation)

    def unavailable(*args):
        raise OperationalError("private SQL", {}, RuntimeError("private password"))

    event.listen(sqlite_engine, "before_cursor_execute", unavailable)
    try:
        with pytest.raises(ExecutionPersistenceError) as error:
            _read(persisted, signed_plan, operation)
        assert "private" not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", unavailable)
    assert _read(SignedExecutionRepository(sqlite_engine), signed_plan, operation) == expected


@pytest.mark.parametrize("operation", ["plan", "submission", "fill", "incident"])
@pytest.mark.parametrize("failure", ["audit", "database"])
def test_write_failure_rolls_back_whole_boundary_before_valid_retry(
    sqlite_engine, signed_plan, monkeypatch, operation, failure
):
    from adaptive_trader.platform.execution import DeterministicFakePaperBroker
    from tests.unit.test_platform_execution_persistence import _fake_submission_authority

    repo = SignedExecutionRepository(sqlite_engine)
    broker = DeterministicFakePaperBroker(initial_time=NOW)
    broker.set_mark_prices((("AAA", Decimal(100)),))
    intent = signed_plan.intents[0]

    def persist():
        repo.persist_plan_and_intents(
            signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
        )

    if operation != "plan":
        persist()
    if operation == "plan":
        action = persist
    elif operation == "submission":
        authority = _fake_submission_authority(repo, broker, intent.client_order_id)
        action = partial(
            repo.record_submission_started,
            intent.client_order_id,
            started_at=NOW,
            authority=authority,
        )
    elif operation == "fill":
        update = _submitted(repo, signed_plan)
        action = partial(repo.apply_broker_update, update)
    else:
        incident = Incident.create(
            idempotency_key=f"flatten_failure_{'9' * 64}",
            experiment_hash=signed_plan.plan.experiment_hash,
            correlation_id=signed_plan.plan.correlation_id,
            reason_code="forced_flat_planning_failed",
            opened_at=NOW,
        )
        action = partial(repo.record_incident, incident)

    def snapshot():
        with sqlite_engine.connect() as connection:
            return (
                repo.all_orders(),
                repo.order_events(),
                repo.fills(),
                repo.incidents(),
                tuple(connection.execute(select(aqa_audit_events)).all()),
            )

    before = snapshot()

    def fail(*args, **kwargs):
        if failure == "audit":
            raise AuditPersistenceError("private password")
        raise OperationalError("private SQL", {}, RuntimeError("private password"))

    with monkeypatch.context() as patch:
        patch.setattr(repo, "_append_audit", fail)
        with pytest.raises(ExecutionPersistenceError) as error:
            action()
        assert "private" not in str(error.value)
    assert snapshot() == before
    action()
    after = snapshot()
    assert after != before
    if operation != "submission":
        action()
        assert snapshot() == after


@pytest.mark.parametrize(
    "change,reason",
    [
        (dict(account_id="different-account"), "account_id_mismatch"),
        (dict(cash=Decimal("999")), "account_cash_changed"),
        (dict(equity=Decimal("999")), "account_equity_changed"),
        (dict(observed_at=NOW + timedelta(seconds=1)), "account_snapshot_mismatch"),
    ],
)
def test_fresh_broker_account_must_match_exact_signed_authority(monkeypatch, change, reason):
    plan, repo, broker, service = _persisted_service()
    account = broker.account(observed_at=NOW)
    monkeypatch.setattr(broker, "account", lambda **_: replace(account, **change))
    result = service.submit_one(plan.intents[0].client_order_id, safety=_safety())
    assert not result.submitted and reason in result.reason_codes
    assert repo.get_order(plan.intents[0].client_order_id).state is OrderState.INTENT_COMMITTED
    assert repo.order_events() == ()


@pytest.mark.parametrize(
    "boundary,value",
    [
        ("positions", [Position("AAA", Decimal(1))]),
        ("positions", (Position("AAA", Decimal(1)), Position("AAA", Decimal(2)))),
        ("open_client_order_ids", ["unknown"]),
        ("open_client_order_ids", ("same", "same")),
    ],
)
def test_malformed_broker_snapshots_cannot_supply_submission_authority(
    monkeypatch, boundary, value
):
    plan, repo, broker, service = _persisted_service()
    monkeypatch.setattr(broker, boundary, lambda: value)
    result = service.submit_one(plan.intents[0].client_order_id, safety=_safety())
    assert not result.submitted and result.reason_codes == ("broker_state_unavailable",)
    assert repo.order_events() == ()


def test_unexpected_submit_exception_is_durable_ambiguity_and_never_resubmitted(monkeypatch):
    plan, repo, broker, service = _persisted_service()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private provider diagnostic")

    monkeypatch.setattr(broker, "submit", fail)
    first = service.submit_one(plan.intents[0].client_order_id, safety=_safety())
    second = service.submit_one(plan.intents[0].client_order_id, safety=_safety())
    assert first.state is OrderState.SUBMISSION_UNKNOWN and first.submitted
    assert not second.submitted and second.reason_codes == ("reconciliation_required",)
    assert calls == [1]
    assert (
        repo.get_order(plan.intents[0].client_order_id).safe_error_code
        == "broker_submission_exception"
    )


def test_stale_claim_race_stops_before_broker_submission(monkeypatch):
    plan, repo, broker, service = _persisted_service()

    def stale(*args, **kwargs):
        raise ExecutionValidationError("submission authority became stale")

    monkeypatch.setattr(repo, "record_submission_started", stale)
    result = service.submit_one(plan.intents[0].client_order_id, safety=_safety())
    assert result.reason_codes == ("submission_authority_stale",) and not result.submitted
    assert broker.open_client_order_ids() == () and repo.order_events() == ()


@pytest.mark.parametrize("operation", ["refresh_missing", "cancel_unknown"])
def test_known_order_disappearance_and_cancel_uncertainty_require_reconciliation(
    monkeypatch, operation
):
    plan, repo, broker, service = _persisted_service(scenario=FakeBrokerScenario.DELAYED_UPDATE)
    client = plan.intents[0].client_order_id
    service.submit_one(client, safety=_safety())
    if operation == "refresh_missing":
        monkeypatch.setattr(broker, "lookup", lambda *a, **k: None)
        result = service.refresh_nonterminal(client, observed_at=NOW + timedelta(seconds=1))
    else:

        def fail(*args, **kwargs):
            assert repo.get_order(client).state is OrderState.CANCEL_REQUESTED
            raise TimeoutError("private cancel diagnostic")

        monkeypatch.setattr(broker, "cancel", fail)
        result = service.cancel_one(client, requested_at=NOW + timedelta(seconds=1))
    assert result.state is OrderState.RECONCILIATION_REQUIRED
    assert service.submit_one(client, safety=_safety()).reason_codes == ("reconciliation_required",)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("state", ["committed", "unknown", "filled"])
@pytest.mark.parametrize("transition", ["cancel", "unknown", "reconcile"])
def test_recovery_transitions_obey_durable_state_and_idempotency(
    sqlite_engine, signed_plan, backend, state, transition
):
    from adaptive_trader.platform.execution import MemoryExecutionRepository

    repo = (
        MemoryExecutionRepository()
        if backend == "memory"
        else SignedExecutionRepository(sqlite_engine)
    )
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    client = signed_plan.intents[0].client_order_id
    if state != "committed":
        update = _submitted(repo, signed_plan)
        if state == "filled":
            repo.apply_broker_update(update)
        else:
            repo.record_submission_unknown(client, observed_at=NOW, reason_code="broker_timeout")
    actions = {
        "cancel": partial(repo.record_cancel_requested, client, requested_at=NOW),
        "unknown": partial(
            repo.record_submission_unknown, client, observed_at=NOW, reason_code="broker_timeout"
        ),
        "reconcile": partial(
            repo.record_reconciliation_required,
            client,
            observed_at=NOW,
            reason_code="broker_order_not_found",
        ),
    }
    allowed = state == "unknown" and transition in {"unknown", "reconcile"}
    before = repo.all_orders(), repo.order_events()
    if allowed:
        first = actions[transition]()
        once = repo.all_orders(), repo.order_events()
        assert actions[transition]() == first
        assert (repo.all_orders(), repo.order_events()) == once
    else:
        with pytest.raises(ExecutionValidationError):
            actions[transition]()
        assert (repo.all_orders(), repo.order_events()) == before


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_forged_clean_receipt_cannot_hide_a_durable_ambiguous_order(
    sqlite_engine, signed_plan, backend
):
    from adaptive_trader.platform.execution import MemoryExecutionRepository, reconcile
    from tests.unit.test_platform_execution_persistence import _reconciliation_request

    repo = (
        MemoryExecutionRepository()
        if backend == "memory"
        else SignedExecutionRepository(sqlite_engine)
    )
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    _submitted(repo, signed_plan)
    original = _reconciliation_request(repo, signed_plan)
    omitted = replace(original, durable_orders=())
    receipt = reconcile(omitted)
    assert receipt.status.value == "CLEAN"
    with pytest.raises((ExecutionValidationError, ExecutionPersistenceError)):
        repo.record_reconciliation_bundle(receipt, request=omitted, latch_event=None, incident=None)
    assert repo.reconciliations() == ()


def test_unfilled_reduction_barrier_blocks_later_entry_in_same_plan():
    from adaptive_trader.platform.execution import (
        DeterministicFakePaperBroker,
        ExecutionService,
        MemoryExecutionRepository,
        plan_signed_orders,
    )
    from tests.unit.test_platform_execution_planner import planning_request

    result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)), ("BBB", Decimal(0))),
            target_weights=(("AAA", Decimal(0)), ("BBB", Decimal("0.1"))),
        )
    )
    repo = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(
        initial_time=NOW,
        initial_cash=Decimal(1000),
        default_scenario=FakeBrokerScenario.FULL_FILL,
    )
    broker.set_mark_prices((("AAA", Decimal(100)), ("BBB", Decimal(100))))
    from tests.unit.test_platform_execution_broker import _long_plan

    broker.submit(_long_plan().intents[0], submitted_at=NOW)
    broker.set_scenario(result.intents[0].client_order_id, FakeBrokerScenario.PARTIAL_FILL)
    service = ExecutionService(repository=repo, broker=broker)
    outcomes = service.submit_plan(
        result,
        safety_provider=lambda _: replace(
            _safety(), active_symbols=("AAA", "BBB"), shortable_symbols=("AAA", "BBB")
        ),
        paper_context_provider=lambda _: None,
    )
    assert outcomes[0].state is OrderState.PARTIALLY_FILLED
    assert outcomes[1].reason_codes == ("close_phase_incomplete",) and not outcomes[1].submitted
    assert repo.get_order(result.intents[1].client_order_id).state is OrderState.INTENT_COMMITTED


@pytest.mark.parametrize("operation", ["get_plan", "get_intent", "get_order", "get_risk_decision"])
def test_missing_durable_authority_is_distinct_from_empty_results(persisted, operation):
    with pytest.raises(ExecutionPersistenceError, match="does not exist"):
        getattr(persisted, operation)("missing-authority")


@pytest.mark.parametrize("state", ["committed", "filled", "accepted"])
def test_service_recovery_entry_points_reject_wrong_lifecycle_without_events(state):
    plan, repo, _broker, service = _persisted_service(
        scenario=FakeBrokerScenario.DELAYED_UPDATE
        if state == "accepted"
        else FakeBrokerScenario.FULL_FILL
    )
    client = plan.intents[0].client_order_id
    if state != "committed":
        service.submit_one(client, safety=_safety())
    before = repo.order_events()
    with pytest.raises(ExecutionValidationError, match="ambiguous"):
        service.resolve_ambiguous(client, observed_at=NOW)
    if state == "committed":
        with pytest.raises(ExecutionValidationError, match="requires submission"):
            service.refresh_nonterminal(client, observed_at=NOW)
    elif state == "filled":
        assert service.refresh_nonterminal(client, observed_at=NOW) == repo.get_order(client)
    else:
        assert service.submit_one(client, safety=_safety()).reason_codes == (
            "order_already_submitted",
        )
    assert repo.order_events() == before
