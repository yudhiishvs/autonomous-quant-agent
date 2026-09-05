"""Structural and SQLite compatibility tests for the platform schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, create_engine, insert, select
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.schema import CheckConstraint, PrimaryKeyConstraint, UniqueConstraint

from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    PLATFORM_TABLE_NAMES,
    FiniteNumeric,
    UTCDateTime,
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_broker_orders,
    aqa_decision_slots,
    aqa_experiments,
    aqa_fills,
    aqa_jobs,
    aqa_outbox_events,
    aqa_signal_envelopes,
    metadata,
    platform_tables,
)

EXPECTED_TABLE_NAMES = frozenset(
    {
        "aqa_experiments",
        "aqa_experiment_symbols",
        "aqa_security_metadata_events",
        "aqa_bar_identities",
        "aqa_bar_events",
        "aqa_bar_latest",
        "aqa_data_gaps",
        "aqa_symbol_watermarks",
        "aqa_basket_watermarks",
        "aqa_dataset_manifests",
        "aqa_decision_slots",
        "aqa_signal_envelopes",
        "aqa_risk_latch_events",
        "aqa_risk_decisions",
        "aqa_execution_plans",
        "aqa_order_intents",
        "aqa_broker_orders",
        "aqa_order_events",
        "aqa_fills",
        "aqa_reconciliations",
        "aqa_incidents",
        "aqa_jobs",
        "aqa_job_attempts",
        "aqa_outbox_events",
        "aqa_audit_events",
    }
)


def _sqlite_platform_engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        execution_options={"schema_translate_map": {PLATFORM_SCHEMA: None}},
    )
    metadata.create_all(engine)
    return engine


def _unique_column_sets(table: Table) -> set[tuple[str, ...]]:
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
    }
    unique_sets.update((column.name,) for column in table.columns if column.unique)
    return unique_sets


def test_platform_schema_defines_exact_required_table_set() -> None:
    assert PLATFORM_SCHEMA == "aqa"
    assert PLATFORM_TABLE_NAMES == EXPECTED_TABLE_NAMES
    assert {table.name for table in platform_tables()} == EXPECTED_TABLE_NAMES


def test_every_table_has_concurrency_and_integrity_contracts() -> None:
    for table in platform_tables():
        assert table.primary_key.columns, table.name
        if table.info.get("append_only"):
            assert "content_hash" in table.c, table.name
        if table.info.get("state"):
            assert "version" in table.c or "sequence" in table.c, table.name

        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert isinstance(column.type, UTCDateTime), f"{table.name}.{column.name}"
                assert column.type.impl.timezone is True


def test_every_financial_numeric_column_has_a_nonfinite_database_guard() -> None:
    for table in platform_tables():
        numeric_columns = tuple(
            column.name for column in table.columns if isinstance(column.type, FiniteNumeric)
        )
        if not numeric_columns:
            continue
        finite_constraints = tuple(
            constraint
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name is not None
            and constraint.name.endswith("financial_values_finite")
        )
        assert len(finite_constraints) == 1, table.name
        expression = str(finite_constraints[0].sqltext)
        for column_name in numeric_columns:
            assert f"CAST({column_name} AS TEXT)" in expression, (
                f"{table.name}.{column_name} lacks a finite-value check"
            )


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        (
            aqa_bar_identities,
            ("provider", "feed", "adjustment", "symbol", "timeframe", "start_at"),
        ),
        (aqa_bar_events, ("bar_identity_id", "revision")),
        (aqa_bar_latest, ("bar_identity_id",)),
        (aqa_experiments, ("experiment_id", "experiment_version")),
        (
            aqa_decision_slots,
            ("experiment_hash", "source_interval_end", "decision_type"),
        ),
        (aqa_signal_envelopes, ("signal_id",)),
        (aqa_signal_envelopes, ("content_hash",)),
        (aqa_broker_orders, ("client_order_id",)),
        (aqa_fills, ("broker_execution_id",)),
        (aqa_jobs, ("job_type", "idempotency_key")),
        (aqa_outbox_events, ("outbox_event_id",)),
        (metadata.tables[f"{PLATFORM_SCHEMA}.aqa_audit_events"], ("stream_id", "sequence")),
    ],
)
def test_normative_uniqueness_contracts(table: Table, columns: tuple[str, ...]) -> None:
    assert columns in _unique_column_sets(table)


def test_bar_history_allows_a_prior_payload_to_become_effective_again() -> None:
    """Idempotency applies to the latest revision, not all historical payloads."""

    assert ("bar_identity_id", "content_hash") not in _unique_column_sets(aqa_bar_events)
    assert ("bar_identity_id", "normalized_payload_hash") not in _unique_column_sets(aqa_bar_events)
    assert aqa_bar_events.c.normalized_payload_hash.nullable is False
    assert aqa_bar_events.c.lineage_hash.nullable is True
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in aqa_bar_events.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert (
        constraints["ck_aqa_bar_events_event_hash_domain_separation"]
        == "content_hash <> normalized_payload_hash"
    )
    assert (
        constraints["ck_aqa_bar_events_lineage_hash_sha256_length"]
        == "lineage_hash IS NULL OR length(lineage_hash) = 64"
    )


def test_metadata_creates_cleanly_on_sqlite_without_postgresql_ddl() -> None:
    engine = _sqlite_platform_engine()
    try:
        with engine.connect() as connection:
            created = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).scalars()
            )
    finally:
        engine.dispose()

    assert created == EXPECTED_TABLE_NAMES


def test_database_rejects_duplicate_experiment_identity() -> None:
    engine = _sqlite_platform_engine()
    first = {
        "experiment_hash": "1" * 64,
        "experiment_id": "fixture",
        "experiment_version": 1,
        "schema_version": 1,
        "configuration": {"fixture": True},
        "content_hash": "2" * 64,
        "registered_at": datetime(2026, 9, 5, 12, tzinfo=UTC),
    }
    second = {
        **first,
        "experiment_hash": "3" * 64,
        "content_hash": "4" * 64,
    }
    try:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert(aqa_experiments), [first, second])
    finally:
        engine.dispose()


def test_utc_datetime_round_trips_sqlite_and_rejects_other_time_semantics() -> None:
    isolated_metadata = MetaData()
    events = Table(
        "events",
        isolated_metadata,
        Column("event_id", UTCDateTime(), primary_key=True),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    isolated_metadata.create_all(engine)
    instant = datetime(2026, 9, 5, 12, 30, 45, 123456, tzinfo=UTC)

    try:
        with engine.begin() as connection:
            connection.execute(insert(events).values(event_id=instant))
        with engine.connect() as connection:
            restored = connection.execute(select(events.c.event_id)).scalar_one()

        assert restored == instant
        assert restored.tzinfo is UTC

        for invalid in (
            instant.replace(tzinfo=None),
            instant.astimezone(timezone(timedelta(hours=-4))),
        ):
            with pytest.raises(StatementError) as captured, engine.begin() as connection:
                connection.execute(insert(events).values(event_id=invalid))
            assert isinstance(captured.value.orig, DomainValidationError)
    finally:
        engine.dispose()


def test_utc_datetime_normalizes_postgresql_session_offsets_on_read() -> None:
    stored = datetime(2026, 9, 5, 8, 30, tzinfo=timezone(timedelta(hours=-4)))

    restored = UTCDateTime().process_result_value(stored, postgresql_dialect())

    assert restored == datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
    assert restored is not None
    assert restored.tzinfo is UTC


def test_utc_datetime_rejects_naive_postgresql_results() -> None:
    with pytest.raises(DomainValidationError, match="database timestamp"):
        UTCDateTime().process_result_value(
            datetime(2026, 9, 5, 12, 30),
            postgresql_dialect(),
        )


def test_finite_numeric_rejects_nan_before_sqlite_can_coerce_it_to_null() -> None:
    isolated_metadata = MetaData()
    values = Table(
        "numeric_values",
        isolated_metadata,
        Column("value_id", Integer(), primary_key=True),
        Column("value", FiniteNumeric(), nullable=True),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    isolated_metadata.create_all(engine)
    finite_values = (
        Decimal("12.340000000000000000"),
        Decimal("-987654321.123456789012345678"),
        Decimal("0.000000000000000001"),
    )

    try:
        with pytest.raises(StatementError) as captured, engine.begin() as connection:
            connection.execute(insert(values).values(value_id=1, value=Decimal("NaN")))
        assert isinstance(captured.value.orig, DomainValidationError)

        with engine.begin() as connection:
            connection.execute(
                insert(values),
                [
                    {"value_id": index, "value": value}
                    for index, value in enumerate(finite_values, start=1)
                ],
            )
        with engine.connect() as connection:
            restored = tuple(
                connection.execute(select(values.c.value).order_by(values.c.value_id)).scalars()
            )
        assert restored == finite_values
        assert all(type(value) is Decimal for value in restored)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "column",
    [aqa_bar_events.c.vwap, aqa_broker_orders.c.average_fill_price],
)
def test_nullable_financial_columns_reject_explicit_nan(column: Column[Decimal]) -> None:
    assert isinstance(column.type, FiniteNumeric)
    with pytest.raises(DomainValidationError):
        column.type.process_bind_param(Decimal("NaN"), postgresql_dialect())


def test_finite_numeric_enforces_declared_precision_and_scale_without_rounding() -> None:
    price_type = FiniteNumeric(38, 18)
    maximum_exact = Decimal("99999999999999999999.123456789012345678")

    assert price_type.process_bind_param(maximum_exact, sqlite_dialect()) == format(
        maximum_exact,
        "f",
    )
    for invalid in (
        Decimal("100000000000000000000.000000000000000000"),
        Decimal("0.0000000000000000001"),
    ):
        with pytest.raises(DomainValidationError, match="precision or scale"):
            price_type.process_bind_param(invalid, sqlite_dialect())

    with pytest.raises(DomainValidationError, match="precision or scale"):
        FiniteNumeric(38, 0).process_bind_param(Decimal("1.1"), sqlite_dialect())
