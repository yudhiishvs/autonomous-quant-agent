"""Transactional decision-slot persistence, claiming, and restart recovery."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import TracebackType
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy import Connection, Engine, Select, Table, and_, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adaptive_trader.platform.domain import (
    AuditPayload,
    AuditWriter,
    DeterministicId,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.scheduling.models import (
    LEASE_DURATION,
    DecisionSlot,
    DecisionType,
    SessionSlotSchedule,
    SlotState,
    SlotValidationError,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    aqa_decision_slots,
    aqa_execution_plans,
    aqa_risk_decisions,
    aqa_signal_envelopes,
)
from adaptive_trader.platform.storage.transactions import SerializedTransactionCoordinator

_SUPPORTED_DIALECTS = frozenset({"postgresql", "sqlite"})
_REQUIRED_SLOT_COLUMNS = frozenset(
    {
        "slot_id",
        "experiment_id",
        "experiment_version",
        "experiment_hash",
        "signal_provider_id",
        "signal_provider_version",
        "session_date",
        "source_interval_start",
        "source_interval_end",
        "ready_at",
        "deadline_at",
        "required_completion_at",
        "decision_type",
        "state",
        "claim_owner",
        "claimed_at",
        "lease_expires_at",
        "attempt_count",
        "completed_at",
        "reason_code",
        "correlation_id",
        "content_hash",
        "version",
        "created_at",
        "updated_at",
    }
)
_CLAIMABLE_STATES = (SlotState.READY.value, SlotState.FLATTEN_REQUIRED.value)


class SlotSchemaError(RuntimeError):
    """Raised when persistence has not been migrated to the Phase 4 slot contract."""


class SlotPersistenceError(RuntimeError):
    """Raised when a slot transaction cannot be completed safely."""


class SlotTransitionError(SlotPersistenceError):
    """Raised when a requested slot transition is stale or prohibited."""


class ClaimStatus(StrEnum):
    """Closed outcomes of a transactional claim attempt."""

    CLAIMED = "CLAIMED"
    RECLAIMED = "RECLAIMED"
    LEASE_HELD = "LEASE_HELD"
    NOT_READY = "NOT_READY"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    DEADLINE_ELAPSED = "DEADLINE_ELAPSED"
    MATERIALIZED = "MATERIALIZED"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Durable result of one claim or expired-lease recovery decision."""

    status: ClaimStatus
    slot: DecisionSlot

    def __post_init__(self) -> None:
        if type(self.status) is not ClaimStatus or type(self.slot) is not DecisionSlot:
            raise TypeError("claim result must use closed scheduling contracts")


@runtime_checkable
class MaterializationProbe(Protocol):
    """Checks authoritative signal/risk/plan state before any lease reclaim."""

    def exists(self, connection: Connection, *, slot_id: str) -> bool:
        """Return whether a consequential decision already exists for the slot."""


@runtime_checkable
class SlotTransitionRecorder(Protocol):
    """Records one slot state in the same transaction as its projection update."""

    def record(
        self,
        connection: Connection,
        *,
        previous: DecisionSlot | None,
        current: DecisionSlot,
        occurred_at: datetime,
    ) -> None:
        """Append immutable evidence for a created or transitioned slot."""


class PlatformMaterializationProbe:
    """Queries the canonical signal, risk, and execution-plan relations."""

    def exists(self, connection: Connection, *, slot_id: str) -> bool:
        for statement in (
            select(aqa_signal_envelopes.c.signal_id)
            .where(aqa_signal_envelopes.c.slot_id == slot_id)
            .limit(1),
            select(aqa_risk_decisions.c.risk_decision_id)
            .where(aqa_risk_decisions.c.slot_id == slot_id)
            .limit(1),
            select(aqa_execution_plans.c.execution_plan_id)
            .select_from(
                aqa_execution_plans.join(
                    aqa_risk_decisions,
                    aqa_execution_plans.c.risk_decision_id == aqa_risk_decisions.c.risk_decision_id,
                )
            )
            .where(aqa_risk_decisions.c.slot_id == slot_id)
            .limit(1),
        ):
            if connection.scalar(statement) is not None:
                return True
        return False


