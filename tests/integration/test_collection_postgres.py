"""PostgreSQL integration tests for the market-data persistence boundary.

These tests intentionally require a caller-provided, disposable PostgreSQL
database.  Supplying ``APA_TEST_POSTGRES_URL`` opts that database into schema
migration and cleanup by this module.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import create_engine, delete, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.contracts import MarketBarV1, RawBarObservationV1
from adaptive_trader.collection.migrations import (
    database_revision,
    upgrade_database,
)
from adaptive_trader.collection.postgres import (
    PostgresMarketDataRepository,
    normalize_postgres_url,
    postgres_connect_args,
)
from adaptive_trader.collection.repository import (
    CheckpointKey,
    CheckpointRegressionError,
    CoverageAdvance,
    LeaseLostError,
    LeaseToken,
    SchemaNotReadyError,
)
from adaptive_trader.collection.schema import (
    SCHEMA_NAME,
    bar_observations,
    collection_universes,
    collector_checkpoints,
    collector_leases,
    current_bars,
    ingestion_runs,
)
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1

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

_BASE = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
_PLATFORM_SCHEMA = "aqa"


def _drop_disposable_test_schemas() -> None:
    """Reset only the positively guarded schemas in the disposable test database."""

    engine = create_engine(
        _TEST_DATABASE,
        hide_parameters=True,
        connect_args=postgres_connect_args("collection-pg-test-reset", migration=True),
    )
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(_PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(SCHEMA_NAME, cascade=True, if_exists=True))
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    """Migrate a caller-designated disposable database from base to head."""

    _drop_disposable_test_schemas()
    upgrade_database(_DATABASE_URL)
    try:
        yield _DATABASE_URL
    finally:
        _drop_disposable_test_schemas()


@pytest.fixture
def repository(database_url: str) -> Iterator[PostgresMarketDataRepository]:
    selected = PostgresMarketDataRepository(database_url, application_name="collection-pg-tests")
    try:
        yield selected
    finally:
        selected.close()


@pytest.fixture
def mutation_lease(
    repository: PostgresMarketDataRepository,
    request: pytest.FixtureRequest,
) -> Iterator[LeaseToken]:
    token = repository.try_acquire_lease(
        lease_name=f"mutation-{request.node.name}",
        holder_id="integration-writer",
        ttl_seconds=120,
    )
    assert token is not None
    try:
        yield token
    finally:
        repository.release_lease(token)


def _observation(
    *,
    minute: int,
    close: str = "100",
    source: str = "iex_bar",
    is_correction: bool = False,
    receipt_offset_seconds: int = 65,
) -> RawBarObservationV1:
    timestamp = _BASE + timedelta(minutes=minute)
    close_value = Decimal(close)
    high = max(Decimal("101"), close_value)
    low = min(Decimal("99"), close_value)
    bar = MarketBarV1(
        provider="alpaca",
        feed="IEX",
        adjustment="raw",
        symbol="SPY",
        timeframe="1m",
        bar_timestamp_utc=timestamp,
        provider_event_timestamp_utc=timestamp + timedelta(seconds=receipt_offset_seconds - 1),
        receipt_timestamp_utc=timestamp + timedelta(seconds=receipt_offset_seconds),
        open=Decimal("100"),
        high=high,
        low=low,
        close=close_value,
        volume=1_000,
        trade_count=20,
        vwap=Decimal("100.25"),
        quality_flags=frozenset(),
        source=source,
    )
    return RawBarObservationV1(
        bar=bar,
        is_correction=is_correction,
        raw_payload_json=('{"close":"' + close + '","stream":"' + source + '","symbol":"SPY"}'),
    )


def test_migration_up_creates_the_complete_schema(
    database_url: str,
    repository: PostgresMarketDataRepository,
) -> None:
    current, expected = database_revision(database_url)

    assert current == expected
    repository.verify_schema()
    assert {
        "bar_observations",
        "collection_universes",
        "collector_checkpoints",
        "collector_events",
        "collector_leases",
        "current_bars",
        "data_gaps",
        "ingestion_runs",
    } <= set(inspect(repository.engine).get_table_names(schema=SCHEMA_NAME))


def test_schema_verification_fails_when_an_integrity_trigger_is_disabled(
    repository: PostgresMarketDataRepository,
) -> None:
    with repository.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE market_data.bar_observations "
                "DISABLE TRIGGER bar_observations_are_immutable"
            )
        )
    try:
        with pytest.raises(SchemaNotReadyError, match="integrity guards"):
            repository.verify_schema()
    finally:
        with repository.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE market_data.bar_observations "
                    "ENABLE TRIGGER bar_observations_are_immutable"
                )
            )
    repository.verify_schema()


def test_register_universe_and_run_lifecycle_are_idempotent_and_durable(
    repository: PostgresMarketDataRepository,
) -> None:
    repository.register_universe()
    repository.register_universe()
    lease = repository.try_acquire_lease(
        lease_name="integration-run-lifecycle",
        holder_id="integration-holder",
        ttl_seconds=30,
    )
    assert lease is not None
    run_id = repository.start_run(mode="stream", lease=lease)
    with repository.engine.begin() as connection:
        database_now = connection.scalar(select(func.statement_timestamp()))
        assert isinstance(database_now, datetime)
        connection.execute(
            update(collector_leases)
            .where(collector_leases.c.lease_name == lease.lease_name)
            .values(
                acquired_at=database_now - timedelta(minutes=3),
                renewed_at=database_now - timedelta(minutes=2),
                expires_at=database_now - timedelta(minutes=1),
            )
        )
    repository.finish_run(
        run_id,
        lease=lease,
        status="completed",
        counters={"bars": 2},
    )
    assert repository.release_lease(lease) is True

    with repository.engine.connect() as connection:
        universe_count = connection.scalar(
            select(func.count())
            .select_from(collection_universes)
            .where(collection_universes.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash)
        )
        run = (
            connection.execute(select(ingestion_runs).where(ingestion_runs.c.run_id == run_id))
            .mappings()
            .one()
        )

    assert universe_count == 1
    assert run["universe_hash"] == COLLECTION_UNIVERSE_V1.universe_hash
    assert run["lease_name"] == lease.lease_name
    assert run["fencing_token"] == lease.fencing_token
    assert run["status"] == "completed"
    assert run["counters"] == {"bars": 2}
    assert run["completed_at"].tzinfo is not None


def test_lease_takeover_supersedes_stale_run_and_correlates_readiness(
    repository: PostgresMarketDataRepository,
) -> None:
    repository.register_universe()
    lease_name = "integration-run-takeover"
    first = repository.try_acquire_lease(
        lease_name=lease_name,
        holder_id="collector-one",
        ttl_seconds=30,
    )
    assert first is not None
    first_run_id = repository.start_run(mode="run", lease=first)

    with repository.engine.begin() as connection:
        database_now = connection.scalar(select(func.statement_timestamp()))
        assert isinstance(database_now, datetime)
        connection.execute(
            update(collector_leases)
            .where(collector_leases.c.lease_name == lease_name)
            .values(
                acquired_at=database_now - timedelta(minutes=3),
                renewed_at=database_now - timedelta(minutes=2),
                expires_at=database_now - timedelta(minutes=1),
            )
        )

    second = repository.try_acquire_lease(
        lease_name=lease_name,
        holder_id="collector-two",
        ttl_seconds=30,
    )
    assert second is not None
    second_run_id = repository.start_run(mode="run", lease=second)
    with pytest.raises(LeaseLostError):
        repository.finish_run(
            first_run_id,
            lease=first,
            status="stopped",
            counters={"late": 1},
        )

    with repository.engine.connect() as connection:
        rows = {
            row["run_id"]: row
            for row in connection.execute(
                select(ingestion_runs).where(
                    ingestion_runs.c.run_id.in_((first_run_id, second_run_id))
                )
            ).mappings()
        }
    snapshot = repository.status()

    assert rows[first_run_id]["status"] == "failed"
    assert rows[first_run_id]["completed_at"] is not None
    assert rows[first_run_id]["error"] == "Superseded after collector lease takeover"
    assert rows[first_run_id]["counters"] == {}
    assert rows[second_run_id]["status"] == "running"
    assert rows[second_run_id]["holder_id"] == second.holder_id
    assert rows[second_run_id]["fencing_token"] == second.fencing_token
    assert snapshot.running_run_count == 1
    assert snapshot.active_lease_count == 1
    assert snapshot.active_run_count == 1
    assert repository.is_ready(lease_name=lease_name) is True
    assert repository.is_ready(lease_name="unrelated-collector") is False

    repository.finish_run(
        second_run_id,
        lease=second,
        status="stopped",
        counters={},
    )
    assert repository.is_ready(lease_name=lease_name) is False
    assert repository.release_lease(second) is True


def test_duplicate_observation_is_retry_idempotent(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    observation = _observation(minute=0)

    first = repository.append_batch((observation,), lease=mutation_lease)
    retried = repository.append_batch((observation,), lease=mutation_lease)

    assert first.observations_inserted == 1
    assert first.current_rows_inserted == 1
    assert retried.observations_inserted == 0
    assert retried.duplicates == 1
    with repository.engine.connect() as connection:
        observation_count = connection.scalar(
            select(func.count())
            .select_from(bar_observations)
            .where(bar_observations.c.observation_id == observation.observation_id)
        )
        current = (
            connection.execute(
                select(current_bars).where(
                    current_bars.c.identity_hash == observation.identity_hash
                )
            )
            .mappings()
            .one()
        )

    assert observation_count == 1
    assert current["revision"] == 1
    assert current["observation_count"] == 1
    assert current["current_observation_id"] == observation.observation_id


def test_correction_preserves_observations_and_revises_current_projection(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    original = _observation(minute=10, close="100")
    correction = _observation(
        minute=10,
        close="102",
        source="iex_updated_bar",
        is_correction=True,
        receipt_offset_seconds=75,
    )

    repository.append_batch((original,), lease=mutation_lease)
    corrected = repository.append_batch((correction,), lease=mutation_lease)

    assert corrected.observations_inserted == 1
    assert corrected.current_rows_revised == 1
    with repository.engine.connect() as connection:
        observations = connection.execute(
            select(
                bar_observations.c.observation_id,
                bar_observations.c.close,
                bar_observations.c.is_correction,
            )
            .where(bar_observations.c.identity_hash == original.identity_hash)
            .order_by(bar_observations.c.receipt_timestamp_utc)
        ).all()
        current = (
            connection.execute(
                select(current_bars).where(current_bars.c.identity_hash == original.identity_hash)
            )
            .mappings()
            .one()
        )

    assert observations == [
        (original.observation_id, Decimal("100.000000000000"), False),
        (correction.observation_id, Decimal("102.000000000000"), True),
    ]
    assert current["current_observation_id"] == correction.observation_id
    assert current["content_hash"] == correction.content_hash
    assert current["close"] == Decimal("102.000000000000")
    assert current["revision"] == 2
    assert current["observation_count"] == 2


def test_batched_projection_preserves_receipt_range_across_precedence(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    winning_older_receipt = _observation(
        minute=11,
        close="102",
        source="historical_reconciliation",
        receipt_offset_seconds=65,
    )
    losing_newer_receipt = _observation(
        minute=11,
        close="101",
        source="iex_bar",
        receipt_offset_seconds=120,
    )

    repository.append_batch(
        (losing_newer_receipt, winning_older_receipt),
        lease=mutation_lease,
    )

    with repository.engine.connect() as connection:
        current = (
            connection.execute(
                select(current_bars).where(
                    current_bars.c.identity_hash == winning_older_receipt.identity_hash
                )
            )
            .mappings()
            .one()
        )

    assert current["current_observation_id"] == winning_older_receipt.observation_id
    assert current["observation_count"] == 2
    assert current["first_observed_at"] == winning_older_receipt.bar.receipt_timestamp_utc
    assert current["last_observed_at"] == losing_newer_receipt.bar.receipt_timestamp_utc


def test_empty_coverage_advances_checkpoint_without_inventing_a_bar(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    key = CheckpointKey("historical", "alpaca", "IEX", "raw", "QQQ", "1m")
    first_boundary = _BASE + timedelta(hours=1)
    second_boundary = first_boundary + timedelta(hours=1)

    first = repository.append_batch(
        (),
        lease=mutation_lease,
        coverage_advances=(CoverageAdvance(key, first_boundary, {"empty": True}),),
    )
    second = repository.append_batch(
        (),
        lease=mutation_lease,
        coverage_advances=(CoverageAdvance(key, second_boundary, {"empty": True}),),
    )

    assert first.checkpoints_advanced == 1
    assert second.checkpoints_advanced == 1
    checkpoint = repository.checkpoints(checkpoint_name="historical")["QQQ"]
    assert checkpoint.committed_through_utc == second_boundary
    assert checkpoint.last_bar_timestamp_utc is None
    assert checkpoint.last_observation_id is None
    assert checkpoint.version == 2

    with pytest.raises(CheckpointRegressionError):
        repository.append_batch(
            (),
            lease=mutation_lease,
            coverage_advances=(CoverageAdvance(key, first_boundary, {"empty": True}),),
        )


def test_checkpoint_reader_ignores_other_series_with_the_same_name_and_symbol(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    checkpoint_name = "series-filter"
    symbol = "AMD"
    canonical = CheckpointKey(checkpoint_name, "alpaca", "IEX", "raw", symbol, "1m")
    unrelated = CheckpointKey(checkpoint_name, "other", "REPLAY", "split", symbol, "15m")

    repository.append_batch(
        (),
        lease=mutation_lease,
        coverage_advances=(
            CoverageAdvance(canonical, _BASE + timedelta(hours=1)),
            CoverageAdvance(unrelated, _BASE + timedelta(days=1)),
        ),
    )

    checkpoint = repository.checkpoints(checkpoint_name=checkpoint_name)[symbol]

    assert checkpoint.key == canonical
    assert checkpoint.committed_through_utc == _BASE + timedelta(hours=1)


def test_separate_coverage_advance_preserves_matching_last_bar_lineage(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    older = _observation(minute=30)
    newest = _observation(minute=31)
    repository.append_batch((older, newest), lease=mutation_lease)
    key = CheckpointKey("lineage", "alpaca", "IEX", "raw", "SPY", "1m")

    repository.append_batch(
        (),
        lease=mutation_lease,
        coverage_advances=(
            CoverageAdvance(
                key=key,
                committed_through_utc=_BASE + timedelta(minutes=32),
                last_bar_timestamp_utc=newest.bar.bar_timestamp_utc,
                last_observation_id=newest.observation_id,
            ),
        ),
    )
    repository.append_batch(
        (),
        lease=mutation_lease,
        coverage_advances=(
            CoverageAdvance(
                key=key,
                committed_through_utc=_BASE + timedelta(minutes=33),
                last_bar_timestamp_utc=older.bar.bar_timestamp_utc,
                last_observation_id=older.observation_id,
            ),
        ),
    )

    checkpoint = repository.checkpoints(checkpoint_name="lineage")["SPY"]
    assert checkpoint.committed_through_utc == _BASE + timedelta(minutes=33)
    assert checkpoint.last_bar_timestamp_utc == newest.bar.bar_timestamp_utc
    assert checkpoint.last_observation_id == newest.observation_id
    assert checkpoint.version == 2


def test_lease_is_mutually_exclusive_and_stale_tokens_are_fenced(
    repository: PostgresMarketDataRepository,
) -> None:
    missing_lease = cast(LeaseToken, None)
    with pytest.raises(LeaseLostError, match="valid collector lease"):
        repository.append_batch((), lease=missing_lease)
    with pytest.raises(LeaseLostError, match="valid collector lease"):
        repository.record_event(event_type="missing_lease_probe", lease=missing_lease)

    lease_name = "integration-singleton"
    first = repository.try_acquire_lease(
        lease_name=lease_name,
        holder_id="collector-one",
        ttl_seconds=10,
    )

    assert first is not None
    assert (
        repository.try_acquire_lease(
            lease_name=lease_name,
            holder_id="collector-two",
            ttl_seconds=10,
        )
        is None
    )

    with repository.engine.begin() as connection:
        database_now = connection.scalar(select(func.statement_timestamp()))
        assert isinstance(database_now, datetime)
        connection.execute(
            update(collector_leases)
            .where(collector_leases.c.lease_name == lease_name)
            .values(
                acquired_at=database_now - timedelta(minutes=3),
                renewed_at=database_now - timedelta(minutes=2),
                expires_at=database_now - timedelta(minutes=1),
            )
        )

    second = repository.try_acquire_lease(
        lease_name=lease_name,
        holder_id="collector-two",
        ttl_seconds=10,
    )
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1

    with pytest.raises(LeaseLostError):
        repository.append_batch((), lease=first)
    with pytest.raises(LeaseLostError):
        repository.record_event(event_type="stale_lease_probe", lease=first)
    with pytest.raises(LeaseLostError):
        repository.start_run(mode="run", lease=first)
    assert repository.append_batch((), lease=second).received == 0
    assert repository.record_event(event_type="valid_lease_probe", lease=second)


def test_bar_observation_append_only_trigger_rejects_update_and_delete(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    observation = _observation(minute=20)
    repository.append_batch((observation,), lease=mutation_lease)

    statements = (
        update(bar_observations)
        .where(bar_observations.c.observation_id == observation.observation_id)
        .values(close=Decimal("999")),
        delete(bar_observations).where(
            bar_observations.c.observation_id == observation.observation_id
        ),
    )
    for statement in statements:
        with pytest.raises(DBAPIError) as exc_info, repository.engine.begin() as connection:
            connection.execute(statement)
        assert "bar observations are append-only" in str(exc_info.value.orig)

    with repository.engine.connect() as connection:
        stored_close = connection.scalar(
            select(bar_observations.c.close).where(
                bar_observations.c.observation_id == observation.observation_id
            )
        )
    assert stored_close == Decimal("100.000000000000")

    with pytest.raises(DBAPIError) as exc_info, repository.engine.begin() as connection:
        connection.execute(text("TRUNCATE market_data.bar_observations CASCADE"))
    assert "bar observations are append-only" in str(exc_info.value.orig)


def test_checkpoint_guards_reject_delete_and_truncate(
    repository: PostgresMarketDataRepository,
    mutation_lease: LeaseToken,
) -> None:
    key = CheckpointKey("protected", "alpaca", "IEX", "raw", "SPY", "1m")
    repository.append_batch(
        (),
        lease=mutation_lease,
        coverage_advances=(CoverageAdvance(key, _BASE + timedelta(hours=1)),),
    )

    statements = (
        delete(collector_checkpoints).where(collector_checkpoints.c.checkpoint_name == "protected"),
        text("TRUNCATE market_data.collector_checkpoints"),
    )
    for statement in statements:
        with pytest.raises(DBAPIError) as exc_info, repository.engine.begin() as connection:
            connection.execute(statement)
        assert "collector checkpoints cannot be removed" in str(exc_info.value.orig)
