"""Atomic, idempotent, and restart-safe platform market-data persistence tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, Table, create_engine, delete, event, func, insert, select, update
from sqlalchemy.engine import Connection

from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteStatus,
    EligibleWatermark,
    MarketDataIntegrityError,
    MarketDataPersistenceError,
    MarketDataRepository,
    MarketDataValidationError,
    _watermark_lock_resource,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_audit_events,
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_experiments,
    aqa_symbol_watermarks,
    metadata,
)

_BASE = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
_EXPERIMENT_HASH = "a" * 64


def _sqlite_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
        pool_pre_ping=True,
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(engine)
    return engine


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = _sqlite_engine(tmp_path / "market-data.sqlite3")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def repository(sqlite_engine: Engine) -> MarketDataRepository:
    return MarketDataRepository(sqlite_engine)


def _identity(*, minute: int = 0, symbol: str = "NVDA") -> BarIdentity:
    start = _BASE + timedelta(minutes=minute)
    return BarIdentity(
        provider="fixture",
        feed="iex",
        adjustment="raw",
        symbol=symbol,
        timeframe="1Min",
        start_at=start,
        end_at=start + timedelta(minutes=1),
    )


def _bar(
    *,
    identity: BarIdentity | None = None,
    close: Decimal = Decimal("10.25"),
    received_offset: int = 0,
    source: str = "fixture",
    source_event_id: str = "fixture-event",
    source_mode: str = "offline_fixture",
    is_correction: bool = False,
    correction_of_source_event_id: str | None = None,
) -> BarWrite:
    selected = _identity() if identity is None else identity
    return BarWrite(
        identity=selected,
        received_at=selected.end_at + timedelta(seconds=received_offset),
        provider_timestamp=selected.end_at,
        open=Decimal("10.00"),
        high=Decimal("11.00"),
        low=Decimal("9.00"),
        close=close,
        volume=Decimal("1000"),
        trade_count=25,
        vwap=Decimal("10.20"),
        quality_flags=("complete",),
        source=source,
        source_mode=source_mode,
        source_event_id=source_event_id,
        source_payload_hash=sha256_hex(("source", source_event_id)),
        is_correction=is_correction,
        correction_of_source_event_id=correction_of_source_event_id,
    )


def _watermark(
    bar: BarWrite,
    *,
    quality: str,
    delay_seconds: int = 1,
) -> EligibleWatermark:
    return EligibleWatermark(
        experiment_hash=_EXPERIMENT_HASH,
        quality_hash=sha256_hex(("quality", quality)),
        updated_at=bar.received_at + timedelta(seconds=delay_seconds),
    )


def _seed_experiment(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="market_data_repository_test",
                experiment_version=1,
                schema_version=1,
                configuration={"fixture": True},
                content_hash="b" * 64,
                registered_at=_BASE,
            )
        )


def _row_count(connection: Connection, table: Table) -> int:
    return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def test_latest_only_idempotency_allows_a_b_a_revision_history(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    first_payload = _bar()
    duplicate_payload = replace(
        first_payload,
        received_at=first_payload.received_at + timedelta(seconds=1),
        source_payload_hash=sha256_hex(("source", "first-redelivery")),
    )
    corrected_payload = _bar(close=Decimal("10.50"), received_offset=2)
    restored_payload = replace(
        first_payload,
        received_at=first_payload.received_at + timedelta(seconds=3),
        source_payload_hash=sha256_hex(("source", "provider-restored-a")),
    )

    inserted = repository.append(first_payload)
    duplicate = repository.append(duplicate_payload)
    corrected = repository.append(corrected_payload)
    restored = repository.append(restored_payload)
    repeated_latest = repository.append(
        replace(restored_payload, received_at=restored_payload.received_at + timedelta(seconds=1))
    )

    assert inserted.status is BarWriteStatus.INSERTED
    assert inserted.event.revision == inserted.latest_version == 1
    assert duplicate.status is BarWriteStatus.DUPLICATE
    assert duplicate.event == inserted.event
    assert duplicate.latest_version == 1
    assert corrected.status is BarWriteStatus.CORRECTED
    assert corrected.event.revision == corrected.latest_version == 2
    assert corrected.event.correction_of_event_id == inserted.event.bar_event_id
    assert restored.status is BarWriteStatus.CORRECTED
    assert restored.event.revision == restored.latest_version == 3
    assert restored.event.correction_of_event_id == corrected.event.bar_event_id
    assert restored.event.normalized_payload_hash == inserted.event.normalized_payload_hash
    assert restored.event.content_hash != inserted.event.content_hash
    assert restored.event.bar_event_id != inserted.event.bar_event_id
    assert repeated_latest.status is BarWriteStatus.DUPLICATE
    assert repeated_latest.event == restored.event
    assert repeated_latest.latest_version == 3
    assert repository.list_events(first_payload.identity) == (
        inserted.event,
        corrected.event,
        restored.event,
    )
    assert repository.latest(first_payload.identity) == restored.event

    with sqlite_engine.connect() as connection:
        assert _row_count(connection, aqa_bar_identities) == 1
        assert _row_count(connection, aqa_bar_events) == 3
        assert _row_count(connection, aqa_bar_latest) == 1


def test_canonical_correction_metadata_changes_semantics_and_round_trips(
    repository: MarketDataRepository,
) -> None:
    fixture_identity = _identity()
    external_identity = BarIdentity(
        provider="alpaca",
        feed=fixture_identity.feed,
        adjustment=fixture_identity.adjustment,
        symbol=fixture_identity.symbol,
        timeframe=fixture_identity.timeframe,
        start_at=fixture_identity.start_at,
        end_at=fixture_identity.end_at,
    )
    original = _bar(
        identity=external_identity,
        source="alpaca",
        source_mode="external_provider",
        source_event_id="alpaca_event_1",
    )
    update = replace(
        original,
        received_at=original.received_at + timedelta(seconds=1),
        is_correction=True,
        correction_of_source_event_id="alpaca_event_0",
        source_payload_hash=sha256_hex(("source", "alpaca-update")),
    )

    inserted = repository.append(original)
    corrected = repository.append(update)
    reloaded = repository.latest(original.identity)

    assert inserted.status is BarWriteStatus.INSERTED
    assert corrected.status is BarWriteStatus.CORRECTED
    assert corrected.event.normalized_payload_hash != inserted.event.normalized_payload_hash
    assert reloaded == corrected.event
    assert reloaded is not None
    assert reloaded.bar.source_mode == "external_provider"
    assert reloaded.bar.is_correction is True
    assert reloaded.bar.correction_of_source_event_id == "alpaca_event_0"


def test_new_correction_appends_one_retry_stable_audit_event(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    original = _bar(source_event_id="provider-event-a")
    correction = _bar(
        close=Decimal("10.50"),
        received_offset=1,
        source_event_id="provider-event-b",
        is_correction=True,
        correction_of_source_event_id="provider-event-a",
    )

    repository.append(original)
    corrected = repository.append(correction)
    duplicate = repository.append(
        replace(
            correction,
            received_at=correction.received_at + timedelta(seconds=1),
            source_payload_hash=sha256_hex(("delivery", "provider-event-b", 2)),
        )
    )

    stream_id = f"aqa_collector:bar:{original.identity.bar_identity_id}"
    audit_events = AuditRepository(
        sqlite_engine,
        writer=AuditWriter.COLLECTOR,
    ).list_events(stream_id=stream_id)

    assert corrected.status is BarWriteStatus.CORRECTED
    assert duplicate.status is BarWriteStatus.DUPLICATE
    assert duplicate.event == corrected.event
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "bar.corrected"
    assert audit_events[0].occurred_at == corrected.event.bar.received_at
    assert audit_events[0].audit_payload.value == {
        "bar_id": corrected.event.bar_event_id,
        "content_hash": corrected.event.content_hash,
        "idempotency_key": corrected.event.bar_event_id,
        "revision": 2,
        "symbol": original.identity.symbol,
    }


def test_audit_failure_rolls_back_correction_revision_and_latest_projection(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    original = repository.append(_bar(source_event_id="audit-rollback-a"))
    correction = _bar(
        close=Decimal("10.50"),
        received_offset=1,
        source_event_id="audit-rollback-b",
        is_correction=True,
        correction_of_source_event_id="audit-rollback-a",
    )

    class InjectedAuditFailure(RuntimeError):
        pass

    def fail_audit_insert(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if "aqa_audit_events" in statement and statement.lstrip().upper().startswith("INSERT"):
            raise InjectedAuditFailure("injected correction audit failure")

    event.listen(sqlite_engine, "before_cursor_execute", fail_audit_insert)
    try:
        with pytest.raises(InjectedAuditFailure, match="correction audit failure"):
            repository.append(correction)
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", fail_audit_insert)

    assert repository.list_events(original.event.identity) == (original.event,)
    assert repository.latest(original.event.identity) == original.event
    with sqlite_engine.connect() as connection:
        assert _row_count(connection, aqa_bar_events) == 1
        assert _row_count(connection, aqa_bar_latest) == 1
        assert _row_count(connection, aqa_audit_events) == 0


def test_zero_vwap_is_valid_market_data(repository: MarketDataRepository) -> None:
    zero_vwap = replace(_bar(source_event_id="zero-vwap"), vwap=Decimal("0"))

    inserted = repository.append(zero_vwap)

    assert inserted.event.bar.vwap == Decimal("0")
    assert repository.latest(zero_vwap.identity) == inserted.event


def test_changed_lineage_creates_a_revision_when_bar_values_are_unchanged(
    repository: MarketDataRepository,
) -> None:
    first_payload = replace(_bar(), lineage_hash=sha256_hex(("lineage", "first")))
    revised_payload = replace(
        first_payload,
        received_at=first_payload.received_at + timedelta(seconds=1),
        source_payload_hash=sha256_hex(("source", "recomputed-lineage")),
        lineage_hash=sha256_hex(("lineage", "recomputed")),
    )

    inserted = repository.append(first_payload)
    revised = repository.append(revised_payload)

    assert revised.status is BarWriteStatus.CORRECTED
    assert revised.event.revision == 2
    assert revised.event.bar.open == inserted.event.bar.open
    assert revised.event.bar.close == inserted.event.bar.close
    assert revised.event.normalized_payload_hash != inserted.event.normalized_payload_hash


def test_eligible_watermark_advances_only_contiguously_and_tracks_corrections(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    first_bar = _bar(identity=_identity(minute=0), source_event_id="minute-0")
    gap_bar = _bar(identity=_identity(minute=2), source_event_id="minute-2")
    middle_bar = _bar(identity=_identity(minute=1), source_event_id="minute-1")

    first = repository.append(
        first_bar,
        eligible_watermark=_watermark(first_bar, quality="minute-0"),
    )
    gap = repository.append(
        gap_bar,
        eligible_watermark=_watermark(gap_bar, quality="minute-2"),
    )
    middle = repository.append(
        middle_bar,
        eligible_watermark=_watermark(middle_bar, quality="minute-1"),
    )
    recovered = repository.append(
        replace(gap_bar, received_at=gap_bar.received_at + timedelta(seconds=10)),
        eligible_watermark=_watermark(
            replace(gap_bar, received_at=gap_bar.received_at + timedelta(seconds=10)),
            quality="minute-2",
        ),
    )

    assert first.watermark_changed is True
    assert first.watermark is not None
    assert first.watermark.contiguous_through == first_bar.identity.end_at
    assert first.watermark.version == 1
    assert gap.watermark_changed is False
    assert gap.watermark == first.watermark
    assert middle.watermark_changed is True
    assert middle.watermark is not None
    assert middle.watermark.contiguous_through == middle_bar.identity.end_at
    assert middle.watermark.version == 2
    assert recovered.status is BarWriteStatus.DUPLICATE
    assert recovered.watermark_changed is True
    assert recovered.watermark is not None
    assert recovered.watermark.contiguous_through == gap_bar.identity.end_at
    assert recovered.watermark.version == 3

    stale_correction_bar = _bar(
        identity=first_bar.identity,
        close=Decimal("10.75"),
        received_offset=20,
        source_event_id="minute-0-correction",
    )
    stale = repository.append(
        stale_correction_bar,
        eligible_watermark=_watermark(stale_correction_bar, quality="stale-correction"),
    )
    assert stale.status is BarWriteStatus.CORRECTED
    assert stale.watermark_changed is False
    assert stale.watermark == recovered.watermark

    current_correction_bar = _bar(
        identity=gap_bar.identity,
        close=Decimal("10.75"),
        received_offset=30,
        source_event_id="minute-2-correction",
    )
    current = repository.append(
        current_correction_bar,
        eligible_watermark=_watermark(current_correction_bar, quality="current-correction"),
    )
    assert current.status is BarWriteStatus.CORRECTED
    assert current.watermark_changed is True
    assert current.watermark is not None
    assert current.watermark.contiguous_through == gap_bar.identity.end_at
    assert current.watermark.latest_bar_event_id == current.event.bar_event_id
    assert current.watermark.version == 4
    assert (
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=first_bar.identity,
        )
        == current.watermark
    )


def test_failure_after_event_and_projection_rolls_back_every_owned_row(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    bar = _bar()

    class InjectedFailure(RuntimeError):
        pass

    def fail_before_watermark(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if "aqa_symbol_watermarks" in statement and statement.lstrip().upper().startswith("INSERT"):
            raise InjectedFailure("injected after latest projection")

    event.listen(sqlite_engine, "before_cursor_execute", fail_before_watermark)
    try:
        with pytest.raises(InjectedFailure, match="after latest"):
            repository.append(
                bar,
                eligible_watermark=_watermark(bar, quality="rollback"),
            )
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", fail_before_watermark)

    with sqlite_engine.connect() as connection:
        assert _row_count(connection, aqa_bar_identities) == 0
        assert _row_count(connection, aqa_bar_events) == 0
        assert _row_count(connection, aqa_bar_latest) == 0
        assert _row_count(connection, aqa_symbol_watermarks) == 0


def test_bound_source_event_id_preserves_sql_text_as_data(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    injected = "event'); DROP TABLE aqa_bar_events; --"
    result = repository.append(_bar(source_event_id=injected))

    assert result.event.bar.source_event_id == injected
    assert repository.latest(result.event.identity) == result.event
    with sqlite_engine.connect() as connection:
        assert _row_count(connection, aqa_bar_events) == 1


def test_restart_reads_and_independently_verifies_durable_history(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    first_engine = _sqlite_engine(path)
    identity = _identity()
    try:
        first_repository = MarketDataRepository(first_engine)
        first = first_repository.append(_bar(identity=identity))
        second = first_repository.append(
            _bar(identity=identity, close=Decimal("10.75"), received_offset=1)
        )
    finally:
        first_engine.dispose()

    second_engine = _sqlite_engine(path)
    try:
        restarted = MarketDataRepository(second_engine)
        assert restarted.list_events(identity) == (first.event, second.event)
        assert restarted.latest(identity) == second.event
    finally:
        second_engine.dispose()


def test_owned_reads_commit_explicit_transactions_and_external_reads_require_one(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    expected = repository.append(_bar()).event
    observed: list[str] = []

    def record_begin(connection: object) -> None:
        del connection
        observed.append("begin")

    def record_commit(connection: object) -> None:
        del connection
        observed.append("commit")

    event.listen(sqlite_engine, "begin", record_begin)
    event.listen(sqlite_engine, "commit", record_commit)
    try:
        assert repository.list_events(expected.identity) == (expected,)
        assert repository.latest(expected.identity) == expected
        assert (
            repository.watermark(
                experiment_hash=_EXPERIMENT_HASH,
                identity=expected.identity,
            )
            is None
        )
    finally:
        event.remove(sqlite_engine, "begin", record_begin)
        event.remove(sqlite_engine, "commit", record_commit)

    assert observed == ["begin", "commit"] * 3

    with sqlite_engine.connect() as connection:
        with pytest.raises(MarketDataPersistenceError, match="active transaction"):
            repository.list_events(expected.identity, connection=connection)
        with pytest.raises(MarketDataPersistenceError, match="active transaction"):
            repository.latest(expected.identity, connection=connection)
        with pytest.raises(MarketDataPersistenceError, match="active transaction"):
            repository.watermark(
                experiment_hash=_EXPERIMENT_HASH,
                identity=expected.identity,
                connection=connection,
            )

    with sqlite_engine.begin() as connection:
        assert repository.latest(expected.identity, connection=connection) == expected


def test_watermark_read_rejects_a_validly_rehashed_cross_series_event_reference(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    primary_bar = _bar(source_event_id="primary-series")
    primary = repository.append(
        primary_bar,
        eligible_watermark=_watermark(primary_bar, quality="primary-series"),
    )
    assert primary.watermark is not None
    foreign = repository.append(
        _bar(
            identity=_identity(symbol="AMD"),
            source_event_id="foreign-series",
        )
    )
    watermark = primary.watermark
    forged_hash = sha256_hex(
        (
            watermark.symbol_watermark_id,
            watermark.experiment_hash,
            watermark.provider,
            watermark.feed,
            watermark.adjustment,
            watermark.symbol,
            watermark.timeframe,
            watermark.contiguous_through,
            watermark.quality_hash,
            foreign.event.bar_event_id,
            watermark.version,
            watermark.updated_at,
        )
    )
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_symbol_watermarks)
            .where(aqa_symbol_watermarks.c.symbol_watermark_id == watermark.symbol_watermark_id)
            .values(
                latest_bar_event_id=foreign.event.bar_event_id,
                content_hash=forged_hash,
            )
        )

    with pytest.raises(MarketDataIntegrityError, match="reference is inconsistent"):
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=primary_bar.identity,
        )


def test_canonical_event_identity_detects_immutable_provenance_tampering(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    inserted = repository.append(_bar(source_event_id="original-delivery"))
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_bar_events)
            .where(aqa_bar_events.c.bar_event_id == inserted.event.bar_event_id)
            .values(source_event_id="tampered-delivery")
        )

    with pytest.raises(MarketDataIntegrityError, match="event identity"):
        repository.latest(inserted.event.identity)


def test_event_read_rejects_a_tampered_stored_normalized_hash(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    inserted = repository.append(_bar(source_event_id="normalized-hash-tamper"))
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_bar_events)
            .where(aqa_bar_events.c.bar_event_id == inserted.event.bar_event_id)
            .values(normalized_payload_hash="f" * 64)
        )

    with pytest.raises(MarketDataIntegrityError, match="content is inconsistent"):
        repository.latest(inserted.event.identity)


@pytest.mark.parametrize("read_method", ["list_events", "latest"])
def test_event_reads_reject_a_tampered_identity_origin_timestamp(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
    read_method: str,
) -> None:
    inserted = repository.append(_bar(source_event_id=f"identity-origin-{read_method}"))
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_bar_identities)
            .where(aqa_bar_identities.c.bar_identity_id == inserted.event.identity.bar_identity_id)
            .values(created_at=inserted.event.bar.received_at + timedelta(seconds=1))
        )

    read = getattr(repository, read_method)
    with pytest.raises(MarketDataIntegrityError, match="origin timestamp is inconsistent"):
        read(inserted.event.identity)


@pytest.mark.parametrize("read_method", ["list_events", "latest"])
def test_event_reads_reject_an_orphaned_persisted_identity(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
    read_method: str,
) -> None:
    inserted = repository.append(_bar(source_event_id=f"orphan-{read_method}"))
    identity_id = inserted.event.identity.bar_identity_id
    with sqlite_engine.begin() as connection:
        connection.execute(
            delete(aqa_bar_latest).where(aqa_bar_latest.c.bar_identity_id == identity_id)
        )
        connection.execute(
            delete(aqa_bar_events).where(aqa_bar_events.c.bar_identity_id == identity_id)
        )

    read = getattr(repository, read_method)
    with pytest.raises(MarketDataIntegrityError, match="identity has no revision history"):
        read(inserted.event.identity)


@pytest.mark.parametrize(
    ("mutation_sql", "sentinel"),
    [
        ("UPDATE aqa_bar_events SET received_at = ? WHERE bar_event_id = ?", "secret-time"),
        (
            "UPDATE aqa_bar_events SET quality_flags = ? WHERE bar_event_id = ?",
            "secret-json",
        ),
        ("UPDATE aqa_bar_events SET open = ? WHERE bar_event_id = ?", "secret-decimal"),
    ],
)
def test_raw_result_decoding_failures_are_redacted_as_integrity_errors(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
    mutation_sql: str,
    sentinel: str,
) -> None:
    inserted = repository.append(_bar(source_event_id=f"raw-{sentinel}"))
    with sqlite_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(mutation_sql, (sentinel, inserted.event.bar_event_id))
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(MarketDataIntegrityError) as error:
        repository.latest(inserted.event.identity)

    assert str(error.value) == "persisted market-data state is malformed"
    assert sentinel not in str(error.value)


def test_deeply_nested_persisted_json_is_redacted_as_an_integrity_error(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    inserted = repository.append(_bar(source_event_id="deep-json"))
    nested_json = ("[" * 2_000) + ("0") + ("]" * 2_000)
    with sqlite_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(
            "UPDATE aqa_bar_events SET quality_flags = ? WHERE bar_event_id = ?",
            (nested_json, inserted.event.bar_event_id),
        )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    with pytest.raises(MarketDataIntegrityError) as error:
        repository.latest(inserted.event.identity)

    assert str(error.value) == "persisted market-data state is malformed"


def test_watermark_fails_closed_after_unapproved_correction_and_recovers_on_retry(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    original_bar = _bar(source_event_id="approved-original")
    original = repository.append(
        original_bar,
        eligible_watermark=_watermark(original_bar, quality="approved-original"),
    )
    correction_bar = _bar(
        close=Decimal("10.75"),
        received_offset=10,
        source_event_id="unapproved-correction",
    )
    correction = repository.append(correction_bar)

    assert original.watermark is not None
    assert correction.status is BarWriteStatus.CORRECTED
    with pytest.raises(MarketDataIntegrityError, match="not the latest revision"):
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=original_bar.identity,
        )

    recovered = repository.append(
        replace(
            correction_bar,
            received_at=correction_bar.received_at + timedelta(seconds=1),
            source_payload_hash=sha256_hex(("source", "approved-correction-retry")),
        ),
        eligible_watermark=EligibleWatermark(
            experiment_hash=_EXPERIMENT_HASH,
            quality_hash=sha256_hex(("quality", "approved-correction")),
            updated_at=correction_bar.received_at + timedelta(seconds=2),
        ),
    )

    assert recovered.status is BarWriteStatus.DUPLICATE
    assert recovered.event == correction.event
    assert recovered.watermark_changed is True
    assert recovered.watermark is not None
    assert recovered.watermark.latest_bar_event_id == correction.event.bar_event_id
    assert (
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=original_bar.identity,
        )
        == recovered.watermark
    )


def test_watermark_recovers_after_multiple_unapproved_corrections(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    original_bar = _bar(source_event_id="approved-origin")
    repository.append(
        original_bar,
        eligible_watermark=_watermark(original_bar, quality="approved-origin"),
    )
    second_bar = _bar(
        close=Decimal("10.75"),
        received_offset=10,
        source_event_id="unapproved-second",
    )
    repository.append(second_bar)
    third_bar = _bar(
        close=Decimal("11.00"),
        received_offset=20,
        source_event_id="unapproved-third",
    )
    third = repository.append(third_bar)

    with pytest.raises(MarketDataIntegrityError, match="not the latest revision"):
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=original_bar.identity,
        )

    recovered = repository.append(
        replace(
            third_bar,
            received_at=third_bar.received_at + timedelta(seconds=1),
            source_payload_hash=sha256_hex(("source", "approved-third-retry")),
        ),
        eligible_watermark=EligibleWatermark(
            experiment_hash=_EXPERIMENT_HASH,
            quality_hash=sha256_hex(("quality", "approved-third")),
            updated_at=third_bar.received_at + timedelta(seconds=2),
        ),
    )

    assert recovered.status is BarWriteStatus.DUPLICATE
    assert recovered.event == third.event
    assert recovered.watermark_changed is True
    assert recovered.watermark is not None
    assert recovered.watermark.latest_bar_event_id == third.event.bar_event_id


def test_watermark_read_rejects_timestamp_before_referenced_event(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    bar = _bar(source_event_id="watermark-time-order")
    result = repository.append(
        bar,
        eligible_watermark=_watermark(bar, quality="watermark-time-order"),
    )
    assert result.watermark is not None
    watermark = result.watermark
    forged_updated_at = bar.received_at - timedelta(seconds=1)
    forged_hash = sha256_hex(
        (
            watermark.symbol_watermark_id,
            watermark.experiment_hash,
            watermark.provider,
            watermark.feed,
            watermark.adjustment,
            watermark.symbol,
            watermark.timeframe,
            watermark.contiguous_through,
            watermark.quality_hash,
            watermark.latest_bar_event_id,
            watermark.version,
            forged_updated_at,
        )
    )
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_symbol_watermarks)
            .where(aqa_symbol_watermarks.c.symbol_watermark_id == watermark.symbol_watermark_id)
            .values(updated_at=forged_updated_at, content_hash=forged_hash)
        )

    with pytest.raises(MarketDataIntegrityError, match="timestamp precedes referenced event"):
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=bar.identity,
        )


def test_watermark_rejects_a_validly_rehashed_nonlatest_revision(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    original_bar = _bar(source_event_id="revision-one")
    original = repository.append(
        original_bar,
        eligible_watermark=_watermark(original_bar, quality="revision-one"),
    )
    correction_bar = _bar(
        close=Decimal("10.75"),
        received_offset=10,
        source_event_id="revision-two",
    )
    correction = repository.append(
        correction_bar,
        eligible_watermark=_watermark(correction_bar, quality="revision-two"),
    )
    assert original.watermark is not None
    assert correction.watermark is not None
    watermark = correction.watermark
    forged_hash = sha256_hex(
        (
            watermark.symbol_watermark_id,
            watermark.experiment_hash,
            watermark.provider,
            watermark.feed,
            watermark.adjustment,
            watermark.symbol,
            watermark.timeframe,
            watermark.contiguous_through,
            watermark.quality_hash,
            original.event.bar_event_id,
            watermark.version,
            watermark.updated_at,
        )
    )
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_symbol_watermarks)
            .where(aqa_symbol_watermarks.c.symbol_watermark_id == watermark.symbol_watermark_id)
            .values(
                latest_bar_event_id=original.event.bar_event_id,
                content_hash=forged_hash,
            )
        )

    with pytest.raises(MarketDataIntegrityError, match="not the latest revision"):
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=original_bar.identity,
        )


def test_watermark_quality_updates_are_timestamp_monotonic_and_conflicts_fail_closed(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    bar = _bar(source_event_id="quality-revision")
    repository.append(bar)
    older = _watermark(bar, quality="older", delay_seconds=10)
    newer = _watermark(bar, quality="newer", delay_seconds=20)

    newer_result = repository.append(bar, eligible_watermark=newer)
    stale_result = repository.append(bar, eligible_watermark=older)

    assert newer_result.watermark is not None
    assert stale_result.watermark_changed is False
    assert stale_result.watermark == newer_result.watermark
    with pytest.raises(MarketDataValidationError, match="same timestamp"):
        repository.append(
            bar,
            eligible_watermark=EligibleWatermark(
                experiment_hash=_EXPERIMENT_HASH,
                quality_hash=sha256_hex(("quality", "conflicting")),
                updated_at=newer.updated_at,
            ),
        )
    assert (
        repository.watermark(
            experiment_hash=_EXPERIMENT_HASH,
            identity=bar.identity,
        )
        == newer_result.watermark
    )


def test_concurrent_watermark_quality_updates_converge_on_latest_timestamp(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    _seed_experiment(sqlite_engine)
    bar = _bar(source_event_id="concurrent-quality")
    repository.append(bar)
    older = _watermark(bar, quality="concurrent-older", delay_seconds=10)
    newer = _watermark(bar, quality="concurrent-newer", delay_seconds=20)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda request: repository.append(bar, eligible_watermark=request),
                (newer, older),
            )
        )

    final = repository.watermark(experiment_hash=_EXPERIMENT_HASH, identity=bar.identity)
    assert final is not None
    assert final.updated_at == newer.updated_at
    assert final.quality_hash == newer.quality_hash
    assert any(result.watermark == final for result in results)


def test_shared_transaction_composes_market_data_and_audit_commit_and_rollback(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    audit_repository = AuditRepository(sqlite_engine, writer=AuditWriter.COLLECTOR)
    committed_bar = _bar(identity=_identity(symbol="AAPL"), source_event_id="committed-bar")
    committed_payload = AuditPayload.from_mapping(
        {
            "idempotency_key": f"market_data_{sha256_hex('commit')}",
            "state": "committed",
        }
    )
    with repository.transaction() as connection:
        committed = repository.append(committed_bar, connection=connection)
        audit_event = audit_repository.append(
            stream_id="aqa_collector:data:committed",
            event_type="data.persisted",
            occurred_at=committed_bar.received_at,
            payload=committed_payload,
            connection=connection,
        )

    assert repository.latest(committed_bar.identity) == committed.event
    assert audit_repository.list_events(stream_id=audit_event.stream_id) == (audit_event,)

    rolled_back_bar = _bar(identity=_identity(symbol="META"), source_event_id="rolled-back-bar")
    with (
        pytest.raises(RuntimeError, match="abort composed transaction"),
        audit_repository.transaction() as connection,
    ):
        repository.append(rolled_back_bar, connection=connection)
        audit_repository.append(
            stream_id="aqa_collector:data:rolled-back",
            event_type="data.persisted",
            occurred_at=rolled_back_bar.received_at,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"market_data_{sha256_hex('rollback')}",
                    "state": "rolled-back",
                }
            ),
            connection=connection,
        )
        raise RuntimeError("abort composed transaction")

    assert repository.latest(rolled_back_bar.identity) is None
    assert audit_repository.list_events(stream_id="aqa_collector:data:rolled-back") == ()


def test_concurrent_identical_sqlite_writers_create_one_effective_revision(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
) -> None:
    base = _bar()

    def write(index: int):
        return repository.append(
            replace(
                base,
                received_at=base.received_at + timedelta(microseconds=index),
                source_payload_hash=sha256_hex(("delivery", index)),
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(write, range(24)))

    assert sum(result.status is BarWriteStatus.INSERTED for result in results) == 1
    assert sum(result.status is BarWriteStatus.DUPLICATE for result in results) == 23
    latest = repository.latest(base.identity)
    assert latest is not None
    assert {result.event.bar_event_id for result in results} == {latest.bar_event_id}
    with sqlite_engine.connect() as connection:
        assert _row_count(connection, aqa_bar_events) == 1
        assert _row_count(connection, aqa_bar_latest) == 1


def test_append_rejects_foreign_and_unserialized_sqlite_transactions(
    repository: MarketDataRepository,
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    with (
        sqlite_engine.begin() as connection,
        pytest.raises(MarketDataPersistenceError, match="serialized transaction"),
    ):
        repository.append(_bar(), connection=connection)

    foreign_engine = _sqlite_engine(tmp_path / "foreign.sqlite3")
    try:
        with (
            foreign_engine.begin() as connection,
            pytest.raises(MarketDataPersistenceError, match="another repository"),
        ):
            repository.append(_bar(), connection=connection)
    finally:
        foreign_engine.dispose()


def test_postgres_watermark_lock_resource_is_stable_for_the_complete_series() -> None:
    expected = "symbol_watermark_9b0978897ce78b442cda61ed1701be0981d3694e6a69d759192a47bb5dd3b682"

    assert _watermark_lock_resource(_EXPERIMENT_HASH, _identity()) == expected
    assert expected == _watermark_lock_resource(_EXPERIMENT_HASH, _identity(minute=1))
    assert expected != _watermark_lock_resource(
        _EXPERIMENT_HASH,
        _identity(symbol="AMD"),
    )


def test_bar_identity_rejects_hyphenated_symbol_outside_the_generic_contract() -> None:
    with pytest.raises(MarketDataValidationError, match="symbol"):
        _identity(symbol="BRK-B")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bar: replace(bar, received_at=bar.identity.end_at.replace(tzinfo=None)),
        lambda bar: replace(
            bar,
            received_at=bar.identity.end_at.astimezone(timezone(timedelta(hours=-4))),
        ),
        lambda bar: replace(bar, close=Decimal("NaN")),
        lambda bar: replace(bar, source_payload_hash="not-a-hash"),
        lambda bar: replace(bar, lineage_hash="not-a-hash"),
        lambda bar: replace(bar, volume=Decimal("1.5")),
    ],
)
def test_bar_write_rejects_invalid_time_numeric_and_hash_inputs(
    mutate: Callable[[BarWrite], BarWrite],
) -> None:
    with pytest.raises(MarketDataValidationError):
        mutate(_bar())
