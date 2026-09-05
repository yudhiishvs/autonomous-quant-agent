"""Complete canonical market-data provenance and readiness state.

Revision ID: 20260905_0005
Revises: 20260905_0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260905_0005"
down_revision: str | None = "20260905_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_EVENT_HISTORY_GUARD = sa.text(
    """
    DO $aqa_market_data_contract_guard$
    BEGIN
        IF EXISTS (SELECT 1 FROM aqa.aqa_bar_events LIMIT 1) THEN
            RAISE EXCEPTION
                'revision 20260905_0005 requires an explicit canonical-provenance backfill '
                'for existing platform bar events';
        END IF;
    END
    $aqa_market_data_contract_guard$
    """
)


def upgrade() -> None:
    """Install the lossless canonical-bar and blocked-basket contracts."""

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("revision 20260905_0005 requires PostgreSQL")
    connection.execute(_EMPTY_EVENT_HISTORY_GUARD)

    op.add_column(
        "aqa_bar_events",
        sa.Column(
            "source_mode",
            sa.String(length=32),
            nullable=False,
            server_default="offline_fixture",
        ),
        schema="aqa",
    )
    op.add_column(
        "aqa_bar_events",
        sa.Column("is_correction", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="aqa",
    )
    op.add_column(
        "aqa_bar_events",
        sa.Column("correction_of_source_event_id", sa.String(length=128)),
        schema="aqa",
    )
    op.alter_column("aqa_bar_events", "source_mode", server_default=None, schema="aqa")
    op.alter_column("aqa_bar_events", "is_correction", server_default=None, schema="aqa")
    op.drop_constraint(
        "ck_aqa_bar_events_bar_vwap_positive",
        "aqa_bar_events",
        schema="aqa",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_bar_vwap_nonnegative"),
        "aqa_bar_events",
        "vwap IS NULL OR CAST(vwap AS NUMERIC) >= 0",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_bar_source_mode"),
        "aqa_bar_events",
        "source_mode IN ('external_provider', 'offline_fixture')",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_bar_source_correction_consistent"),
        "aqa_bar_events",
        "correction_of_source_event_id IS NULL OR is_correction",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_bar_source_correction_not_self"),
        "aqa_bar_events",
        "correction_of_source_event_id IS NULL OR source_event_id IS NULL "
        "OR correction_of_source_event_id <> source_event_id",
        schema="aqa",
    )

    op.add_column(
        "aqa_basket_watermarks",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="ready",
        ),
        schema="aqa",
    )
    op.alter_column("aqa_basket_watermarks", "status", server_default=None, schema="aqa")
    op.alter_column(
        "aqa_basket_watermarks",
        "contiguous_through",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_basket_watermarks_basket_watermark_status"),
        "aqa_basket_watermarks",
        "status IN ('ready', 'blocked')",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_basket_watermarks_basket_watermark_readiness_consistent"),
        "aqa_basket_watermarks",
        "(status = 'ready' AND contiguous_through IS NOT NULL) "
        "OR (status = 'blocked' AND contiguous_through IS NULL)",
        schema="aqa",
    )

    op.execute("DROP VIEW aqa.aqa_effective_bars_v")
    op.execute(
        """
        CREATE VIEW aqa.aqa_effective_bars_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT
            identity.bar_identity_id,
            identity.provider,
            identity.feed,
            identity.adjustment,
            identity.symbol,
            identity.timeframe,
            identity.start_at,
            identity.end_at,
            event.bar_event_id,
            event.revision,
            event.schema_version,
            event.provider_timestamp,
            event.received_at,
            event.open,
            event.high,
            event.low,
            event.close,
            event.volume,
            event.trade_count,
            event.vwap,
            event.quality_flags,
            event.source,
            event.source_mode,
            event.source_event_id,
            event.is_correction,
            event.correction_of_source_event_id,
            event.source_payload_hash,
            event.lineage_hash,
            event.normalized_payload_hash,
            event.correction_of_event_id,
            event.content_hash,
            latest.version AS projection_version,
            latest.projected_at
        FROM aqa.aqa_bar_latest AS latest
        JOIN aqa.aqa_bar_identities AS identity
          ON identity.bar_identity_id = latest.bar_identity_id
        JOIN aqa.aqa_bar_events AS event
          ON event.bar_identity_id = latest.bar_identity_id
         AND event.bar_event_id = latest.bar_event_id
         AND event.revision = latest.revision
        """
    )
    op.execute("ALTER VIEW aqa.aqa_effective_bars_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_effective_bars_v FROM PUBLIC")
    op.execute(
        "GRANT SELECT ON aqa.aqa_effective_bars_v TO aqa_strategy, aqa_control, aqa_readonly"
    )

    op.execute("DROP VIEW aqa.aqa_basket_watermarks_v")
    op.execute(
        """
        CREATE VIEW aqa.aqa_basket_watermarks_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT basket_watermark_id, experiment_hash, role, timeframe, status,
               contiguous_through, component_hash, version, updated_at
        FROM aqa.aqa_basket_watermarks
        """
    )
    op.execute("ALTER VIEW aqa.aqa_basket_watermarks_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_basket_watermarks_v FROM PUBLIC")
    op.execute(
        "GRANT SELECT ON aqa.aqa_basket_watermarks_v "
        "TO aqa_scheduler, aqa_strategy, aqa_control, aqa_readonly"
    )


def downgrade() -> None:
    """Refuse removal of canonical provenance or durable blocked readiness."""

    raise RuntimeError("Destructive downgrade of market-data contracts is not supported")
