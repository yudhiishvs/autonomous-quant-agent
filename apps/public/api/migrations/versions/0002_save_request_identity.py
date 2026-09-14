"""Bind retried version saves to an owner-scoped request identity."""

from alembic import op

revision = "public_0002"
down_revision = "public_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE aqa_public.strategy_versions ADD COLUMN request_id uuid NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        "CREATE UNIQUE INDEX version_save_request ON aqa_public.strategy_versions (owner_id, request_id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE aqa_public.strategy_versions DROP COLUMN request_id")