class AuditSlotTransitionRecorder:
    """Writes actor-scoped hash-chained slot evidence atomically with slot state."""

    def __init__(self, engine: Engine) -> None:
        self._audit = AuditRepository(engine, writer=AuditWriter.SCHEDULER)

    def record(
        self,
        connection: Connection,
        *,
        previous: DecisionSlot | None,
        current: DecisionSlot,
        occurred_at: datetime,
    ) -> None:
        previous_state = previous.state.value.lower() if previous is not None else "pending"
        payload_values: dict[str, object] = {
            "attempt_count": current.attempt_count,
            "from_state": previous_state,
            "idempotency_key": DeterministicId.from_hash_input(
                prefix="slot_transition",
                hash_input=("slot-transition-v1", current.slot_id, current.version),
            ).value,
            "slot_id": current.slot_id,
            "to_state": current.state.value.lower(),
            "version": current.version,
        }
        if current.reason_code is not None:
            payload_values["reason_code"] = current.reason_code
        if current.completed_at is not None:
            payload_values["completed_at"] = _timestamp_text(current.completed_at)
        self._audit.append(
            stream_id=f"aqa_scheduler:{current.slot_id}",
            event_type="slot.created" if previous is None else "slot.transitioned",
            occurred_at=occurred_at,
            payload=AuditPayload.from_mapping(payload_values),
            connection=connection,
        )


