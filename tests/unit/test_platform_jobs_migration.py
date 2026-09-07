"""Offline contracts for the durable jobs and outbox migration."""

from __future__ import annotations

import importlib
import io
import re
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, Index, Table, UniqueConstraint

from adaptive_trader.platform.jobs.schema import (
    aqa_job_attempts_contract,
    aqa_jobs_contract,
    aqa_outbox_events_contract,
)
from adaptive_trader.platform.storage.tables import (
    aqa_job_attempts,
    aqa_jobs,
    aqa_outbox_events,
)

_REVISION_MODULE = "migrations.versions.20260905_0009_durable_job_contracts"


def _migration_module() -> ModuleType:
    return importlib.import_module(_REVISION_MODULE)


def _render_upgrade(monkeypatch: pytest.MonkeyPatch) -> str:
    migration = _migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.upgrade()
    return output.getvalue()


def test_durable_job_revision_is_linear_after_signed_execution() -> None:
    migration = _migration_module()

    assert migration.revision == "20260905_0009"
    assert migration.down_revision == "20260905_0008"


@pytest.mark.parametrize(
    ("canonical", "isolated"),
    (
        (aqa_jobs, aqa_jobs_contract),
        (aqa_job_attempts, aqa_job_attempts_contract),
        (aqa_outbox_events, aqa_outbox_events_contract),
    ),
)
def test_canonical_metadata_matches_the_isolated_job_contract(
    canonical: Table,
    isolated: Table,
) -> None:
    def constraints(table: Table) -> set[tuple[str | None, str, tuple[str, ...]]]:
        return {
            (
                constraint.name,
                str(constraint.sqltext) if isinstance(constraint, CheckConstraint) else "",
                tuple(column.name for column in constraint.columns),
            )
            for constraint in table.constraints
            if isinstance(constraint, (CheckConstraint, UniqueConstraint))
        }

    def indexes(table: Table) -> set[tuple[str, tuple[str, ...]]]:
        return {
            (
                index.name,
                tuple(column.name for column in index.columns),
            )
            for index in table.indexes
            if isinstance(index, Index) and index.name is not None
        }

    assert tuple(
        (column.name, str(column.type), column.nullable, column.primary_key)
        for column in canonical.columns
    ) == tuple(
        (column.name, str(column.type), column.nullable, column.primary_key)
        for column in isolated.columns
    )
    assert constraints(canonical) == constraints(isolated)
    assert indexes(canonical) == indexes(isolated)
    assert canonical.info == isolated.info


def test_upgrade_guards_provisional_rows_and_replaces_exact_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = " ".join(_render_upgrade(monkeypatch).split())

    for table in ("aqa_jobs", "aqa_job_attempts", "aqa_outbox_events"):
        assert f"EXISTS (SELECT 1 FROM aqa.{table} LIMIT 1)" in normalized
        assert f"DROP TABLE aqa.{table}" in normalized
        assert f"CREATE TABLE aqa.{table}" in normalized
    lock = (
        "LOCK TABLE aqa.aqa_jobs, aqa.aqa_job_attempts, "
        "aqa.aqa_outbox_events IN ACCESS EXCLUSIVE MODE"
    )
    temporary_read = (
        "GRANT SELECT, UPDATE ON TABLE aqa.aqa_jobs, aqa.aqa_job_attempts, "
        "aqa.aqa_outbox_events TO aqa_migrate"
    )
    assert "SET ROLE aqa_migrate" in normalized
    assert temporary_read in normalized
    assert normalized.index("SET ROLE aqa_migrate") < normalized.index(temporary_read)
    assert lock in normalized
    assert normalized.index(temporary_read) < normalized.index(lock)
    assert normalized.index(lock) < normalized.index("EXISTS (SELECT 1 FROM aqa.aqa_jobs LIMIT 1)")
    assert "requires an explicit durable-job backfill" in normalized
    assert normalized.index("DROP TABLE aqa.aqa_jobs") < normalized.index(
        "CREATE TABLE aqa.aqa_jobs"
    )


def test_upgrade_declares_closed_states_leases_hashes_and_claim_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _render_upgrade(monkeypatch)
    normalized = " ".join(sql.split())

    assert (
        "'PENDING', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEAD', 'CANCELED'" in normalized
    )
    assert "'DATA_QUALITY_AUDIT', 'GAP_REPAIR', 'DATASET_FREEZE', 'OFFLINE_DEMO'" in normalized
    assert "'PENDING', 'CLAIMED', 'PUBLISHED', 'FAILED', 'DEAD'" in normalized
    assert "attempt_count BETWEEN 0 AND 3" in normalized
    assert "max_attempts = 3" in normalized
    assert "lease_expires_at TIMESTAMP WITH TIME ZONE" in normalized
    assert "FOREIGN KEY(aggregate_id) REFERENCES aqa.aqa_jobs (job_id)" in normalized
    for expression in (
        "updated_at >= created_at",
        "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
        "lease_expires_at IS NULL OR lease_expires_at > updated_at",
        "completed_at IS NULL OR completed_at = updated_at",
        "published_at IS NULL OR published_at = updated_at",
    ):
        assert expression in normalized
    assert sql.count("length(payload_hash) = 64") == 2
    assert sql.count("length(content_hash) = 64") == 3
    indexes = frozenset(re.findall(r"CREATE INDEX (ix_aqa_[a-z_]+)", sql))
    assert indexes == {
        "ix_aqa_jobs_claim",
        "ix_aqa_job_attempts_job_attempt",
        "ix_aqa_outbox_delivery",
    }


def test_upgrade_recreates_a_minimal_security_barrier_view_and_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = " ".join(_render_upgrade(monkeypatch).split())

    view = normalized[normalized.index("CREATE VIEW aqa.aqa_jobs_v") :]
    assert "security_barrier = true, security_invoker = false" in view
    assert "payload" not in view.split("FROM aqa.aqa_jobs", maxsplit=1)[0]
    assert "idempotency_key" not in view.split("FROM aqa.aqa_jobs", maxsplit=1)[0]
    assert "correlation_id" not in view.split("FROM aqa.aqa_jobs", maxsplit=1)[0]
    assert "ALTER TABLE aqa.aqa_jobs OWNER TO aqa_migrate" in normalized
    assert "ALTER VIEW aqa.aqa_jobs_v OWNER TO aqa_migrate" in normalized
    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE aqa.aqa_jobs, aqa.aqa_outbox_events TO aqa_control"
    ) in normalized
    assert "GRANT SELECT, INSERT ON TABLE aqa.aqa_job_attempts TO aqa_control" in normalized
    assert "GRANT SELECT, INSERT ON TABLE aqa.aqa_risk_latch_events TO aqa_control" in normalized
    assert "GRANT UPDATE ON TABLE aqa.aqa_risk_latch_events TO aqa_control" not in normalized
    assert "GRANT DELETE ON TABLE aqa.aqa_risk_latch_events TO aqa_control" not in normalized
    assert "GRANT SELECT ON TABLE aqa.aqa_jobs_v TO aqa_control, aqa_readonly" in normalized
    for table in ("aqa_jobs", "aqa_job_attempts", "aqa_outbox_events"):
        assert (
            f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE aqa.{table} FROM aqa_migrate"
            in normalized
        )
    assert "GRANT UPDATE (created_at) ON TABLE aqa.aqa_jobs TO aqa_migrate" in normalized


def test_downgrade_refuses_destructive_job_evidence_removal() -> None:
    with pytest.raises(RuntimeError, match="Destructive downgrade"):
        _migration_module().downgrade()
