"""Offline contracts for the PostgreSQL service-role migration."""

from __future__ import annotations

import importlib
import io
import re
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

_REVISION_MODULE = "migrations.versions.20260905_0004_platform_roles"
_EXPECTED_AUTHORIZATION_ROLES = (
    "aqa_migrate",
    "aqa_collector",
    "aqa_scheduler",
    "aqa_strategy",
    "aqa_execution",
    "aqa_control",
    "aqa_readonly",
)
_EXPECTED_LOGIN_ROLES = tuple(f"{role}_login" for role in _EXPECTED_AUTHORIZATION_ROLES)
_EXPECTED_ROLES = _EXPECTED_AUTHORIZATION_ROLES + _EXPECTED_LOGIN_ROLES
_EXPECTED_VIEWS = frozenset(
    {
        "aqa_audit_events_v",
        "aqa_audit_status_v",
        "aqa_basket_watermarks_v",
        "aqa_data_gaps_v",
        "aqa_datasets_v",
        "aqa_decision_slots_v",
        "aqa_effective_bars_v",
        "aqa_execution_plans_v",
        "aqa_experiment_context_v",
        "aqa_fills_v",
        "aqa_incidents_v",
        "aqa_jobs_v",
        "aqa_orders_v",
        "aqa_reconciliations_v",
        "aqa_risk_decisions_v",
        "aqa_risk_latches_v",
        "aqa_security_metadata_v",
        "aqa_signals_v",
        "aqa_symbol_watermarks_v",
    }
)
_EXPECTED_WRITER_AUDIT_VIEWS = frozenset(
    {
        "aqa_collector_audit_events_v",
        "aqa_scheduler_audit_events_v",
        "aqa_strategy_audit_events_v",
        "aqa_execution_audit_events_v",
    }
)
_EXPECTED_CREATED_VIEWS = _EXPECTED_VIEWS | _EXPECTED_WRITER_AUDIT_VIEWS


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


def test_role_revision_follows_platform_schema() -> None:
    migration = _migration_module()

    assert migration.revision == "20260905_0004"
    assert migration.down_revision == "20260905_0003"
    assert migration.PLATFORM_ROLES == _EXPECTED_ROLES
    assert frozenset(migration.SAFE_VIEW_NAMES) == _EXPECTED_VIEWS


def test_role_upgrade_requires_prebootstrapped_group_and_login_roles_without_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _render_upgrade(monkeypatch)

    created_roles = tuple(re.findall(r"CREATE ROLE (aqa_[a-z]+)", sql))
    assert created_roles == ()
    for role in _EXPECTED_AUTHORIZATION_ROLES:
        assert f"'{role}'" in sql
        assert f"'{role}_login'" in sql
    assert "run cluster bootstrap" in sql
    assert "PASSWORD" not in sql.upper()
    assert "CREATE ROLE aqa_dashboard" not in sql


def test_role_upgrade_creates_only_explicit_security_barrier_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _render_upgrade(monkeypatch)

    created_views = frozenset(re.findall(r"CREATE VIEW aqa\.(aqa_[a-z_]+)", sql))
    assert created_views == _EXPECTED_CREATED_VIEWS
    assert sql.count("security_barrier = true, security_invoker = false") == len(
        _EXPECTED_CREATED_VIEWS
    )
    assert "ALTER SCHEMA aqa OWNER TO aqa_migrate" in sql
    assert "ALTER SCHEMA market_data OWNER TO aqa_migrate" in sql
    assert "ALTER TABLE market_data.collection_universes OWNER TO aqa_migrate" in sql
    assert "ALTER FUNCTION market_data.guard_checkpoint_update() OWNER TO aqa_migrate" in sql
    assert "event.lineage_hash" in sql
    assert "SELECT *" not in sql.upper()


def test_role_upgrade_revokes_implicit_and_unbounded_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _render_upgrade(monkeypatch)
    normalized = " ".join(sql.split()).replace("%%", "%")

    assert "REVOKE ALL PRIVILEGES ON DATABASE %I FROM aqa_migrate," in normalized
    assert "REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC" in normalized
    assert "REVOKE CONNECT" not in normalized
    assert "platform roles retain unsafe public-schema authority" in normalized
    assert "has_schema_privilege(managed_role.role_name, 'public', 'USAGE')" in normalized
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public" not in normalized
    assert "REVOKE ALL PRIVILEGES ON SCHEMA aqa FROM PUBLIC," in normalized
    assert "REVOKE ALL PRIVILEGES ON SCHEMA market_data FROM PUBLIC," in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA market_data" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA market_data" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA market_data" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA aqa" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA aqa" in normalized
    assert "REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA aqa" in normalized
    assert "FROM pg_catalog.pg_type AS managed_type" in normalized
    assert "namespace.nspname IN ('aqa', 'market_data')" in normalized
    assert "managed_type.typtype <> 'p'" in normalized
    assert "managed_type.typelem = 0" in normalized
    assert "REVOKE ALL PRIVILEGES ON TYPE %I.%I FROM PUBLIC, aqa_collector" in normalized
    for login_role in _EXPECTED_LOGIN_ROLES:
        assert login_role in normalized
    assert (
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA aqa, market_data FROM aqa_migrate"
        not in normalized
    )
    assert "TRUNCATE, REFERENCES" not in normalized
    assert "TRUNCATE, TRIGGER" not in normalized
    assert "REVOKE EXECUTE ON ROUTINES FROM PUBLIC" in normalized
    assert "REVOKE USAGE ON TYPES FROM PUBLIC" in normalized
    assert "REVOKE ALL PRIVILEGES ON SCHEMAS FROM PUBLIC" in normalized
    assert "ON SEQUENCES FROM aqa_migrate" not in normalized
    assert "GRANT CONNECT, CREATE ON DATABASE %I TO aqa_migrate" in normalized
    assert "GRANT USAGE ON SCHEMA market_data TO aqa_migrate" in normalized
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE market_data.alembic_version TO aqa_migrate"
        in normalized
    )
    assert "GRANT SELECT ON ALL TABLES" not in normalized
    assert "GRANT ALL" not in normalized
    assert "WITH GRANT OPTION" not in normalized
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate REVOKE INSERT, UPDATE, DELETE, "
        "TRUNCATE ON TABLES FROM aqa_migrate" in normalized
    )
    assert (
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA aqa, market_data "
        "FROM aqa_migrate" in normalized
    )


