"""Forced-flat orchestration that reports success only after signed reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.execution.authorization import SubmissionSafetySnapshot
from adaptive_trader.platform.execution.broker import Broker
from adaptive_trader.platform.execution.models import (
    AccountState,
    ExecutionValidationError,
    Incident,
    OrderIntent,
    OrderState,
    Position,
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
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.latches import RiskLatchState
from adaptive_trader.platform.risk.models import RiskExecutionScope
from adaptive_trader.platform.scheduling.models import DecisionSlot, DecisionType, SlotState
from adaptive_trader.platform.scheduling.service import ClaimResult, ClaimStatus


class ForcedFlatSlotClaimer(Protocol):
    """Narrow durable scheduler capability required by forced-flat execution."""

    def __call__(self, slot_id: str, *, owner: str, now: datetime) -> ClaimResult:
        """Atomically claim the exact forced-flat slot."""


class ForcedFlatSlotRenewer(Protocol):
    """Narrow durable lease-renewal capability for an owned forced-flat slot."""

    def __call__(self, slot_id: str, *, owner: str, now: datetime) -> DecisionSlot:
        """Renew and fence the exact owned forced-flat slot."""


class ForcedFlatSlotCompleter(Protocol):
    """Narrow durable success transition for a forced-flat slot."""

    def __call__(self, slot_id: str, *, owner: str, now: datetime) -> DecisionSlot:
        """Complete the exact owned slot after final proof."""


class ForcedFlatSlotFailer(Protocol):
    """Narrow durable failure transition for a forced-flat slot."""

    def __call__(
        self,
        slot_id: str,
        *,
        owner: str,
        reason_code: str,
        now: datetime,
    ) -> DecisionSlot:
        """Fail the exact owned slot with a bounded reason."""


@dataclass(frozen=True, slots=True)
class ForcedFlattenRequest:
    """Complete orchestration inputs for one exact forced-flat slot."""

    slot_id: str
    experiment_hash: str
    claim_owner: str
    attempted_at: datetime
    claim_slot: ForcedFlatSlotClaimer
    renew_slot: ForcedFlatSlotRenewer
    complete_slot: ForcedFlatSlotCompleter
    fail_slot: ForcedFlatSlotFailer
    latch_state_provider: Callable[[], RiskLatchState]
    pre_reconciliation: Callable[[], ReconciliationRequest]
    planning_provider: Callable[
        [ReconciliationReceipt, AccountState, tuple[Position, ...]],
        ExecutionPlanningResult,
    ]
    safety_provider: Callable[[OrderIntent], SubmissionSafetySnapshot]
    final_reconciliation: Callable[[], ReconciliationRequest]

    def __post_init__(self) -> None:
        if type(self.slot_id) is not str or not self.slot_id.startswith("slot_"):
            raise ExecutionValidationError("forced flatten slot ID is invalid")
        if type(self.experiment_hash) is not str or len(self.experiment_hash) != 64:
            raise ExecutionValidationError("forced flatten experiment hash is invalid")
        if type(self.claim_owner) is not str or not self.claim_owner:
            raise ExecutionValidationError("forced flatten claim owner is invalid")
        try:
            require_utc_instant(self.attempted_at, field_name="attempted_at")
        except DomainValidationError:
            raise ExecutionValidationError(
                "forced-flat timestamps must be timezone-aware UTC"
            ) from None
        callbacks = (
            self.claim_slot,
            self.renew_slot,
            self.complete_slot,
            self.fail_slot,
            self.latch_state_provider,
            self.pre_reconciliation,
            self.planning_provider,
            self.safety_provider,
            self.final_reconciliation,
        )
        if any(not callable(callback) for callback in callbacks):
            raise ExecutionValidationError("forced flatten callbacks are invalid")


@dataclass(frozen=True, slots=True)
class ForcedFlattenResult:
    """Forced-flat result that cannot represent unproven success."""

    success: bool
    slot: DecisionSlot
    canceled_client_order_ids: tuple[str, ...]
    planning_result: ExecutionPlanningResult | None
    submissions: tuple[SubmissionResult, ...]
    reconciliation: ReconciliationOutcome

    def __post_init__(self) -> None:
        proven = _receipt_proves_flat(self.reconciliation.receipt)
        if self.success is not proven:
            raise ExecutionValidationError("forced-flat success must equal reconciled flat state")
        if type(self.slot) is not DecisionSlot:
            raise ExecutionValidationError("forced-flat result slot is invalid")
        expected_state = SlotState.COMPLETED if self.success else SlotState.FAILED
        if (
            self.slot.decision_type is not DecisionType.FORCED_FLAT
            or self.slot.state is not expected_state
        ):
            raise ExecutionValidationError("forced-flat result slot is not terminal")
        if self.planning_result is not None and (
            type(self.planning_result) is not ExecutionPlanningResult
            or self.planning_result.plan.experiment_hash != self.slot.experiment_hash
        ):
            raise ExecutionValidationError("forced-flat result plan is invalid")


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
        claim = request.claim_slot(
            request.slot_id,
            owner=request.claim_owner,
            now=request.attempted_at,
        )
        slot = _claimed_forced_flat_slot(claim, request=request)
        lifecycle_at = request.attempted_at
        stage = "entry_control"
        canceled: list[str] = []
        try:
            # A second audited CLAIMED version is the explicit durable entry-disabled
            # control. Every subsequent phase is fenced by a newer owned lease version.
            slot = _renewed_slot(
                request.renew_slot(
                    slot.slot_id,
                    owner=request.claim_owner,
                    now=lifecycle_at,
                ),
                previous=slot,
                owner=request.claim_owner,
                renewed_at=lifecycle_at,
            )

            stage = "cancellation"
            conflicting = tuple(
                sorted(
                    intent.client_order_id
                    for intent in self._repository.all_intents()
                    if intent.experiment_hash == slot.experiment_hash
                    and intent.position_effect.opens_exposure
                    and not self._repository.get_order(intent.client_order_id).state.terminal
                    and self._repository.get_order(intent.client_order_id).broker_order_id
                    is not None
                )
            )
            for client_order_id in conflicting:
                canceled_order = self._execution.cancel_one(
                    client_order_id,
                    requested_at=request.attempted_at,
                )
                if canceled_order.state is OrderState.CANCELED:
                    canceled.append(client_order_id)

            stage = "pre_reconciliation"
            pre_request = request.pre_reconciliation()
            if (
                type(pre_request) is not ReconciliationRequest
                or pre_request.experiment_hash != slot.experiment_hash
                or pre_request.require_flat
                or pre_request.started_at < request.attempted_at
                or pre_request.completed_at >= slot.deadline_at
            ):
                raise ExecutionValidationError(
                    "forced-flat pre-reconciliation is not fresh slot authority"
                )
            lifecycle_at = pre_request.completed_at
            pre_reconciliation = reconcile_and_persist(
                repository=self._repository,
                request=pre_request,
                latch_state=_latch_state(request.latch_state_provider(), slot=slot),
            )
            if pre_reconciliation.receipt.status is not ReconciliationStatus.CLEAN:
                terminal = _failed_slot(
                    request.fail_slot(
                        slot.slot_id,
                        owner=request.claim_owner,
                        reason_code="forced_flat_reconciliation_blocking",
                        now=lifecycle_at,
                    ),
                    previous=slot,
                    failed_at=lifecycle_at,
                    reason_code="forced_flat_reconciliation_blocking",
                )
                return ForcedFlattenResult(
                    success=False,
                    slot=terminal,
                    canceled_client_order_ids=tuple(canceled),
                    planning_result=None,
                    submissions=(),
                    reconciliation=pre_reconciliation,
                )

            stage = "lease_renewal"
            slot = _renewed_slot(
                request.renew_slot(
                    slot.slot_id,
                    owner=request.claim_owner,
                    now=lifecycle_at,
                ),
                previous=slot,
                owner=request.claim_owner,
                renewed_at=lifecycle_at,
            )

            stage = "broker_refresh"
            account = self._broker.account(observed_at=lifecycle_at)
            positions = self._broker.positions()
            stage = "planning"
            planning_result = request.planning_provider(
                pre_reconciliation.receipt,
                account,
                positions,
            )
            _validate_fresh_flatten_plan(
                planning_result,
                slot=slot,
                pre_reconciliation=pre_reconciliation.receipt,
                account=account,
                positions=positions,
            )
            lifecycle_at = planning_result.plan.created_at
            slot = _renewed_slot(
                request.renew_slot(
                    slot.slot_id,
                    owner=request.claim_owner,
                    now=lifecycle_at,
                ),
                previous=slot,
                owner=request.claim_owner,
                renewed_at=lifecycle_at,
            )

            stage = "persistence"
            self._execution.persist(planning_result)
            submitted: list[SubmissionResult] = []
            for intent in planning_result.intents:
                stage = "submission_safety"
                safety = _entry_disabled_snapshot(request.safety_provider(intent))
                lifecycle_at = safety.evaluated_at
                stage = "submission_lease_renewal"
                slot = _renewed_slot(
                    request.renew_slot(
                        slot.slot_id,
                        owner=request.claim_owner,
                        now=lifecycle_at,
                    ),
                    previous=slot,
                    owner=request.claim_owner,
                    renewed_at=lifecycle_at,
                )
                stage = "submission"
                submitted.append(
                    self._execution.submit_one(
                        intent.client_order_id,
                        safety=safety,
                    )
                )
            submissions = tuple(submitted)

            stage = "final_reconciliation"
            reconciliation_request = request.final_reconciliation()
            if (
                type(reconciliation_request) is not ReconciliationRequest
                or not reconciliation_request.require_flat
                or reconciliation_request.required_flat_at != slot.required_completion_at
                or reconciliation_request.execution_plan_id
                != planning_result.plan.execution_plan_id
                or reconciliation_request.experiment_hash != slot.experiment_hash
                or reconciliation_request.slot_id != slot.slot_id
                or reconciliation_request.started_at < planning_result.plan.created_at
            ):
                raise ExecutionValidationError(
                    "forced-flat reconciliation must bind the plan and required deadline"
                )
            lifecycle_at = reconciliation_request.completed_at
            if lifecycle_at <= slot.required_completion_at:
                slot = _renewed_slot(
                    request.renew_slot(
                        slot.slot_id,
                        owner=request.claim_owner,
                        now=lifecycle_at,
                    ),
                    previous=slot,
                    owner=request.claim_owner,
                    renewed_at=lifecycle_at,
                )
            reconciliation = reconcile_and_persist(
                repository=self._repository,
                request=reconciliation_request,
                latch_state=_latch_state(request.latch_state_provider(), slot=slot),
            )
            success = _receipt_proves_flat(reconciliation.receipt)
            if success:
                stage = "slot_completion"
                terminal = _completed_slot(
                    request.complete_slot(
                        slot.slot_id,
                        owner=request.claim_owner,
                        now=lifecycle_at,
                    ),
                    previous=slot,
                    completed_at=lifecycle_at,
                )
            else:
                stage = "slot_failure"
                terminal = _failed_slot(
                    request.fail_slot(
                        slot.slot_id,
                        owner=request.claim_owner,
                        reason_code="forced_flat_not_proven",
                        now=lifecycle_at,
                    ),
                    previous=slot,
                    failed_at=lifecycle_at,
                    reason_code="forced_flat_not_proven",
                )
            return ForcedFlattenResult(
                success=success,
                slot=terminal,
                canceled_client_order_ids=tuple(canceled),
                planning_result=planning_result,
                submissions=submissions,
                reconciliation=reconciliation,
            )
        except Exception as error:
            self._persist_post_claim_failure(
                slot=slot,
                stage=stage,
                opened_at=lifecycle_at,
            )
            try:
                terminal = request.fail_slot(
                    slot.slot_id,
                    owner=request.claim_owner,
                    reason_code="forced_flat_lifecycle_failed",
                    now=lifecycle_at,
                )
                _failed_slot(
                    terminal,
                    previous=slot,
                    failed_at=lifecycle_at,
                    reason_code="forced_flat_lifecycle_failed",
                )
            except Exception:
                raise ExecutionValidationError(
                    "forced-flat failure was recorded but slot termination failed"
                ) from error
            raise

    def _persist_post_claim_failure(
        self,
        *,
        slot: DecisionSlot,
        stage: str,
        opened_at: datetime,
    ) -> Incident:
        reason_code = f"forced_flat_{stage}_failed"
        incident = Incident.create(
            idempotency_key=(
                "flatten_failure_"
                + sha256_hex(
                    (
                        "forced-flat-lifecycle-failure-v1",
                        slot.slot_id,
                        slot.attempt_count,
                        stage,
                    )
                )[:64]
            ),
            experiment_hash=slot.experiment_hash,
            correlation_id=slot.correlation_id,
            reason_code=reason_code,
            opened_at=opened_at,
        )
        return self._repository.record_incident(incident)


def _entry_disabled_snapshot(safety: SubmissionSafetySnapshot) -> SubmissionSafetySnapshot:
    if type(safety) is not SubmissionSafetySnapshot or not safety.entry_disabled:
        raise ExecutionValidationError("forced flatten requires entry-disabled state")
    return safety


def _latch_state(state: RiskLatchState, *, slot: DecisionSlot) -> RiskLatchState:
    if type(state) is not RiskLatchState or state.experiment_hash != slot.experiment_hash:
        raise ExecutionValidationError("forced-flat latch state is invalid")
    return state


def _renewed_slot(
    current: DecisionSlot,
    *,
    previous: DecisionSlot,
    owner: str,
    renewed_at: datetime,
) -> DecisionSlot:
    if (
        type(current) is not DecisionSlot
        or not _same_slot_identity(current, previous)
        or current.state is not SlotState.CLAIMED
        or current.claim_owner != owner
        or current.claimed_at != renewed_at
        or current.lease_expires_at is None
        or current.lease_expires_at <= renewed_at
        or current.attempt_count != previous.attempt_count
        or current.version != previous.version + 1
    ):
        raise ExecutionValidationError("forced-flat lease renewal is invalid")
    return current


def _completed_slot(
    current: DecisionSlot,
    *,
    previous: DecisionSlot,
    completed_at: datetime,
) -> DecisionSlot:
    if (
        type(current) is not DecisionSlot
        or not _same_slot_identity(current, previous)
        or current.state is not SlotState.COMPLETED
        or current.completed_at != completed_at
        or current.reason_code is None
        or current.attempt_count != previous.attempt_count
        or current.version != previous.version + 1
    ):
        raise ExecutionValidationError("forced-flat completion transition is invalid")
    return current


def _failed_slot(
    current: DecisionSlot,
    *,
    previous: DecisionSlot,
    failed_at: datetime,
    reason_code: str,
) -> DecisionSlot:
    if (
        type(current) is not DecisionSlot
        or not _same_slot_identity(current, previous)
        or current.state is not SlotState.FAILED
        or current.completed_at != failed_at
        or current.reason_code != reason_code
        or current.attempt_count != previous.attempt_count
        or current.version != previous.version + 1
    ):
        raise ExecutionValidationError("forced-flat failure transition is invalid")
    return current


def _same_slot_identity(left: DecisionSlot, right: DecisionSlot) -> bool:
    return (
        left.slot_id == right.slot_id
        and left.experiment_hash == right.experiment_hash
        and left.correlation_id == right.correlation_id
        and left.decision_type is right.decision_type
        and left.ready_at == right.ready_at
        and left.deadline_at == right.deadline_at
        and left.required_completion_at == right.required_completion_at
    )


def _claimed_forced_flat_slot(
    claim: ClaimResult,
    *,
    request: ForcedFlattenRequest,
) -> DecisionSlot:
    if type(claim) is not ClaimResult or claim.status not in {
        ClaimStatus.CLAIMED,
        ClaimStatus.RECLAIMED,
    }:
        raise ExecutionValidationError("forced-flat slot was not claimed")
    slot = claim.slot
    if (
        slot.slot_id != request.slot_id
        or slot.experiment_hash != request.experiment_hash
        or slot.decision_type is not DecisionType.FORCED_FLAT
        or slot.state is not SlotState.CLAIMED
        or slot.claim_owner != request.claim_owner
        or slot.claimed_at != request.attempted_at
        or slot.lease_expires_at is None
        or slot.lease_expires_at <= request.attempted_at
        or request.attempted_at < slot.ready_at
        or request.attempted_at >= slot.deadline_at
    ):
        raise ExecutionValidationError("forced-flat slot claim is invalid")
    return slot


def _validate_fresh_flatten_plan(
    result: ExecutionPlanningResult,
    *,
    slot: DecisionSlot,
    pre_reconciliation: ReconciliationReceipt,
    account: AccountState,
    positions: tuple[Position, ...],
) -> None:
    if type(result) is not ExecutionPlanningResult:
        raise ExecutionValidationError("forced-flat planning provider returned invalid state")
    if (
        type(account) is not AccountState
        or type(positions) is not tuple
        or any(type(position) is not Position for position in positions)
    ):
        raise ExecutionValidationError("forced-flat broker state is invalid")
    plan = result.plan
    decision = result.risk_decision
    nonzero_positions = tuple(position for position in positions if position.quantity != 0)
    broker_positions = {position.symbol: position.quantity for position in nonzero_positions}
    plan_positions = {
        position.symbol: position.quantity
        for position in plan.current_positions
        if position.quantity != 0
    }
    if len(broker_positions) != len(nonzero_positions):
        raise ExecutionValidationError("forced-flat broker positions contain duplicates")
    if (
        not plan.forced_flat
        or plan.experiment_hash != slot.experiment_hash
        or plan.risk_decision_id != decision.risk_decision_id
        or decision.slot_id != slot.slot_id
        or decision.correlation_id != slot.correlation_id
        or decision.execution_scope is not RiskExecutionScope.RISK_REDUCING_ONLY
        or any(target != 0 for _, target in decision.final_targets)
        or broker_positions != plan_positions
        or decision.account_snapshot.account_id_hash != account.account_id_hash
        or abs(decision.account_snapshot.cash - account.cash) > Decimal("0.01")
        or abs(decision.account_snapshot.equity - account.equity) > Decimal("0.01")
        or decision.account_snapshot.buying_power != account.buying_power
        or decision.account_snapshot.observed_at != account.observed_at
        or dict(decision.source_timestamps).get("account") != account.observed_at
        or dict(decision.source_timestamps).get("reconciliation") != pre_reconciliation.completed_at
        or decision.decided_at < pre_reconciliation.completed_at
        or plan.created_at < pre_reconciliation.completed_at
        or plan.created_at >= slot.deadline_at
        or plan.deadline_at > slot.deadline_at
    ):
        raise ExecutionValidationError(
            "forced-flat plan does not match fresh post-cancel authority"
        )


def _receipt_proves_flat(receipt: ReconciliationReceipt) -> bool:
    return receipt.status is ReconciliationStatus.CLEAN and all(
        quantities_match(position.quantity, Decimal(0)) for position in receipt.observed_positions
    )
