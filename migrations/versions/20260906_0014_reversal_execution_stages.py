"""Preserve independently authorized reversal stages and final slot completion evidence."""

import sqlalchemy as sa
from alembic import op

revision = "20260906_0014"
down_revision = "20260906_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "aqa_risk_decisions",
        sa.Column("execution_stage", sa.BigInteger(), nullable=False, server_default="1"),
        schema="aqa",
    )
    op.add_column(
        "aqa_risk_decisions", sa.Column("preceding_reconciliation_id", sa.String(128)), schema="aqa"
    )
    op.drop_constraint(
        "uq_aqa_risk_decisions_signal_id", "aqa_risk_decisions", schema="aqa", type_="unique"
    )
    op.create_unique_constraint(
        "risk_signal_execution_stage",
        "aqa_risk_decisions",
        ["signal_id", "execution_stage"],
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_risk_decisions_risk_execution_stage"),
        "aqa_risk_decisions",
        "(execution_stage = 1 AND preceding_reconciliation_id IS NULL) OR (execution_stage = 2 AND preceding_reconciliation_id IS NOT NULL)",
        schema="aqa",
    )
    op.execute("""
        CREATE VIEW aqa.aqa_execution_completion_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT r.slot_id, r.status, r.completed_at
        FROM aqa.aqa_reconciliations r
        JOIN aqa.aqa_execution_plans p USING (execution_plan_id)
        WHERE r.status <> 'CLEAN' OR NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(p.targets) t,
                          jsonb_array_elements(p.current_positions) c
            WHERE t->>0 = c->>0 AND (t->>1)::numeric * (c->>1)::numeric < 0
        )
    """)
    op.execute("GRANT SELECT ON aqa.aqa_execution_completion_v TO aqa_scheduler")
    op.execute(
        "GRANT SELECT ON aqa.aqa_signals_v, aqa.aqa_decision_slots_v, aqa.aqa_effective_bars_v TO aqa_execution"
    )


def downgrade() -> None:
    # Multiple immutable stages cannot be collapsed without destroying authorization evidence.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM aqa.aqa_risk_decisions WHERE execution_stage <> 1) THEN
                RAISE EXCEPTION 'reversal stage evidence prevents downgrade';
            END IF;
        END $$
    """)
    op.execute(
        "REVOKE SELECT ON aqa.aqa_signals_v, aqa.aqa_decision_slots_v, aqa.aqa_effective_bars_v FROM aqa_execution"
    )
    op.execute("DROP VIEW aqa.aqa_execution_completion_v")
    op.drop_constraint(
        op.f("ck_aqa_risk_decisions_risk_execution_stage"),
        "aqa_risk_decisions",
        schema="aqa",
        type_="check",
    )
    op.drop_constraint(
        "risk_signal_execution_stage", "aqa_risk_decisions", schema="aqa", type_="unique"
    )
    op.create_unique_constraint(
        "uq_aqa_risk_decisions_signal_id", "aqa_risk_decisions", ["signal_id"], schema="aqa"
    )
    op.drop_column("aqa_risk_decisions", "preceding_reconciliation_id", schema="aqa")
    op.drop_column("aqa_risk_decisions", "execution_stage", schema="aqa")
