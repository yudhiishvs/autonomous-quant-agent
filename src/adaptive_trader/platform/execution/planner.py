"""Deterministic signed quantity planning with close-first reversal barriers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from adaptive_trader.platform.domain import (
    DecimalRounding,
    quantize_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.execution.models import (
    ExecutionPlan,
    ExecutionValidationError,
    IntentPhase,
    OrderIntent,
    OrderSide,
    Position,
    PositionEffect,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.models import RiskDecision, RiskExecutionScope

LONG_QUANTUM = Decimal("0.000001")
SHORT_QUANTUM = Decimal(1)
MINIMUM_ORDER_NOTIONAL = Decimal("25.00")
MAXIMUM_SINGLE_INTENT_EQUITY_FRACTION = Decimal("0.15")
MAXIMUM_INTENTS_PER_PLAN = 16


@dataclass(frozen=True, slots=True)
class ExecutionPlanningRequest:
    """Complete immutable input for one signed order-planning pass."""

    risk_decision: RiskDecision
    current_positions: tuple[Position, ...]
    reference_prices: tuple[tuple[str, Decimal], ...]
    equity: Decimal
    target_version: int
    created_at: datetime
    deadline_at: datetime
    forced_flat: bool = False

    def __post_init__(self) -> None:
        if type(self.risk_decision) is not RiskDecision:
            raise ExecutionValidationError("planning requires an immutable risk decision")
        if type(self.current_positions) is not tuple or any(
            type(position) is not Position for position in self.current_positions
        ):
            raise ExecutionValidationError("current positions must be immutable positions")
        if type(self.reference_prices) is not tuple or any(
            type(item) is not tuple or len(item) != 2 for item in self.reference_prices
        ):
            raise ExecutionValidationError("reference prices must be immutable symbol-price pairs")
        symbols = tuple(symbol for symbol, _ in self.risk_decision.final_targets)
        if tuple(position.symbol for position in self.current_positions) != symbols:
            raise ExecutionValidationError("current positions must contain exactly risk symbols")
        if tuple(symbol for symbol, _ in self.reference_prices) != symbols:
            raise ExecutionValidationError("reference prices must contain exactly risk symbols")
        if any(
            type(price) is not Decimal or price <= 0 or not price.is_finite()
            for _, price in self.reference_prices
        ):
            raise ExecutionValidationError("reference prices must be positive finite Decimals")
        if type(self.equity) is not Decimal or not self.equity.is_finite() or self.equity <= 0:
            raise ExecutionValidationError("planning equity must be a positive finite Decimal")
        if type(self.target_version) is not int or not 1 <= self.target_version <= 999_999:
            raise ExecutionValidationError("target version must be between one and 999999")
        if type(self.forced_flat) is not bool:
            raise ExecutionValidationError("forced-flat flag must be boolean")
        try:
            created_at = require_utc_instant(self.created_at, field_name="created_at")
            deadline_at = require_utc_instant(self.deadline_at, field_name="deadline_at")
        except DomainValidationError:
            raise ExecutionValidationError(
                "planning timestamps must be timezone-aware UTC"
            ) from None
        if created_at >= deadline_at:
            raise ExecutionValidationError("planning deadline must follow creation time")
        if self.forced_flat and any(weight != 0 for _, weight in self.risk_decision.final_targets):
            raise ExecutionValidationError("forced-flat planning requires exact zero risk targets")


@dataclass(frozen=True, slots=True)
class ExecutionPlanningResult:
    """A plan and its atomically persistable first-stage intents."""

    plan: ExecutionPlan
    intents: tuple[OrderIntent, ...]
    reversal_symbols: tuple[str, ...]

    @property
    def reversal_barrier_required(self) -> bool:
        """Return whether a fresh reconciliation and risk pass is required for entry."""

        return bool(self.reversal_symbols)


def signed_target_quantity(*, weight: Decimal, equity: Decimal, price: Decimal) -> Decimal:
    """Convert a signed weight to the exact supported broker quantity."""

    if any(
        type(value) is not Decimal or not value.is_finite() for value in (weight, equity, price)
    ):
        raise ExecutionValidationError("quantity inputs must be finite Decimals")
    if equity <= 0 or price <= 0:
        raise ExecutionValidationError("quantity equity and price must be positive")
    raw = weight * equity / price
    if raw == 0:
        return Decimal(0)
    quantum = LONG_QUANTUM if raw > 0 else SHORT_QUANTUM
    magnitude = quantize_decimal(
        abs(raw),
        quantum=quantum,
        rounding=DecimalRounding.DOWN,
        field_name="target_quantity",
    )
    return magnitude if raw > 0 else -magnitude


def plan_signed_orders(request: ExecutionPlanningRequest) -> ExecutionPlanningResult:
    """Create deterministic intents, stopping every sign reversal at zero.

    A later opposite opening leg must use a new risk decision after terminal close fills,
    reconciliation, fresh inputs, and a still-open deadline. Calling this function again with
    the refreshed zero position naturally creates only that opposite entry.
    """

    if type(request) is not ExecutionPlanningRequest:
        raise ExecutionValidationError("planner requires a validated request")
    decision = request.risk_decision
    if decision.execution_scope is RiskExecutionScope.NONE:
        raise ExecutionValidationError("risk decision grants no execution authority")
    if decision.decided_at > request.created_at:
        raise ExecutionValidationError("risk decision cannot postdate the execution plan")

    weights = dict(decision.final_targets)
    prices = dict(request.reference_prices)
    current = {position.symbol: position.quantity for position in request.current_positions}
    if decision.execution_scope is RiskExecutionScope.RISK_REDUCING_ONLY and any(
        weight != 0 for weight in weights.values()
    ):
        raise ExecutionValidationError("risk-reducing scope cannot authorize nonzero targets")

    targets = tuple(
        Position(
            symbol=symbol,
            quantity=(
                Decimal(0)
                if request.forced_flat
                else signed_target_quantity(
                    weight=weights[symbol],
                    equity=request.equity,
                    price=prices[symbol],
                )
            ),
        )
        for symbol in sorted(weights)
    )
    plan = ExecutionPlan.create(
        risk_decision_id=decision.risk_decision_id,
        risk_decision_hash=decision.content_hash,
        experiment_hash=decision.experiment_hash,
        correlation_id=decision.correlation_id,
        target_version=request.target_version,
        forced_flat=request.forced_flat,
        target_quantities=targets,
        created_at=request.created_at,
        deadline_at=request.deadline_at,
    )
    target_hash = sha256_hex(
        (
            "signed-quantity-target-v1",
            tuple((position.symbol, position.quantity) for position in targets),
        )
    )
    drafts: list[_IntentDraft] = []
    reversal_symbols: list[str] = []
    for target in targets:
        current_quantity = current[target.symbol]
        price = prices[target.symbol]
        draft, reversal = _transition_draft(
            symbol=target.symbol,
            current=current_quantity,
            target=target.quantity,
            forced_flat=request.forced_flat,
        )
        if draft is not None:
            drafts.extend(
                _split_draft(
                    draft,
                    price=price,
                    maximum_notional=request.equity * MAXIMUM_SINGLE_INTENT_EQUITY_FRACTION,
                )
            )
        if reversal:
            reversal_symbols.append(target.symbol)

    ordered = sorted(drafts, key=lambda item: (item.phase_order, item.symbol, item.part))
    if len(ordered) > MAXIMUM_INTENTS_PER_PLAN:
        raise ExecutionValidationError("execution plan exceeds the 16-intent limit")
    intents = tuple(
        OrderIntent.create(
            execution_plan_id=plan.execution_plan_id,
            risk_decision_id=plan.risk_decision_id,
            experiment_hash=plan.experiment_hash,
            correlation_id=plan.correlation_id,
            symbol=draft.symbol,
            side=draft.side,
            position_effect=draft.effect,
            phase=draft.phase,
            sequence=sequence,
            target_version=plan.target_version,
            quantity=draft.quantity,
            reference_price=prices[draft.symbol],
            final_target_quantity=draft.stage_target,
            forced_flat=request.forced_flat,
            created_at=request.created_at,
            deadline_at=request.deadline_at,
            target_hash=target_hash,
        )
        for sequence, draft in enumerate(ordered)
    )
    for intent in intents:
        if not request.forced_flat and intent.notional < MINIMUM_ORDER_NOTIONAL:
            raise ExecutionValidationError("non-flattening intent is below minimum notional")
    return ExecutionPlanningResult(
        plan=plan,
        intents=intents,
        reversal_symbols=tuple(sorted(reversal_symbols)),
    )


@dataclass(frozen=True, slots=True)
class _IntentDraft:
    symbol: str
    side: OrderSide
    effect: PositionEffect
    phase: IntentPhase
    quantity: Decimal
    stage_target: Decimal
    part: int = 0

    @property
    def phase_order(self) -> int:
        return {
            IntentPhase.EXIT: 0,
            IntentPhase.FLATTEN: 0,
            IntentPhase.ENTRY: 1,
        }[self.phase]


def _transition_draft(
    *,
    symbol: str,
    current: Decimal,
    target: Decimal,
    forced_flat: bool,
) -> tuple[_IntentDraft | None, bool]:
    if current == target:
        return None, False
    if forced_flat:
        if target != 0:
            raise ExecutionValidationError("forced-flat target must be exact zero")
        if current > 0:
            return (
                _IntentDraft(
                    symbol,
                    OrderSide.SELL,
                    PositionEffect.FORCED_FLAT_LONG,
                    IntentPhase.FLATTEN,
                    current,
                    Decimal(0),
                ),
                False,
            )
        if current < 0:
            return (
                _IntentDraft(
                    symbol,
                    OrderSide.BUY,
                    PositionEffect.FORCED_FLAT_SHORT,
                    IntentPhase.FLATTEN,
                    abs(current),
                    Decimal(0),
                ),
                False,
            )
        return None, False

    if current == 0 and target > 0:
        return _entry(symbol, target, long=True, increase=False), False
    if current == 0 and target < 0:
        return _entry(symbol, abs(target), long=False, increase=False), False
    if current > 0 and target >= 0:
        if target > current:
            return _entry(symbol, target - current, long=True, increase=True), False
        effect = PositionEffect.CLOSE_LONG if target == 0 else PositionEffect.REDUCE_LONG
        return _exit(symbol, current - target, effect, target), False
    if current < 0 and target <= 0:
        if target < current:
            return _entry(symbol, abs(target - current), long=False, increase=True), False
        effect = PositionEffect.CLOSE_SHORT if target == 0 else PositionEffect.REDUCE_SHORT
        return _exit(symbol, target - current, effect, target), False
    if current > 0 and target < 0:
        return _exit(symbol, current, PositionEffect.CLOSE_LONG, Decimal(0)), True
    if current < 0 and target > 0:
        return _exit(symbol, abs(current), PositionEffect.CLOSE_SHORT, Decimal(0)), True
    raise ExecutionValidationError("signed transition could not be classified")


def _entry(symbol: str, quantity: Decimal, *, long: bool, increase: bool) -> _IntentDraft:
    if long:
        effect = PositionEffect.INCREASE_LONG if increase else PositionEffect.OPEN_LONG
        side = OrderSide.BUY
        stage_target = quantity
    else:
        effect = PositionEffect.INCREASE_SHORT if increase else PositionEffect.OPEN_SHORT
        side = OrderSide.SELL
        stage_target = -quantity
    return _IntentDraft(symbol, side, effect, IntentPhase.ENTRY, quantity, stage_target)


def _exit(
    symbol: str,
    quantity: Decimal,
    effect: PositionEffect,
    stage_target: Decimal,
) -> _IntentDraft:
    side = (
        OrderSide.SELL
        if effect in {PositionEffect.REDUCE_LONG, PositionEffect.CLOSE_LONG}
        else OrderSide.BUY
    )
    return _IntentDraft(symbol, side, effect, IntentPhase.EXIT, quantity, stage_target)


def _split_draft(
    draft: _IntentDraft,
    *,
    price: Decimal,
    maximum_notional: Decimal,
) -> tuple[_IntentDraft, ...]:
    if draft.quantity * price <= maximum_notional:
        return (draft,)
    quantum = (
        LONG_QUANTUM
        if draft.effect
        in {
            PositionEffect.OPEN_LONG,
            PositionEffect.INCREASE_LONG,
            PositionEffect.REDUCE_LONG,
            PositionEffect.CLOSE_LONG,
            PositionEffect.FORCED_FLAT_LONG,
        }
        else SHORT_QUANTUM
    )
    maximum_quantity = quantize_decimal(
        maximum_notional / price,
        quantum=quantum,
        rounding=DecimalRounding.DOWN,
        field_name="maximum_intent_quantity",
    )
    if maximum_quantity <= 0:
        raise ExecutionValidationError("maximum intent notional cannot support one quantity unit")
    remaining = draft.quantity
    quantities: list[Decimal] = []
    while remaining > 0:
        quantity = min(remaining, maximum_quantity)
        remaining -= quantity
        quantities.append(quantity)
        if len(quantities) > MAXIMUM_INTENTS_PER_PLAN:
            raise ExecutionValidationError("one transition exceeds the 16-intent limit")
    result: list[_IntentDraft] = []
    for part, quantity in enumerate(quantities):
        effect = _split_effect(
            draft.effect,
            first=part == 0,
            last=part == len(quantities) - 1,
        )
        result.append(
            _IntentDraft(
                symbol=draft.symbol,
                side=draft.side,
                effect=effect,
                phase=draft.phase,
                quantity=quantity,
                stage_target=draft.stage_target,
                part=part,
            )
        )
    return tuple(result)


def _split_effect(
    effect: PositionEffect,
    *,
    first: bool,
    last: bool,
) -> PositionEffect:
    if effect is PositionEffect.OPEN_LONG:
        return PositionEffect.OPEN_LONG if first else PositionEffect.INCREASE_LONG
    if effect is PositionEffect.OPEN_SHORT:
        return PositionEffect.OPEN_SHORT if first else PositionEffect.INCREASE_SHORT
    if effect is PositionEffect.CLOSE_LONG:
        return PositionEffect.CLOSE_LONG if last else PositionEffect.REDUCE_LONG
    if effect is PositionEffect.CLOSE_SHORT:
        return PositionEffect.CLOSE_SHORT if last else PositionEffect.REDUCE_SHORT
    return effect
