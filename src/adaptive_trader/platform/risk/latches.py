"""Append-only signed-risk latch events and deterministic restart reconstruction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, DecimalException, localcontext
from enum import StrEnum
from typing import Final

from adaptive_trader.platform.domain import (
    DeterministicId,
    require_finite_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

LATCH_CLEAR_ACKNOWLEDGEMENT: Final = "I_HAVE_REVIEWED_AQA_PAPER_STATE"
SESSION_LOSS_TRIGGER: Final = Decimal("-0.02")
DEPLOYMENT_DRAWDOWN_TRIGGER: Final = Decimal("-0.15")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_LATCH_EVENT_ID = re.compile(r"^latch_[0-9a-f]{64}$", flags=re.ASCII)
_CORRELATION_ID = re.compile(r"^correlation_[0-9a-f]{64}$", flags=re.ASCII)
_ARITHMETIC = Context(prec=50)


class RiskLatchError(DomainValidationError):
    """Raised when a latch operation would weaken the append-only safety boundary."""


class RiskLatchKind(StrEnum):
    """Closed blocking latch inventory owned by the execution boundary."""

    DEPLOYMENT_DRAWDOWN = "deployment_drawdown"
    OPERATOR_HALT = "operator_halt"
    RECONCILIATION = "reconciliation"
    SESSION_LOSS = "session_loss"


class RiskLatchAction(StrEnum):
    """Exact immutable transitions stored for each latch."""

    ENGAGED = "ENGAGED"
    CLEARED = "CLEARED"


@dataclass(frozen=True, slots=True)
class RiskLatchEvent:
    """One immutable, hash-bound transition for a single risk latch stream."""

    latch_event_id: str
    experiment_hash: str
    latch_type: RiskLatchKind
    sequence: int
    action: RiskLatchAction
    reason_code: str
    actor: str
    occurred_at: datetime
    correlation_id: str
    idempotency_key: str
    payload_hash: str
    content_hash: str

    def __post_init__(self) -> None:
        _validate_event(self)

    @classmethod
    def create(
        cls,
        *,
        experiment_hash: str,
        latch_type: RiskLatchKind,
        sequence: int,
        action: RiskLatchAction,
        reason_code: str,
        actor: str,
        occurred_at: datetime,
        correlation_id: str,
        idempotency_key: str,
    ) -> RiskLatchEvent:
        """Construct a replayable transition whose ID is its complete request identity."""

        normalized_at = _instant(occurred_at, field_name="occurred_at")
        _hash(experiment_hash, field_name="experiment_hash")
        if type(latch_type) is not RiskLatchKind or type(action) is not RiskLatchAction:
            raise RiskLatchError("latch type and action must use the closed contract")
        _positive_sequence(sequence)
        _identifier(reason_code, field_name="reason_code")
        _identifier(actor, field_name="actor")
        _identifier(idempotency_key, field_name="idempotency_key")
        _correlation_id(correlation_id)
        payload = {
            "action": action,
            "actor": actor,
            "correlation_id": correlation_id,
            "experiment_hash": experiment_hash,
            "idempotency_key": idempotency_key,
            "latch_type": latch_type,
            "occurred_at": normalized_at,
            "reason_code": reason_code,
            "schema": "risk-latch-event-v1",
            "sequence": sequence,
        }
        payload_hash = sha256_hex(payload)
        latch_event_id = DeterministicId.from_hash_input(
            prefix="latch",
            hash_input=("risk-latch-id-v1", payload_hash),
        ).value
        return cls(
            latch_event_id=latch_event_id,
            experiment_hash=experiment_hash,
            latch_type=latch_type,
            sequence=sequence,
            action=action,
            reason_code=reason_code,
            actor=actor,
            occurred_at=normalized_at,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            content_hash=sha256_hex(
                {
                    "latch_event_id": latch_event_id,
                    "payload_hash": payload_hash,
                    "schema": "risk-latch-content-v1",
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskLatchState:
    """Current latch state reconstructed only from immutable ordered events."""

    experiment_hash: str
    active: tuple[RiskLatchKind, ...]
    last_sequences: tuple[tuple[RiskLatchKind, int], ...]
    event_count: int
    source_hash: str
    content_hash: str

    def __post_init__(self) -> None:
        _validate_state(self)

    @classmethod
    def empty(cls, *, experiment_hash: str) -> RiskLatchState:
        """Create the deterministic initial state before any latch transition."""

        _hash(experiment_hash, field_name="experiment_hash")
        source_hash = sha256_hex(("risk-latch-events-v1", experiment_hash, ()))
        return cls(
            experiment_hash=experiment_hash,
            active=(),
            last_sequences=(),
            event_count=0,
            source_hash=source_hash,
            content_hash=_state_hash(
                experiment_hash=experiment_hash,
                active=(),
                last_sequences=(),
                event_count=0,
                source_hash=source_hash,
            ),
        )

    @classmethod
    def from_events(
        cls,
        *,
        experiment_hash: str,
        events: tuple[RiskLatchEvent, ...],
    ) -> RiskLatchState:
        """Verify all streams and derive state suitable for restart recovery."""

        _hash(experiment_hash, field_name="experiment_hash")
        if type(events) is not tuple or any(type(event) is not RiskLatchEvent for event in events):
            raise RiskLatchError("latch history must be an immutable event tuple")
        streams: dict[RiskLatchKind, list[RiskLatchEvent]] = {}
        seen_ids: set[str] = set()
        for event in events:
            if event.experiment_hash != experiment_hash:
                raise RiskLatchError("latch history contains another experiment")
            if event.latch_event_id in seen_ids:
                raise RiskLatchError("latch history contains a duplicate event")
            seen_ids.add(event.latch_event_id)
            streams.setdefault(event.latch_type, []).append(event)

        active: list[RiskLatchKind] = []
        last_sequences: list[tuple[RiskLatchKind, int]] = []
        for latch_type in sorted(streams, key=str):
            stream = sorted(streams[latch_type], key=lambda item: item.sequence)
            if tuple(event.sequence for event in stream) != tuple(range(1, len(stream) + 1)):
                raise RiskLatchError("latch event sequence is not contiguous")
            engaged = False
            for event in stream:
                if event.action is RiskLatchAction.ENGAGED:
                    if engaged:
                        raise RiskLatchError("an active latch cannot be engaged again")
                    engaged = True
                else:
                    if not engaged:
                        raise RiskLatchError("an inactive latch cannot be cleared")
                    engaged = False
            if engaged:
                active.append(latch_type)
            last_sequences.append((latch_type, stream[-1].sequence))

        ordered_events = tuple(
            sorted(events, key=lambda event: (event.latch_type.value, event.sequence))
        )
        source_hash = sha256_hex(
            (
                "risk-latch-events-v1",
                experiment_hash,
                tuple(event.content_hash for event in ordered_events),
            )
        )
        state = cls(
            experiment_hash=experiment_hash,
            active=tuple(sorted(active, key=str)),
            last_sequences=tuple(last_sequences),
            event_count=len(events),
            source_hash=source_hash,
            content_hash=_state_hash(
                experiment_hash=experiment_hash,
                active=tuple(sorted(active, key=str)),
                last_sequences=tuple(last_sequences),
                event_count=len(events),
                source_hash=source_hash,
            ),
        )
        return state

    def is_active(self, latch_type: RiskLatchKind) -> bool:
        """Return whether an exact closed latch is engaged."""

        if type(latch_type) is not RiskLatchKind:
            raise RiskLatchError("latch query must use the closed contract")
        return latch_type in self.active

    def next_sequence(self, latch_type: RiskLatchKind) -> int:
        """Return the next sequence expected for one transactional latch append."""

        if type(latch_type) is not RiskLatchKind:
            raise RiskLatchError("latch query must use the closed contract")
        return dict(self.last_sequences).get(latch_type, 0) + 1


@dataclass(frozen=True, slots=True)
class FinancialLatchAssessment:
    """Pure assessment of loss/drawdown levels and newly required latch types."""

    session_return: Decimal
    deployment_drawdown: Decimal
    engage: tuple[RiskLatchKind, ...]
    content_hash: str

    def __post_init__(self) -> None:
        session_return = _decimal(self.session_return, field_name="session_return")
        drawdown = _decimal(self.deployment_drawdown, field_name="deployment_drawdown")
        if type(self.engage) is not tuple or any(
            latch_type not in {RiskLatchKind.SESSION_LOSS, RiskLatchKind.DEPLOYMENT_DRAWDOWN}
            for latch_type in self.engage
        ):
            raise RiskLatchError("financial latch assessment contains an invalid latch")
        expected = sha256_hex(
            {
                "deployment_drawdown": drawdown,
                "engage": self.engage,
                "schema": "financial-latch-assessment-v1",
                "session_return": session_return,
            }
        )
        if self.content_hash != expected:
            raise RiskLatchError("financial latch assessment hash is invalid")


def assess_financial_latches(
    *,
    account_equity: Decimal,
    session_start_equity: Decimal,
    deployment_high_water_equity: Decimal,
    latch_state: RiskLatchState,
    session_loss_trigger: Decimal = SESSION_LOSS_TRIGGER,
    deployment_drawdown_trigger: Decimal = DEPLOYMENT_DRAWDOWN_TRIGGER,
) -> FinancialLatchAssessment:
    """Return newly required financial latches using inclusive trigger thresholds."""

    equity = _nonnegative_decimal(account_equity, field_name="account_equity")
    session_start = _positive_decimal(session_start_equity, field_name="session_start_equity")
    high_water = _positive_decimal(
        deployment_high_water_equity,
        field_name="deployment_high_water_equity",
    )
    session_trigger = _negative_trigger(session_loss_trigger, field_name="session_loss_trigger")
    drawdown_trigger = _negative_trigger(
        deployment_drawdown_trigger,
        field_name="deployment_drawdown_trigger",
    )
    if type(latch_state) is not RiskLatchState:
        raise RiskLatchError("financial latch assessment requires a validated latch state")
    try:
        with localcontext(_ARITHMETIC):
            session_return = (equity - session_start) / session_start
            drawdown = (equity - high_water) / high_water
    except DecimalException:
        raise RiskLatchError("financial latch levels could not be calculated") from None
    engage: list[RiskLatchKind] = []
    if session_return <= session_trigger and not latch_state.is_active(RiskLatchKind.SESSION_LOSS):
        engage.append(RiskLatchKind.SESSION_LOSS)
    if drawdown <= drawdown_trigger and not latch_state.is_active(
        RiskLatchKind.DEPLOYMENT_DRAWDOWN
    ):
        engage.append(RiskLatchKind.DEPLOYMENT_DRAWDOWN)
    ordered = tuple(sorted(engage, key=str))
    payload = {
        "deployment_drawdown": drawdown,
        "engage": ordered,
        "schema": "financial-latch-assessment-v1",
        "session_return": session_return,
    }
    return FinancialLatchAssessment(
        session_return=session_return,
        deployment_drawdown=drawdown,
        engage=ordered,
        content_hash=sha256_hex(payload),
    )


def create_latch_engagement(
    *,
    latch_state: RiskLatchState,
    latch_type: RiskLatchKind,
    reason_code: str,
    actor: str,
    occurred_at: datetime,
    correlation_id: str,
    idempotency_key: str,
) -> RiskLatchEvent:
    """Create one ENGAGED append without mutating prior state."""

    if type(latch_state) is not RiskLatchState or type(latch_type) is not RiskLatchKind:
        raise RiskLatchError("latch engagement requires validated state and type")
    if latch_state.is_active(latch_type):
        raise RiskLatchError("an active latch cannot be engaged again")
    return RiskLatchEvent.create(
        experiment_hash=latch_state.experiment_hash,
        latch_type=latch_type,
        sequence=latch_state.next_sequence(latch_type),
        action=RiskLatchAction.ENGAGED,
        reason_code=reason_code,
        actor=actor,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )


def create_authenticated_latch_clear(
    *,
    latch_state: RiskLatchState,
    latch_type: RiskLatchKind,
    authenticated: bool,
    acknowledgement: str,
    actor: str,
    occurred_at: datetime,
    correlation_id: str,
    idempotency_key: str,
) -> RiskLatchEvent:
    """Create one CLEARED append after exact authentication and acknowledgement checks."""

    if type(authenticated) is not bool or not authenticated:
        raise RiskLatchError("latch clear requires an authenticated operator")
    if type(acknowledgement) is not str or acknowledgement != LATCH_CLEAR_ACKNOWLEDGEMENT:
        raise RiskLatchError("latch clear acknowledgement is invalid")
    if type(latch_state) is not RiskLatchState or type(latch_type) is not RiskLatchKind:
        raise RiskLatchError("latch clear requires validated state and type")
    if not latch_state.is_active(latch_type):
        raise RiskLatchError("an inactive latch cannot be cleared")
    return RiskLatchEvent.create(
        experiment_hash=latch_state.experiment_hash,
        latch_type=latch_type,
        sequence=latch_state.next_sequence(latch_type),
        action=RiskLatchAction.CLEARED,
        reason_code="operator_reviewed_state",
        actor=actor,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )


def _validate_event(event: RiskLatchEvent) -> None:
    if (
        type(event.latch_event_id) is not str
        or _LATCH_EVENT_ID.fullmatch(event.latch_event_id) is None
    ):
        raise RiskLatchError("latch event ID is invalid")
    _hash(event.experiment_hash, field_name="experiment_hash")
    if type(event.latch_type) is not RiskLatchKind or type(event.action) is not RiskLatchAction:
        raise RiskLatchError("latch event uses an invalid closed value")
    _positive_sequence(event.sequence)
    _identifier(event.reason_code, field_name="reason_code")
    _identifier(event.actor, field_name="actor")
    _identifier(event.idempotency_key, field_name="idempotency_key")
    occurred_at = _instant(event.occurred_at, field_name="occurred_at")
    _correlation_id(event.correlation_id)
    _hash(event.payload_hash, field_name="payload_hash")
    _hash(event.content_hash, field_name="content_hash")
    payload_hash = sha256_hex(
        {
            "action": event.action,
            "actor": event.actor,
            "correlation_id": event.correlation_id,
            "experiment_hash": event.experiment_hash,
            "idempotency_key": event.idempotency_key,
            "latch_type": event.latch_type,
            "occurred_at": occurred_at,
            "reason_code": event.reason_code,
            "schema": "risk-latch-event-v1",
            "sequence": event.sequence,
        }
    )
    if event.payload_hash != payload_hash:
        raise RiskLatchError("latch event payload hash is invalid")
    expected_id = DeterministicId.from_hash_input(
        prefix="latch",
        hash_input=("risk-latch-id-v1", payload_hash),
    ).value
    if event.latch_event_id != expected_id:
        raise RiskLatchError("latch event ID is invalid")
    if event.content_hash != sha256_hex(
        {
            "latch_event_id": event.latch_event_id,
            "payload_hash": event.payload_hash,
            "schema": "risk-latch-content-v1",
        }
    ):
        raise RiskLatchError("latch event content hash is invalid")


def _validate_state(state: RiskLatchState) -> None:
    _hash(state.experiment_hash, field_name="experiment_hash")
    if type(state.active) is not tuple or state.active != tuple(sorted(set(state.active), key=str)):
        raise RiskLatchError("active latch state must be unique and ordered")
    if any(type(latch_type) is not RiskLatchKind for latch_type in state.active):
        raise RiskLatchError("active latch state contains an invalid type")
    if type(state.last_sequences) is not tuple:
        raise RiskLatchError("latch sequences must be immutable")
    previous: str | None = None
    sequence_total = 0
    for latch_type, sequence in state.last_sequences:
        if type(latch_type) is not RiskLatchKind:
            raise RiskLatchError("latch sequences contain an invalid type")
        if previous is not None and latch_type.value <= previous:
            raise RiskLatchError("latch sequences must be unique and ordered")
        _positive_sequence(sequence)
        previous = latch_type.value
        sequence_total += sequence
    if type(state.event_count) is not int or state.event_count < 0:
        raise RiskLatchError("latch event count is invalid")
    if sequence_total != state.event_count:
        raise RiskLatchError("latch event count does not match stream sequences")
    _hash(state.source_hash, field_name="source_hash")
    _hash(state.content_hash, field_name="content_hash")
    if state.content_hash != _state_hash(
        experiment_hash=state.experiment_hash,
        active=state.active,
        last_sequences=state.last_sequences,
        event_count=state.event_count,
        source_hash=state.source_hash,
    ):
        raise RiskLatchError("latch state hash is invalid")


def _state_hash(
    *,
    experiment_hash: str,
    active: tuple[RiskLatchKind, ...],
    last_sequences: tuple[tuple[RiskLatchKind, int], ...],
    event_count: int,
    source_hash: str,
) -> str:
    return sha256_hex(
        {
            "active": active,
            "event_count": event_count,
            "experiment_hash": experiment_hash,
            "last_sequences": last_sequences,
            "schema": "risk-latch-state-v1",
            "source_hash": source_hash,
        }
    )


def _decimal(value: object, *, field_name: str) -> Decimal:
    try:
        return require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise RiskLatchError(f"{field_name} must be a finite exact decimal") from None


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    result = _decimal(value, field_name=field_name)
    if result <= 0:
        raise RiskLatchError(f"{field_name} must be positive")
    return result


def _nonnegative_decimal(value: object, *, field_name: str) -> Decimal:
    result = _decimal(value, field_name=field_name)
    if result < 0:
        raise RiskLatchError(f"{field_name} cannot be negative")
    return result


def _negative_trigger(value: object, *, field_name: str) -> Decimal:
    result = _decimal(value, field_name=field_name)
    if not Decimal(-1) <= result < 0:
        raise RiskLatchError(f"{field_name} must be in [-1, 0)")
    return result


def _positive_sequence(value: object) -> int:
    if type(value) is not int or value < 1:
        raise RiskLatchError("latch sequence must be a positive integer")
    return value


def _identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise RiskLatchError(f"{field_name} must be a lowercase ASCII identifier")
    return value


def _hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise RiskLatchError(f"{field_name} must be lowercase SHA-256")
    return value


def _instant(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise RiskLatchError(f"{field_name} must be a UTC instant") from None


def _correlation_id(value: object) -> str:
    if type(value) is not str or _CORRELATION_ID.fullmatch(value) is None:
        raise RiskLatchError("correlation ID is invalid")
    return value
