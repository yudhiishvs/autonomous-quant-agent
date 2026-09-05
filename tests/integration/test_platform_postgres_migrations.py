"""Guarded PostgreSQL integration tests for the generic-platform migration."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event as ThreadEvent
from time import monotonic
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.migrations import (
    _alembic_config,
    database_revision,
    upgrade_database,
)
from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as COLLECTION_SCHEMA
from adaptive_trader.collection.schema import collection_universes
from adaptive_trader.collection.schema import metadata as collection_metadata
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import BarIdentity
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    PLATFORM_TABLE_NAMES,
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_broker_orders,
    aqa_decision_slots,
    aqa_execution_plans,
    aqa_experiments,
    aqa_fills,
    aqa_order_intents,
    aqa_risk_decisions,
    aqa_signal_envelopes,
)
from adaptive_trader.platform.storage.tables import metadata as platform_metadata

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

_PRIOR_REVISION = "20260903_0001"


def _engine(*, application_name: str) -> Engine:
    return create_engine(
        _TEST_DATABASE,
        hide_parameters=True,
        connect_args=postgres_connect_args(application_name, migration=True),
    )


def _drop_disposable_test_schemas() -> None:
    engine = _engine(application_name="platform-migration-test-reset")
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(COLLECTION_SCHEMA, cascade=True, if_exists=True))
    finally:
        engine.dispose()


def _include_owned_name(
    name: str | None,
    object_type: str,
    parent_names: dict[str, str | None],
) -> bool:
    if object_type == "schema":
        return name in {COLLECTION_SCHEMA, PLATFORM_SCHEMA}
    if object_type == "table":
        return (
            parent_names.get("schema_name") in {COLLECTION_SCHEMA, PLATFORM_SCHEMA}
            and name != "alembic_version"
        )
    return True


def _metadata_differences(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "include_name": _include_owned_name,
                "include_schemas": True,
                "version_table_schema": COLLECTION_SCHEMA,
            },
        )
        return compare_metadata(context, (collection_metadata, platform_metadata))


def _seed_legacy_bar_history(
    engine: Engine,
    *,
    revisions: int,
    projected_revision: int | None,
) -> tuple[dict[str, Any], ...]:
    start_at = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
    end_at = datetime(2026, 9, 5, 14, 31, tzinfo=UTC)
    identity_hash = sha256_hex(("fixture", "iex", "raw", "MIGR", "1Min", start_at))
    identity_id = f"bar_identity_{identity_hash}"
    first_received_at = end_at.replace(second=1)
    events: list[dict[str, Any]] = []
    previous_event_id: str | None = None
    with engine.begin() as connection:
        connection.execute(
            aqa_bar_identities.insert().values(
                bar_identity_id=identity_id,
                provider="fixture",
                feed="iex",
                adjustment="raw",
                symbol="MIGR",
                timeframe="1Min",
                start_at=start_at,
                end_at=end_at,
                content_hash=identity_hash,
                created_at=first_received_at,
            )
        )
        for revision in range(1, revisions + 1):
            close = Decimal("10.25") + (Decimal("0.25") * (revision - 1))
            received_at = end_at.replace(second=revision)
            normalized_hash = sha256_hex(
                (
                    "canonical_bar_v1",
                    1,
                    identity_hash,
                    end_at,
                    end_at,
                    Decimal("10"),
                    Decimal("11"),
                    Decimal("9"),
                    close,
                    Decimal("1000"),
                    25,
                    Decimal("10.20"),
                    ("complete",),
                )
            )
            event_id = f"bar_event_{sha256_hex((identity_id, revision, normalized_hash))}"
            values = {
                "bar_event_id": event_id,
                "bar_identity_id": identity_id,
                "revision": revision,
                "schema_version": 1,
                "provider_timestamp": end_at,
                "received_at": received_at,
                "open": Decimal("10"),
                "high": Decimal("11"),
                "low": Decimal("9"),
                "close": close,
                "volume": Decimal("1000"),
                "trade_count": 25,
                "vwap": Decimal("10.20"),
                "quality_flags": ["complete"],
                "source": "fixture",
                "source_event_id": f"legacy-delivery-{revision}",
                "source_payload_hash": sha256_hex(("legacy-source", revision)),
                "correction_of_event_id": previous_event_id,
                "content_hash": normalized_hash,
                "created_at": received_at,
            }
            connection.execute(aqa_bar_events.insert().values(**values))
            events.append(values)
            previous_event_id = event_id
        if projected_revision is not None:
            projected = events[projected_revision - 1]
            connection.execute(
                aqa_bar_latest.insert().values(
                    bar_identity_id=identity_id,
                    bar_event_id=projected["bar_event_id"],
                    revision=projected_revision,
                    content_hash=projected["content_hash"],
                    version=projected_revision,
                    projected_at=projected["received_at"],
                )
            )
    return tuple(events)


def _insert_legacy_bar_history(
    connection: Connection,
    *,
    symbol: str,
    minute: int,
) -> tuple[BarIdentity, dict[str, Any]]:
    start_at = datetime(2026, 9, 5, 15, minute, tzinfo=UTC)
    end_at = start_at.replace(minute=minute + 1)
    identity = BarIdentity(
        provider="fixture",
        feed="iex",
        adjustment="raw",
        symbol=symbol,
        timeframe="1Min",
        start_at=start_at,
        end_at=end_at,
    )
    received_at = end_at.replace(second=1)
    normalized_hash = sha256_hex(
        (
            "canonical_bar_v1",
            1,
            identity.identity_hash,
            end_at,
            end_at,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.25"),
            Decimal("1000"),
            25,
            Decimal("10.20"),
            ("complete",),
        )
    )
    event_id = f"bar_event_{sha256_hex((identity.bar_identity_id, 1, normalized_hash))}"
    connection.execute(
        aqa_bar_identities.insert().values(
            bar_identity_id=identity.bar_identity_id,
            provider=identity.provider,
            feed=identity.feed,
            adjustment=identity.adjustment,
            symbol=identity.symbol,
            timeframe=identity.timeframe,
            start_at=identity.start_at,
            end_at=identity.end_at,
            content_hash=identity.identity_hash,
            created_at=received_at,
        )
    )
    event_values = {
        "bar_event_id": event_id,
        "bar_identity_id": identity.bar_identity_id,
        "revision": 1,
        "schema_version": 1,
        "provider_timestamp": end_at,
        "received_at": received_at,
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10.25"),
        "volume": Decimal("1000"),
        "trade_count": 25,
        "vwap": Decimal("10.20"),
        "quality_flags": ["complete"],
        "source": "fixture",
        "source_event_id": f"legacy-{symbol.lower()}",
        "source_payload_hash": sha256_hex(("legacy-source", symbol)),
        "correction_of_event_id": None,
        "content_hash": normalized_hash,
        "created_at": received_at,
    }
    connection.execute(aqa_bar_events.insert().values(**event_values))
    connection.execute(
        aqa_bar_latest.insert().values(
            bar_identity_id=identity.bar_identity_id,
            bar_event_id=event_id,
            revision=1,
            content_hash=normalized_hash,
            version=1,
            projected_at=received_at,
        )
    )
    return identity, event_values


def _wait_for_relation_lock(
    engine: Engine,
    *,
    mode: str,
    granted: bool,
    minimum_count: int = 1,
) -> None:
    deadline = monotonic() + 10
    statement = text(
        """
        SELECT count(*)
        FROM pg_locks AS lock
        JOIN pg_class AS relation ON relation.oid = lock.relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'aqa'
          AND relation.relname = 'aqa_bar_identities'
          AND lock.mode = :mode
          AND lock.granted = :granted
        """
    )
    while monotonic() < deadline:
        with engine.connect() as connection:
            count = connection.scalar(statement, {"mode": mode, "granted": granted})
        if type(count) is int and count >= minimum_count:
            return
    raise AssertionError(f"timed out waiting for {mode} relation lock")


@pytest.fixture
def empty_database() -> Iterator[str]:
    """Yield only the positively guarded disposable database with owned schemas absent."""

    _drop_disposable_test_schemas()
    try:
        yield _DATABASE_URL
    finally:
        _drop_disposable_test_schemas()


def test_upgrade_from_empty_database_creates_both_schemas(empty_database: str) -> None:
    upgrade_database(empty_database)
    engine = _engine(application_name="platform-migration-empty-test")
    try:
        current, expected = database_revision(empty_database)
        inspector = inspect(engine)
        assert current == expected == "20260905_0006"
        assert frozenset(inspector.get_table_names(schema=PLATFORM_SCHEMA)) == PLATFORM_TABLE_NAMES
        assert "collection_universes" in inspector.get_table_names(schema=COLLECTION_SCHEMA)
        assert _metadata_differences(engine) == []
    finally:
        engine.dispose()


def test_upgrade_from_prior_revision_preserves_collector_state(empty_database: str) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, _PRIOR_REVISION)
    engine = _engine(application_name="platform-migration-prior-test")
    legacy_hash = "1" * 64
    try:
        with engine.begin() as connection:
            connection.execute(
                collection_universes.insert().values(
                    universe_hash=legacy_hash,
                    schema_version="test-v1",
                    members=[],
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            legacy_rows = connection.scalar(
                select(func.count())
                .select_from(collection_universes)
                .where(collection_universes.c.universe_hash == legacy_hash)
            )
        assert legacy_rows == 1
        assert (
            frozenset(inspect(engine).get_table_names(schema=PLATFORM_SCHEMA))
            == PLATFORM_TABLE_NAMES
        )
    finally:
        engine.dispose()


def test_revision_three_backfills_existing_event_and_terminal_projection_hashes(
    empty_database: str,
) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "20260905_0002")
    engine = _engine(application_name="platform-migration-hash-backfill-test")
    try:
        legacy_events = _seed_legacy_bar_history(
            engine,
            revisions=2,
            projected_revision=2,
        )

        command.upgrade(config, "20260905_0003")

        with engine.connect() as connection:
            migrated_events = (
                connection.execute(
                    select(
                        aqa_bar_events.c.bar_event_id,
                        aqa_bar_events.c.lineage_hash,
                        aqa_bar_events.c.normalized_payload_hash,
                        aqa_bar_events.c.content_hash,
                    ).order_by(aqa_bar_events.c.revision)
                )
                .mappings()
                .all()
            )
            migrated_latest_hash = connection.scalar(select(aqa_bar_latest.c.content_hash))
        assert tuple(row["bar_event_id"] for row in migrated_events) == tuple(
            event["bar_event_id"] for event in legacy_events
        )
        assert all(
            row["content_hash"] != legacy["content_hash"]
            for row, legacy in zip(migrated_events, legacy_events, strict=True)
        )
        assert tuple(row["normalized_payload_hash"] for row in migrated_events) == tuple(
            event["content_hash"] for event in legacy_events
        )
        assert all(row["lineage_hash"] is None for row in migrated_events)
        assert migrated_latest_hash == migrated_events[-1]["content_hash"]
    finally:
        engine.dispose()


def test_revision_three_serializes_old_writers_and_fences_queued_clients(
    empty_database: str,
) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "20260905_0002")
    observer = _engine(application_name="platform-migration-lock-observer")
    held_ready = ThreadEvent()
    release_held_writer = ThreadEvent()

    def held_legacy_writer() -> tuple[BarIdentity, dict[str, Any]]:
        engine = _engine(application_name="platform-held-legacy-writer")
        try:
            with engine.begin() as connection:
                inserted = _insert_legacy_bar_history(
                    connection,
                    symbol="LOCKA",
                    minute=0,
                )
                held_ready.set()
                if not release_held_writer.wait(timeout=15):
                    raise AssertionError("timed out waiting to release held legacy writer")
                return inserted
        finally:
            engine.dispose()

    def migrate() -> None:
        command.upgrade(config, "20260905_0003")

    def queued_legacy_writer() -> None:
        engine = _engine(application_name="platform-queued-legacy-writer")
        try:
            with engine.begin() as connection:
                _insert_legacy_bar_history(
                    connection,
                    symbol="LOCKB",
                    minute=2,
                )
        finally:
            engine.dispose()

    executor = ThreadPoolExecutor(max_workers=3)
    try:
        held_future = executor.submit(held_legacy_writer)
        assert held_ready.wait(timeout=10)
        migration_future = executor.submit(migrate)
        _wait_for_relation_lock(
            observer,
            mode="AccessExclusiveLock",
            granted=False,
        )
        queued_future = executor.submit(queued_legacy_writer)
        _wait_for_relation_lock(
            observer,
            mode="RowExclusiveLock",
            granted=False,
        )

        release_held_writer.set()
        migrated_identity, legacy_event = held_future.result(timeout=20)
        migration_future.result(timeout=20)
        with pytest.raises(IntegrityError):
            queued_future.result(timeout=20)
    finally:
        release_held_writer.set()
        executor.shutdown(wait=True, cancel_futures=True)

    try:
        current, _ = database_revision(empty_database)
        assert current == "20260905_0003"
        with observer.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(aqa_bar_identities)
                    .where(aqa_bar_identities.c.symbol == "LOCKB")
                )
                == 0
            )

        # Validate the intermediate revision through its own schema. Current application
        # repositories intentionally target Alembic head and must not select columns that are
        # introduced by later revisions while this migration-lock proof is paused at 0003.
        with observer.connect() as connection:
            migrated_event = (
                connection.execute(
                    select(
                        aqa_bar_events.c.bar_event_id,
                        aqa_bar_events.c.bar_identity_id,
                        aqa_bar_events.c.revision,
                        aqa_bar_events.c.lineage_hash,
                        aqa_bar_events.c.normalized_payload_hash,
                        aqa_bar_events.c.correction_of_event_id,
                        aqa_bar_events.c.content_hash,
                    ).where(aqa_bar_events.c.bar_identity_id == migrated_identity.bar_identity_id)
                )
                .mappings()
                .one()
            )
            migrated_latest = (
                connection.execute(
                    select(
                        aqa_bar_latest.c.bar_event_id,
                        aqa_bar_latest.c.revision,
                        aqa_bar_latest.c.content_hash,
                        aqa_bar_latest.c.version,
                    ).where(aqa_bar_latest.c.bar_identity_id == migrated_identity.bar_identity_id)
                )
                .mappings()
                .one()
            )

        assert migrated_event["bar_event_id"] == legacy_event["bar_event_id"]
        assert migrated_event["revision"] == 1
        assert migrated_event["lineage_hash"] is None
        assert migrated_event["normalized_payload_hash"] == legacy_event["content_hash"]
        assert migrated_event["correction_of_event_id"] is None
        assert migrated_event["content_hash"] != migrated_event["normalized_payload_hash"]
        assert dict(migrated_latest) == {
            "bar_event_id": migrated_event["bar_event_id"],
            "revision": 1,
            "content_hash": migrated_event["content_hash"],
            "version": 1,
        }
    finally:
        observer.dispose()


@pytest.mark.parametrize(
    ("revisions", "projected_revision", "message"),
    [
        (1, None, "incomplete latest projections"),
        (2, 1, "malformed latest projection"),
    ],
)
def test_revision_three_rejects_missing_or_nonterminal_latest_projection(
    empty_database: str,
    revisions: int,
    projected_revision: int | None,
    message: str,
) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "20260905_0002")
    engine = _engine(application_name="platform-migration-invalid-projection-test")
    try:
        legacy_events = _seed_legacy_bar_history(
            engine,
            revisions=revisions,
            projected_revision=projected_revision,
        )

        with pytest.raises(RuntimeError, match=message):
            command.upgrade(config, "20260905_0003")

        current, _ = database_revision(empty_database)
        with engine.connect() as connection:
            retained_hashes = tuple(
                connection.scalars(
                    select(aqa_bar_events.c.content_hash).order_by(aqa_bar_events.c.revision)
                )
            )
        assert current == "20260905_0002"
        assert retained_hashes == tuple(event["content_hash"] for event in legacy_events)
    finally:
        engine.dispose()


def test_revision_three_rejects_identity_origin_timestamp_tampering(
    empty_database: str,
) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "20260905_0002")
    engine = _engine(application_name="platform-migration-identity-origin-test")
    try:
        legacy_events = _seed_legacy_bar_history(
            engine,
            revisions=2,
            projected_revision=2,
        )
        first_received_at = legacy_events[0]["received_at"]
        with engine.begin() as connection:
            connection.execute(
                aqa_bar_identities.update().values(
                    created_at=first_received_at + timedelta(seconds=1)
                )
            )

        with pytest.raises(RuntimeError, match="malformed durable state"):
            command.upgrade(config, "20260905_0003")

        current, _ = database_revision(empty_database)
        assert current == "20260905_0002"
    finally:
        engine.dispose()


def test_downgrade_refusal_preserves_platform_state(empty_database: str) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "head")
    engine = _engine(application_name="platform-migration-downgrade-test")
    experiment_hash = "2" * 64
    try:
        with engine.begin() as connection:
            connection.execute(
                aqa_experiments.insert().values(
                    experiment_hash=experiment_hash,
                    experiment_id="migration-test",
                    experiment_version=1,
                    schema_version=1,
                    configuration={},
                    content_hash="3" * 64,
                    registered_at=datetime(2026, 9, 5, tzinfo=UTC),
                )
            )

        with pytest.raises(RuntimeError, match="Destructive downgrade"):
            command.downgrade(config, _PRIOR_REVISION)

        current, expected = database_revision(empty_database)
        with engine.connect() as connection:
            retained_rows = connection.scalar(
                select(func.count())
                .select_from(aqa_experiments)
                .where(aqa_experiments.c.experiment_hash == experiment_hash)
            )
        assert current == expected == "20260905_0006"
        assert retained_rows == 1
        assert (
            frozenset(inspect(engine).get_table_names(schema=PLATFORM_SCHEMA))
            == PLATFORM_TABLE_NAMES
        )
    finally:
        engine.dispose()


def test_postgresql_rejects_nonfinite_market_and_execution_numerics(
    empty_database: str,
) -> None:
    upgrade_database(empty_database)
    engine = _engine(application_name="platform-migration-nonfinite-test")
    instant = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
    later = datetime(2026, 9, 5, 14, 31, tzinfo=UTC)
    digest = "1" * 64
    try:
        with engine.begin() as connection:
            connection.execute(
                aqa_experiments.insert().values(
                    experiment_hash=digest,
                    experiment_id="nonfinite-test",
                    experiment_version=1,
                    schema_version=1,
                    configuration={},
                    content_hash="2" * 64,
                    registered_at=instant,
                )
            )
            connection.execute(
                aqa_bar_identities.insert().values(
                    bar_identity_id="bar-identity",
                    provider="fixture",
                    feed="test",
                    adjustment="raw",
                    symbol="TEST",
                    timeframe="1Min",
                    start_at=instant,
                    end_at=later,
                    content_hash="3" * 64,
                    created_at=instant,
                )
            )
            connection.execute(
                aqa_decision_slots.insert().values(
                    slot_id="slot-1",
                    experiment_id="nonfinite-test",
                    experiment_version=1,
                    experiment_hash=digest,
                    signal_provider_id="fixture",
                    signal_provider_version="1",
                    session_date=instant.date(),
                    source_interval_start=instant - timedelta(minutes=1),
                    source_interval_end=instant,
                    decision_type="STRATEGY",
                    ready_at=instant,
                    deadline_at=later,
                    required_completion_at=later,
                    state="PENDING",
                    attempt_count=0,
                    correlation_id="correlation-1",
                    content_hash="4" * 64,
                    version=1,
                    created_at=instant,
                    updated_at=instant,
                )
            )
            connection.execute(
                aqa_signal_envelopes.insert().values(
                    signal_id="signal-1",
                    slot_id="slot-1",
                    experiment_hash=digest,
                    provider_id="fixture",
                    provider_version="1",
                    contract_version=1,
                    correlation_id="correlation-1",
                    provider_source_mode="builtin",
                    experiment_id="nonfinite-test",
                    experiment_version=1,
                    data_contract_hash="5" * 64,
                    policy_hash="6" * 64,
                    source_bar_end=instant,
                    created_at=instant,
                    expires_at=later,
                    active_symbols=["TEST"],
                    availability_mask=[True],
                    actions=["FLAT"],
                    expected_edge_bps=["0"],
                    proposed_signed_target_inputs=["0"],
                    promotable=False,
                    paper_submission_eligible=False,
                    content_hash="7" * 64,
                )
            )
            connection.execute(
                aqa_risk_decisions.insert().values(
                    risk_decision_id="risk-1",
                    slot_id="slot-1",
                    signal_id="signal-1",
                    experiment_hash=digest,
                    policy_id="fixture",
                    policy_version=1,
                    decided_at=instant,
                    input_hash="8" * 64,
                    proposed_targets={},
                    approved_targets={},
                    controls={},
                    reason_codes=[],
                    gross_exposure=Decimal("0"),
                    net_exposure=Decimal("0"),
                    cash_weight=Decimal("1"),
                    payload_hash="9" * 64,
                    signature="a" * 64,
                    content_hash="b" * 64,
                )
            )
            connection.execute(
                aqa_execution_plans.insert().values(
                    execution_plan_id="plan-1",
                    risk_decision_id="risk-1",
                    experiment_hash=digest,
                    target_version=1,
                    forced_flat=False,
                    targets={},
                    created_at=instant,
                    payload_hash="c" * 64,
                    signature="d" * 64,
                    content_hash="e" * 64,
                )
            )
            connection.execute(
                aqa_order_intents.insert().values(
                    order_intent_id="intent-valid",
                    execution_plan_id="plan-1",
                    client_order_id="client-valid",
                    symbol="TEST",
                    side="buy",
                    effect="open",
                    phase="entry",
                    sequence=0,
                    quantity=Decimal("1"),
                    notional=Decimal("10"),
                    reference_price=Decimal("10"),
                    order_type="market",
                    time_in_force="day",
                    created_at=instant,
                    payload_hash="f" * 64,
                    content_hash="0" * 64,
                )
            )
            connection.execute(
                aqa_broker_orders.insert().values(
                    client_order_id="client-valid",
                    state="planned",
                    updated_at=instant,
                    cumulative_filled_quantity=Decimal("0"),
                    last_event_sequence=0,
                    content_hash="1" * 64,
                    version=1,
                )
            )

        with pytest.raises(StatementError), engine.begin() as connection:
            connection.execute(
                aqa_bar_events.insert().values(
                    bar_event_id="bar-nan",
                    bar_identity_id="bar-identity",
                    revision=1,
                    schema_version=1,
                    received_at=instant,
                    open=Decimal("NaN"),
                    high=Decimal("NaN"),
                    low=Decimal("NaN"),
                    close=Decimal("NaN"),
                    volume=Decimal("NaN"),
                    quality_flags=[],
                    source="fixture",
                    source_payload_hash="2" * 64,
                    normalized_payload_hash="4" * 64,
                    content_hash="3" * 64,
                    created_at=instant,
                )
            )

        with pytest.raises(StatementError), engine.begin() as connection:
            connection.execute(
                aqa_order_intents.insert().values(
                    order_intent_id="intent-nan",
                    execution_plan_id="plan-1",
                    client_order_id="client-nan",
                    symbol="TEST",
                    side="buy",
                    effect="open",
                    phase="entry",
                    sequence=1,
                    quantity=Decimal("NaN"),
                    notional=Decimal("10"),
                    reference_price=Decimal("10"),
                    order_type="market",
                    time_in_force="day",
                    created_at=instant,
                    payload_hash="4" * 64,
                    content_hash="5" * 64,
                )
            )

        with pytest.raises(StatementError), engine.begin() as connection:
            connection.execute(
                aqa_fills.insert().values(
                    fill_id="fill-nan",
                    client_order_id="client-valid",
                    broker_execution_id="execution-nan",
                    symbol="TEST",
                    side="buy",
                    quantity=Decimal("NaN"),
                    price=Decimal("10"),
                    fee=Decimal("0"),
                    occurred_at=instant,
                    payload_hash="6" * 64,
                    content_hash="7" * 64,
                )
            )

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO aqa.aqa_bar_events (
                        bar_event_id, bar_identity_id, revision, schema_version,
                        received_at, open, high, low, close, volume,
                        quality_flags, source, source_payload_hash,
                        normalized_payload_hash, content_hash, created_at
                    ) VALUES (
                        'bar-database-nan', 'bar-identity', 1, 1,
                        :instant, CAST('NaN' AS NUMERIC), CAST('NaN' AS NUMERIC),
                        CAST('NaN' AS NUMERIC), CAST('NaN' AS NUMERIC),
                        CAST('NaN' AS NUMERIC), CAST('[]' AS JSONB), 'fixture',
                        :source_payload_hash, :normalized_payload_hash,
                        :content_hash, :instant
                    )
                    """
                ),
                {
                    "instant": instant,
                    "source_payload_hash": "8" * 64,
                    "normalized_payload_hash": "a" * 64,
                    "content_hash": "9" * 64,
                },
            )
    finally:
        engine.dispose()
