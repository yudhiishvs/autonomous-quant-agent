"""Restart-safe execution ledger protocol and deterministic in-memory implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, Self, TypeVar

from adaptive_trader.platform.constants import AUDIT_GENESIS_HASH
from adaptive_trader.platform.domain import AuditEvent, AuditPayload, AuditWriter
from adaptive_trader.platform.execution.authorization import (
    SubmissionAuthoritySnapshot,
    SubmissionLedgerSnapshot,
)
from adaptive_trader.platform.execution.models import (
    BrokerOrder,
    ExecutionPlan,
    ExecutionValidationError,
    Fill,
    Incident,
    OrderEvent,
    OrderIntent,
    OrderState,
    Position,
    ReconciliationReceipt,
)
from adaptive_trader.platform.risk.latches import RiskLatchEvent, RiskLatchKind, RiskLatchState
from adaptive_trader.platform.risk.models import RiskDecision
from adaptive_trader.platform.storage.repositories import verify_audit_chain

if TYPE_CHECKING:
    from adaptive_trader.platform.execution.broker import BrokerUpdate
    from adaptive_trader.platform.execution.reconciliation import ReconciliationRequest


class ExecutionRepository(Protocol):
    """Atomic persistence capabilities required by the execution service."""

    def persist_plan_and_intents(
        self,
        plan: ExecutionPlan,
        intents: tuple[OrderIntent, ...],
        *,
        risk_decision: RiskDecision,
    ) -> None:
        """Persist the plan, every intent, and initial order state atomically."""

    def get_order(self, client_order_id: str) -> BrokerOrder:
        """Load one durable order projection."""

    def get_plan(self, execution_plan_id: str) -> ExecutionPlan:
        """Load one immutable execution plan."""

    def get_risk_decision(self, risk_decision_id: str) -> RiskDecision:
        """Load the immutable risk authority for one plan."""

    def get_intent(self, client_order_id: str) -> OrderIntent:
        """Load one immutable intent."""

    def all_intents(self) -> tuple[OrderIntent, ...]:
        """Return every immutable intent in deterministic order."""

    def intents_for_plan(self, execution_plan_id: str) -> tuple[OrderIntent, ...]:
        """Return one plan's intents in deterministic submission order."""

    def all_orders(self) -> tuple[BrokerOrder, ...]:
        """Return every durable order projection in deterministic order."""

    def fills(self) -> tuple[Fill, ...]:
        """Return unique fills ordered by broker execution ID."""

    def submission_ledger_snapshot(
        self,
        client_order_id: str,
    ) -> SubmissionLedgerSnapshot:
        """Return all durable evidence that must stay stable until submission is claimed."""

    def record_submission_started(
        self,
        client_order_id: str,
        *,
        started_at: datetime,
        authority: SubmissionAuthoritySnapshot,
    ) -> BrokerOrder:
        """Durably record that a side-effect call is about to begin."""

    def record_submission_unknown(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
        reason_code: str,
    ) -> BrokerOrder:
        """Persist an ambiguous outcome without retrying it."""

    def record_reconciliation_required(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
        reason_code: str,
    ) -> BrokerOrder:
        """Persist an unresolved order state discovered during recovery."""

    def record_cancel_requested(
        self,
        client_order_id: str,
        *,
        requested_at: datetime,
    ) -> BrokerOrder:
        """Persist cancellation intent before contacting the broker."""

    def apply_broker_update(self, update: BrokerUpdate) -> BrokerOrder:
        """Atomically apply an update and idempotently append unique fills."""

    def record_reconciliation_bundle(
        self,
        receipt: ReconciliationReceipt,
        *,
        request: ReconciliationRequest,
        latch_event: RiskLatchEvent | None,
        incident: Incident | None,
    ) -> None:
        """Atomically append reconciliation and any required blocking controls."""

    def record_incident(self, incident: Incident) -> Incident:
        """Idempotently persist a standalone execution incident and audit evidence."""


@dataclass(frozen=True, slots=True)
class ExecutionLedgerState:
    """Immutable restart image used by deterministic tests and offline demo."""

    plans: tuple[ExecutionPlan, ...]
    risk_decisions: tuple[RiskDecision, ...]
    intents: tuple[OrderIntent, ...]
    orders: tuple[BrokerOrder, ...]
    order_events: tuple[OrderEvent, ...]
    fills: tuple[Fill, ...]
    reconciliations: tuple[ReconciliationReceipt, ...]
    incidents: tuple[Incident, ...]
    latch_events: tuple[RiskLatchEvent, ...]
    audit_events: tuple[AuditEvent, ...]


