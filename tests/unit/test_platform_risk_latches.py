"""Threshold, append-only, restart, and authenticated-clear tests for risk latches."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from adaptive_trader.platform.domain import DeterministicId
from adaptive_trader.platform.risk import (
    LATCH_CLEAR_ACKNOWLEDGEMENT,
    RiskLatchAction,
    RiskLatchError,
    RiskLatchEvent,
    RiskLatchKind,
    RiskLatchState,
    assess_financial_latches,
    create_authenticated_latch_clear,
    create_latch_engagement,
)

_EXPERIMENT_HASH = "a" * 64
_AT = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
_CORRELATION_ID = DeterministicId.from_hash_input(
    prefix="correlation", hash_input=("risk-latch-test", 1)
).value


def _empty() -> RiskLatchState:
    return RiskLatchState.empty(experiment_hash=_EXPERIMENT_HASH)


def _engage(kind: RiskLatchKind = RiskLatchKind.SESSION_LOSS):
    return create_latch_engagement(
        latch_state=_empty(),
        latch_type=kind,
        reason_code=kind.value,
        actor="aqa_execution",
        occurred_at=_AT,
        correlation_id=_CORRELATION_ID,
        idempotency_key=f"engage_{kind.value}",
    )


def test_loss_and_drawdown_thresholds_are_inclusive() -> None:
    session = assess_financial_latches(
        account_equity=Decimal("98000"),
        session_start_equity=Decimal("100000"),
        deployment_high_water_equity=Decimal("100000"),
        latch_state=_empty(),
    )
    drawdown = assess_financial_latches(
        account_equity=Decimal("85000"),
        session_start_equity=Decimal("80000"),
        deployment_high_water_equity=Decimal("100000"),
        latch_state=_empty(),
    )
    above = assess_financial_latches(
        account_equity=Decimal("98000.01"),
        session_start_equity=Decimal("100000"),
        deployment_high_water_equity=Decimal("100000"),
        latch_state=_empty(),
    )

    assert session.session_return == Decimal("-0.02")
    assert session.engage == (RiskLatchKind.SESSION_LOSS,)
    assert drawdown.deployment_drawdown == Decimal("-0.15")
    assert drawdown.engage == (RiskLatchKind.DEPLOYMENT_DRAWDOWN,)
    assert above.engage == ()


def test_engaged_latch_survives_restart_and_does_not_retrigger() -> None:
    event = _engage()
    restarted = RiskLatchState.from_events(
        experiment_hash=_EXPERIMENT_HASH,
        events=(event,),
    )
    assessment = assess_financial_latches(
        account_equity=Decimal("97000"),
        session_start_equity=Decimal("100000"),
        deployment_high_water_equity=Decimal("100000"),
        latch_state=restarted,
    )

    assert event.action is RiskLatchAction.ENGAGED
    assert restarted.is_active(RiskLatchKind.SESSION_LOSS)
    assert restarted.next_sequence(RiskLatchKind.SESSION_LOSS) == 2
    assert assessment.engage == ()
    assert restarted == RiskLatchState.from_events(
        experiment_hash=_EXPERIMENT_HASH,
        events=(event,),
    )


@pytest.mark.parametrize(
    ("authenticated", "acknowledgement", "message"),
    [
        (False, LATCH_CLEAR_ACKNOWLEDGEMENT, "authenticated"),
        (True, "I_HAVE_REVIEWED_SOMETHING_ELSE", "acknowledgement"),
        (True, LATCH_CLEAR_ACKNOWLEDGEMENT + " ", "acknowledgement"),
    ],
)
def test_clear_requires_authentication_and_exact_acknowledgement(
    authenticated: bool,
    acknowledgement: str,
    message: str,
) -> None:
    engaged = _engage()
    state = RiskLatchState.from_events(experiment_hash=_EXPERIMENT_HASH, events=(engaged,))
    with pytest.raises(RiskLatchError, match=message):
        create_authenticated_latch_clear(
            latch_state=state,
            latch_type=RiskLatchKind.SESSION_LOSS,
            authenticated=authenticated,
            acknowledgement=acknowledgement,
            actor="operator",
            occurred_at=_AT,
            correlation_id=_CORRELATION_ID,
            idempotency_key="clear_session_loss",
        )


def test_clear_appends_without_mutating_history_and_restart_is_inactive() -> None:
    engaged = _engage(RiskLatchKind.DEPLOYMENT_DRAWDOWN)
    state = RiskLatchState.from_events(experiment_hash=_EXPERIMENT_HASH, events=(engaged,))
    cleared = create_authenticated_latch_clear(
        latch_state=state,
        latch_type=RiskLatchKind.DEPLOYMENT_DRAWDOWN,
        authenticated=True,
        acknowledgement=LATCH_CLEAR_ACKNOWLEDGEMENT,
        actor="operator",
        occurred_at=_AT,
        correlation_id=_CORRELATION_ID,
        idempotency_key="clear_deployment_drawdown",
    )
    restarted = RiskLatchState.from_events(
        experiment_hash=_EXPERIMENT_HASH,
        events=(engaged, cleared),
    )

    assert cleared.action is RiskLatchAction.CLEARED
    assert cleared.sequence == 2
    assert not restarted.is_active(RiskLatchKind.DEPLOYMENT_DRAWDOWN)
    assert restarted.event_count == 2
    with pytest.raises(FrozenInstanceError):
        engaged.reason_code = "changed"


def test_latch_history_rejects_gaps_duplicate_transitions_and_tampering() -> None:
    engaged = _engage()
    with pytest.raises(RiskLatchError, match="payload hash"):
        replace(engaged, reason_code="changed")
    with pytest.raises(RiskLatchError, match="duplicate"):
        RiskLatchState.from_events(
            experiment_hash=_EXPERIMENT_HASH,
            events=(engaged, engaged),
        )
    with pytest.raises(RiskLatchError, match="contiguous"):
        gap = RiskLatchEvent.create(
            experiment_hash=_EXPERIMENT_HASH,
            latch_type=RiskLatchKind.SESSION_LOSS,
            sequence=2,
            action=RiskLatchAction.ENGAGED,
            reason_code="session_loss",
            actor="aqa_execution",
            occurred_at=_AT,
            correlation_id=_CORRELATION_ID,
            idempotency_key="gap_session_loss",
        )
        RiskLatchState.from_events(
            experiment_hash=_EXPERIMENT_HASH,
            events=(gap,),
        )
    with pytest.raises(RiskLatchError, match="engaged again"):
        second = RiskLatchEvent.create(
            experiment_hash=_EXPERIMENT_HASH,
            latch_type=RiskLatchKind.SESSION_LOSS,
            sequence=2,
            action=RiskLatchAction.ENGAGED,
            reason_code="session_loss",
            actor="aqa_execution",
            occurred_at=_AT,
            correlation_id=_CORRELATION_ID,
            idempotency_key="engage_session_loss_again",
        )
        RiskLatchState.from_events(
            experiment_hash=_EXPERIMENT_HASH,
            events=(engaged, second),
        )


def test_every_closed_latch_type_round_trips_deterministically() -> None:
    assert tuple(RiskLatchKind) == (
        RiskLatchKind.DEPLOYMENT_DRAWDOWN,
        RiskLatchKind.OPERATOR_HALT,
        RiskLatchKind.RECONCILIATION,
        RiskLatchKind.SESSION_LOSS,
    )
    events = tuple(_engage(kind) for kind in RiskLatchKind)
    first = RiskLatchState.from_events(experiment_hash=_EXPERIMENT_HASH, events=events)
    replay = RiskLatchState.from_events(
        experiment_hash=_EXPERIMENT_HASH,
        events=tuple(reversed(events)),
    )

    assert first == replay
    assert first.active == tuple(RiskLatchKind)
