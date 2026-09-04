"""Create the PostgreSQL market-data foundation.

Revision ID: 20260903_0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "market_data"


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    op.create_table(
        "collection_universes",
        sa.Column("universe_hash", sa.String(64), primary_key=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("members", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(universe_hash) = 64", name="ck_universe_hash_length"),
        schema=SCHEMA,
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column(
            "universe_hash",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.collection_universes.universe_hash"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("holder_id", sa.String(128), nullable=False),
        sa.Column("lease_name", sa.String(128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "counters", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("mode IN ('backfill', 'stream', 'run')", name="ck_ingestion_run_mode"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'stopped')",
            name="ck_ingestion_run_status",
        ),
        sa.CheckConstraint(
            "fencing_token >= 1",
            name="ck_ingestion_run_fencing_token",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_ingestion_runs_active_lease",
        "ingestion_runs",
        ["universe_hash", "lease_name"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "bar_observations",
        sa.Column("observation_id", sa.String(64), primary_key=True),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("feed", sa.String(32), nullable=False),
        sa.Column("adjustment", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("bar_timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_event_timestamp_utc", sa.DateTime(timezone=True)),
        sa.Column("receipt_timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(28, 12), nullable=False),
        sa.Column("high", sa.Numeric(28, 12), nullable=False),
        sa.Column("low", sa.Numeric(28, 12), nullable=False),
        sa.Column("close", sa.Numeric(28, 12), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("trade_count", sa.BigInteger()),
        sa.Column("vwap", sa.Numeric(28, 12)),
        sa.Column(
            "quality_flags",
            postgresql.ARRAY(sa.String(64)),
            server_default=sa.text("'{}'::character varying[]"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_precedence", sa.Integer(), nullable=False),
        sa.Column("is_correction", sa.Boolean(), nullable=False),
        sa.Column("provider_event_id", sa.String(255)),
        sa.Column("raw_payload_sha256", sa.String(64)),
        sa.Column("raw_payload", postgresql.JSONB()),
        sa.Column(
            "inserted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "observation_id",
            "identity_hash",
            "content_hash",
            name="uq_bar_observation_projection_reference",
        ),
        sa.CheckConstraint("length(observation_id) = 64", name="ck_observation_id_length"),
        sa.CheckConstraint(
            "length(identity_hash) = 64", name="ck_observation_identity_hash_length"
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_observation_content_hash_length"),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_bar_prices_positive",
        ),
        sa.CheckConstraint(
            "high >= open AND high >= close AND high >= low",
            name="ck_bar_high",
        ),
        sa.CheckConstraint("low <= open AND low <= close AND low <= high", name="ck_bar_low"),
        sa.CheckConstraint("volume >= 0", name="ck_bar_volume"),
        sa.CheckConstraint("trade_count IS NULL OR trade_count >= 0", name="ck_bar_trade_count"),
        sa.CheckConstraint("vwap IS NULL OR vwap > 0", name="ck_bar_vwap"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_bar_observations_identity_hash", "bar_observations", ["identity_hash"], schema=SCHEMA
    )
    op.create_index(
        "ix_bar_observations_lookup",
        "bar_observations",
        [
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            sa.text("bar_timestamp_utc DESC"),
        ],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_bar_observations_inserted_brin",
        "bar_observations",
        ["inserted_at"],
        schema=SCHEMA,
        postgresql_using="brin",
    )
    op.create_table(
        "current_bars",
        sa.Column("identity_hash", sa.String(64), primary_key=True),
        sa.Column("current_observation_id", sa.String(64), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("feed", sa.String(32), nullable=False),
        sa.Column("adjustment", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("bar_timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_event_timestamp_utc", sa.DateTime(timezone=True)),
        sa.Column("receipt_timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(28, 12), nullable=False),
        sa.Column("high", sa.Numeric(28, 12), nullable=False),
        sa.Column("low", sa.Numeric(28, 12), nullable=False),
        sa.Column("close", sa.Numeric(28, 12), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("trade_count", sa.BigInteger()),
        sa.Column("vwap", sa.Numeric(28, 12)),
        sa.Column(
            "quality_flags",
            postgresql.ARRAY(sa.String(64)),
            server_default=sa.text("'{}'::character varying[]"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_precedence", sa.Integer(), nullable=False),
        sa.Column("is_correction", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("observation_count", sa.BigInteger(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "projected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["current_observation_id", "identity_hash", "content_hash"],
            [
                f"{SCHEMA}.bar_observations.observation_id",
                f"{SCHEMA}.bar_observations.identity_hash",
                f"{SCHEMA}.bar_observations.content_hash",
            ],
            name="fk_current_bar_observation",
        ),
        sa.UniqueConstraint(
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            "bar_timestamp_utc",
            name="uq_current_bar_natural_identity",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_current_bar_revision"),
        sa.CheckConstraint("observation_count >= 1", name="ck_current_bar_observation_count"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_current_bars_query",
        "current_bars",
        ["symbol", "timeframe", sa.text("bar_timestamp_utc DESC")],
        schema=SCHEMA,
    )
    op.create_table(
        "collector_leases",
        sa.Column("lease_name", sa.String(128), primary_key=True),
        sa.Column("holder_id", sa.String(128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.CheckConstraint("fencing_token >= 1", name="ck_lease_fencing_token"),
        sa.CheckConstraint("expires_at > renewed_at", name="ck_lease_expiry"),
        sa.CheckConstraint("renewed_at >= acquired_at", name="ck_lease_renewed"),
        schema=SCHEMA,
    )
    op.create_table(
        "collector_checkpoints",
        sa.Column("checkpoint_name", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("feed", sa.String(32), primary_key=True),
        sa.Column("adjustment", sa.String(32), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("timeframe", sa.String(16), primary_key=True),
        sa.Column("committed_through_utc", sa.DateTime(timezone=True)),
        sa.Column("last_bar_timestamp_utc", sa.DateTime(timezone=True)),
        sa.Column(
            "last_observation_id",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.bar_observations.observation_id"),
        ),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("holder_id", sa.String(128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version >= 0", name="ck_checkpoint_version"),
        schema=SCHEMA,
    )
    op.create_table(
        "data_gaps",
        sa.Column("gap_id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("feed", sa.String(32), nullable=False),
        sa.Column("adjustment", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("gap_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_kind", sa.String(64)),
        sa.Column(
            "resolution_details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            "gap_start_utc",
            "gap_end_utc",
            name="uq_data_gap_interval",
        ),
        sa.CheckConstraint("gap_start_utc < gap_end_utc", name="ck_data_gap_interval"),
        sa.CheckConstraint(
            "status IN ('open', 'repairing', 'resolved', 'waived')",
            name="ck_data_gap_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_data_gaps_active",
        "data_gaps",
        ["status", "gap_start_utc"],
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('open', 'repairing')"),
    )
    op.create_table(
        "collector_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey(f"{SCHEMA}.ingestion_runs.run_id")),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32)),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.CheckConstraint(
            "severity IN ('debug', 'info', 'warning', 'error', 'critical')",
            name="ck_collector_event_severity",
        ),
        schema=SCHEMA,
    )

    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {SCHEMA}.reject_bar_observation_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'bar observations are append-only';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER bar_observations_are_immutable
            BEFORE UPDATE OR DELETE ON {SCHEMA}.bar_observations
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_bar_observation_mutation()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER bar_observations_reject_truncate
            BEFORE TRUNCATE ON {SCHEMA}.bar_observations
            FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_bar_observation_mutation()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {SCHEMA}.guard_checkpoint_update()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.committed_through_utc IS NOT NULL
                   AND (NEW.committed_through_utc IS NULL
                        OR NEW.committed_through_utc < OLD.committed_through_utc) THEN
                    RAISE EXCEPTION 'collector checkpoint cannot regress';
                END IF;
                IF NEW.version <> OLD.version + 1 THEN
                    RAISE EXCEPTION 'collector checkpoint version must advance exactly once';
                END IF;
                NEW.updated_at := statement_timestamp();
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER collector_checkpoints_are_monotonic
            BEFORE UPDATE ON {SCHEMA}.collector_checkpoints
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_checkpoint_update()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {SCHEMA}.reject_checkpoint_removal()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'collector checkpoints cannot be removed';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER collector_checkpoints_reject_delete
            BEFORE DELETE ON {SCHEMA}.collector_checkpoints
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_checkpoint_removal()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER collector_checkpoints_reject_truncate
            BEFORE TRUNCATE ON {SCHEMA}.collector_checkpoints
            FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_checkpoint_removal()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS collector_checkpoints_reject_truncate "
        f"ON {SCHEMA}.collector_checkpoints"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS collector_checkpoints_reject_delete "
        f"ON {SCHEMA}.collector_checkpoints"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_checkpoint_removal()")
    op.execute(
        f"DROP TRIGGER IF EXISTS collector_checkpoints_are_monotonic ON {SCHEMA}.collector_checkpoints"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_checkpoint_update()")
    op.execute(
        f"DROP TRIGGER IF EXISTS bar_observations_reject_truncate ON {SCHEMA}.bar_observations"
    )
    op.execute(
        f"DROP TRIGGER IF EXISTS bar_observations_are_immutable ON {SCHEMA}.bar_observations"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_bar_observation_mutation()")
    op.drop_table("collector_events", schema=SCHEMA)
    op.drop_index("ix_data_gaps_active", table_name="data_gaps", schema=SCHEMA)
    op.drop_table("data_gaps", schema=SCHEMA)
    op.drop_table("collector_checkpoints", schema=SCHEMA)
    op.drop_table("collector_leases", schema=SCHEMA)
    op.drop_index("ix_current_bars_query", table_name="current_bars", schema=SCHEMA)
    op.drop_table("current_bars", schema=SCHEMA)
    op.drop_index("ix_bar_observations_inserted_brin", table_name="bar_observations", schema=SCHEMA)
    op.drop_index("ix_bar_observations_lookup", table_name="bar_observations", schema=SCHEMA)
    op.drop_index("ix_bar_observations_identity_hash", table_name="bar_observations", schema=SCHEMA)
    op.drop_table("bar_observations", schema=SCHEMA)
    op.drop_index(
        "uq_ingestion_runs_active_lease",
        table_name="ingestion_runs",
        schema=SCHEMA,
    )
    op.drop_table("ingestion_runs", schema=SCHEMA)
    op.drop_table("collection_universes", schema=SCHEMA)