class MemoryExecutionRepository:
    """Transaction-like execution ledger for isolated tests and offline operation.

    The immutable exported state simulates a durable process boundary. Production wiring uses
    the same protocol with PostgreSQL; this implementation deliberately performs no filesystem,
    network, or broker activity.
    """

    def __init__(self) -> None:
        self._plans: dict[str, ExecutionPlan] = {}
        self._risk_decisions: dict[str, RiskDecision] = {}
        self._intents: dict[str, OrderIntent] = {}
        self._intent_ids: dict[str, OrderIntent] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._order_events: dict[str, OrderEvent] = {}
        self._broker_event_ids: dict[str, str] = {}
        self._fills: dict[str, Fill] = {}
        self._reconciliations: dict[str, ReconciliationReceipt] = {}
        self._incidents: dict[str, Incident] = {}
        self._incident_keys: dict[str, Incident] = {}
        self._latch_events: dict[str, RiskLatchEvent] = {}
        self._audit_events: list[AuditEvent] = []

    def persist_plan_and_intents(
        self,
        plan: ExecutionPlan,
        intents: tuple[OrderIntent, ...],
        *,
        risk_decision: RiskDecision,
    ) -> None:
        """Atomically make intents visible before a broker call can start."""

        if type(plan) is not ExecutionPlan or type(intents) is not tuple:
            raise ExecutionValidationError("execution persistence requires validated inputs")
        if any(type(intent) is not OrderIntent for intent in intents):
            raise ExecutionValidationError("execution intents must be immutable")
        if any(intent.execution_plan_id != plan.execution_plan_id for intent in intents):
            raise ExecutionValidationError("execution intent belongs to another plan")
        if tuple(intent.sequence for intent in intents) != tuple(range(len(intents))):
            raise ExecutionValidationError("execution intent sequence must be contiguous")
        from adaptive_trader.platform.execution.planner import (
            ExecutionPlanningRequest,
            plan_signed_orders,
        )

        if type(risk_decision) is not RiskDecision:
            raise ExecutionValidationError("execution authorization is invalid")
        expected = plan_signed_orders(
            ExecutionPlanningRequest(
                risk_decision=risk_decision,
                current_positions=plan.current_positions,
                reference_prices=plan.reference_prices,
                equity=plan.equity,
                target_version=plan.target_version,
                created_at=plan.created_at,
                deadline_at=plan.deadline_at,
                forced_flat=plan.forced_flat,
            )
        )
        if expected.plan != plan or expected.intents != intents:
            raise ExecutionValidationError(
                "execution bundle does not match its signed risk authorization"
            )
        existing_plan = self._plans.get(plan.execution_plan_id)
        if existing_plan is not None:
            existing = self.intents_for_plan(plan.execution_plan_id)
            existing_risk = self._risk_decisions.get(plan.risk_decision_id)
            if existing_plan == plan and existing == intents and existing_risk == risk_decision:
                return
            raise ExecutionValidationError("execution plan identity was reused")
        for intent in intents:
            if (
                intent.client_order_id in self._intents
                or intent.order_intent_id in self._intent_ids
            ):
                raise ExecutionValidationError("order intent identity already exists")

        audit = self._next_audit(
            plan=plan,
            event_type="execution.plan_persisted",
            occurred_at=plan.created_at,
            payload={
                "content_hash": plan.content_hash,
                "count": len(intents),
                "execution_plan_id": plan.execution_plan_id,
                "idempotency_key": plan.execution_plan_id,
            },
        )
        self._plans[plan.execution_plan_id] = plan
        self._risk_decisions[plan.risk_decision_id] = risk_decision
        for intent in intents:
            self._intents[intent.client_order_id] = intent
            self._intent_ids[intent.order_intent_id] = intent
            self._orders[intent.client_order_id] = BrokerOrder.committed(intent)
        self._audit_events.append(audit)

    def get_plan(self, execution_plan_id: str) -> ExecutionPlan:
        """Load one immutable execution plan."""

        try:
            return self._plans[execution_plan_id]
        except KeyError:
            raise ExecutionValidationError("execution plan does not exist") from None

    def get_risk_decision(self, risk_decision_id: str) -> RiskDecision:
        """Load one immutable risk authority."""

        try:
            return self._risk_decisions[risk_decision_id]
        except KeyError:
            raise ExecutionValidationError("risk decision does not exist") from None

    def get_order(self, client_order_id: str) -> BrokerOrder:
        """Load one durable order projection."""

        try:
            return self._orders[client_order_id]
        except KeyError:
            raise ExecutionValidationError("broker order does not exist") from None

    def get_intent(self, client_order_id: str) -> OrderIntent:
        """Load one immutable intent."""

        try:
            return self._intents[client_order_id]
        except KeyError:
            raise ExecutionValidationError("order intent does not exist") from None

    def intents_for_plan(self, execution_plan_id: str) -> tuple[OrderIntent, ...]:
        """Return a plan's intents in submission order."""

        return tuple(
            sorted(
                (
                    intent
                    for intent in self._intents.values()
                    if intent.execution_plan_id == execution_plan_id
                ),
                key=lambda item: item.sequence,
            )
        )

    def all_intents(self) -> tuple[OrderIntent, ...]:
        """Return every immutable intent in deterministic order."""

        return tuple(sorted(self._intents.values(), key=lambda item: item.client_order_id))

    def all_orders(self) -> tuple[BrokerOrder, ...]:
        """Return every durable order projection in deterministic order."""

        return tuple(self._orders[key] for key in sorted(self._orders))

    def order_events(self) -> tuple[OrderEvent, ...]:
        """Return append-only order transitions in deterministic order."""

        return tuple(
            sorted(
                self._order_events.values(),
                key=lambda event: (event.client_order_id, event.sequence),
            )
        )

    def fills(self) -> tuple[Fill, ...]:
        """Return unique fills ordered by broker execution ID."""

        return tuple(self._fills[key] for key in sorted(self._fills))

    def submission_ledger_snapshot(
        self,
        client_order_id: str,
    ) -> SubmissionLedgerSnapshot:
        """Bind the current order to every same-plan fill, active order, and latch."""

        intent = self.get_intent(client_order_id)
        plan = self.get_plan(intent.execution_plan_id)
        decision = self.get_risk_decision(plan.risk_decision_id)
        plan_client_ids = {
            item.client_order_id for item in self.intents_for_plan(plan.execution_plan_id)
        }
        plan_fill_hashes = tuple(
            sorted(
                fill.content_hash
                for fill in self._fills.values()
                if fill.client_order_id in plan_client_ids
            )
        )
        experiment_client_ids = {
            item.client_order_id
            for item in self._intents.values()
            if item.experiment_hash == plan.experiment_hash
        }
        active_order_hashes = tuple(
            sorted(
                order.content_hash
                for order in self._orders.values()
                if order.client_order_id in experiment_client_ids and not order.state.terminal
            )
        )
        latch_state = RiskLatchState.from_events(
            experiment_hash=plan.experiment_hash,
            events=tuple(
                event
                for event in self.latch_events()
                if event.experiment_hash == plan.experiment_hash
            ),
        )
        return SubmissionLedgerSnapshot.create(
            client_order_id=client_order_id,
            experiment_hash=plan.experiment_hash,
            execution_plan_id=plan.execution_plan_id,
            risk_decision_hash=decision.content_hash,
            intent_hash=intent.content_hash,
            order_hash=self.get_order(client_order_id).content_hash,
            plan_fill_hashes=plan_fill_hashes,
            active_order_hashes=active_order_hashes,
            active_latches=latch_state.active,
        )

    def reconciliations(self) -> tuple[ReconciliationReceipt, ...]:
        """Return signed reconciliation receipts in deterministic order."""

        return tuple(self._reconciliations[key] for key in sorted(self._reconciliations))

    def incidents(self) -> tuple[Incident, ...]:
        """Return blocking incidents in deterministic order."""

        return tuple(self._incidents[key] for key in sorted(self._incidents))

    def record_incident(self, incident: Incident) -> Incident:
        """Idempotently persist a standalone execution incident and audit evidence."""

        if type(incident) is not Incident:
            raise ExecutionValidationError("execution incident is invalid")
        existing = self._incident_keys.get(incident.idempotency_key)
        if existing is not None:
            if existing == incident:
                return existing
            raise ExecutionValidationError("incident idempotency key was reused")
        identity = self._incidents.get(incident.incident_id)
        if identity is not None:
            if identity == incident:
                return identity
            raise ExecutionValidationError("incident identity was reused")
        audit = self._next_audit(
            experiment_hash=incident.experiment_hash,
            event_type="incident.opened",
            occurred_at=incident.opened_at,
            payload={
                "content_hash": incident.content_hash,
                "idempotency_key": incident.idempotency_key,
                "incident_id": incident.incident_id,
                "reason_code": incident.reason_code,
            },
        )
        self._incidents[incident.incident_id] = incident
        self._incident_keys[incident.idempotency_key] = incident
        self._audit_events.append(audit)
        return incident

    def latch_events(self) -> tuple[RiskLatchEvent, ...]:
        """Return reconciliation latch history in stream order."""

        return tuple(
            sorted(
                self._latch_events.values(),
                key=lambda event: (event.latch_type.value, event.sequence),
            )
        )

    def audit_events(self) -> tuple[AuditEvent, ...]:
        """Return append-only execution audit evidence."""

        return tuple(self._audit_events)

    def has_ambiguous_order(self) -> bool:
        """Return whether any durable projection blocks new exposure."""

        return any(
            order.state.ambiguous or order.state is OrderState.SUBMISSION_STARTED
            for order in self._orders.values()
        )

    def record_submission_started(
        self,
        client_order_id: str,
        *,
        started_at: datetime,
        authority: SubmissionAuthoritySnapshot,
    ) -> BrokerOrder:
        """Persist the final pre-side-effect boundary."""

        previous = self.get_order(client_order_id)
        if type(authority) is not SubmissionAuthoritySnapshot:
            raise ExecutionValidationError("submission authority is invalid")
        if authority.client_order_id != client_order_id:
            raise ExecutionValidationError("submission authority belongs to another order")
        if self.submission_ledger_snapshot(client_order_id).content_hash != (
            authority.ledger_snapshot_hash
        ):
            raise ExecutionValidationError("submission authority became stale")
        if previous.state is not OrderState.INTENT_COMMITTED:
            raise ExecutionValidationError("only a committed intent can begin submission")
        current = previous.evolve(
            state=OrderState.SUBMISSION_STARTED,
            updated_at=started_at,
            submitted_at=started_at,
        )
        self._append_order_transition(
            previous,
            current,
            submission_authority_hash=authority.content_hash,
        )
        return current

    def record_reconciliation_required(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
        reason_code: str,
    ) -> BrokerOrder:
        """Move an ambiguous/nonterminal order behind the reconciliation barrier."""

        previous = self.get_order(client_order_id)
        if previous.state is OrderState.RECONCILIATION_REQUIRED:
            return previous
        if previous.state.terminal:
            raise ExecutionValidationError("terminal order cannot require reconciliation")
        current = previous.evolve(
            state=OrderState.RECONCILIATION_REQUIRED,
            updated_at=observed_at,
            safe_error_code=reason_code,
        )
        self._append_order_transition(previous, current)
        return current

    def record_cancel_requested(
        self,
        client_order_id: str,
        *,
        requested_at: datetime,
    ) -> BrokerOrder:
        """Persist cancellation authority before contacting the broker."""

        previous = self.get_order(client_order_id)
        if previous.state is OrderState.CANCEL_REQUESTED:
            return previous
        if previous.state.terminal or previous.state.ambiguous:
            raise ExecutionValidationError("order cannot enter cancellation from current state")
        current = previous.evolve(
            state=OrderState.CANCEL_REQUESTED,
            updated_at=requested_at,
        )
        self._append_order_transition(previous, current)
        return current

    def record_submission_unknown(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
        reason_code: str,
    ) -> BrokerOrder:
        """Persist an ambiguity once and never turn it into an automatic retry."""

        previous = self.get_order(client_order_id)
        if previous.state is OrderState.SUBMISSION_UNKNOWN:
            return previous
        if previous.state is not OrderState.SUBMISSION_STARTED:
            raise ExecutionValidationError("only a started submission can become unknown")
        current = previous.evolve(
            state=OrderState.SUBMISSION_UNKNOWN,
            updated_at=observed_at,
            safe_error_code=reason_code,
        )
        self._append_order_transition(previous, current)
        return current

    def apply_broker_update(self, update: BrokerUpdate) -> BrokerOrder:
        """Apply cumulative broker state and fills exactly once."""

        from adaptive_trader.platform.execution.broker import BrokerUpdate

        if type(update) is not BrokerUpdate:
            raise ExecutionValidationError("broker update is invalid")
        previous = self.get_order(update.client_order_id)
        intent = self.get_intent(update.client_order_id)
        if previous.state is OrderState.INTENT_COMMITTED:
            raise ExecutionValidationError("broker update arrived before submission started")
        if update.cumulative_filled_quantity > intent.quantity:
            raise ExecutionValidationError("broker cumulative fill exceeds order quantity")
        if update.state is OrderState.FILLED and (
            update.cumulative_filled_quantity != intent.quantity
        ):
            raise ExecutionValidationError("filled order must contain the complete intent quantity")
        if update.state is OrderState.PARTIALLY_FILLED and not (
            Decimal(0) < update.cumulative_filled_quantity < intent.quantity
        ):
            raise ExecutionValidationError(
                "partial-fill state requires an incomplete positive fill"
            )
        if (
            update.state
            in {
                OrderState.SUBMITTED,
                OrderState.ACCEPTED,
                OrderState.PENDING,
                OrderState.REJECTED,
            }
            and update.cumulative_filled_quantity != 0
        ):
            raise ExecutionValidationError("unfilled broker state cannot contain fills")
        existing_event_owner = self._broker_event_ids.get(update.broker_event_id)
        if existing_event_owner is not None and existing_event_owner != update.client_order_id:
            raise ExecutionValidationError("broker event ID was reused across orders")
        if any(
            order.client_order_id != update.client_order_id
            and order.broker_order_id == update.broker_order_id
            for order in self._orders.values()
        ):
            raise ExecutionValidationError("broker order ID was reused across intents")
        if (
            previous.broker_order_id is not None
            and update.broker_order_id != previous.broker_order_id
        ):
            raise ExecutionValidationError("broker order ID cannot change once assigned")
        for fill in update.fills:
            if (
                fill.client_order_id != intent.client_order_id
                or fill.symbol != intent.symbol
                or fill.side is not intent.side
            ):
                raise ExecutionValidationError("broker fill does not match its durable intent")
            existing = self._fills.get(fill.broker_execution_id)
            if existing is not None and existing != fill:
                raise ExecutionValidationError("broker execution ID was reused with new content")
        candidate_fills = {
            fill.broker_execution_id: fill
            for fill in (*self.fills_for_order(intent.client_order_id), *update.fills)
        }
        filled_quantity = sum(
            (fill.quantity for fill in candidate_fills.values()),
            start=Decimal(0),
        )
        if filled_quantity != update.cumulative_filled_quantity:
            raise ExecutionValidationError("broker cumulative fill disagrees with unique fills")
        _verify_fill_evidence(
            intent=intent,
            order=previous,
            fills=tuple(candidate_fills.values()),
            cumulative_quantity=update.cumulative_filled_quantity,
            average_price=update.average_fill_price,
            update_at=update.occurred_at,
        )

        same_projection = (
            previous.broker_order_id == update.broker_order_id
            and previous.state is update.state
            and previous.cumulative_filled_quantity == update.cumulative_filled_quantity
            and previous.average_fill_price == update.average_fill_price
            and previous.safe_error_code == update.safe_error_code
        )
        if same_projection:
            if any(fill.broker_execution_id not in self._fills for fill in update.fills):
                raise ExecutionValidationError(
                    "unchanged broker projection cannot introduce new fill evidence"
                )
            return previous
        accepted_at = (
            update.occurred_at
            if update.state
            in {
                OrderState.ACCEPTED,
                OrderState.PENDING,
                OrderState.PARTIALLY_FILLED,
                OrderState.FILLED,
                OrderState.CANCEL_REQUESTED,
                OrderState.CANCELED,
            }
            and previous.accepted_at is None
            else previous.accepted_at
        )
        current = previous.evolve(
            state=update.state,
            updated_at=update.occurred_at,
            broker_order_id=update.broker_order_id,
            accepted_at=accepted_at,
            cumulative_filled_quantity=update.cumulative_filled_quantity,
            average_fill_price=update.average_fill_price,
            safe_error_code=update.safe_error_code,
        )
        self._append_order_transition(
            previous,
            current,
            broker_event_id=update.broker_event_id,
        )
        for fill in update.fills:
            if fill.broker_execution_id in self._fills:
                continue
            self._fills[fill.broker_execution_id] = fill
            self._audit_events.append(
                self._next_audit(
                    plan=self.get_plan(intent.execution_plan_id),
                    event_type="fill.recorded",
                    occurred_at=fill.occurred_at,
                    payload={
                        "content_hash": fill.content_hash,
                        "fill_id": fill.broker_execution_id,
                        "idempotency_key": fill.broker_execution_id,
                        "order_intent_id": intent.order_intent_id,
                        "quantity": fill.quantity,
                        "symbol": fill.symbol,
                    },
                )
            )
        return current

    def fills_for_order(self, client_order_id: str) -> tuple[Fill, ...]:
        """Return unique fills for one order in occurrence/ID order."""

        return tuple(
            sorted(
                (fill for fill in self._fills.values() if fill.client_order_id == client_order_id),
                key=lambda fill: (fill.occurred_at, fill.broker_execution_id),
            )
        )

    def record_reconciliation_bundle(
        self,
        receipt: ReconciliationReceipt,
        *,
        request: ReconciliationRequest,
        latch_event: RiskLatchEvent | None,
        incident: Incident | None,
    ) -> None:
        """Atomically append a receipt, reconciliation latch, incident, and audit event."""

        from adaptive_trader.platform.execution.reconciliation import (
            ReconciliationRequest,
            reconcile,
        )

        if type(receipt) is not ReconciliationReceipt or type(request) is not ReconciliationRequest:
            raise ExecutionValidationError("reconciliation receipt is invalid")
        if latch_event is not None:
            if type(latch_event) is not RiskLatchEvent:
                raise ExecutionValidationError("reconciliation latch event is invalid")
            if latch_event.experiment_hash != receipt.experiment_hash:
                raise ExecutionValidationError("reconciliation latch experiment mismatch")
            prior = self._latch_events.get(latch_event.latch_event_id)
            if prior is not None and prior != latch_event:
                raise ExecutionValidationError("latch event identity was reused")
        if incident is not None:
            if type(incident) is not Incident:
                raise ExecutionValidationError("reconciliation incident is invalid")
            if incident.experiment_hash != receipt.experiment_hash:
                raise ExecutionValidationError("reconciliation incident experiment mismatch")
            keyed = self._incident_keys.get(incident.idempotency_key)
            if keyed is not None and keyed != incident:
                raise ExecutionValidationError("incident idempotency key was reused")
        plan = (
            self.get_plan(receipt.execution_plan_id)
            if receipt.execution_plan_id is not None
            else None
        )
        if plan is not None and plan.experiment_hash != receipt.experiment_hash:
            raise ExecutionValidationError("reconciliation plan experiment mismatch")
        if plan is None:
            raise ExecutionValidationError(
                "persisted reconciliation requires an authoritative execution plan"
            )
        decision = self._risk_decisions.get(plan.risk_decision_id)
        if decision is None:
            raise ExecutionValidationError("reconciliation plan has no signed risk authorization")
        active_symbols = tuple(symbol for symbol, _ in decision.final_targets)
        short_eligible_symbols = tuple(
            security.symbol
            for security in decision.security_metadata
            if security.asset_active
            and security.tradable
            and security.shortable
            and security.easy_to_borrow
            and security.primary_listing_eligible
            and security.broker_capability_known
        )
        if (
            receipt.slot_id != decision.slot_id
            or request.execution_plan_id != plan.execution_plan_id
            or request.experiment_hash != plan.experiment_hash
            or request.correlation_id != plan.correlation_id
            or request.slot_id != decision.slot_id
            or request.active_symbols != active_symbols
            or request.short_eligible_symbols != short_eligible_symbols
            or _normalized_positions(request.baseline_positions)
            != _normalized_positions(plan.current_positions)
            or request.baseline_cash != decision.account_snapshot.cash
            or request.expected_account_id_hash != decision.account_snapshot.account_id_hash
            or request.mark_prices != plan.reference_prices
        ):
            raise ExecutionValidationError(
                "reconciliation request does not match its execution plan"
            )
        plan_intents = self.intents_for_plan(plan.execution_plan_id)
        plan_client_ids = {intent.client_order_id for intent in plan_intents}
        authoritative_orders = tuple(
            order
            for order in self.all_orders()
            if self.get_plan(
                self.get_intent(order.client_order_id).execution_plan_id
            ).experiment_hash
            == plan.experiment_hash
        )
        authoritative_intents = tuple(
            sorted(
                {
                    intent.client_order_id: intent
                    for intent in (
                        *plan_intents,
                        *(self.get_intent(order.client_order_id) for order in authoritative_orders),
                    )
                }.values(),
                key=lambda intent: intent.client_order_id,
            )
        )
        authoritative = replace(
            request,
            active_symbols=active_symbols,
            short_eligible_symbols=short_eligible_symbols,
            baseline_positions=plan.current_positions,
            baseline_cash=decision.account_snapshot.cash,
            fills=tuple(fill for fill in self.fills() if fill.client_order_id in plan_client_ids),
            order_fills=tuple(
                fill
                for fill in self.fills()
                if fill.client_order_id in {order.client_order_id for order in authoritative_orders}
            ),
            intents=authoritative_intents,
            durable_orders=authoritative_orders,
            expected_account_id_hash=decision.account_snapshot.account_id_hash,
            mark_prices=plan.reference_prices,
        )
        if reconcile(authoritative) != receipt:
            raise ExecutionValidationError(
                "reconciliation receipt does not match authoritative durable state"
            )
        self._validate_reconciliation_controls(
            receipt=receipt,
            latch_event=latch_event,
            incident=incident,
        )
        existing = self._reconciliations.get(receipt.reconciliation_id)
        if existing is not None:
            if existing != receipt:
                raise ExecutionValidationError("reconciliation identity was reused")
            if incident is not None and self._incidents.get(incident.incident_id) != incident:
                raise ExecutionValidationError("reconciliation retry is missing incident evidence")
            return
        audit = self._next_audit(
            plan=plan,
            experiment_hash=receipt.experiment_hash,
            event_type="reconciliation.completed",
            occurred_at=receipt.completed_at,
            payload={
                "content_hash": receipt.content_hash,
                "count": len(receipt.discrepancies),
                "idempotency_key": receipt.reconciliation_id,
                "reconciliation_id": receipt.reconciliation_id,
                "status": receipt.status.value.lower(),
            },
        )
        self._reconciliations[receipt.reconciliation_id] = receipt
        if latch_event is not None:
            self._latch_events.setdefault(latch_event.latch_event_id, latch_event)
        if incident is not None:
            self._incidents[incident.incident_id] = incident
            self._incident_keys[incident.idempotency_key] = incident
        self._audit_events.append(audit)

    def _validate_reconciliation_controls(
        self,
        *,
        receipt: ReconciliationReceipt,
        latch_event: RiskLatchEvent | None,
        incident: Incident | None,
    ) -> None:
        if receipt.status.value == "CLEAN":
            if latch_event is not None or incident is not None:
                raise ExecutionValidationError(
                    "clean reconciliation cannot create blocking controls"
                )
            return
        if incident is None or (
            incident.experiment_hash != receipt.experiment_hash
            or incident.correlation_id != receipt.correlation_id
            or incident.reason_code != "reconciliation_blocking"
            or incident.idempotency_key != f"reconciliation:{receipt.content_hash[:32]}"
            or incident.opened_at != receipt.completed_at
        ):
            raise ExecutionValidationError("blocking reconciliation requires its exact incident")
        prospective = tuple(self._latch_events.values()) + (
            () if latch_event is None else (latch_event,)
        )
        if latch_event is not None and (
            latch_event.latch_type is not RiskLatchKind.RECONCILIATION
            or latch_event.action.value != "ENGAGED"
            or latch_event.correlation_id != receipt.correlation_id
            or latch_event.reason_code != "reconciliation_blocking"
            or latch_event.idempotency_key != f"reconciliation_{receipt.content_hash[:32]}"
            or latch_event.occurred_at != receipt.completed_at
        ):
            raise ExecutionValidationError("blocking reconciliation latch evidence is invalid")
        state = RiskLatchState.from_events(
            experiment_hash=receipt.experiment_hash,
            events=tuple(
                event
                for event in prospective
                if event.experiment_hash == receipt.experiment_hash
                and event.occurred_at <= receipt.completed_at
            ),
        )
        if not state.is_active(RiskLatchKind.RECONCILIATION):
            raise ExecutionValidationError(
                "blocking reconciliation requires an active reconciliation latch"
            )

    def export_state(self) -> ExecutionLedgerState:
        """Freeze all durable state for deterministic restart simulation."""

        return ExecutionLedgerState(
            plans=tuple(self._plans[key] for key in sorted(self._plans)),
            risk_decisions=tuple(self._risk_decisions[key] for key in sorted(self._risk_decisions)),
            intents=tuple(self._intents[key] for key in sorted(self._intents)),
            orders=tuple(self._orders[key] for key in sorted(self._orders)),
            order_events=self.order_events(),
            fills=self.fills(),
            reconciliations=self.reconciliations(),
            incidents=self.incidents(),
            latch_events=self.latch_events(),
            audit_events=self.audit_events(),
        )

    @classmethod
    def from_state(cls, state: ExecutionLedgerState) -> Self:
        """Reconstruct indexes while rejecting duplicate or corrupt durable state."""

        if type(state) is not ExecutionLedgerState:
            raise ExecutionValidationError("execution restart state is invalid")
        repository = cls()
        for decision in state.risk_decisions:
            if decision.risk_decision_id in repository._risk_decisions:
                raise ExecutionValidationError("restart state contains duplicate risk decision")
            repository._risk_decisions[decision.risk_decision_id] = decision
        for plan in state.plans:
            if plan.execution_plan_id in repository._plans:
                raise ExecutionValidationError("restart state contains duplicate plan")
            repository._plans[plan.execution_plan_id] = plan
        for intent in state.intents:
            if (
                intent.client_order_id in repository._intents
                or intent.order_intent_id in repository._intent_ids
            ):
                raise ExecutionValidationError("restart state contains duplicate intent")
            if intent.execution_plan_id not in repository._plans:
                raise ExecutionValidationError("restart intent references a missing plan")
            repository._intents[intent.client_order_id] = intent
            repository._intent_ids[intent.order_intent_id] = intent
        for order in state.orders:
            if order.client_order_id in repository._orders:
                raise ExecutionValidationError("restart state contains duplicate order")
            if order.client_order_id not in repository._intents:
                raise ExecutionValidationError("restart order references a missing intent")
            repository._orders[order.client_order_id] = order
        for event in state.order_events:
            if event.order_event_id in repository._order_events:
                raise ExecutionValidationError("restart state contains duplicate order event")
            repository._order_events[event.order_event_id] = event
            if event.broker_event_id is not None:
                owner = repository._broker_event_ids.setdefault(
                    event.broker_event_id,
                    event.client_order_id,
                )
                if owner != event.client_order_id:
                    raise ExecutionValidationError("restart state reuses a broker event ID")
        for fill in state.fills:
            existing = repository._fills.setdefault(fill.broker_execution_id, fill)
            if existing != fill:
                raise ExecutionValidationError("restart state reuses an execution ID")
        repository._reconciliations = _unique_by(
            state.reconciliations,
            key=lambda item: item.reconciliation_id,
            description="reconciliation",
        )
        repository._incidents = _unique_by(
            state.incidents,
            key=lambda item: item.incident_id,
            description="incident",
        )
        repository._incident_keys = _unique_by(
            state.incidents,
            key=lambda item: item.idempotency_key,
            description="incident idempotency key",
        )
        repository._latch_events = _unique_by(
            state.latch_events,
            key=lambda item: item.latch_event_id,
            description="latch event",
        )
        verify_audit_chain(state.audit_events)
        repository._audit_events = list(state.audit_events)
        repository._verify_authorized_plans()
        repository._verify_projection_history()
        for receipt in repository.reconciliations():
            repository._validate_reconciliation_controls(
                receipt=receipt,
                latch_event=None,
                incident=repository._incident_keys.get(
                    f"reconciliation:{receipt.content_hash[:32]}"
                ),
            )
        return repository

    def _verify_authorized_plans(self) -> None:
        from adaptive_trader.platform.execution.planner import (
            ExecutionPlanningRequest,
            plan_signed_orders,
        )

        for plan in self._plans.values():
            decision = self._risk_decisions.get(plan.risk_decision_id)
            if decision is None:
                raise ExecutionValidationError("restart plan references a missing risk decision")
            expected = plan_signed_orders(
                ExecutionPlanningRequest(
                    risk_decision=decision,
                    current_positions=plan.current_positions,
                    reference_prices=plan.reference_prices,
                    equity=plan.equity,
                    target_version=plan.target_version,
                    created_at=plan.created_at,
                    deadline_at=plan.deadline_at,
                    forced_flat=plan.forced_flat,
                )
            )
            if expected.plan != plan or expected.intents != self.intents_for_plan(
                plan.execution_plan_id
            ):
                raise ExecutionValidationError(
                    "restart execution bundle does not match signed risk authority"
                )

    def _append_order_transition(
        self,
        previous: BrokerOrder,
        current: BrokerOrder,
        *,
        broker_event_id: str | None = None,
        submission_authority_hash: str | None = None,
    ) -> None:
        if broker_event_id is not None and broker_event_id in self._broker_event_ids:
            owner = self._broker_event_ids[broker_event_id]
            if owner != current.client_order_id:
                raise ExecutionValidationError("broker event ID was reused across orders")
            raise ExecutionValidationError("broker event ID was reused with a new projection")
        event = OrderEvent.from_orders(
            previous,
            current,
            broker_event_id=broker_event_id,
        )
        if event.order_event_id in self._order_events:
            raise ExecutionValidationError("broker event conflicts with durable order history")
        intent = self.get_intent(current.client_order_id)
        plan = self.get_plan(intent.execution_plan_id)
        audit = self._next_audit(
            plan=plan,
            event_type="order.state_changed",
            occurred_at=current.updated_at,
            payload={
                "content_hash": current.content_hash,
                "from_state": previous.state.value.lower(),
                "idempotency_key": event.order_event_id,
                "order_intent_id": current.order_intent_id,
                **(
                    {"submission_authority_hash": submission_authority_hash}
                    if submission_authority_hash is not None
                    else {}
                ),
                "to_state": current.state.value.lower(),
            },
        )
        self._orders[current.client_order_id] = current
        self._order_events[event.order_event_id] = event
        if broker_event_id is not None:
            self._broker_event_ids[broker_event_id] = current.client_order_id
        self._audit_events.append(audit)

    def _next_audit(
        self,
        *,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, object],
        plan: ExecutionPlan | None = None,
        experiment_hash: str | None = None,
    ) -> AuditEvent:
        digest = plan.experiment_hash if plan is not None else experiment_hash
        if digest is None:
            raise ExecutionValidationError("execution audit requires an experiment")
        stream_id = f"aqa_execution:{digest}"
        stream = [event for event in self._audit_events if event.stream_id == stream_id]
        previous = stream[-1] if stream else None
        return AuditEvent.create(
            stream_id=stream_id,
            sequence=1 if previous is None else previous.sequence + 1,
            previous_hash=AUDIT_GENESIS_HASH if previous is None else previous.event_hash,
            event_type=event_type,
            actor=AuditWriter.EXECUTION,
            occurred_at=occurred_at,
            payload=AuditPayload.from_mapping(payload),
        )

    def _verify_projection_history(self) -> None:
        if self._orders.keys() != self._intents.keys():
            raise ExecutionValidationError("restart intents and order projections differ")
        if any(event.client_order_id not in self._orders for event in self._order_events.values()):
            raise ExecutionValidationError("restart order event references a missing order")
        if any(fill.client_order_id not in self._orders for fill in self._fills.values()):
            raise ExecutionValidationError("restart fill references a missing order")
        broker_order_owners: dict[str, str] = {}
        events_by_client: dict[str, list[OrderEvent]] = {}
        for event in self._order_events.values():
            events_by_client.setdefault(event.client_order_id, []).append(event)
        for client_id, order in self._orders.items():
            if order.broker_order_id is not None:
                owner = broker_order_owners.setdefault(order.broker_order_id, client_id)
                if owner != client_id:
                    raise ExecutionValidationError("restart state reuses a broker order ID")
            events = sorted(events_by_client.get(client_id, []), key=lambda event: event.sequence)
            if tuple(event.sequence for event in events) != tuple(range(1, len(events) + 1)):
                raise ExecutionValidationError("restart order event sequence is not contiguous")
            if order.last_event_sequence != len(events):
                raise ExecutionValidationError("restart order projection is not at history head")
            expected_state = OrderState.INTENT_COMMITTED
            for event in events:
                if event.from_state is not expected_state:
                    raise ExecutionValidationError("restart order history has a broken state edge")
                expected_state = event.to_state
            if order.state is not expected_state or order.version != len(events) + 1:
                raise ExecutionValidationError("restart order projection disagrees with history")
            filled = sum(
                (
                    fill.quantity
                    for fill in self._fills.values()
                    if fill.client_order_id == client_id
                ),
                start=Decimal(0),
            )
            if order.cumulative_filled_quantity != filled:
                raise ExecutionValidationError("restart order projection disagrees with fills")
            _verify_fill_evidence(
                intent=self._intents[client_id],
                order=order,
                fills=self.fills_for_order(client_id),
                cumulative_quantity=order.cumulative_filled_quantity,
                average_price=order.average_fill_price,
                update_at=order.updated_at,
            )


