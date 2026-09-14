"""Expose migration metadata without granting access to collector storage."""

from alembic import op

revision = "20260912_0016"
down_revision = "20260906_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE VIEW aqa.aqa_schema_version_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT version_num FROM market_data.alembic_version
    """)
    op.execute("ALTER VIEW aqa.aqa_schema_version_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL ON aqa.aqa_schema_version_v FROM PUBLIC")
    op.execute("""
        GRANT SELECT ON aqa.aqa_schema_version_v
        TO aqa_collector, aqa_scheduler, aqa_strategy, aqa_execution,
           aqa_control, aqa_readonly
    """)


def downgrade() -> None:
    op.execute("DROP VIEW aqa.aqa_schema_version_v")