def test_role_upgrade_normalizes_column_acls_in_both_managed_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = " ".join(_render_upgrade(monkeypatch).split()).replace("%%", "%")

    assert "attribute.attacl IS NOT NULL" in normalized
    assert "namespace.nspname IN ('aqa', 'market_data')" in normalized
    assert "REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM %s" in normalized
    for role in ("PUBLIC", *_EXPECTED_ROLES):
        assert f"'{role}'" in normalized


def test_role_upgrade_enforces_audit_actor_stream_and_event_family_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = " ".join(_render_upgrade(monkeypatch).split()).replace("%%", "%")

    assert "ALTER TABLE aqa.aqa_audit_events ENABLE ROW LEVEL SECURITY" in normalized
    expected = {
        "aqa_collector": ("bar.%", "data.%", "gap.%", "dataset.%", "collector.%", "security.%"),
        "aqa_scheduler": ("slot.%", "scheduler.%", "security.%"),
        "aqa_strategy": ("signal.%", "strategy.%", "security.%"),
        "aqa_execution": (
            "risk.%",
            "latch.%",
            "execution.%",
            "intent.%",
            "order.%",
            "fill.%",
            "reconciliation.%",
            "incident.%",
            "security.%",
        ),
        "aqa_control": ("experiment.%", "operator.%", "job.%", "control.%", "security.%"),
    }
    for role, prefixes in expected.items():
        assert f"FOR INSERT TO {role}" in normalized
        assert f"actor = '{role}'" in normalized
        assert f"left(stream_id, char_length('{role}:')) = '{role}:'" in normalized
        for prefix in prefixes:
            assert f"event_type LIKE '{prefix}'" in normalized
    assert "FORCE ROW LEVEL SECURITY" not in normalized


def test_role_upgrade_contains_each_service_write_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _render_upgrade(monkeypatch)
    normalized = " ".join(sql.split())

    assert "aqa.aqa_dataset_manifests, aqa.aqa_audit_events TO aqa_collector" in normalized
    assert "GRANT SELECT, INSERT ON TABLE aqa.aqa_decision_slots TO aqa_scheduler" in normalized
    assert "GRANT SELECT, INSERT ON TABLE aqa.aqa_signal_envelopes TO aqa_strategy" in normalized
    assert (
        "aqa.aqa_reconciliations, aqa.aqa_incidents, aqa.aqa_audit_events TO aqa_execution"
        in normalized
    )
    assert "aqa.aqa_jobs, aqa.aqa_outbox_events TO aqa_control" in normalized
    assert "GRANT INSERT ON TABLE aqa.aqa_audit_events TO aqa_control" in normalized
    assert "GRANT SELECT, INSERT ON TABLE aqa.aqa_job_attempts TO aqa_control" in normalized
    assert "GRANT INSERT ON TABLE aqa.aqa_risk_latch_events TO aqa_control" in normalized
    assert "GRANT USAGE ON SCHEMA market_data TO aqa_collector" in normalized
    assert (
        "market_data.collector_checkpoints, market_data.collector_events TO aqa_collector"
        in normalized
    )
    for role in _EXPECTED_AUTHORIZATION_ROLES[1:]:
        assert f"GRANT SELECT ON TABLE aqa.aqa_audit_events TO {role}" not in normalized


def test_role_upgrade_activates_migration_role_after_ownership_and_before_acl_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _render_upgrade(monkeypatch)
    normalized = " ".join(sql.split())

    ownership = "ALTER FUNCTION market_data.reject_checkpoint_removal() OWNER TO aqa_migrate;"
    first_acl_change = "DO $aqa_column_acls$"
    set_role = "SET ROLE aqa_migrate;"
    assert normalized.index(ownership) < normalized.index(set_role)
    assert normalized.index(set_role) < normalized.index(first_acl_change)


def test_audit_events_view_exposes_the_complete_validated_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalized = " ".join(_render_upgrade(monkeypatch).split())

    assert (
        "CREATE VIEW aqa.aqa_audit_events_v WITH "
        "(security_barrier = true, security_invoker = false) AS "
        "SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor, "
        "occurred_at, payload, payload_hash, event_hash, content_hash "
        "FROM aqa.aqa_audit_events" in normalized
    )


def test_role_downgrade_refuses_before_rendering_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    with pytest.raises(RuntimeError, match="Destructive downgrade"):
        migration.downgrade()

    assert output.getvalue() == ""
