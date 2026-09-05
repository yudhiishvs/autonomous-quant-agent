"""Guarded PostgreSQL integration tests for the generic-platform migration."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
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
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    PLATFORM_TABLE_NAMES,
    aqa_bar_events,
    aqa_bar_identities,
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
        assert current == expected == "20260905_0002"
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
        assert current == expected == "20260905_0002"
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
                    experiment_hash=digest,
                    source_bar_end=instant,
                    decision_type="scheduled",
                    scheduled_at=instant,
                    data_deadline_at=later,
                    signal_deadline_at=later,
                    execution_deadline_at=later,
                    state="pending",
                    attempt_count=0,
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
                    schema_version=1,
                    source_bar_end=instant,
                    emitted_at=instant,
                    expires_at=later,
                    targets={},
                    reason_codes=[],
                    payload_hash="5" * 64,
                    signature="6" * 64,
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

        with pytest.raises(IntegrityError), engine.begin() as connection:
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
                    content_hash="3" * 64,
                    created_at=instant,
                )
            )

        with pytest.raises(IntegrityError), engine.begin() as connection:
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

        with pytest.raises(IntegrityError), engine.begin() as connection:
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
    finally:
        engine.dispose()
