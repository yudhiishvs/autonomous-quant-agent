"""Intent-before-side-effect execution orchestration and restart recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from adaptive_trader.platform.execution.authorization import (
    PaperGateContext,
    SubmissionAuthorization,
    SubmissionSafetySnapshot,
    authorize_intent,
    authorize_paper_intent,
)
from adaptive_trader.platform.execution.broker import (
    AlpacaPaperBrokerAdapter,
    Broker,
    BrokerSubmissionUncertain,
)
from adaptive_trader.platform.execution.models import (
    BrokerOrder,
    ExecutionValidationError,
    IntentPhase,
    OrderIntent,
    OrderState,
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
        self._repository = repository
        self._broker = broker

    def persist(self, result: ExecutionPlanningResult) -> None:
        """Atomically persist one plan and all first-stage intents."""

        if type(result) is not ExecutionPlanningResult:
            raise ExecutionValidationError("execution persistence requires a planning result")
        self._repository.persist_plan_and_intents(result.plan, result.intents)

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
        self._repository.record_submission_started(
            client_order_id,
            started_at=safety.evaluated_at,
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
        is_paper_adapter = isinstance(self._broker, AlpacaPaperBrokerAdapter)
        if is_paper_adapter:
            if paper_context is None:
                return _denied("paper_gate_context_missing")
            if paper_context.adapter_paper_only is not self._broker.paper_only:
                return _denied("paper_adapter_identity_mismatch")
            if paper_context.safety != safety:
                return _denied("paper_safety_snapshot_mismatch")
            return authorize_paper_intent(intent, context=paper_context)
        if paper_context is not None:
            return _denied("paper_context_with_nonpaper_adapter")
        return authorize_intent(intent, safety=safety)


def _denied(reason: str) -> SubmissionAuthorization:
    return SubmissionAuthorization(approved=False, reasons=(reason,))