class DecisionSlotRepository:
    """Persist and transition durable slots with serialized SQLite/PostgreSQL semantics.

    The relation is injectable so migration tests can exercise the new contract before an
    operational upgrade. Constructing this repository against an old relation fails immediately;
    it never degrades to the legacy six-state schema.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        transition_recorder: SlotTransitionRecorder | None = None,
        materialization_probe: MaterializationProbe | None = None,
        table: Table = aqa_decision_slots,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("slot repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in _SUPPORTED_DIALECTS:
            raise ValueError("slot repository requires PostgreSQL or SQLite")
        if not isinstance(table, Table):
            raise TypeError("slot repository requires a concrete SQLAlchemy Table")
        missing = _REQUIRED_SLOT_COLUMNS.difference(table.c.keys())
        if missing:
            raise SlotSchemaError("decision-slot relation is missing the Phase 4 contract")
        selected_recorder = transition_recorder or AuditSlotTransitionRecorder(engine)
        if not isinstance(selected_recorder, SlotTransitionRecorder):
            raise TypeError("slot repository requires a transactional transition recorder")
        selected_probe = materialization_probe or PlatformMaterializationProbe()
        if not isinstance(selected_probe, MaterializationProbe):
            raise TypeError("slot repository requires a materialization probe")
        self._engine = engine
        self._table = table
        self._recorder = selected_recorder
        self._materialization_probe = selected_probe
        self._transactions = SerializedTransactionCoordinator(engine)

    def transaction(self) -> AbstractContextManager[Connection]:
        """Open a repository-owned serialized transaction."""

        return self._transactions.transaction()

    def create_schedule(
        self,
        schedule: SessionSlotSchedule,
        *,
        recorded_at: datetime,
    ) -> tuple[DecisionSlot, ...]:
        """Create a full schedule idempotently and audit each first insertion."""

        if type(schedule) is not SessionSlotSchedule:
            raise TypeError("schedule must use the immutable slot contract")
        instant = _utc(recorded_at, field_name="recorded_at")
        try:
            with self.transaction() as connection:
                created: list[DecisionSlot] = []
                for slot in schedule.slots:
                    existing = self._get_on_connection(connection, slot.slot_id, for_update=True)
                    if existing is not None:
                        if existing != slot:
                            raise SlotPersistenceError(
                                "deterministic slot identity already has different content"
                            )
                        created.append(existing)
                        continue
                    connection.execute(
                        insert(self._table).values(**_slot_row(slot, created_at=instant))
                    )
                    self._recorder.record(
                        connection,
                        previous=None,
                        current=slot,
                        occurred_at=instant,
                    )
                    created.append(slot)
                return tuple(created)
        except (SlotPersistenceError, SlotValidationError):
            raise
        except IntegrityError:
            raise SlotPersistenceError("decision schedule conflicts with durable state") from None
        except SQLAlchemyError:
            raise SlotPersistenceError("decision schedule could not be persisted") from None

    def get(self, slot_id: str) -> DecisionSlot | None:
        """Return one immutable slot snapshot."""

        _require_slot_id(slot_id)
        try:
            with self._engine.begin() as connection:
                return self._get_on_connection(connection, slot_id, for_update=False)
        except SlotValidationError:
            raise SlotPersistenceError("persisted decision slot is malformed") from None
        except SQLAlchemyError:
            raise SlotPersistenceError("decision slot could not be read") from None

    def list_for_session(
        self,
        *,
        experiment_hash: str,
        session_date: object,
    ) -> tuple[DecisionSlot, ...]:
        """Return a session's slots in deterministic timeline order."""

        _require_hash(experiment_hash)
        if type(session_date) is not date:
            raise SlotTransitionError("session date is invalid")
        statement = (
            select(self._table)
            .where(
                self._table.c.experiment_hash == experiment_hash,
                self._table.c.session_date == session_date,
            )
            .order_by(self._table.c.ready_at, self._table.c.slot_id)
        )
        try:
            with self._engine.begin() as connection:
                return tuple(
                    _slot_from_row(row) for row in connection.execute(statement).mappings()
                )
        except SlotValidationError:
            raise SlotPersistenceError("persisted decision slot is malformed") from None
        except SQLAlchemyError:
            raise SlotPersistenceError("decision slots could not be read") from None

    def evaluate_readiness(
        self,
        slot_id: str,
        *,
        active_basket_watermark: datetime | None,
        now: datetime,
    ) -> DecisionSlot:
        """Persist PENDING/WAITING/READY/EXPIRED from contiguous basket readiness."""

        instant = _utc(now, field_name="now")
        watermark = (
            None
            if active_basket_watermark is None
            else _utc(active_basket_watermark, field_name="active_basket_watermark")
        )
        with self._safe_transaction() as connection:
            slot = self._require_on_connection(connection, slot_id, for_update=True)
            if slot.decision_type is DecisionType.FORCED_FLAT or slot.state in _terminal_states():
                return slot
            if slot.state is SlotState.CLAIMED:
                raise SlotTransitionError("claimed slot readiness cannot be reevaluated")
            if instant >= slot.deadline_at:
                return self._expire_locked(connection, slot=slot, now=instant)
            if instant < slot.ready_at:
                return slot
            if watermark is not None and watermark >= slot.source_interval_end:
                if slot.state is SlotState.READY:
                    return slot
                return self._transition(
                    connection,
                    slot,
                    state=SlotState.READY,
                    now=instant,
                    reason_code=None,
                )
            if slot.state is SlotState.WAITING_FOR_DATA:
                return slot
            return self._transition(
                connection,
                slot,
                state=SlotState.WAITING_FOR_DATA,
                now=instant,
                reason_code="active_basket_not_ready",
            )

    def claim(self, slot_id: str, *, owner: str, now: datetime) -> ClaimResult:
        """Claim or safely recover one slot under a fixed 30-second lease."""

        _require_owner(owner)
        instant = _utc(now, field_name="now")
        with self._safe_transaction() as connection:
            slot = self._require_on_connection(connection, slot_id, for_update=True)
            return self._claim_locked(connection, slot=slot, owner=owner, now=instant)

    def claim_next(self, *, owner: str, now: datetime) -> ClaimResult | None:
        """Lock and claim the next eligible slot, using SKIP LOCKED on PostgreSQL."""

        _require_owner(owner)
        instant = _utc(now, field_name="now")
        with self._safe_transaction() as connection:
            eligible = (
                select(self._table)
                .where(
                    self._table.c.ready_at <= instant,
                    or_(
                        self._table.c.state.in_(_CLAIMABLE_STATES),
                        and_(
                            self._table.c.state == SlotState.CLAIMED.value,
                            or_(
                                self._table.c.lease_expires_at <= instant,
                                self._table.c.deadline_at <= instant,
                            ),
                        ),
                    ),
                )
                .order_by(self._table.c.deadline_at, self._table.c.slot_id)
                .limit(1)
            )
            if connection.dialect.name == "postgresql":
                eligible = eligible.with_for_update(skip_locked=True)
            row = connection.execute(eligible).mappings().one_or_none()
            if row is None:
                return None
            slot = _slot_from_row(row)
            if (
                slot.state is SlotState.CLAIMED
                and slot.lease_expires_at is not None
                and slot.lease_expires_at > instant
            ):
                return None
            return self._claim_locked(connection, slot=slot, owner=owner, now=instant)

    def renew_lease(self, slot_id: str, *, owner: str, now: datetime) -> DecisionSlot:
        """Renew an unexpired owned lease without extending the decision deadline."""

        _require_owner(owner)
        instant = _utc(now, field_name="now")
        with self._safe_transaction() as connection:
            slot = self._require_on_connection(connection, slot_id, for_update=True)
            _require_owned_claim(slot, owner=owner)
            if instant >= slot.deadline_at:
                return self._expire_locked(connection, slot=slot, now=instant)
            _require_live_claim(slot, now=instant)
            return self._transition(
                connection,
                slot,
                state=SlotState.CLAIMED,
                now=instant,
                claim_owner=owner,
                claimed_at=instant,
                lease_expires_at=instant + LEASE_DURATION,
                attempt_count=slot.attempt_count,
                reason_code=None,
            )

    def complete(self, slot_id: str, *, owner: str, now: datetime) -> DecisionSlot:
        """Complete only an owned claim backed by an authoritative materialization."""

        _require_owner(owner)
        instant = _utc(now, field_name="now")
        with self._safe_transaction() as connection:
            slot = self._require_on_connection(connection, slot_id, for_update=True)
            _require_owned_live_claim(slot, owner=owner, now=instant)
            if not self._materialization_probe.exists(connection, slot_id=slot.slot_id):
                if instant >= slot.deadline_at:
                    return self._expire_locked(connection, slot=slot, now=instant)
                raise SlotTransitionError("slot cannot complete before decision materialization")
            return self._transition(
                connection,
                slot,
                state=SlotState.COMPLETED,
                now=instant,
                completed_at=instant,
                reason_code="decision_materialized",
            )

    def skip(self, slot_id: str, *, reason_code: str, now: datetime) -> DecisionSlot:
        """Persist a non-catch-up skipped outcome before materialization."""

        _require_reason(reason_code)
        instant = _utc(now, field_name="now")
        with self._safe_transaction() as connection:
            slot = self._require_on_connection(connection, slot_id, for_update=True)
            if slot.state not in {SlotState.PENDING, SlotState.WAITING_FOR_DATA, SlotState.READY}:
                raise SlotTransitionError("slot is not eligible to be skipped")
            if self._materialization_probe.exists(connection, slot_id=slot.slot_id):
                raise SlotTransitionError("materialized decision cannot be skipped")
            return self._transition(
                connection,
                slot,
                state=SlotState.SKIPPED,
                now=instant,
                completed_at=instant,
                reason_code=reason_code,
            )

    def fail(
        self,
        slot_id: str,
        *,
        owner: str,
        reason_code: str,
        now: datetime,
    ) -> DecisionSlot:
        """Persist a bounded failure for an owned claim."""

        _require_owner(owner)
        _require_reason(reason_code)
        instant = _utc(now, field_name="now")
        with self._safe_transaction() as connection:
            slot = self._require_on_connection(connection, slot_id, for_update=True)
            _require_owned_live_claim(slot, owner=owner, now=instant)
            return self._transition(
                connection,
                slot,
                state=SlotState.FAILED,
                now=instant,
                completed_at=instant,
                reason_code=reason_code,
            )

    def recover_expired_claims(self, *, owner: str, now: datetime) -> tuple[ClaimResult, ...]:
        """Inspect and recover every expired claim deterministically after restart."""

        _require_owner(owner)
        instant = _utc(now, field_name="now")
        recovered: list[ClaimResult] = []
        with self._safe_transaction() as connection:
            statement = (
                select(self._table)
                .where(
                    self._table.c.state == SlotState.CLAIMED.value,
                    or_(
                        self._table.c.lease_expires_at <= instant,
                        self._table.c.deadline_at <= instant,
                    ),
                )
                .order_by(self._table.c.deadline_at, self._table.c.slot_id)
            )
            if connection.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            for row in connection.execute(statement).mappings():
                recovered.append(
                    self._claim_locked(
                        connection,
                        slot=_slot_from_row(row),
                        owner=owner,
                        now=instant,
                    )
                )
        return tuple(recovered)

    def _claim_locked(
        self,
        connection: Connection,
        *,
        slot: DecisionSlot,
        owner: str,
        now: datetime,
    ) -> ClaimResult:
        if slot.state in _terminal_states():
            return ClaimResult(ClaimStatus.NOT_AVAILABLE, slot)
        if slot.state is SlotState.CLAIMED:
            assert slot.lease_expires_at is not None
            if now >= slot.deadline_at:
                if self._materialization_probe.exists(connection, slot_id=slot.slot_id):
                    completed = self._transition(
                        connection,
                        slot,
                        state=SlotState.COMPLETED,
                        now=now,
                        completed_at=now,
                        reason_code="decision_materialized_before_recovery",
                    )
                    return ClaimResult(ClaimStatus.MATERIALIZED, completed)
                expired = self._expire_locked(connection, slot=slot, now=now)
                return ClaimResult(ClaimStatus.DEADLINE_ELAPSED, expired)
            if slot.lease_expires_at > now:
                return ClaimResult(ClaimStatus.LEASE_HELD, slot)
            if self._materialization_probe.exists(connection, slot_id=slot.slot_id):
                completed = self._transition(
                    connection,
                    slot,
                    state=SlotState.COMPLETED,
                    now=now,
                    completed_at=now,
                    reason_code="decision_materialized_before_recovery",
                )
                return ClaimResult(ClaimStatus.MATERIALIZED, completed)
            reclaimed = self._transition(
                connection,
                slot,
                state=SlotState.CLAIMED,
                now=now,
                claim_owner=owner,
                claimed_at=now,
                lease_expires_at=now + LEASE_DURATION,
                attempt_count=slot.attempt_count + 1,
                reason_code=None,
            )
            return ClaimResult(ClaimStatus.RECLAIMED, reclaimed)
        if now >= slot.deadline_at:
            expired = self._expire_locked(connection, slot=slot, now=now)
            return ClaimResult(ClaimStatus.DEADLINE_ELAPSED, expired)
        if slot.state not in {SlotState.READY, SlotState.FLATTEN_REQUIRED} or now < slot.ready_at:
            return ClaimResult(ClaimStatus.NOT_READY, slot)
        claimed = self._transition(
            connection,
            slot,
            state=SlotState.CLAIMED,
            now=now,
            claim_owner=owner,
            claimed_at=now,
            lease_expires_at=now + LEASE_DURATION,
            attempt_count=slot.attempt_count + 1,
            reason_code=None,
        )
        return ClaimResult(ClaimStatus.CLAIMED, claimed)

    def _expire_locked(
        self,
        connection: Connection,
        *,
        slot: DecisionSlot,
        now: datetime,
    ) -> DecisionSlot:
        state = (
            SlotState.FAILED
            if slot.decision_type is DecisionType.FORCED_FLAT
            else SlotState.EXPIRED
        )
        reason = (
            "forced_flat_submission_deadline_elapsed"
            if slot.decision_type is DecisionType.FORCED_FLAT
            else "decision_deadline_elapsed"
        )
        return self._transition(
            connection,
            slot,
            state=state,
            now=now,
            completed_at=now,
            reason_code=reason,
        )

    def _transition(
        self,
        connection: Connection,
        previous: DecisionSlot,
        *,
        state: SlotState,
        now: datetime,
        claim_owner: str | None = None,
        claimed_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
        attempt_count: int | None = None,
        completed_at: datetime | None = None,
        reason_code: str | None,
    ) -> DecisionSlot:
        if state not in _allowed_next_states(previous.state):
            raise SlotTransitionError("slot state transition is prohibited")
        current = previous.evolve(
            state=state,
            claim_owner=claim_owner,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            attempt_count=(previous.attempt_count if attempt_count is None else attempt_count),
            completed_at=completed_at,
            reason_code=reason_code,
        )
        result = connection.execute(
            update(self._table)
            .where(
                self._table.c.slot_id == previous.slot_id,
                self._table.c.version == previous.version,
                self._table.c.content_hash == previous.content_hash,
            )
            .values(**_slot_row(current, updated_at=now))
        )
        if result.rowcount != 1:
            raise SlotTransitionError("decision slot changed during transition")
        self._recorder.record(
            connection,
            previous=previous,
            current=current,
            occurred_at=now,
        )
        return current

    def _get_on_connection(
        self,
        connection: Connection,
        slot_id: str,
        *,
        for_update: bool,
    ) -> DecisionSlot | None:
        _require_slot_id(slot_id)
        statement: Select[tuple[object, ...]] = select(self._table).where(
            self._table.c.slot_id == slot_id
        )
        if for_update and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _slot_from_row(row)

    def _require_on_connection(
        self,
        connection: Connection,
        slot_id: str,
        *,
        for_update: bool,
    ) -> DecisionSlot:
        slot = self._get_on_connection(connection, slot_id, for_update=for_update)
        if slot is None:
            raise SlotTransitionError("decision slot does not exist")
        return slot

    def _safe_transaction(self) -> _TransactionIterator:
        return _TransactionIterator(self._transactions.transaction())


