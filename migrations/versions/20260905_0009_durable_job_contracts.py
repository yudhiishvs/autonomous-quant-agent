"""Install durable bounded jobs and transactional outbox contracts.

Revision ID: 20260905_0009
Revises: 20260905_0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0009"
down_revision: str | None = "20260905_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_PROVISIONAL_JOB_GUARD = sa.text(
    """
    DO $aqa_durable_job_contract_guard$
    BEGIN
        IF EXISTS (SELECT 1 FROM aqa.aqa_jobs LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_job_attempts LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_outbox_events LIMIT 1) THEN
            RAISE EXCEPTION
                'revision 20260905_0009 requires an explicit durable-job backfill '
                'for existing rows';
        END IF;
    END
    $aqa_durable_job_contract_guard$
    """
)


def upgrade() -> None:
    """Replace unused provisional relations with the durable job ledger."""

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("revision 20260905_0009 requires PostgreSQL")
    op.execute("SET ROLE aqa_migrate")
    op.execute(
        "GRANT SELECT, UPDATE ON TABLE aqa.aqa_jobs, aqa.aqa_job_attempts, "
        "aqa.aqa_outbox_events TO aqa_migrate"
    )
    op.execute(
        "LOCK TABLE aqa.aqa_jobs, aqa.aqa_job_attempts, "
        "aqa.aqa_outbox_events IN ACCESS EXCLUSIVE MODE"
    )
    connection.execute(_EMPTY_PROVISIONAL_JOB_GUARD)
    _drop_provisional_contract()
    _create_jobs()
    _create_job_attempts()
    _create_outbox_events()
    _create_safe_view_and_grants()


def _drop_provisional_contract() -> None:
    op.execute("DROP VIEW aqa.aqa_jobs_v")
    op.drop_table("aqa_job_attempts", schema="aqa")
    op.drop_table("aqa_outbox_events", schema="aqa")
    op.drop_table("aqa_jobs", schema="aqa")


def _create_jobs() -> None:
    op.create_table(
        "aqa_jobs",
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_last_error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_last_error_message", sa.String(length=256), nullable=True),
        sa.Column("result_artifact_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "job_type IN ('DATA_QUALITY_AUDIT', 'GAP_REPAIR', 'DATASET_FREEZE', 'OFFLINE_DEMO')",
            name=op.f("ck_aqa_jobs_job_type_allowlist"),
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEAD', 'CANCELED')",
            name=op.f("ck_aqa_jobs_job_state"),
        ),
        sa.CheckConstraint(
            "schema_version = 1",
            name=op.f("ck_aqa_jobs_job_schema_version"),
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name=op.f("ck_aqa_jobs_job_attempt_count"),
        ),
        sa.CheckConstraint(
            "max_attempts = 3",
            name=op.f("ck_aqa_jobs_job_max_attempts"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_aqa_jobs_job_version_positive"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_aqa_jobs_job_timestamps_monotonic"),
        ),
        sa.CheckConstraint(
            "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
            name=op.f("ck_aqa_jobs_job_retry_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name=op.f("ck_aqa_jobs_job_lease_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "claimed_at IS NULL OR (claimed_at >= created_at AND claimed_at <= updated_at)",
            name=op.f("ck_aqa_jobs_job_claim_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR (started_at >= claimed_at AND started_at <= updated_at)",
            name=op.f("ck_aqa_jobs_job_start_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at = updated_at",
            name=op.f("ck_aqa_jobs_job_completion_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "((state IN ('CLAIMED', 'RUNNING') AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL) OR "
            "(state NOT IN ('CLAIMED', 'RUNNING') AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND claimed_at IS NULL))",
            name=op.f("ck_aqa_jobs_job_lease_consistent"),
        ),
        sa.CheckConstraint(
            "((state = 'RUNNING' AND started_at IS NOT NULL) OR "
            "(state <> 'RUNNING' AND started_at IS NULL))",
            name=op.f("ck_aqa_jobs_job_started_consistent"),
        ),
        sa.CheckConstraint(
            "((state IN ('SUCCEEDED', 'DEAD', 'CANCELED') AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('SUCCEEDED', 'DEAD', 'CANCELED') AND completed_at IS NULL))",
            name=op.f("ck_aqa_jobs_job_completed_consistent"),
        ),
        sa.CheckConstraint(
            "((state IN ('PENDING', 'FAILED') AND next_attempt_at IS NOT NULL) OR "
            "(state NOT IN ('PENDING', 'FAILED') AND next_attempt_at IS NULL))",
            name=op.f("ck_aqa_jobs_job_retry_time_consistent"),
        ),
        sa.CheckConstraint(
            "((safe_last_error_code IS NULL) = (safe_last_error_message IS NULL))",
            name=op.f("ck_aqa_jobs_job_error_pair"),
        ),
        sa.CheckConstraint(
            "result_artifact_id IS NULL OR "
            "(result_artifact_id NOT LIKE '%/%' AND result_artifact_id NOT LIKE '%..%')",
            name=op.f("ck_aqa_jobs_job_result_artifact_shape"),
        ),
        _hash_check("aqa_jobs", "payload_hash"),
        _hash_check("aqa_jobs", "content_hash"),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_aqa_jobs")),
        sa.UniqueConstraint("job_type", "idempotency_key", name="job_type_idempotency"),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_jobs_claim",
        "aqa_jobs",
        ["state", "next_attempt_at", "created_at", "job_id"],
        schema="aqa",
    )


def _create_job_attempts() -> None:
    op.create_table(
        "aqa_job_attempts",
        sa.Column("job_attempt_event_id", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("transition", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_error_message", sa.String(length=256), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "attempt_number BETWEEN 1 AND 3",
            name=op.f("ck_aqa_job_attempts_job_attempt_number"),
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 1 AND 4",
            name=op.f("ck_aqa_job_attempts_job_attempt_event_sequence"),
        ),
        sa.CheckConstraint(
            "transition IN ('CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ABANDONED')",
            name=op.f("ck_aqa_job_attempts_job_attempt_transition"),
        ),
        sa.CheckConstraint(
            "((transition = 'CLAIMED' AND lease_expires_at IS NOT NULL) OR "
            "(transition <> 'CLAIMED' AND lease_expires_at IS NULL))",
            name=op.f("ck_aqa_job_attempts_job_attempt_lease_consistent"),
        ),
        sa.CheckConstraint(
            "((safe_error_code IS NULL) = (safe_error_message IS NULL))",
            name=op.f("ck_aqa_job_attempts_job_attempt_error_pair"),
        ),
        _hash_check("aqa_job_attempts", "content_hash"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["aqa.aqa_jobs.job_id"],
            name=op.f("fk_aqa_job_attempts_job_id_aqa_jobs"),
        ),
        sa.PrimaryKeyConstraint(
            "job_attempt_event_id",
            name=op.f("pk_aqa_job_attempts"),
        ),
        sa.UniqueConstraint(
            "job_id",
            "attempt_number",
            "sequence",
            name="job_attempt_sequence",
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_job_attempts_job_attempt",
        "aqa_job_attempts",
        ["job_id", "attempt_number", "sequence"],
        schema="aqa",
    )


def _create_outbox_events() -> None:
    op.create_table(
        "aqa_outbox_events",
        sa.Column("outbox_event_id", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_last_error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_last_error_message", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "aggregate_type = 'job'",
            name=op.f("ck_aqa_outbox_events_outbox_aggregate_type"),
        ),
        sa.CheckConstraint(
            "schema_version = 1",
            name=op.f("ck_aqa_outbox_events_outbox_schema_version"),
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'CLAIMED', 'PUBLISHED', 'FAILED', 'DEAD')",
            name=op.f("ck_aqa_outbox_events_outbox_state"),
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name=op.f("ck_aqa_outbox_events_outbox_attempt_count"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_aqa_outbox_events_outbox_version_positive"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_aqa_outbox_events_outbox_timestamps_monotonic"),
        ),
        sa.CheckConstraint(
            "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
            name=op.f("ck_aqa_outbox_events_outbox_retry_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name=op.f("ck_aqa_outbox_events_outbox_lease_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at = updated_at",
            name=op.f("ck_aqa_outbox_events_outbox_publish_timestamp_monotonic"),
        ),
        sa.CheckConstraint(
            "((state = 'CLAIMED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'CLAIMED' AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name=op.f("ck_aqa_outbox_events_outbox_lease_consistent"),
        ),
        sa.CheckConstraint(
            "((state = 'PUBLISHED' AND published_at IS NOT NULL) OR "
            "(state <> 'PUBLISHED' AND published_at IS NULL))",
            name=op.f("ck_aqa_outbox_events_outbox_publication_consistent"),
        ),
        sa.CheckConstraint(
            "((state IN ('PENDING', 'FAILED') AND next_attempt_at IS NOT NULL) OR "
            "(state NOT IN ('PENDING', 'FAILED') AND next_attempt_at IS NULL))",
            name=op.f("ck_aqa_outbox_events_outbox_retry_time_consistent"),
        ),
        sa.CheckConstraint(
            "((safe_last_error_code IS NULL) = (safe_last_error_message IS NULL))",
            name=op.f("ck_aqa_outbox_events_outbox_error_pair"),
        ),
        _hash_check("aqa_outbox_events", "payload_hash"),
        _hash_check("aqa_outbox_events", "content_hash"),
        sa.ForeignKeyConstraint(
            ["aggregate_id"],
            ["aqa.aqa_jobs.job_id"],
            name=op.f("fk_aqa_outbox_events_aggregate_id_aqa_jobs"),
        ),
        sa.PrimaryKeyConstraint(
            "outbox_event_id",
            name=op.f("pk_aqa_outbox_events"),
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_outbox_delivery",
        "aqa_outbox_events",
        ["state", "next_attempt_at", "created_at", "outbox_event_id"],
        schema="aqa",
    )


def _hash_check(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column}) = 64",
        name=op.f(f"ck_{table}_{column}_sha256_length"),
    )


def _create_safe_view_and_grants() -> None:
    op.execute(
        """
        CREATE VIEW aqa.aqa_jobs_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT job_id, job_type, state, attempt_count, max_attempts,
               next_attempt_at, lease_expires_at, completed_at,
               safe_last_error_code, safe_last_error_message,
               result_artifact_id, created_at, updated_at, version
        FROM aqa.aqa_jobs
        """
    )
    for table in ("aqa_jobs", "aqa_job_attempts", "aqa_outbox_events"):
        op.execute(f"ALTER TABLE aqa.{table} OWNER TO aqa_migrate")
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE aqa.{table} FROM PUBLIC, "
            "aqa_collector, aqa_scheduler, aqa_strategy, aqa_execution, "
            "aqa_control, aqa_readonly"
        )
        op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE aqa.{table} FROM aqa_migrate")
    op.execute("ALTER VIEW aqa.aqa_jobs_v OWNER TO aqa_migrate")
    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE aqa.aqa_jobs_v FROM PUBLIC, "
        "aqa_collector, aqa_scheduler, aqa_strategy, aqa_execution, "
        "aqa_control, aqa_readonly"
    )
    op.execute("REVOKE ALL PRIVILEGES ON TABLE aqa.aqa_risk_latch_events FROM aqa_control")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE aqa.aqa_jobs, aqa.aqa_outbox_events TO aqa_control"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE aqa.aqa_job_attempts TO aqa_control")
    op.execute("GRANT SELECT, INSERT ON TABLE aqa.aqa_risk_latch_events TO aqa_control")
    op.execute("GRANT SELECT ON TABLE aqa.aqa_jobs_v TO aqa_control, aqa_readonly")
    op.execute("GRANT UPDATE (created_at) ON TABLE aqa.aqa_jobs TO aqa_migrate")


def downgrade() -> None:
    """Refuse removal of durable job and outbox evidence."""

    raise RuntimeError("Destructive downgrade of durable-job contracts is not supported")
