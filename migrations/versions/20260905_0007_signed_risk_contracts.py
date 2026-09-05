"""Install signed-risk decisions and restart-safe latch evidence.

Revision ID: 20260905_0007
Revises: 20260905_0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0007"
down_revision: str | None = "20260905_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_RISK_CONTRACT_GUARD = sa.text(
    """
    DO $aqa_signed_risk_contract_guard$
    BEGIN
        IF EXISTS (SELECT 1 FROM aqa.aqa_risk_latch_events LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_risk_decisions LIMIT 1) THEN
            RAISE EXCEPTION
                'revision 20260905_0007 requires an explicit signed-risk backfill '
                'for existing rows';
        END IF;
    END
    $aqa_signed_risk_contract_guard$
    """
)


def upgrade() -> None:
    """Replace the unused provisional risk contract with signed-risk evidence."""

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("revision 20260905_0007 requires PostgreSQL")
    connection.execute(_EMPTY_RISK_CONTRACT_GUARD)

    op.drop_constraint(
        "ck_aqa_risk_latch_events_risk_latch_action",
        "aqa_risk_latch_events",
        schema="aqa",
        type_="check",
    )
    for column in (
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
    ):
        op.add_column("aqa_risk_latch_events", column, schema="aqa")
    op.create_unique_constraint(
        "risk_latch_idempotency",
        "aqa_risk_latch_events",
        ["experiment_hash", "latch_type", "idempotency_key"],
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_risk_latch_events_risk_latch_action"),
        "aqa_risk_latch_events",
        "action IN ('ENGAGED', 'CLEARED')",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_risk_latch_events_risk_latch_type"),
        "aqa_risk_latch_events",
        "latch_type IN ('deployment_drawdown', 'operator_halt', 'reconciliation', 'session_loss')",
        schema="aqa",
    )

    for column in (
        sa.Column("signal_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("statistics_hash", sa.String(length=64), nullable=False),
        sa.Column("latch_state_hash", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("execution_scope", sa.String(length=24), nullable=False),
        sa.Column("original_proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("before_exposure", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_exposure", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_timestamps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active_latches", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "required_latch_event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    ):
        op.add_column("aqa_risk_decisions", column, schema="aqa")
    op.alter_column(
        "aqa_risk_decisions",
        "signature",
        existing_type=sa.String(length=64),
        nullable=True,
        schema="aqa",
    )
    op.drop_constraint(
        "ck_aqa_risk_decisions_signature_sha256_length",
        "aqa_risk_decisions",
        schema="aqa",
        type_="check",
    )
    op.create_unique_constraint(
        op.f("uq_aqa_risk_decisions_content_hash"),
        "aqa_risk_decisions",
        ["content_hash"],
        schema="aqa",
    )
    _create_risk_checks()
    _replace_risk_views()


def _create_risk_checks() -> None:
    checks = (
        (
            "ck_aqa_risk_decisions_risk_execution_scope",
            "execution_scope IN ('FULL', 'NONE', 'RISK_REDUCING_ONLY')",
        ),
        (
            "ck_aqa_risk_decisions_signal_hash_sha256_length",
            "length(signal_hash) = 64",
        ),
        (
            "ck_aqa_risk_decisions_policy_hash_sha256_length",
            "length(policy_hash) = 64",
        ),
        (
            "ck_aqa_risk_decisions_statistics_hash_sha256_length",
            "length(statistics_hash) = 64",
        ),
        (
            "ck_aqa_risk_decisions_latch_state_hash_sha256_length",
            "length(latch_state_hash) = 64",
        ),
        (
            "ck_aqa_risk_decisions_risk_signature_sha256_length",
            "signature IS NULL OR length(signature) = 64",
        ),
    )
    for name, expression in checks:
        op.create_check_constraint(op.f(name), "aqa_risk_decisions", expression, schema="aqa")


def _replace_risk_views() -> None:
    op.execute("DROP VIEW aqa.aqa_risk_latches_v")
    op.execute(
        """
        CREATE VIEW aqa.aqa_risk_latches_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT latch_event_id, experiment_hash, latch_type, sequence, action,
               correlation_id, idempotency_key, reason_code, actor, occurred_at,
               payload_hash, content_hash
        FROM aqa.aqa_risk_latch_events
        """
    )
    op.execute("ALTER VIEW aqa.aqa_risk_latches_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_risk_latches_v FROM PUBLIC")
    op.execute("GRANT SELECT ON aqa.aqa_risk_latches_v TO aqa_control, aqa_readonly")

    op.execute("DROP VIEW aqa.aqa_risk_decisions_v")
    op.execute(
        """
        CREATE VIEW aqa.aqa_risk_decisions_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT risk_decision_id, slot_id, signal_id, experiment_hash, policy_id,
               policy_version, decided_at, signal_hash, policy_hash, statistics_hash,
               latch_state_hash, correlation_id, execution_scope, proposed_targets,
               approved_targets, before_exposure, after_exposure, controls, reason_codes,
               active_latches, required_latch_event_ids, gross_exposure, net_exposure,
               cash_weight, content_hash
        FROM aqa.aqa_risk_decisions
        """
    )
    op.execute("ALTER VIEW aqa.aqa_risk_decisions_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_risk_decisions_v FROM PUBLIC")
    op.execute("GRANT SELECT ON aqa.aqa_risk_decisions_v TO aqa_control, aqa_readonly")


def downgrade() -> None:
    """Refuse removal of signed-risk evidence."""

    raise RuntimeError("Destructive downgrade of signed-risk contracts is not supported")