class _TransactionIterator:
    """Translate only database failures while preserving domain transition errors."""

    def __init__(self, context: AbstractContextManager[Connection]) -> None:
        self._context = context

    def __enter__(self) -> Connection:
        try:
            return self._context.__enter__()
        except SQLAlchemyError:
            raise SlotPersistenceError("decision-slot transaction could not start") from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            self._context.__exit__(exc_type, exc, traceback)
        except (SlotPersistenceError, SlotValidationError):
            raise
        except SQLAlchemyError:
            raise SlotPersistenceError("decision-slot transaction failed") from None
        return False


def _slot_row(
    slot: DecisionSlot,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "slot_id": slot.slot_id,
        "experiment_id": slot.experiment_id,
        "experiment_version": slot.experiment_version,
        "experiment_hash": slot.experiment_hash,
        "signal_provider_id": slot.signal_provider_id,
        "signal_provider_version": slot.signal_provider_version,
        "session_date": slot.session_date,
        "source_interval_start": slot.source_interval_start,
        "source_interval_end": slot.source_interval_end,
        "ready_at": slot.ready_at,
        "deadline_at": slot.deadline_at,
        "required_completion_at": slot.required_completion_at,
        "decision_type": slot.decision_type.value,
        "state": slot.state.value,
        "claim_owner": slot.claim_owner,
        "claimed_at": slot.claimed_at,
        "lease_expires_at": slot.lease_expires_at,
        "attempt_count": slot.attempt_count,
        "completed_at": slot.completed_at,
        "reason_code": slot.reason_code,
        "correlation_id": slot.correlation_id,
        "content_hash": slot.content_hash,
        "version": slot.version,
    }
    if created_at is not None:
        values["created_at"] = created_at
        values["updated_at"] = created_at
    elif updated_at is not None:
        values["updated_at"] = updated_at
    return values


