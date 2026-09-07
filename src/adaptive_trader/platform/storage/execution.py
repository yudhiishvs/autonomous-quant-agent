"""Transactional persistence for signed execution and reconciliation evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import TYPE_CHECKING, cast

from sqlalchemy import Connection, Engine, RowMapping, Table, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError, AuditValidationError
from adaptive_trader.platform.execution.authorization import (
    SubmissionAuthoritySnapshot,
    SubmissionLedgerSnapshot,
)
from adaptive_trader.platform.execution.models import (
    BrokerOrder,
    DiscrepancyCode,
    ExecutionPlan,
    ExecutionValidationError,
    Fill,
    Incident,
    IntentPhase,
    OrderEvent,
    OrderIntent,
    OrderSide,
    OrderState,
    Position,
    PositionEffect,
    ReconciliationDiscrepancy,
    ReconciliationReceipt,
    ReconciliationStatus,
)
from adaptive_trader.platform.execution.planner import (
    ExecutionPlanningRequest,
    plan_signed_orders,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.latches import (
    RiskLatchAction,
    RiskLatchError,
    RiskLatchEvent,
    RiskLatchKind,
    RiskLatchState,
)
from adaptive_trader.platform.risk.models import RiskDecision
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.risk import RiskPersistenceError, SignedRiskRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_broker_orders,
    aqa_execution_plans,
    aqa_fills,
    aqa_incidents,
    aqa_order_events,
    aqa_order_intents,
    aqa_reconciliations,
    aqa_risk_decisions,
    aqa_risk_latch_events,
)
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
)

if TYPE_CHECKING:
    from adaptive_trader.platform.execution.broker import BrokerUpdate
    from adaptive_trader.platform.execution.reconciliation import ReconciliationRequest

_PLAN_COLUMNS = frozenset(
    {
        "execution_plan_id",
        "risk_decision_id",
        "risk_decision_hash",
        "experiment_hash",
        "correlation_id",
        "target_version",
        "forced_flat",
        "targets",
        "current_positions",
        "reference_prices",
        "equity",
        "created_at",
        "deadline_at",
        "payload_hash",
        "signature",
        "content_hash",
    }
)
_INTENT_COLUMNS = frozenset(
    {
        "order_intent_id",
        "execution_plan_id",
        "risk_decision_id",
        "experiment_hash",
        "correlation_id",
        "client_order_id",
        "symbol",
        "side",
        "effect",
        "phase",
        "sequence",
        "target_version",
        "quantity",
        "notional",
        "reference_price",
        "final_target_quantity",
        "forced_flat",
        "order_type",
        "time_in_force",
        "created_at",
        "deadline_at",
        "target_hash",
        "payload_hash",
        "content_hash",
    }
)


class ExecutionPersistenceError(RuntimeError):
    """A durable execution operation failed without exposing database details."""


class SignedExecutionRepository:
    """Persist intent-first order state and fail-closed reconciliation atomically."""

    def __init__(
        self,
        engine: Engine,
        *,
        plan_table: Table = aqa_execution_plans,
        intent_table: Table = aqa_order_intents,
        order_table: Table = aqa_broker_orders,
        event_table: Table = aqa_order_events,
        fill_table: Table = aqa_fills,
        reconciliation_table: Table = aqa_reconciliations,
        incident_table: Table = aqa_incidents,
        latch_table: Table = aqa_risk_latch_events,
        decision_table: Table = aqa_risk_decisions,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("execution repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("execution repository requires PostgreSQL or SQLite")
        if engine.dialect.name == "sqlite":
            schema_map = engine.get_execution_options().get("schema_translate_map")
            if (
                not isinstance(schema_map, dict)
                or schema_map.get(PLATFORM_SCHEMA, object()) is not None
            ):
                raise ValueError("SQLite execution repository requires the platform schema map")
        required = (
            (plan_table, _PLAN_COLUMNS),
            (intent_table, _INTENT_COLUMNS),
            (order_table, frozenset(BrokerOrder.__dataclass_fields__)),
            (event_table, frozenset(OrderEvent.__dataclass_fields__) | {"payload", "payload_hash"}),
            (fill_table, frozenset(Fill.__dataclass_fields__) | {"payload_hash"}),
            (
                reconciliation_table,
                frozenset(ReconciliationReceipt.__dataclass_fields__) | {"payload_hash"},
            ),
            (incident_table, frozenset(Incident.__dataclass_fields__) | {"details"}),
            (
                latch_table,
                frozenset(RiskLatchEvent.__dataclass_fields__) | {"payload"},
            ),
            (
                decision_table,
                frozenset(
                    {
                        "risk_decision_id",
                        "experiment_hash",
                        "correlation_id",
                        "content_hash",
                    }
                ),
            ),
        )
        if any(
            not isinstance(table, Table) or columns.difference(table.c.keys())
            for table, columns in required
        ):
            raise ValueError("execution relation is missing the signed contract")
        self._engine = engine
        self._plans = plan_table
        self._intents = intent_table
        self._orders = order_table
        self._events = event_table
        self._fills = fill_table
        self._reconciliations = reconciliation_table
        self._incidents = incident_table
        self._latches = latch_table
        self._decisions = decision_table
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.EXECUTION)
        self._risk = SignedRiskRepository(
            engine,
            latch_table=latch_table,
            decision_table=decision_table,
        )

    @property
    def engine(self) -> Engine:
        """Return the concrete engine backing this repository."""

        return self._engine

    def transaction(self) -> AbstractContextManager[Connection]:
        """Open a serialized execution transaction."""

        return self._transactions.transaction()

    def persist_plan_and_intents(
        self,
        plan: ExecutionPlan,
        intents: tuple[OrderIntent, ...],
        *,
        risk_decision: RiskDecision,
    ) -> None:
        """Persist a risk-bound plan, all intents, projections, and audit evidence."""

        self._validate_plan_bundle(plan, intents)
        if type(risk_decision) is not RiskDecision:
            raise ExecutionValidationError("execution authorization is invalid")
        try:
            with self.transaction() as connection:
                self._transactions.acquire_postgres_advisory_locks(
                    connection,
                    [
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.EXECUTION_EXPERIMENT,
                            plan.experiment_hash,
                        ),
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.EXECUTION_PLAN,
                            plan.execution_plan_id,
                        ),
                        *(
                            PostgresAdvisoryLockRequest.for_resource(
                                PostgresAdvisoryLockNamespace.ORDER_CLIENT_ID,
                                intent.client_order_id,
                            )
                            for intent in intents
                        ),
                    ],
                )
                self._verify_authorized_bundle(
                    connection,
                    plan,
                    intents,
                    supplied_decision=risk_decision,
                )
                existing = self._plan(connection, plan.execution_plan_id)
                if existing is not None:
                    persisted = self._intents_for_plan(connection, plan.execution_plan_id)
                    if existing == plan and persisted == intents:
                        return
                    raise ExecutionPersistenceError("execution plan identity was reused")
                connection.execute(insert(self._plans).values(**_plan_row(plan)))
                for intent in intents:
                    connection.execute(insert(self._intents).values(**_intent_row(intent)))
                    connection.execute(
                        insert(self._orders).values(**_order_row(BrokerOrder.committed(intent)))
                    )
                self._append_audit(
                    connection,
                    experiment_hash=plan.experiment_hash,
                    event_type="execution.plan_persisted",
                    occurred_at=plan.created_at,
                    payload={
                        "content_hash": plan.content_hash,
                        "count": len(intents),
                        "execution_plan_id": plan.execution_plan_id,
                        "idempotency_key": plan.execution_plan_id,
                    },
                )
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise ExecutionPersistenceError("execution audit could not be persisted") from None
        except (DecimalException, IntegrityError, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("execution plan could not be persisted") from None

    def get_plan(self, execution_plan_id: str) -> ExecutionPlan:
        """Load and verify one immutable execution plan."""

        try:
            with self._engine.begin() as connection:
                plan = self._plan(connection, execution_plan_id)
                if plan is None:
                    raise ExecutionPersistenceError("execution plan does not exist")
                self._verify_authorized_bundle(
                    connection,
                    plan,
                    self._intents_for_plan(connection, plan.execution_plan_id),
                )
                return plan
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("execution plan could not be read safely") from None

    def get_intent(self, client_order_id: str) -> OrderIntent:
        """Load and verify one immutable order intent."""

        try:
            with self._engine.begin() as connection:
                return self._required_intent(connection, client_order_id)
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("order intent could not be read safely") from None

    def get_risk_decision(self, risk_decision_id: str) -> RiskDecision:
        """Load and verify one immutable risk authority."""

        try:
            with self._engine.begin() as connection:
                row = (
                    connection.execute(
                        select(
                            self._decisions.c.risk_decision_id,
                            self._decisions.c.signal_id,
                            self._decisions.c.content_hash,
                        ).where(self._decisions.c.risk_decision_id == risk_decision_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise ExecutionPersistenceError("risk decision does not exist")
                decision = self._risk.decision_by_id(
                    _string(row["risk_decision_id"]),
                    connection=connection,
                )
                if (
                    decision is None
                    or decision.risk_decision_id != risk_decision_id
                    or decision.content_hash != row["content_hash"]
                ):
                    raise ExecutionPersistenceError("risk decision is not authoritative")
                return decision
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (
            RiskPersistenceError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("risk decision could not be read safely") from None

    def intents_for_plan(self, execution_plan_id: str) -> tuple[OrderIntent, ...]:
        """Return one plan's hash-verified intents in submission order."""

        try:
            with self._engine.begin() as connection:
                plan = self._plan(connection, execution_plan_id)
                if plan is None:
                    raise ExecutionPersistenceError("execution plan does not exist")
                intents = self._intents_for_plan(connection, execution_plan_id)
                self._verify_authorized_bundle(connection, plan, intents)
                return intents
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("plan intents could not be read safely") from None

    def get_order(self, client_order_id: str) -> BrokerOrder:
        """Load an order projection and verify its complete durable history."""

        try:
            with self._engine.begin() as connection:
                order = self._required_order(connection, client_order_id, for_update=False)
                intent = self._required_intent(connection, client_order_id)
                self._verify_order_history(connection, order, intent)
                return order
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("broker order could not be read safely") from None

    def all_intents(self) -> tuple[OrderIntent, ...]:
        """Return hash-verified intents in deterministic order."""

        try:
            with self._engine.begin() as connection:
                rows = connection.execute(
                    select(self._intents).order_by(self._intents.c.client_order_id)
                ).mappings()
                intents = tuple(_intent_from_row(row) for row in rows)
                for plan_id in sorted({intent.execution_plan_id for intent in intents}):
                    plan = self._plan(connection, plan_id)
                    if plan is None:
                        raise ExecutionPersistenceError("order intent has no execution plan")
                    self._verify_authorized_bundle(
                        connection,
                        plan,
                        tuple(intent for intent in intents if intent.execution_plan_id == plan_id),
                    )
                return intents
        except (
            ExecutionValidationError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("order intents could not be read safely") from None

    def all_orders(self) -> tuple[BrokerOrder, ...]:
        """Return verified projections in deterministic order."""

        try:
            with self._engine.begin() as connection:
                rows = connection.execute(
                    select(self._orders).order_by(self._orders.c.client_order_id)
                ).mappings()
                orders = tuple(_order_from_row(row) for row in rows)
                for order in orders:
                    intent = self._required_intent(connection, order.client_order_id)
                    self._verify_order_history(connection, order, intent)
                return orders
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("broker orders could not be read safely") from None

    def order_events(self) -> tuple[OrderEvent, ...]:
        """Return immutable transition evidence in stream order."""

        try:
            with self._engine.begin() as connection:
                rows = connection.execute(
                    select(self._events).order_by(
                        self._events.c.client_order_id,
                        self._events.c.sequence,
                    )
                ).mappings()
                return tuple(_event_from_row(row) for row in rows)
        except (
            ExecutionValidationError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("order events could not be read safely") from None

    def fills(self) -> tuple[Fill, ...]:
        """Return unique fill evidence ordered by broker execution ID."""

        try:
            with self._engine.begin() as connection:
                rows = connection.execute(
                    select(self._fills).order_by(self._fills.c.broker_execution_id)
                ).mappings()
                return tuple(_fill_from_row(row) for row in rows)
        except (
            ExecutionValidationError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("fills could not be read safely") from None

    def fills_for_order(self, client_order_id: str) -> tuple[Fill, ...]:
        """Return one order's fills in occurrence and execution-ID order."""

        try:
            with self._engine.begin() as connection:
                return self._fills_for_order(connection, client_order_id)
        except (
            ExecutionValidationError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("order fills could not be read safely") from None

    def submission_ledger_snapshot(
        self,
        client_order_id: str,
    ) -> SubmissionLedgerSnapshot:
        """Return the durable state that must remain stable before a broker side effect."""

        try:
            with self._engine.begin() as connection:
                return self._submission_ledger_snapshot(connection, client_order_id)
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError(
                "submission ledger snapshot could not be read safely"
            ) from None

    def reconciliations(self) -> tuple[ReconciliationReceipt, ...]:
        """Return signed reconciliation receipts in deterministic order."""

        try:
            with self._engine.begin() as connection:
                rows = connection.execute(
                    select(self._reconciliations).order_by(
                        self._reconciliations.c.reconciliation_id
                    )
                ).mappings()
                return tuple(_reconciliation_from_row(row) for row in rows)
        except (
            ExecutionValidationError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("reconciliations could not be read safely") from None

    def incidents(self) -> tuple[Incident, ...]:
        """Return open reconciliation incidents in deterministic order."""

        try:
            with self._engine.begin() as connection:
                rows = connection.execute(
                    select(self._incidents).order_by(self._incidents.c.incident_id)
                ).mappings()
                return tuple(_incident_from_row(row) for row in rows)
        except (
            ExecutionValidationError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            raise ExecutionPersistenceError("incidents could not be read safely") from None

    def record_incident(self, incident: Incident) -> Incident:
        """Idempotently persist a standalone execution incident and audit evidence."""

        if type(incident) is not Incident:
            raise ExecutionValidationError("execution incident is invalid")
        try:
            with self.transaction() as connection:
                self._transactions.acquire_postgres_advisory_lock(
                    connection,
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.EXECUTION_EXPERIMENT,
                        incident.experiment_hash,
                    ),
                )
                inserted = self._append_incident(connection, incident)
                if inserted:
                    self._append_audit(
                        connection,
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
                return incident
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise ExecutionPersistenceError("incident audit could not be persisted") from None
        except (DecimalException, IntegrityError, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("execution incident could not be persisted") from None

    def has_ambiguous_order(self) -> bool:
        """Return whether durable state blocks any new exposure."""

        return any(
            order.state.ambiguous or order.state is OrderState.SUBMISSION_STARTED
            for order in self.all_orders()
        )

    def record_submission_started(
        self,
        client_order_id: str,
        *,
        started_at: datetime,
        authority: SubmissionAuthoritySnapshot,
    ) -> BrokerOrder:
        """Persist the final pre-side-effect boundary."""

        return self._record_local_transition(
            client_order_id,
            target=OrderState.SUBMISSION_STARTED,
            occurred_at=started_at,
            required=OrderState.INTENT_COMMITTED,
            submitted_at=started_at,
            submission_authority=authority,
        )

    def record_submission_unknown(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
        reason_code: str,
    ) -> BrokerOrder:
        """Persist an ambiguous side-effect outcome without retrying it."""

        return self._record_local_transition(
            client_order_id,
            target=OrderState.SUBMISSION_UNKNOWN,
            occurred_at=observed_at,
            required=OrderState.SUBMISSION_STARTED,
            safe_error_code=reason_code,
        )

    def record_reconciliation_required(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
        reason_code: str,
    ) -> BrokerOrder:
        """Move a nonterminal projection behind the reconciliation barrier."""

        return self._record_local_transition(
            client_order_id,
            target=OrderState.RECONCILIATION_REQUIRED,
            occurred_at=observed_at,
            safe_error_code=reason_code,
            prohibit_terminal=True,
        )

    def record_cancel_requested(
        self,
        client_order_id: str,
        *,
        requested_at: datetime,
    ) -> BrokerOrder:
        """Persist cancellation authority before any broker cancellation call."""

        return self._record_local_transition(
            client_order_id,
            target=OrderState.CANCEL_REQUESTED,
            occurred_at=requested_at,
            prohibit_terminal=True,
            prohibit_ambiguous=True,
        )

    def apply_broker_update(self, update_value: BrokerUpdate) -> BrokerOrder:
        """Atomically apply cumulative broker state and idempotent unique fills."""

        from adaptive_trader.platform.execution.broker import BrokerUpdate

        if type(update_value) is not BrokerUpdate:
            raise ExecutionValidationError("broker update is invalid")
        try:
            with self.transaction() as connection:
                intent = self._required_intent(connection, update_value.client_order_id)
                self._transactions.acquire_postgres_advisory_locks(
                    connection,
                    [
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.EXECUTION_EXPERIMENT,
                            intent.experiment_hash,
                        ),
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.ORDER_CLIENT_ID,
                            update_value.client_order_id,
                        ),
                    ],
                )
                previous = self._required_order(
                    connection,
                    update_value.client_order_id,
                    for_update=True,
                )
                self._verify_order_owner(previous, intent)
                self._validate_broker_update(connection, previous, intent, update_value)
                same_projection = (
                    previous.broker_order_id == update_value.broker_order_id
                    and previous.state is update_value.state
                    and previous.cumulative_filled_quantity
                    == update_value.cumulative_filled_quantity
                    and previous.average_fill_price == update_value.average_fill_price
                    and previous.safe_error_code == update_value.safe_error_code
                )
                if same_projection:
                    if any(
                        self._fill_by_execution_id(connection, fill.broker_execution_id) is None
                        for fill in update_value.fills
                    ):
                        raise ExecutionValidationError(
                            "unchanged broker projection cannot introduce new fill evidence"
                        )
                    return previous
                accepted_at = (
                    update_value.occurred_at
                    if update_value.state
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
                    state=update_value.state,
                    updated_at=update_value.occurred_at,
                    broker_order_id=update_value.broker_order_id,
                    accepted_at=accepted_at,
                    cumulative_filled_quantity=update_value.cumulative_filled_quantity,
                    average_fill_price=update_value.average_fill_price,
                    safe_error_code=update_value.safe_error_code,
                )
                self._persist_transition(
                    connection,
                    previous=previous,
                    current=current,
                    broker_event_id=update_value.broker_event_id,
                )
                for fill in update_value.fills:
                    if self._fill_by_execution_id(connection, fill.broker_execution_id) is None:
                        connection.execute(insert(self._fills).values(**_fill_row(fill)))
                        self._append_audit(
                            connection,
                            experiment_hash=intent.experiment_hash,
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
                return current
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise ExecutionPersistenceError("order audit could not be persisted") from None
        except (DecimalException, IntegrityError, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("broker update could not be persisted") from None

    def record_reconciliation_bundle(
        self,
        receipt: ReconciliationReceipt,
        *,
        request: ReconciliationRequest,
        latch_event: RiskLatchEvent | None,
        incident: Incident | None,
    ) -> None:
        """Atomically append reconciliation and all required blocking controls."""

        from adaptive_trader.platform.execution.reconciliation import ReconciliationRequest

        self._validate_reconciliation_bundle(receipt, latch_event=latch_event, incident=incident)
        if type(request) is not ReconciliationRequest:
            raise ExecutionValidationError("reconciliation request is invalid")
        try:
            with self.transaction() as connection:
                boundary_locks: list[PostgresAdvisoryLockRequest] = []
                if latch_event is not None:
                    boundary_locks.append(
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.RISK_LATCH,
                            f"{receipt.experiment_hash}:{latch_event.latch_type.value}",
                        )
                    )
                boundary_locks.append(
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.EXECUTION_EXPERIMENT,
                        receipt.experiment_hash,
                    )
                )
                self._transactions.acquire_postgres_advisory_locks(
                    connection,
                    boundary_locks,
                )

                locks: list[PostgresAdvisoryLockRequest] = []
                locked_client_ids: set[str] = set()
                if receipt.execution_plan_id is not None:
                    locks.append(
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.EXECUTION_PLAN,
                            receipt.execution_plan_id,
                        )
                    )
                    for intent in self._intents_for_plan(
                        connection,
                        receipt.execution_plan_id,
                    ):
                        locked_client_ids.add(intent.client_order_id)
                active_client_ids = connection.scalars(
                    select(self._orders.c.client_order_id)
                    .join(
                        self._intents,
                        self._intents.c.client_order_id == self._orders.c.client_order_id,
                    )
                    .where(
                        self._intents.c.experiment_hash == receipt.experiment_hash,
                        self._orders.c.state.not_in(
                            tuple(state.value for state in OrderState if state.terminal)
                        ),
                    )
                )
                locked_client_ids.update(active_client_ids)
                for client_order_id in sorted(locked_client_ids):
                    locks.append(
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.ORDER_CLIENT_ID,
                            client_order_id,
                        )
                    )
                locks.append(
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.RECONCILIATION,
                        receipt.reconciliation_id,
                    )
                )
                self._transactions.acquire_postgres_advisory_locks(connection, locks)
                self._verify_reconciliation_authority(
                    connection,
                    receipt=receipt,
                    request=request,
                )
                existing = self._reconciliation(connection, receipt.reconciliation_id)
                if existing is not None:
                    if existing != receipt:
                        raise ExecutionPersistenceError("reconciliation identity was reused")
                    self._verify_existing_bundle(
                        connection,
                        receipt=receipt,
                        latch_event=latch_event,
                        incident=incident,
                    )
                    return
                if latch_event is not None:
                    self._append_latch(connection, latch_event)
                if receipt.status is ReconciliationStatus.BLOCKING:
                    latch_state = RiskLatchState.from_events(
                        experiment_hash=receipt.experiment_hash,
                        events=self._latch_events(connection, receipt.experiment_hash),
                    )
                    if not latch_state.is_active(RiskLatchKind.RECONCILIATION):
                        raise ExecutionPersistenceError(
                            "blocking reconciliation requires an active latch"
                        )
                if incident is not None:
                    self._append_incident(connection, incident)
                connection.execute(
                    insert(self._reconciliations).values(**_reconciliation_row(receipt))
                )
                self._append_audit(
                    connection,
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
        except (ExecutionPersistenceError, ExecutionValidationError, RiskLatchError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise ExecutionPersistenceError("reconciliation audit could not be persisted") from None
        except (DecimalException, IntegrityError, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("reconciliation could not be persisted") from None

    def _record_local_transition(
        self,
        client_order_id: str,
        *,
        target: OrderState,
        occurred_at: datetime,
        required: OrderState | None = None,
        submitted_at: datetime | None = None,
        safe_error_code: str | None = None,
        prohibit_terminal: bool = False,
        prohibit_ambiguous: bool = False,
        submission_authority: SubmissionAuthoritySnapshot | None = None,
    ) -> BrokerOrder:
        try:
            with self.transaction() as connection:
                intent = self._required_intent(connection, client_order_id)
                self._transactions.acquire_postgres_advisory_locks(
                    connection,
                    [
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.EXECUTION_EXPERIMENT,
                            intent.experiment_hash,
                        ),
                        PostgresAdvisoryLockRequest.for_resource(
                            PostgresAdvisoryLockNamespace.ORDER_CLIENT_ID,
                            client_order_id,
                        ),
                    ],
                )
                previous = self._required_order(connection, client_order_id, for_update=True)
                self._verify_order_owner(previous, intent)
                if submission_authority is not None:
                    if (
                        type(submission_authority) is not SubmissionAuthoritySnapshot
                        or submission_authority.client_order_id != client_order_id
                        or self._submission_ledger_snapshot(
                            connection,
                            client_order_id,
                        ).content_hash
                        != submission_authority.ledger_snapshot_hash
                    ):
                        raise ExecutionValidationError("submission authority became stale")
                elif previous.state is target:
                    return previous
                if required is not None and previous.state is not required:
                    raise ExecutionValidationError("order cannot enter requested state")
                if prohibit_terminal and previous.state.terminal:
                    raise ExecutionValidationError("terminal order cannot enter requested state")
                if prohibit_ambiguous and previous.state.ambiguous:
                    raise ExecutionValidationError("ambiguous order cannot enter requested state")
                current = previous.evolve(
                    state=target,
                    updated_at=occurred_at,
                    submitted_at=submitted_at,
                    safe_error_code=safe_error_code,
                )
                self._persist_transition(
                    connection,
                    previous=previous,
                    current=current,
                    submission_authority_hash=(
                        submission_authority.content_hash
                        if submission_authority is not None
                        else None
                    ),
                )
                return current
        except (ExecutionPersistenceError, ExecutionValidationError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise ExecutionPersistenceError("order audit could not be persisted") from None
        except (DecimalException, IntegrityError, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise ExecutionPersistenceError("order transition could not be persisted") from None

    def _persist_transition(
        self,
        connection: Connection,
        *,
        previous: BrokerOrder,
        current: BrokerOrder,
        broker_event_id: str | None = None,
        submission_authority_hash: str | None = None,
    ) -> None:
        event = OrderEvent.from_orders(previous, current, broker_event_id=broker_event_id)
        result = connection.execute(
            update(self._orders)
            .where(
                self._orders.c.client_order_id == previous.client_order_id,
                self._orders.c.version == previous.version,
            )
            .values(**_order_row(current))
        )
        if result.rowcount != 1:
            raise ExecutionPersistenceError("broker order changed concurrently")
        connection.execute(insert(self._events).values(**_event_row(event)))
        intent = self._required_intent(connection, current.client_order_id)
        self._verify_order_owner(current, intent)
        self._append_audit(
            connection,
            experiment_hash=intent.experiment_hash,
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

    def _validate_broker_update(
        self,
        connection: Connection,
        previous: BrokerOrder,
        intent: OrderIntent,
        update_value: BrokerUpdate,
    ) -> None:
        if previous.state is OrderState.INTENT_COMMITTED:
            raise ExecutionValidationError("broker update arrived before submission started")
        if update_value.cumulative_filled_quantity > intent.quantity:
            raise ExecutionValidationError("broker cumulative fill exceeds order quantity")
        if (
            update_value.state is OrderState.FILLED
            and update_value.cumulative_filled_quantity != intent.quantity
        ):
            raise ExecutionValidationError("filled order must contain the complete intent quantity")
        if update_value.state is OrderState.PARTIALLY_FILLED and not (
            Decimal(0) < update_value.cumulative_filled_quantity < intent.quantity
        ):
            raise ExecutionValidationError(
                "partial-fill state requires an incomplete positive fill"
            )
        if (
            update_value.state
            in {
                OrderState.SUBMITTED,
                OrderState.ACCEPTED,
                OrderState.PENDING,
                OrderState.REJECTED,
            }
            and update_value.cumulative_filled_quantity != 0
        ):
            raise ExecutionValidationError("unfilled broker state cannot contain fills")
        event_owner = connection.scalar(
            select(self._events.c.client_order_id).where(
                self._events.c.broker_event_id == update_value.broker_event_id
            )
        )
        if event_owner is not None and event_owner != update_value.client_order_id:
            raise ExecutionValidationError("broker event ID was reused across orders")
        order_owner = connection.scalar(
            select(self._orders.c.client_order_id).where(
                self._orders.c.broker_order_id == update_value.broker_order_id
            )
        )
        if order_owner is not None and order_owner != update_value.client_order_id:
            raise ExecutionValidationError("broker order ID was reused across intents")
        if (
            previous.broker_order_id is not None
            and update_value.broker_order_id != previous.broker_order_id
        ):
            raise ExecutionValidationError("broker order ID cannot change once assigned")
        existing_fills = {
            fill.broker_execution_id: fill
            for fill in self._fills_for_order(connection, update_value.client_order_id)
        }
        for fill in update_value.fills:
            if (
                fill.client_order_id != intent.client_order_id
                or fill.symbol != intent.symbol
                or fill.side is not intent.side
            ):
                raise ExecutionValidationError("broker fill does not match its durable intent")
            existing = self._fill_by_execution_id(connection, fill.broker_execution_id)
            if existing is not None and existing != fill:
                raise ExecutionValidationError("broker execution ID was reused with new content")
            prior = existing_fills.setdefault(fill.broker_execution_id, fill)
            if prior != fill:
                raise ExecutionValidationError("broker execution ID was reused with new content")
        filled = sum((fill.quantity for fill in existing_fills.values()), start=Decimal(0))
        if filled != update_value.cumulative_filled_quantity:
            raise ExecutionValidationError("broker cumulative fill disagrees with unique fills")
        _verify_fill_evidence(
            intent=intent,
            order=previous,
            fills=tuple(existing_fills.values()),
            cumulative_quantity=update_value.cumulative_filled_quantity,
            average_price=update_value.average_fill_price,
            update_at=update_value.occurred_at,
        )

    def _validate_plan_bundle(
        self,
        plan: ExecutionPlan,
        intents: tuple[OrderIntent, ...],
    ) -> None:
        if type(plan) is not ExecutionPlan or type(intents) is not tuple:
            raise ExecutionValidationError("execution persistence requires validated inputs")
        if any(type(intent) is not OrderIntent for intent in intents):
            raise ExecutionValidationError("execution intents must be immutable")
        if any(
            intent.execution_plan_id != plan.execution_plan_id
            or intent.risk_decision_id != plan.risk_decision_id
            or intent.experiment_hash != plan.experiment_hash
            or intent.correlation_id != plan.correlation_id
            or intent.target_version != plan.target_version
            or intent.forced_flat != plan.forced_flat
            or intent.deadline_at != plan.deadline_at
            for intent in intents
        ):
            raise ExecutionValidationError("execution intent does not match its signed plan")
        if tuple(intent.sequence for intent in intents) != tuple(range(len(intents))):
            raise ExecutionValidationError("execution intent sequence must be contiguous")

    def _validate_reconciliation_bundle(
        self,
        receipt: ReconciliationReceipt,
        *,
        latch_event: RiskLatchEvent | None,
        incident: Incident | None,
    ) -> None:
        if type(receipt) is not ReconciliationReceipt:
            raise ExecutionValidationError("reconciliation receipt is invalid")
        if latch_event is not None and (
            type(latch_event) is not RiskLatchEvent
            or latch_event.experiment_hash != receipt.experiment_hash
            or latch_event.latch_type is not RiskLatchKind.RECONCILIATION
            or latch_event.action is not RiskLatchAction.ENGAGED
            or latch_event.correlation_id != receipt.correlation_id
            or latch_event.reason_code != "reconciliation_blocking"
            or latch_event.idempotency_key != f"reconciliation_{receipt.content_hash[:32]}"
            or latch_event.occurred_at != receipt.completed_at
        ):
            raise ExecutionValidationError("reconciliation latch does not match receipt")
        if incident is not None and (
            type(incident) is not Incident
            or incident.experiment_hash != receipt.experiment_hash
            or incident.correlation_id != receipt.correlation_id
            or incident.reason_code != "reconciliation_blocking"
            or incident.idempotency_key != f"reconciliation:{receipt.content_hash[:32]}"
            or incident.opened_at != receipt.completed_at
        ):
            raise ExecutionValidationError("reconciliation incident does not match receipt")
        if receipt.status is ReconciliationStatus.BLOCKING:
            if incident is None:
                raise ExecutionValidationError("blocking reconciliation requires an incident")
        elif latch_event is not None or incident is not None:
            raise ExecutionValidationError("clean reconciliation cannot create blocking controls")

    def _authoritative_risk_decision(
        self,
        connection: Connection,
        plan: ExecutionPlan,
    ) -> RiskDecision:
        row = (
            connection.execute(
                select(
                    self._decisions.c.risk_decision_id,
                    self._decisions.c.signal_id,
                    self._decisions.c.experiment_hash,
                    self._decisions.c.correlation_id,
                    self._decisions.c.content_hash,
                ).where(self._decisions.c.risk_decision_id == plan.risk_decision_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ExecutionPersistenceError(
                "execution plan does not match its signed risk decision"
            )
        try:
            decision = self._risk.decision_by_id(
                _string(row["risk_decision_id"]),
                connection=connection,
            )
        except RiskPersistenceError:
            raise ExecutionPersistenceError(
                "execution plan does not match its signed risk decision"
            ) from None
        if decision is None or (
            decision.risk_decision_id != plan.risk_decision_id
            or decision.experiment_hash != plan.experiment_hash
            or decision.correlation_id != plan.correlation_id
            or decision.content_hash != plan.risk_decision_hash
            or row["risk_decision_id"] != decision.risk_decision_id
            or row["experiment_hash"] != plan.experiment_hash
            or row["correlation_id"] != plan.correlation_id
            or row["content_hash"] != plan.risk_decision_hash
        ):
            raise ExecutionPersistenceError(
                "execution plan does not match its signed risk decision"
            )
        return decision

    def _verify_authorized_bundle(
        self,
        connection: Connection,
        plan: ExecutionPlan,
        intents: tuple[OrderIntent, ...],
        *,
        supplied_decision: RiskDecision | None = None,
    ) -> None:
        decision = self._authoritative_risk_decision(connection, plan)
        if supplied_decision is not None and supplied_decision != decision:
            raise ExecutionPersistenceError(
                "execution bundle does not match its signed risk authorization"
            )
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
        if expected.plan != plan or expected.intents != intents:
            raise ExecutionPersistenceError(
                "execution bundle does not match its signed risk authorization"
            )

    def _verify_order_history(
        self,
        connection: Connection,
        order: BrokerOrder,
        intent: OrderIntent,
    ) -> None:
        self._verify_order_owner(order, intent)
        rows = connection.execute(
            select(self._events)
            .where(self._events.c.client_order_id == order.client_order_id)
            .order_by(self._events.c.sequence)
        ).mappings()
        events = tuple(_event_from_row(row) for row in rows)
        if tuple(event.sequence for event in events) != tuple(range(1, len(events) + 1)):
            raise ExecutionPersistenceError("order event sequence is not contiguous")
        expected = OrderState.INTENT_COMMITTED
        for event in events:
            if event.from_state is not expected:
                raise ExecutionPersistenceError("order event history has a broken state edge")
            expected = event.to_state
        if (
            order.last_event_sequence != len(events)
            or order.version != len(events) + 1
            or order.state is not expected
        ):
            raise ExecutionPersistenceError("order projection disagrees with event history")
        fills = self._fills_for_order(connection, order.client_order_id)
        if any(
            fill.client_order_id != intent.client_order_id
            or fill.symbol != intent.symbol
            or fill.side is not intent.side
            for fill in fills
        ):
            raise ExecutionPersistenceError("persisted fill does not match its order intent")
        filled = sum((fill.quantity for fill in fills), start=Decimal(0))
        if filled != order.cumulative_filled_quantity:
            raise ExecutionPersistenceError("order projection disagrees with fill history")
        if filled > intent.quantity:
            raise ExecutionPersistenceError("persisted fills exceed their order intent")
        if order.state is OrderState.FILLED and filled != intent.quantity:
            raise ExecutionPersistenceError("filled projection is incomplete")
        if order.state is OrderState.PARTIALLY_FILLED and not Decimal(0) < filled < intent.quantity:
            raise ExecutionPersistenceError("partial-fill projection is inconsistent")
        try:
            _verify_fill_evidence(
                intent=intent,
                order=order,
                fills=fills,
                cumulative_quantity=order.cumulative_filled_quantity,
                average_price=order.average_fill_price,
                update_at=order.updated_at,
            )
        except ExecutionValidationError:
            raise ExecutionPersistenceError("persisted fill evidence is inconsistent") from None

    def _verify_reconciliation_authority(
        self,
        connection: Connection,
        *,
        receipt: ReconciliationReceipt,
        request: ReconciliationRequest,
    ) -> None:
        from adaptive_trader.platform.execution.reconciliation import reconcile

        if receipt.execution_plan_id is None:
            raise ExecutionPersistenceError(
                "persisted reconciliation requires an authoritative execution plan"
            )
        plan = self._plan(connection, receipt.execution_plan_id)
        if plan is None:
            raise ExecutionPersistenceError("reconciliation execution plan does not exist")
        plan_intents = self._intents_for_plan(connection, plan.execution_plan_id)
        self._verify_authorized_bundle(connection, plan, plan_intents)
        decision = self._authoritative_risk_decision(connection, plan)
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
            receipt.experiment_hash != plan.experiment_hash
            or receipt.slot_id != decision.slot_id
            or receipt.correlation_id != plan.correlation_id
            or request.execution_plan_id != plan.execution_plan_id
            or request.experiment_hash != plan.experiment_hash
            or request.slot_id != decision.slot_id
            or request.correlation_id != plan.correlation_id
            or request.active_symbols != active_symbols
            or request.short_eligible_symbols != short_eligible_symbols
            or _normalized_positions(request.baseline_positions)
            != _normalized_positions(plan.current_positions)
            or request.baseline_cash != decision.account_snapshot.cash
            or request.expected_account_id_hash != decision.account_snapshot.account_id_hash
            or request.mark_prices != plan.reference_prices
        ):
            raise ExecutionPersistenceError(
                "reconciliation request does not match its execution authority"
            )

        authoritative_orders: list[BrokerOrder] = []
        authoritative_intents: dict[str, OrderIntent] = {
            intent.client_order_id: intent for intent in plan_intents
        }
        order_rows = connection.execute(
            select(self._orders).order_by(self._orders.c.client_order_id)
        ).mappings()
        for row in order_rows:
            order = _order_from_row(row)
            intent = self._required_intent(connection, order.client_order_id)
            owner_plan = self._plan(connection, intent.execution_plan_id)
            if owner_plan is None or owner_plan.experiment_hash != plan.experiment_hash:
                continue
            self._verify_order_history(connection, order, intent)
            authoritative_orders.append(order)
            authoritative_intents[intent.client_order_id] = intent

        current_fills = tuple(
            fill
            for intent in plan_intents
            for fill in self._fills_for_order(connection, intent.client_order_id)
        )
        order_fills = tuple(
            fill
            for order in authoritative_orders
            for fill in self._fills_for_order(connection, order.client_order_id)
        )
        authoritative_request = replace(
            request,
            active_symbols=active_symbols,
            short_eligible_symbols=short_eligible_symbols,
            baseline_positions=plan.current_positions,
            baseline_cash=decision.account_snapshot.cash,
            fills=current_fills,
            order_fills=order_fills,
            intents=tuple(authoritative_intents[key] for key in sorted(authoritative_intents)),
            durable_orders=tuple(authoritative_orders),
            expected_account_id_hash=decision.account_snapshot.account_id_hash,
            mark_prices=plan.reference_prices,
        )
        if reconcile(authoritative_request) != receipt:
            raise ExecutionPersistenceError(
                "reconciliation receipt does not match authoritative durable state"
            )

    @staticmethod
    def _verify_order_owner(order: BrokerOrder, intent: OrderIntent) -> None:
        if (
            order.client_order_id != intent.client_order_id
            or order.order_intent_id != intent.order_intent_id
        ):
            raise ExecutionPersistenceError("broker order does not match its authoritative intent")

    def _append_latch(self, connection: Connection, event: RiskLatchEvent) -> None:
        events = self._latch_events(connection, event.experiment_hash)
        for prior in events:
            if prior.latch_event_id == event.latch_event_id or (
                prior.latch_type is event.latch_type
                and prior.idempotency_key == event.idempotency_key
            ):
                if prior == event:
                    return
                raise ExecutionPersistenceError("latch identity was reused")
        before = RiskLatchState.from_events(
            experiment_hash=event.experiment_hash,
            events=events,
        )
        if event.sequence != before.next_sequence(event.latch_type):
            raise ExecutionPersistenceError("latch event sequence is stale or noncontiguous")
        RiskLatchState.from_events(experiment_hash=event.experiment_hash, events=(*events, event))
        connection.execute(insert(self._latches).values(**_latch_row(event)))
        self._append_audit(
            connection,
            experiment_hash=event.experiment_hash,
            event_type="latch.engaged",
            occurred_at=event.occurred_at,
            stream_id=f"aqa_execution:latch:{event.experiment_hash}:{event.latch_type.value}",
            payload={
                "event_ids": [event.latch_event_id],
                "idempotency_key": event.latch_event_id,
                "reason_code": event.reason_code,
                "sequence": event.sequence,
            },
        )

    def _append_incident(self, connection: Connection, incident: Incident) -> bool:
        existing = (
            connection.execute(
                select(self._incidents).where(
                    self._incidents.c.idempotency_key == incident.idempotency_key
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if _incident_from_row(existing) == incident:
                return False
            raise ExecutionPersistenceError("incident idempotency key was reused")
        connection.execute(insert(self._incidents).values(**_incident_row(incident)))
        return True

    def _verify_existing_bundle(
        self,
        connection: Connection,
        *,
        receipt: ReconciliationReceipt,
        latch_event: RiskLatchEvent | None,
        incident: Incident | None,
    ) -> None:
        if latch_event is not None and latch_event not in self._latch_events(
            connection, latch_event.experiment_hash
        ):
            raise ExecutionPersistenceError("reconciliation retry is missing latch evidence")
        if incident is not None:
            row = (
                connection.execute(
                    select(self._incidents).where(
                        self._incidents.c.idempotency_key == incident.idempotency_key
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or _incident_from_row(row) != incident:
                raise ExecutionPersistenceError("reconciliation retry is missing incident evidence")
        if receipt.status is ReconciliationStatus.BLOCKING:
            state = RiskLatchState.from_events(
                experiment_hash=receipt.experiment_hash,
                events=self._latch_events(connection, receipt.experiment_hash),
            )
            if not state.is_active(RiskLatchKind.RECONCILIATION):
                raise ExecutionPersistenceError("reconciliation retry is missing its active latch")

    def _append_audit(
        self,
        connection: Connection,
        *,
        experiment_hash: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
        stream_id: str | None = None,
    ) -> None:
        self._audit.append(
            stream_id=stream_id or f"aqa_execution:{experiment_hash}",
            event_type=event_type,
            occurred_at=occurred_at,
            payload=AuditPayload.from_mapping(dict(payload)),
            connection=connection,
        )

    def _lock(
        self,
        connection: Connection,
        namespace: PostgresAdvisoryLockNamespace,
        resource: str,
    ) -> None:
        self._transactions.acquire_postgres_advisory_lock(
            connection,
            PostgresAdvisoryLockRequest.for_resource(namespace, resource),
        )

    def _plan(self, connection: Connection, execution_plan_id: str) -> ExecutionPlan | None:
        row = (
            connection.execute(
                select(self._plans).where(self._plans.c.execution_plan_id == execution_plan_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _plan_from_row(row)

    def _intents_for_plan(
        self,
        connection: Connection,
        execution_plan_id: str,
    ) -> tuple[OrderIntent, ...]:
        rows = connection.execute(
            select(self._intents)
            .where(self._intents.c.execution_plan_id == execution_plan_id)
            .order_by(self._intents.c.sequence)
        ).mappings()
        return tuple(_intent_from_row(row) for row in rows)

    def _required_intent(self, connection: Connection, client_order_id: str) -> OrderIntent:
        row = (
            connection.execute(
                select(self._intents).where(self._intents.c.client_order_id == client_order_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ExecutionPersistenceError("order intent does not exist")
        intent = _intent_from_row(row)
        plan = self._plan(connection, intent.execution_plan_id)
        if plan is None or (
            plan.risk_decision_id != intent.risk_decision_id
            or plan.experiment_hash != intent.experiment_hash
            or plan.correlation_id != intent.correlation_id
            or plan.target_version != intent.target_version
            or plan.forced_flat != intent.forced_flat
            or plan.deadline_at != intent.deadline_at
        ):
            raise ExecutionPersistenceError("order intent does not match its signed plan")
        self._verify_authorized_bundle(
            connection,
            plan,
            self._intents_for_plan(connection, plan.execution_plan_id),
        )
        return intent

    def _required_order(
        self,
        connection: Connection,
        client_order_id: str,
        *,
        for_update: bool,
    ) -> BrokerOrder:
        statement = select(self._orders).where(self._orders.c.client_order_id == client_order_id)
        if for_update and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise ExecutionPersistenceError("broker order does not exist")
        return _order_from_row(row)

    def _fills_for_order(self, connection: Connection, client_order_id: str) -> tuple[Fill, ...]:
        rows = connection.execute(
            select(self._fills)
            .where(self._fills.c.client_order_id == client_order_id)
            .order_by(self._fills.c.occurred_at, self._fills.c.broker_execution_id)
        ).mappings()
        return tuple(_fill_from_row(row) for row in rows)

    def _fill_by_execution_id(
        self,
        connection: Connection,
        broker_execution_id: str,
    ) -> Fill | None:
        row = (
            connection.execute(
                select(self._fills).where(self._fills.c.broker_execution_id == broker_execution_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _fill_from_row(row)

    def _reconciliation(
        self,
        connection: Connection,
        reconciliation_id: str,
    ) -> ReconciliationReceipt | None:
        row = (
            connection.execute(
                select(self._reconciliations).where(
                    self._reconciliations.c.reconciliation_id == reconciliation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _reconciliation_from_row(row)

    def _submission_ledger_snapshot(
        self,
        connection: Connection,
        client_order_id: str,
    ) -> SubmissionLedgerSnapshot:
        intent = self._required_intent(connection, client_order_id)
        plan = self._plan(connection, intent.execution_plan_id)
        if plan is None:
            raise ExecutionPersistenceError("order intent has no execution plan")
        decision = self._authoritative_risk_decision(connection, plan)
        plan_intents = self._intents_for_plan(connection, plan.execution_plan_id)
        plan_client_ids = tuple(item.client_order_id for item in plan_intents)
        fill_rows = (
            connection.execute(
                select(self._fills).where(self._fills.c.client_order_id.in_(plan_client_ids))
            )
            .mappings()
            .all()
            if plan_client_ids
            else []
        )
        active_rows = (
            connection.execute(
                select(self._orders)
                .join(
                    self._intents,
                    self._intents.c.client_order_id == self._orders.c.client_order_id,
                )
                .where(
                    self._intents.c.experiment_hash == plan.experiment_hash,
                    self._orders.c.state.not_in(
                        tuple(state.value for state in OrderState if state.terminal)
                    ),
                )
            )
            .mappings()
            .all()
        )
        latch_state = RiskLatchState.from_events(
            experiment_hash=plan.experiment_hash,
            events=self._latch_events(connection, plan.experiment_hash),
        )
        return SubmissionLedgerSnapshot.create(
            client_order_id=client_order_id,
            experiment_hash=plan.experiment_hash,
            execution_plan_id=plan.execution_plan_id,
            risk_decision_hash=decision.content_hash,
            intent_hash=intent.content_hash,
            order_hash=self._required_order(
                connection,
                client_order_id,
                for_update=False,
            ).content_hash,
            plan_fill_hashes=tuple(sorted(_fill_from_row(row).content_hash for row in fill_rows)),
            active_order_hashes=tuple(
                sorted(_order_from_row(row).content_hash for row in active_rows)
            ),
            active_latches=latch_state.active,
        )

    def _latch_events(
        self,
        connection: Connection,
        experiment_hash: str,
    ) -> tuple[RiskLatchEvent, ...]:
        rows = connection.execute(
            select(self._latches)
            .where(self._latches.c.experiment_hash == experiment_hash)
            .order_by(self._latches.c.latch_type, self._latches.c.sequence)
        ).mappings()
        return tuple(_latch_from_row(row) for row in rows)


def _normalized_positions(positions: tuple[Position, ...]) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for position in positions:
        if position.symbol in normalized:
            raise ExecutionValidationError("positions contain a duplicate symbol")
        if position.quantity != 0:
            normalized[position.symbol] = position.quantity
    return normalized


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


def _plan_row(plan: ExecutionPlan) -> dict[str, object]:
    return {
        "execution_plan_id": plan.execution_plan_id,
        "risk_decision_id": plan.risk_decision_id,
        "risk_decision_hash": plan.risk_decision_hash,
        "experiment_hash": plan.experiment_hash,
        "correlation_id": plan.correlation_id,
        "target_version": plan.target_version,
        "forced_flat": plan.forced_flat,
        "targets": _positions_row(plan.target_quantities),
        "current_positions": _positions_row(plan.current_positions),
        "reference_prices": _decimal_pairs_row(plan.reference_prices),
        "equity": plan.equity,
        "created_at": plan.created_at,
        "deadline_at": plan.deadline_at,
        "payload_hash": plan.content_hash,
        "signature": plan.content_hash,
        "content_hash": plan.content_hash,
    }


def _plan_from_row(row: RowMapping) -> ExecutionPlan:
    plan = ExecutionPlan(
        execution_plan_id=_string(row["execution_plan_id"]),
        risk_decision_id=_string(row["risk_decision_id"]),
        risk_decision_hash=_string(row["risk_decision_hash"]),
        experiment_hash=_string(row["experiment_hash"]),
        correlation_id=_string(row["correlation_id"]),
        target_version=_integer(row["target_version"]),
        forced_flat=_boolean(row["forced_flat"]),
        target_quantities=_positions(row["targets"]),
        current_positions=_positions(row["current_positions"]),
        reference_prices=_decimal_pairs(row["reference_prices"]),
        equity=_decimal(row["equity"]),
        created_at=_datetime(row["created_at"]),
        deadline_at=_datetime(row["deadline_at"]),
        content_hash=_string(row["content_hash"]),
    )
    if row["payload_hash"] != plan.content_hash or row["signature"] != plan.content_hash:
        raise ExecutionPersistenceError("persisted execution plan proof is inconsistent")
    return plan


def _intent_row(intent: OrderIntent) -> dict[str, object]:
    return {
        "order_intent_id": intent.order_intent_id,
        "execution_plan_id": intent.execution_plan_id,
        "risk_decision_id": intent.risk_decision_id,
        "experiment_hash": intent.experiment_hash,
        "correlation_id": intent.correlation_id,
        "client_order_id": intent.client_order_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "effect": intent.position_effect.value,
        "phase": intent.phase.value,
        "sequence": intent.sequence,
        "target_version": intent.target_version,
        "quantity": intent.quantity,
        "notional": intent.notional,
        "reference_price": intent.reference_price,
        "final_target_quantity": intent.final_target_quantity,
        "forced_flat": intent.forced_flat,
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "created_at": intent.created_at,
        "deadline_at": intent.deadline_at,
        "target_hash": intent.target_hash,
        "payload_hash": intent.content_hash,
        "content_hash": intent.content_hash,
    }


def _intent_from_row(row: RowMapping) -> OrderIntent:
    intent = OrderIntent(
        order_intent_id=_string(row["order_intent_id"]),
        execution_plan_id=_string(row["execution_plan_id"]),
        risk_decision_id=_string(row["risk_decision_id"]),
        experiment_hash=_string(row["experiment_hash"]),
        correlation_id=_string(row["correlation_id"]),
        client_order_id=_string(row["client_order_id"]),
        symbol=_string(row["symbol"]),
        side=OrderSide(_string(row["side"])),
        position_effect=PositionEffect(_string(row["effect"])),
        phase=IntentPhase(_string(row["phase"])),
        sequence=_integer(row["sequence"]),
        target_version=_integer(row["target_version"]),
        quantity=_decimal(row["quantity"]),
        notional=_decimal(row["notional"]),
        reference_price=_decimal(row["reference_price"]),
        final_target_quantity=_decimal(row["final_target_quantity"]),
        forced_flat=_boolean(row["forced_flat"]),
        created_at=_datetime(row["created_at"]),
        deadline_at=_datetime(row["deadline_at"]),
        target_hash=_string(row["target_hash"]),
        content_hash=_string(row["content_hash"]),
    )
    if (
        row["order_type"] != "MARKET"
        or row["time_in_force"] != "DAY"
        or row["payload_hash"] != intent.content_hash
    ):
        raise ExecutionPersistenceError("persisted order intent proof is inconsistent")
    return intent


def _order_row(order: BrokerOrder) -> dict[str, object]:
    return {
        "client_order_id": order.client_order_id,
        "order_intent_id": order.order_intent_id,
        "broker_order_id": order.broker_order_id,
        "state": order.state.value,
        "submitted_at": order.submitted_at,
        "accepted_at": order.accepted_at,
        "updated_at": order.updated_at,
        "cumulative_filled_quantity": order.cumulative_filled_quantity,
        "average_fill_price": order.average_fill_price,
        "last_event_sequence": order.last_event_sequence,
        "safe_error_code": order.safe_error_code,
        "content_hash": order.content_hash,
        "version": order.version,
    }


def _order_from_row(row: RowMapping) -> BrokerOrder:
    return BrokerOrder(
        client_order_id=_string(row["client_order_id"]),
        order_intent_id=_string(row["order_intent_id"]),
        broker_order_id=_optional_string(row["broker_order_id"]),
        state=OrderState(_string(row["state"])),
        submitted_at=_optional_datetime(row["submitted_at"]),
        accepted_at=_optional_datetime(row["accepted_at"]),
        updated_at=_datetime(row["updated_at"]),
        cumulative_filled_quantity=_decimal(row["cumulative_filled_quantity"]),
        average_fill_price=_optional_decimal(row["average_fill_price"]),
        last_event_sequence=_integer(row["last_event_sequence"]),
        safe_error_code=_optional_string(row["safe_error_code"]),
        version=_integer(row["version"]),
        content_hash=_string(row["content_hash"]),
    )


def _event_payload(event: OrderEvent) -> dict[str, object]:
    return {
        "broker_event_id": event.broker_event_id,
        "client_order_id": event.client_order_id,
        "from_state": event.from_state.value,
        "occurred_at": _timestamp(event.occurred_at),
        "safe_error_code": event.safe_error_code,
        "schema": "order-event-v1",
        "sequence": event.sequence,
        "to_state": event.to_state.value,
    }


def _event_row(event: OrderEvent) -> dict[str, object]:
    payload = _event_payload(event)
    return {
        "order_event_id": event.order_event_id,
        "client_order_id": event.client_order_id,
        "sequence": event.sequence,
        "from_state": event.from_state.value,
        "to_state": event.to_state.value,
        "broker_event_id": event.broker_event_id,
        "occurred_at": event.occurred_at,
        "safe_error_code": event.safe_error_code,
        "payload": payload,
        "payload_hash": sha256_hex(payload),
        "content_hash": event.content_hash,
    }


def _event_from_row(row: RowMapping) -> OrderEvent:
    event = OrderEvent(
        order_event_id=_string(row["order_event_id"]),
        client_order_id=_string(row["client_order_id"]),
        sequence=_integer(row["sequence"]),
        from_state=OrderState(_string(row["from_state"])),
        to_state=OrderState(_string(row["to_state"])),
        broker_event_id=_optional_string(row["broker_event_id"]),
        occurred_at=_datetime(row["occurred_at"]),
        safe_error_code=_optional_string(row["safe_error_code"]),
        content_hash=_string(row["content_hash"]),
    )
    payload = _mapping(row["payload"])
    if payload != _event_payload(event) or row["payload_hash"] != sha256_hex(payload):
        raise ExecutionPersistenceError("persisted order event payload is inconsistent")
    return event


def _fill_row(fill: Fill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "client_order_id": fill.client_order_id,
        "broker_execution_id": fill.broker_execution_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": fill.price,
        "fee": fill.fee,
        "occurred_at": fill.occurred_at,
        "payload_hash": fill.content_hash,
        "content_hash": fill.content_hash,
    }


def _fill_from_row(row: RowMapping) -> Fill:
    fill = Fill(
        fill_id=_string(row["fill_id"]),
        client_order_id=_string(row["client_order_id"]),
        broker_execution_id=_string(row["broker_execution_id"]),
        symbol=_string(row["symbol"]),
        side=OrderSide(_string(row["side"])),
        quantity=_decimal(row["quantity"]),
        price=_decimal(row["price"]),
        fee=_decimal(row["fee"]),
        occurred_at=_datetime(row["occurred_at"]),
        content_hash=_string(row["content_hash"]),
    )
    if row["payload_hash"] != fill.content_hash:
        raise ExecutionPersistenceError("persisted fill proof is inconsistent")
    return fill


def _reconciliation_row(receipt: ReconciliationReceipt) -> dict[str, object]:
    return {
        "reconciliation_id": receipt.reconciliation_id,
        "experiment_hash": receipt.experiment_hash,
        "slot_id": receipt.slot_id,
        "execution_plan_id": receipt.execution_plan_id,
        "correlation_id": receipt.correlation_id,
        "account_id_hash": receipt.account_id_hash,
        "account_observed_at": receipt.account_observed_at,
        "started_at": receipt.started_at,
        "completed_at": receipt.completed_at,
        "status": receipt.status.value,
        "expected_positions": _positions_row(receipt.expected_positions),
        "observed_positions": _positions_row(receipt.observed_positions),
        "expected_cash": receipt.expected_cash,
        "observed_cash": receipt.observed_cash,
        "expected_equity": receipt.expected_equity,
        "observed_equity": receipt.observed_equity,
        "mark_prices": _decimal_pairs_row(receipt.mark_prices),
        "fill_hashes": list(receipt.fill_hashes),
        "order_hashes": list(receipt.order_hashes),
        "require_flat": receipt.require_flat,
        "required_flat_at": receipt.required_flat_at,
        "discrepancies": [_discrepancy_row(item) for item in receipt.discrepancies],
        "payload_hash": receipt.content_hash,
        "content_hash": receipt.content_hash,
    }


def _reconciliation_from_row(row: RowMapping) -> ReconciliationReceipt:
    receipt = ReconciliationReceipt(
        reconciliation_id=_string(row["reconciliation_id"]),
        experiment_hash=_string(row["experiment_hash"]),
        slot_id=_optional_string(row["slot_id"]),
        execution_plan_id=_optional_string(row["execution_plan_id"]),
        correlation_id=_string(row["correlation_id"]),
        account_id_hash=_string(row["account_id_hash"]),
        account_observed_at=_datetime(row["account_observed_at"]),
        started_at=_datetime(row["started_at"]),
        completed_at=_datetime(row["completed_at"]),
        status=ReconciliationStatus(_string(row["status"])),
        expected_positions=_positions(row["expected_positions"]),
        observed_positions=_positions(row["observed_positions"]),
        expected_cash=_decimal(row["expected_cash"]),
        observed_cash=_decimal(row["observed_cash"]),
        expected_equity=_decimal(row["expected_equity"]),
        observed_equity=_decimal(row["observed_equity"]),
        mark_prices=_decimal_pairs(row["mark_prices"]),
        fill_hashes=_string_tuple(row["fill_hashes"]),
        order_hashes=_string_tuple(row["order_hashes"]),
        require_flat=_boolean(row["require_flat"]),
        required_flat_at=_optional_datetime(row["required_flat_at"]),
        discrepancies=tuple(
            _discrepancy_from_value(item)
            for item in _list(row["discrepancies"], field_name="discrepancies")
        ),
        content_hash=_string(row["content_hash"]),
    )
    if row["payload_hash"] != receipt.content_hash:
        raise ExecutionPersistenceError("persisted reconciliation proof is inconsistent")
    return receipt


def _incident_row(incident: Incident) -> dict[str, object]:
    return {
        "incident_id": incident.incident_id,
        "idempotency_key": incident.idempotency_key,
        "experiment_hash": incident.experiment_hash,
        "correlation_id": incident.correlation_id,
        "incident_type": "execution_reconciliation",
        "severity": "critical",
        "status": "open",
        "reason_code": incident.reason_code,
        "details": {"contract": "execution-incident-v1"},
        "opened_at": incident.opened_at,
        "resolved_at": None,
        "content_hash": incident.content_hash,
        "version": 1,
    }


def _incident_from_row(row: RowMapping) -> Incident:
    incident = Incident(
        incident_id=_string(row["incident_id"]),
        idempotency_key=_string(row["idempotency_key"]),
        experiment_hash=_string(row["experiment_hash"]),
        correlation_id=_string(row["correlation_id"]),
        reason_code=_string(row["reason_code"]),
        opened_at=_datetime(row["opened_at"]),
        content_hash=_string(row["content_hash"]),
    )
    if (
        row["incident_type"] != "execution_reconciliation"
        or row["severity"] != "critical"
        or row["status"] != "open"
        or row["resolved_at"] is not None
        or row["version"] != 1
        or _mapping(row["details"]) != {"contract": "execution-incident-v1"}
    ):
        raise ExecutionPersistenceError("persisted execution incident is inconsistent")
    return incident


def _latch_row(event: RiskLatchEvent) -> dict[str, object]:
    payload = _latch_payload(event)
    return {
        "latch_event_id": event.latch_event_id,
        "experiment_hash": event.experiment_hash,
        "latch_type": event.latch_type.value,
        "sequence": event.sequence,
        "action": event.action.value,
        "correlation_id": event.correlation_id,
        "idempotency_key": event.idempotency_key,
        "reason_code": event.reason_code,
        "actor": event.actor,
        "occurred_at": event.occurred_at,
        "payload": payload,
        "payload_hash": event.payload_hash,
        "content_hash": event.content_hash,
    }


def _latch_payload(event: RiskLatchEvent) -> dict[str, object]:
    return {
        "action": event.action.value,
        "actor": event.actor,
        "correlation_id": event.correlation_id,
        "experiment_hash": event.experiment_hash,
        "idempotency_key": event.idempotency_key,
        "latch_type": event.latch_type.value,
        "occurred_at": _timestamp(event.occurred_at),
        "reason_code": event.reason_code,
        "schema": "risk-latch-event-v1",
        "sequence": event.sequence,
    }


def _latch_from_row(row: RowMapping) -> RiskLatchEvent:
    event = RiskLatchEvent(
        latch_event_id=_string(row["latch_event_id"]),
        experiment_hash=_string(row["experiment_hash"]),
        latch_type=RiskLatchKind(_string(row["latch_type"])),
        sequence=_integer(row["sequence"]),
        action=RiskLatchAction(_string(row["action"])),
        reason_code=_string(row["reason_code"]),
        actor=_string(row["actor"]),
        occurred_at=_datetime(row["occurred_at"]),
        correlation_id=_string(row["correlation_id"]),
        idempotency_key=_string(row["idempotency_key"]),
        payload_hash=_string(row["payload_hash"]),
        content_hash=_string(row["content_hash"]),
    )
    if _mapping(row["payload"]) != _latch_payload(event):
        raise ExecutionPersistenceError("persisted reconciliation latch is inconsistent")
    return event


def _positions_row(positions: Sequence[Position]) -> list[list[str]]:
    return [[position.symbol, format(position.quantity, "f")] for position in positions]


def _decimal_pairs_row(values: Sequence[tuple[str, Decimal]]) -> list[list[str]]:
    return [[name, format(value, "f")] for name, value in values]


def _positions(value: object) -> tuple[Position, ...]:
    result: list[Position] = []
    for raw in _list(value, field_name="positions"):
        item = _list(raw, field_name="position")
        if len(item) != 2:
            raise ExecutionPersistenceError("persisted position is malformed")
        result.append(Position(symbol=_string(item[0]), quantity=_decimal(item[1])))
    return tuple(result)


def _decimal_pairs(value: object) -> tuple[tuple[str, Decimal], ...]:
    result: list[tuple[str, Decimal]] = []
    for raw in _list(value, field_name="decimal pairs"):
        item = _list(raw, field_name="decimal pair")
        if len(item) != 2:
            raise ExecutionPersistenceError("persisted decimal pair is malformed")
        result.append((_string(item[0]), _decimal(item[1])))
    return tuple(result)


def _discrepancy_row(item: ReconciliationDiscrepancy) -> dict[str, object]:
    return {
        "client_order_id": item.client_order_id,
        "code": item.code.value,
        "expected": None if item.expected is None else format(item.expected, "f"),
        "observed": None if item.observed is None else format(item.observed, "f"),
        "symbol": item.symbol,
    }


def _discrepancy_from_value(value: object) -> ReconciliationDiscrepancy:
    record = _mapping(value)
    if set(record) != {"client_order_id", "code", "expected", "observed", "symbol"}:
        raise ExecutionPersistenceError("persisted reconciliation discrepancy is malformed")
    return ReconciliationDiscrepancy(
        code=DiscrepancyCode(_string(record["code"])),
        symbol=_optional_string(record["symbol"]),
        client_order_id=_optional_string(record["client_order_id"]),
        expected=_optional_decimal(record["expected"]),
        observed=_optional_decimal(record["observed"]),
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ExecutionPersistenceError("persisted JSON object is malformed")
    return {cast(str, key): item for key, item in value.items()}


def _list(value: object, *, field_name: str) -> list[object]:
    if type(value) is not list:
        raise ExecutionPersistenceError(f"persisted {field_name} is malformed")
    return cast(list[object], value)


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _list(value, field_name="hash list"))


def _string(value: object) -> str:
    if type(value) is not str:
        raise ExecutionPersistenceError("persisted text value is malformed")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ExecutionPersistenceError("persisted integer value is malformed")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ExecutionPersistenceError("persisted boolean value is malformed")
    return value


def _decimal(value: object) -> Decimal:
    if type(value) is Decimal:
        result = value
    elif type(value) is str:
        result = Decimal(value)
    else:
        raise ExecutionPersistenceError("persisted decimal value is malformed")
    if not result.is_finite():
        raise ExecutionPersistenceError("persisted decimal value is malformed")
    return result


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else _decimal(value)


def _datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionPersistenceError("persisted timestamp is malformed")
    return value.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
