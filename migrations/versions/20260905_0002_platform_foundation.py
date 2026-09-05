"""Create the additive generic-platform schema.

Revision ID: 20260905_0002
Revises: 20260903_0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0002"
down_revision: str | None = "20260903_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aqa"


def upgrade() -> None:
    """Create the generic platform schema without changing collector-owned state."""

    op.execute(sa.schema.CreateSchema(SCHEMA, if_not_exists=True))
    op.create_table(
        "aqa_audit_events",
        sa.Column("audit_event_id", sa.String(length=128), nullable=False),
        sa.Column("stream_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "content_hash = event_hash",
            name=op.f("ck_aqa_audit_events_audit_content_matches_event"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_audit_events_content_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64", name=op.f("ck_aqa_audit_events_event_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name=op.f("ck_aqa_audit_events_payload_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(previous_hash) = 64",
            name=op.f("ck_aqa_audit_events_previous_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_aqa_audit_events_audit_sequence_positive")
        ),
        sa.PrimaryKeyConstraint("audit_event_id", name=op.f("pk_aqa_audit_events")),
        sa.UniqueConstraint("event_hash", name=op.f("uq_aqa_audit_events_event_hash")),
        sa.UniqueConstraint("stream_id", "sequence", name="audit_stream_sequence"),
        schema="aqa",
    )
    op.create_table(
        "aqa_bar_identities",
        sa.Column("bar_identity_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.Column("adjustment", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_bar_identities_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "start_at < end_at", name=op.f("ck_aqa_bar_identities_bar_interval_positive")
        ),
        sa.PrimaryKeyConstraint("bar_identity_id", name=op.f("pk_aqa_bar_identities")),
        sa.UniqueConstraint(
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            "start_at",
            name="bar_series_start",
        ),
        schema="aqa",
    )
    op.create_table(
        "aqa_experiments",
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_version", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "configuration",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "experiment_version >= 1", name=op.f("ck_aqa_experiments_experiment_version_positive")
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_experiments_content_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(experiment_hash) = 64",
            name=op.f("ck_aqa_experiments_experiment_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_aqa_experiments_schema_version_positive")
        ),
        sa.PrimaryKeyConstraint("experiment_hash", name=op.f("pk_aqa_experiments")),
        sa.UniqueConstraint("content_hash", name=op.f("uq_aqa_experiments_content_hash")),
        sa.UniqueConstraint("experiment_id", "experiment_version", name="experiment_id_version"),
        schema="aqa",
    )
    op.create_table(
        "aqa_jobs",
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "((state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name=op.f("ck_aqa_jobs_job_lease_consistent"),
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_aqa_jobs_job_state"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_aqa_jobs_job_attempts_nonnegative")),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_jobs_content_hash_sha256_length")
        ),
        sa.CheckConstraint("max_attempts = 3", name=op.f("ck_aqa_jobs_job_max_attempts")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_aqa_jobs_job_version_positive")),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_aqa_jobs")),
        sa.UniqueConstraint("job_type", "idempotency_key", name="job_type_idempotency"),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_jobs_claim",
        "aqa_jobs",
        ["state", "next_attempt_at", "created_at"],
        unique=False,
        schema="aqa",
    )
    op.create_table(
        "aqa_outbox_events",
        sa.Column("outbox_event_id", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(state = 'published' AND published_at IS NOT NULL) OR (state <> 'published' AND published_at IS NULL)",
            name=op.f("ck_aqa_outbox_events_outbox_publication_consistent"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'published', 'failed')",
            name=op.f("ck_aqa_outbox_events_outbox_state"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_aqa_outbox_events_outbox_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_outbox_events_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_outbox_events_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_aqa_outbox_events_outbox_version_positive")
        ),
        sa.PrimaryKeyConstraint("outbox_event_id", name=op.f("pk_aqa_outbox_events")),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_outbox_delivery",
        "aqa_outbox_events",
        ["state", "next_attempt_at"],
        unique=False,
        schema="aqa",
    )
    op.create_table(
        "aqa_security_metadata_events",
        sa.Column("security_event_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tradable", sa.Boolean(), nullable=False),
        sa.Column("fractionable", sa.Boolean(), nullable=False),
        sa.Column("shortable", sa.Boolean(), nullable=False),
        sa.Column(
            "attributes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_security_metadata_events_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_security_metadata_events_payload_hash_sha256_length"),
        ),
        sa.PrimaryKeyConstraint("security_event_id", name=op.f("pk_aqa_security_metadata_events")),
        sa.UniqueConstraint("source", "source_event_id", name="security_source_event"),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_security_metadata_symbol_effective",
        "aqa_security_metadata_events",
        ["symbol", "effective_at"],
        unique=False,
        schema="aqa",
    )
    op.create_table(
        "aqa_bar_events",
        sa.Column("bar_event_id", sa.String(length=128), nullable=False),
        sa.Column("bar_identity_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("high", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("low", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("close", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("volume", sa.Numeric(precision=38, scale=0), nullable=False),
        sa.Column("trade_count", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column(
            "quality_flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("source_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("correction_of_event_id", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "CAST(high AS NUMERIC) >= CAST(open AS NUMERIC) "
            "AND CAST(high AS NUMERIC) >= CAST(close AS NUMERIC) "
            "AND CAST(high AS NUMERIC) >= CAST(low AS NUMERIC)",
            name=op.f("ck_aqa_bar_events_bar_high_coherent"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_bar_events_content_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(source_payload_hash) = 64",
            name=op.f("ck_aqa_bar_events_source_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "CAST(low AS NUMERIC) <= CAST(open AS NUMERIC) "
            "AND CAST(low AS NUMERIC) <= CAST(close AS NUMERIC) "
            "AND CAST(low AS NUMERIC) <= CAST(high AS NUMERIC)",
            name=op.f("ck_aqa_bar_events_bar_low_coherent"),
        ),
        sa.CheckConstraint(
            "CAST(open AS NUMERIC) > 0 AND CAST(high AS NUMERIC) > 0 "
            "AND CAST(low AS NUMERIC) > 0 AND CAST(close AS NUMERIC) > 0",
            name=op.f("ck_aqa_bar_events_bar_prices_positive"),
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_aqa_bar_events_bar_revision_positive")),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_aqa_bar_events_bar_schema_version_positive")
        ),
        sa.CheckConstraint(
            "trade_count IS NULL OR trade_count >= 0",
            name=op.f("ck_aqa_bar_events_bar_trades_nonnegative"),
        ),
        sa.CheckConstraint(
            "CAST(volume AS NUMERIC) >= 0",
            name=op.f("ck_aqa_bar_events_bar_volume_nonnegative"),
        ),
        sa.CheckConstraint(
            "vwap IS NULL OR CAST(vwap AS NUMERIC) > 0",
            name=op.f("ck_aqa_bar_events_bar_vwap_positive"),
        ),
        sa.CheckConstraint(
            "CAST(open AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(high AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(low AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(close AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(volume AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "(vwap IS NULL OR CAST(vwap AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name=op.f("ck_aqa_bar_events_financial_values_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["bar_identity_id"],
            ["aqa.aqa_bar_identities.bar_identity_id"],
            name=op.f("fk_aqa_bar_events_bar_identity_id_aqa_bar_identities"),
        ),
        sa.ForeignKeyConstraint(
            ["correction_of_event_id"],
            ["aqa.aqa_bar_events.bar_event_id"],
            name="bar_event_correction",
        ),
        sa.PrimaryKeyConstraint("bar_event_id", name=op.f("pk_aqa_bar_events")),
        sa.UniqueConstraint(
            "bar_identity_id", "bar_event_id", "revision", name="bar_latest_reference"
        ),
        sa.UniqueConstraint("bar_identity_id", "content_hash", name="bar_identity_content"),
        sa.UniqueConstraint("bar_identity_id", "revision", name="bar_identity_revision"),
        schema="aqa",
    )
    op.create_table(
        "aqa_basket_watermarks",
        sa.Column("basket_watermark_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("contiguous_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("component_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('active', 'benchmark', 'context')",
            name=op.f("ck_aqa_basket_watermarks_basket_watermark_role_value"),
        ),
        sa.CheckConstraint(
            "length(component_hash) = 64",
            name=op.f("ck_aqa_basket_watermarks_component_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_basket_watermarks_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_aqa_basket_watermarks_basket_watermark_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_basket_watermarks_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("basket_watermark_id", name=op.f("pk_aqa_basket_watermarks")),
        sa.UniqueConstraint("experiment_hash", "role", "timeframe", name="basket_watermark_role"),
        schema="aqa",
    )
    op.create_table(
        "aqa_data_gaps",
        sa.Column("gap_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.Column("adjustment", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("gap_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'repairing', 'resolved', 'waived')",
            name=op.f("ck_aqa_data_gaps_gap_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_aqa_data_gaps_gap_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "gap_start_at < gap_end_at", name=op.f("ck_aqa_data_gaps_gap_interval_positive")
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_data_gaps_content_hash_sha256_length")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_aqa_data_gaps_gap_version_positive")),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_data_gaps_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("gap_id", name=op.f("pk_aqa_data_gaps")),
        sa.UniqueConstraint(
            "experiment_hash",
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            "gap_start_at",
            "gap_end_at",
            name="gap_series_interval",
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_data_gaps_status_start",
        "aqa_data_gaps",
        ["status", "gap_start_at"],
        unique=False,
        schema="aqa",
    )
    op.create_table(
        "aqa_dataset_manifests",
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.Column("adjustment", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("range_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "roles",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "symbols",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "row_counts",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "gap_summary",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "correction_summary",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("logical_hash", sa.String(length=64), nullable=False),
        sa.Column("physical_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("source_git_commit", sa.String(length=40), nullable=False),
        sa.Column("dirty_worktree", sa.Boolean(), nullable=False),
        sa.Column("uv_lock_hash", sa.String(length=64), nullable=False),
        sa.Column("promotable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('promotable', 'diagnostic')",
            name=op.f("ck_aqa_dataset_manifests_dataset_status"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_dataset_manifests_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(logical_hash) = 64",
            name=op.f("ck_aqa_dataset_manifests_logical_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64",
            name=op.f("ck_aqa_dataset_manifests_manifest_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(physical_hash) = 64",
            name=op.f("ck_aqa_dataset_manifests_physical_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(uv_lock_hash) = 64",
            name=op.f("ck_aqa_dataset_manifests_uv_lock_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "range_start_at < range_end_at",
            name=op.f("ck_aqa_dataset_manifests_dataset_interval_positive"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_aqa_dataset_manifests_dataset_schema_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_dataset_manifests_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("dataset_id", name=op.f("pk_aqa_dataset_manifests")),
        sa.UniqueConstraint("artifact_id", name=op.f("uq_aqa_dataset_manifests_artifact_id")),
        sa.UniqueConstraint("logical_hash", name=op.f("uq_aqa_dataset_manifests_logical_hash")),
        sa.UniqueConstraint("manifest_hash", name=op.f("uq_aqa_dataset_manifests_manifest_hash")),
        schema="aqa",
    )
    op.create_table(
        "aqa_decision_slots",
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("source_bar_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("claim_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "((state = 'claimed' AND claim_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state <> 'claimed' AND claim_owner IS NULL AND lease_expires_at IS NULL))",
            name=op.f("ck_aqa_decision_slots_decision_claim_consistent"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'completed', 'skipped', 'expired', 'failed')",
            name=op.f("ck_aqa_decision_slots_decision_state"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_aqa_decision_slots_decision_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_decision_slots_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "scheduled_at <= data_deadline_at AND data_deadline_at <= signal_deadline_at AND signal_deadline_at <= execution_deadline_at",
            name=op.f("ck_aqa_decision_slots_decision_deadlines_ordered"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_aqa_decision_slots_decision_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_decision_slots_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("slot_id", name=op.f("pk_aqa_decision_slots")),
        sa.UniqueConstraint(
            "experiment_hash", "source_bar_end", "decision_type", name="decision_source_type"
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_decision_slots_claim",
        "aqa_decision_slots",
        ["state", "scheduled_at"],
        unique=False,
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_decision_slots_lease",
        "aqa_decision_slots",
        ["lease_expires_at"],
        unique=False,
        schema="aqa",
    )
    op.create_table(
        "aqa_experiment_symbols",
        sa.Column("experiment_symbol_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('active', 'benchmark', 'context', 'excluded')",
            name=op.f("ck_aqa_experiment_symbols_symbol_role"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_experiment_symbols_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_aqa_experiment_symbols_symbol_ordinal_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_experiment_symbols_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("experiment_symbol_id", name=op.f("pk_aqa_experiment_symbols")),
        sa.UniqueConstraint("experiment_hash", "role", "ordinal", name="experiment_role_ordinal"),
        sa.UniqueConstraint("experiment_hash", "symbol", name="experiment_symbol"),
        schema="aqa",
    )
    op.create_table(
        "aqa_incidents",
        sa.Column("incident_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=True),
        sa.Column("incident_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "(status = 'resolved' AND resolved_at IS NOT NULL) OR (status = 'open' AND resolved_at IS NULL)",
            name=op.f("ck_aqa_incidents_incident_resolution_consistent"),
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error', 'critical')",
            name=op.f("ck_aqa_incidents_incident_severity"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=op.f("ck_aqa_incidents_incident_status")
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_incidents_content_hash_sha256_length")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_aqa_incidents_incident_version_positive")),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_incidents_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("incident_id", name=op.f("pk_aqa_incidents")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_aqa_incidents_idempotency_key")),
        schema="aqa",
    )
    op.create_table(
        "aqa_job_attempts",
        sa.Column("job_attempt_id", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'failed', 'abandoned')",
            name=op.f("ck_aqa_job_attempts_job_attempt_outcome"),
        ),
        sa.CheckConstraint(
            "attempt_number >= 1", name=op.f("ck_aqa_job_attempts_job_attempt_number_positive")
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_aqa_job_attempts_job_attempt_times_ordered"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_job_attempts_content_hash_sha256_length")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["aqa.aqa_jobs.job_id"], name=op.f("fk_aqa_job_attempts_job_id_aqa_jobs")
        ),
        sa.PrimaryKeyConstraint("job_attempt_id", name=op.f("pk_aqa_job_attempts")),
        sa.UniqueConstraint("job_id", "attempt_number", name="job_attempt_number"),
        schema="aqa",
    )
    op.create_table(
        "aqa_risk_latch_events",
        sa.Column("latch_event_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("latch_type", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "action IN ('engage', 'clear')", name=op.f("ck_aqa_risk_latch_events_risk_latch_action")
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_risk_latch_events_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_risk_latch_events_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_aqa_risk_latch_events_risk_latch_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_risk_latch_events_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("latch_event_id", name=op.f("pk_aqa_risk_latch_events")),
        sa.UniqueConstraint(
            "experiment_hash", "latch_type", "sequence", name="risk_latch_sequence"
        ),
        schema="aqa",
    )
    op.create_table(
        "aqa_bar_latest",
        sa.Column("bar_identity_id", sa.String(length=128), nullable=False),
        sa.Column("bar_event_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_bar_latest_content_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_aqa_bar_latest_bar_latest_revision_positive")
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_aqa_bar_latest_bar_latest_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["bar_identity_id", "bar_event_id", "revision"],
            [
                "aqa.aqa_bar_events.bar_identity_id",
                "aqa.aqa_bar_events.bar_event_id",
                "aqa.aqa_bar_events.revision",
            ],
            name="bar_latest_event",
        ),
        sa.ForeignKeyConstraint(
            ["bar_identity_id"],
            ["aqa.aqa_bar_identities.bar_identity_id"],
            name=op.f("fk_aqa_bar_latest_bar_identity_id_aqa_bar_identities"),
        ),
        sa.PrimaryKeyConstraint("bar_identity_id", name=op.f("pk_aqa_bar_latest")),
        sa.UniqueConstraint("bar_event_id", name=op.f("uq_aqa_bar_latest_bar_event_id")),
        schema="aqa",
    )
    op.create_table(
        "aqa_signal_envelopes",
        sa.Column("signal_id", sa.String(length=128), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source_bar_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "targets",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "reason_codes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_signal_envelopes_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_signal_envelopes_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(signature) = 64", name=op.f("ck_aqa_signal_envelopes_signature_sha256_length")
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=op.f("ck_aqa_signal_envelopes_signal_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "source_bar_end <= emitted_at AND emitted_at < expires_at",
            name=op.f("ck_aqa_signal_envelopes_signal_times_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_signal_envelopes_experiment_hash_aqa_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["slot_id"],
            ["aqa.aqa_decision_slots.slot_id"],
            name=op.f("fk_aqa_signal_envelopes_slot_id_aqa_decision_slots"),
        ),
        sa.PrimaryKeyConstraint("signal_id", name=op.f("pk_aqa_signal_envelopes")),
        sa.UniqueConstraint("content_hash", name=op.f("uq_aqa_signal_envelopes_content_hash")),
        sa.UniqueConstraint("slot_id", name=op.f("uq_aqa_signal_envelopes_slot_id")),
        schema="aqa",
    )
    op.create_table(
        "aqa_symbol_watermarks",
        sa.Column("symbol_watermark_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.Column("adjustment", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("contiguous_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_hash", sa.String(length=64), nullable=False),
        sa.Column("latest_bar_event_id", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_symbol_watermarks_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(quality_hash) = 64",
            name=op.f("ck_aqa_symbol_watermarks_quality_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_aqa_symbol_watermarks_symbol_watermark_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_symbol_watermarks_experiment_hash_aqa_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["latest_bar_event_id"],
            ["aqa.aqa_bar_events.bar_event_id"],
            name=op.f("fk_aqa_symbol_watermarks_latest_bar_event_id_aqa_bar_events"),
        ),
        sa.PrimaryKeyConstraint("symbol_watermark_id", name=op.f("pk_aqa_symbol_watermarks")),
        sa.UniqueConstraint(
            "experiment_hash",
            "provider",
            "feed",
            "adjustment",
            "symbol",
            "timeframe",
            name="symbol_watermark_series",
        ),
        schema="aqa",
    )
    op.create_table(
        "aqa_risk_decisions",
        sa.Column("risk_decision_id", sa.String(length=128), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=False),
        sa.Column("signal_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "proposed_targets",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "approved_targets",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "controls",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "reason_codes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("gross_exposure", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("net_exposure", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("cash_weight", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_risk_decisions_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64", name=op.f("ck_aqa_risk_decisions_input_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_risk_decisions_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(signature) = 64", name=op.f("ck_aqa_risk_decisions_signature_sha256_length")
        ),
        sa.CheckConstraint(
            "policy_version >= 1", name=op.f("ck_aqa_risk_decisions_risk_policy_version_positive")
        ),
        sa.CheckConstraint(
            "CAST(gross_exposure AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(net_exposure AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(cash_weight AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name=op.f("ck_aqa_risk_decisions_financial_values_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_risk_decisions_experiment_hash_aqa_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["aqa.aqa_signal_envelopes.signal_id"],
            name=op.f("fk_aqa_risk_decisions_signal_id_aqa_signal_envelopes"),
        ),
        sa.ForeignKeyConstraint(
            ["slot_id"],
            ["aqa.aqa_decision_slots.slot_id"],
            name=op.f("fk_aqa_risk_decisions_slot_id_aqa_decision_slots"),
        ),
        sa.PrimaryKeyConstraint("risk_decision_id", name=op.f("pk_aqa_risk_decisions")),
        sa.UniqueConstraint("signal_id", name=op.f("uq_aqa_risk_decisions_signal_id")),
        schema="aqa",
    )
    op.create_table(
        "aqa_execution_plans",
        sa.Column("execution_plan_id", sa.String(length=128), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("forced_flat", sa.Boolean(), nullable=False),
        sa.Column(
            "targets",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_execution_plans_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_execution_plans_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(signature) = 64", name=op.f("ck_aqa_execution_plans_signature_sha256_length")
        ),
        sa.CheckConstraint(
            "target_version >= 1",
            name=op.f("ck_aqa_execution_plans_execution_target_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_execution_plans_experiment_hash_aqa_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"],
            ["aqa.aqa_risk_decisions.risk_decision_id"],
            name=op.f("fk_aqa_execution_plans_risk_decision_id_aqa_risk_decisions"),
        ),
        sa.PrimaryKeyConstraint("execution_plan_id", name=op.f("pk_aqa_execution_plans")),
        sa.UniqueConstraint(
            "risk_decision_id", "target_version", name="execution_risk_target_version"
        ),
        schema="aqa",
    )
    op.create_table(
        "aqa_order_intents",
        sa.Column("order_intent_id", sa.String(length=128), nullable=False),
        sa.Column("execution_plan_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("notional", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("reference_price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("time_in_force", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "effect IN ('open', 'close', 'reduce')", name=op.f("ck_aqa_order_intents_order_effect")
        ),
        sa.CheckConstraint(
            "phase IN ('exit', 'entry', 'flatten')", name=op.f("ck_aqa_order_intents_order_phase")
        ),
        sa.CheckConstraint("side IN ('buy', 'sell')", name=op.f("ck_aqa_order_intents_order_side")),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_order_intents_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_order_intents_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "CAST(quantity AS NUMERIC) > 0 AND CAST(notional AS NUMERIC) >= 0 "
            "AND CAST(reference_price AS NUMERIC) > 0",
            name=op.f("ck_aqa_order_intents_order_numbers_valid"),
        ),
        sa.CheckConstraint(
            "CAST(quantity AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(notional AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(reference_price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name=op.f("ck_aqa_order_intents_financial_values_finite"),
        ),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_aqa_order_intents_order_sequence_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["execution_plan_id"],
            ["aqa.aqa_execution_plans.execution_plan_id"],
            name=op.f("fk_aqa_order_intents_execution_plan_id_aqa_execution_plans"),
        ),
        sa.PrimaryKeyConstraint("order_intent_id", name=op.f("pk_aqa_order_intents")),
        sa.UniqueConstraint("client_order_id", name=op.f("uq_aqa_order_intents_client_order_id")),
        sa.UniqueConstraint(
            "execution_plan_id",
            "phase",
            "sequence",
            "symbol",
            name="order_plan_phase_sequence_symbol",
        ),
        schema="aqa",
    )
    op.create_table(
        "aqa_reconciliations",
        sa.Column("reconciliation_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=True),
        sa.Column("execution_plan_id", sa.String(length=128), nullable=True),
        sa.Column("account_id_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column(
            "positions",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "orders",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "discrepancies",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "(status = 'blocking') = blocking",
            name=op.f("ck_aqa_reconciliations_reconciliation_blocking_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('clean', 'blocking')",
            name=op.f("ck_aqa_reconciliations_reconciliation_status"),
        ),
        sa.CheckConstraint(
            "length(account_id_hash) = 64",
            name=op.f("ck_aqa_reconciliations_account_id_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_reconciliations_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name=op.f("ck_aqa_reconciliations_payload_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "started_at <= completed_at",
            name=op.f("ck_aqa_reconciliations_reconciliation_times_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_plan_id"],
            ["aqa.aqa_execution_plans.execution_plan_id"],
            name=op.f("fk_aqa_reconciliations_execution_plan_id_aqa_execution_plans"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_reconciliations_experiment_hash_aqa_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["slot_id"],
            ["aqa.aqa_decision_slots.slot_id"],
            name=op.f("fk_aqa_reconciliations_slot_id_aqa_decision_slots"),
        ),
        sa.PrimaryKeyConstraint("reconciliation_id", name=op.f("pk_aqa_reconciliations")),
        schema="aqa",
    )
    op.create_table(
        "aqa_broker_orders",
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cumulative_filled_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("average_fill_price", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "state IN ('planned', 'submitting', 'accepted', 'partially_filled', 'filled', 'cancel_pending', 'cancelled', 'rejected', 'expired', 'unknown')",
            name=op.f("ck_aqa_broker_orders_broker_order_state"),
        ),
        sa.CheckConstraint(
            "average_fill_price IS NULL OR CAST(average_fill_price AS NUMERIC) > 0",
            name=op.f("ck_aqa_broker_orders_broker_order_average_positive"),
        ),
        sa.CheckConstraint(
            "CAST(cumulative_filled_quantity AS NUMERIC) >= 0",
            name=op.f("ck_aqa_broker_orders_broker_order_filled_nonnegative"),
        ),
        sa.CheckConstraint(
            "CAST(cumulative_filled_quantity AS TEXT) "
            "NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "(average_fill_price IS NULL OR CAST(average_fill_price AS TEXT) "
            "NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name=op.f("ck_aqa_broker_orders_financial_values_finite"),
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0",
            name=op.f("ck_aqa_broker_orders_broker_order_event_sequence_nonnegative"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_aqa_broker_orders_content_hash_sha256_length"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_aqa_broker_orders_broker_order_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["aqa.aqa_order_intents.client_order_id"],
            name=op.f("fk_aqa_broker_orders_client_order_id_aqa_order_intents"),
        ),
        sa.PrimaryKeyConstraint("client_order_id", name=op.f("pk_aqa_broker_orders")),
        sa.UniqueConstraint("broker_order_id", name=op.f("uq_aqa_broker_orders_broker_order_id")),
        schema="aqa",
    )
    op.create_table(
        "aqa_fills",
        sa.Column("fill_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("broker_execution_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fee", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("side IN ('buy', 'sell')", name=op.f("ck_aqa_fills_fill_side")),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_fills_content_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name=op.f("ck_aqa_fills_payload_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "CAST(quantity AS NUMERIC) > 0 AND CAST(price AS NUMERIC) > 0 "
            "AND CAST(fee AS NUMERIC) >= 0",
            name=op.f("ck_aqa_fills_fill_numbers_valid"),
        ),
        sa.CheckConstraint(
            "CAST(quantity AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "CAST(fee AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name=op.f("ck_aqa_fills_financial_values_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["aqa.aqa_broker_orders.client_order_id"],
            name=op.f("fk_aqa_fills_client_order_id_aqa_broker_orders"),
        ),
        sa.PrimaryKeyConstraint("fill_id", name=op.f("pk_aqa_fills")),
        sa.UniqueConstraint("broker_execution_id", name=op.f("uq_aqa_fills_broker_execution_id")),
        schema="aqa",
    )
    op.create_table(
        "aqa_order_events",
        sa.Column("order_event_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("broker_event_id", sa.String(length=128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64", name=op.f("ck_aqa_order_events_content_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name=op.f("ck_aqa_order_events_payload_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_aqa_order_events_order_event_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["aqa.aqa_broker_orders.client_order_id"],
            name=op.f("fk_aqa_order_events_client_order_id_aqa_broker_orders"),
        ),
        sa.PrimaryKeyConstraint("order_event_id", name=op.f("pk_aqa_order_events")),
        sa.UniqueConstraint("broker_event_id", name=op.f("uq_aqa_order_events_broker_event_id")),
        sa.UniqueConstraint("client_order_id", "sequence", name="order_event_sequence"),
        schema="aqa",
    )


def downgrade() -> None:
    """Refuse destructive removal of durable platform history."""

    raise RuntimeError("Destructive downgrade of the generic platform schema is not supported")
