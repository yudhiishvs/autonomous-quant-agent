"""Narrow persistence adapter for audited operator halt and resume latch events."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.platform.control.models import (
    OPERATOR_RESUME_ACKNOWLEDGEMENT,
    OperatorHaltRequest,
    OperatorResumeRequest,
)
from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError, AuditValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.latches import (
    RiskLatchAction,
    RiskLatchError,
    RiskLatchEvent,
    RiskLatchKind,
    RiskLatchState,
    create_authenticated_latch_clear,
    create_latch_engagement,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import aqa_risk_latch_events
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_EVENT_ID = re.compile(r"^latch_[0-9a-f]{64}$", re.ASCII)
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)


class OperatorControlError(RuntimeError):
    """An operator latch mutation failed without exposing persistence details."""


class OperatorControlConflictError(OperatorControlError):
    """The requested latch transition conflicts with authoritative state."""


@dataclass(frozen=True, slots=True)
class OperatorControlResult:
    event_id: str
    state: Literal["engaged", "cleared"]
    reason_code: str
    latch_type: RiskLatchKind = RiskLatchKind.OPERATOR_HALT

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or _EVENT_ID.fullmatch(self.event_id) is None:
            raise ValueError("operator event ID is invalid")
        if self.state not in {"engaged", "cleared"}:
            raise ValueError("operator control state is invalid")
        if type(self.reason_code) is not str or _REASON_CODE.fullmatch(self.reason_code) is None:
            raise ValueError("operator reason code is invalid")
        if type(self.latch_type) is not RiskLatchKind or self.latch_type not in {
            RiskLatchKind.DEPLOYMENT_DRAWDOWN,
            RiskLatchKind.OPERATOR_HALT,
            RiskLatchKind.SESSION_LOSS,
        }:
            raise ValueError("operator latch type is not clearable")


class OperatorControlPort(Protocol):
    """Persist latch evidence only; this boundary has no order methods."""

    def halt(
        self, *, request: OperatorHaltRequest, occurred_at: datetime
    ) -> OperatorControlResult: ...

    def resume(
        self,
        *,
        request: OperatorResumeRequest,
        occurred_at: datetime,
    ) -> OperatorControlResult: ...


class SQLAlchemyOperatorControls:
    """Append an operator latch and its control-owned audit evidence atomically."""

    def __init__(self, engine: Engine, *, experiment_hash: str) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("operator controls require a concrete SQLAlchemy Engine")
        if type(experiment_hash) is not str or _SHA256.fullmatch(experiment_hash) is None:
            raise ValueError("operator controls require an experiment hash")
        self._engine = engine
        self._experiment_hash = experiment_hash
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.CONTROL)

    def halt(
        self,
        *,
        request: OperatorHaltRequest,
        occurred_at: datetime,
    ) -> OperatorControlResult:
        if type(request) is not OperatorHaltRequest:
            raise TypeError("operator halt requires the strict request contract")
        return self._transition(request=request, occurred_at=occurred_at, halt=True)

    def resume(
        self,
        *,
        request: OperatorResumeRequest,
        occurred_at: datetime,
    ) -> OperatorControlResult:
        if type(request) is not OperatorResumeRequest:
            raise TypeError("operator resume requires the strict request contract")
        require_resume_acknowledgement(request.acknowledgement)
        return self._transition(request=request, occurred_at=occurred_at, halt=False)

    def _transition(
        self,
        *,
        request: OperatorHaltRequest | OperatorResumeRequest,
        occurred_at: datetime,
        halt: bool,
    ) -> OperatorControlResult:
        latch_type = (
            RiskLatchKind.OPERATOR_HALT
            if halt
            else RiskLatchKind(cast(OperatorResumeRequest, request).latch_type)
        )
        idempotency_key = _internal_idempotency_key(
            request.idempotency_key,
            halt=halt,
            latch_type=latch_type,
        )
        try:
            with self._transactions.transaction() as connection:
                self._acquire_latch_lock(connection, latch_type=latch_type)
                events = self._latch_events(connection, latch_type=latch_type)
                existing = _existing_transition(
                    events,
                    idempotency_key=idempotency_key,
                    expected_action=(RiskLatchAction.ENGAGED if halt else RiskLatchAction.CLEARED),
                )
                if existing is not None:
                    self._append_audit(connection, existing)
                    return _result(existing)

                latch_state = (
                    RiskLatchState.from_events(
                        experiment_hash=self._experiment_hash,
                        events=events,
                    )
                    if events
                    else RiskLatchState.empty(experiment_hash=self._experiment_hash)
                )
                event = self._create_event(
                    request=request,
                    occurred_at=occurred_at,
                    halt=halt,
                    latch_state=latch_state,
                    idempotency_key=idempotency_key,
                    latch_type=latch_type,
                )
                connection.execute(insert(aqa_risk_latch_events).values(**_latch_row(event)))
                self._append_audit(connection, event)
                return _result(event)
        except OperatorControlConflictError:
            raise
        except RiskLatchError:
            raise OperatorControlConflictError(
                "operator latch transition conflicts with durable state"
            ) from None
        except (AuditPersistenceError, AuditValidationError, SQLAlchemyError):
            raise OperatorControlError("operator latch state is unavailable") from None
        except (KeyError, TypeError, ValueError):
            raise OperatorControlError("operator latch state is unavailable") from None

    def _acquire_latch_lock(
        self,
        connection: Connection,
        *,
        latch_type: RiskLatchKind,
    ) -> None:
        self._transactions.acquire_postgres_advisory_lock(
            connection,
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.RISK_LATCH,
                f"{self._experiment_hash}:{latch_type.value}",
            ),
        )

    def _latch_events(
        self,
        connection: Connection,
        *,
        latch_type: RiskLatchKind,
    ) -> tuple[RiskLatchEvent, ...]:
        rows = (
            connection.execute(
                select(aqa_risk_latch_events)
                .where(
                    aqa_risk_latch_events.c.experiment_hash == self._experiment_hash,
                    aqa_risk_latch_events.c.latch_type == latch_type.value,
                )
                .order_by(aqa_risk_latch_events.c.sequence)
            )
            .mappings()
            .all()
        )
        return tuple(_latch_from_row(row) for row in rows)

    def _create_event(
        self,
        *,
        request: OperatorHaltRequest | OperatorResumeRequest,
        occurred_at: datetime,
        halt: bool,
        latch_state: RiskLatchState,
        idempotency_key: str,
        latch_type: RiskLatchKind,
    ) -> RiskLatchEvent:
        correlation_id = _internal_correlation_id(request.correlation_id)
        if halt:
            return create_latch_engagement(
                latch_state=latch_state,
                latch_type=latch_type,
                reason_code=cast(OperatorHaltRequest, request).reason_code,
                actor="operator",
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        return create_authenticated_latch_clear(
            latch_state=latch_state,
            latch_type=latch_type,
            authenticated=True,
            acknowledgement=cast(OperatorResumeRequest, request).acknowledgement,
            actor="operator",
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )

    def _append_audit(self, connection: Connection, event: RiskLatchEvent) -> None:
        self._audit.append(
            stream_id=(f"aqa_control:operator:{event.experiment_hash}:{event.latch_type.value}"),
            event_type=(
                "operator.halted" if event.action is RiskLatchAction.ENGAGED else "operator.resumed"
            ),
            occurred_at=event.occurred_at,
            payload=AuditPayload.from_mapping(
                {
                    "event_ids": [event.latch_event_id],
                    "idempotency_key": event.latch_event_id,
                    "latch_type": event.latch_type.value,
                    "reason_code": event.reason_code,
                    "sequence": event.sequence,
                    **(
                        {"acknowledgement": "accepted"}
                        if event.action is RiskLatchAction.CLEARED
                        else {}
                    ),
                }
            ),
            connection=connection,
        )


def _existing_transition(
    events: tuple[RiskLatchEvent, ...],
    *,
    idempotency_key: str,
    expected_action: RiskLatchAction,
) -> RiskLatchEvent | None:
    existing = next(
        (event for event in events if event.idempotency_key == idempotency_key),
        None,
    )
    if existing is not None and existing.action is not expected_action:
        raise OperatorControlConflictError("operator idempotency key conflicts with durable state")
    return existing


def _result(event: RiskLatchEvent) -> OperatorControlResult:
    state: Literal["engaged", "cleared"] = (
        "engaged" if event.action is RiskLatchAction.ENGAGED else "cleared"
    )
    return OperatorControlResult(
        event_id=event.latch_event_id,
        state=state,
        reason_code=event.reason_code,
        latch_type=event.latch_type,
    )


def _latch_row(event: RiskLatchEvent) -> dict[str, object]:
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
        "payload": _latch_payload(event),
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
        raise ValueError("persisted operator latch payload is malformed")
    return event


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError("persisted operator latch payload is malformed")
    return {cast(str, key): item for key, item in value.items()}


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("persisted operator latch text is malformed")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("persisted operator latch integer is malformed")
    return value


def _datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted operator latch timestamp is malformed")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def require_resume_acknowledgement(value: object) -> str:
    if value != OPERATOR_RESUME_ACKNOWLEDGEMENT or type(value) is not str:
        raise ValueError("operator resume acknowledgement is invalid")
    return value


def _internal_idempotency_key(
    value: str,
    *,
    halt: bool,
    latch_type: RiskLatchKind,
) -> str:
    digest = sha256_hex(
        (
            "operator-control-idempotency-v1",
            "halt" if halt else "resume",
            latch_type.value,
            value,
        )
    )
    return f"ctl_{digest[:60]}"


def _internal_correlation_id(value: str) -> str:
    return f"correlation_{sha256_hex(('operator-control-correlation-v1', value))}"
