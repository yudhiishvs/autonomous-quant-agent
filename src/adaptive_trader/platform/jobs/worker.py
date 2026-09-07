"""Bounded workers with an explicit, non-dynamic job dispatch table."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from adaptive_trader.platform.jobs.models import (
    JOB_LEASE_DURATION,
    ClaimedOutboxEvent,
    JobPayload,
    JobRecord,
    JobState,
    JobType,
    JobValidationError,
    SafeJobError,
    require_job_types,
)
from adaptive_trader.platform.jobs.repository import (
    JobConflictError,
    JobPersistenceError,
    JobRepository,
)

_DEFAULT_HEARTBEAT_SECONDS = JOB_LEASE_DURATION.total_seconds() / 3
JobTransitionObserver = Callable[[JobState], None]


class JobHandler(Protocol):
    """Operator-wired handler that must re-read authoritative state before acting."""

    def __call__(self, *, job: JobRecord, lease: JobLeaseGuard) -> str | None: ...


class OutboxPublisher(Protocol):
    """Injected delivery boundary; payloads never select a transport or destination."""

    def publish(
        self,
        *,
        outbox_event_id: str,
        event_type: str,
        aggregate_id: str,
        payload: object,
    ) -> None: ...


class JobExecutionError(RuntimeError):
    """A handler failure carrying only pre-sanitized persistent metadata."""

    def __init__(self, error: SafeJobError) -> None:
        if type(error) is not SafeJobError:
            raise TypeError("job execution error requires safe metadata")
        self.error = error
        super().__init__(error.code)


class _HeartbeatLeaseGuard:
    """Serialize lease extensions and remember the first fail-closed loss."""

    def __init__(self, heartbeat: Callable[[], object], *, interval_seconds: float) -> None:
        if not callable(heartbeat):
            raise TypeError("lease guard requires a heartbeat callback")
        if (
            type(interval_seconds) not in {float, int}
            or not 0 < interval_seconds < JOB_LEASE_DURATION.total_seconds()
        ):
            raise ValueError("lease heartbeat interval is invalid")
        self._heartbeat = heartbeat
        self._interval_seconds = float(interval_seconds)
        self._lock = threading.Lock()
        self._lost: JobConflictError | None = None

    def checkpoint(self) -> object:
        with self._lock:
            if self._lost is not None:
                raise self._lost
            try:
                return self._heartbeat()
            except (JobPersistenceError, JobValidationError):
                self._lost = JobConflictError("worker lease could not be maintained")
                raise self._lost from None

    @contextmanager
    def keep_alive(self) -> Iterator[None]:
        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop,),
            name="aqa-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=self._interval_seconds)
            if thread.is_alive():
                with self._lock:
                    self._lost = JobConflictError("worker lease heartbeat did not stop")
                raise self._lost

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval_seconds):
            try:
                self.checkpoint()
            except JobConflictError:
                return


class JobLeaseGuard:
    """A fenced, renewable lease capability supplied to every fixed job handler."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        job: JobRecord,
        owner: str,
        clock: Callable[[], datetime],
        interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if not isinstance(repository, JobRepository) or type(job) is not JobRecord:
            raise TypeError("job lease guard requires durable job state")
        if job.lease_owner != owner or job.attempt_count < 1 or not callable(clock):
            raise ValueError("job lease guard requires the active attempt")
        self._repository = repository
        self._job_id = job.job_id
        self._owner = owner
        self._attempt_number = job.attempt_count
        self._clock = clock
        self._guard = _HeartbeatLeaseGuard(
            self._heartbeat,
            interval_seconds=interval_seconds,
        )

    def _heartbeat(self) -> JobRecord:
        from adaptive_trader.platform.jobs.repository import _utc

        return self._repository.heartbeat(
            job_id=self._job_id,
            owner=self._owner,
            attempt_number=self._attempt_number,
            now=_utc(self._clock()),
        )

    def checkpoint(self) -> JobRecord:
        return cast(JobRecord, self._guard.checkpoint())

    def keep_alive(self) -> AbstractContextManager[None]:
        return self._guard.keep_alive()


class _OutboxLeaseGuard:
    def __init__(
        self,
        repository: JobRepository,
        *,
        event: ClaimedOutboxEvent,
        owner: str,
        clock: Callable[[], datetime],
        interval_seconds: float,
    ) -> None:
        self._repository = repository
        self._event_id = event.outbox_event_id
        self._owner = owner
        self._attempt_number = event.attempt_count
        self._clock = clock
        self._guard = _HeartbeatLeaseGuard(
            self._heartbeat,
            interval_seconds=interval_seconds,
        )

    def _heartbeat(self) -> ClaimedOutboxEvent:
        from adaptive_trader.platform.jobs.repository import _utc

        return self._repository.heartbeat_outbox(
            event_id=self._event_id,
            owner=self._owner,
            attempt_number=self._attempt_number,
            now=_utc(self._clock()),
        )

    def checkpoint(self) -> ClaimedOutboxEvent:
        return cast(ClaimedOutboxEvent, self._guard.checkpoint())

    def keep_alive(self) -> AbstractContextManager[None]:
        return self._guard.keep_alive()


