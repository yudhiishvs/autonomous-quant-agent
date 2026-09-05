"""Immutable identities and exact XNAS session timing for durable decision slots."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Self, cast
from zoneinfo import ZoneInfo

from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.data.calendar import ExchangeCalendar
from adaptive_trader.platform.domain import DeterministicId, require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

LEASE_DURATION = timedelta(seconds=30)
STRATEGY_CLOSE_TIMES = tuple(
    time(hour, minute)
    for hour, minutes in (
        (9, (45,)),
        (10, (0, 15, 30, 45)),
        (11, (0, 15, 30, 45)),
        (12, (0, 15, 30, 45)),
        (13, (0, 15, 30, 45)),
        (14, (0, 15, 30)),
    )
    for minute in minutes
)

_NEW_YORK = ZoneInfo("America/New_York")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", flags=re.ASCII)
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_SLOT_ID = re.compile(r"^slot_[0-9a-f]{64}$", flags=re.ASCII)
_CORRELATION_ID = re.compile(r"^correlation_[0-9a-f]{64}$", flags=re.ASCII)
_FULL_OPEN = time(9, 30)
_FULL_CLOSE = time(16, 0)
_FORCED_FLAT_TARGET = time(15, 43)
_FORCED_FLAT_DEADLINE = time(15, 44)
_REQUIRED_FLAT = time(15, 45)


class SlotValidationError(DomainValidationError):
    """Raised when a slot or schedule violates the immutable contract."""


class DecisionType(StrEnum):
    """Closed types of durable intraday decisions."""

    STRATEGY = "STRATEGY"
    FORCED_FLAT = "FORCED_FLAT"


class SlotState(StrEnum):
    """Exact durable decision-slot states."""

    PENDING = "PENDING"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    READY = "READY"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    FLATTEN_REQUIRED = "FLATTEN_REQUIRED"


_TERMINAL_STATES = frozenset(
    {SlotState.COMPLETED, SlotState.SKIPPED, SlotState.EXPIRED, SlotState.FAILED}
)
_REASON_REQUIRED_STATES = frozenset(
    {SlotState.WAITING_FOR_DATA, SlotState.SKIPPED, SlotState.EXPIRED, SlotState.FAILED}
)


@dataclass(frozen=True, slots=True)
class DecisionSlot:
    """One deterministic slot and its versioned durable state.

    ``slot_id`` hashes the immutable tuple ``("decision-slot-v1", experiment identity,
    provider identity, session date, source interval, decision type)``. ``content_hash`` hashes
    every persisted field except itself, so each state transition receives a new version and
    digest while retaining the same replayable slot identity.
    """

    slot_id: str
    experiment_id: str
    experiment_version: int
    experiment_hash: str
    signal_provider_id: str
    signal_provider_version: str
    session_date: date
    source_interval_start: datetime
    source_interval_end: datetime
    ready_at: datetime
    deadline_at: datetime
    required_completion_at: datetime
    decision_type: DecisionType
    state: SlotState
    claim_owner: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    attempt_count: int
    completed_at: datetime | None
    reason_code: str | None
    correlation_id: str
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        _validate_slot(self)

    @classmethod
    def create(
        cls,
        *,
        experiment_id: str,
        experiment_version: int,
        experiment_hash: str,
        signal_provider_id: str,
        signal_provider_version: str,
        session_date: date,
        source_interval_start: datetime,
        source_interval_end: datetime,
        ready_at: datetime,
        deadline_at: datetime,
        required_completion_at: datetime,
        decision_type: DecisionType,
        initial_state: SlotState,
    ) -> DecisionSlot:
        """Create the first immutable state for a replayable slot."""

        identity = _slot_identity_payload(
            experiment_id=experiment_id,
            experiment_version=experiment_version,
            experiment_hash=experiment_hash,
            signal_provider_id=signal_provider_id,
            signal_provider_version=signal_provider_version,
            session_date=session_date,
            source_interval_start=source_interval_start,
            source_interval_end=source_interval_end,
            decision_type=decision_type,
        )
        slot_id = DeterministicId.from_hash_input(prefix="slot", hash_input=identity).value
        correlation_id = DeterministicId.from_hash_input(
            prefix="correlation",
            hash_input=("slot-correlation-v1", slot_id),
        ).value
        values: dict[str, object] = {
            "slot_id": slot_id,
            "experiment_id": experiment_id,
            "experiment_version": experiment_version,
            "experiment_hash": experiment_hash,
            "signal_provider_id": signal_provider_id,
            "signal_provider_version": signal_provider_version,
            "session_date": session_date,
            "source_interval_start": source_interval_start,
            "source_interval_end": source_interval_end,
            "ready_at": ready_at,
            "deadline_at": deadline_at,
            "required_completion_at": required_completion_at,
            "decision_type": decision_type,
            "state": initial_state,
            "claim_owner": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "attempt_count": 0,
            "completed_at": None,
            "reason_code": None,
            "correlation_id": correlation_id,
            "version": 1,
        }
        return cls(**values, content_hash=sha256_hex(_slot_content_payload(values)))  # type: ignore[arg-type]

    def evolve(
        self,
        *,
        state: SlotState,
        claim_owner: str | None,
        claimed_at: datetime | None,
        lease_expires_at: datetime | None,
        attempt_count: int,
        completed_at: datetime | None,
        reason_code: str | None,
    ) -> Self:
        """Return a validated next state with a monotonic version and refreshed hash."""

        values = {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
            if field != "content_hash"
        }
        values.update(
            {
                "state": state,
                "claim_owner": claim_owner,
                "claimed_at": claimed_at,
                "lease_expires_at": lease_expires_at,
                "attempt_count": attempt_count,
                "completed_at": completed_at,
                "reason_code": reason_code,
            }
        )
        values["version"] = self.version + 1
        return replace(
            self,
            state=state,
            claim_owner=claim_owner,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            attempt_count=attempt_count,
            completed_at=completed_at,
            reason_code=reason_code,
            version=self.version + 1,
            content_hash=sha256_hex(_slot_content_payload(values)),
        )


@dataclass(frozen=True, slots=True)
class SessionSlotSchedule:
    """Deterministic outcome of planning one exchange session."""

    session_date: date
    strategy_slots: tuple[DecisionSlot, ...]
    forced_flat_slot: DecisionSlot | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise SlotValidationError("session date must be a date")
        if type(self.strategy_slots) is not tuple or any(
            type(slot) is not DecisionSlot for slot in self.strategy_slots
        ):
            raise SlotValidationError("strategy slots must be an immutable slot tuple")
        if self.forced_flat_slot is not None and type(self.forced_flat_slot) is not DecisionSlot:
            raise SlotValidationError("forced-flat slot is invalid")
        if self.reason_code is not None:
            _require_reason(self.reason_code)
            if self.strategy_slots or self.forced_flat_slot is not None:
                raise SlotValidationError("unavailable sessions cannot contain decision slots")
        elif len(self.strategy_slots) != len(STRATEGY_CLOSE_TIMES) or self.forced_flat_slot is None:
            raise SlotValidationError("a full session schedule must contain every exact slot")

    @property
    def slots(self) -> tuple[DecisionSlot, ...]:
        """Return all slots in chronological order."""

        if self.forced_flat_slot is None:
            return self.strategy_slots
        return (*self.strategy_slots, self.forced_flat_slot)


def build_session_schedule(
    *,
    experiment: ExperimentDefinition,
    signal_provider_id: str,
    signal_provider_version: str,
    session_date: date,
    calendar: ExchangeCalendar,
) -> SessionSlotSchedule:
    """Build the exact twenty-entry-plus-flatten schedule for one full XNAS session.

    Closed and nonstandard sessions produce no entry or flatten slot. Forced risk reduction for a
    shortened session is deliberately outside this scheduler and therefore fails closed.
    """

    if type(experiment) is not ExperimentDefinition:
        raise SlotValidationError("scheduler requires a validated experiment definition")
    _require_identifier(signal_provider_id, field_name="signal provider ID")
    _require_version(signal_provider_version, field_name="signal provider version")
    if type(session_date) is not date:
        raise SlotValidationError("session date must be a date")
    if not isinstance(calendar, ExchangeCalendar) or calendar.name != "XNAS":
        raise SlotValidationError("scheduler requires the injected XNAS calendar")
    _validate_exact_experiment_timing(experiment)

    session = calendar.session(session_date)
    if session is None:
        return SessionSlotSchedule(session_date, (), None, "market_closed")
    if not session.is_standard_full_session:
        return SessionSlotSchedule(session_date, (), None, "unsupported_nonstandard_session")
    expected_open = _market_instant(session_date, _FULL_OPEN)
    expected_close = _market_instant(session_date, _FULL_CLOSE)
    if session.open_at != expected_open or session.close_at != expected_close:
        raise SlotValidationError("XNAS full-session boundaries do not match the exact timetable")

    strategy_slots = tuple(
        _strategy_slot(
            experiment=experiment,
            provider_id=signal_provider_id,
            provider_version=signal_provider_version,
            session_date=session_date,
            close_time=close_time,
        )
        for close_time in STRATEGY_CLOSE_TIMES
    )
    forced_flat = _forced_flat_slot(
        experiment=experiment,
        provider_id=signal_provider_id,
        provider_version=signal_provider_version,
        session_date=session_date,
    )
    return SessionSlotSchedule(session_date, strategy_slots, forced_flat, None)


def _strategy_slot(
    *,
    experiment: ExperimentDefinition,
    provider_id: str,
    provider_version: str,
    session_date: date,
    close_time: time,
) -> DecisionSlot:
    interval_end = _market_instant(session_date, close_time)
    interval_start = interval_end - timedelta(minutes=15)
    ready_at = interval_end + timedelta(seconds=60)
    deadline_at = interval_end + timedelta(seconds=120)
    return DecisionSlot.create(
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.experiment_version,
        experiment_hash=experiment.content_hash,
        signal_provider_id=provider_id,
        signal_provider_version=provider_version,
        session_date=session_date,
        source_interval_start=interval_start,
        source_interval_end=interval_end,
        ready_at=ready_at,
        deadline_at=deadline_at,
        required_completion_at=deadline_at,
        decision_type=DecisionType.STRATEGY,
        initial_state=SlotState.PENDING,
    )


def _forced_flat_slot(
    *,
    experiment: ExperimentDefinition,
    provider_id: str,
    provider_version: str,
    session_date: date,
) -> DecisionSlot:
    target = _market_instant(session_date, _FORCED_FLAT_TARGET)
    deadline = _market_instant(session_date, _FORCED_FLAT_DEADLINE)
    required = _market_instant(session_date, _REQUIRED_FLAT)
    return DecisionSlot.create(
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.experiment_version,
        experiment_hash=experiment.content_hash,
        signal_provider_id=provider_id,
        signal_provider_version=provider_version,
        session_date=session_date,
        # This operational interval identifies the final minute before the predeclared target;
        # it does not claim that a 15-minute strategy bar exists at 15:43.
        source_interval_start=target - timedelta(minutes=1),
        source_interval_end=target,
        ready_at=target,
        deadline_at=deadline,
        required_completion_at=required,
        decision_type=DecisionType.FORCED_FLAT,
        initial_state=SlotState.FLATTEN_REQUIRED,
    )


def _validate_slot(slot: DecisionSlot) -> None:
    if type(slot.slot_id) is not str or _SLOT_ID.fullmatch(slot.slot_id) is None:
        raise SlotValidationError("slot ID is invalid")
    _require_identifier(slot.experiment_id, field_name="experiment ID")
    if type(slot.experiment_version) is not int or slot.experiment_version < 1:
        raise SlotValidationError("experiment version must be a positive integer")
    _require_hash(slot.experiment_hash, field_name="experiment hash")
    _require_identifier(slot.signal_provider_id, field_name="signal provider ID")
    _require_version(slot.signal_provider_version, field_name="signal provider version")
    if type(slot.session_date) is not date:
        raise SlotValidationError("session date must be a date")
    instants = {
        "source interval start": slot.source_interval_start,
        "source interval end": slot.source_interval_end,
        "ready time": slot.ready_at,
        "deadline": slot.deadline_at,
        "required completion": slot.required_completion_at,
    }
    for label, value in instants.items():
        try:
            require_utc_instant(value, field_name=label.replace(" ", "_"))
        except DomainValidationError:
            raise SlotValidationError(f"{label} must be a UTC instant") from None
    if not (
        slot.source_interval_start
        < slot.source_interval_end
        <= slot.ready_at
        < slot.deadline_at
        <= slot.required_completion_at
    ):
        raise SlotValidationError("slot interval and deadlines are not ordered")
    if type(slot.decision_type) is not DecisionType or type(slot.state) is not SlotState:
        raise SlotValidationError("slot decision type and state must use closed contracts")
    if type(slot.attempt_count) is not int or slot.attempt_count < 0:
        raise SlotValidationError("slot attempt count must be nonnegative")
    if type(slot.version) is not int or slot.version < 1:
        raise SlotValidationError("slot version must be positive")
    if (
        type(slot.correlation_id) is not str
        or _CORRELATION_ID.fullmatch(slot.correlation_id) is None
    ):
        raise SlotValidationError("slot correlation ID is invalid")
    _require_hash(slot.content_hash, field_name="slot content hash")

    claim_values = (slot.claim_owner, slot.claimed_at, slot.lease_expires_at)
    if slot.state is SlotState.CLAIMED:
        if any(value is None for value in claim_values):
            raise SlotValidationError("claimed slot requires complete lease metadata")
        _require_owner(slot.claim_owner)
        claimed_at = require_utc_instant(slot.claimed_at, field_name="claimed_at")
        lease_expires_at = require_utc_instant(slot.lease_expires_at, field_name="lease_expires_at")
        if lease_expires_at - claimed_at != LEASE_DURATION:
            raise SlotValidationError("slot lease must be exactly 30 seconds")
    elif any(value is not None for value in claim_values):
        raise SlotValidationError("unclaimed slot cannot retain lease metadata")

    if slot.state in _TERMINAL_STATES:
        if slot.completed_at is None:
            raise SlotValidationError("terminal slot requires a completion timestamp")
        require_utc_instant(slot.completed_at, field_name="completed_at")
    elif slot.completed_at is not None:
        raise SlotValidationError("nonterminal slot cannot have a completion timestamp")
    if slot.state in _REASON_REQUIRED_STATES and slot.reason_code is None:
        raise SlotValidationError("slot outcome requires a durable reason code")
    if slot.reason_code is not None:
        _require_reason(slot.reason_code)

    expected_id = DeterministicId.from_hash_input(
        prefix="slot",
        hash_input=_slot_identity_payload(
            experiment_id=slot.experiment_id,
            experiment_version=slot.experiment_version,
            experiment_hash=slot.experiment_hash,
            signal_provider_id=slot.signal_provider_id,
            signal_provider_version=slot.signal_provider_version,
            session_date=slot.session_date,
            source_interval_start=slot.source_interval_start,
            source_interval_end=slot.source_interval_end,
            decision_type=slot.decision_type,
        ),
    ).value
    if slot.slot_id != expected_id:
        raise SlotValidationError("slot ID does not match its immutable identity")
    expected_correlation = DeterministicId.from_hash_input(
        prefix="correlation", hash_input=("slot-correlation-v1", slot.slot_id)
    ).value
    if slot.correlation_id != expected_correlation:
        raise SlotValidationError("correlation ID does not match the slot")
    values = {
        field: getattr(slot, field)
        for field in slot.__dataclass_fields__
        if field != "content_hash"
    }
    if slot.content_hash != sha256_hex(_slot_content_payload(values)):
        raise SlotValidationError("slot content hash does not match its state")


def _slot_identity_payload(
    *,
    experiment_id: str,
    experiment_version: int,
    experiment_hash: str,
    signal_provider_id: str,
    signal_provider_version: str,
    session_date: date,
    source_interval_start: datetime,
    source_interval_end: datetime,
    decision_type: DecisionType,
) -> tuple[object, ...]:
    return (
        "decision-slot-v1",
        experiment_id,
        experiment_version,
        experiment_hash,
        signal_provider_id,
        signal_provider_version,
        session_date.isoformat(),
        source_interval_start,
        source_interval_end,
        decision_type,
    )


def _slot_content_payload(values: dict[str, object]) -> dict[str, object]:
    session_date = cast(date, values["session_date"])
    return {
        **values,
        "session_date": session_date.isoformat(),
    }


def _validate_exact_experiment_timing(experiment: ExperimentDefinition) -> None:
    session = experiment.session
    exact_values = (
        experiment.market_data.exchange_calendar == "XNAS",
        experiment.market_data.decision_timeframe == "15Min",
        session.open == "09:30:00",
        session.close == "16:00:00",
        session.first_strategy_bar_close == "09:45:00",
        session.last_strategy_bar_close == "14:30:00",
        session.decision_ready_delay_seconds == 60,
        session.decision_deadline_delay_seconds == 120,
        session.forced_flat_target_time == "15:43:00",
        session.forced_flat_submit_deadline == "15:44:00",
        session.required_flat_time == "15:45:00",
    )
    if not all(exact_values):
        raise SlotValidationError("experiment does not match the exact XNAS slot timetable")


def _market_instant(session_date: date, local_time: time) -> datetime:
    return datetime.combine(session_date, local_time, tzinfo=_NEW_YORK).astimezone(UTC)


def _require_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SlotValidationError(f"{field_name} is invalid")
    return value


def _require_version(value: object, *, field_name: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise SlotValidationError(f"{field_name} is invalid")
    return value


def _require_owner(value: object) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise SlotValidationError("claim owner is invalid")
    return value


def _require_reason(value: object) -> str:
    if type(value) is not str or _REASON.fullmatch(value) is None:
        raise SlotValidationError("slot reason code is invalid")
    return value


def _require_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise SlotValidationError(f"{field_name} is invalid")
    return value


__all__ = [
    "LEASE_DURATION",
    "STRATEGY_CLOSE_TIMES",
    "DecisionSlot",
    "DecisionType",
    "SessionSlotSchedule",
    "SlotState",
    "SlotValidationError",
    "build_session_schedule",
]
