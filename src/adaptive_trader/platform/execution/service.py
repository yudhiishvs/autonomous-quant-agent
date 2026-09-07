"""Intent-before-side-effect execution orchestration and restart recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from adaptive_trader.platform.execution.alpaca_paper import is_factory_created_paper_adapter
from adaptive_trader.platform.execution.authorization import (
    PaperGateContext,
    SubmissionAuthoritySnapshot,
    SubmissionAuthorization,
    SubmissionSafetySnapshot,
    authorize_intent,
    authorize_paper_intent,
)
from adaptive_trader.platform.execution.broker import (
    AlpacaPaperBrokerAdapter,
    Broker,
    BrokerSubmissionUncertain,
    DeterministicFakePaperBroker,
)
from adaptive_trader.platform.execution.models import (
    BrokerOrder,
    ExecutionValidationError,
    Fill,
    IntentPhase,
    OrderIntent,
    OrderSide,
    OrderState,
    Position,
)
from adaptive_trader.platform.execution.planner import ExecutionPlanningResult
from adaptive_trader.platform.execution.repository import ExecutionRepository


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Safe execution-service outcome without raw broker response data."""

    client_order_id: str
    state: OrderState
    submitted: bool
    reason_codes: tuple[str, ...]


class ExecutionService:
    """Submit only precommitted, current, independently authorized intents."""

    def __init__(self, *, repository: ExecutionRepository, broker: Broker) -> None:
        if repository is None or broker is None:
            raise ExecutionValidationError("execution service dependencies are required")
        if type(broker) not in {DeterministicFakePaperBroker, AlpacaPaperBrokerAdapter}:
            raise ExecutionValidationError("broker adapter is not in the closed allowlist")
        if broker.paper_only is not True:
            raise ExecutionValidationError("broker adapter must be permanently paper-only")
        if type(broker) is AlpacaPaperBrokerAdapter and not is_factory_created_paper_adapter(
            broker
        ):
            raise ExecutionValidationError("paper adapter lacks factory-owned authority")
        self._repository = repository
        self._broker = broker

    def persist(self, result: ExecutionPlanningResult) -> None:
        """Atomically persist one plan and all first-stage intents."""

        if type(result) is not ExecutionPlanningResult:
            raise ExecutionValidationError("execution persistence requires a planning result")
        self._repository.persist_plan_and_intents(
            result.plan,
            result.intents,
            risk_decision=result.risk_decision,
        )

    def submit_one(
        self,
        client_order_id: str,
        *,
        safety: SubmissionSafetySnapshot,
        paper_context: PaperGateContext | None = None,
    ) -> SubmissionResult:
        """Submit one intent once, persisting ambiguity on every uncertain call."""

        intent = self._repository.get_intent(client_order_id)
        order = self._repository.get_order(client_order_id)
        if order.state.terminal:
            return SubmissionResult(client_order_id, order.state, False, ("already_terminal",))
        if order.state.ambiguous or order.state is OrderState.SUBMISSION_STARTED:
            return SubmissionResult(
                client_order_id,
                order.state,
                False,
                ("reconciliation_required",),
            )
        if order.state is not OrderState.INTENT_COMMITTED:
            return SubmissionResult(
                client_order_id,
                order.state,
                False,
                ("order_already_submitted",),
            )
        authorization = self._authorize(
            intent,
            safety=safety,
            paper_context=paper_context,
        )
        if not authorization.approved:
            return SubmissionResult(
                client_order_id,
                order.state,
                False,
                authorization.reasons,
            )
        authority, authority_reasons = self._submission_authority(intent, safety=safety)
        if authority is None:
            return SubmissionResult(
                client_order_id,
                order.state,
                False,
                authority_reasons,
            )
        try:
            self._repository.record_submission_started(
                client_order_id,
                started_at=safety.evaluated_at,
                authority=authority,
            )
        except ExecutionValidationError as error:
            if str(error) != "submission authority became stale":
                raise
            return SubmissionResult(
                client_order_id,
                self._repository.get_order(client_order_id).state,
                False,
                ("submission_authority_stale",),
            )
        try:
            update = self._broker.submit(intent, submitted_at=safety.evaluated_at)
        except BrokerSubmissionUncertain as error:
            unknown = self._repository.record_submission_unknown(
                client_order_id,
                observed_at=safety.evaluated_at,
                reason_code=error.reason_code,
            )
            return SubmissionResult(
                client_order_id,
                unknown.state,
                True,
                ("submission_unknown",),
            )
        except Exception:
            unknown = self._repository.record_submission_unknown(
                client_order_id,
                observed_at=safety.evaluated_at,
                reason_code="broker_submission_exception",
            )
            return SubmissionResult(
                client_order_id,
                unknown.state,
                True,
                ("submission_unknown",),
            )
        current = self._repository.apply_broker_update(update)
        return SubmissionResult(client_order_id, current.state, True, ())

    def submit_plan(
        self,
        result: ExecutionPlanningResult,
        *,
        safety_provider: Callable[[OrderIntent], SubmissionSafetySnapshot],
        paper_context_provider: Callable[[OrderIntent], PaperGateContext | None] | None = None,
    ) -> tuple[SubmissionResult, ...]:
        """Submit reductions before increases and stop at the first unfilled barrier."""

        if type(result) is not ExecutionPlanningResult or not callable(safety_provider):
            raise ExecutionValidationError("plan submission inputs are invalid")
        self.persist(result)
        outcomes: list[SubmissionResult] = []
        exits_complete = True
        for intent in result.intents:
            if intent.phase is IntentPhase.ENTRY and not exits_complete:
                outcomes.append(
                    SubmissionResult(
                        intent.client_order_id,
                        self._repository.get_order(intent.client_order_id).state,
                        False,
                        ("close_phase_incomplete",),
                    )
                )
                continue
            safety = safety_provider(intent)
            paper_context = (
                None if paper_context_provider is None else paper_context_provider(intent)
            )
            outcome = self.submit_one(
                intent.client_order_id,
                safety=safety,
                paper_context=paper_context,
            )
            outcomes.append(outcome)
            if intent.phase in {IntentPhase.EXIT, IntentPhase.FLATTEN} and (
                outcome.state is not OrderState.FILLED
            ):
                exits_complete = False
        return tuple(outcomes)

    def resolve_ambiguous(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
    ) -> BrokerOrder:
        """Resolve ambiguity only by deterministic client-ID lookup; never resubmit."""

        current = self._repository.get_order(client_order_id)
        if current.state not in {
            OrderState.SUBMISSION_STARTED,
            OrderState.SUBMISSION_UNKNOWN,
            OrderState.RECONCILIATION_REQUIRED,
        }:
            raise ExecutionValidationError("order does not require ambiguous-submission recovery")
        update = self._broker.lookup(client_order_id, observed_at=observed_at)
        if update is None:
            return self._repository.record_reconciliation_required(
                client_order_id,
                observed_at=observed_at,
                reason_code="broker_order_not_found",
            )
        return self._repository.apply_broker_update(update)

    def refresh_nonterminal(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
    ) -> BrokerOrder:
        """Refresh a known submitted order without creating another side effect."""

        current = self._repository.get_order(client_order_id)
        if current.state.terminal:
            return current
        if current.state in {
            OrderState.INTENT_COMMITTED,
            OrderState.SUBMISSION_STARTED,
            OrderState.SUBMISSION_UNKNOWN,
            OrderState.RECONCILIATION_REQUIRED,
        }:
            raise ExecutionValidationError("order requires submission or ambiguity recovery")
        update = self._broker.lookup(client_order_id, observed_at=observed_at)
        if update is None:
            return self._repository.record_reconciliation_required(
                client_order_id,
                observed_at=observed_at,
                reason_code="broker_order_not_found",
            )
        return self._repository.apply_broker_update(update)

    def cancel_one(self, client_order_id: str, *, requested_at: datetime) -> BrokerOrder:
        """Persist a cancel request before invoking the broker."""

        self._repository.record_cancel_requested(
            client_order_id,
            requested_at=requested_at,
        )
        try:
            update = self._broker.cancel(client_order_id, canceled_at=requested_at)
        except Exception:
            return self._repository.record_reconciliation_required(
                client_order_id,
                observed_at=requested_at,
                reason_code="cancellation_outcome_unknown",
            )
        return self._repository.apply_broker_update(update)

    def _authorize(
        self,
        intent: OrderIntent,
        *,
        safety: SubmissionSafetySnapshot,
        paper_context: PaperGateContext | None,
    ) -> SubmissionAuthorization:
        broker_type = type(self._broker)
        if broker_type is AlpacaPaperBrokerAdapter:
            if self._broker.paper_only is not True:
                return _denied("broker_not_paper_only")
            if paper_context is None:
                return _denied("paper_gate_context_missing")
            if paper_context.adapter_paper_only is not self._broker.paper_only:
                return _denied("paper_adapter_identity_mismatch")
            if paper_context.safety != safety:
                return _denied("paper_safety_snapshot_mismatch")
            return authorize_paper_intent(intent, context=paper_context)
        if broker_type is not DeterministicFakePaperBroker:
            return _denied("broker_adapter_not_allowed")
        if self._broker.paper_only is not True:
            return _denied("broker_not_paper_only")
        if paper_context is not None:
            return _denied("paper_context_with_nonpaper_adapter")
        return authorize_intent(intent, safety=safety)

    def _submission_authority(
        self,
        intent: OrderIntent,
        *,
        safety: SubmissionSafetySnapshot,
    ) -> tuple[SubmissionAuthoritySnapshot | None, tuple[str, ...]]:
        """Re-read broker and ledger state immediately before claiming submission."""

        ledger = self._repository.submission_ledger_snapshot(intent.client_order_id)
        plan = self._repository.get_plan(intent.execution_plan_id)
        decision = self._repository.get_risk_decision(plan.risk_decision_id)
        try:
            account = self._broker.account(observed_at=safety.evaluated_at)
            broker_positions = _normalized_positions(self._broker.positions())
            broker_open_orders = _ordered_ids(self._broker.open_client_order_ids())
        except Exception:
            return None, ("broker_state_unavailable",)

        reasons: list[str] = []
        if (
            ledger.intent_hash != intent.content_hash
            or ledger.execution_plan_id != plan.execution_plan_id
            or ledger.risk_decision_hash != decision.content_hash
        ):
            reasons.append("signed_authority_mismatch")

        source_timestamps = dict(decision.source_timestamps)
        expected_price_time = min(price.observed_at for price in decision.planning_prices)
        expected_security_time = min(
            security.observed_at for security in decision.security_metadata
        )
        expected_reconciliation_time = source_timestamps.get("reconciliation")
        expected_symbols = tuple(symbol for symbol, _ in decision.final_targets)
        expected_shortable = tuple(
            security.symbol
            for security in decision.security_metadata
            if security.asset_active
            and security.tradable
            and security.shortable
            and security.easy_to_borrow
            and security.primary_listing_eligible
            and security.broker_capability_known
        )
        if safety.account_observed_at != account.observed_at:
            reasons.append("account_snapshot_mismatch")
        if safety.price_observed_at != expected_price_time:
            reasons.append("planning_price_snapshot_mismatch")
        if safety.security_observed_at != expected_security_time:
            reasons.append("security_snapshot_mismatch")
        if (
            expected_reconciliation_time is None
            or safety.reconciliation_observed_at != expected_reconciliation_time
        ):
            reasons.append("reconciliation_snapshot_mismatch")
        if safety.active_symbols != expected_symbols:
            reasons.append("active_symbols_changed")
        if safety.shortable_symbols != expected_shortable:
            reasons.append("security_eligibility_changed")

        plan_client_ids = {
            item.client_order_id
            for item in self._repository.intents_for_plan(plan.execution_plan_id)
        }
        plan_fills = tuple(
            fill for fill in self._repository.fills() if fill.client_order_id in plan_client_ids
        )
        expected_positions = _positions_after_fills(plan.current_positions, plan_fills)
        expected_cash = _cash_after_fills(decision.account_snapshot.cash, plan_fills)
        prices = dict(plan.reference_prices)
        expected_equity = expected_cash + sum(
            (position.quantity * prices[position.symbol] for position in expected_positions),
            start=Decimal(0),
        )
        experiment_orders = tuple(
            current
            for current in self._repository.all_orders()
            if self._repository.get_intent(current.client_order_id).experiment_hash
            == plan.experiment_hash
        )
        expected_open_orders = tuple(
            sorted(
                current.client_order_id
                for current in experiment_orders
                if not current.state.terminal and current.broker_order_id is not None
            )
        )
        durable_ambiguous = any(
            current.state.ambiguous or current.state is OrderState.SUBMISSION_STARTED
            for current in experiment_orders
        )
        if safety.ambiguous_order_exists is not durable_ambiguous:
            reasons.append("ambiguous_order_snapshot_mismatch")
        if safety.blocking_latch_exists is not bool(ledger.active_latches):
            reasons.append("latch_snapshot_mismatch")
        if account.account_id_hash != decision.account_snapshot.account_id_hash:
            reasons.append("account_id_mismatch")
        if abs(account.cash - expected_cash) > Decimal("0.01"):
            reasons.append("account_cash_changed")
        if abs(account.equity - expected_equity) > Decimal("0.01"):
            reasons.append("account_equity_changed")
        if broker_positions != expected_positions:
            reasons.append("positions_changed")
        if broker_open_orders != expected_open_orders:
            reasons.append("open_orders_changed")
        if reasons:
            return None, tuple(sorted(set(reasons)))
        return (
            SubmissionAuthoritySnapshot.create(
                ledger=ledger,
                safety=safety,
                account=account,
                positions=broker_positions,
                open_client_order_ids=broker_open_orders,
            ),
            (),
        )