@dataclass(frozen=True, slots=True)
class BoundedJobHandlers:
    """All handlers are supplied explicitly; payloads cannot name executable code."""

    data_quality_audit: JobHandler
    gap_repair: JobHandler
    dataset_freeze: JobHandler
    offline_demo: JobHandler

    def for_type(self, job_type: JobType) -> JobHandler:
        if type(job_type) is not JobType:
            raise TypeError("job type must use the closed contract")
        return {
            JobType.DATA_QUALITY_AUDIT: self.data_quality_audit,
            JobType.GAP_REPAIR: self.gap_repair,
            JobType.DATASET_FREEZE: self.dataset_freeze,
            JobType.OFFLINE_DEMO: self.offline_demo,
        }[job_type]


class DurableJobWorker:
    """Run at most one claimed job through the fixed handler allowlist."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        owner: str,
        handlers: BoundedJobHandlers,
        clock: Callable[[], datetime],
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        transition_observer: JobTransitionObserver | None = None,
        job_types: frozenset[JobType] = frozenset(JobType),
    ) -> None:
        if not isinstance(repository, JobRepository):
            raise TypeError("job worker requires a job repository")
        if type(handlers) is not BoundedJobHandlers:
            raise TypeError("job worker requires the bounded handler set")
        if not callable(clock):
            raise TypeError("job worker requires an injected clock")
        if transition_observer is not None and not callable(transition_observer):
            raise TypeError("job transition observer must be callable")
        self._repository = repository
        self._owner = owner
        self._handlers = handlers
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._transition_observer = transition_observer
        self._job_types = require_job_types(job_types)

    def run_one(self) -> JobRecord | None:
        from adaptive_trader.platform.jobs.repository import _utc

        claimed = self._repository.claim_next(
            owner=self._owner,
            now=_utc(self._clock()),
            job_types=self._job_types,
        )
        if claimed is None:
            return None
        self._observe(claimed)
        running = self._repository.mark_running(
            job_id=claimed.job_id,
            owner=self._owner,
            attempt_number=claimed.attempt_count,
            now=_utc(self._clock()),
        )
        self._observe(running)
        handler = self._handlers.for_type(running.job_type)
        lease = JobLeaseGuard(
            self._repository,
            job=running,
            owner=self._owner,
            clock=self._clock,
            interval_seconds=self._heartbeat_interval_seconds,
        )
        error: SafeJobError | None = None
        artifact_id: str | None = None
        with lease.keep_alive():
            try:
                artifact_id = handler(job=running, lease=lease)
            except JobExecutionError as execution_error:
                error = execution_error.error
            except Exception:
                error = SafeJobError("worker_failure", "Job handler failed without safe details")
            completion = lease.checkpoint()
        if error is not None:
            result = self._repository.fail(
                job_id=running.job_id,
                owner=self._owner,
                attempt_number=running.attempt_count,
                now=completion.updated_at,
                error=error,
            )
        else:
            result = self._repository.succeed(
                job_id=running.job_id,
                owner=self._owner,
                attempt_number=running.attempt_count,
                now=completion.updated_at,
                result_artifact_id=artifact_id,
            )
        self._observe(result)
        return result

    def _observe(self, job: JobRecord) -> None:
        observer = self._transition_observer
        if observer is None:
            return
        try:
            observer(job.state)
        except Exception:
            return


class DurableOutboxWorker:
    """Deliver at most one outbox event through an injected fixed publisher."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        owner: str,
        publisher: OutboxPublisher,
        clock: Callable[[], datetime],
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if not isinstance(repository, JobRepository):
            raise TypeError("outbox worker requires a job repository")
        if not hasattr(publisher, "publish") or not callable(publisher.publish):
            raise TypeError("outbox worker requires an injected publisher")
        if not callable(clock):
            raise TypeError("outbox worker requires an injected clock")
        self._repository = repository
        self._owner = owner
        self._publisher = publisher
        self._clock = clock
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    def run_one(self) -> bool:
        from adaptive_trader.platform.jobs.repository import _utc

        event = self._repository.claim_outbox(owner=self._owner, now=_utc(self._clock()))
        if event is None:
            return False
        lease = _OutboxLeaseGuard(
            self._repository,
            event=event,
            owner=self._owner,
            clock=self._clock,
            interval_seconds=self._heartbeat_interval_seconds,
        )
        failed = False
        with lease.keep_alive():
            try:
                self._publisher.publish(
                    outbox_event_id=event.outbox_event_id,
                    event_type=event.event_type,
                    aggregate_id=event.aggregate_id,
                    payload=event.payload,
                )
            except Exception:
                failed = True
            completion = lease.checkpoint()
        if failed:
            self._repository.fail_outbox(
                event_id=event.outbox_event_id,
                owner=self._owner,
                attempt_number=event.attempt_count,
                now=completion.updated_at,
                error=SafeJobError(
                    "delivery_failure",
                    "Outbox publisher failed without safe details",
                ),
            )
            return False
        self._repository.publish_outbox(
            event_id=event.outbox_event_id,
            owner=self._owner,
            attempt_number=event.attempt_count,
            now=completion.updated_at,
        )
        return True


def routing_value(payload: JobPayload, key: str) -> str:
    """Read one known routing field without granting it authorization semantics."""

    if type(payload) is not JobPayload:
        raise TypeError("job payload must use the closed contract")
    expected = {
        JobType.DATA_QUALITY_AUDIT: "experiment_hash",
        JobType.GAP_REPAIR: "gap_id",
        JobType.DATASET_FREEZE: "experiment_hash",
        JobType.OFFLINE_DEMO: "demo_id",
    }[payload.job_type]
    if key != expected:
        raise ValueError("job routing field is not allowed for this job type")
    value = payload.value[key]
    if type(value) is not str:
        raise JobExecutionError(SafeJobError("invalid_payload", "Job routing data is invalid"))
    return value
