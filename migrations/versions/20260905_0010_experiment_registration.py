"""Add the immutable deployment experiment-registration boundary.

Revision ID: 20260905_0010
Revises: 20260905_0009
"""

from __future__ import annotations

from alembic import op

revision = "20260905_0010"
down_revision = "20260905_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Permit only append-only deployment metadata registration."""

    op.execute("SET ROLE aqa_migrate")
    op.execute(
        """
        CREATE FUNCTION aqa.reject_experiment_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'registered experiment metadata is immutable'
                USING ERRCODE = '55000';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER aqa_experiments_are_immutable
        BEFORE UPDATE OR DELETE ON aqa.aqa_experiments
        FOR EACH ROW EXECUTE FUNCTION aqa.reject_experiment_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER aqa_experiment_symbols_are_immutable
        BEFORE UPDATE OR DELETE ON aqa.aqa_experiment_symbols
        FOR EACH ROW EXECUTE FUNCTION aqa.reject_experiment_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER aqa_experiments_reject_truncate
        BEFORE TRUNCATE ON aqa.aqa_experiments
        FOR EACH STATEMENT EXECUTE FUNCTION aqa.reject_experiment_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER aqa_experiment_symbols_reject_truncate
        BEFORE TRUNCATE ON aqa.aqa_experiment_symbols
        FOR EACH STATEMENT EXECUTE FUNCTION aqa.reject_experiment_mutation()
        """
    )
    op.execute("REVOKE ALL ON FUNCTION aqa.reject_experiment_mutation() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION aqa.reject_experiment_mutation() TO aqa_migrate")
    op.execute(
        "GRANT SELECT, INSERT ON TABLE aqa.aqa_experiments, "
        "aqa.aqa_experiment_symbols TO aqa_migrate"
    )
    op.execute("GRANT SELECT, INSERT ON TABLE aqa.aqa_audit_events TO aqa_migrate")
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE aqa.aqa_experiments, "
        "aqa.aqa_experiment_symbols FROM aqa_migrate"
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW aqa.aqa_decision_slots_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT slot_id, experiment_id, experiment_version, experiment_hash,
               signal_provider_id, signal_provider_version, session_date,
               source_interval_start, source_interval_end, decision_type, ready_at,
               deadline_at, required_completion_at, state, claim_owner, claimed_at,
               lease_expires_at, attempt_count, completed_at, reason_code, correlation_id,
               version, created_at, updated_at, content_hash
        FROM aqa.aqa_decision_slots
        """
    )
    op.execute("ALTER VIEW aqa.aqa_decision_slots_v OWNER TO aqa_migrate")
    op.execute("REVOKE ALL PRIVILEGES ON aqa.aqa_decision_slots_v FROM PUBLIC")
    op.execute(
        "GRANT SELECT ON aqa.aqa_decision_slots_v TO aqa_strategy, aqa_control, aqa_readonly"
    )
    op.execute("GRANT SELECT ON aqa.aqa_signals_v TO aqa_scheduler")


def downgrade() -> None:
    """Refuse removal of the immutable registration boundary."""

    raise RuntimeError("Destructive downgrade of experiment registration is not supported")
