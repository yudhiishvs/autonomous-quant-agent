"""Transactional persistence for immutable signed-risk decisions and latch streams."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import cast

from sqlalchemy import Connection, Engine, Table, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError, AuditValidationError
from adaptive_trader.platform.risk.latches import (
    RiskLatchAction,
    RiskLatchError,
    RiskLatchEvent,
    RiskLatchKind,
    RiskLatchState,
)
from adaptive_trader.platform.risk.models import (
    AccountSnapshot,
    PlanningPrice,
    RiskDecision,
    RiskExecutionScope,
    SecurityMetadataSnapshot,
    SignedPosition,
    SignedRiskValidationError,
)
from adaptive_trader.platform.risk.policy import AppliedRiskControl, ExposureSnapshot, RiskControl
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    aqa_risk_decisions,
    aqa_risk_latch_events,
    aqa_signal_envelopes,
)
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
)

_REQUIRED_LATCH_COLUMNS = frozenset(
    {
        "latch_event_id",
        "experiment_hash",
        "latch_type",
        "sequence",
        "action",
        "correlation_id",
        "idempotency_key",
        "reason_code",
        "actor",
        "occurred_at",
        "payload",
        "payload_hash",
        "content_hash",
    }
)
_REQUIRED_DECISION_COLUMNS = frozenset(
    {
        "risk_decision_id",
        "execution_stage",
        "preceding_reconciliation_id",
        "slot_id",
        "signal_id",
        "experiment_hash",
        "policy_id",
        "policy_version",
        "decided_at",
        "input_hash",
        "signal_hash",
        "policy_hash",
        "statistics_hash",
        "latch_state_hash",
        "correlation_id",
        "execution_scope",
        "original_proposal",
        "proposed_targets",
        "approved_targets",
        "before_exposure",
        "after_exposure",
        "account_snapshot",
        "planning_positions",
        "planning_prices",
        "security_metadata",
        "source_timestamps",
        "active_latches",
        "required_latch_event_ids",
        "controls",
        "reason_codes",
        "gross_exposure",
        "net_exposure",
        "cash_weight",
        "payload_hash",
        "signature",
        "content_hash",
    }
)
_REQUIRED_SIGNAL_COLUMNS = frozenset(
    {"signal_id", "slot_id", "experiment_hash", "correlation_id", "content_hash"}
)


class RiskSchemaError(RuntimeError):
    """Raised when persistence has not been migrated to the signed-risk contract."""


class RiskPersistenceError(RuntimeError):
    """Raised when signed-risk state cannot be persisted or verified safely."""


class SignedRiskRepository:
    """Serialize latch state with each signal's immutable execution-stage decisions."""

    def __init__(
        self,
        engine: Engine,
        *,
        latch_table: Table = aqa_risk_latch_events,
        decision_table: Table = aqa_risk_decisions,
        signal_table: Table = aqa_signal_envelopes,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("risk repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("risk repository requires PostgreSQL or SQLite")
        if not isinstance(latch_table, Table) or _REQUIRED_LATCH_COLUMNS.difference(
            latch_table.c.keys()
        ):
            raise RiskSchemaError("latch relation is missing the signed-risk contract")
        if not isinstance(decision_table, Table) or _REQUIRED_DECISION_COLUMNS.difference(
            decision_table.c.keys()
        ):
            raise RiskSchemaError("risk relation is missing the signed-risk contract")
        if not isinstance(signal_table, Table) or _REQUIRED_SIGNAL_COLUMNS.difference(
            signal_table.c.keys()
        ):
            raise RiskSchemaError("signal relation is missing the signed-risk input contract")
        self._engine = engine
        self._latches = latch_table
        self._decisions = decision_table
        self._signals = signal_table
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.EXECUTION)

    def transaction(self) -> AbstractContextManager[Connection]:
        """Open a serialized risk transaction."""

        return self._transactions.transaction()

    def latch_state(
        self,
        experiment_hash: str,
        *,
        connection: Connection | None = None,
    ) -> RiskLatchState:
        """Reconstruct and verify every durable latch stream for an experiment."""

        try:
            if connection is not None:
                self._transactions.validate_connection(
                    connection,
                    require_serialized_sqlite=False,
                )
                return self._latch_state_on_connection(connection, experiment_hash)
            with self._engine.begin() as owned_connection:
                return self._latch_state_on_connection(owned_connection, experiment_hash)
        except (RiskLatchError, RiskPersistenceError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise RiskPersistenceError("risk latch state could not be read safely") from None

    def append_latch(self, event: RiskLatchEvent) -> RiskLatchEvent:
        """Append one exact latch transition with atomic audit evidence."""

        if type(event) is not RiskLatchEvent:
            raise RiskPersistenceError("latch append requires an immutable event")
        try:
            with self.transaction() as connection:
                self._acquire_locks(
                    connection,
                    signal_id=None,
                    experiment_hash=event.experiment_hash,
                )
                return self._append_latch_on_connection(connection, event)
        except (RiskLatchError, RiskPersistenceError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise RiskPersistenceError("latch audit could not be persisted") from None
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise RiskPersistenceError("risk latch event could not be persisted") from None

    def persist(
        self, decision: RiskDecision, *, preceding_reconciliation_id: str | None = None
    ) -> RiskDecision:
        """Atomically append required latches and one immutable decision per signal stage."""

        if type(decision) is not RiskDecision:
            raise RiskPersistenceError("risk persistence requires an immutable decision")
        stage = 1 if preceding_reconciliation_id is None else 2
        try:
            with self.transaction() as connection:
                self._acquire_locks(
                    connection,
                    signal_id=decision.signal_id,
                    experiment_hash=decision.experiment_hash,
                )
                existing = self._decision_for_signal(
                    connection, decision.signal_id, execution_stage=stage
                )
                if existing is not None:
                    if existing == decision:
                        return existing
                    raise RiskPersistenceError("signal already has a different risk decision")

                self._verify_signal(connection, decision)
                if preceding_reconciliation_id is not None:
                    self._verify_continuation(connection, decision, preceding_reconciliation_id)

                before = self._latch_state_on_connection(
                    connection,
                    decision.experiment_hash,
                )
                if before.content_hash != decision.latch_state_hash:
                    raise RiskPersistenceError("risk decision was evaluated from stale latch state")
                events = list(self._latch_events(connection, decision.experiment_hash))
                for required in decision.required_latch_events:
                    persisted = self._append_latch_on_connection(connection, required)
                    if persisted != required:
                        raise RiskPersistenceError("required latch retry has different content")
                    if required not in events:
                        events.append(required)
                after = RiskLatchState.from_events(
                    experiment_hash=decision.experiment_hash,
                    events=tuple(events),
                )
                if after.active != decision.active_latches:
                    raise RiskPersistenceError(
                        "risk decision active latches do not match persistence"
                    )

                connection.execute(
                    insert(self._decisions).values(
                        **_decision_row(decision),
                        execution_stage=stage,
                        preceding_reconciliation_id=preceding_reconciliation_id,
                    )
                )
                self._append_decision_audit(connection, decision)
                stored = self._decision_for_signal(
                    connection, decision.signal_id, execution_stage=stage
                )
                if stored != decision:
                    raise RiskPersistenceError("persisted risk decision failed verification")
                return decision
        except (RiskLatchError, SignedRiskValidationError, RiskPersistenceError):
            raise
        except (AuditPersistenceError, AuditValidationError):
            raise RiskPersistenceError("risk decision audit could not be persisted") from None
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise RiskPersistenceError("risk decision could not be persisted") from None

    def decision_for_signal(
        self,
        signal_id: str,
        *,
        connection: Connection | None = None,
        execution_stage: int = 1,
    ) -> RiskDecision | None:
        """Read and hash-verify the immutable decision for one signal stage."""

        try:
            if connection is not None:
                self._transactions.validate_connection(
                    connection,
                    require_serialized_sqlite=False,
                )
                return self._decision_for_signal(
                    connection, signal_id, execution_stage=execution_stage
                )
            with self._engine.begin() as connection:
                return self._decision_for_signal(
                    connection, signal_id, execution_stage=execution_stage
                )
        except (RiskLatchError, SignedRiskValidationError, RiskPersistenceError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise RiskPersistenceError("risk decision could not be read safely") from None

    def _acquire_locks(
        self,
        connection: Connection,
        *,
        signal_id: str | None,
        experiment_hash: str,
    ) -> None:
        requests: list[PostgresAdvisoryLockRequest] = []
        if signal_id is not None:
            requests.append(
                PostgresAdvisoryLockRequest.for_resource(
                    PostgresAdvisoryLockNamespace.RISK_SIGNAL,
                    signal_id,
                )
            )
        requests.extend(
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.RISK_LATCH,
                f"{experiment_hash}:{latch_type.value}",
            )
            for latch_type in RiskLatchKind
        )
        self._transactions.acquire_postgres_advisory_locks(connection, requests)

    def _latch_state_on_connection(
        self,
        connection: Connection,
        experiment_hash: str,
    ) -> RiskLatchState:
        events = self._latch_events(connection, experiment_hash)
        if not events:
            return RiskLatchState.empty(experiment_hash=experiment_hash)
        return RiskLatchState.from_events(experiment_hash=experiment_hash, events=events)

    def _latch_events(
        self,
        connection: Connection,
        experiment_hash: str,
    ) -> tuple[RiskLatchEvent, ...]:
        statement = (
            select(self._latches)
            .where(self._latches.c.experiment_hash == experiment_hash)
            .order_by(self._latches.c.latch_type, self._latches.c.sequence)
        )
        rows = connection.execute(statement).mappings().all()
        return tuple(_latch_from_row(row) for row in rows)

    def _append_latch_on_connection(
        self,
        connection: Connection,
        event: RiskLatchEvent,
    ) -> RiskLatchEvent:
        events = list(self._latch_events(connection, event.experiment_hash))
        for existing in events:
            same_identity = existing.latch_event_id == event.latch_event_id
            same_idempotency = (
                existing.latch_type is event.latch_type
                and existing.idempotency_key == event.idempotency_key
            )
            if same_identity or same_idempotency:
                if existing == event:
                    return existing
                raise RiskPersistenceError("latch identity already has different content")
        before = (
            RiskLatchState.empty(experiment_hash=event.experiment_hash)
            if not events
            else RiskLatchState.from_events(
                experiment_hash=event.experiment_hash,
                events=tuple(events),
            )
        )
        if event.sequence != before.next_sequence(event.latch_type):
            raise RiskPersistenceError("latch event sequence is stale or noncontiguous")
        RiskLatchState.from_events(
            experiment_hash=event.experiment_hash,
            events=(*events, event),
        )
        connection.execute(insert(self._latches).values(**_latch_row(event)))
        self._append_latch_audit(connection, event)
        return event

    def _decision_for_signal(
        self,
        connection: Connection,
        signal_id: str,
        *,
        execution_stage: int = 1,
    ) -> RiskDecision | None:
        row = (
            connection.execute(
                select(self._decisions).where(
                    self._decisions.c.signal_id == signal_id,
                    self._decisions.c.execution_stage == execution_stage,
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._verified_decision_row(connection, row)

    def decision_by_id(
        self, risk_decision_id: str, *, connection: Connection | None = None
    ) -> RiskDecision | None:
        """Read one immutable stage by its signed identity."""
        try:
            if connection is None:
                with self._engine.begin() as selected:
                    return self.decision_by_id(risk_decision_id, connection=selected)
            self._transactions.validate_connection(connection, require_serialized_sqlite=False)
            row = (
                connection.execute(
                    select(self._decisions).where(
                        self._decisions.c.risk_decision_id == risk_decision_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            return self._verified_decision_row(connection, row)
        except (RiskLatchError, SignedRiskValidationError, RiskPersistenceError):
            raise
        except (DecimalException, KeyError, TypeError, ValueError, SQLAlchemyError):
            raise RiskPersistenceError("risk decision could not be read safely") from None

    def _verified_decision_row(
        self, connection: Connection, row: RowMapping | None
    ) -> RiskDecision | None:
        if row is None:
            return None
        required_ids = _string_list(row["required_latch_event_ids"], field_name="latch event IDs")
        event_rows = connection.execute(
            select(self._latches).where(self._latches.c.latch_event_id.in_(required_ids))
        ).mappings()
        events_by_id = {
            event.latch_event_id: event
            for event in (_latch_from_row(event_row) for event_row in event_rows)
        }
        if set(events_by_id) != set(required_ids):
            raise RiskPersistenceError("risk decision references missing latch evidence")
        decision = _decision_from_row(
            row,
            tuple(events_by_id[event_id] for event_id in required_ids),
        )
        self._verify_signal(connection, decision)
        return decision

    def _verify_continuation(
        self, connection: Connection, decision: RiskDecision, receipt_id: str
    ) -> None:
        """Only a clean, terminal, zero-position close authorizes a fresh second stage."""
        from adaptive_trader.platform.execution.models import OrderState, ReconciliationStatus
        from adaptive_trader.platform.storage.execution import (
            _plan_from_row,
            _reconciliation_from_row,
        )
        from adaptive_trader.platform.storage.tables import (
            aqa_broker_orders,
            aqa_execution_plans,
            aqa_order_intents,
            aqa_reconciliations,
        )

        prior = self._decision_for_signal(connection, decision.signal_id)
        receipt_row = (
            connection.execute(
                select(aqa_reconciliations).where(
                    aqa_reconciliations.c.reconciliation_id == receipt_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if prior is None or receipt_row is None:
            raise RiskPersistenceError("reversal close evidence is unavailable")
        receipt = _reconciliation_from_row(receipt_row)
        plan_row = (
            connection.execute(
                select(aqa_execution_plans).where(
                    aqa_execution_plans.c.execution_plan_id == receipt.execution_plan_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if plan_row is None:
            raise RiskPersistenceError("reversal close plan is unavailable")
        plan = _plan_from_row(plan_row)
        current = {item.symbol: item.quantity for item in plan.current_positions}
        reversed_symbols = tuple(
            item.symbol
            for item in plan.target_quantities
            if current[item.symbol] * item.quantity < 0
        )
        positions = {item.symbol: item.quantity for item in receipt.observed_positions}
        planning_positions = {
            item.symbol: item.quantity for item in decision.planning_positions if item.quantity != 0
        }
        observed_positions = {symbol: qty for symbol, qty in positions.items() if qty != 0}
        order_rows = connection.execute(
            select(aqa_broker_orders.c.state)
            .join(
                aqa_order_intents,
                aqa_order_intents.c.client_order_id == aqa_broker_orders.c.client_order_id,
            )
            .where(aqa_order_intents.c.execution_plan_id == plan.execution_plan_id)
        ).all()
        fresh_times = (
            decision.account_snapshot.observed_at,
            *(item.observed_at for item in decision.planning_prices),
            *(item.observed_at for item in decision.security_metadata),
            dict(decision.source_timestamps).get("reconciliation", prior.decided_at),
        )
        if (
            plan.risk_decision_id != prior.risk_decision_id
            or plan.target_version != 1
            or plan.forced_flat
            or not reversed_symbols
            or receipt.status is not ReconciliationStatus.CLEAN
            or receipt.slot_id != decision.slot_id
            or receipt.experiment_hash != decision.experiment_hash
            or receipt.correlation_id != decision.correlation_id
            or any(
                abs(positions.get(symbol, Decimal(0))) > Decimal("0.000001")
                for symbol in reversed_symbols
            )
            or not order_rows
            or any(row.state != OrderState.FILLED.value for row in order_rows)
            or planning_positions != observed_positions
            or decision.account_snapshot.account_id_hash != receipt.account_id_hash
            or abs(decision.account_snapshot.cash - receipt.observed_cash) > Decimal("0.01")
            or not receipt.completed_at < decision.decided_at < plan.deadline_at
            or any(instant < receipt.completed_at for instant in fresh_times)
        ):
            raise RiskPersistenceError("reversal continuation lacks fresh reconciled authority")

    def _verify_signal(self, connection: Connection, decision: RiskDecision) -> None:
        row = (
            connection.execute(
                select(
                    self._signals.c.slot_id,
                    self._signals.c.experiment_hash,
                    self._signals.c.correlation_id,
                    self._signals.c.content_hash,
                ).where(self._signals.c.signal_id == decision.signal_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or (
            row["slot_id"] != decision.slot_id
            or row["experiment_hash"] != decision.experiment_hash
            or row["correlation_id"] != decision.correlation_id
            or row["content_hash"] != decision.signal_hash
        ):
            raise RiskPersistenceError("risk decision does not match its authoritative signal")

    def _append_latch_audit(self, connection: Connection, event: RiskLatchEvent) -> None:
        self._audit.append(
            stream_id=f"aqa_execution:latch:{event.experiment_hash}:{event.latch_type.value}",
            event_type=(
                "latch.engaged" if event.action is RiskLatchAction.ENGAGED else "latch.cleared"
            ),
            occurred_at=event.occurred_at,
            payload=AuditPayload.from_mapping(
                {
                    "event_ids": [event.latch_event_id],
                    "idempotency_key": event.latch_event_id,
                    "reason_code": event.reason_code,
                    "sequence": event.sequence,
                }
            ),
            connection=connection,
        )

    def _append_decision_audit(self, connection: Connection, decision: RiskDecision) -> None:
        self._audit.append(
            stream_id=f"aqa_execution:risk:{decision.experiment_hash}",
            event_type="risk.decision_created",
            occurred_at=decision.decided_at,
            payload=AuditPayload.from_mapping(
                {
                    "content_hash": decision.content_hash,
                    "idempotency_key": decision.risk_decision_id,
                    "risk_decision_id": decision.risk_decision_id,
                    "signal_id": decision.signal_id,
                }
            ),
            connection=connection,
        )


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
        raise RiskPersistenceError("persisted latch payload is malformed")
    return event


def _decision_row(decision: RiskDecision) -> dict[str, object]:
    return {
        "risk_decision_id": decision.risk_decision_id,
        "slot_id": decision.slot_id,
        "signal_id": decision.signal_id,
        "experiment_hash": decision.experiment_hash,
        "policy_id": decision.policy_id,
        "policy_version": decision.policy_version,
        "decided_at": decision.decided_at,
        "input_hash": decision.input_hash,
        "signal_hash": decision.signal_hash,
        "policy_hash": decision.policy_hash,
        "statistics_hash": decision.statistics_hash,
        "latch_state_hash": decision.latch_state_hash,
        "correlation_id": decision.correlation_id,
        "execution_scope": decision.execution_scope.value,
        "original_proposal": [
            [
                symbol,
                action,
                None if edge is None else format(edge, "f"),
                None if target is None else format(target, "f"),
            ]
            for symbol, action, edge, target in decision.original_proposal
        ],
        "proposed_targets": _decimal_pairs(decision.proposed_targets),
        "approved_targets": _decimal_pairs(decision.final_targets),
        "before_exposure": _exposure_row(decision.before_exposure),
        "after_exposure": _exposure_row(decision.after_exposure),
        "account_snapshot": {
            "account_id_hash": decision.account_snapshot.account_id_hash,
            "buying_power": format(decision.account_snapshot.buying_power, "f"),
            "cash": format(decision.account_snapshot.cash, "f"),
            "equity": format(decision.account_snapshot.equity, "f"),
            "observed_at": _timestamp(decision.account_snapshot.observed_at),
        },
        "planning_positions": [
            [position.symbol, format(position.quantity, "f")]
            for position in decision.planning_positions
        ],
        "planning_prices": [
            [
                price.symbol,
                format(price.price, "f"),
                _timestamp(price.observed_at),
                price.validated,
            ]
            for price in decision.planning_prices
        ],
        "security_metadata": [
            [
                security.symbol,
                security.asset_active,
                security.tradable,
                security.shortable,
                security.easy_to_borrow,
                security.primary_listing_eligible,
                security.broker_capability_known,
                _timestamp(security.observed_at),
            ]
            for security in decision.security_metadata
        ],
        "source_timestamps": [
            [name, _timestamp(instant)] for name, instant in decision.source_timestamps
        ],
        "active_latches": [latch.value for latch in decision.active_latches],
        "required_latch_event_ids": [
            event.latch_event_id for event in decision.required_latch_events
        ],
        "controls": [_control_row(control) for control in decision.ordered_controls],
        "reason_codes": {
            "block": list(decision.block_reasons),
            "flatten": list(decision.flatten_reasons),
        },
        "gross_exposure": decision.after_exposure.gross,
        "net_exposure": decision.after_exposure.net,
        "cash_weight": decision.cash_weight,
        "payload_hash": decision.content_hash,
        "signature": None,
        "content_hash": decision.content_hash,
    }


def _decision_from_row(
    row: RowMapping,
    required_latch_events: tuple[RiskLatchEvent, ...],
) -> RiskDecision:
    before = _exposure_from_json(row["before_exposure"])
    after = _exposure_from_json(row["after_exposure"])
    reasons = _mapping(row["reason_codes"])
    if set(reasons) != {"block", "flatten"}:
        raise RiskPersistenceError("persisted risk reasons are malformed")
    account = _mapping(row["account_snapshot"])
    if set(account) != {
        "account_id_hash",
        "buying_power",
        "cash",
        "equity",
        "observed_at",
    }:
        raise RiskPersistenceError("persisted account snapshot is malformed")
    decision = RiskDecision(
        risk_decision_id=_string(row["risk_decision_id"]),
        slot_id=_string(row["slot_id"]),
        signal_id=_string(row["signal_id"]),
        signal_hash=_string(row["signal_hash"]),
        experiment_hash=_string(row["experiment_hash"]),
        policy_id=_string(row["policy_id"]),
        policy_version=_integer(row["policy_version"]),
        policy_hash=_string(row["policy_hash"]),
        correlation_id=_string(row["correlation_id"]),
        decided_at=_datetime(row["decided_at"]),
        input_hash=_string(row["input_hash"]),
        statistics_hash=_string(row["statistics_hash"]),
        account_snapshot=AccountSnapshot(
            account_id_hash=_string(account["account_id_hash"]),
            equity=_decimal(account["equity"]),
            cash=_decimal(account["cash"]),
            buying_power=_decimal(account["buying_power"]),
            observed_at=_timestamp_from_text(account["observed_at"]),
        ),
        planning_positions=_planning_positions(row["planning_positions"]),
        planning_prices=_planning_prices(row["planning_prices"]),
        security_metadata=_security_metadata(row["security_metadata"]),
        original_proposal=_proposal(row["original_proposal"]),
        proposed_targets=_pairs(row["proposed_targets"]),
        final_targets=_pairs(row["approved_targets"]),
        before_exposure=before,
        after_exposure=after,
        ordered_controls=_controls(row["controls"]),
        block_reasons=_string_tuple(reasons["block"], field_name="block reasons"),
        flatten_reasons=_string_tuple(reasons["flatten"], field_name="flatten reasons"),
        source_timestamps=_timestamps(row["source_timestamps"]),
        latch_state_hash=_string(row["latch_state_hash"]),
        active_latches=tuple(
            RiskLatchKind(value)
            for value in _string_list(row["active_latches"], field_name="active latches")
        ),
        required_latch_events=required_latch_events,
        execution_scope=RiskExecutionScope(_string(row["execution_scope"])),
        content_hash=_string(row["content_hash"]),
    )
    if (
        row["signature"] is not None
        or _string(row["payload_hash"]) != decision.content_hash
        or _decimal(row["gross_exposure"]) != decision.after_exposure.gross
        or _decimal(row["net_exposure"]) != decision.after_exposure.net
        or _decimal(row["cash_weight"]) != decision.cash_weight
    ):
        raise RiskPersistenceError("persisted risk projection is inconsistent")
    return decision


def _planning_positions(value: object) -> tuple[SignedPosition, ...]:
    result: list[SignedPosition] = []
    for raw in _list(value, field_name="planning positions"):
        if type(raw) is not list or len(raw) != 2:
            raise RiskPersistenceError("persisted planning positions are malformed")
        result.append(SignedPosition(symbol=_string(raw[0]), quantity=_decimal(raw[1])))
    return tuple(result)


def _planning_prices(value: object) -> tuple[PlanningPrice, ...]:
    result: list[PlanningPrice] = []
    for raw in _list(value, field_name="planning prices"):
        if type(raw) is not list or len(raw) != 4 or type(raw[3]) is not bool:
            raise RiskPersistenceError("persisted planning prices are malformed")
        result.append(
            PlanningPrice(
                symbol=_string(raw[0]),
                price=_decimal(raw[1]),
                observed_at=_timestamp_from_text(raw[2]),
                validated=raw[3],
            )
        )
    return tuple(result)


def _security_metadata(value: object) -> tuple[SecurityMetadataSnapshot, ...]:
    result: list[SecurityMetadataSnapshot] = []
    for raw in _list(value, field_name="security metadata"):
        if (
            type(raw) is not list
            or len(raw) != 8
            or any(type(item) is not bool for item in raw[1:7])
        ):
            raise RiskPersistenceError("persisted security metadata is malformed")
        result.append(
            SecurityMetadataSnapshot(
                symbol=_string(raw[0]),
                asset_active=raw[1],
                tradable=raw[2],
                shortable=raw[3],
                easy_to_borrow=raw[4],
                primary_listing_eligible=raw[5],
                broker_capability_known=raw[6],
                observed_at=_timestamp_from_text(raw[7]),
            )
        )
    return tuple(result)


def _control_row(control: AppliedRiskControl) -> dict[str, object]:
    return {
        "after": _decimal_pairs(control.after),
        "before": _decimal_pairs(control.before),
        "control": control.control.value,
        "factor": format(control.factor, "f"),
        "ordinal": control.ordinal,
        "pass_number": control.pass_number,
        "scope": control.scope,
    }


def _controls(value: object) -> tuple[AppliedRiskControl, ...]:
    records = _list(value, field_name="controls")
    controls: list[AppliedRiskControl] = []
    for raw in records:
        record = _mapping(raw)
        if set(record) != {
            "after",
            "before",
            "control",
            "factor",
            "ordinal",
            "pass_number",
            "scope",
        }:
            raise RiskPersistenceError("persisted risk control is malformed")
        controls.append(
            AppliedRiskControl(
                pass_number=_integer(record["pass_number"]),
                ordinal=_integer(record["ordinal"]),
                control=RiskControl(_string(record["control"])),
                scope=_string(record["scope"]),
                factor=_decimal(record["factor"]),
                before=_pairs(record["before"]),
                after=_pairs(record["after"]),
            )
        )
    return tuple(controls)


def _exposure_row(exposure: ExposureSnapshot) -> dict[str, object]:
    return {
        "cluster_gross": _decimal_pairs(exposure.cluster_gross),
        "gross": format(exposure.gross, "f"),
        "group_gross": _decimal_pairs(exposure.group_gross),
        "net": format(exposure.net, "f"),
        "positive": format(exposure.positive, "f"),
        "short_abs": format(exposure.short_abs, "f"),
    }


def _exposure_from_json(value: object) -> ExposureSnapshot:
    record = _mapping(value)
    if set(record) != {
        "cluster_gross",
        "gross",
        "group_gross",
        "net",
        "positive",
        "short_abs",
    }:
        raise RiskPersistenceError("persisted risk exposure is malformed")
    return ExposureSnapshot(
        gross=_decimal(record["gross"]),
        net=_decimal(record["net"]),
        positive=_decimal(record["positive"]),
        short_abs=_decimal(record["short_abs"]),
        group_gross=_pairs(record["group_gross"]),
        cluster_gross=_pairs(record["cluster_gross"]),
    )


def _proposal(value: object) -> tuple[tuple[str, str, Decimal | None, Decimal | None], ...]:
    records = _list(value, field_name="original proposal")
    result: list[tuple[str, str, Decimal | None, Decimal | None]] = []
    for raw in records:
        item = _list(raw, field_name="original proposal entry")
        if len(item) != 4:
            raise RiskPersistenceError("persisted original proposal is malformed")
        result.append(
            (
                _string(item[0]),
                _string(item[1]),
                None if item[2] is None else _decimal(item[2]),
                None if item[3] is None else _decimal(item[3]),
            )
        )
    return tuple(result)


def _decimal_pairs(value: Sequence[tuple[str, Decimal]]) -> list[list[str]]:
    return [[name, format(amount, "f")] for name, amount in value]


def _pairs(value: object) -> tuple[tuple[str, Decimal], ...]:
    records = _list(value, field_name="decimal pairs")
    result: list[tuple[str, Decimal]] = []
    for raw in records:
        item = _list(raw, field_name="decimal pair")
        if len(item) != 2:
            raise RiskPersistenceError("persisted decimal pair is malformed")
        result.append((_string(item[0]), _decimal(item[1])))
    return tuple(result)


def _timestamps(value: object) -> tuple[tuple[str, datetime], ...]:
    records = _list(value, field_name="source timestamps")
    result: list[tuple[str, datetime]] = []
    for raw in records:
        item = _list(raw, field_name="source timestamp")
        if len(item) != 2:
            raise RiskPersistenceError("persisted source timestamp is malformed")
        result.append((_string(item[0]), _timestamp_from_text(item[1])))
    return tuple(result)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_from_text(value: object) -> datetime:
    text_value = _string(value)
    if not text_value.endswith("Z"):
        raise RiskPersistenceError("persisted timestamp is malformed")
    parsed = datetime.fromisoformat(text_value[:-1] + "+00:00")
    if _timestamp(parsed) != text_value:
        raise RiskPersistenceError("persisted timestamp is not canonical")
    return parsed


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise RiskPersistenceError("persisted JSON object is malformed")
    return {cast(str, key): item for key, item in value.items()}


def _list(value: object, *, field_name: str) -> list[object]:
    if type(value) is not list:
        raise RiskPersistenceError(f"persisted {field_name} is malformed")
    return cast(list[object], value)


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    return tuple(_string(item) for item in _list(value, field_name=field_name))


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    return _string_list(value, field_name=field_name)


def _string(value: object) -> str:
    if type(value) is not str:
        raise RiskPersistenceError("persisted text is malformed")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise RiskPersistenceError("persisted integer is malformed")
    return value


def _decimal(value: object) -> Decimal:
    if type(value) is Decimal:
        return value
    if type(value) is not str:
        raise RiskPersistenceError("persisted decimal is malformed")
    return Decimal(value)


def _datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise RiskPersistenceError("persisted timestamp is malformed")
    return value.astimezone(UTC)


__all__ = [
    "RiskPersistenceError",
    "RiskSchemaError",
    "SignedRiskRepository",
]