def _slot_from_row(row: RowMapping) -> DecisionSlot:
    return DecisionSlot(
        slot_id=row["slot_id"],
        experiment_id=row["experiment_id"],
        experiment_version=row["experiment_version"],
        experiment_hash=row["experiment_hash"],
        signal_provider_id=row["signal_provider_id"],
        signal_provider_version=row["signal_provider_version"],
        session_date=row["session_date"],
        source_interval_start=row["source_interval_start"],
        source_interval_end=row["source_interval_end"],
        ready_at=row["ready_at"],
        deadline_at=row["deadline_at"],
        required_completion_at=row["required_completion_at"],
        decision_type=DecisionType(row["decision_type"]),
        state=SlotState(row["state"]),
        claim_owner=row["claim_owner"],
        claimed_at=row["claimed_at"],
        lease_expires_at=row["lease_expires_at"],
        attempt_count=row["attempt_count"],
        completed_at=row["completed_at"],
        reason_code=row["reason_code"],
        correlation_id=row["correlation_id"],
        content_hash=row["content_hash"],
        version=row["version"],
    )


def _allowed_next_states(state: SlotState) -> frozenset[SlotState]:
    return {
        SlotState.PENDING: frozenset(
            {SlotState.WAITING_FOR_DATA, SlotState.READY, SlotState.SKIPPED, SlotState.EXPIRED}
        ),
        SlotState.WAITING_FOR_DATA: frozenset(
            {SlotState.WAITING_FOR_DATA, SlotState.READY, SlotState.SKIPPED, SlotState.EXPIRED}
        ),
        SlotState.READY: frozenset({SlotState.CLAIMED, SlotState.SKIPPED, SlotState.EXPIRED}),
        SlotState.FLATTEN_REQUIRED: frozenset({SlotState.CLAIMED, SlotState.FAILED}),
        SlotState.CLAIMED: frozenset(
            {SlotState.CLAIMED, SlotState.COMPLETED, SlotState.EXPIRED, SlotState.FAILED}
        ),
        SlotState.COMPLETED: frozenset(),
        SlotState.SKIPPED: frozenset(),
        SlotState.EXPIRED: frozenset(),
        SlotState.FAILED: frozenset(),
    }[state]


