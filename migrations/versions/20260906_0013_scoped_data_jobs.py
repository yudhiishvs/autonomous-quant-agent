"""Let the collector execute bounded data jobs without control or order authority."""

from alembic import op

revision = "20260906_0013"
down_revision = "20260906_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Filter collector job authority by durable type at the database boundary."""

    op.execute("GRANT SELECT, UPDATE ON aqa.aqa_jobs TO aqa_collector")
    op.execute("GRANT SELECT, INSERT ON aqa.aqa_job_attempts TO aqa_collector")
    op.execute("GRANT INSERT ON aqa.aqa_outbox_events TO aqa_collector")
    for name in ("aqa_jobs", "aqa_job_attempts", "aqa_outbox_events"):
        op.execute(f"ALTER TABLE aqa.{name} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY control_jobs ON aqa.{name} "
            "FOR ALL TO aqa_control USING (true) WITH CHECK (true)"
        )
    allowed = "job_type IN ('GAP_REPAIR', 'DATASET_FREEZE')"
    op.execute(
        "CREATE POLICY collector_job_read ON aqa.aqa_jobs "
        f"FOR SELECT TO aqa_collector USING ({allowed})"
    )
    op.execute(
        "CREATE POLICY collector_job_update ON aqa.aqa_jobs "
        f"FOR UPDATE TO aqa_collector USING ({allowed}) WITH CHECK ({allowed})"
    )
    attempt_scope = (
        "EXISTS (SELECT 1 FROM aqa.aqa_jobs j "
        "WHERE j.job_id = aqa_job_attempts.job_id "
        "AND j.job_type IN ('GAP_REPAIR', 'DATASET_FREEZE'))"
    )
    op.execute(
        "CREATE POLICY collector_attempt_read ON aqa.aqa_job_attempts "
        f"FOR SELECT TO aqa_collector USING ({attempt_scope})"
    )
    op.execute(
        "CREATE POLICY collector_attempt_insert ON aqa.aqa_job_attempts "
        f"FOR INSERT TO aqa_collector WITH CHECK ({attempt_scope})"
    )
    op.execute(
        "CREATE POLICY collector_outbox_insert ON aqa.aqa_outbox_events "
        "FOR INSERT TO aqa_collector WITH CHECK (aggregate_type = 'job' AND "
        "EXISTS (SELECT 1 FROM aqa.aqa_jobs j WHERE j.job_id = aggregate_id "
        "AND j.job_type IN ('GAP_REPAIR', 'DATASET_FREEZE')))"
    )
    op.execute(
        "CREATE POLICY collector_job_audit_insert ON aqa.aqa_audit_events "
        "FOR INSERT TO aqa_collector WITH CHECK (actor = 'aqa_collector' "
        "AND event_type LIKE 'job.%' AND EXISTS (SELECT 1 FROM aqa.aqa_jobs j "
        "WHERE stream_id = 'aqa_collector:job:' || j.job_id "
        "AND j.job_type IN ('GAP_REPAIR', 'DATASET_FREEZE')))"
    )


def downgrade() -> None:
    """Revoke collector authority while preserving jobs, attempts and outbox evidence."""

    op.execute("DROP POLICY collector_job_audit_insert ON aqa.aqa_audit_events")
    for table, policies in (
        ("aqa_jobs", ("collector_job_read", "collector_job_update")),
        ("aqa_job_attempts", ("collector_attempt_read", "collector_attempt_insert")),
        ("aqa_outbox_events", ("collector_outbox_insert",)),
    ):
        for policy in (*policies, "control_jobs"):
            op.execute(f"DROP POLICY {policy} ON aqa.{table}")
        op.execute(f"ALTER TABLE aqa.{table} DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE SELECT, UPDATE ON aqa.aqa_jobs FROM aqa_collector")
    op.execute("REVOKE SELECT, INSERT ON aqa.aqa_job_attempts FROM aqa_collector")
    op.execute("REVOKE INSERT ON aqa.aqa_outbox_events FROM aqa_collector")
