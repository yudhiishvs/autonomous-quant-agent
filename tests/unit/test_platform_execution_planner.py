"""Signed execution planning and close-first transition tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from adaptive_trader.platform.execution import (
    ExecutionPlanningRequest,
    ExecutionValidationError,
    IntentPhase,
    Position,
    PositionEffect,
    plan_signed_orders,
    signed_target_quantity,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.models import RiskDecision, RiskExecutionScope
from adaptive_trader.platform.risk.policy import ExposureSnapshot

NOW = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=1)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CORRELATION_ID = f"correlation_{'d' * 64}"


def _exposure(targets: tuple[tuple[str, Decimal], ...]) -> ExposureSnapshot:
    positive = sum((value for _, value in targets if value > 0), start=Decimal(0))
    short_abs = sum((-value for _, value in targets if value < 0), start=Decimal(0))
    return ExposureSnapshot(
        gross=positive + short_abs,
        net=positive - short_abs,
        positive=positive,
        short_abs=short_abs,
        group_gross=(),
        cluster_gross=(),
    )


def risk_decision(
    targets: tuple[tuple[str, Decimal], ...],
    *,
    decided_at: datetime = NOW,
    scope: RiskExecutionScope = RiskExecutionScope.FULL,
) -> RiskDecision:
    exposure = _exposure(targets)
    proposal = tuple(
        (
            symbol,
            "LONG" if value > 0 else "SHORT" if value < 0 else "FLAT",
            None,
            value,
        )
        for symbol, value in targets
    )
    reasons = () if scope is RiskExecutionScope.FULL else ("risk_blocked",)
    return RiskDecision.create(
        slot_id=f"slot_{'1' * 64}",
        signal_id=f"signal_{'2' * 64}",
        signal_hash=HASH_A,
        experiment_hash=HASH_B,
        policy_id="signed_intraday",
        policy_version=1,
        policy_hash=HASH_C,
        correlation_id=CORRELATION_ID,
        decided_at=decided_at,
        input_hash=sha256_hex(("risk-input", targets, decided_at)),
        statistics_hash=sha256_hex(("statistics", targets)),
        original_proposal=proposal,
        proposed_targets=targets,
        final_targets=targets,
        before_exposure=exposure,
        after_exposure=exposure,
        ordered_controls=(),
        block_reasons=reasons,
        flatten_reasons=reasons if scope is RiskExecutionScope.RISK_REDUCING_ONLY else (),
        source_timestamps=(("account", decided_at),),
        latch_state_hash=sha256_hex(("latches",)),
        active_latches=(),
        required_latch_events=(),
        execution_scope=scope,
    )


def planning_request(
    *,
    current: tuple[tuple[str, Decimal], ...],
    target_weights: tuple[tuple[str, Decimal], ...],
    prices: tuple[tuple[str, Decimal], ...] | None = None,
    forced_flat: bool = False,
    scope: RiskExecutionScope = RiskExecutionScope.FULL,
) -> ExecutionPlanningRequest:
    selected_prices = prices or tuple((symbol, Decimal("100")) for symbol, _ in target_weights)
    return ExecutionPlanningRequest(
        risk_decision=risk_decision(target_weights, scope=scope),
        current_positions=tuple(Position(symbol, quantity) for symbol, quantity in current),
        reference_prices=selected_prices,
        equity=Decimal("1000"),
        target_version=1,
        created_at=NOW,
        deadline_at=DEADLINE,
        forced_flat=forced_flat,
    )


def test_signed_quantity_uses_fractional_longs_and_whole_share_shorts() -> None:
    assert signed_target_quantity(
        weight=Decimal("0.123456789"),
        equity=Decimal("1000"),
        price=Decimal("100"),
    ) == Decimal("1.234567")
    assert signed_target_quantity(
        weight=Decimal("-0.199"),
        equity=Decimal("1000"),
        price=Decimal("60"),
    ) == Decimal("-3")
    assert (
        signed_target_quantity(
            weight=Decimal(0),
            equity=Decimal("1000"),
            price=Decimal("100"),
        )
        == 0
    )


@pytest.mark.parametrize(
    ("current", "target_weight", "effect", "phase"),
    [
        (Decimal(0), Decimal("0.10"), PositionEffect.OPEN_LONG, IntentPhase.ENTRY),
        (Decimal("0.5"), Decimal("0.10"), PositionEffect.INCREASE_LONG, IntentPhase.ENTRY),
        (Decimal(1), Decimal("0.05"), PositionEffect.REDUCE_LONG, IntentPhase.EXIT),
        (Decimal(1), Decimal(0), PositionEffect.CLOSE_LONG, IntentPhase.EXIT),
        (Decimal(0), Decimal("-0.10"), PositionEffect.OPEN_SHORT, IntentPhase.ENTRY),
        (Decimal(-1), Decimal("-0.20"), PositionEffect.INCREASE_SHORT, IntentPhase.ENTRY),
        (Decimal(-2), Decimal("-0.10"), PositionEffect.REDUCE_SHORT, IntentPhase.EXIT),
        (Decimal(-1), Decimal(0), PositionEffect.CLOSE_SHORT, IntentPhase.EXIT),
    ],
)
def test_every_nonreversal_transition_has_explicit_effect(
    current: Decimal,
    target_weight: Decimal,
    effect: PositionEffect,
    phase: IntentPhase,
) -> None:
    result = plan_signed_orders(
        planning_request(
            current=(("AAA", current),),
            target_weights=(("AAA", target_weight),),
        )
    )

    assert len(result.intents) == 1
    assert result.intents[0].position_effect is effect
    assert result.intents[0].phase is phase
    assert not result.reversal_barrier_required


@pytest.mark.parametrize(
    ("current", "target_weight", "effect"),
    [
        (Decimal(1), Decimal("-0.10"), PositionEffect.CLOSE_LONG),
        (Decimal(-1), Decimal("0.10"), PositionEffect.CLOSE_SHORT),
    ],
)
def test_sign_reversal_emits_only_close_stage(
    current: Decimal,
    target_weight: Decimal,
    effect: PositionEffect,
) -> None:
    first = plan_signed_orders(
        planning_request(
            current=(("AAA", current),),
            target_weights=(("AAA", target_weight),),
        )
    )

    assert first.reversal_symbols == ("AAA",)
    assert len(first.intents) == 1
    assert first.intents[0].position_effect is effect
    assert first.intents[0].final_target_quantity == 0

    refreshed = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", target_weight),),
        )
    )
    assert refreshed.intents[0].position_effect in {
        PositionEffect.OPEN_LONG,
        PositionEffect.OPEN_SHORT,
    }


def test_reductions_sort_before_increases_then_alphabetically() -> None:
    result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(1)), ("BBB", Decimal(0)), ("CCC", Decimal(-1))),
            target_weights=(
                ("AAA", Decimal(0)),
                ("BBB", Decimal("0.10")),
                ("CCC", Decimal(0)),
            ),
        )
    )

    assert tuple((intent.phase, intent.symbol) for intent in result.intents) == (
        (IntentPhase.EXIT, "AAA"),
        (IntentPhase.EXIT, "CCC"),
        (IntentPhase.ENTRY, "BBB"),
    )
    assert tuple(intent.sequence for intent in result.intents) == (0, 1, 2)


def test_client_order_ids_are_stable_bounded_and_stage_specific() -> None:
    request = planning_request(
        current=(("LONGSYMBOL", Decimal(1)),),
        target_weights=(("LONGSYMBOL", Decimal(0)),),
    )
    first = plan_signed_orders(request)
    retry = plan_signed_orders(request)

    assert first == retry
    assert first.intents[0].client_order_id == retry.intents[0].client_order_id
    assert first.intents[0].client_order_id.startswith("aqa-")
    assert len(first.intents[0].client_order_id) <= 48


def test_minimum_notional_and_intent_count_fail_closed() -> None:
    with pytest.raises(ExecutionValidationError, match="minimum notional"):
        plan_signed_orders(
            planning_request(
                current=(("AAA", Decimal(0)),),
                target_weights=(("AAA", Decimal("0.01")),),
                prices=(("AAA", Decimal("10")),),
            )
        )

    with pytest.raises(ExecutionValidationError, match="16-intent"):
        plan_signed_orders(
            planning_request(
                current=(("AAA", Decimal("30")),),
                target_weights=(("AAA", Decimal(0)),),
            )
        )


def test_long_intent_splitting_preserves_fractional_quantity_quantum() -> None:
    result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal("0.30")),),
        )
    )

    assert tuple(intent.quantity for intent in result.intents) == (
        Decimal("1.500000"),
        Decimal("1.500000"),
    )
    assert tuple(intent.position_effect for intent in result.intents) == (
        PositionEffect.OPEN_LONG,
        PositionEffect.INCREASE_LONG,
    )
    assert all(intent.notional <= Decimal("150.00") for intent in result.intents)


def test_forced_flat_bypasses_minimum_notional_and_uses_explicit_effects() -> None:
    result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal("0.1")), ("BBB", Decimal(-1))),
            target_weights=(("AAA", Decimal(0)), ("BBB", Decimal(0))),
            prices=(("AAA", Decimal("10")), ("BBB", Decimal("10"))),
            forced_flat=True,
            scope=RiskExecutionScope.RISK_REDUCING_ONLY,
        )
    )

    assert tuple(intent.position_effect for intent in result.intents) == (
        PositionEffect.FORCED_FLAT_LONG,
        PositionEffect.FORCED_FLAT_SHORT,
    )
    assert all(intent.phase is IntentPhase.FLATTEN for intent in result.intents)


def test_no_execution_scope_cannot_create_a_plan() -> None:
    with pytest.raises(ExecutionValidationError, match="no execution authority"):
        plan_signed_orders(
            planning_request(
                current=(("AAA", Decimal(0)),),
                target_weights=(("AAA", Decimal(0)),),
                scope=RiskExecutionScope.NONE,
            )
        )


def test_risk_decision_cannot_postdate_execution_plan() -> None:
    request = planning_request(
        current=(("AAA", Decimal(0)),),
        target_weights=(("AAA", Decimal(0)),),
    )
    future_decision = risk_decision(
        (("AAA", Decimal(0)),),
        decided_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ExecutionValidationError, match="cannot postdate"):
        plan_signed_orders(replace(request, risk_decision=future_decision))