def _terminal_states() -> frozenset[SlotState]:
    return frozenset({SlotState.COMPLETED, SlotState.SKIPPED, SlotState.EXPIRED, SlotState.FAILED})


def _require_owned_claim(slot: DecisionSlot, *, owner: str) -> None:
    if slot.state is not SlotState.CLAIMED or slot.claim_owner != owner:
        raise SlotTransitionError("slot is not claimed by this worker")


def _require_owned_live_claim(slot: DecisionSlot, *, owner: str, now: datetime) -> None:
    _require_owned_claim(slot, owner=owner)
    _require_live_claim(slot, now=now)


def _require_live_claim(slot: DecisionSlot, *, now: datetime) -> None:
    assert slot.lease_expires_at is not None
    if slot.lease_expires_at <= now:
        raise SlotTransitionError("slot lease has expired")


def _utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise SlotTransitionError(f"{field_name} must be a UTC instant") from None


def _require_slot_id(value: object) -> str:
    if type(value) is not str or not value.startswith("slot_") or len(value) != 69:
        raise SlotTransitionError("slot ID is invalid")
    _require_hash(value.removeprefix("slot_"))
    return value


def _require_hash(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SlotTransitionError("hash is invalid")
    return value


def _require_owner(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or not value[0].isalnum()
        or any(not (character.isalnum() or character in "_.-") for character in value)
        or not value.isascii()
    ):
        raise SlotTransitionError("claim owner is invalid")
    return value


def _require_reason(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or not value[0].islower()
        or any(
            not (character.islower() or character.isdigit() or character == "_")
            for character in value
        )
        or not value.isascii()
    ):
        raise SlotTransitionError("reason code is invalid")
    return value


def _timestamp_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "AuditSlotTransitionRecorder",
    "ClaimResult",
    "ClaimStatus",
    "DecisionSlotRepository",
    "MaterializationProbe",
    "PlatformMaterializationProbe",
    "SlotPersistenceError",
    "SlotSchemaError",
    "SlotTransitionError",
    "SlotTransitionRecorder",
]
