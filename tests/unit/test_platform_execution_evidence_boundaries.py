"""Adversarial ledger evidence must fail atomically and leave valid recovery possible."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from adaptive_trader.platform.execution import (
    DeterministicFakePaperBroker,
    ExecutionValidationError,
    Incident,
    MemoryExecutionRepository,
    OrderSide,
    OrderState,
    Position,
    reconcile,
)
from adaptive_trader.platform.risk.latches import (
    RiskLatchKind,
    RiskLatchState,
    create_latch_engagement,
)
from adaptive_trader.platform.storage.execution import (
    ExecutionPersistenceError,
    SignedExecutionRepository,
)
from adaptive_trader.platform.storage.repositories import AuditPersistenceError
from adaptive_trader.platform.storage.tables import aqa_audit_events, aqa_risk_latch_events
from tests.unit.test_platform_execution_persistence import (
    _NOW,
    _fake_submission_authority,
    _reconciliation_request,
    signed_plan,
    sqlite_engine,
)

__all__ = ["signed_plan", "sqlite_engine"]


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, sqlite_engine, signed_plan):
    repo = (
        MemoryExecutionRepository()
        if request.param == "memory"
        else SignedExecutionRepository(sqlite_engine)
    )
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    return repo


def _snapshot(repo, engine):
    with engine.connect() as connection:
        audit = tuple(connection.execute(select(aqa_audit_events)).all())
    return (
        repo.all_orders(),
        repo.order_events(),
        repo.fills(),
        audit,
        (repo.audit_events() if isinstance(repo, MemoryExecutionRepository) else ()),
    )


def _submitted(repo, plan):
    broker = DeterministicFakePaperBroker(initial_time=_NOW)
    broker.set_mark_prices((("AAA", Decimal("100")),))
    intent = plan.intents[0]
    repo.record_submission_started(
        intent.client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(repo, broker, intent.client_order_id),
    )
    return broker.submit(intent, submitted_at=_NOW)


def _fill(original, **changes):
    values = dict(
        client_order_id=original.client_order_id,
        broker_execution_id=original.broker_execution_id,
        symbol=original.symbol,
        side=original.side,
        quantity=original.quantity,
        price=original.price,
        fee=original.fee,
        occurred_at=original.occurred_at,
    )
    return original.create(**(values | changes))


@pytest.mark.parametrize(
    "mutation",
    [
        "overfill",
        "incomplete_filled",
        "complete_partial",
        "unfilled_state",
        "missing_fills",
        "wrong_symbol",
        "wrong_side",
        "before_submission",
        "future_fill",
        "wrong_average",
    ],
)
def test_malformed_broker_evidence_is_atomic_and_valid_retry_recovers(
    ledger, sqlite_engine, signed_plan, mutation
):
    good = _submitted(ledger, signed_plan)
    fill = good.fills[0]
    variants = {
        "overfill": dict(cumulative_filled_quantity=good.cumulative_filled_quantity + 1),
        "incomplete_filled": dict(cumulative_filled_quantity=good.cumulative_filled_quantity / 2),
        "complete_partial": dict(state=OrderState.PARTIALLY_FILLED),
        "unfilled_state": dict(state=OrderState.ACCEPTED),
        "missing_fills": dict(fills=()),
        "wrong_symbol": dict(fills=(_fill(fill, symbol="BBB"),)),
        "wrong_side": dict(fills=(_fill(fill, side=OrderSide.SELL),)),
        "before_submission": dict(fills=(_fill(fill, occurred_at=_NOW - timedelta(seconds=1)),)),
        "future_fill": dict(
            fills=(_fill(fill, occurred_at=good.occurred_at + timedelta(seconds=1)),)
        ),
        "wrong_average": dict(average_fill_price=good.average_fill_price + 1),
    }
    bad = replace(good, **variants[mutation])
    before = _snapshot(ledger, sqlite_engine)
    with pytest.raises(ExecutionValidationError):
        ledger.apply_broker_update(bad)
    assert _snapshot(ledger, sqlite_engine) == before
    assert ledger.apply_broker_update(good).state is OrderState.FILLED
    assert len(ledger.fills()) == 1


def test_stable_broker_identity_cannot_be_rebound_after_fill(ledger, sqlite_engine, signed_plan):
    good = _submitted(ledger, signed_plan)
    ledger.apply_broker_update(good)
    before = _snapshot(ledger, sqlite_engine)
    with pytest.raises(ExecutionValidationError, match="cannot change"):
        ledger.apply_broker_update(replace(good, broker_order_id="different-broker-order"))
    assert _snapshot(ledger, sqlite_engine) == before
    assert ledger.apply_broker_update(good).state is OrderState.FILLED


@pytest.mark.parametrize(
    "field, value",
    [
        ("baseline_cash", Decimal("999")),
        ("mark_prices", (("AAA", Decimal("101")),)),
        ("expected_account_id_hash", "f" * 64),
        ("short_eligible_symbols", ()),
    ],
)
def test_clean_receipt_cannot_be_paired_with_substituted_signed_authority(
    ledger, sqlite_engine, signed_plan, field, value
):
    original = _reconciliation_request(ledger, signed_plan)
    receipt = reconcile(original)
    request = replace(original, **{field: value})
    before = _snapshot(ledger, sqlite_engine)
    with pytest.raises((ExecutionValidationError, ExecutionPersistenceError)):
        ledger.record_reconciliation_bundle(
            receipt, request=request, latch_event=None, incident=None
        )
    assert ledger.reconciliations() == ()
    assert _snapshot(ledger, sqlite_engine) == before


def test_sql_blocking_bundle_audit_failure_rolls_back_controls_then_retry_is_exactly_once(
    sqlite_engine, signed_plan, monkeypatch
):
    repo = SignedExecutionRepository(sqlite_engine)
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    request = _reconciliation_request(
        repo, signed_plan, observed_positions=(Position("AAA", Decimal(1)),)
    )
    receipt = reconcile(request)
    latch = create_latch_engagement(
        latch_state=RiskLatchState.empty(experiment_hash=receipt.experiment_hash),
        latch_type=RiskLatchKind.RECONCILIATION,
        reason_code="reconciliation_blocking",
        actor="execution_worker",
        occurred_at=receipt.completed_at,
        correlation_id=receipt.correlation_id,
        idempotency_key=f"reconciliation_{receipt.content_hash[:32]}",
    )
    incident = Incident.create(
        idempotency_key=f"reconciliation:{receipt.content_hash[:32]}",
        experiment_hash=receipt.experiment_hash,
        correlation_id=receipt.correlation_id,
        reason_code="reconciliation_blocking",
        opened_at=receipt.completed_at,
    )
    original = repo._append_audit

    def fail(*args, **kwargs):
        if kwargs.get("event_type") == "reconciliation.completed":
            raise AuditPersistenceError("injected storage failure")
        return original(*args, **kwargs)

    before = _snapshot(repo, sqlite_engine)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_append_audit", fail)
        with pytest.raises(ExecutionPersistenceError, match="audit"):
            repo.record_reconciliation_bundle(
                receipt, request=request, latch_event=latch, incident=incident
            )
    assert repo.reconciliations() == () and repo.incidents() == ()
    with sqlite_engine.connect() as connection:
        assert connection.execute(select(aqa_risk_latch_events)).all() == []
    assert _snapshot(repo, sqlite_engine) == before
    for _ in range(2):
        repo.record_reconciliation_bundle(
            receipt, request=request, latch_event=latch, incident=incident
        )
    assert repo.reconciliations() == (receipt,)
    assert repo.incidents() == (incident,)


@pytest.mark.parametrize(
    "corruption",
    ["missing_event", "missing_fill", "wrong_symbol", "wrong_side", "future_fill", "wrong_price"],
)
def test_sql_restart_rejects_internally_hashed_evidence_that_disagrees_with_history(
    sqlite_engine, signed_plan, corruption
):
    from sqlalchemy import delete, update

    from adaptive_trader.platform.storage.execution import _fill_row
    from adaptive_trader.platform.storage.tables import aqa_fills, aqa_order_events

    repo = SignedExecutionRepository(sqlite_engine)
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    good = _submitted(repo, signed_plan)
    repo.apply_broker_update(good)
    fill = good.fills[0]
    with sqlite_engine.begin() as connection:
        if corruption == "missing_event":
            connection.execute(delete(aqa_order_events).where(aqa_order_events.c.sequence == 1))
        elif corruption == "missing_fill":
            connection.execute(delete(aqa_fills))
        else:
            changes = {
                "wrong_symbol": dict(symbol="BBB"),
                "wrong_side": dict(side=OrderSide.SELL),
                "future_fill": dict(occurred_at=good.occurred_at + timedelta(seconds=1)),
                "wrong_price": dict(price=fill.price + 1),
            }
            replacement = _fill(fill, **changes[corruption])
            connection.execute(update(aqa_fills).values(**_fill_row(replacement)))
    with pytest.raises(ExecutionPersistenceError):
        SignedExecutionRepository(sqlite_engine).get_order(good.client_order_id)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_event",
        "missing_fill",
        "wrong_symbol",
        "wrong_side",
        "future_fill",
        "wrong_price",
        "duplicate_plan",
        "missing_risk",
        "duplicate_order",
    ],
)
def test_memory_restart_rejects_corrupt_durable_relationships(
    sqlite_engine, signed_plan, corruption
):
    repo = MemoryExecutionRepository()
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    good = _submitted(repo, signed_plan)
    repo.apply_broker_update(good)
    state = repo.export_state()
    fill = state.fills[0]
    changes = {
        "missing_event": dict(order_events=state.order_events[1:]),
        "missing_fill": dict(fills=()),
        "wrong_symbol": dict(fills=(_fill(fill, symbol="BBB"),)),
        "wrong_side": dict(fills=(_fill(fill, side=OrderSide.SELL),)),
        "future_fill": dict(
            fills=(_fill(fill, occurred_at=good.occurred_at + timedelta(seconds=1)),)
        ),
        "wrong_price": dict(fills=(_fill(fill, price=fill.price + 1),)),
        "duplicate_plan": dict(plans=state.plans + state.plans),
        "missing_risk": dict(risk_decisions=()),
        "duplicate_order": dict(orders=state.orders + state.orders),
    }
    with pytest.raises(ExecutionValidationError):
        MemoryExecutionRepository.from_state(replace(state, **changes[corruption]))
    restored = MemoryExecutionRepository.from_state(state)
    assert restored.export_state() == state
    assert restored.apply_broker_update(good) == repo.get_order(good.client_order_id)


@pytest.mark.parametrize("missing", ["projection", "event_owner", "fill_owner"])
def test_memory_restart_requires_every_durable_evidence_owner(signed_plan, missing):
    repo = MemoryExecutionRepository()
    repo.persist_plan_and_intents(
        signed_plan.plan, signed_plan.intents, risk_decision=signed_plan.risk_decision
    )
    good = _submitted(repo, signed_plan)
    repo.apply_broker_update(good)
    state = repo.export_state()
    # Empty authority is valid for an empty ledger, but can never own retained events/fills.
    if missing == "projection":
        broken = replace(state, orders=())
    else:
        broken = replace(
            state,
            plans=(),
            risk_decisions=(),
            intents=(),
            orders=(),
            order_events=state.order_events if missing == "event_owner" else (),
            fills=state.fills if missing == "fill_owner" else (),
        )
    with pytest.raises(ExecutionValidationError, match=r"(projections differ|missing order)"):
        MemoryExecutionRepository.from_state(broken)
    assert MemoryExecutionRepository.from_state(state).export_state() == state
