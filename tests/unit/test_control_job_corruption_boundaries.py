"""Corrupt persisted jobs and database failures never become control authority."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError

from adaptive_trader.platform.control.queries import (
    ControlQueryError,
    ReadResource,
    SQLAlchemyControlQueryService,
)
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.jobs import JobCreateRequest, JobPayload, JobRepository
from adaptive_trader.platform.jobs.models import JobState, JobValidationError
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, metadata

NOW = datetime(2026, 9, 5, 16, tzinfo=UTC)


@pytest.fixture
def durable_jobs():
    engine = create_engine("sqlite://").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )
    metadata.create_all(engine)
    repository = JobRepository(engine, audit=AuditRepository(engine, writer=AuditWriter.CONTROL))
    job = repository.create(
        JobCreateRequest(
            payload=JobPayload.offline_demo(demo_id="corruption-fixture"),
            idempotency_key="corruption-fixture",
            correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ab",
            requested_at=NOW,
        )
    )
    try:
        yield repository, job
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        {"state": JobState.PENDING, "attempt_count": 1},
        {"state": JobState.PENDING, "next_attempt_at": None},
        {"state": JobState.PENDING, "next_attempt_at": NOW - timedelta(seconds=1)},
        {"state": JobState.SUCCEEDED},
        {"state": JobState.CANCELED},
        {"state": JobState.CLAIMED},
        {"state": JobState.RUNNING},
        {"state": JobState.FAILED},
        {"state": JobState.DEAD},
        {"result_artifact_id": "demo/" + "a" * 64 + ".json"},
        {"started_at": NOW},
        {"completed_at": NOW},
    ],
)
def test_persisted_pending_job_cannot_be_reinterpreted_as_executed(durable_jobs, mutation):
    repository, original = durable_jobs
    with pytest.raises(JobValidationError):
        replace(original, **mutation)
    assert repository.get(original.job_id) == original


@pytest.mark.parametrize(
    "mutation",
    [
        {"started_at": None},
        {"started_at": NOW - timedelta(seconds=1)},
        {"claimed_at": NOW + timedelta(seconds=1)},
        {"lease_expires_at": NOW},
        {"lease_owner": None},
        {"attempt_count": 0},
        {"next_attempt_at": NOW},
        {"state": JobState.CLAIMED},
        {"updated_at": NOW - timedelta(seconds=1)},
    ],
)
def test_running_job_rejects_corrupt_lease_and_completion_state(durable_jobs, mutation):
    repository, original = durable_jobs
    claimed = repository.claim_next(owner="fixture-worker", now=NOW)
    assert claimed is not None
    running = repository.mark_running(
        job_id=original.job_id,
        owner="fixture-worker",
        attempt_number=claimed.attempt_count,
        now=NOW,
    )
    with pytest.raises(JobValidationError):
        replace(running, **mutation)
    assert repository.get(original.job_id) == running


@pytest.mark.parametrize("resource", [ReadResource.SYSTEM_STATUS, ReadResource.DATA_GAPS])
def test_database_failure_is_redacted_and_connection_returned(resource):
    engine = create_engine("sqlite://")
    returned = []

    @event.listens_for(engine, "checkin")
    def checkin(*args):
        returned.append(True)

    @event.listens_for(engine, "before_cursor_execute")
    def failure(*args):
        raise OperationalError("private_sql", {}, RuntimeError("private-password"))

    try:
        queries = SQLAlchemyControlQueryService(engine)
        assert not queries.ready()
        with pytest.raises(ControlQueryError) as error:
            queries.page(resource, limit=1, offset=0)
        assert str(error.value) == "safe operational state is unavailable"
        assert len(returned) == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "arguments",
    [
        {"resource": "data_gaps", "limit": 1, "offset": 0},
        {"resource": ReadResource.DATA_GAPS, "limit": True, "offset": 0},
        {"resource": ReadResource.DATA_GAPS, "limit": 0, "offset": 0},
        {"resource": ReadResource.DATA_GAPS, "limit": 101, "offset": 0},
        {"resource": ReadResource.DATA_GAPS, "limit": 1, "offset": True},
        {"resource": ReadResource.DATA_GAPS, "limit": 1, "offset": -1},
    ],
)
def test_invalid_page_authority_is_rejected_before_database_checkout(arguments):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "checkout")
    def forbidden(*args):
        pytest.fail("invalid page input opened a database connection")

    try:
        with pytest.raises((ValueError, TypeError)):
            SQLAlchemyControlQueryService(engine).page(**arguments)
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["corrupt-gap", "endless-gaps", "forged-demo"])
def test_job_evidence_requires_bounded_authoritative_inputs(tmp_path, mode):
    from pathlib import Path
    from types import SimpleNamespace

    from adaptive_trader.platform.config import load_experiment
    from adaptive_trader.platform.control.models import SafePageResponse
    from adaptive_trader.platform.jobs import JobExecutionError, JobLeaseGuard
    from adaptive_trader.platform.jobs.artifacts import ImmutableJobArtifactStore
    from adaptive_trader.platform.jobs.handlers import PlatformJobHandlerSet

    experiment = load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=Path(__file__).resolve().parents[2] / "configs",
    )
    engine = create_engine("sqlite://").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )
    metadata.create_all(engine)
    repository = JobRepository(engine, audit=AuditRepository(engine, writer=AuditWriter.CONTROL))
    calls = []

    def page(resource, *, limit, offset):
        calls.append(offset)
        gap = {
            "experiment_hash": experiment.content_hash,
            "status": "open",
            "gap_id": None if mode == "corrupt-gap" else "gap-fixture",
        }
        items = (gap,) if mode == "corrupt-gap" else tuple(dict(gap) for _ in range(limit))
        return SafePageResponse(items=items, limit=limit, offset=offset, count=len(items))

    artifacts = tmp_path / "artifacts"
    handlers = PlatformJobHandlerSet(
        experiment=experiment,
        audit=AuditRepository(engine),
        queries=SimpleNamespace(page=page),
        artifacts=ImmutableJobArtifactStore(artifacts),
        clock=lambda: NOW,
        demo_evidence_provider=lambda: {
            "schema": "offline-demo-evidence-v1",
            "evidence_label": "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE",
            "evidence_manifest_hash": "a" * 64,
        },
    )
    payload = (
        JobPayload.offline_demo(demo_id="forged")
        if mode == "forged-demo"
        else JobPayload.data_quality_audit(experiment_hash=experiment.content_hash)
    )
    created = repository.create(
        JobCreateRequest(
            payload=payload,
            idempotency_key="quality-bound",
            correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ab",
            requested_at=NOW,
        )
    )
    claimed = repository.claim_next(owner="fixture-worker", now=NOW)
    assert claimed is not None
    running = repository.mark_running(
        job_id=created.job_id, owner="fixture-worker", attempt_number=claimed.attempt_count, now=NOW
    )
    lease = JobLeaseGuard(repository, job=running, owner="fixture-worker", clock=lambda: NOW)
    try:
        handler = handlers.offline_demo if mode == "forged-demo" else handlers.data_quality_audit
        with pytest.raises(JobExecutionError):
            handler(job=running, lease=lease)
        assert not list(artifacts.rglob("*.json"))
        if mode == "endless-gaps":
            assert calls == list(range(0, 10_000, 100))
        assert repository.get(running.job_id).state is JobState.RUNNING
    finally:
        engine.dispose()
