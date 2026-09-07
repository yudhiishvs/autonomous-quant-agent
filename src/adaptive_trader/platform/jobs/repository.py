"""Transactional persistence for bounded jobs and their outbox events."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import cast

from sqlalchemy import Connection, Engine, RowMapping, and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.jobs.models import (
    JOB_LEASE_DURATION,
    JOB_MAX_ATTEMPTS,
    JOB_SCHEMA_VERSION,
    ClaimedOutboxEvent,
    JobAttemptTransition,
    JobCreateRequest,
    JobPayload,
    JobRecord,
    JobState,
    JobType,
    JobValidationError,
    OutboxState,
    SafeJobError,
    require_artifact_id,
    require_job_types,
    retry_delay,
)
from adaptive_trader.platform.jobs.schema import (
    aqa_job_attempts_contract,
    aqa_jobs_contract,
    aqa_outbox_events_contract,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA
from adaptive_trader.platform.storage.transactions import SerializedTransactionCoordinator


class JobPersistenceError(RuntimeError):
    """A durable job operation failed without disclosing database details."""


class JobConflictError(JobPersistenceError):
    """An idempotency key or state transition conflicts with durable state."""


class JobNotFoundError(JobPersistenceError):
    """The requested job is absent from the durable store."""


class _Unchanged:
    __slots__ = ()


_UNCHANGED = _Unchanged()


class JobRepository:
    """Store jobs, attempt evidence, audit events, and outbox messages atomically."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditRepository,
        audit_writer: AuditWriter = AuditWriter.CONTROL,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("job repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("job repository requires PostgreSQL or SQLite")
        if engine.dialect.name == "sqlite":
            schema_map = engine.get_execution_options().get("schema_translate_map")
            if (
                not isinstance(schema_map, dict)
                or schema_map.get(PLATFORM_SCHEMA, object()) is not None
            ):
                raise ValueError("SQLite job repository requires the platform schema map")
        if not isinstance(audit, AuditRepository) or audit.engine is not engine:
            raise TypeError("job repository requires an audit repository on the same engine")
        if type(audit_writer) is not AuditWriter or audit_writer not in {
            AuditWriter.CONTROL,
            AuditWriter.COLLECTOR,
        }:
            raise TypeError("job repository requires control or collector audit authority")
        self._engine = engine
        self._audit = audit
        self._audit_writer = audit_writer
        self._transactions = SerializedTransactionCoordinator(engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    def transaction(self) -> AbstractContextManager[Connection]:
        return self._transactions.transaction()

    def create(self, request: JobCreateRequest) -> JobRecord:
        """Create a job and initial outbox/audit evidence in one transaction."""

        if type(request) is not JobCreateRequest:
            raise JobValidationError("job creation requires the typed request")
        job_id = _job_id(request)
        try:
            with self.transaction() as connection:
                existing = self._load_job(connection, job_id=job_id, for_update=True)
                if existing is not None:
                    if _same_create_request(existing, request):
                        return existing
                    raise JobConflictError("job idempotency key conflicts with existing content")

                values = _new_job_values(job_id=job_id, request=request)
                connection.execute(insert(aqa_jobs_contract).values(**values))
                job = _job_from_values(values)
                self._append_transition_evidence(
                    connection,
                    job=job,
                    event_type="job.created",
                )
                return job
        except (JobConflictError, JobValidationError):
            raise
        except IntegrityError:
            return self._load_after_create_race(job_id=job_id, request=request)
        except SQLAlchemyError:
            raise JobPersistenceError("job could not be created") from None

    def get(self, job_id: str) -> JobRecord | None:
        """Return one validated job without exposing a SQLAlchemy row."""

        _require_resource_id(job_id, field_name="job ID")
        try:
            with self._engine.begin() as connection:
                return self._load_job(connection, job_id=job_id, for_update=False)
        except JobValidationError:
            raise JobPersistenceError("persisted job state is malformed") from None
        except SQLAlchemyError:
            raise JobPersistenceError("job could not be read") from None

    def claim_next(
        self,
        *,
        owner: str,
        now: datetime,
        job_types: frozenset[JobType] = frozenset(JobType),
    ) -> JobRecord | None:
        """Claim one due job using SKIP LOCKED on PostgreSQL and serialized SQLite writes."""

        _require_resource_id(owner, field_name="job lease owner")
        require_job_types(job_types)
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                candidates = self._claim_candidates(
                    connection, now=normalized_now, job_types=job_types
                )
                for candidate in candidates:
                    if candidate.state in {JobState.CLAIMED, JobState.RUNNING}:
                        recovered = self._recover_expired(
                            connection,
                            job=candidate,
                            now=normalized_now,
                        )
                        if recovered.state is not JobState.FAILED:
                            continue
                        candidate = recovered
                    if (
                        candidate.next_attempt_at is None
                        or candidate.next_attempt_at > normalized_now
                    ):
                        continue
                    claimed = _replace_job(
                        candidate,
                        state=JobState.CLAIMED,
                        attempt_count=candidate.attempt_count + 1,
                        next_attempt_at=None,
                        lease_owner=owner,
                        lease_expires_at=normalized_now + JOB_LEASE_DURATION,
                        claimed_at=normalized_now,
                        started_at=None,
                        completed_at=None,
                        safe_error=None,
                        result_artifact_id=None,
                        updated_at=normalized_now,
                    )
                    self._persist_transition(connection, prior=candidate, current=claimed)
                    self._append_attempt(
                        connection,
                        job=claimed,
                        transition=JobAttemptTransition.CLAIMED,
                        owner=owner,
                        occurred_at=normalized_now,
                        sequence=1,
                        lease_expires_at=claimed.lease_expires_at,
                    )
                    self._append_transition_evidence(
                        connection,
                        job=claimed,
                        event_type="job.claimed",
                    )
                    return claimed
                return None
        except (JobConflictError, JobValidationError):
            raise
        except SQLAlchemyError:
            raise JobPersistenceError("job could not be claimed") from None

    def mark_running(
        self,
        *,
        job_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
    ) -> JobRecord:
        """Move a valid unexpired claim to RUNNING."""

        return self._worker_transition(
            job_id=job_id,
            owner=owner,
            attempt_number=attempt_number,
            now=now,
            required_state=JobState.CLAIMED,
            target_state=JobState.RUNNING,
            attempt_transition=JobAttemptTransition.RUNNING,
        )

    def succeed(
        self,
        *,
        job_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
        result_artifact_id: str | None = None,
    ) -> JobRecord:
        """Commit successful completion and its evidence atomically."""

        if result_artifact_id is not None:
            require_artifact_id(result_artifact_id)
        return self._worker_transition(
            job_id=job_id,
            owner=owner,
            attempt_number=attempt_number,
            now=now,
            required_state=JobState.RUNNING,
            target_state=JobState.SUCCEEDED,
            attempt_transition=JobAttemptTransition.SUCCEEDED,
            result_artifact_id=result_artifact_id,
        )

    def fail(
        self,
        *,
        job_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
        error: SafeJobError,
    ) -> JobRecord:
        """Record a safe failure, scheduling a retry or making the job dead."""

        if type(error) is not SafeJobError:
            raise JobValidationError("job failure requires a safe error contract")
        return self._worker_transition(
            job_id=job_id,
            owner=owner,
            attempt_number=attempt_number,
            now=now,
            required_state=JobState.RUNNING,
            target_state=JobState.FAILED,
            attempt_transition=JobAttemptTransition.FAILED,
            safe_error=error,
        )

    def cancel(self, *, job_id: str, now: datetime) -> JobRecord:
        """Cancel only a pending or retryable job; no public API route exposes this method."""

        _require_resource_id(job_id, field_name="job ID")
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                prior = self._required_job(connection, job_id=job_id)
                if prior.state not in {JobState.PENDING, JobState.FAILED}:
                    raise JobConflictError("job cannot be canceled from its current state")
                _require_forward_time(prior.updated_at, normalized_now)
                canceled = _replace_job(
                    prior,
                    state=JobState.CANCELED,
                    next_attempt_at=None,
                    completed_at=normalized_now,
                    safe_error=None,
                    result_artifact_id=None,
                    updated_at=normalized_now,
                )
                self._persist_transition(connection, prior=prior, current=canceled)
                self._append_transition_evidence(
                    connection,
                    job=canceled,
                    event_type="job.canceled",
                )
                return canceled
        except (JobConflictError, JobValidationError):
            raise
        except SQLAlchemyError:
            raise JobPersistenceError("job could not be canceled") from None

    def heartbeat(
        self,
        *,
        job_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
    ) -> JobRecord:
        """Extend one exact running attempt without granting a stale worker authority."""

        _require_resource_id(job_id, field_name="job ID")
        _require_resource_id(owner, field_name="job lease owner")
        _require_attempt_number(attempt_number)
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                prior = self._required_job(connection, job_id=job_id)
                _require_worker_lease(
                    prior,
                    owner=owner,
                    attempt_number=attempt_number,
                    now=normalized_now,
                    state=JobState.RUNNING,
                )
                current = _replace_job(
                    prior,
                    state=JobState.RUNNING,
                    lease_expires_at=normalized_now + JOB_LEASE_DURATION,
                    updated_at=normalized_now,
                )
                self._persist_transition(connection, prior=prior, current=current)
                return current
        except (JobConflictError, JobNotFoundError, JobValidationError):
            raise
        except SQLAlchemyError:
            raise JobPersistenceError("job heartbeat could not be persisted") from None

    def claim_outbox(self, *, owner: str, now: datetime) -> ClaimedOutboxEvent | None:
        """Claim one due outbox event for an injected publisher."""

        _require_resource_id(owner, field_name="outbox lease owner")
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                statement = (
                    select(aqa_outbox_events_contract)
                    .where(
                        or_(
                            and_(
                                aqa_outbox_events_contract.c.state.in_(
                                    (OutboxState.PENDING.value, OutboxState.FAILED.value)
                                ),
                                aqa_outbox_events_contract.c.next_attempt_at <= normalized_now,
                            ),
                            and_(
                                aqa_outbox_events_contract.c.state == OutboxState.CLAIMED.value,
                                aqa_outbox_events_contract.c.lease_expires_at <= normalized_now,
                            ),
                        )
                    )
                    .order_by(
                        aqa_outbox_events_contract.c.created_at,
                        aqa_outbox_events_contract.c.outbox_event_id,
                    )
                    .limit(1)
                )
                if connection.dialect.name == "postgresql":
                    statement = statement.with_for_update(skip_locked=True)
                row = connection.execute(statement).mappings().first()
                if row is None:
                    return None
                _validate_outbox_row(row)
                _require_forward_time(cast(datetime, row["updated_at"]), normalized_now)
                attempts = int(row["attempt_count"])
                if attempts >= JOB_MAX_ATTEMPTS:
                    self._update_outbox_dead(connection, row=row, now=normalized_now)
                    return None
                values = _outbox_row_values(row)
                values.update(
                    state=OutboxState.CLAIMED.value,
                    attempt_count=attempts + 1,
                    next_attempt_at=None,
                    lease_owner=owner,
                    lease_expires_at=normalized_now + JOB_LEASE_DURATION,
                    safe_last_error_code=None,
                    safe_last_error_message=None,
                    updated_at=normalized_now,
                    version=int(row["version"]) + 1,
                )
                values["content_hash"] = _outbox_content_hash(values)
                self._persist_outbox(connection, prior=row, values=values)
                return _claimed_outbox_event(values)
        except JobValidationError:
            raise JobPersistenceError("persisted outbox state is malformed") from None
        except SQLAlchemyError:
            raise JobPersistenceError("outbox event could not be claimed") from None

    def heartbeat_outbox(
        self,
        *,
        event_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
    ) -> ClaimedOutboxEvent:
        """Extend one exact outbox delivery attempt and return its fenced claim."""

        _require_resource_id(event_id, field_name="outbox event ID")
        _require_resource_id(owner, field_name="outbox lease owner")
        _require_attempt_number(attempt_number)
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                row = self._required_outbox(connection, event_id=event_id)
                _require_outbox_lease(
                    row,
                    owner=owner,
                    attempt_number=attempt_number,
                    now=normalized_now,
                )
                values = _outbox_row_values(row)
                values.update(
                    lease_expires_at=normalized_now + JOB_LEASE_DURATION,
                    updated_at=normalized_now,
                    version=int(row["version"]) + 1,
                )
                values["content_hash"] = _outbox_content_hash(values)
                self._persist_outbox(connection, prior=row, values=values)
                return _claimed_outbox_event(values)
        except (JobConflictError, JobNotFoundError, JobValidationError):
            raise
        except SQLAlchemyError:
            raise JobPersistenceError("outbox heartbeat could not be persisted") from None

    def publish_outbox(
        self,
        *,
        event_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
    ) -> None:
        """Mark a claimed outbox event published after the injected publisher succeeds."""

        self._complete_outbox(
            event_id=event_id,
            owner=owner,
            attempt_number=attempt_number,
            now=now,
            published=True,
            error=None,
        )

    def fail_outbox(
        self,
        *,
        event_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
        error: SafeJobError,
    ) -> None:
        """Record a safe outbox failure with bounded retry behavior."""

        if type(error) is not SafeJobError:
            raise JobValidationError("outbox failure requires a safe error contract")
        self._complete_outbox(
            event_id=event_id,
            owner=owner,
            attempt_number=attempt_number,
            now=now,
            published=False,
            error=error,
        )

    def _worker_transition(
        self,
        *,
        job_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
        required_state: JobState,
        target_state: JobState,
        attempt_transition: JobAttemptTransition,
        safe_error: SafeJobError | None = None,
        result_artifact_id: str | None = None,
    ) -> JobRecord:
        _require_resource_id(job_id, field_name="job ID")
        _require_resource_id(owner, field_name="job lease owner")
        _require_attempt_number(attempt_number)
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                prior = self._required_job(connection, job_id=job_id)
                _require_worker_lease(
                    prior,
                    owner=owner,
                    attempt_number=attempt_number,
                    now=normalized_now,
                    state=required_state,
                )
                if target_state is JobState.RUNNING:
                    current = _replace_job(
                        prior,
                        state=target_state,
                        started_at=normalized_now,
                        updated_at=normalized_now,
                    )
                    sequence = 2
                elif target_state is JobState.SUCCEEDED:
                    current = _replace_job(
                        prior,
                        state=target_state,
                        lease_owner=None,
                        lease_expires_at=None,
                        claimed_at=None,
                        started_at=None,
                        completed_at=normalized_now,
                        result_artifact_id=result_artifact_id,
                        updated_at=normalized_now,
                    )
                    sequence = 3
                else:
                    terminal = prior.attempt_count >= JOB_MAX_ATTEMPTS
                    actual_state = JobState.DEAD if terminal else JobState.FAILED
                    current = _replace_job(
                        prior,
                        state=actual_state,
                        next_attempt_at=(
                            None if terminal else normalized_now + retry_delay(prior.attempt_count)
                        ),
                        lease_owner=None,
                        lease_expires_at=None,
                        claimed_at=None,
                        started_at=None,
                        completed_at=normalized_now if terminal else None,
                        safe_error=safe_error,
                        updated_at=normalized_now,
                    )
                    sequence = 3
                self._persist_transition(connection, prior=prior, current=current)
                self._append_attempt(
                    connection,
                    job=current,
                    transition=attempt_transition,
                    owner=owner,
                    occurred_at=normalized_now,
                    sequence=sequence,
                    safe_error=safe_error,
                )
                self._append_transition_evidence(
                    connection,
                    job=current,
                    event_type=f"job.{current.state.value.lower()}",
                    reason_code=safe_error.code if safe_error is not None else None,
                )
                return current
        except (JobConflictError, JobNotFoundError, JobValidationError):
            raise
        except SQLAlchemyError:
            raise JobPersistenceError("job transition could not be persisted") from None

    def _claim_candidates(
        self,
        connection: Connection,
        *,
        now: datetime,
        job_types: frozenset[JobType],
    ) -> tuple[JobRecord, ...]:
        statement = (
            select(aqa_jobs_contract)
            .where(
                aqa_jobs_contract.c.job_type.in_(sorted(item.value for item in job_types)),
                or_(
                    and_(
                        aqa_jobs_contract.c.state.in_(
                            (JobState.PENDING.value, JobState.FAILED.value)
                        ),
                        aqa_jobs_contract.c.next_attempt_at <= now,
                    ),
                    and_(
                        aqa_jobs_contract.c.state.in_(
                            (JobState.CLAIMED.value, JobState.RUNNING.value)
                        ),
                        aqa_jobs_contract.c.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(
                aqa_jobs_contract.c.next_attempt_at,
                aqa_jobs_contract.c.created_at,
                aqa_jobs_contract.c.job_id,
            )
            .limit(32)
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        return tuple(_job_from_row(row) for row in connection.execute(statement).mappings())

    def _recover_expired(
        self,
        connection: Connection,
        *,
        job: JobRecord,
        now: datetime,
    ) -> JobRecord:
        terminal = job.attempt_count >= JOB_MAX_ATTEMPTS
        error = SafeJobError("lease_expired", "Worker lease expired before completion")
        recovered = _replace_job(
            job,
            state=JobState.DEAD if terminal else JobState.FAILED,
            next_attempt_at=None if terminal else now + retry_delay(job.attempt_count),
            lease_owner=None,
            lease_expires_at=None,
            claimed_at=None,
            started_at=None,
            completed_at=now if terminal else None,
            safe_error=error,
            updated_at=now,
        )
        self._persist_transition(connection, prior=job, current=recovered)
        self._append_attempt(
            connection,
            job=recovered,
            transition=JobAttemptTransition.ABANDONED,
            owner=cast(str, job.lease_owner),
            occurred_at=now,
            sequence=3 if job.state is JobState.RUNNING else 2,
            safe_error=error,
        )
        self._append_transition_evidence(
            connection,
            job=recovered,
            event_type=f"job.{recovered.state.value.lower()}",
            reason_code=error.code,
        )
        return recovered

    def _required_job(self, connection: Connection, *, job_id: str) -> JobRecord:
        job = self._load_job(connection, job_id=job_id, for_update=True)
        if job is None:
            raise JobNotFoundError("job does not exist")
        return job

    def _required_outbox(self, connection: Connection, *, event_id: str) -> RowMapping:
        statement = select(aqa_outbox_events_contract).where(
            aqa_outbox_events_contract.c.outbox_event_id == event_id
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise JobNotFoundError("outbox event does not exist")
        _validate_outbox_row(row)
        return row

    def _load_job(
        self,
        connection: Connection,
        *,
        job_id: str,
        for_update: bool,
    ) -> JobRecord | None:
        statement = select(aqa_jobs_contract).where(aqa_jobs_contract.c.job_id == job_id)
        if for_update and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        return None if row is None else _job_from_row(row)

    def _persist_transition(
        self,
        connection: Connection,
        *,
        prior: JobRecord,
        current: JobRecord,
    ) -> None:
        values = _job_values(current)
        result = connection.execute(
            update(aqa_jobs_contract)
            .where(
                aqa_jobs_contract.c.job_id == prior.job_id,
                aqa_jobs_contract.c.version == prior.version,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise JobConflictError("job changed concurrently")

    def _append_attempt(
        self,
        connection: Connection,
        *,
        job: JobRecord,
        transition: JobAttemptTransition,
        owner: str,
        occurred_at: datetime,
        sequence: int,
        lease_expires_at: datetime | None = None,
        safe_error: SafeJobError | None = None,
    ) -> None:
        content = {
            "job_id": job.job_id,
            "attempt_number": job.attempt_count,
            "sequence": sequence,
            "transition": transition.value,
            "owner": owner,
            "occurred_at": occurred_at,
            "lease_expires_at": lease_expires_at,
            "safe_error_code": None if safe_error is None else safe_error.code,
            "safe_error_message": None if safe_error is None else safe_error.message,
        }
        content_hash = sha256_hex(content)
        connection.execute(
            insert(aqa_job_attempts_contract).values(
                job_attempt_event_id=f"jobattempt_{content_hash}",
                content_hash=content_hash,
                **content,
            )
        )

    def _append_transition_evidence(
        self,
        connection: Connection,
        *,
        job: JobRecord,
        event_type: str,
        reason_code: str | None = None,
    ) -> None:
        payload: dict[str, JsonValue] = {
            "job_id": job.job_id,
            "job_type": job.job_type.value.lower(),
            "correlation_id": job.correlation_id,
            "state": job.state.value.lower(),
            "attempt_count": job.attempt_count,
            "version": job.version,
        }
        if reason_code is not None:
            payload["reason_code"] = reason_code
        outbox_hash = sha256_hex(("job-outbox", job.job_id, job.version, event_type, payload))
        outbox_values: dict[str, object] = {
            "outbox_event_id": f"outbox_{outbox_hash}",
            "aggregate_type": "job",
            "aggregate_id": job.job_id,
            "event_type": event_type,
            "schema_version": JOB_SCHEMA_VERSION,
            "payload": payload,
            "payload_hash": sha256_hex(payload),
            "state": OutboxState.PENDING.value,
            "attempt_count": 0,
            "next_attempt_at": job.updated_at,
            "lease_owner": None,
            "lease_expires_at": None,
            "published_at": None,
            "safe_last_error_code": None,
            "safe_last_error_message": None,
            "created_at": job.updated_at,
            "updated_at": job.updated_at,
            "version": 1,
        }
        outbox_values["content_hash"] = _outbox_content_hash(outbox_values)
        connection.execute(insert(aqa_outbox_events_contract).values(**outbox_values))

        audit_payload = dict(payload)
        audit_payload["idempotency_key"] = f"jobevent_{outbox_hash}"
        self._audit.append(
            stream_id=f"{self._audit_writer.value}:job:{job.job_id}",
            event_type=event_type,
            occurred_at=job.updated_at,
            payload=AuditPayload.from_mapping(audit_payload),
            connection=connection,
        )

    def _complete_outbox(
        self,
        *,
        event_id: str,
        owner: str,
        attempt_number: int,
        now: datetime,
        published: bool,
        error: SafeJobError | None,
    ) -> None:
        _require_resource_id(event_id, field_name="outbox event ID")
        _require_resource_id(owner, field_name="outbox lease owner")
        _require_attempt_number(attempt_number)
        normalized_now = _utc(now)
        try:
            with self.transaction() as connection:
                row = self._required_outbox(connection, event_id=event_id)
                _require_outbox_lease(
                    row,
                    owner=owner,
                    attempt_number=attempt_number,
                    now=normalized_now,
                )
                attempts = int(row["attempt_count"])
                terminal = not published and attempts >= JOB_MAX_ATTEMPTS
                values = _outbox_row_values(row)
                values.update(
                    state=(
                        OutboxState.PUBLISHED.value
                        if published
                        else OutboxState.DEAD.value
                        if terminal
                        else OutboxState.FAILED.value
                    ),
                    next_attempt_at=(
                        None if published or terminal else normalized_now + retry_delay(attempts)
                    ),
                    lease_owner=None,
                    lease_expires_at=None,
                    published_at=normalized_now if published else None,
                    safe_last_error_code=None if error is None else error.code,
                    safe_last_error_message=None if error is None else error.message,
                    updated_at=normalized_now,
                    version=int(row["version"]) + 1,
                )
                values["content_hash"] = _outbox_content_hash(values)
                self._persist_outbox(connection, prior=row, values=values)
        except (JobConflictError, JobNotFoundError, JobValidationError):
            raise
        except SQLAlchemyError:
            raise JobPersistenceError("outbox transition could not be persisted") from None

    def _update_outbox_dead(
        self,
        connection: Connection,
        *,
        row: RowMapping,
        now: datetime,
    ) -> None:
        error = SafeJobError("delivery_attempts_exhausted", "Outbox delivery attempts exhausted")
        values = _outbox_row_values(row)
        values.update(
            state=OutboxState.DEAD.value,
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            safe_last_error_code=error.code,
            safe_last_error_message=error.message,
            updated_at=now,
            version=int(row["version"]) + 1,
        )
        values["content_hash"] = _outbox_content_hash(values)
        self._persist_outbox(connection, prior=row, values=values)

    def _persist_outbox(
        self,
        connection: Connection,
        *,
        prior: RowMapping,
        values: dict[str, object],
    ) -> None:
        event_id = cast(str, prior["outbox_event_id"])
        prior_version = cast(int, prior["version"])
        result = connection.execute(
            update(aqa_outbox_events_contract)
            .where(
                aqa_outbox_events_contract.c.outbox_event_id == event_id,
                aqa_outbox_events_contract.c.version == prior_version,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise JobConflictError("outbox event changed concurrently")

    def _load_after_create_race(
        self,
        *,
        job_id: str,
        request: JobCreateRequest,
    ) -> JobRecord:
        job = self.get(job_id)
        if job is None or not _same_create_request(job, request):
            raise JobConflictError("job idempotency key conflicts with existing content")
        return job


def _job_id(request: JobCreateRequest) -> str:
    digest = sha256_hex(
        (
            "job-identity",
            request.payload.job_type.value,
            request.idempotency_key,
        )
    )
    return f"job_{digest}"


def _new_job_values(*, job_id: str, request: JobCreateRequest) -> dict[str, object]:
    values: dict[str, object] = {
        "job_id": job_id,
        "job_type": request.payload.job_type.value,
        "schema_version": JOB_SCHEMA_VERSION,
        "payload": request.payload.value,
        "payload_hash": request.payload.payload_hash,
        "idempotency_key": request.idempotency_key,
        "correlation_id": request.correlation_id,
        "state": JobState.PENDING.value,
        "attempt_count": 0,
        "max_attempts": JOB_MAX_ATTEMPTS,
        "next_attempt_at": request.requested_at,
        "lease_owner": None,
        "lease_expires_at": None,
        "claimed_at": None,
        "started_at": None,
        "completed_at": None,
        "safe_last_error_code": None,
        "safe_last_error_message": None,
        "result_artifact_id": None,
        "created_at": request.requested_at,
        "updated_at": request.requested_at,
        "version": 1,
    }
    values["content_hash"] = _job_content_hash(values)
    return values


def _job_values(job: JobRecord) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "schema_version": job.schema_version,
        "payload": job.payload.value,
        "payload_hash": job.payload.payload_hash,
        "idempotency_key": job.idempotency_key,
        "correlation_id": job.correlation_id,
        "state": job.state.value,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "next_attempt_at": job.next_attempt_at,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at,
        "claimed_at": job.claimed_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "safe_last_error_code": job.safe_last_error_code,
        "safe_last_error_message": job.safe_last_error_message,
        "result_artifact_id": job.result_artifact_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "content_hash": job.content_hash,
        "version": job.version,
    }


def _job_from_values(values: dict[str, object]) -> JobRecord:
    return _job_from_mapping(values)


def _job_from_row(row: RowMapping) -> JobRecord:
    return _job_from_mapping(dict(row))


def _job_from_mapping(values: dict[str, object]) -> JobRecord:
    try:
        job_type = JobType(cast(str, values["job_type"]))
        payload_mapping = _mapping(values["payload"])
        payload = JobPayload(
            job_type=job_type,
            payload_json=canonical_json_bytes(payload_mapping).decode("utf-8"),
            payload_hash=cast(str, values["payload_hash"]),
        )
        record = JobRecord(
            job_id=cast(str, values["job_id"]),
            job_type=job_type,
            schema_version=cast(int, values["schema_version"]),
            payload=payload,
            idempotency_key=cast(str, values["idempotency_key"]),
            correlation_id=cast(str, values["correlation_id"]),
            state=JobState(cast(str, values["state"])),
            attempt_count=cast(int, values["attempt_count"]),
            max_attempts=cast(int, values["max_attempts"]),
            next_attempt_at=cast(datetime | None, values["next_attempt_at"]),
            lease_owner=cast(str | None, values["lease_owner"]),
            lease_expires_at=cast(datetime | None, values["lease_expires_at"]),
            claimed_at=cast(datetime | None, values["claimed_at"]),
            started_at=cast(datetime | None, values["started_at"]),
            completed_at=cast(datetime | None, values["completed_at"]),
            safe_last_error_code=cast(str | None, values["safe_last_error_code"]),
            safe_last_error_message=cast(str | None, values["safe_last_error_message"]),
            result_artifact_id=cast(str | None, values["result_artifact_id"]),
            created_at=cast(datetime, values["created_at"]),
            updated_at=cast(datetime, values["updated_at"]),
            content_hash=cast(str, values["content_hash"]),
            version=cast(int, values["version"]),
        )
        if _job_content_hash(_job_values(record)) != record.content_hash:
            raise JobValidationError("persisted job content hash is invalid")
        return record
    except (KeyError, TypeError, ValueError):
        raise JobValidationError("persisted job state is malformed") from None


def _replace_job(
    prior: JobRecord,
    *,
    state: JobState,
    updated_at: datetime,
    attempt_count: int | None = None,
    next_attempt_at: datetime | _Unchanged | None = _UNCHANGED,
    lease_owner: str | _Unchanged | None = _UNCHANGED,
    lease_expires_at: datetime | _Unchanged | None = _UNCHANGED,
    claimed_at: datetime | _Unchanged | None = _UNCHANGED,
    started_at: datetime | _Unchanged | None = _UNCHANGED,
    completed_at: datetime | _Unchanged | None = _UNCHANGED,
    safe_error: SafeJobError | _Unchanged | None = _UNCHANGED,
    result_artifact_id: str | _Unchanged | None = _UNCHANGED,
) -> JobRecord:
    def select_value(new: object, old: object) -> object:
        return old if new is _UNCHANGED else new

    if safe_error is _UNCHANGED:
        selected_error: SafeJobError | None = (
            None
            if prior.safe_last_error_code is None
            else SafeJobError(
                prior.safe_last_error_code,
                cast(str, prior.safe_last_error_message),
            )
        )
    else:
        selected_error = cast(SafeJobError | None, safe_error)
    values: dict[str, object] = {
        **_job_values(prior),
        "state": state.value,
        "attempt_count": prior.attempt_count if attempt_count is None else attempt_count,
        "next_attempt_at": select_value(next_attempt_at, prior.next_attempt_at),
        "lease_owner": select_value(lease_owner, prior.lease_owner),
        "lease_expires_at": select_value(lease_expires_at, prior.lease_expires_at),
        "claimed_at": select_value(claimed_at, prior.claimed_at),
        "started_at": select_value(started_at, prior.started_at),
        "completed_at": select_value(completed_at, prior.completed_at),
        "safe_last_error_code": (None if selected_error is None else selected_error.code),
        "safe_last_error_message": (None if selected_error is None else selected_error.message),
        "result_artifact_id": select_value(result_artifact_id, prior.result_artifact_id),
        "updated_at": updated_at,
        "version": prior.version + 1,
    }
    values["content_hash"] = _job_content_hash(values)
    return _job_from_values(values)


def _job_content_hash(values: dict[str, object]) -> str:
    return sha256_hex(
        (
            "job-state-v1",
            {key: value for key, value in values.items() if key != "content_hash"},
        )
    )


def _outbox_content_hash(values: dict[str, object]) -> str:
    return sha256_hex(
        (
            "outbox-state-v1",
            {key: value for key, value in values.items() if key != "content_hash"},
        )
    )


def _outbox_row_values(row: RowMapping) -> dict[str, object]:
    return {str(column.name): row[column.name] for column in aqa_outbox_events_contract.columns}


def _claimed_outbox_event(values: dict[str, object]) -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        outbox_event_id=cast(str, values["outbox_event_id"]),
        event_type=cast(str, values["event_type"]),
        aggregate_id=cast(str, values["aggregate_id"]),
        payload=_mapping(values["payload"]),
        attempt_count=cast(int, values["attempt_count"]),
        lease_owner=cast(str, values["lease_owner"]),
        lease_expires_at=cast(datetime, values["lease_expires_at"]),
        updated_at=cast(datetime, values["updated_at"]),
    )


def _validate_outbox_row(row: RowMapping) -> None:
    values = _outbox_row_values(row)
    try:
        payload = _mapping(values["payload"])
        state = OutboxState(cast(str, values["state"]))
        attempt_count = cast(int, values["attempt_count"])
        version = cast(int, values["version"])
        payload_hash = cast(str, values["payload_hash"])
        content_hash = cast(str, values["content_hash"])
        aggregate_id = _require_resource_id(
            values["aggregate_id"], field_name="outbox aggregate ID"
        )
        event_type = _require_resource_id(values["event_type"], field_name="outbox event type")
        event_id = _require_resource_id(values["outbox_event_id"], field_name="outbox event ID")
        created_at = _utc(values["created_at"])
        updated_at = _utc(values["updated_at"])
        if (
            values["aggregate_type"] != "job"
            or values["schema_version"] != JOB_SCHEMA_VERSION
            or type(attempt_count) is not int
            or not 0 <= attempt_count <= JOB_MAX_ATTEMPTS
            or type(version) is not int
            or version < 1
            or sha256_hex(payload) != payload_hash
            or _outbox_content_hash(values) != content_hash
        ):
            raise JobValidationError("persisted outbox state is malformed")
        _validate_outbox_payload(
            event_id=event_id,
            event_type=event_type,
            aggregate_id=aggregate_id,
            payload=payload,
        )
        if updated_at < created_at:
            raise JobValidationError("persisted outbox timestamps are not monotonic")
        lease_present = values["lease_owner"] is not None and values["lease_expires_at"] is not None
        if lease_present is not (state is OutboxState.CLAIMED):
            raise JobValidationError("persisted outbox state is malformed")
        if state is OutboxState.CLAIMED:
            _require_resource_id(values["lease_owner"], field_name="outbox lease owner")
            if _utc(values["lease_expires_at"]) <= updated_at:
                raise JobValidationError("persisted outbox lease timestamp is invalid")
        next_attempt_at = values["next_attempt_at"]
        retryable = state in {OutboxState.PENDING, OutboxState.FAILED}
        if retryable != (next_attempt_at is not None):
            raise JobValidationError("persisted outbox retry state is malformed")
        if next_attempt_at is not None and _utc(next_attempt_at) < updated_at:
            raise JobValidationError("persisted outbox retry timestamp is invalid")
        published_at = values["published_at"]
        if (state is OutboxState.PUBLISHED) != (published_at is not None):
            raise JobValidationError("persisted outbox publication state is malformed")
        if published_at is not None and _utc(published_at) != updated_at:
            raise JobValidationError("persisted outbox publication timestamp is invalid")
        error_present = values["safe_last_error_code"] is not None
        if error_present != (values["safe_last_error_message"] is not None):
            raise JobValidationError("persisted outbox error fields are malformed")
        if error_present != (state in {OutboxState.FAILED, OutboxState.DEAD}):
            raise JobValidationError("persisted outbox error state is malformed")
        if error_present:
            SafeJobError(
                cast(str, values["safe_last_error_code"]),
                cast(str, values["safe_last_error_message"]),
            )
    except (KeyError, TypeError, ValueError):
        raise JobValidationError("persisted outbox state is malformed") from None


def _validate_outbox_payload(
    *,
    event_id: str,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, JsonValue],
) -> None:
    base_keys = frozenset(
        {"job_id", "job_type", "correlation_id", "state", "attempt_count", "version"}
    )
    keys = frozenset(payload)
    if keys not in {base_keys, base_keys | {"reason_code"}}:
        raise JobValidationError("persisted outbox payload fields are malformed")
    job_id = _require_resource_id(payload.get("job_id"), field_name="outbox payload job ID")
    _require_resource_id(payload.get("correlation_id"), field_name="outbox correlation ID")
    try:
        job_type = JobType(str(payload.get("job_type")).upper())
        job_state = JobState(str(payload.get("state")).upper())
    except ValueError:
        raise JobValidationError("persisted outbox payload routing is malformed") from None
    if (
        payload.get("job_type") != job_type.value.lower()
        or payload.get("state") != job_state.value.lower()
    ):
        raise JobValidationError("persisted outbox payload routing is malformed")
    attempt_count = payload.get("attempt_count")
    job_version = payload.get("version")
    if (
        job_id != aggregate_id
        or type(attempt_count) is not int
        or not 0 <= attempt_count <= JOB_MAX_ATTEMPTS
        or type(job_version) is not int
        or job_version < 1
    ):
        raise JobValidationError("persisted outbox payload identity is malformed")
    expected_event_type = (
        "job.created"
        if event_type == "job.created" and job_state is JobState.PENDING
        else f"job.{job_state.value.lower()}"
    )
    reason = payload.get("reason_code")
    failure_event = job_state in {JobState.FAILED, JobState.DEAD}
    if (
        event_type != expected_event_type
        or failure_event != ("reason_code" in payload)
        or (
            reason is not None
            and _require_resource_id(reason, field_name="outbox reason code") != reason
        )
    ):
        raise JobValidationError("persisted outbox payload event binding is malformed")
    expected_hash = sha256_hex(("job-outbox", job_id, job_version, event_type, payload))
    if event_id != f"outbox_{expected_hash}":
        raise JobValidationError("persisted outbox event identity is malformed")


def _same_create_request(job: JobRecord, request: JobCreateRequest) -> bool:
    return (
        job.job_type is request.payload.job_type
        and job.payload.payload_hash == request.payload.payload_hash
        and job.idempotency_key == request.idempotency_key
        and job.correlation_id == request.correlation_id
    )


def _require_worker_lease(
    job: JobRecord,
    *,
    owner: str,
    attempt_number: int,
    now: datetime,
    state: JobState,
) -> None:
    _require_forward_time(job.updated_at, now)
    if (
        job.state is not state
        or job.lease_owner != owner
        or job.attempt_count != attempt_number
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        raise JobConflictError("job worker lease is not valid")


def _require_outbox_lease(
    row: RowMapping,
    *,
    owner: str,
    attempt_number: int,
    now: datetime,
) -> None:
    _validate_outbox_row(row)
    _require_forward_time(cast(datetime, row["updated_at"]), now)
    if (
        row["state"] != OutboxState.CLAIMED.value
        or row["lease_owner"] != owner
        or row["attempt_count"] != attempt_number
        or cast(datetime, row["lease_expires_at"]) <= now
    ):
        raise JobConflictError("outbox lease is not valid")


def _require_attempt_number(value: object) -> int:
    if type(value) is not int or not 1 <= value <= JOB_MAX_ATTEMPTS:
        raise JobValidationError("lease attempt number is invalid")
    return value


def _require_forward_time(previous: datetime, current: datetime) -> None:
    if current < previous:
        raise JobConflictError("transition timestamp is older than durable state")


def _mapping(value: object) -> dict[str, JsonValue]:
    if type(value) is str:
        try:
            value = json.loads(value)
        except ValueError:
            raise JobValidationError("persisted JSON object is malformed") from None
    if type(value) is not dict:
        raise JobValidationError("persisted JSON object is malformed")
    try:
        normalized = json.loads(canonical_json_bytes(value))
    except (TypeError, UnicodeError, ValueError):
        raise JobValidationError("persisted JSON object is malformed") from None
    return cast(dict[str, JsonValue], normalized)


def _require_resource_id(value: object, *, field_name: str) -> str:
    from adaptive_trader.platform.jobs.models import _require_identifier

    return _require_identifier(value, field_name=field_name)


def _utc(value: object) -> datetime:
    from adaptive_trader.platform.jobs.models import _require_utc

    return _require_utc(value, field_name="job transition timestamp")


@contextmanager
def initialized_sqlite_job_schema(engine: Engine) -> Iterator[None]:
    """Create only the isolated test schema; operational setup always uses Alembic."""

    if engine.dialect.name != "sqlite":
        raise ValueError("the isolated schema helper is SQLite-only")
    from adaptive_trader.platform.jobs.schema import job_metadata
    from adaptive_trader.platform.storage.tables import aqa_audit_events

    job_metadata.create_all(engine)
    aqa_audit_events.create(engine, checkfirst=True)
    try:
        yield
    finally:
        pass