def _denied(reason: str) -> SubmissionAuthorization:
    return SubmissionAuthorization(approved=False, reasons=(reason,))


def _normalized_positions(values: object) -> tuple[Position, ...]:
    if type(values) is not tuple or any(type(value) is not Position for value in values):
        raise ExecutionValidationError("broker positions are invalid")
    positions = tuple(
        sorted(
            (value for value in values if value.quantity != 0),
            key=lambda value: value.symbol,
        )
    )
    if len({position.symbol for position in positions}) != len(positions):
        raise ExecutionValidationError("broker positions contain a duplicate symbol")
    return positions


def _ordered_ids(values: object) -> tuple[str, ...]:
    if (
        type(values) is not tuple
        or any(type(value) is not str or not value for value in values)
        or values != tuple(sorted(set(values)))
    ):
        raise ExecutionValidationError("broker open-order IDs are invalid")
    return values


def _positions_after_fills(
    baseline: tuple[Position, ...],
    fills: tuple[Fill, ...],
) -> tuple[Position, ...]:
    quantities = {position.symbol: position.quantity for position in baseline}
    for fill in fills:
        if fill.symbol not in quantities:
            raise ExecutionValidationError("durable fill does not match plan symbols")
        delta = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        quantities[fill.symbol] += delta
    return tuple(
        Position(symbol, quantity)
        for symbol, quantity in sorted(quantities.items())
        if quantity != 0
    )


def _cash_after_fills(baseline: Decimal, fills: tuple[Fill, ...]) -> Decimal:
    cash = baseline
    for fill in fills:
        notional = fill.quantity * fill.price
        cash += notional if fill.side is OrderSide.SELL else -notional
        cash -= fill.fee
    return cash
