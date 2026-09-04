"""PostgreSQL schema declarations for the isolated market-data service."""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA_NAME = "market_data"

metadata = MetaData(schema=SCHEMA_NAME)

collection_universes = Table(
    "collection_universes",
    metadata,
    Column("universe_hash", String(64), primary_key=True),
    Column("schema_version", String(64), nullable=False),
    Column("members", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("length(universe_hash) = 64", name="ck_universe_hash_length"),
)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column(
        "universe_hash",
        ForeignKey(f"{SCHEMA_NAME}.collection_universes.universe_hash"),
        nullable=False,
    ),
    Column("mode", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("holder_id", String(128), nullable=False),
    Column("lease_name", String(128), nullable=False),
    Column("fencing_token", BigInteger, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    Column("counters", JSONB, nullable=False, server_default="{}"),
    Column("error", Text),
    CheckConstraint(
        "mode IN ('backfill', 'stream', 'run')",
        name="ck_ingestion_run_mode",
    ),
    CheckConstraint(
        "status IN ('running', 'completed', 'failed', 'stopped')",
        name="ck_ingestion_run_status",
    ),
    CheckConstraint("fencing_token >= 1", name="ck_ingestion_run_fencing_token"),
)

Index(
    "uq_ingestion_runs_active_lease",
    ingestion_runs.c.universe_hash,
    ingestion_runs.c.lease_name,
    unique=True,
    postgresql_where=ingestion_runs.c.status == "running",
)

bar_observations = Table(
    "bar_observations",
    metadata,
    Column("observation_id", String(64), primary_key=True),
    Column("schema_version", String(64), nullable=False),
    Column("identity_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("feed", String(32), nullable=False),
    Column("adjustment", String(32), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("bar_timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("provider_event_timestamp_utc", DateTime(timezone=True)),
    Column("receipt_timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("open", Numeric(28, 12), nullable=False),
    Column("high", Numeric(28, 12), nullable=False),
    Column("low", Numeric(28, 12), nullable=False),
    Column("close", Numeric(28, 12), nullable=False),
    Column("volume", BigInteger, nullable=False),
    Column("trade_count", BigInteger),
    Column("vwap", Numeric(28, 12)),
    Column("quality_flags", ARRAY(String(64)), nullable=False, server_default="{}"),
    Column("source", String(64), nullable=False),
    Column("source_precedence", Integer, nullable=False),
    Column("is_correction", Boolean, nullable=False),
    Column("provider_event_id", String(255)),
    Column("raw_payload_sha256", String(64)),
    Column("raw_payload", JSONB),
    Column("inserted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "observation_id",
        "identity_hash",
        "content_hash",
        name="uq_bar_observation_projection_reference",
    ),
    CheckConstraint("length(observation_id) = 64", name="ck_observation_id_length"),
    CheckConstraint("length(identity_hash) = 64", name="ck_observation_identity_hash_length"),
    CheckConstraint("length(content_hash) = 64", name="ck_observation_content_hash_length"),
    CheckConstraint(
        "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_bar_prices_positive"
    ),
    CheckConstraint("high >= open AND high >= close AND high >= low", name="ck_bar_high"),
    CheckConstraint("low <= open AND low <= close AND low <= high", name="ck_bar_low"),
    CheckConstraint("volume >= 0", name="ck_bar_volume"),
    CheckConstraint("trade_count IS NULL OR trade_count >= 0", name="ck_bar_trade_count"),
    CheckConstraint("vwap IS NULL OR vwap > 0", name="ck_bar_vwap"),
)

Index("ix_bar_observations_identity_hash", bar_observations.c.identity_hash)
Index(
    "ix_bar_observations_lookup",
    bar_observations.c.provider,
    bar_observations.c.feed,
    bar_observations.c.adjustment,
    bar_observations.c.symbol,
    bar_observations.c.timeframe,
    bar_observations.c.bar_timestamp_utc.desc(),
)
Index(
    "ix_bar_observations_inserted_brin",
    bar_observations.c.inserted_at,
    postgresql_using="brin",
)

current_bars = Table(
    "current_bars",
    metadata,
    Column("identity_hash", String(64), primary_key=True),
    Column("current_observation_id", String(64), nullable=False, unique=True),
    Column("content_hash", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("feed", String(32), nullable=False),
    Column("adjustment", String(32), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("bar_timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("provider_event_timestamp_utc", DateTime(timezone=True)),
    Column("receipt_timestamp_utc", DateTime(timezone=True), nullable=False),
    Column("open", Numeric(28, 12), nullable=False),
    Column("high", Numeric(28, 12), nullable=False),
    Column("low", Numeric(28, 12), nullable=False),
    Column("close", Numeric(28, 12), nullable=False),
    Column("volume", BigInteger, nullable=False),
    Column("trade_count", BigInteger),
    Column("vwap", Numeric(28, 12)),
    Column("quality_flags", ARRAY(String(64)), nullable=False, server_default="{}"),
    Column("source", String(64), nullable=False),
    Column("source_precedence", Integer, nullable=False),
    Column("is_correction", Boolean, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("observation_count", BigInteger, nullable=False),
    Column("first_observed_at", DateTime(timezone=True), nullable=False),
    Column("last_observed_at", DateTime(timezone=True), nullable=False),
    Column("projected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "provider",
        "feed",
        "adjustment",
        "symbol",
        "timeframe",
        "bar_timestamp_utc",
        name="uq_current_bar_natural_identity",
    ),
    ForeignKeyConstraint(
        ("current_observation_id", "identity_hash", "content_hash"),
        (
            f"{SCHEMA_NAME}.bar_observations.observation_id",
            f"{SCHEMA_NAME}.bar_observations.identity_hash",
            f"{SCHEMA_NAME}.bar_observations.content_hash",
        ),
        name="fk_current_bar_observation",
    ),
    CheckConstraint("revision >= 1", name="ck_current_bar_revision"),
    CheckConstraint("observation_count >= 1", name="ck_current_bar_observation_count"),
)

Index(
    "ix_current_bars_query",
    current_bars.c.symbol,
    current_bars.c.timeframe,
    current_bars.c.bar_timestamp_utc.desc(),
)

collector_leases = Table(
    "collector_leases",
    metadata,
    Column("lease_name", String(128), primary_key=True),
    Column("holder_id", String(128), nullable=False),
    Column("fencing_token", BigInteger, nullable=False),
    Column("acquired_at", DateTime(timezone=True), nullable=False),
    Column("renewed_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    CheckConstraint("fencing_token >= 1", name="ck_lease_fencing_token"),
    CheckConstraint("expires_at > renewed_at", name="ck_lease_expiry"),
    CheckConstraint("renewed_at >= acquired_at", name="ck_lease_renewed"),
)

collector_checkpoints = Table(
    "collector_checkpoints",
    metadata,
    Column("checkpoint_name", String(64), primary_key=True),
    Column("provider", String(32), primary_key=True),
    Column("feed", String(32), primary_key=True),
    Column("adjustment", String(32), primary_key=True),
    Column("symbol", String(32), primary_key=True),
    Column("timeframe", String(16), primary_key=True),
    Column("committed_through_utc", DateTime(timezone=True)),
    Column("last_bar_timestamp_utc", DateTime(timezone=True)),
    Column(
        "last_observation_id",
        String(64),
        ForeignKey(f"{SCHEMA_NAME}.bar_observations.observation_id"),
    ),
    Column("version", BigInteger, nullable=False, server_default="0"),
    Column("holder_id", String(128), nullable=False),
    Column("fencing_token", BigInteger, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("version >= 0", name="ck_checkpoint_version"),
)

data_gaps = Table(
    "data_gaps",
    metadata,
    Column("gap_id", String(64), primary_key=True),
    Column("provider", String(32), nullable=False),
    Column("feed", String(32), nullable=False),
    Column("adjustment", String(32), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("gap_start_utc", DateTime(timezone=True), nullable=False),
    Column("gap_end_utc", DateTime(timezone=True), nullable=False),
    Column("detected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("reason", String(128), nullable=False),
    Column("status", String(16), nullable=False, server_default="open"),
    Column("version", BigInteger, nullable=False, server_default="0"),
    Column("resolved_at", DateTime(timezone=True)),
    Column("resolution_kind", String(64)),
    Column("resolution_details", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint(
        "provider",
        "feed",
        "adjustment",
        "symbol",
        "timeframe",
        "gap_start_utc",
        "gap_end_utc",
        name="uq_data_gap_interval",
    ),
    CheckConstraint("gap_start_utc < gap_end_utc", name="ck_data_gap_interval"),
    CheckConstraint(
        "status IN ('open', 'repairing', 'resolved', 'waived')",
        name="ck_data_gap_status",
    ),
)

Index(
    "ix_data_gaps_active",
    data_gaps.c.status,
    data_gaps.c.gap_start_utc,
    postgresql_where=data_gaps.c.status.in_(("open", "repairing")),
)

collector_events = Table(
    "collector_events",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column("run_id", String(36), ForeignKey(f"{SCHEMA_NAME}.ingestion_runs.run_id")),
    Column("event_type", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("symbol", String(32)),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("details", JSONB, nullable=False, server_default="{}"),
    CheckConstraint(
        "severity IN ('debug', 'info', 'warning', 'error', 'critical')",
        name="ck_collector_event_severity",
    ),
)
