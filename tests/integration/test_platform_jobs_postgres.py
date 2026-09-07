"""PostgreSQL durability and migration checks for bounded jobs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.migrations import (
    _alembic_config,
    database_revision,
    upgrade_database,
)
from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as COLLECTION_SCHEMA
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.jobs import JobCreateRequest, JobPayload, JobRepository, JobState
from adaptive_trader.platform.jobs.schema import aqa_jobs_contract, aqa_outbox_events_contract
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

_DATABASE_URL = os.environ.get("APA_TEST_POSTGRES_URL", "").strip()
if not _DATABASE_URL:
    pytest.skip(
        "APA_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
        allow_module_level=True,
    )
if os.environ.get("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE") != "YES":
    raise RuntimeError(
        "PostgreSQL integration tests require APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES"
    )
_TEST_DATABASE = normalize_postgres_url(_DATABASE_URL)
if _TEST_DATABASE.host not in {"127.0.0.1", "::1", "localhost"}:
    raise RuntimeError("PostgreSQL integration tests require a loopback database host")
if _TEST_DATABASE.database != "collector_test":
    raise RuntimeError("PostgreSQL integration tests require the collector_test database")

_NOW = datetime(2026, 9, 5, 19, 0, tzinfo=UTC)
_EXPERIMENT_HASH = "a" * 64


def _engine(*, application_name: str) -> Engine:
    return create_engine(
        _TEST_DATABASE,
        hide_parameters=True,
        connect_args=postgres_connect_args(application_name, migration=True),
    )


def _drop_disposable_test_schemas() -> None:
    engine = _engine(application_name="platform-job-test-reset")
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(COLLECTION_SCHEMA, cascade=True, if_exists=True))
    finally:
        engine.dispose()


@pytest.fixture
def empty_database() -> Iterator[str]:
    _drop_disposable_test_schemas()
    try:
        yield _DATABASE_URL
    finally:
        _drop_disposable_test_schemas()


def _request(index: int) -> JobCreateRequest:
    return JobCreateRequest(
        payload=JobPayload.data_quality_audit(experiment_hash=_EXPERIMENT_HASH),
        idempotency_key=f"postgres-job-{index}",
        correlation_id=f"00000000-0000-4000-8000-{index:012d}",
        requested_at=_NOW,
    )


def test_postgresql_job_lifecycle_and_outbox_are_atomic(empty_database: str) -> None:
    upgrade_database(empty_database)
    engine = _engine(application_name="platform-job-lifecycle-test")
    repository = JobRepository(
        engine,
        audit=AuditRepository(engine, writer=AuditWriter.CONTROL),
    )
    try:
        created = repository.create(_request(1))
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(aqa_jobs_contract)) == 1
            assert (
                connection.scalar(select(func.count()).select_from(aqa_outbox_events_contract)) == 1
            )

        claimed = repository.claim_next(owner="postgres-worker-1", now=_NOW)
        assert claimed is not None
        assert claimed.state is JobState.CLAIMED
        assert claimed.lease_expires_at == _NOW + timedelta(seconds=60)
        repository.mark_running(
            job_id=created.job_id,
            owner="postgres-worker-1",
            attempt_number=claimed.attempt_count,
            now=_NOW,
        )
        completed = repository.succeed(
            job_id=created.job_id,
            owner="postgres-worker-1",
            attempt_number=claimed.attempt_count,
            now=_NOW + timedelta(seconds=1),
            result_artifact_id="postgres-job-evidence",
        )
        assert completed.state is JobState.SUCCEEDED
        assert repository.get(created.job_id) == completed
    finally:
        engine.dispose()


def test_postgresql_claim_skips_a_row_locked_by_another_worker(empty_database: str) -> None:
    upgrade_database(empty_database)
    engine = _engine(application_name="platform-job-skip-locked-test")
    repository = JobRepository(
        engine,
        audit=AuditRepository(engine, writer=AuditWriter.CONTROL),
    )
    try:
        created = {repository.create(_request(index)).job_id for index in (1, 2)}
        with engine.begin() as locking_connection:
            locked_id = locking_connection.scalar(
                select(aqa_jobs_contract.c.job_id)
                .order_by(aqa_jobs_contract.c.created_at, aqa_jobs_contract.c.job_id)
                .limit(1)
                .with_for_update()
            )
            claimed = repository.claim_next(owner="postgres-worker-2", now=_NOW)
            assert claimed is not None
            assert claimed.job_id != locked_id
            assert claimed.job_id in created
    finally:
        engine.dispose()


def test_revision_nine_refuses_nonempty_provisional_job_tables(empty_database: str) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "20260905_0008")
    engine = _engine(application_name="platform-job-migration-guard-test")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO aqa.aqa_jobs (
                        job_id, job_type, idempotency_key, state, payload, result,
                        lease_owner, lease_expires_at, attempt_count, max_attempts,
                        next_attempt_at, safe_error_code, content_hash, version,
                        created_at, updated_at
                    ) VALUES (
                        'legacy-job', 'legacy', 'legacy-key', 'queued', '{}'::jsonb, NULL,
                        NULL, NULL, 0, 3, :now, NULL, :content_hash, 1, :now, :now
                    )
                    """
                ),
                {"now": _NOW, "content_hash": "f" * 64},
            )

        with pytest.raises(DBAPIError, match="requires an explicit durable-job backfill"):
            command.upgrade(config, "20260905_0009")

        current, expected = database_revision(empty_database)
        assert current == "20260905_0008"
        assert expected == "20260906_0015"
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM aqa.aqa_jobs")) == 1
    finally:
        engine.dispose()
