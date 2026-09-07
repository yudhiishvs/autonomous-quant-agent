"""Permit scheduling to observe completed reconciliation without execution authority."""

from alembic import op

revision = "20260906_0012"
down_revision = "20260906_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Grant the existing sanitized read model only."""

    op.execute("GRANT SELECT ON aqa.aqa_reconciliations_v TO aqa_scheduler")
    # Keep the physical constraint name below PostgreSQL's 63-byte identifier limit,
    # so schema drift comparison uses the same name as application metadata.
    op.execute(
        "ALTER TABLE aqa.aqa_reconciliations RENAME CONSTRAINT "
        "ck_aqa_reconciliations_reconciliation_account_observed__bb85 "
        "TO ck_aqa_reconciliations_reconciliation_account_time"
    )


def downgrade() -> None:
    """Remove the read grant without changing durable execution evidence."""

    op.execute("REVOKE SELECT ON aqa.aqa_reconciliations_v FROM aqa_scheduler")
    op.execute(
        "ALTER TABLE aqa.aqa_reconciliations RENAME CONSTRAINT "
        "ck_aqa_reconciliations_reconciliation_account_time "
        "TO ck_aqa_reconciliations_reconciliation_account_observed__bb85"
    )
