"""Expose only fail-closed operational readiness to non-collector workers."""

from alembic import op

revision = "20260906_0015"
down_revision = "20260906_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE VIEW aqa.aqa_operational_readiness_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT b.experiment_hash, b.timeframe, b.role, b.status,
               b.contiguous_through, b.updated_at,
               EXISTS (SELECT 1 FROM market_data.canonical_work) AS pending_work,
               EXISTS (SELECT 1 FROM aqa.aqa_data_gaps g
                       WHERE g.experiment_hash = b.experiment_hash
                         AND g.status IN ('open', 'repairing')) AS unresolved_gaps
        FROM aqa.aqa_basket_watermarks b
    """)
    op.execute("ALTER VIEW aqa.aqa_operational_readiness_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL ON aqa.aqa_operational_readiness_v FROM PUBLIC")
    op.execute(
        "GRANT SELECT ON aqa.aqa_operational_readiness_v TO aqa_scheduler, aqa_strategy, aqa_execution"
    )


def downgrade() -> None:
    op.execute("DROP VIEW aqa.aqa_operational_readiness_v")
