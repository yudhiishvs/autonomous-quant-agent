"""Guarded PostgreSQL transaction and concurrency tests for audit evidence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.migrations import upgrade_database
from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as COLLECTION_SCHEMA
from adaptive_trader.platform.domain import AuditEvent, AuditPayload, AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
    TransactionBoundaryError,
    TransactionViolation,
)

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

_OCCURRED_AT = datetime(2026, 9, 5, 19, 0, tzinfo=UTC)


def _engine(*, application_name: str) -> Engine:
    return create_engine(
        _TEST_DATABASE,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=postgres_connect_args(application_name, migration=True),
    )


def _drop_disposable_test_schemas() -> None:
    engine = _engine(application_name="platform-audit-test-reset")
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(COLLECTION_SCHEMA, cascade=True, if_exists=True))
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def postgres_repository() -> Iterator[AuditRepository]:
    """Yield only the positively guarded disposable database after migration to head."""

    _drop_disposable_test_schemas()
    upgrade_database(_DATABASE_URL)
    engine = _engine(application_name="platform-audit-tests")
    try:
        yield AuditRepository(engine, writer=AuditWriter.CONTROL)
    finally:
        engine.dispose()
        _drop_disposable_test_schemas()


def _append(
    repository: AuditRepository,
    *,
    stream_id: str,
    ordinal: int,
) -> AuditEvent:
    return repository.append(
        stream_id=stream_id,
        event_type="job.progressed",
        occurred_at=_OCCURRED_AT + timedelta(microseconds=ordinal),
        payload=AuditPayload.from_mapping(
            {
                "idempotency_key": f"postgres_{sha256_hex(('audit-postgres', stream_id, ordinal))}",
                "ordinal": ordinal,
            }
        ),
    )


def test_postgres_concurrent_writers_allocate_one_contiguous_stream(
    postgres_repository: AuditRepository,
) -> None:
    stream_id = "aqa_control:job:postgres-concurrent"

    def append(ordinal: int) -> AuditEvent:
        return _append(postgres_repository, stream_id=stream_id, ordinal=ordinal)

    with ThreadPoolExecutor(max_workers=12) as pool:
        events = tuple(pool.map(append, range(32)))

    assert sorted(event.sequence for event in events) == list(range(1, 33))
    assert len({event.event_hash for event in events}) == 32
    report = postgres_repository.verify(stream_id=stream_id)
    assert report.event_count == 32
    assert report.stream_heads[0].sequence == 32


def test_postgres_verification_reads_the_safe_full_event_view(
    postgres_repository: AuditRepository,
) -> None:
    stream_id = "aqa_control:job:postgres-safe-view"
    event = _append(postgres_repository, stream_id=stream_id, ordinal=0)
    statements: list[str] = []

    def capture_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement)

    sqlalchemy_event.listen(
        postgres_repository.engine,
        "before_cursor_execute",
        capture_statement,
    )
    try:
        report = postgres_repository.verify(
            stream_id=stream_id,
            expected_sequence=1,
            expected_hash=event.event_hash,
        )
    finally:
        sqlalchemy_event.remove(
            postgres_repository.engine,
            "before_cursor_execute",
            capture_statement,
        )

    assert report.event_count == 1
    assert any("aqa_audit_events_v" in statement for statement in statements)


def test_postgres_repository_owned_transaction_rolls_back_all_appends(
    postgres_repository: AuditRepository,
) -> None:
    stream_id = "aqa_control:job:postgres-owned-rollback"

    with (
        pytest.raises(RuntimeError, match="abort owned transaction"),
        postgres_repository.transaction() as connection,
    ):
        first = postgres_repository.append(
            stream_id=stream_id,
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"postgres_{sha256_hex('owned-start')}",
                    "state": "running",
                }
            ),
            connection=connection,
        )
        second = postgres_repository.append(
            stream_id=stream_id,
            event_type="job.completed",
            occurred_at=_OCCURRED_AT + timedelta(seconds=1),
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"postgres_{sha256_hex('owned-complete')}",
                    "state": "completed",
                }
            ),
            connection=connection,
        )
        assert (first.sequence, second.sequence) == (1, 2)
        raise RuntimeError("abort owned transaction")

    assert postgres_repository.list_events(stream_id=stream_id) == ()


def test_postgres_idempotent_retry_returns_the_committed_event(
    postgres_repository: AuditRepository,
) -> None:
    stream_id = "aqa_control:job:postgres-idempotent-retry"
    payload = AuditPayload.from_mapping(
        {
            "idempotency_key": f"postgres_{sha256_hex('ambiguous-commit')}",
            "state": "completed",
        }
    )
    first = postgres_repository.append(
        stream_id=stream_id,
        event_type="job.completed",
        occurred_at=_OCCURRED_AT,
        payload=payload,
    )
    retry = postgres_repository.append(
        stream_id=stream_id,
        event_type="job.completed",
        occurred_at=_OCCURRED_AT,
        payload=payload,
    )

    assert retry == first
    assert (
        postgres_repository.verify(
            stream_id=stream_id,
            expected_sequence=1,
            expected_hash=first.event_hash,
        ).event_count
        == 1
    )


def test_postgres_external_transaction_controls_commit_and_rollback(
    postgres_repository: AuditRepository,
) -> None:
    committed_stream = "aqa_control:job:postgres-external-commit"
    rolled_back_stream = "aqa_control:job:postgres-external-rollback"

    with postgres_repository.engine.begin() as connection:
        committed = postgres_repository.append(
            stream_id=committed_stream,
            event_type="job.completed",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"postgres_{sha256_hex('external-commit')}",
                    "state": "completed",
                }
            ),
            connection=connection,
        )
        assert (
            postgres_repository.verify(
                stream_id=committed_stream,
                connection=connection,
            ).event_count
            == 1
        )

    assert postgres_repository.list_events(stream_id=committed_stream) == (committed,)

    with (
        pytest.raises(RuntimeError, match="abort external transaction"),
        postgres_repository.engine.begin() as connection,
    ):
        postgres_repository.append(
            stream_id=rolled_back_stream,
            event_type="job.failed",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"postgres_{sha256_hex('external-rollback')}",
                    "state": "failed",
                }
            ),
            connection=connection,
        )
        raise RuntimeError("abort external transaction")

    assert postgres_repository.list_events(stream_id=rolled_back_stream) == ()


def test_postgres_external_connection_must_have_an_active_owned_transaction(
    postgres_repository: AuditRepository,
) -> None:
    with (
        postgres_repository.engine.connect() as connection,
        pytest.raises(AuditPersistenceError, match="active transaction"),
    ):
        postgres_repository.append(
            stream_id="aqa_control:job:postgres-no-transaction",
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"postgres_{sha256_hex('no-transaction')}",
                    "state": "running",
                }
            ),
            connection=connection,
        )


def test_postgres_global_verification_uses_python_code_point_stream_order(
    postgres_repository: AuditRepository,
) -> None:
    later = _append(
        postgres_repository,
        stream_id="aqa_control:job:punctuation_a",
        ordinal=41,
    )
    earlier = _append(
        postgres_repository,
        stream_id="aqa_control:job:punctuation-a",
        ordinal=42,
    )

    selected = tuple(
        event
        for event in postgres_repository.list_events()
        if event.stream_id in {earlier.stream_id, later.stream_id}
    )
    assert selected == (earlier, later)
    heads = {head.stream_id: head for head in postgres_repository.verify().stream_heads}
    assert tuple(stream_id for stream_id in heads if "punctuation" in stream_id) == (
        earlier.stream_id,
        later.stream_id,
    )


def test_postgres_advisory_locks_are_reentrant_ordered_and_fail_fast(
    postgres_repository: AuditRepository,
) -> None:
    coordinator = SerializedTransactionCoordinator(postgres_repository.engine)
    watermark = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
        "alpaca:sip:AAPL:1Min",
    )
    identity = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
        "bar_" + ("a" * 64),
    )
    audit = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.AUDIT,
        "aqa_control:job:lock-order",
    )
    late_lower_namespace = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
        "alpaca:sip:AMD:1Min",
    )
    statements: list[str] = []

    def capture_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if "pg_advisory_xact_lock" in statement:
            statements.append(statement)

    sqlalchemy_event.listen(
        postgres_repository.engine,
        "before_cursor_execute",
        capture_statement,
    )
    try:
        with postgres_repository.engine.begin() as connection:
            coordinator.acquire_postgres_advisory_lock(connection, watermark)
            coordinator.acquire_postgres_advisory_lock(connection, watermark)
            coordinator.acquire_postgres_advisory_lock(connection, identity)
            coordinator.acquire_postgres_advisory_lock(connection, audit)
            coordinator.acquire_postgres_advisory_lock(connection, watermark)
            with pytest.raises(TransactionBoundaryError) as captured:
                coordinator.acquire_postgres_advisory_lock(
                    connection,
                    late_lower_namespace,
                )
            assert captured.value.violation is TransactionViolation.ADVISORY_LOCK_ORDER
            assert connection.scalar(select(1)) == 1
    finally:
        sqlalchemy_event.remove(
            postgres_repository.engine,
            "before_cursor_execute",
            capture_statement,
        )

    assert len(statements) == 3


def test_postgres_audit_lock_is_compatible_with_prior_market_data_namespace(
    postgres_repository: AuditRepository,
) -> None:
    coordinator = SerializedTransactionCoordinator(postgres_repository.engine)
    watermark = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
        "alpaca:sip:NVDA:1Min",
    )

    with postgres_repository.transaction() as connection:
        coordinator.acquire_postgres_advisory_lock(connection, watermark)
        event = postgres_repository.append(
            stream_id="aqa_control:job:cross-repository-lock-order",
            event_type="job.completed",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"postgres_{sha256_hex('cross-lock-order')}",
                    "state": "completed",
                }
            ),
            connection=connection,
        )

    assert event.sequence == 1
