"""Guarded PostgreSQL proofs for atomic platform market-data persistence."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, current_thread
from threading import Event as ThreadEvent

import pytest
from sqlalchemy import Engine, create_engine, delete, event, func, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.migrations import upgrade_database
from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as COLLECTION_SCHEMA
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteStatus,
    EligibleWatermark,
    MarketDataRepository,
)
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_experiments,
    aqa_symbol_watermarks,
)
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
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

_BASE = datetime(2026, 9, 5, 18, 30, tzinfo=UTC)
_EXPERIMENT_HASH = "d" * 64


def _identity(*, minute: int = 0, symbol: str = "MDPG") -> BarIdentity:
    start_at = _BASE + timedelta(minutes=minute)
    return BarIdentity(
        provider="fixture",
        feed="iex",
        adjustment="raw",
        symbol=symbol,
        timeframe="1Min",
        start_at=start_at,
        end_at=start_at + timedelta(minutes=1),
    )


def _bar(
    *,
    close: Decimal,
    receipt_offset: int,
    source_event_id: str,
    identity: BarIdentity | None = None,
    is_correction: bool = False,
    correction_of_source_event_id: str | None = None,
) -> BarWrite:
    selected_identity = _identity() if identity is None else identity
    return BarWrite(
        identity=selected_identity,
        received_at=selected_identity.end_at + timedelta(seconds=receipt_offset),
        provider_timestamp=selected_identity.end_at,
        open=Decimal("100"),
        high=max(Decimal("101"), close),
        low=min(Decimal("99"), close),
        close=close,
        volume=Decimal("1000"),
        trade_count=25,
        vwap=Decimal("100.25"),
        quality_flags=("complete",),
        source="fixture",
        source_event_id=source_event_id,
        source_payload_hash=sha256_hex(("postgres-fixture", source_event_id)),
        is_correction=is_correction,
        correction_of_source_event_id=correction_of_source_event_id,
    )


def _watermark(bar: BarWrite, *, quality: str) -> EligibleWatermark:
    return EligibleWatermark(
        experiment_hash=_EXPERIMENT_HASH,
        quality_hash=sha256_hex(("postgres-quality", quality)),
        updated_at=bar.received_at + timedelta(seconds=1),
    )


def _cleanup(engine: Engine) -> None:
    identity_ids = (
        _identity().bar_identity_id,
        _identity(symbol="WMPG").bar_identity_id,
        _identity(minute=1, symbol="WMPG").bar_identity_id,
        _identity(symbol="QMPG").bar_identity_id,
        _identity(symbol="RDPG").bar_identity_id,
        _identity(symbol="LQPG").bar_identity_id,
        _identity(minute=1, symbol="LQPG").bar_identity_id,
    )
    with engine.begin() as connection:
        event_ids = tuple(
            connection.scalars(
                select(aqa_bar_events.c.bar_event_id).where(
                    aqa_bar_events.c.bar_identity_id.in_(identity_ids)
                )
            )
        )
        connection.execute(
            delete(aqa_symbol_watermarks).where(
                aqa_symbol_watermarks.c.experiment_hash == _EXPERIMENT_HASH
            )
        )
        if event_ids:
            connection.execute(
                delete(aqa_symbol_watermarks).where(
                    aqa_symbol_watermarks.c.latest_bar_event_id.in_(event_ids)
                )
            )
        connection.execute(
            delete(aqa_bar_latest).where(aqa_bar_latest.c.bar_identity_id.in_(identity_ids))
        )
        connection.execute(
            delete(aqa_bar_events).where(aqa_bar_events.c.bar_identity_id.in_(identity_ids))
        )
        connection.execute(
            delete(aqa_bar_identities).where(aqa_bar_identities.c.bar_identity_id.in_(identity_ids))
        )
        connection.execute(
            delete(aqa_experiments).where(aqa_experiments.c.experiment_hash == _EXPERIMENT_HASH)
        )


def _drop_disposable_test_schemas(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
        connection.execute(DropSchema(COLLECTION_SCHEMA, cascade=True, if_exists=True))


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    upgrade_database(_DATABASE_URL)
    engine = create_engine(
        _TEST_DATABASE,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=postgres_connect_args("platform-market-data-pg-test", migration=True),
    )
    _cleanup(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="market_data_watermark_pg_test",
                experiment_version=1,
                schema_version=1,
                configuration={"fixture": True},
                content_hash="e" * 64,
                registered_at=_BASE,
            )
        )
    try:
        yield engine
    finally:
        # Governed event history is deliberately append-only. Reset the explicitly
        # guarded disposable database at the schema boundary instead of granting the
        # migration owner broad DELETE privileges solely for test teardown.
        _drop_disposable_test_schemas(engine)
        engine.dispose()


def test_postgres_serializes_identical_corrections_and_preserves_a_b_a_history(
    postgres_engine: Engine,
) -> None:
    repository = MarketDataRepository(postgres_engine)
    original = _bar(close=Decimal("100"), receipt_offset=0, source_event_id="provider-a-1")
    inserted = repository.append(original)
    correction = _bar(
        close=Decimal("102"),
        receipt_offset=10,
        source_event_id="provider-b-1",
        is_correction=True,
        correction_of_source_event_id="provider-a-1",
    )

    def write(delivery: int):
        return repository.append(
            replace(
                correction,
                received_at=correction.received_at + timedelta(microseconds=delivery),
                source_payload_hash=sha256_hex(("correction-b-delivery", delivery)),
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        correction_results = tuple(pool.map(write, range(16)))

    restored = repository.append(
        _bar(
            close=Decimal("100"),
            receipt_offset=30,
            source_event_id="provider-a-2",
            is_correction=True,
            correction_of_source_event_id="provider-b-1",
        )
    )
    history = repository.list_events(original.identity)

    assert inserted.status is BarWriteStatus.INSERTED
    assert sum(result.status is BarWriteStatus.CORRECTED for result in correction_results) == 1
    assert sum(result.status is BarWriteStatus.DUPLICATE for result in correction_results) == 15
    assert {result.event.bar_event_id for result in correction_results} == {history[1].bar_event_id}
    assert restored.status is BarWriteStatus.CORRECTED
    assert tuple(event.revision for event in history) == (1, 2, 3)
    assert tuple(event.bar.close for event in history) == (
        Decimal("100"),
        Decimal("102"),
        Decimal("100"),
    )
    assert tuple(event.bar.source_event_id for event in history) == (
        "provider-a-1",
        "provider-b-1",
        "provider-a-2",
    )
    assert tuple(event.bar.correction_of_source_event_id for event in history) == (
        None,
        "provider-a-1",
        "provider-b-1",
    )
    assert len({event.normalized_payload_hash for event in history}) == 3
    assert history[0].content_hash != history[2].content_hash
    assert history[0].bar_event_id != history[2].bar_event_id
    assert history[1].correction_of_event_id == history[0].bar_event_id
    assert history[2].correction_of_event_id == history[1].bar_event_id
    assert repository.latest(original.identity) == history[2]

    postgres_engine.dispose()
    restarted = MarketDataRepository(postgres_engine)
    assert restarted.list_events(original.identity) == history
    with postgres_engine.connect() as connection:
        identity_count = connection.scalar(
            select(func.count())
            .select_from(aqa_bar_identities)
            .where(aqa_bar_identities.c.bar_identity_id == original.identity.bar_identity_id)
        )
        event_count = connection.scalar(
            select(func.count())
            .select_from(aqa_bar_events)
            .where(aqa_bar_events.c.bar_identity_id == original.identity.bar_identity_id)
        )
        latest_count = connection.scalar(
            select(func.count())
            .select_from(aqa_bar_latest)
            .where(aqa_bar_latest.c.bar_identity_id == original.identity.bar_identity_id)
        )
    assert (identity_count, event_count, latest_count) == (1, 3, 1)


def test_postgres_serializes_watermark_writers_across_bar_identities(
    postgres_engine: Engine,
) -> None:
    repository = MarketDataRepository(postgres_engine)
    first_bar = _bar(
        identity=_identity(symbol="WMPG"),
        close=Decimal("100"),
        receipt_offset=0,
        source_event_id="watermark-minute-0",
    )
    next_bar = _bar(
        identity=_identity(minute=1, symbol="WMPG"),
        close=Decimal("102"),
        receipt_offset=0,
        source_event_id="watermark-minute-1",
    )
    repository.append(first_bar)
    repository.append(next_bar)
    lock_barrier = Barrier(2)

    def synchronize_watermark_lock(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, statement, context, executemany
        if isinstance(parameters, Mapping) and parameters.get("lock_namespace") == int(
            PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK
        ):
            lock_barrier.wait(timeout=10)

    def write(bar: BarWrite):
        return repository.append(
            bar,
            eligible_watermark=_watermark(bar, quality=bar.source_event_id or "unknown"),
        )

    event.listen(postgres_engine, "before_cursor_execute", synchronize_watermark_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(write, (first_bar, next_bar)))
    finally:
        event.remove(postgres_engine, "before_cursor_execute", synchronize_watermark_lock)

    assert all(result.status is BarWriteStatus.DUPLICATE for result in results)
    final = repository.watermark(
        experiment_hash=_EXPERIMENT_HASH,
        identity=first_bar.identity,
    )
    assert final is not None
    assert final.contiguous_through == next_bar.identity.end_at
    next_result = next(result for result in results if result.event.identity == next_bar.identity)
    assert final.latest_bar_event_id == next_result.event.bar_event_id


def test_postgres_sequential_watermark_advance_locks_both_identities_in_order(
    postgres_engine: Engine,
) -> None:
    repository = MarketDataRepository(postgres_engine)
    first_bar = _bar(
        identity=_identity(symbol="LQPG"),
        close=Decimal("100"),
        receipt_offset=0,
        source_event_id="sequential-minute-0",
    )
    next_bar = _bar(
        identity=_identity(minute=1, symbol="LQPG"),
        close=Decimal("102"),
        receipt_offset=0,
        source_event_id="sequential-minute-1",
    )
    prior_request = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
        first_bar.identity.bar_identity_id,
    )
    incoming_request = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
        next_bar.identity.bar_identity_id,
    )
    assert prior_request < incoming_request

    first = repository.append(
        first_bar,
        eligible_watermark=_watermark(first_bar, quality="sequential-minute-0"),
    )
    advanced = repository.append(
        next_bar,
        eligible_watermark=_watermark(next_bar, quality="sequential-minute-1"),
    )

    assert first.watermark_changed is True
    assert advanced.watermark_changed is True
    assert advanced.watermark is not None
    assert advanced.watermark.contiguous_through == next_bar.identity.end_at
    assert advanced.watermark.latest_bar_event_id == advanced.event.bar_event_id


def test_postgres_concurrent_quality_updates_keep_the_newest_timestamp(
    postgres_engine: Engine,
) -> None:
    repository = MarketDataRepository(postgres_engine)
    bar = _bar(
        identity=_identity(symbol="QMPG"),
        close=Decimal("100"),
        receipt_offset=0,
        source_event_id="quality-bar",
    )
    repository.append(bar)
    older = EligibleWatermark(
        experiment_hash=_EXPERIMENT_HASH,
        quality_hash=sha256_hex(("postgres-quality", "older")),
        updated_at=bar.received_at + timedelta(seconds=10),
    )
    newer = EligibleWatermark(
        experiment_hash=_EXPERIMENT_HASH,
        quality_hash=sha256_hex(("postgres-quality", "newer")),
        updated_at=bar.received_at + timedelta(seconds=20),
    )
    lock_barrier = Barrier(2)

    def synchronize_watermark_lock(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, statement, context, executemany
        if isinstance(parameters, Mapping) and parameters.get("lock_namespace") == int(
            PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK
        ):
            lock_barrier.wait(timeout=10)

    event.listen(postgres_engine, "before_cursor_execute", synchronize_watermark_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(
                pool.map(
                    lambda request: repository.append(bar, eligible_watermark=request),
                    (newer, older),
                )
            )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", synchronize_watermark_lock)

    final = repository.watermark(experiment_hash=_EXPERIMENT_HASH, identity=bar.identity)
    assert final is not None
    assert final.updated_at == newer.updated_at
    assert final.quality_hash == newer.quality_hash


def test_postgres_reader_holds_identity_lock_across_history_and_projection_reads(
    postgres_engine: Engine,
) -> None:
    repository = MarketDataRepository(postgres_engine)
    original_bar = _bar(
        identity=_identity(symbol="RDPG"),
        close=Decimal("100"),
        receipt_offset=0,
        source_event_id="reader-original",
    )
    original = repository.append(original_bar)
    correction_bar = _bar(
        identity=original_bar.identity,
        close=Decimal("102"),
        receipt_offset=10,
        source_event_id="reader-correction",
    )
    reader_at_history = ThreadEvent()
    allow_reader = ThreadEvent()
    writer_started = ThreadEvent()
    writer_completed = ThreadEvent()

    def pause_reader_before_history(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if (
            current_thread().name.startswith("market-reader")
            and statement.lstrip().upper().startswith("SELECT")
            and "aqa_bar_events" in statement
        ):
            reader_at_history.set()
            if not allow_reader.wait(timeout=10):
                raise TimeoutError("reader interleaving fixture timed out")

    def write_correction():
        writer_started.set()
        try:
            return repository.append(correction_bar)
        finally:
            writer_completed.set()

    event.listen(postgres_engine, "before_cursor_execute", pause_reader_before_history)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-reader") as reader_pool,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-writer") as writer_pool,
        ):
            reader_future = reader_pool.submit(repository.list_events, original_bar.identity)
            assert reader_at_history.wait(timeout=10)
            writer_future = writer_pool.submit(write_correction)
            assert writer_started.wait(timeout=10)
            assert not writer_completed.wait(timeout=0.25)
            allow_reader.set()
            observed = reader_future.result(timeout=10)
            correction = writer_future.result(timeout=10)
    finally:
        allow_reader.set()
        event.remove(postgres_engine, "before_cursor_execute", pause_reader_before_history)

    assert observed == (original.event,)
    assert correction.status is BarWriteStatus.CORRECTED
    assert repository.latest(original_bar.identity) == correction.event
