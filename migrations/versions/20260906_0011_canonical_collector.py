"""Unify collector intake authority and durable canonical processing.

Revision ID: 20260906_0011
Revises: 20260905_0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260906_0011"
down_revision = "20260905_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET ROLE aqa_migrate")
    op.create_table(
        "canonical_work",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("session_date", sa.Date(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("through_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("generation >= 1", name="ck_canonical_work_generation"),
        schema="market_data",
    )
    op.create_index(
        "ix_canonical_work_session",
        "canonical_work",
        ["session_date", "symbol"],
        schema="market_data",
    )
    op.create_table(
        "collector_configuration",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("universe_hash", sa.String(64), nullable=False),
        sa.Column("experiment_hash", sa.String(64), nullable=False),
        sa.Column("history_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("name = 'canonical'", name="ck_collector_configuration_name"),
        schema="market_data",
    )
    op.execute("GRANT USAGE ON SCHEMA market_data TO aqa_collector")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA market_data TO aqa_collector")
    op.execute(
        "GRANT INSERT ON market_data.collection_universes, market_data.bar_observations, "
        "market_data.ingestion_runs, market_data.current_bars, "
        "market_data.collector_checkpoints, market_data.collector_leases, "
        "market_data.collector_events, market_data.canonical_work, "
        "market_data.collector_configuration TO aqa_collector"
    )
    op.execute(
        "GRANT UPDATE ON market_data.ingestion_runs, market_data.current_bars, "
        "market_data.collector_checkpoints, market_data.collector_leases, "
        "market_data.canonical_work TO aqa_collector"
    )
    op.execute("GRANT DELETE ON market_data.canonical_work TO aqa_collector")
    # Raw evidence and checkpoint deletion remain denied and trigger-guarded.


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade of canonical collector work is not supported")
