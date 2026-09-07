"""Disposable PostgreSQL proof of collector operational state and runtime privileges."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, delete, func, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.contracts import (
    MarketBarV1,
    RawBarObservationV1,
    RawBarObservationV2,
)
from adaptive_trader.collection.derived import DerivedDataProcessor
from adaptive_trader.collection.migrations import upgrade_database
from adaptive_trader.collection.operations import (
    collection_experiment,
    ensure_configuration,
    health_snapshot,
)
from adaptive_trader.collection.postgres import (
    PostgresMarketDataRepository,
    normalize_postgres_url,
    postgres_connect_args,
)
from adaptive_trader.collection.recovery import next_gap_repair, restore_canonical_projection
from adaptive_trader.collection.repository import CheckpointKey, CoverageAdvance
from adaptive_trader.collection.schema import (
    canonical_work,
    collector_checkpoints,
    collector_configuration,
    collector_events,
    collector_leases,
    ingestion_runs,
)
from adaptive_trader.collection.service import CollectorService
from adaptive_trader.collection.snapshots import freeze_collection_snapshot
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.datasets import DatasetValidationError
from adaptive_trader.platform.data.watermarks import DataSeries, GapRepository
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.market_data import BarIdentity, BarWrite, MarketDataRepository
from adaptive_trader.platform.storage.tables import (
    aqa_bar_events,
    aqa_bar_identities,
    aqa_basket_watermarks,
    aqa_data_gaps,
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

_NOW = datetime(2026, 9, 8, 14, 1, tzinfo=UTC)
_EXPECTED = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
_HISTORY_START = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
_RUN_ID = "11111111-1111-1111-1111-111111111111"
_LEASE_NAME = "market-data-collector.v1"


@dataclass(frozen=True)
class _Database:
    admin: Engine
    collector: Engine
    experiment: ExperimentDefinition


@pytest.fixture
def database(
    platform_migration_database_url: str,
    platform_login_database_urls: Mapping[str, str],
) -> Iterator[_Database]:
    """Reuse the guarded cluster-role fixture and reset only its disposable schemas."""

    admin = create_engine(
        _TEST_DATABASE,
        hide_parameters=True,
        connect_args=postgres_connect_args("operations-test-admin"),
    )
    collector: Engine | None = None
    try:
        with admin.begin() as connection:
            connection.execute(DropSchema("aqa", cascade=True, if_exists=True))
            connection.execute(DropSchema("market_data", cascade=True, if_exists=True))
        upgrade_database(platform_migration_database_url)
        collector = create_engine(
            normalize_postgres_url(platform_login_database_urls["aqa_collector"]),
            hide_parameters=True,
            connect_args=postgres_connect_args("operations-test-collector"),
        )
        experiment = collection_experiment()
        ExperimentRepository(admin).register(experiment, registered_at=_HISTORY_START)
        yield _Database(admin, collector, experiment)
    finally:
        if collector is not None:
            collector.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema("aqa", cascade=True, if_exists=True))
            connection.execute(DropSchema("market_data", cascade=True, if_exists=True))
        admin.dispose()


def _configuration(database: _Database) -> datetime:
    result = ensure_configuration(
        database.collector, experiment=database.experiment, history_start=_HISTORY_START, now=_NOW
    )
    assert isinstance(result, datetime)
    return result


def test_collect_once_restarts_from_postgresql_without_stream_or_runner_files(
    database: _Database, platform_login_database_urls: Mapping[str, str]
) -> None:
    _configuration(database)
    boundary = _HISTORY_START + timedelta(minutes=1)

    class Historical:
        def fetch(
            self,
            symbols: Sequence[str],
            *,
            start: datetime,
            end: datetime,
            source: str = "historical_backfill",
        ) -> tuple[RawBarObservationV1, ...]:
            assert start == _HISTORY_START and end == boundary
            return tuple(
                RawBarObservationV2(
                    bar=MarketBarV1(
                        provider="alpaca",
                        feed="IEX",
                        adjustment="raw",
                        symbol=symbol,
                        timeframe="1m",
                        bar_timestamp_utc=_HISTORY_START,
                        provider_event_timestamp_utc=_HISTORY_START,
                        receipt_timestamp_utc=boundary + timedelta(seconds=30),
                        open=Decimal("100"),
                        high=Decimal("102"),
                        low=Decimal("99"),
                        close=Decimal("101"),
                        volume=100,
                        trade_count=10,
                        vwap=Decimal("100"),
                        source=source,
                    ),
                    raw_payload_json=json.dumps({"fixture": "collect_once", "symbol": symbol}),
                )
                for symbol in symbols
            )

    clock_value = _NOW

    def clock() -> datetime:
        nonlocal clock_value
        clock_value += timedelta(microseconds=1)
        return clock_value

    def invoke() -> None:
        # A new repository and service model a fresh GitHub-hosted runner.
        repository = PostgresMarketDataRepository(
            platform_login_database_urls["aqa_collector"], canonical=True
        )
        try:
            service: CollectorService
            processor = DerivedDataProcessor(
                repository.engine,
                database.experiment,
                _HISTORY_START,
                clock=clock,
                lease_validator=lambda: service.validate_active_lease(),
                transaction_guard=lambda connection: repository.validate_ownership(
                    connection, lease=service.require_active_lease()
                ),
            )
            service = CollectorService(
                repository,
                Historical(),
                clock=lambda: boundary + timedelta(minutes=1),
                maintenance=processor.drain_backfill,
                prepare=lambda run_id: restore_canonical_projection(
                    repository,
                    current_lease=service.require_active_lease,
                    run_id=run_id,
                    history_start=_HISTORY_START,
                    now=boundary + timedelta(minutes=1),
                ),
            )
            service.collect_once(history_start=_HISTORY_START)
        finally:
            repository.close()

    invoke()
    invoke()
    with database.collector.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 29
        assert connection.scalar(select(func.count()).select_from(canonical_work)) == 0
        assert set(
            connection.execute(select(collector_checkpoints.c.committed_through_utc)).scalars()
        ) == {boundary}
        assert list(connection.execute(select(ingestion_runs.c.status)).scalars()) == [
            "completed",
            "completed",
        ]


def test_canonical_backfill_restart_and_correction_use_actual_collector_role(
    database: _Database, platform_login_database_urls: Mapping[str, str], tmp_path: Path
) -> None:
    """Exercise real leased intake, fenced derivation and a restart without provider access."""

    _configuration(database)
    boundary = _HISTORY_START + timedelta(minutes=15)

    class Historical:
        corrected = False

        def fetch(
            self,
            symbols: Sequence[str],
            *,
            start: datetime,
            end: datetime,
            source: str = "historical_backfill",
        ) -> tuple[RawBarObservationV1, ...]:
            assert set(symbols) == set(COLLECTION_UNIVERSE_V1.symbols)
            return tuple(
                RawBarObservationV2(
                    bar=MarketBarV1(
                        provider="alpaca",
                        feed="IEX",
                        adjustment="raw",
                        symbol=symbol,
                        timeframe="1m",
                        bar_timestamp_utc=instant,
                        provider_event_timestamp_utc=instant,
                        receipt_timestamp_utc=boundary + timedelta(seconds=30 + self.corrected),
                        open=Decimal("100"),
                        high=Decimal(
                            "105" if self.corrected and symbol == "AMD" and minute == 7 else "102"
                        ),
                        low=Decimal("99"),
                        close=Decimal("101"),
                        volume=100,
                        trade_count=10,
                        vwap=Decimal("100"),
                        source=source,
                    ),
                    raw_payload_json=json.dumps(
                        {
                            "fixture": "canonical_restart",
                            "symbol": symbol,
                            "minute": minute,
                            "corrected": self.corrected and symbol == "AMD" and minute == 7,
                        }
                    ),
                )
                for symbol in symbols
                for minute in range(15)
                if start <= (instant := _HISTORY_START + timedelta(minutes=minute)) < end
            )

    source = Historical()
    clock_value = _NOW

    def clock() -> datetime:
        nonlocal clock_value
        clock_value += timedelta(microseconds=1)
        return clock_value

    repository = PostgresMarketDataRepository(
        platform_login_database_urls["aqa_collector"],
        canonical=True,
    )
    try:

        def backfill() -> None:
            service: CollectorService
            processor = DerivedDataProcessor(
                repository.engine,
                database.experiment,
                _HISTORY_START,
                clock=clock,
                lease_validator=lambda: service.validate_active_lease(),
                transaction_guard=lambda connection: repository.validate_ownership(
                    connection, lease=service.require_active_lease()
                ),
            )
            service = CollectorService(
                repository,
                source,
                clock=lambda: _NOW,
                maintenance=processor.drain_backfill,
                prepare=lambda run_id: restore_canonical_projection(
                    repository,
                    current_lease=service.require_active_lease,
                    run_id=run_id,
                    history_start=_HISTORY_START,
                    now=_NOW,
                ),
            )
            service.backfill(start=_HISTORY_START, end=boundary)

        backfill()
        with database.collector.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(canonical_work)) == 0
            assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 446
            assert connection.scalar(select(func.count()).select_from(collector_checkpoints)) == 29
            basket = (
                connection.execute(
                    select(aqa_basket_watermarks).where(
                        aqa_basket_watermarks.c.role == "active",
                        aqa_basket_watermarks.c.timeframe == "15Min",
                    )
                )
                .mappings()
                .one()
            )
            assert basket["status"] == "ready"
            assert basket["contiguous_through"] == boundary
        backfill()
        with database.collector.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 446
        source.corrected = True
        backfill()
        market = MarketDataRepository(repository.engine)
        identity = BarIdentity("alpaca", "iex", "raw", "AMD", "15Min", _HISTORY_START, boundary)
        revisions = market.list_events(identity)
        assert len(revisions) == 2
        assert revisions[0].bar.high == Decimal("102")
        assert revisions[1].bar.high == Decimal("105")
        arguments: dict[str, Any] = dict(
            experiment=database.experiment,
            artifact_root=tmp_path / "snapshot-artifacts",
            range_start=_HISTORY_START,
            range_end=boundary,
            source_git_commit="1" * 40,
            dirty_worktree=True,
            uv_lock_hash="2" * 64,
            created_at=_NOW,
        )
        with pytest.raises(DatasetValidationError, match="diagnostic"):
            freeze_collection_snapshot(repository.engine, **arguments)
        frozen, registration = freeze_collection_snapshot(
            repository.engine, diagnostic=True, **arguments
        )
        assert frozen.promotable is False
        assert frozen.manifest["row_count"] == 165
        assert registration.created is True
        repeated, same_registration = freeze_collection_snapshot(
            repository.engine, diagnostic=True, **arguments
        )
        assert repeated.dataset_id == frozen.dataset_id
        assert repeated.manifest_bytes == frozen.manifest_bytes
        assert same_registration.created is False
        with database.collector.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 448
            assert connection.scalar(select(func.count()).select_from(canonical_work)) == 0
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(ingestion_runs)
                    .where(ingestion_runs.c.status == "completed")
                )
                == 3
            )
    finally:
        repository.close()


def test_legacy_empty_coverage_becomes_explicit_repairable_canonical_gap(
    database: _Database, platform_login_database_urls: Mapping[str, str]
) -> None:
    _configuration(database)
    repository = PostgresMarketDataRepository(platform_login_database_urls["aqa_collector"])
    repository.register_universe()
    lease = repository.try_acquire_lease(
        lease_name=_LEASE_NAME, holder_id="empty-migration", ttl_seconds=90
    )
    assert lease is not None
    boundary = _HISTORY_START + timedelta(minutes=15)
    try:
        run_id = repository.start_run(mode="backfill", lease=lease)
        repository.append_batch(
            (),
            lease=lease,
            coverage_advances=(
                CoverageAdvance(
                    CheckpointKey("rest_coverage", "alpaca", "IEX", "raw", "AMD", "1m"),
                    boundary,
                ),
            ),
        )
        with repository.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(canonical_work)) == 0
        restore_canonical_projection(
            repository,
            current_lease=lambda: lease,
            run_id=run_id,
            history_start=_HISTORY_START,
            now=_NOW,
        )
        with repository.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(canonical_work)) == 1
        clock_value = _NOW

        def clock() -> datetime:
            nonlocal clock_value
            clock_value += timedelta(microseconds=1)
            return clock_value

        def validate() -> None:
            with repository.engine.begin() as connection:
                repository.validate_ownership(connection, lease=lease)

        processor = DerivedDataProcessor(
            repository.engine,
            database.experiment,
            _HISTORY_START,
            clock=clock,
            lease_validator=validate,
            transaction_guard=lambda connection: repository.validate_ownership(
                connection, lease=lease
            ),
        )
        assert processor.drain_backfill() == 1
        assert processor.drain_backfill() == 0
        gaps = GapRepository(repository.engine, calendar=XnasExchangeCalendar()).list_unresolved(
            experiment_hash=database.experiment.content_hash,
            series=DataSeries("alpaca", "iex", "raw", "AMD", "1Min"),
        )
        assert len(gaps) == 1
        assert gaps[0].start_at == _HISTORY_START
        assert gaps[0].end_at == boundary
        repair = next_gap_repair(
            repository,
            experiment=database.experiment,
            history_start=_HISTORY_START,
            now=_NOW,
        )
        assert repair is not None and repair.gap_id == gaps[0].gap_id
        assert (
            repository.checkpoints(checkpoint_name="rest_coverage")["AMD"].committed_through_utc
            == boundary
        )
    finally:
        repository.release_lease(lease)
        repository.close()


def _seed_health(
    database: _Database,
    *,
    lease_name: str = _LEASE_NAME,
    include_bars: bool = True,
    substitute_tsla_feed: bool = False,
) -> None:
    _configuration(database)
    repository = PostgresMarketDataRepository(_DATABASE_URL)
    try:
        repository.register_universe()
    finally:
        repository.close()
    with database.admin.begin() as connection:
        connection.execute(
            insert(collector_leases).values(
                lease_name=lease_name,
                holder_id="operations-test",
                fencing_token=1,
                acquired_at=_NOW - timedelta(minutes=5),
                renewed_at=_NOW - timedelta(seconds=10),
                expires_at=_NOW + timedelta(minutes=1),
                metadata={},
            )
        )
        connection.execute(
            insert(ingestion_runs).values(
                run_id=_RUN_ID,
                universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
                mode="run",
                status="running",
                holder_id="operations-test",
                lease_name=lease_name,
                fencing_token=1,
                started_at=_NOW - timedelta(minutes=5),
                counters={},
            )
        )
        connection.execute(
            insert(collector_events).values(
                event_id="22222222-2222-2222-2222-222222222222",
                run_id=_RUN_ID,
                event_type="market_data_stream_subscribed",
                severity="info",
                occurred_at=_NOW - timedelta(seconds=2),
                details={},
            )
        )
        connection.execute(
            insert(collector_checkpoints),
            [
                dict(
                    checkpoint_name="rest_coverage",
                    provider="alpaca",
                    feed="SIP" if substitute_tsla_feed and symbol == "TSLA" else "IEX",
                    adjustment="raw",
                    symbol=symbol,
                    timeframe="1m",
                    committed_through_utc=_EXPECTED,
                    holder_id="operations-test",
                    fencing_token=1,
                )
                for symbol in COLLECTION_UNIVERSE_V1.symbols
            ],
        )
        connection.execute(
            insert(aqa_basket_watermarks).values(
                basket_watermark_id="operations_basket",
                experiment_hash=database.experiment.content_hash,
                role="active",
                timeframe="15Min",
                status="ready",
                contiguous_through=_EXPECTED,
                component_hash="a" * 64,
                content_hash="b" * 64,
                version=1,
                updated_at=_NOW,
            )
        )
    if include_bars:
        market_repository = MarketDataRepository(database.collector)
        for symbol in database.experiment.active_tradable:
            identity = BarIdentity(
                provider="alpaca",
                feed="iex",
                adjustment="raw",
                symbol=symbol,
                timeframe="1Min",
                start_at=_EXPECTED - timedelta(minutes=1),
                end_at=_EXPECTED,
            )
            market_repository.append(
                BarWrite(
                    identity=identity,
                    received_at=_EXPECTED + timedelta(seconds=30),
                    provider_timestamp=_EXPECTED,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=Decimal("1000"),
                    trade_count=20,
                    vwap=Decimal("100.25"),
                    quality_flags=("complete",),
                    source="alpaca",
                    source_event_id=f"health-{symbol}",
                    source_payload_hash="c" * 64,
                )
            )


def _health(database: _Database) -> dict[str, Any]:
    result = health_snapshot(database.collector, now=_NOW)
    assert isinstance(result, dict)
    return result


def test_initial_configuration_requires_history_and_restarts_preserve_it(
    database: _Database,
) -> None:
    with pytest.raises(ValueError, match="required for first activation"):
        ensure_configuration(database.collector, experiment=database.experiment, history_start=None)
    assert _configuration(database) == _HISTORY_START
    assert _configuration(database) == _HISTORY_START
    assert (
        ensure_configuration(database.collector, experiment=database.experiment, history_start=None)
        == _HISTORY_START
    )
    with database.collector.connect() as connection:
        rows = connection.execute(select(collector_configuration)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["universe_hash"] == COLLECTION_UNIVERSE_V1.universe_hash
    assert rows[0]["experiment_hash"] == database.experiment.content_hash


@pytest.mark.parametrize("offset", [-1, 1])
def test_history_drift_is_rejected_without_mutating_durable_scope(
    database: _Database, offset: int
) -> None:
    _configuration(database)
    with pytest.raises(ValueError, match="differs from durable history"):
        ensure_configuration(
            database.collector,
            experiment=database.experiment,
            history_start=_HISTORY_START + timedelta(days=offset),
        )
    assert (
        ensure_configuration(database.collector, experiment=database.experiment, history_start=None)
        == _HISTORY_START
    )


@pytest.mark.parametrize("column", ["universe_hash", "experiment_hash"])
def test_configuration_rejects_incompatible_durable_identity(
    database: _Database, column: str
) -> None:
    _configuration(database)
    with database.admin.begin() as connection:
        connection.execute(update(collector_configuration).values({column: "0" * 64}))
    with pytest.raises(ValueError, match="differs from durable history"):
        ensure_configuration(database.collector, experiment=database.experiment, history_start=None)


def test_collector_cannot_rewrite_or_delete_initial_scope(database: _Database) -> None:
    _configuration(database)
    for statement in (
        update(collector_configuration).values(history_start=_NOW),
        delete(collector_configuration),
    ):
        with database.collector.begin() as connection:
            with pytest.raises(DBAPIError) as error:
                connection.execute(statement)
            assert getattr(error.value.orig, "sqlstate", None) == "42501"


def test_empty_database_is_not_healthy_merely_because_it_is_reachable(database: _Database) -> None:
    result = _health(database)
    assert result["database_healthy"] is True
    assert result["service_ready"] is False
    assert result["research_ready"] is False
    assert result["active_runs"] == 0
    assert result["checkpoint_lag_symbols"] == sorted(COLLECTION_UNIVERSE_V1.symbols)


def test_complete_active_collector_is_fresh_through_the_expected_minute(
    database: _Database,
) -> None:
    _seed_health(database)
    result = _health(database)
    assert result["service_ready"] is True
    assert result["research_ready"] is True
    assert result["market_closed"] is False
    assert result["expected_completed_through"] == _EXPECTED.isoformat()
    assert result["universe_count"] == 29
    assert result["checkpoint_lag_symbols"] == []
    assert result["stale_research_symbols"] == []


def test_completed_one_shot_data_remains_ready_after_ownership_is_released(
    database: _Database,
) -> None:
    _seed_health(database)
    with database.admin.begin() as connection:
        connection.execute(
            update(ingestion_runs)
            .where(ingestion_runs.c.run_id == _RUN_ID)
            .values(mode="backfill", status="completed", completed_at=_NOW - timedelta(seconds=1))
        )
        connection.execute(update(collector_leases).values(expires_at=_NOW - timedelta(seconds=1)))
    result = _health(database)
    assert result["active_runs"] == 0
    assert result["service_ready"] is False
    assert result["subscribed"] is False
    assert result["coverage_ready"] is True
    assert result["research_ready"] is True
    assert result["last_invocation_status"] == "completed"
    assert result["last_invocation_completed_at"] == (_NOW - timedelta(seconds=1)).isoformat()


@pytest.mark.parametrize(
    "defect",
    [
        "expired_lease",
        "fencing_mismatch",
        "disconnected",
        "checkpoint_lag",
        "dirty_queue",
        "two_runs",
    ],
)
def test_operational_failure_blocks_service_readiness(database: _Database, defect: str) -> None:
    _seed_health(database, substitute_tsla_feed=defect == "checkpoint_lag")
    if defect == "checkpoint_lag":
        # Another feed cannot substitute for missing authoritative IEX coverage.
        result = _health(database)
        assert result["checkpoint_lag_symbols"] == ["TSLA"]
        assert result["service_ready"] is False
        return
    with database.admin.begin() as connection:
        if defect == "expired_lease":
            connection.execute(
                update(collector_leases).values(
                    renewed_at=_NOW - timedelta(minutes=1), expires_at=_NOW
                )
            )
        elif defect == "fencing_mismatch":
            connection.execute(update(collector_leases).values(fencing_token=2))
        elif defect == "disconnected":
            connection.execute(
                insert(collector_events).values(
                    event_id="33333333-3333-3333-3333-333333333333",
                    run_id=_RUN_ID,
                    event_type="market_data_stream_disconnected",
                    severity="warning",
                    occurred_at=_NOW,
                    details={},
                )
            )
        elif defect == "dirty_queue":
            connection.execute(
                insert(canonical_work).values(
                    symbol="AAOI", session_date=_NOW.date(), generation=1, through_at=_EXPECTED
                )
            )
        else:
            connection.execute(
                insert(ingestion_runs).values(
                    run_id="44444444-4444-4444-4444-444444444444",
                    universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
                    mode="run",
                    status="running",
                    holder_id="operations-test",
                    lease_name="other-collector",
                    fencing_token=1,
                    started_at=_NOW,
                    counters={},
                )
            )
            connection.execute(
                insert(collector_leases).values(
                    lease_name="other-collector",
                    holder_id="operations-test",
                    fencing_token=1,
                    acquired_at=_NOW,
                    renewed_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=1),
                    metadata={},
                )
            )
    assert _health(database)["service_ready"] is False


def test_noncanonical_lease_cannot_establish_production_readiness(database: _Database) -> None:
    _seed_health(database, lease_name="unrelated-lease")
    assert _health(database)["service_ready"] is False


def test_disconnect_tied_with_acknowledgement_fails_closed(database: _Database) -> None:
    _seed_health(database)
    with database.admin.begin() as connection:
        connection.execute(
            insert(collector_events).values(
                event_id="00000000-0000-0000-0000-000000000000",
                run_id=_RUN_ID,
                event_type="market_data_stream_disconnected",
                severity="warning",
                occurred_at=_NOW - timedelta(seconds=2),
                details={},
            )
        )
    assert _health(database)["subscribed"] is False


def test_research_readiness_requires_persisted_current_bars(database: _Database) -> None:
    _seed_health(database, include_bars=False)
    with database.admin.begin() as connection:
        connection.execute(
            insert(aqa_bar_identities),
            [
                dict(
                    bar_identity_id=f"orphan_{symbol}",
                    provider="alpaca",
                    feed="iex",
                    adjustment="raw",
                    symbol=symbol,
                    timeframe="1Min",
                    start_at=_EXPECTED - timedelta(minutes=1),
                    end_at=_EXPECTED,
                    content_hash="c" * 64,
                    created_at=_NOW,
                )
                for symbol in database.experiment.active_tradable
            ],
        )
    result = _health(database)
    assert result["service_ready"] is True
    assert result["research_ready"] is False
    assert result["stale_research_symbols"] == list(database.experiment.active_tradable)


@pytest.mark.parametrize(
    ("symbol", "provider", "blocks"),
    [("AAOI", "alpaca", True), ("TSLA", "alpaca", False), ("AAOI", "fixture", False)],
)
def test_only_required_canonical_gaps_block_research(
    database: _Database, symbol: str, provider: str, blocks: bool
) -> None:
    _seed_health(database)
    with database.admin.begin() as connection:
        connection.execute(
            insert(aqa_data_gaps).values(
                gap_id="health_gap",
                experiment_hash=database.experiment.content_hash,
                provider=provider,
                feed="iex",
                adjustment="raw",
                symbol=symbol,
                timeframe="1Min",
                gap_start_at=_HISTORY_START,
                gap_end_at=_HISTORY_START + timedelta(minutes=1),
                status="open",
                reason_code="missing_bar",
                attempt_count=0,
                detected_at=_NOW,
                content_hash="d" * 64,
                version=1,
            )
        )
    result = _health(database)
    assert result["service_ready"] is True
    assert result["research_ready"] is not blocks


def test_current_bars_without_aggregate_basket_remain_research_unready(database: _Database) -> None:
    _seed_health(database)
    with database.admin.begin() as connection:
        connection.execute(
            update(aqa_basket_watermarks).values(status="blocked", contiguous_through=None)
        )
    result = _health(database)
    assert result["service_ready"] is True
    assert result["aggregate_basket_ready"] is False
    assert result["research_ready"] is False


@pytest.mark.parametrize("start", [_NOW + timedelta(days=1), _HISTORY_START + timedelta(seconds=1)])
def test_invalid_first_history_start_cannot_poison_immutable_configuration(
    database: _Database, start: datetime
) -> None:
    with pytest.raises(ValueError, match="collection history start"):
        ensure_configuration(
            database.collector, experiment=database.experiment, history_start=start, now=_NOW
        )
    with database.collector.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(collector_configuration)) == 0
    assert _configuration(database) == _HISTORY_START


def test_old_gap_repair_uses_durable_cooldown_and_rotating_required_series(
    database: _Database, platform_login_database_urls: Mapping[str, str]
) -> None:
    _seed_health(database)
    with database.admin.begin() as connection:
        for gap_id, symbol, provider in (
            ("gap_a", "AMD", "alpaca"),
            ("gap_b", "NVDA", "alpaca"),
            ("gap_0", "AAOI", "fixture"),
            ("gap_c", "TSLA", "alpaca"),
        ):
            connection.execute(
                insert(aqa_data_gaps).values(
                    gap_id=gap_id,
                    experiment_hash=database.experiment.content_hash,
                    provider=provider,
                    feed="iex",
                    adjustment="raw",
                    symbol=symbol,
                    timeframe="1Min",
                    gap_start_at=_HISTORY_START,
                    gap_end_at=_HISTORY_START + timedelta(minutes=1),
                    status="open",
                    reason_code="missing_bar",
                    attempt_count=0,
                    detected_at=_NOW,
                    content_hash="d" * 64,
                    version=1,
                )
            )
    repository = PostgresMarketDataRepository(platform_login_database_urls["aqa_collector"])
    try:

        def next_at(now: datetime) -> str | None:
            window = next_gap_repair(
                repository, experiment=database.experiment, history_start=_HISTORY_START, now=now
            )
            if window is not None:
                assert window.start == _HISTORY_START
                assert window.end == _HISTORY_START + timedelta(minutes=1)
            return None if window is None else window.gap_id

        assert next_at(_NOW) == "gap_a"
        with database.admin.begin() as connection:
            connection.execute(
                insert(collector_events).values(
                    event_id="55555555-5555-5555-5555-555555555555",
                    run_id=_RUN_ID,
                    event_type="canonical_gap_reconciliation_started",
                    severity="info",
                    occurred_at=_NOW,
                    details={"gap_id": "gap_a"},
                )
            )
        assert next_at(_NOW + timedelta(minutes=4, seconds=59)) is None
        assert next_at(_NOW + timedelta(minutes=5)) == "gap_b"
        with database.admin.begin() as connection:
            connection.execute(
                insert(collector_events).values(
                    event_id="66666666-6666-6666-6666-666666666666",
                    run_id=_RUN_ID,
                    event_type="canonical_gap_reconciliation_started",
                    severity="info",
                    occurred_at=_NOW + timedelta(minutes=5),
                    details={"gap_id": "gap_b"},
                )
            )
        assert next_at(_NOW + timedelta(minutes=10)) == "gap_a"
    finally:
        repository.close()


def _data_snapshot(engine: Engine) -> dict[str, tuple[int, str]]:
    """Hash every synthetic row without returning provider payloads or connection details."""

    result: dict[str, tuple[int, str]] = {}
    quote = engine.dialect.identifier_preparer.quote_identifier
    with engine.connect() as connection:
        inspector = inspect(connection)
        for schema in ("aqa", "market_data"):
            for name in sorted(inspector.get_table_names(schema=schema)):
                statement = text(
                    "SELECT row_to_json(row_value)::text FROM "
                    f"{quote(schema)}.{quote(name)} AS row_value"
                )
                rows = sorted(connection.scalars(statement).all())
                encoded = json.dumps(rows, separators=(",", ":")).encode("utf-8")
                result[f"{schema}.{name}"] = (len(rows), hashlib.sha256(encoded).hexdigest())
    return result


def test_raw_canonical_and_checkpoint_state_survives_data_only_restore(
    database: _Database,
) -> None:
    """Use pg tools only in an explicitly identified disposable PostgreSQL container."""

    container = os.environ.get("APA_TEST_POSTGRES_CONTAINER", "")
    if not container or shutil.which("docker") is None:
        pytest.skip(
            "data-only restore requires an explicitly identified disposable Docker container"
        )

    def docker(arguments: list[str], *, payload: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["docker", *arguments], input=payload, capture_output=True, timeout=90, check=False
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return result.stdout

    published = docker(["port", container, "5432/tcp"]).decode("ascii").strip()
    assert published == f"127.0.0.1:{_TEST_DATABASE.port or 5432}"
    _configuration(database)
    repository = PostgresMarketDataRepository(_DATABASE_URL, canonical=True)
    lease = None
    try:
        repository.register_universe()
        lease = repository.try_acquire_lease(
            lease_name=_LEASE_NAME, holder_id="data-restore-fixture", ttl_seconds=120
        )
        assert lease is not None
        run_id = repository.start_run(mode="backfill", lease=lease)
        bar = MarketBarV1(
            provider="alpaca",
            feed="IEX",
            adjustment="raw",
            symbol="AAOI",
            timeframe="1m",
            bar_timestamp_utc=_HISTORY_START,
            provider_event_timestamp_utc=_HISTORY_START + timedelta(minutes=1),
            receipt_timestamp_utc=_HISTORY_START + timedelta(seconds=65),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1000,
            trade_count=20,
            vwap=Decimal("100.25"),
            quality_flags=frozenset(),
            source="historical_backfill",
        )
        observation = RawBarObservationV1(
            bar=bar,
            is_correction=False,
            raw_payload_json='{"fixture":"synthetic_data_restore","symbol":"AAOI"}',
        )
        repository.append_batch(
            (observation,),
            lease=lease,
            coverage_advances=(
                CoverageAdvance(
                    CheckpointKey("rest_coverage", "alpaca", "IEX", "raw", "AAOI", "1m"),
                    _HISTORY_START + timedelta(minutes=1),
                ),
            ),
        )
        repository.finish_run(run_id, lease=lease, status="completed", counters={"observations": 1})
    finally:
        if lease is not None:
            repository.release_lease(lease)
        repository.close()

    original = _data_snapshot(database.admin)
    for table in (
        "market_data.bar_observations",
        "market_data.current_bars",
        "market_data.collector_checkpoints",
        "market_data.collector_configuration",
        "market_data.canonical_work",
        "aqa.aqa_bar_events",
        "aqa.aqa_bar_latest",
    ):
        assert original[table][0] == 1
    dump = docker(
        [
            "exec",
            container,
            "pg_dump",
            "-U",
            _TEST_DATABASE.username or "",
            "-d",
            "collector_test",
            "--format=custom",
            "--schema=market_data",
            "--schema=aqa",
            "--no-owner",
            "--no-acl",
        ]
    )
    assert 0 < len(dump) < 20_000_000
    restored_database = "collector_restore_test"
    created = False
    restored_engine: Engine | None = None
    try:
        # createdb refuses an existing database; cleanup runs only for our successful creation.
        docker(
            ["exec", container, "createdb", "-U", _TEST_DATABASE.username or "", restored_database]
        )
        created = True
        docker(
            [
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                _TEST_DATABASE.username or "",
                "-d",
                restored_database,
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
            ],
            payload=dump,
        )
        restored_engine = create_engine(
            _TEST_DATABASE.set(database=restored_database),
            hide_parameters=True,
            connect_args=postgres_connect_args("operations-test-restore"),
        )
        assert _data_snapshot(restored_engine) == original
        print(
            json.dumps(
                {
                    "evidence": "SYNTHETIC_DATA_ONLY_RESTORE",
                    "table_count": len(original),
                    "row_count": sum(count for count, _ in original.values()),
                    "snapshot_sha256": hashlib.sha256(
                        json.dumps(original, sort_keys=True).encode()
                    ).hexdigest(),
                    "source_and_restore_identical": True,
                },
                sort_keys=True,
            )
        )
    finally:
        if restored_engine is not None:
            restored_engine.dispose()
        if created:
            docker(
                [
                    "exec",
                    container,
                    "dropdb",
                    "-U",
                    _TEST_DATABASE.username or "",
                    restored_database,
                ]
            )
