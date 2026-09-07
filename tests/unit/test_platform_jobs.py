"""Durability, concurrency, retry, and payload-safety tests for bounded jobs."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event, func, select, update

from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.jobs import (
    BoundedJobHandlers,
    DurableJobWorker,
    DurableOutboxWorker,
    JobConflictError,
    JobCreateRequest,
    JobExecutionError,
    JobLeaseGuard,
    JobPayload,
    JobRepository,
    JobState,
    JobType,
    JobValidationError,
    SafeJobError,
)
from adaptive_trader.platform.jobs.models import JOB_RETRY_DELAYS, retry_delay
from adaptive_trader.platform.jobs.repository import (
    _outbox_content_hash,
    initialized_sqlite_job_schema,
)
from adaptive_trader.platform.jobs.schema import (
    aqa_job_attempts_contract,
    aqa_jobs_contract,
    aqa_outbox_events_contract,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, aqa_audit_events

_NOW = datetime(2026, 9, 5, 15, 0, tzinfo=UTC)
_EXPERIMENT_HASH = "a" * 64
_CORRELATION_ID = "0198fa2d-7b8c-7123-8abc-0123456789ab"


def _sqlite_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure(connection: object, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    with initialized_sqlite_job_schema(engine):
        pass
    return engine


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = _sqlite_engine(tmp_path / "jobs.sqlite3")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def repository(sqlite_engine: Engine) -> JobRepository:
    return JobRepository(
        sqlite_engine,
        audit=AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL),
    )


def _request(
    *,
    key: str = "request-1",
    correlation_id: str = _CORRELATION_ID,
    payload: JobPayload | None = None,
    requested_at: datetime = _NOW,
) -> JobCreateRequest:
    return JobCreateRequest(
        payload=payload or JobPayload.data_quality_audit(experiment_hash=_EXPERIMENT_HASH),
        idempotency_key=key,
        correlation_id=correlation_id,
        requested_at=requested_at,
    )


def _counts(engine: Engine) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        return (
            int(connection.scalar(select(func.count()).select_from(aqa_jobs_contract)) or 0),
            int(
                connection.scalar(select(func.count()).select_from(aqa_job_attempts_contract)) or 0
            ),
            int(
                connection.scalar(select(func.count()).select_from(aqa_outbox_events_contract)) or 0
            ),
            int(connection.scalar(select(func.count()).select_from(aqa_audit_events)) or 0),
        )


def test_job_types_and_payloads_are_closed_and_schema_versioned() -> None:
    payloads = (
        JobPayload.data_quality_audit(experiment_hash=_EXPERIMENT_HASH),
        JobPayload.gap_repair(gap_id="gap-1"),
        JobPayload.dataset_freeze(experiment_hash=_EXPERIMENT_HASH),
        JobPayload.offline_demo(demo_id="fixture-1"),
    )

    assert tuple(payload.job_type for payload in payloads) == tuple(JobType)
    assert all(payload.value["contract_version"] == 1 for payload in payloads)
    assert tuple(timedelta(seconds=value) for value in (5, 10, 20)) == JOB_RETRY_DELAYS
    assert tuple(retry_delay(attempt) for attempt in (1, 2, 3)) == JOB_RETRY_DELAYS

    with pytest.raises(JobValidationError, match="content"):
        replace(payloads[0], payload_json='{"command":"whoami","contract_version":1}')
    with pytest.raises(JobValidationError, match="routing"):
        JobPayload.gap_repair(gap_id="https://example.invalid")
    with pytest.raises(JobValidationError, match="routing"):
        JobPayload.offline_demo(demo_id="../escape")


@pytest.mark.parametrize(
    "error",
    [
        ("bad-code", "Safe message"),
        ("worker_failure", "Authorization: Bearer example"),
        ("worker_failure", "database_url is configured"),
        ("worker_failure", "https://example.invalid"),
    ],
)
def test_safe_job_errors_reject_unbounded_or_secret_like_details(error: tuple[str, str]) -> None:
    with pytest.raises(JobValidationError, match="safe job error"):
        SafeJobError(*error)


def test_create_is_atomic_idempotent_and_writes_audit_and_outbox(
    repository: JobRepository,
    sqlite_engine: Engine,
) -> None:
    created = repository.create(_request())
    repeated = repository.create(_request(requested_at=_NOW + timedelta(seconds=10)))

    assert repeated == created
    assert created.state is JobState.PENDING
    assert created.attempt_count == 0
    assert created.next_attempt_at == _NOW
    assert _counts(sqlite_engine) == (1, 0, 1, 1)

    with pytest.raises(JobConflictError, match="conflicts"):
        repository.create(_request(payload=JobPayload.data_quality_audit(experiment_hash="b" * 64)))
    assert _counts(sqlite_engine) == (1, 0, 1, 1)


def test_job_lifecycle_uses_exact_states_leases_attempt_events_and_retry_delays(
    repository: JobRepository,
    sqlite_engine: Engine,
) -> None:
    job = repository.create(_request())
    claimed = repository.claim_next(owner="worker-1", now=_NOW)
    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.state is JobState.CLAIMED
    assert claimed.lease_expires_at == _NOW + timedelta(seconds=60)
    assert claimed.attempt_count == 1

    running = repository.mark_running(
        job_id=job.job_id,
        owner="worker-1",
        attempt_number=claimed.attempt_count,
        now=_NOW,
    )
    assert running.state is JobState.RUNNING

    failed = repository.fail(
        job_id=job.job_id,
        owner="worker-1",
        attempt_number=claimed.attempt_count,
        now=_NOW + timedelta(seconds=1),
        error=SafeJobError("fixture_failure", "Fixture handler failed safely"),
    )
    assert failed.state is JobState.FAILED
    assert failed.next_attempt_at == _NOW + timedelta(seconds=6)
    assert repository.claim_next(owner="worker-2", now=_NOW + timedelta(seconds=5)) is None

    retry = repository.claim_next(owner="worker-2", now=_NOW + timedelta(seconds=6))
    assert retry is not None
    assert retry.attempt_count == 2
    running_retry = repository.mark_running(
        job_id=job.job_id,
        owner="worker-2",
        attempt_number=retry.attempt_count,
        now=_NOW + timedelta(seconds=6),
    )
    assert running_retry.state is JobState.RUNNING
    succeeded = repository.succeed(
        job_id=job.job_id,
        owner="worker-2",
        attempt_number=retry.attempt_count,
        now=_NOW + timedelta(seconds=7),
        result_artifact_id="demo.evidence-1",
    )

    assert succeeded.state is JobState.SUCCEEDED
    assert succeeded.completed_at == _NOW + timedelta(seconds=7)
    assert succeeded.result_artifact_id == "demo.evidence-1"
    assert succeeded.is_terminal
    assert _counts(sqlite_engine) == (1, 6, 7, 7)


def test_third_failed_attempt_becomes_dead_and_cannot_be_reclaimed(
    repository: JobRepository,
) -> None:
    job = repository.create(_request())
    now = _NOW
    for attempt in range(1, 4):
        claimed = repository.claim_next(owner=f"worker-{attempt}", now=now)
        assert claimed is not None
        repository.mark_running(
            job_id=job.job_id,
            owner=f"worker-{attempt}",
            attempt_number=claimed.attempt_count,
            now=now,
        )
        failed = repository.fail(
            job_id=job.job_id,
            owner=f"worker-{attempt}",
            attempt_number=claimed.attempt_count,
            now=now + timedelta(seconds=1),
            error=SafeJobError("fixture_failure", "Fixture handler failed safely"),
        )
        if attempt < 3:
            assert failed.state is JobState.FAILED
            assert failed.next_attempt_at is not None
            now = failed.next_attempt_at
        else:
            assert failed.state is JobState.DEAD
            assert failed.completed_at == now + timedelta(seconds=1)

    assert repository.claim_next(owner="worker-4", now=now + timedelta(days=1)) is None


def test_expired_lease_is_durable_and_recovered_after_repository_restart(
    repository: JobRepository,
    sqlite_engine: Engine,
) -> None:
    job = repository.create(_request())
    repository.claim_next(owner="failed-worker", now=_NOW)

    restarted = JobRepository(
        sqlite_engine,
        audit=AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL),
    )
    assert (
        restarted.claim_next(
            owner="replacement-worker",
            now=_NOW + timedelta(seconds=61),
        )
        is None
    )
    recovered = restarted.get(job.job_id)
    assert recovered is not None
    assert recovered.state is JobState.FAILED
    assert recovered.safe_last_error_code == "lease_expired"
    assert recovered.next_attempt_at == _NOW + timedelta(seconds=66)

    claimed = restarted.claim_next(
        owner="replacement-worker",
        now=_NOW + timedelta(seconds=66),
    )
    assert claimed is not None
    assert claimed.attempt_count == 2


def test_stale_or_wrong_worker_cannot_transition_job(repository: JobRepository) -> None:
    job = repository.create(_request())
    repository.claim_next(owner="worker-1", now=_NOW)

    with pytest.raises(JobConflictError, match="lease"):
        repository.mark_running(
            job_id=job.job_id,
            owner="worker-2",
            attempt_number=1,
            now=_NOW,
        )
    with pytest.raises(JobConflictError, match="lease"):
        repository.mark_running(
            job_id=job.job_id,
            owner="worker-1",
            attempt_number=1,
            now=_NOW + timedelta(seconds=60),
        )


def test_heartbeat_extends_exact_attempt_and_same_owner_cannot_reuse_stale_fence(
    repository: JobRepository,
) -> None:
    created = repository.create(_request())
    first = repository.claim_next(owner="stable-owner", now=_NOW)
    assert first is not None
    repository.mark_running(
        job_id=created.job_id,
        owner="stable-owner",
        attempt_number=first.attempt_count,
        now=_NOW,
    )
    heartbeat = repository.heartbeat(
        job_id=created.job_id,
        owner="stable-owner",
        attempt_number=first.attempt_count,
        now=_NOW + timedelta(seconds=50),
    )
    assert heartbeat.lease_expires_at == _NOW + timedelta(seconds=110)
    assert repository.claim_next(owner="replacement", now=_NOW + timedelta(seconds=61)) is None

    repository.fail(
        job_id=created.job_id,
        owner="stable-owner",
        attempt_number=first.attempt_count,
        now=_NOW + timedelta(seconds=70),
        error=SafeJobError("fixture_failure", "Fixture handler failed safely"),
    )
    second = repository.claim_next(owner="stable-owner", now=_NOW + timedelta(seconds=75))
    assert second is not None and second.attempt_count == 2
    repository.mark_running(
        job_id=created.job_id,
        owner="stable-owner",
        attempt_number=second.attempt_count,
        now=_NOW + timedelta(seconds=75),
    )
    with pytest.raises(JobConflictError, match="lease"):
        repository.succeed(
            job_id=created.job_id,
            owner="stable-owner",
            attempt_number=first.attempt_count,
            now=_NOW + timedelta(seconds=76),
        )


def test_job_transitions_reject_time_regression(repository: JobRepository) -> None:
    created = repository.create(_request())
    with pytest.raises(JobConflictError, match="timestamp"):
        repository.cancel(job_id=created.job_id, now=_NOW - timedelta(microseconds=1))

    claimed = repository.claim_next(owner="worker-1", now=_NOW)
    assert claimed is not None
    running = repository.mark_running(
        job_id=created.job_id,
        owner="worker-1",
        attempt_number=claimed.attempt_count,
        now=_NOW + timedelta(seconds=1),
    )
    with pytest.raises(JobConflictError, match="timestamp"):
        repository.heartbeat(
            job_id=running.job_id,
            owner="worker-1",
            attempt_number=running.attempt_count,
            now=_NOW,
        )


def test_sqlite_claim_is_deterministic_under_concurrency(sqlite_engine: Engine) -> None:
    first_repository = JobRepository(
        sqlite_engine,
        audit=AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL),
    )
    second_repository = JobRepository(
        sqlite_engine,
        audit=AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL),
    )
    job = first_repository.create(_request())

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(
            pool.map(
                lambda entry: entry[0].claim_next(owner=entry[1], now=_NOW),
                ((first_repository, "worker-1"), (second_repository, "worker-2")),
            )
        )

    claimed = tuple(result for result in claims if result is not None)
    assert len(claimed) == 1
    assert claimed[0].job_id == job.job_id


def test_worker_dispatch_is_fixed_and_unknown_failures_are_not_persisted(
    repository: JobRepository,
) -> None:
    job = repository.create(_request())
    seen: list[JobType] = []

    def explode(*, job: object, lease: object) -> None:
        del job, lease
        raise RuntimeError("Bearer not-a-real-token")

    def unused(*, job: object, lease: object) -> None:
        del job, lease

    handlers = BoundedJobHandlers(explode, unused, unused, unused)
    moments = iter((_NOW, _NOW, _NOW + timedelta(seconds=1)))
    worker = DurableJobWorker(
        repository,
        owner="worker-1",
        handlers=handlers,
        clock=lambda: next(moments),
    )
    result = worker.run_one()

    assert result is not None
    assert result.job_id == job.job_id
    assert result.state is JobState.FAILED
    assert result.safe_last_error_code == "worker_failure"
    assert result.safe_last_error_message == "Job handler failed without safe details"
    assert "Bearer" not in repr(result)
    assert seen == []


def test_worker_persists_only_handler_returned_artifact_id(repository: JobRepository) -> None:
    repository.create(_request(payload=JobPayload.gap_repair(gap_id="gap-1")))

    def gap_handler(*, job: object, lease: object) -> str:
        del job, lease
        return "gap-repair.evidence"

    def unused(*, job: object, lease: object) -> None:
        del job, lease

    moments = iter((_NOW, _NOW, _NOW + timedelta(seconds=1)))
    result = DurableJobWorker(
        repository,
        owner="worker-1",
        handlers=BoundedJobHandlers(unused, gap_handler, unused, unused),
        clock=lambda: next(moments),
    ).run_one()

    assert result is not None
    assert result.state is JobState.SUCCEEDED
    assert result.result_artifact_id == "gap-repair.evidence"


def test_worker_heartbeats_long_handler_and_reports_persisted_transitions(
    repository: JobRepository,
) -> None:
    repository.create(_request())
    heartbeat_seen = threading.Event()
    clock_lock = threading.Lock()
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        with clock_lock:
            instant = _NOW + timedelta(seconds=clock_calls)
            clock_calls += 1
            if clock_calls >= 3:
                heartbeat_seen.set()
            return instant

    def handler(*, job: object, lease: JobLeaseGuard) -> None:
        del job, lease
        assert heartbeat_seen.wait(timeout=2)

    transitions: list[JobState] = []
    result = DurableJobWorker(
        repository,
        owner="worker-1",
        handlers=BoundedJobHandlers(handler, handler, handler, handler),
        clock=clock,
        heartbeat_interval_seconds=0.01,
        transition_observer=transitions.append,
    ).run_one()

    assert result is not None and result.state is JobState.SUCCEEDED
    assert transitions == [JobState.CLAIMED, JobState.RUNNING, JobState.SUCCEEDED]


def test_raising_transition_observer_cannot_corrupt_completion(
    repository: JobRepository,
) -> None:
    repository.create(_request())

    def handler(*, job: object, lease: object) -> None:
        del job, lease

    def observer(state: JobState) -> None:
        raise RuntimeError(state.value)

    moments = iter((_NOW, _NOW, _NOW + timedelta(seconds=1)))
    result = DurableJobWorker(
        repository,
        owner="worker-1",
        handlers=BoundedJobHandlers(handler, handler, handler, handler),
        clock=lambda: next(moments),
        transition_observer=observer,
    ).run_one()

    assert result is not None and result.state is JobState.SUCCEEDED
    assert repository.get(result.job_id) == result


def test_handler_checkpoint_blocks_side_effect_after_attempt_is_reclaimed(
    repository: JobRepository,
) -> None:
    created = repository.create(_request())
    instant = _NOW
    side_effects: list[str] = []

    def clock() -> datetime:
        return instant

    def handler(*, job: object, lease: JobLeaseGuard) -> None:
        nonlocal instant
        del job
        instant = _NOW + timedelta(seconds=61)
        assert repository.claim_next(owner="replacement", now=instant) is None
        instant = _NOW + timedelta(seconds=66)
        replacement = repository.claim_next(owner="replacement", now=instant)
        assert replacement is not None and replacement.attempt_count == 2
        lease.checkpoint()
        side_effects.append("unsafe")

    with pytest.raises(JobConflictError, match="lease"):
        DurableJobWorker(
            repository,
            owner="worker-1",
            handlers=BoundedJobHandlers(handler, handler, handler, handler),
            clock=clock,
        ).run_one()

    assert side_effects == []
    current = repository.get(created.job_id)
    assert current is not None
    assert current.state is JobState.CLAIMED
    assert current.attempt_count == 2


def test_outbox_claim_publish_and_failure_are_lease_guarded(repository: JobRepository) -> None:
    repository.create(_request())
    claimed = repository.claim_outbox(owner="publisher-1", now=_NOW)
    assert claimed is not None
    assert claimed.attempt_count == 1

    with pytest.raises(JobConflictError, match="lease"):
        repository.publish_outbox(
            event_id=claimed.outbox_event_id,
            owner="publisher-2",
            attempt_number=claimed.attempt_count,
            now=_NOW,
        )
    repository.fail_outbox(
        event_id=claimed.outbox_event_id,
        owner="publisher-1",
        attempt_number=claimed.attempt_count,
        now=_NOW + timedelta(seconds=1),
        error=SafeJobError("delivery_failure", "Publisher failed without safe details"),
    )
    assert (
        repository.claim_outbox(
            owner="publisher-2",
            now=_NOW + timedelta(seconds=5),
        )
        is None
    )
    retried = repository.claim_outbox(
        owner="publisher-2",
        now=_NOW + timedelta(seconds=6),
    )
    assert retried is not None
    repository.publish_outbox(
        event_id=retried.outbox_event_id,
        owner="publisher-2",
        attempt_number=retried.attempt_count,
        now=_NOW + timedelta(seconds=7),
    )


def test_outbox_worker_uses_only_injected_publisher(repository: JobRepository) -> None:
    repository.create(_request())

    class Publisher:
        def __init__(self) -> None:
            self.events: list[tuple[str, str]] = []

        def publish(
            self,
            *,
            outbox_event_id: str,
            event_type: str,
            aggregate_id: str,
            payload: object,
        ) -> None:
            del aggregate_id, payload
            self.events.append((outbox_event_id, event_type))

    publisher = Publisher()
    moments = iter((_NOW, _NOW + timedelta(seconds=1)))
    worker = DurableOutboxWorker(
        repository,
        owner="publisher-1",
        publisher=publisher,
        clock=lambda: next(moments),
    )

    assert worker.run_one() is True
    assert len(publisher.events) == 1
    assert publisher.events[0][0].startswith("outbox_")
    assert publisher.events[0][1] == "job.created"


def test_outbox_retry_preserves_durable_event_id(repository: JobRepository) -> None:
    repository.create(_request())
    delivered_ids: list[str] = []

    class FailingPublisher:
        def publish(
            self,
            *,
            outbox_event_id: str,
            event_type: str,
            aggregate_id: str,
            payload: object,
        ) -> None:
            del event_type, aggregate_id, payload
            delivered_ids.append(outbox_event_id)
            raise RuntimeError("transport unavailable")

    first_times = iter((_NOW, _NOW + timedelta(seconds=1)))
    assert (
        DurableOutboxWorker(
            repository,
            owner="publisher-1",
            publisher=FailingPublisher(),
            clock=lambda: next(first_times),
        ).run_one()
        is False
    )

    class SuccessfulPublisher(FailingPublisher):
        def publish(
            self,
            *,
            outbox_event_id: str,
            event_type: str,
            aggregate_id: str,
            payload: object,
        ) -> None:
            del event_type, aggregate_id, payload
            delivered_ids.append(outbox_event_id)

    retry_times = iter((_NOW + timedelta(seconds=6), _NOW + timedelta(seconds=7)))
    assert DurableOutboxWorker(
        repository,
        owner="publisher-2",
        publisher=SuccessfulPublisher(),
        clock=lambda: next(retry_times),
    ).run_one()
    assert len(delivered_ids) == 2
    assert delivered_ids[0] == delivered_ids[1]


def test_outbox_validation_binds_aggregate_to_payload_job(
    repository: JobRepository,
    sqlite_engine: Engine,
) -> None:
    first = repository.create(_request())
    second = repository.create(
        _request(key="request-2", correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ac")
    )
    claimed = repository.claim_outbox(owner="publisher-1", now=_NOW)
    assert claimed is not None
    replacement_id = second.job_id if claimed.aggregate_id == first.job_id else first.job_id
    with sqlite_engine.begin() as connection:
        row = (
            connection.execute(
                select(aqa_outbox_events_contract).where(
                    aqa_outbox_events_contract.c.outbox_event_id == claimed.outbox_event_id
                )
            )
            .mappings()
            .one()
        )
        values = {
            str(column.name): row[column.name] for column in aqa_outbox_events_contract.columns
        }
        values["aggregate_id"] = replacement_id
        values["content_hash"] = _outbox_content_hash(values)
        connection.execute(
            update(aqa_outbox_events_contract)
            .where(aqa_outbox_events_contract.c.outbox_event_id == claimed.outbox_event_id)
            .values(aggregate_id=replacement_id, content_hash=values["content_hash"])
        )

    with pytest.raises(JobValidationError, match="malformed"):
        repository.heartbeat_outbox(
            event_id=claimed.outbox_event_id,
            owner="publisher-1",
            attempt_number=claimed.attempt_count,
            now=_NOW + timedelta(seconds=1),
        )


def test_cancel_is_internal_only_and_rejects_running_work(repository: JobRepository) -> None:
    pending = repository.create(_request())
    canceled = repository.cancel(job_id=pending.job_id, now=_NOW)
    assert canceled.state is JobState.CANCELED

    running = repository.create(
        _request(key="request-2", correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ac")
    )
    repository.claim_next(owner="worker-1", now=_NOW)
    repository.mark_running(
        job_id=running.job_id,
        owner="worker-1",
        attempt_number=1,
        now=_NOW,
    )
    with pytest.raises(JobConflictError, match="cannot be canceled"):
        repository.cancel(job_id=running.job_id, now=_NOW)


def test_handler_can_raise_only_pre_sanitized_failure_metadata(repository: JobRepository) -> None:
    repository.create(_request())

    def fail_safely(*, job: object, lease: object) -> None:
        del job, lease
        raise JobExecutionError(SafeJobError("quality_failed", "Data quality audit failed"))

    def unused(*, job: object, lease: object) -> None:
        del job, lease

    moments = iter((_NOW, _NOW, _NOW + timedelta(seconds=1)))
    result = DurableJobWorker(
        repository,
        owner="worker-1",
        handlers=BoundedJobHandlers(fail_safely, unused, unused, unused),
        clock=lambda: next(moments),
    ).run_one()

    assert result is not None
    assert result.safe_last_error_code == "quality_failed"
    assert result.safe_last_error_message == "Data quality audit failed"
