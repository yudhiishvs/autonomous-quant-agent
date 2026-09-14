"""Queue canonical projection changes atomically, including direct role-authorized writes."""

from alembic import op

revision = "20260913_0017"
down_revision = "20260912_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION market_data.queue_canonical_projection_change()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog AS $body$
        BEGIN
            INSERT INTO market_data.canonical_work
                (symbol, session_date, generation, through_at)
            SELECT i.symbol, (i.start_at AT TIME ZONE 'UTC')::date, 1, i.end_at
            FROM aqa.aqa_bar_identities i
            JOIN aqa.aqa_bar_events e ON e.bar_event_id = NEW.bar_event_id
                AND e.bar_identity_id = i.bar_identity_id
            WHERE i.bar_identity_id = NEW.bar_identity_id
              AND i.provider = 'alpaca' AND i.feed = 'iex'
              AND i.adjustment = 'raw' AND i.timeframe IN ('1Min', '15Min')
              AND (e.source_mode = 'external_provider' OR EXISTS (
                  SELECT 1 FROM aqa.aqa_bar_events previous
                  WHERE TG_OP = 'UPDATE' AND previous.bar_event_id = OLD.bar_event_id
                    AND previous.source_mode = 'external_provider'
              ))
            ON CONFLICT (symbol, session_date) DO UPDATE
                SET generation = market_data.canonical_work.generation + 1,
                    through_at = GREATEST(market_data.canonical_work.through_at, EXCLUDED.through_at);
            RETURN NEW;
        END
        $body$
    """)
    op.execute("REVOKE ALL ON FUNCTION market_data.queue_canonical_projection_change() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION market_data.queue_canonical_projection_change() TO aqa_collector"
    )
    op.execute("""
        CREATE TRIGGER canonical_projection_invalidates_readiness
        AFTER INSERT OR UPDATE ON aqa.aqa_bar_latest
        FOR EACH ROW EXECUTE FUNCTION market_data.queue_canonical_projection_change()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER canonical_projection_invalidates_readiness ON aqa.aqa_bar_latest")
    op.execute("DROP FUNCTION market_data.queue_canonical_projection_change()")
