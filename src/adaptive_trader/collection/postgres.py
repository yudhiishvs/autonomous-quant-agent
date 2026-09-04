"""PostgreSQL implementation of the market-data persistence contract."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, inspect, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL, Connection, Engine, create_engine, make_url

from adaptive_trader.collection.contracts import RawBarObservationV1
from adaptive_trader.collection.repository import (
    BatchResult,
    Checkpoint,
    CheckpointKey,
    CheckpointRegressionError,
    CollectionPersistenceError,
    CollectorStatus,
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
    collector_events,
    collector_leases,
    current_bars,
    data_gaps,
    ingestion_runs,
)
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1

_SOURCE_PRECEDENCE = {
    "iex_bar": 10,
    "historical_backfill": 20,
    "iex_updated_bar": 30,
    "historical_reconciliation": 40,
}

_LOCAL_DATABASE_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_CONNECTION_ROUTING_QUERY_KEYS = frozenset(
    {"dbname", "host", "hostaddr", "password", "port", "service", "servicefile", "user"}
)


def postgres_connect_args(
    application_name: str,
    *,
    migration: bool = False,
) -> dict[str, str | int]:
    """Return bounded psycopg settings shared by runtime and migration clients."""

    if not application_name.strip():
        raise ValueError("application_name cannot be empty")
    statement_timeout = 300_000 if migration else 20_000
    lock_timeout = 15_000 if migration else 5_000
    idle_timeout = 300_000 if migration else 30_000
    return {
        "application_name": application_name,
        "connect_timeout": 5,
        "options": (
            f"-c statement_timeout={statement_timeout} "
            f"-c lock_timeout={lock_timeout} "
            f"-c idle_in_transaction_session_timeout={idle_timeout}"
        ),
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value.astimezone(UTC)


def normalize_postgres_url(value: str) -> URL:
    """Return a psycopg3 SQLAlchemy URL without exposing credentials in errors."""

    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError("APA_MARKET_DATA_DATABASE_URL must be a valid PostgreSQL URL") from exc
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("APA_MARKET_DATA_DATABASE_URL must use PostgreSQL with psycopg")
    if not url.database or not url.host:
        raise ValueError("APA_MARKET_DATA_DATABASE_URL must include a host and database")
    routing_overrides = sorted(
        str(key) for key in url.query if str(key).lower() in _CONNECTION_ROUTING_QUERY_KEYS
    )
    if routing_overrides:
        raise ValueError(
            "APA_MARKET_DATA_DATABASE_URL cannot override connection routing in query parameters"
        )
    host = url.host.lower().rstrip(".")
    ssl_mode = url.query.get("sslmode")
    if host not in _LOCAL_DATABASE_HOSTS and ssl_mode != "verify-full":
        raise ValueError(
            "APA_MARKET_DATA_DATABASE_URL must use sslmode=verify-full for a "
            "non-loopback PostgreSQL host"
        )
    return url


def _source_precedence(source: str) -> int:
    return _SOURCE_PRECEDENCE.get(source, 0)


def _observation_row(observation: RawBarObservationV1) -> dict[str, Any]:
    bar = observation.bar
    raw_payload = (
        None if observation.raw_payload_json is None else json.loads(observation.raw_payload_json)
    )
    return {
        "observation_id": observation.observation_id,
        "schema_version": observation.SCHEMA_VERSION,
        "identity_hash": observation.identity_hash,
        "content_hash": observation.content_hash,
        "provider": bar.provider,
        "feed": bar.feed,
        "adjustment": bar.adjustment,
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "bar_timestamp_utc": bar.bar_timestamp_utc,
        "provider_event_timestamp_utc": bar.provider_event_timestamp_utc,
        "receipt_timestamp_utc": bar.receipt_timestamp_utc,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "trade_count": bar.trade_count,
        "vwap": bar.vwap,
        "quality_flags": sorted(bar.quality_flags),
        "source": bar.source,
        "source_precedence": _source_precedence(bar.source),
        "is_correction": observation.is_correction,
        "provider_event_id": observation.provider_event_id,
        "raw_payload_sha256": observation.raw_payload_sha256,
        "raw_payload": raw_payload,
    }


def _projection_row(
    row: Mapping[str, Any],
    *,
    observation_count: int = 1,
    first_observed_at: datetime | None = None,
    last_observed_at: datetime | None = None,
) -> dict[str, Any]:
    projected = {
        key: row[key]
        for key in (
            "identity_hash",
            "content_hash",
            "schema_version",
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            "bar_timestamp_utc",
            "provider_event_timestamp_utc",
            "receipt_timestamp_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
            "quality_flags",
            "source",
            "source_precedence",
            "is_correction",
        )
    }
    projected.update(
        {
            "current_observation_id": row["observation_id"],
            "revision": 1,
            "observation_count": observation_count,
            "first_observed_at": first_observed_at or row["receipt_timestamp_utc"],
            "last_observed_at": last_observed_at or row["receipt_timestamp_utc"],
        }
    )
    return projected


class PostgresMarketDataRepository:
    """Append-only observations plus a deterministic current-value projection."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        application_name: str = "adaptive-market-data",
    ) -> None:
        url = normalize_postgres_url(database_url)
        self.engine: Engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max(2, pool_size),
            pool_timeout=5,
            hide_parameters=True,
            connect_args=postgres_connect_args(application_name),
        )

    def verify_schema(self) -> None:
        try:
            with self.engine.connect() as connection:
                inspector = inspect(connection)
                available = set(inspector.get_table_names(schema=SCHEMA_NAME))
                indexes = inspector.get_indexes("ingestion_runs", schema=SCHEMA_NAME)
                active_run_index_ready = any(
                    item.get("name") == "uq_ingestion_runs_active_lease"
                    and item.get("unique") is True
                    for item in indexes
                )
                trigger_rows = connection.execute(
                    text(
                        """
                        SELECT c.relname, t.tgname
                        FROM pg_catalog.pg_trigger AS t
                        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                        WHERE n.nspname = :schema_name
                          AND NOT t.tgisinternal
                          AND t.tgenabled <> 'D'
                        """
                    ),
                    {"schema_name": SCHEMA_NAME},
                ).all()
                available_triggers = {(str(table), str(trigger)) for table, trigger in trigger_rows}
            required = {
                "bar_observations",
                "collection_universes",
                "collector_checkpoints",
                "collector_events",
                "collector_leases",
                "current_bars",
                "data_gaps",
                "ingestion_runs",
            }
        except Exception as exc:
            raise CollectionPersistenceError(
                "Unable to connect to the market-data database"
            ) from exc
        missing = sorted(required - available)
        if missing:
            raise SchemaNotReadyError(
                "Market-data database migrations are not current; missing tables: "
                + ", ".join(missing)
            )
        required_triggers = {
            ("bar_observations", "bar_observations_are_immutable"),
            ("bar_observations", "bar_observations_reject_truncate"),
            ("collector_checkpoints", "collector_checkpoints_are_monotonic"),
            ("collector_checkpoints", "collector_checkpoints_reject_delete"),
            ("collector_checkpoints", "collector_checkpoints_reject_truncate"),
        }
        missing_triggers = sorted(required_triggers - available_triggers)
        if missing_triggers or not active_run_index_ready:
            raise SchemaNotReadyError(
                "Market-data database integrity guards are missing or disabled"
            )

    def register_universe(self) -> None:
        members = [
            {
                "symbol": member.symbol,
                "company_name": member.company_name,
                "role": member.role.value,
                "execution_authorized": member.execution_authorized,
            }
            for member in COLLECTION_UNIVERSE_V1.members
        ]
        statement = pg_insert(collection_universes).values(
            universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
            schema_version=COLLECTION_UNIVERSE_V1.SCHEMA_VERSION,
            members=members,
        )
        statement = statement.on_conflict_do_nothing(
            index_elements=[collection_universes.c.universe_hash]
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def start_run(self, *, mode: str, lease: LeaseToken) -> str:
        if mode not in {"backfill", "stream", "run"}:
            raise ValueError(f"Unsupported collection mode: {mode!r}")
        run_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            self._verify_lease(connection, lease)
            connection.execute(
                update(ingestion_runs)
                .where(
                    ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                    ingestion_runs.c.lease_name == lease.lease_name,
                    ingestion_runs.c.status == "running",
                )
                .values(
                    status="failed",
                    completed_at=func.statement_timestamp(),
                    error="Superseded after collector lease takeover",
                )
            )
            connection.execute(
                ingestion_runs.insert().values(
                    run_id=run_id,
                    universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
                    mode=mode,
                    status="running",
                    holder_id=lease.holder_id,
                    lease_name=lease.lease_name,
                    fencing_token=lease.fencing_token,
                    counters={},
                )
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        lease: LeaseToken,
        status: str,
        counters: Mapping[str, int],
        error: str | None = None,
    ) -> None:
        if status not in {"completed", "failed", "stopped"}:
            raise ValueError(f"Unsupported completed run status: {status!r}")
        if lease is None:
            raise LeaseLostError("A valid collector lease is required to finalize an ingestion run")
        safe_error = None if error is None else error[:4000]
        with self.engine.begin() as connection:
            result = connection.execute(
                update(ingestion_runs)
                .where(
                    ingestion_runs.c.run_id == run_id,
                    ingestion_runs.c.status == "running",
                    ingestion_runs.c.lease_name == lease.lease_name,
                    ingestion_runs.c.holder_id == lease.holder_id,
                    ingestion_runs.c.fencing_token == lease.fencing_token,
                    select(collector_leases.c.lease_name)
                    .where(
                        collector_leases.c.lease_name == lease.lease_name,
                        collector_leases.c.holder_id == lease.holder_id,
                        collector_leases.c.fencing_token == lease.fencing_token,
                    )
                    .exists(),
                )
                .values(
                    status=status,
                    completed_at=func.now(),
                    counters=dict(counters),
                    error=safe_error,
                )
            )
            if result.rowcount == 1:
                return
            existing = connection.execute(
                select(
                    ingestion_runs.c.status,
                    ingestion_runs.c.lease_name,
                    ingestion_runs.c.holder_id,
                    ingestion_runs.c.fencing_token,
                ).where(ingestion_runs.c.run_id == run_id)
            ).one_or_none()
            if existing is None:
                raise CollectionPersistenceError(f"Unknown ingestion run: {run_id}")
            current_lease = connection.scalar(
                select(collector_leases.c.lease_name).where(
                    collector_leases.c.lease_name == lease.lease_name,
                    collector_leases.c.holder_id == lease.holder_id,
                    collector_leases.c.fencing_token == lease.fencing_token,
                )
            )
            owns_run = (
                existing.lease_name,
                existing.holder_id,
                existing.fencing_token,
            ) == (lease.lease_name, lease.holder_id, lease.fencing_token)
            if current_lease is None or not owns_run or existing.status == "running":
                raise LeaseLostError("The ingestion run belongs to another collector lease")

    @staticmethod
    def _verify_lease(connection: Connection, token: LeaseToken) -> None:
        if token is None:
            raise LeaseLostError("A valid collector lease is required for database mutations")
        valid = connection.execute(
            select(collector_leases.c.fencing_token)
            .where(
                collector_leases.c.lease_name == token.lease_name,
                collector_leases.c.holder_id == token.holder_id,
                collector_leases.c.fencing_token == token.fencing_token,
                collector_leases.c.expires_at > func.statement_timestamp(),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if valid is None:
            raise LeaseLostError("The collector lease expired or was taken over")

    @staticmethod
    def _upsert_projections(connection: Connection, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["identity_hash"])].append(row)
        candidates: list[dict[str, Any]] = []
        for identity_rows in grouped.values():
            ordered = sorted(
                identity_rows,
                key=lambda row: (
                    int(row["source_precedence"]),
                    bool(row["is_correction"]),
                    row["provider_event_timestamp_utc"] or row["receipt_timestamp_utc"],
                    row["receipt_timestamp_utc"],
                    row["observation_id"],
                ),
            )
            receipt_times = [row["receipt_timestamp_utc"] for row in identity_rows]
            candidates.append(
                _projection_row(
                    ordered[-1],
                    observation_count=len(ordered),
                    first_observed_at=min(receipt_times),
                    last_observed_at=max(receipt_times),
                )
            )

        statement = pg_insert(current_bars).values(candidates)
        excluded = statement.excluded
        candidate_wins = tuple_(
            excluded.source_precedence,
            excluded.is_correction,
            func.coalesce(excluded.provider_event_timestamp_utc, excluded.receipt_timestamp_utc),
            excluded.receipt_timestamp_utc,
            excluded.current_observation_id,
        ) > tuple_(
            current_bars.c.source_precedence,
            current_bars.c.is_correction,
            func.coalesce(
                current_bars.c.provider_event_timestamp_utc,
                current_bars.c.receipt_timestamp_utc,
            ),
            current_bars.c.receipt_timestamp_utc,
            current_bars.c.current_observation_id,
        )
        content_changed = current_bars.c.content_hash != excluded.content_hash
        mutable_projection_fields = (
            "current_observation_id",
            "content_hash",
            "schema_version",
            "provider_event_timestamp_utc",
            "receipt_timestamp_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
            "quality_flags",
            "source",
            "source_precedence",
            "is_correction",
        )
        update_values: dict[str, Any] = {
            field_name: case(
                (candidate_wins, getattr(excluded, field_name)),
                else_=getattr(current_bars.c, field_name),
            )
            for field_name in mutable_projection_fields
        }
        update_values.update(
            {
                "revision": current_bars.c.revision
                + case((and_(candidate_wins, content_changed), 1), else_=0),
                "observation_count": current_bars.c.observation_count + excluded.observation_count,
                "last_observed_at": func.greatest(
                    current_bars.c.last_observed_at,
                    excluded.last_observed_at,
                ),
                "projected_at": func.statement_timestamp(),
            }
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[current_bars.c.identity_hash],
                set_=update_values,
            )
        )

    @staticmethod
    def _advance_checkpoints(
        connection: Connection,
        advances: Sequence[CoverageAdvance],
        observations: Sequence[RawBarObservationV1],
        token: LeaseToken,
    ) -> int:
        if not advances:
            return 0
        advance_keys = [
            (
                advance.key.checkpoint_name,
                advance.key.provider,
                advance.key.feed,
                advance.key.adjustment,
                advance.key.symbol,
                advance.key.timeframe,
            )
            for advance in advances
        ]
        if len(set(advance_keys)) != len(advance_keys):
            raise ValueError("A batch cannot advance the same checkpoint more than once")
        existing_checkpoints = {
            (
                str(row["checkpoint_name"]),
                str(row["provider"]),
                str(row["feed"]),
                str(row["adjustment"]),
                str(row["symbol"]),
                str(row["timeframe"]),
            ): row["committed_through_utc"]
            for row in connection.execute(
                select(
                    collector_checkpoints.c.checkpoint_name,
                    collector_checkpoints.c.provider,
                    collector_checkpoints.c.feed,
                    collector_checkpoints.c.adjustment,
                    collector_checkpoints.c.symbol,
                    collector_checkpoints.c.timeframe,
                    collector_checkpoints.c.committed_through_utc,
                )
                .where(
                    tuple_(
                        collector_checkpoints.c.checkpoint_name,
                        collector_checkpoints.c.provider,
                        collector_checkpoints.c.feed,
                        collector_checkpoints.c.adjustment,
                        collector_checkpoints.c.symbol,
                        collector_checkpoints.c.timeframe,
                    ).in_(advance_keys)
                )
                .with_for_update()
            ).mappings()
        }
        for key, advance in zip(advance_keys, advances, strict=True):
            existing_boundary = existing_checkpoints.get(key)
            if existing_boundary is not None and _utc(advance.committed_through_utc) < _utc(
                existing_boundary
            ):
                raise CheckpointRegressionError("A coverage checkpoint would move backward")

        observations_by_series: dict[tuple[str, str, str, str, str], list[RawBarObservationV1]] = (
            defaultdict(list)
        )
        for observation in observations:
            bar = observation.bar
            observations_by_series[
                (bar.provider, bar.feed, bar.adjustment, bar.symbol, bar.timeframe)
            ].append(observation)

        explicit_ids = {
            advance.last_observation_id
            for advance in advances
            if advance.last_observation_id is not None
        }
        explicit_observations = {
            str(row["observation_id"]): row
            for row in connection.execute(
                select(
                    bar_observations.c.observation_id,
                    bar_observations.c.provider,
                    bar_observations.c.feed,
                    bar_observations.c.adjustment,
                    bar_observations.c.symbol,
                    bar_observations.c.timeframe,
                    bar_observations.c.bar_timestamp_utc,
                ).where(bar_observations.c.observation_id.in_(explicit_ids))
            ).mappings()
        }

        rows: list[dict[str, Any]] = []
        for advance in advances:
            committed = _utc(advance.committed_through_utc)
            series_key = (
                advance.key.provider,
                advance.key.feed,
                advance.key.adjustment,
                advance.key.symbol,
                advance.key.timeframe,
            )
            latest_timestamp: datetime | None
            latest_observation_id: str | None
            if advance.last_observation_id is not None:
                stored = explicit_observations.get(advance.last_observation_id)
                if stored is None:
                    raise CollectionPersistenceError(
                        "CoverageAdvance references an unknown last observation"
                    )
                stored_series = (
                    str(stored["provider"]),
                    str(stored["feed"]),
                    str(stored["adjustment"]),
                    str(stored["symbol"]),
                    str(stored["timeframe"]),
                )
                explicit_timestamp_value = advance.last_bar_timestamp_utc
                if explicit_timestamp_value is None:
                    raise CollectionPersistenceError(
                        "CoverageAdvance last observation is missing its bar timestamp"
                    )
                explicit_timestamp = _utc(explicit_timestamp_value)
                if (
                    stored_series != series_key
                    or _utc(stored["bar_timestamp_utc"]) != explicit_timestamp
                ):
                    raise CollectionPersistenceError(
                        "CoverageAdvance last observation does not match its series and timestamp"
                    )
                latest_timestamp = explicit_timestamp
                latest_observation_id = advance.last_observation_id
            else:
                eligible = [
                    observation
                    for observation in observations_by_series.get(series_key, [])
                    if observation.bar.bar_timestamp_utc < committed
                ]
                latest = (
                    max(
                        eligible,
                        key=lambda observation: (
                            observation.bar.bar_timestamp_utc,
                            _source_precedence(observation.bar.source),
                            observation.is_correction,
                            observation.bar.provider_event_timestamp_utc
                            or observation.bar.receipt_timestamp_utc,
                            observation.bar.receipt_timestamp_utc,
                            observation.observation_id,
                        ),
                    )
                    if eligible
                    else None
                )
                latest_timestamp = None if latest is None else latest.bar.bar_timestamp_utc
                latest_observation_id = None if latest is None else latest.observation_id
            if latest_timestamp is not None and latest_timestamp >= committed:
                raise ValueError(
                    "CoverageAdvance last bar must precede its exclusive committed boundary"
                )
            rows.append(
                {
                    "checkpoint_name": advance.key.checkpoint_name,
                    "provider": advance.key.provider,
                    "feed": advance.key.feed,
                    "adjustment": advance.key.adjustment,
                    "symbol": advance.key.symbol,
                    "timeframe": advance.key.timeframe,
                    "committed_through_utc": committed,
                    "last_bar_timestamp_utc": latest_timestamp,
                    "last_observation_id": latest_observation_id,
                    "version": 1,
                    "holder_id": token.holder_id,
                    "fencing_token": token.fencing_token,
                    "metadata": dict(advance.metadata),
                }
            )

        statement = pg_insert(collector_checkpoints).values(rows)
        excluded = statement.excluded
        candidate_last_bar_wins = excluded.last_bar_timestamp_utc.is_not(None) & (
            collector_checkpoints.c.last_bar_timestamp_utc.is_(None)
            | (excluded.last_bar_timestamp_utc >= collector_checkpoints.c.last_bar_timestamp_utc)
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    collector_checkpoints.c.checkpoint_name,
                    collector_checkpoints.c.provider,
                    collector_checkpoints.c.feed,
                    collector_checkpoints.c.adjustment,
                    collector_checkpoints.c.symbol,
                    collector_checkpoints.c.timeframe,
                ],
                set_={
                    "committed_through_utc": excluded.committed_through_utc,
                    "last_bar_timestamp_utc": case(
                        (candidate_last_bar_wins, excluded.last_bar_timestamp_utc),
                        else_=collector_checkpoints.c.last_bar_timestamp_utc,
                    ),
                    "last_observation_id": case(
                        (candidate_last_bar_wins, excluded.last_observation_id),
                        else_=collector_checkpoints.c.last_observation_id,
                    ),
                    "version": collector_checkpoints.c.version + 1,
                    "holder_id": excluded.holder_id,
                    "fencing_token": excluded.fencing_token,
                    "metadata": excluded.metadata,
                    "updated_at": func.statement_timestamp(),
                },
                where=(
                    collector_checkpoints.c.committed_through_utc.is_(None)
                    | (
                        excluded.committed_through_utc
                        >= collector_checkpoints.c.committed_through_utc
                    )
                ),
            )
        )
        return len(advances)

    def append_batch(
        self,
        observations: Sequence[RawBarObservationV1],
        *,
        lease: LeaseToken,
        coverage_advances: Sequence[CoverageAdvance] = (),
    ) -> BatchResult:
        received = len(observations)
        unique = {observation.observation_id: observation for observation in observations}
        rows = [_observation_row(observation) for observation in unique.values()]
        with self.engine.begin() as connection:
            self._verify_lease(connection, lease)
            inserted_ids: set[str] = set()
            if rows:
                statement = (
                    pg_insert(bar_observations)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=[bar_observations.c.observation_id])
                    .returning(bar_observations.c.observation_id)
                )
                inserted_ids = set(connection.execute(statement).scalars())
                inserted_rows = [row for row in rows if row["observation_id"] in inserted_ids]
                inserted_identity_hashes = {str(row["identity_hash"]) for row in inserted_rows}
                existing_identities = (
                    set(
                        connection.execute(
                            select(current_bars.c.identity_hash).where(
                                current_bars.c.identity_hash.in_(inserted_identity_hashes)
                            )
                        ).scalars()
                    )
                    if inserted_identity_hashes
                    else set()
                )
                self._upsert_projections(connection, inserted_rows)
            else:
                inserted_rows = []
                inserted_identity_hashes = set()
                existing_identities = set()
            advanced = self._advance_checkpoints(
                connection,
                coverage_advances,
                tuple(unique.values()),
                lease,
            )

        inserted = len(inserted_ids)
        duplicates = received - inserted
        # ``current_bars`` now contains every inserted identity. The distinction is
        # primarily operational telemetry; exact revision state remains authoritative.
        inserted_current = len(inserted_identity_hashes - existing_identities)
        revised_current = len(inserted_identity_hashes & existing_identities)
        return BatchResult(
            received=received,
            observations_inserted=inserted,
            duplicates=duplicates,
            current_rows_inserted=inserted_current,
            current_rows_revised=revised_current,
            checkpoints_advanced=advanced,
        )

    def checkpoints(self, *, checkpoint_name: str) -> dict[str, Checkpoint]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(collector_checkpoints).where(
                    collector_checkpoints.c.checkpoint_name == checkpoint_name,
                    collector_checkpoints.c.provider == "alpaca",
                    collector_checkpoints.c.feed == "IEX",
                    collector_checkpoints.c.adjustment == "raw",
                    collector_checkpoints.c.timeframe == "1m",
                )
            ).mappings()
            return {
                str(row["symbol"]): Checkpoint(
                    key=CheckpointKey(
                        checkpoint_name=str(row["checkpoint_name"]),
                        provider=str(row["provider"]),
                        feed=str(row["feed"]),
                        adjustment=str(row["adjustment"]),
                        symbol=str(row["symbol"]),
                        timeframe=str(row["timeframe"]),
                    ),
                    committed_through_utc=row["committed_through_utc"],
                    last_bar_timestamp_utc=row["last_bar_timestamp_utc"],
                    last_observation_id=row["last_observation_id"],
                    version=int(row["version"]),
                    updated_at=row["updated_at"],
                )
                for row in rows
            }

    def try_acquire_lease(
        self,
        *,
        lease_name: str,
        holder_id: str,
        ttl_seconds: int,
    ) -> LeaseToken | None:
        if ttl_seconds < 10:
            raise ValueError("lease ttl must be at least 10 seconds")
        with self.engine.begin() as connection:
            now = connection.scalar(select(func.statement_timestamp()))
            if not isinstance(now, datetime):
                raise CollectionPersistenceError("Unable to read the database clock")
            expires_at = now + timedelta(seconds=ttl_seconds)
            insert_statement = pg_insert(collector_leases).values(
                lease_name=lease_name,
                holder_id=holder_id,
                fencing_token=1,
                acquired_at=now,
                renewed_at=now,
                expires_at=expires_at,
                metadata={},
            )
            statement = insert_statement.on_conflict_do_update(
                index_elements=[collector_leases.c.lease_name],
                set_={
                    "holder_id": holder_id,
                    "fencing_token": collector_leases.c.fencing_token + 1,
                    "acquired_at": now,
                    "renewed_at": now,
                    "expires_at": expires_at,
                    "metadata": {},
                },
                where=collector_leases.c.expires_at <= func.statement_timestamp(),
            ).returning(
                collector_leases.c.fencing_token,
                collector_leases.c.expires_at,
            )
            row = connection.execute(statement).one_or_none()
        if row is None:
            return None
        return LeaseToken(lease_name, holder_id, int(row.fencing_token), row.expires_at)

    def renew_lease(self, token: LeaseToken, *, ttl_seconds: int) -> LeaseToken:
        if ttl_seconds < 10:
            raise ValueError("lease ttl must be at least 10 seconds")
        with self.engine.begin() as connection:
            now = connection.scalar(select(func.statement_timestamp()))
            if not isinstance(now, datetime):
                raise CollectionPersistenceError("Unable to read the database clock")
            expires_at = now + timedelta(seconds=ttl_seconds)
            row = connection.execute(
                update(collector_leases)
                .where(
                    collector_leases.c.lease_name == token.lease_name,
                    collector_leases.c.holder_id == token.holder_id,
                    collector_leases.c.fencing_token == token.fencing_token,
                    collector_leases.c.expires_at > func.statement_timestamp(),
                )
                .values(renewed_at=func.statement_timestamp(), expires_at=expires_at)
                .returning(collector_leases.c.expires_at)
            ).one_or_none()
        if row is None:
            raise LeaseLostError("The collector lease expired or was taken over")
        return LeaseToken(
            token.lease_name,
            token.holder_id,
            token.fencing_token,
            row.expires_at,
        )

    def release_lease(self, token: LeaseToken) -> bool:
        with self.engine.begin() as connection:
            now = connection.scalar(select(func.statement_timestamp()))
            if not isinstance(now, datetime):
                raise CollectionPersistenceError("Unable to read the database clock")
            result = connection.execute(
                update(collector_leases)
                .where(
                    collector_leases.c.lease_name == token.lease_name,
                    collector_leases.c.holder_id == token.holder_id,
                    collector_leases.c.fencing_token == token.fencing_token,
                )
                .values(renewed_at=now, expires_at=now + timedelta(microseconds=1))
            )
        return result.rowcount == 1

    def record_event(
        self,
        *,
        event_type: str,
        lease: LeaseToken,
        severity: str = "info",
        run_id: str | None = None,
        symbol: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            self._verify_lease(connection, lease)
            connection.execute(
                collector_events.insert().values(
                    event_id=event_id,
                    run_id=run_id,
                    event_type=event_type,
                    severity=severity,
                    symbol=symbol,
                    details=dict(details or {}),
                )
            )
        return event_id

    def status(self) -> CollectorStatus:
        with self.engine.connect() as connection:
            current_count = connection.scalar(select(func.count()).select_from(current_bars))
            observation_count = connection.scalar(
                select(func.count()).select_from(bar_observations)
            )
            checkpoint_count = connection.scalar(
                select(func.count()).select_from(collector_checkpoints)
            )
            gap_count = connection.scalar(
                select(func.count())
                .select_from(data_gaps)
                .where(data_gaps.c.status.in_(("open", "repairing")))
            )
            run_count = connection.scalar(
                select(func.count())
                .select_from(ingestion_runs)
                .where(
                    ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                    ingestion_runs.c.status == "running",
                )
            )
            active_lease_count = connection.scalar(
                select(func.count()).select_from(
                    select(collector_leases.c.lease_name)
                    .select_from(
                        collector_leases.join(
                            ingestion_runs,
                            and_(
                                ingestion_runs.c.lease_name == collector_leases.c.lease_name,
                                ingestion_runs.c.holder_id == collector_leases.c.holder_id,
                                ingestion_runs.c.fencing_token == collector_leases.c.fencing_token,
                            ),
                        )
                    )
                    .where(
                        ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                        ingestion_runs.c.status == "running",
                        collector_leases.c.expires_at > func.statement_timestamp(),
                    )
                    .distinct()
                    .subquery()
                )
            )
            active_run_count = connection.scalar(
                select(func.count())
                .select_from(
                    ingestion_runs.join(
                        collector_leases,
                        and_(
                            ingestion_runs.c.lease_name == collector_leases.c.lease_name,
                            ingestion_runs.c.holder_id == collector_leases.c.holder_id,
                            ingestion_runs.c.fencing_token == collector_leases.c.fencing_token,
                        ),
                    )
                )
                .where(
                    ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                    ingestion_runs.c.status == "running",
                    collector_leases.c.expires_at > func.statement_timestamp(),
                )
            )
            latest_receipt = connection.scalar(
                select(func.max(current_bars.c.receipt_timestamp_utc))
            )
        return CollectorStatus(
            current_bar_count=int(current_count or 0),
            observation_count=int(observation_count or 0),
            checkpoint_count=int(checkpoint_count or 0),
            open_gap_count=int(gap_count or 0),
            running_run_count=int(run_count or 0),
            latest_receipt_timestamp_utc=latest_receipt,
            active_lease_count=int(active_lease_count or 0),
            active_run_count=int(active_run_count or 0),
        )

    def is_ready(self, *, lease_name: str) -> bool:
        """Check active ownership with an indexed query suitable for frequent health probes."""

        with self.engine.connect() as connection:
            active_run = connection.scalar(
                select(ingestion_runs.c.run_id)
                .select_from(
                    ingestion_runs.join(
                        collector_leases,
                        and_(
                            ingestion_runs.c.lease_name == collector_leases.c.lease_name,
                            ingestion_runs.c.holder_id == collector_leases.c.holder_id,
                            ingestion_runs.c.fencing_token == collector_leases.c.fencing_token,
                        ),
                    )
                )
                .where(
                    ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                    ingestion_runs.c.lease_name == lease_name,
                    ingestion_runs.c.status == "running",
                    collector_leases.c.expires_at > func.statement_timestamp(),
                )
                .limit(1)
            )
        return active_run is not None

    def close(self) -> None:
        self.engine.dispose()
