"""Install durable scheduling and strict signal-envelope contracts.

Revision ID: 20260905_0006
Revises: 20260905_0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0006"
down_revision: str | None = "20260905_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_LEGACY_CONTRACT_GUARD = sa.text(
    """
    DO $aqa_scheduler_signal_contract_guard$
    BEGIN
        IF EXISTS (SELECT 1 FROM aqa.aqa_decision_slots LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_signal_envelopes LIMIT 1) THEN
            RAISE EXCEPTION
                'revision 20260905_0006 requires an explicit scheduler/signal backfill '
                'for existing rows';
        END IF;
    END
    $aqa_scheduler_signal_contract_guard$
    """
)


def upgrade() -> None:
    """Replace unused provisional columns with the exact Phase 4 contracts."""

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("revision 20260905_0006 requires PostgreSQL")
    connection.execute(_EMPTY_LEGACY_CONTRACT_GUARD)

    op.execute("DROP VIEW aqa.aqa_signals_v")
    op.execute("DROP VIEW aqa.aqa_decision_slots_v")

    op.drop_index("ix_aqa_decision_slots_claim", table_name="aqa_decision_slots", schema="aqa")
    for constraint_name in (
        "ck_aqa_decision_slots_decision_claim_consistent",
        "ck_aqa_decision_slots_decision_state",
        "ck_aqa_decision_slots_decision_deadlines_ordered",
        "decision_source_type",
    ):
        op.drop_constraint(
            constraint_name,
            "aqa_decision_slots",
            schema="aqa",
            type_="unique" if constraint_name == "decision_source_type" else "check",
        )

    op.alter_column(
        "aqa_decision_slots", "source_bar_end", new_column_name="source_interval_end", schema="aqa"
    )
    op.alter_column("aqa_decision_slots", "scheduled_at", new_column_name="ready_at", schema="aqa")
    op.alter_column(
        "aqa_decision_slots", "signal_deadline_at", new_column_name="deadline_at", schema="aqa"
    )
    op.alter_column(
        "aqa_decision_slots",
        "execution_deadline_at",
        new_column_name="required_completion_at",
        schema="aqa",
    )
    op.drop_column("aqa_decision_slots", "data_deadline_at", schema="aqa")
    for column in (
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_version", sa.BigInteger(), nullable=False),
        sa.Column("signal_provider_id", sa.String(length=64), nullable=False),
        sa.Column("signal_provider_version", sa.String(length=64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("source_interval_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
    ):
        op.add_column("aqa_decision_slots", column, schema="aqa")

    op.create_unique_constraint(
        "decision_source_type",
        "aqa_decision_slots",
        ["experiment_hash", "source_interval_end", "decision_type"],
        schema="aqa",
    )
    op.create_unique_constraint(
        op.f("uq_aqa_decision_slots_correlation_id"),
        "aqa_decision_slots",
        ["correlation_id"],
        schema="aqa",
    )
    _create_decision_checks()
    op.create_index(
        "ix_aqa_decision_slots_claim",
        "aqa_decision_slots",
        ["state", "ready_at"],
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_decision_slots_session",
        "aqa_decision_slots",
        ["experiment_hash", "session_date", "ready_at"],
        schema="aqa",
    )

    for constraint_name in (
        "ck_aqa_signal_envelopes_payload_hash_sha256_length",
        "ck_aqa_signal_envelopes_signature_sha256_length",
        "ck_aqa_signal_envelopes_signal_schema_version_positive",
        "ck_aqa_signal_envelopes_signal_times_ordered",
    ):
        op.drop_constraint(
            constraint_name,
            "aqa_signal_envelopes",
            schema="aqa",
            type_="check",
        )
    op.alter_column(
        "aqa_signal_envelopes",
        "schema_version",
        new_column_name="contract_version",
        schema="aqa",
    )
    op.alter_column(
        "aqa_signal_envelopes", "emitted_at", new_column_name="created_at", schema="aqa"
    )
    for column_name in ("targets", "reason_codes", "payload_hash", "signature"):
        op.drop_column("aqa_signal_envelopes", column_name, schema="aqa")
    for column in (
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("provider_source_mode", sa.String(length=32), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_version", sa.BigInteger(), nullable=False),
        sa.Column("data_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("active_symbols", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("availability_mask", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_edge_bps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "proposed_signed_target_inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("artifact_id", sa.String(length=128), nullable=True),
        sa.Column("artifact_hash", sa.String(length=64), nullable=True),
        sa.Column("promotable", sa.Boolean(), nullable=False),
        sa.Column("paper_submission_eligible", sa.Boolean(), nullable=False),
    ):
        op.add_column("aqa_signal_envelopes", column, schema="aqa")
    _create_signal_checks()

    _create_read_views()


def _create_decision_checks() -> None:
    checks = (
        (
            "ck_aqa_decision_slots_decision_state",
            "state IN ('PENDING', 'WAITING_FOR_DATA', 'READY', 'CLAIMED', 'COMPLETED', "
            "'SKIPPED', 'EXPIRED', 'FAILED', 'FLATTEN_REQUIRED')",
        ),
        (
            "ck_aqa_decision_slots_decision_claim_consistent",
            "((state = 'CLAIMED' AND claim_owner IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR (state <> 'CLAIMED' AND claim_owner IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL))",
        ),
        (
            "ck_aqa_decision_slots_decision_completion_consistent",
            "((state IN ('COMPLETED', 'SKIPPED', 'EXPIRED', 'FAILED') "
            "AND completed_at IS NOT NULL) OR (state NOT IN "
            "('COMPLETED', 'SKIPPED', 'EXPIRED', 'FAILED') AND completed_at IS NULL))",
        ),
        (
            "ck_aqa_decision_slots_decision_reason_consistent",
            "state NOT IN ('WAITING_FOR_DATA', 'SKIPPED', 'EXPIRED', 'FAILED') "
            "OR reason_code IS NOT NULL",
        ),
        (
            "ck_aqa_decision_slots_decision_experiment_version_positive",
            "experiment_version >= 1",
        ),
        (
            "ck_aqa_decision_slots_decision_deadlines_ordered",
            "source_interval_start < source_interval_end "
            "AND source_interval_end <= ready_at AND ready_at < deadline_at "
            "AND deadline_at <= required_completion_at",
        ),
    )
    for name, expression in checks:
        op.create_check_constraint(op.f(name), "aqa_decision_slots", expression, schema="aqa")


def _create_signal_checks() -> None:
    checks = (
        (
            "ck_aqa_signal_envelopes_signal_contract_version_positive",
            "contract_version >= 1",
        ),
        (
            "ck_aqa_signal_envelopes_signal_experiment_version_positive",
            "experiment_version >= 1",
        ),
        (
            "ck_aqa_signal_envelopes_signal_source_mode",
            "provider_source_mode IN ('builtin', 'offline_fixture', 'registered_plugin')",
        ),
        (
            "ck_aqa_signal_envelopes_signal_times_ordered",
            "source_bar_end <= created_at AND created_at < expires_at",
        ),
        (
            "ck_aqa_signal_envelopes_signal_artifact_consistent",
            "(artifact_id IS NULL AND artifact_hash IS NULL) "
            "OR (artifact_id IS NOT NULL AND artifact_hash IS NOT NULL)",
        ),
        (
            "ck_aqa_signal_envelopes_data_contract_hash_sha256_length",
            "length(data_contract_hash) = 64",
        ),
        (
            "ck_aqa_signal_envelopes_policy_hash_sha256_length",
            "length(policy_hash) = 64",
        ),
        (
            "ck_aqa_signal_envelopes_signal_artifact_hash_sha256_length",
            "artifact_hash IS NULL OR length(artifact_hash) = 64",
        ),
    )
    for name, expression in checks:
        op.create_check_constraint(op.f(name), "aqa_signal_envelopes", expression, schema="aqa")


def _create_read_views() -> None:
    op.execute(
        """
        CREATE VIEW aqa.aqa_decision_slots_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT slot_id, experiment_id, experiment_version, experiment_hash,
               signal_provider_id, signal_provider_version, session_date,
               source_interval_start, source_interval_end, decision_type, ready_at,
               deadline_at, required_completion_at, state, claim_owner, claimed_at,
               lease_expires_at, attempt_count, completed_at, reason_code, correlation_id,
               version, created_at, updated_at
        FROM aqa.aqa_decision_slots
        """
    )
    op.execute("ALTER VIEW aqa.aqa_decision_slots_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_decision_slots_v FROM PUBLIC")
    op.execute(
        "GRANT SELECT ON aqa.aqa_decision_slots_v TO aqa_strategy, aqa_control, aqa_readonly"
    )
    op.execute(
        """
        CREATE VIEW aqa.aqa_signals_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT signal_id, slot_id, correlation_id, provider_id, provider_version,
               provider_source_mode, experiment_id, experiment_version, experiment_hash,
               data_contract_hash, policy_hash, contract_version, source_bar_end, created_at,
               expires_at, active_symbols, availability_mask, actions, expected_edge_bps,
               proposed_signed_target_inputs, artifact_id, artifact_hash, promotable,
               paper_submission_eligible, content_hash
        FROM aqa.aqa_signal_envelopes
        """
    )
    op.execute("ALTER VIEW aqa.aqa_signals_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_signals_v FROM PUBLIC")
    op.execute("GRANT SELECT ON aqa.aqa_signals_v TO aqa_control, aqa_readonly")


def downgrade() -> None:
    """Refuse removal of durable scheduling and signal contracts."""

    raise RuntimeError("Destructive downgrade of scheduler/signal contracts is not supported")
