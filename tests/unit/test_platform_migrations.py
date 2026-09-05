"""Offline contract tests for the additive generic-platform migration."""

from __future__ import annotations

import importlib
import io
import re
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

_REVISION_MODULE = "migrations.versions.20260905_0002_platform_foundation"
_REVISION = "20260905_0002"
_HEAD_REVISION = "20260905_0006"
_PRIOR_REVISION = "20260903_0001"
_EXPECTED_TABLES = frozenset(
    {
        "aqa_experiments",
        "aqa_experiment_symbols",
        "aqa_security_metadata_events",
        "aqa_bar_identities",
        "aqa_bar_events",
        "aqa_bar_latest",
        "aqa_data_gaps",
        "aqa_symbol_watermarks",
        "aqa_basket_watermarks",
        "aqa_dataset_manifests",
        "aqa_decision_slots",
        "aqa_signal_envelopes",
        "aqa_risk_latch_events",
        "aqa_risk_decisions",
        "aqa_execution_plans",
        "aqa_order_intents",
        "aqa_broker_orders",
        "aqa_order_events",
        "aqa_fills",
        "aqa_reconciliations",
        "aqa_incidents",
        "aqa_jobs",
        "aqa_job_attempts",
        "aqa_outbox_events",
        "aqa_audit_events",
    }
)


def _migration_module() -> ModuleType:
    return importlib.import_module(_REVISION_MODULE)


def _postgresql_operations(output: io.StringIO) -> Operations:
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    return Operations(context)


def test_platform_revision_is_the_linear_head() -> None:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    revisions = ScriptDirectory.from_config(config)
    head = revisions.get_current_head()
    migration = revisions.get_revision(_REVISION)

    assert head == _HEAD_REVISION
    assert migration is not None
    assert migration.down_revision == _PRIOR_REVISION


def test_platform_upgrade_compiles_complete_postgresql_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    monkeypatch.setattr(migration, "op", _postgresql_operations(output))

    migration.upgrade()

    sql = output.getvalue()
    created_tables = frozenset(re.findall(r"CREATE TABLE aqa\.(aqa_[a-z_]+)", sql))
    assert "CREATE SCHEMA IF NOT EXISTS aqa" in sql
    assert created_tables == _EXPECTED_TABLES
    assert sql.count("CREATE TABLE aqa.") == len(_EXPECTED_TABLES)
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "JSONB" in sql
    assert "DROP TABLE" not in sql
    assert "market_data." not in sql


def test_platform_upgrade_contains_required_uniqueness_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    monkeypatch.setattr(migration, "op", _postgresql_operations(output))

    migration.upgrade()

    sql = output.getvalue()
    for constraint in (
        "bar_series_start",
        "bar_identity_revision",
        "experiment_id_version",
        "decision_source_type",
        "uq_aqa_signal_envelopes_content_hash",
        "uq_aqa_order_intents_client_order_id",
        "uq_aqa_fills_broker_execution_id",
        "job_type_idempotency",
        "audit_stream_sequence",
    ):
        assert f"CONSTRAINT {constraint}" in sql


def test_platform_upgrade_rejects_nonfinite_financial_numerics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    monkeypatch.setattr(migration, "op", _postgresql_operations(output))

    migration.upgrade()

    sql = output.getvalue()
    for table in (
        "aqa_bar_events",
        "aqa_risk_decisions",
        "aqa_order_intents",
        "aqa_broker_orders",
        "aqa_fills",
    ):
        assert f"ck_{table}_financial_values_finite" in sql
    assert sql.count("NOT IN ('NaN', 'Infinity', '-Infinity')") == 17


def test_platform_downgrade_refuses_before_rendering_destructive_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    monkeypatch.setattr(migration, "op", _postgresql_operations(output))

    with pytest.raises(RuntimeError, match="Destructive downgrade"):
        migration.downgrade()

    assert output.getvalue() == ""