_T = TypeVar("_T")


def _normalized_positions(positions: tuple[Position, ...]) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for position in positions:
        if position.symbol in normalized:
            raise ExecutionValidationError("positions contain a duplicate symbol")
        if position.quantity != 0:
            normalized[position.symbol] = position.quantity
    return normalized


def _baseline_cash(plan: ExecutionPlan) -> Decimal:
    prices = dict(plan.reference_prices)
    return plan.equity - sum(
        (position.quantity * prices[position.symbol] for position in plan.current_positions),
        start=Decimal(0),
    )


def _verify_fill_evidence(
    *,
    intent: OrderIntent,
    order: BrokerOrder,
    fills: tuple[Fill, ...],
    cumulative_quantity: Decimal,
    average_price: Decimal | None,
    update_at: datetime,
) -> None:
    if order.submitted_at is None and fills:
        raise ExecutionValidationError("fill evidence requires a persisted submission marker")
    for fill in fills:
        if (
            fill.client_order_id != intent.client_order_id
            or fill.symbol != intent.symbol
            or fill.side is not intent.side
        ):
            raise ExecutionValidationError("broker fill does not match its durable intent")
        if (
            fill.occurred_at < intent.created_at
            or (order.submitted_at is not None and fill.occurred_at < order.submitted_at)
            or fill.occurred_at > update_at
        ):
            raise ExecutionValidationError("broker fill timestamp is outside the order lifecycle")
    actual_quantity = sum((fill.quantity for fill in fills), start=Decimal(0))
    if actual_quantity != cumulative_quantity:
        raise ExecutionValidationError("broker cumulative fill disagrees with unique fills")
    expected_average = (
        None
        if actual_quantity == 0
        else sum((fill.quantity * fill.price for fill in fills), start=Decimal(0)) / actual_quantity
    )
    if average_price != expected_average:
        raise ExecutionValidationError("broker average fill price disagrees with unique fills")


def _unique_by(
    items: tuple[_T, ...],
    *,
    key: Callable[[_T], str],
    description: str,
) -> dict[str, _T]:
    result: dict[str, _T] = {}
    for item in items:
        identity = key(item)
        if identity in result:
            raise ExecutionValidationError(f"restart state contains duplicate {description}")
        result[identity] = item
    return result
