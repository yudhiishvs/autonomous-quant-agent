"""Forced-flat orchestration that reports success only after signed reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.execution.authorization import SubmissionSafetySnapshot
from adaptive_trader.platform.execution.broker import Broker
from adaptive_trader.platform.execution.models import (
    ExecutionValidationError,
    OrderIntent,
    OrderState,
    ReconciliationReceipt,
    ReconciliationStatus,
    quantities_match,
)
from adaptive_trader.platform.execution.planner import ExecutionPlanningResult
from adaptive_trader.platform.execution.reconciliation import (
    ReconciliationOutcome,
    ReconciliationRequest,
    reconcile_and_persist,
)
from adaptive_trader.platform.execution.repository import ExecutionRepository
from adaptive_trader.platform.execution.service import ExecutionService, SubmissionResult
from adaptive_trader.platform.risk.latches import RiskLatchState


@dataclass(frozen=True, slots=True)
class ForcedFlattenRequest:
    """Complete orchestration inputs for one exact forced-flat slot."""

    planning_result: ExecutionPlanningResult
    pre_reconciliation: ReconciliationReceipt
    latch_state: RiskLatchState
    conflicting_opening_order_ids: tuple[str, ...]
    target_at: datetime
    submission_cutoff_at: datetime
    required_flat_at: datetime
    attempted_at: datetime
    safety_provider: Callable[[OrderIntent], SubmissionSafetySnapshot]
    final_reconciliation: Callable[[], ReconciliationRequest]

    def __post_init__(self) -> None:
        if type(self.planning_result) is not ExecutionPlanningResult:
            raise ExecutionValidationError("forced flatten requires a planning result")
        if not self.planning_result.plan.forced_flat:
            raise ExecutionValidationError("forced flatten requires forced-flat plan authority")
        if type(self.pre_reconciliation) is not ReconciliationReceipt:
            raise ExecutionValidationError("forced flatten requires prior reconciliation")
        if self.pre_reconciliation.status is not ReconciliationStatus.CLEAN:
            raise ExecutionValidationError("forced flatten requires clean prior reconciliation")
        if self.pre_reconciliation.experiment_hash != self.planning_result.plan.experiment_hash:
            raise ExecutionValidationError("forced-flat evidence belongs to another experiment")
        if type(self.latch_state) is not RiskLatchState:
            raise ExecutionValidationError("forced flatten latch state is invalid")
        if self.latch_state.experiment_hash != self.planning_result.plan.experiment_hash:
            raise ExecutionValidationError("forced-flat latches belong to another experiment")
        if type(self.conflicting_opening_order_ids) is not tuple or (
            self.conflicting_opening_order_ids
            != tuple(sorted(set(self.conflicting_opening_order_ids)))
        ):
            raise ExecutionValidationError("conflicting opening order IDs must be ordered")
        try:
            target_at = require_utc_instant(self.target_at, field_name="target_at")
            submission_cutoff_at = require_utc_instant(
                self.submission_cutoff_at,
                field_name="submission_cutoff_at",
            )
            required_flat_at = require_utc_instant(
                self.required_flat_at,
                field_name="required_flat_at",
            )
            attempted_at = require_utc_instant(self.attempted_at, field_name="attempted_at")
        except DomainValidationError:
            raise ExecutionValidationError(
                "forced-flat timestamps must be timezone-aware UTC"
            ) from None
        if not target_at < submission_cutoff_at < required_flat_at:
            raise ExecutionValidationError("forced-flat times are not ordered")
        if attempted_at < target_at:
            raise ExecutionValidationError("forced flatten cannot run before its target")
        if self.pre_reconciliation.completed_at > attempted_at:
            raise ExecutionValidationError("forced-flat reconciliation cannot come from the future")
        if not callable(self.safety_provider) or not callable(self.final_reconciliation):
            raise ExecutionValidationError("forced flatten callbacks are invalid")


@dataclass(frozen=True, slots=True)
class ForcedFlattenResult:
    """Forced-flat result that cannot represent unproven success."""

    success: bool
    canceled_client_order_ids: tuple[str, ...]
    submissions: tuple[SubmissionResult, ...]
    reconciliation: ReconciliationOutcome

    def __post_init__(self) -> None:
        proven = _receipt_proves_flat(self.reconciliation.receipt)
        if self.success is not proven:
            raise ExecutionValidationError("forced-flat success must equal reconciled flat state")


class ForcedFlattenService:
    """Cancel opening conflicts, close signed positions, and prove flat by deadline."""

    def __init__(self, *, repository: ExecutionRepository, broker: Broker) -> None:
        self._repository = repository
        self._broker = broker
        self._execution = ExecutionService(repository=repository, broker=broker)

    def run(self, request: ForcedFlattenRequest) -> ForcedFlattenResult:
        """Execute one forced-flat attempt with a durable fail-closed outcome."""

        if type(request) is not ForcedFlattenRequest:
            raise ExecutionValidationError("forced flatten request is invalid")
        canceled: list[str] = []
        for client_order_id in request.conflicting_opening_order_ids:
            state = self._repository.get_order(client_order_id).state
            if state.terminal:
                continue
            canceled_order = self._execution.cancel_one(
                client_order_id,
                requested_at=request.attempted_at,
            )
            if canceled_order.state is OrderState.CANCELED:
                canceled.append(client_order_id)

        self._execution.persist(request.planning_result)
        submissions: tuple[SubmissionResult, ...]
        if request.planning_result.intents and request.attempted_at >= request.submission_cutoff_at:
            submissions = tuple(
                SubmissionResult(
                    intent.client_order_id,
                    self._repository.get_order(intent.client_order_id).state,
                    False,
                    ("forced_flat_submission_cutoff_passed",),
                )
                for intent in request.planning_result.intents
            )
        else:
            submissions = self._execution.submit_plan(
                request.planning_result,
                safety_provider=_entry_disabled_safety(request.safety_provider),
            )

        reconciliation_request = request.final_reconciliation()
        if (
            not reconciliation_request.require_flat
            or reconciliation_request.required_flat_at != request.required_flat_at
            or reconciliation_request.execution_plan_id
            != request.planning_result.plan.execution_plan_id
        ):
            raise ExecutionValidationError(
                "forced-flat reconciliation must bind the plan and required deadline"
            )
        reconciliation = reconcile_and_persist(
            repository=self._repository,
            request=reconciliation_request,
            latch_state=request.latch_state,
        )
        success = _receipt_proves_flat(reconciliation.receipt)
        return ForcedFlattenResult(
            success=success,
            canceled_client_order_ids=tuple(canceled),
            submissions=submissions,
            reconciliation=reconciliation,
        )


def _entry_disabled_safety(
    provider: Callable[[OrderIntent], SubmissionSafetySnapshot],
) -> Callable[[OrderIntent], SubmissionSafetySnapshot]:
    def checked(intent: OrderIntent) -> SubmissionSafetySnapshot:
        safety = provider(intent)
        if not safety.entry_disabled:
            raise ExecutionValidationError("forced flatten requires entry-disabled state")
        return safety

    return checked


def _receipt_proves_flat(receipt: ReconciliationReceipt) -> bool:
    return receipt.status is ReconciliationStatus.CLEAN and all(
        quantities_match(position.quantity, Decimal(0)) for position in receipt.observed_positions
    )
