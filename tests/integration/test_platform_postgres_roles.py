"""Guarded PostgreSQL 16 authorization tests for platform service roles."""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import DropSchema

import adaptive_trader.platform.storage.migration_runner as migration_runner
from adaptive_trader.collection.contracts import MarketBarV1, RawBarObservationV1
from adaptive_trader.collection.migrations import _alembic_config, upgrade_database
from adaptive_trader.collection.postgres import (
    PostgresMarketDataRepository,
    normalize_postgres_url,
    postgres_connect_args,
)
from adaptive_trader.collection.repository import CheckpointKey, CoverageAdvance
from adaptive_trader.collection.schema import SCHEMA_NAME as COLLECTION_SCHEMA
from adaptive_trader.collection.schema import bar_observations, current_bars
from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from adaptive_trader.platform.storage import AuditRepository
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteStatus,
    MarketDataRepository,
)
from adaptive_trader.platform.storage.migration_roles import (
    REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS,
    MigrationRoleActivationError,
    activate_platform_migration_role,
    migration_role_revision_sets,
)
from adaptive_trader.platform.storage.role_bootstrap import (
    AUTHORIZATION_ROLES,
    LOGIN_ROLE_BY_AUTHORIZATION_ROLE,
    LOGIN_ROLES,
    ROLE_PASSWORD_FILE_BY_AUTHORIZATION_ROLE,
    bootstrap_platform_database_roles,
)
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, PLATFORM_TABLE_NAMES

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

_DATABASE_URL = os.environ.get("APA_TEST_POSTGRES_URL", "").strip()
if not _DATABASE_URL:
    pytest.skip(
        "APA_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
        allow_module_level=True,
    )
if os.environ.get("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE") != "YES":
    raise RuntimeError(
        "PostgreSQL integration tests require APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES"
    )
if os.environ.get("APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES") != "YES":
    raise RuntimeError("PostgreSQL role tests require APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES")
_TEST_DATABASE = normalize_postgres_url(_DATABASE_URL)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if _TEST_DATABASE.host not in {"127.0.0.1", "::1", "localhost"}:
    raise RuntimeError("PostgreSQL integration tests require a loopback database host")
if _TEST_DATABASE.database != "collector_test":
    raise RuntimeError("PostgreSQL integration tests require the collector_test database")

_ROLES = AUTHORIZATION_ROLES
_RUNTIME_ROLES = _ROLES[1:]
_SAFE_VIEWS = frozenset(
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
_AUDIT_READ_VIEW_BY_ROLE = {
    "aqa_collector": "aqa_collector_audit_events_v",
    "aqa_scheduler": "aqa_scheduler_audit_events_v",
    "aqa_strategy": "aqa_strategy_audit_events_v",
    "aqa_execution": "aqa_execution_audit_events_v",
    "aqa_control": "aqa_audit_events_v",
    "aqa_readonly": "aqa_audit_events_v",
}
_VIEWS = _SAFE_VIEWS | frozenset(_AUDIT_READ_VIEW_BY_ROLE.values())
_ALL_SAFE_VIEWS = tuple(sorted(_SAFE_VIEWS))

_COLLECTOR_TABLES = (
    "aqa_bar_events",
    "aqa_bar_identities",
    "aqa_bar_latest",
    "aqa_data_gaps",
    "aqa_symbol_watermarks",
    "aqa_basket_watermarks",
    "aqa_dataset_manifests",
    "aqa_audit_events",
)
_EXECUTION_TABLES = (
    "aqa_risk_latch_events",
    "aqa_risk_decisions",
    "aqa_execution_plans",
    "aqa_order_intents",
    "aqa_broker_orders",
    "aqa_order_events",
    "aqa_fills",
    "aqa_reconciliations",
    "aqa_incidents",
    "aqa_audit_events",
)
_EXECUTION_READ_INPUTS = (
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
)

_SET_ROLE = {
    "aqa_migrate": text("SET LOCAL ROLE aqa_migrate"),
    "aqa_collector": text("SET LOCAL ROLE aqa_collector"),
    "aqa_scheduler": text("SET LOCAL ROLE aqa_scheduler"),
    "aqa_strategy": text("SET LOCAL ROLE aqa_strategy"),
    "aqa_execution": text("SET LOCAL ROLE aqa_execution"),
    "aqa_control": text("SET LOCAL ROLE aqa_control"),
    "aqa_readonly": text("SET LOCAL ROLE aqa_readonly"),
}
_ALLOWED_STATEMENTS = {
    "aqa_migrate": text("CREATE TABLE aqa.aqa_role_probe (value integer)"),
    "aqa_collector": text(
        "INSERT INTO aqa.aqa_bar_identities (bar_identity_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_scheduler": text(
        "INSERT INTO aqa.aqa_decision_slots (slot_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_strategy": text(
        "INSERT INTO aqa.aqa_signal_envelopes (signal_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_execution": text(
        "INSERT INTO aqa.aqa_risk_decisions (risk_decision_id) "
        "SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_control": text("INSERT INTO aqa.aqa_jobs (job_id) SELECT 'permission_probe' WHERE FALSE"),
    "aqa_readonly": text(
        "SELECT audit_event_id, payload, event_hash FROM aqa.aqa_audit_events_v LIMIT 0"
    ),
}
_DENIED_STATEMENTS = {
    "aqa_migrate": text("CREATE TABLE public.aqa_role_forbidden_probe (value integer)"),
    "aqa_collector": text(
        "INSERT INTO aqa.aqa_order_intents (order_intent_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_scheduler": text(
        "INSERT INTO aqa.aqa_broker_orders (client_order_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_strategy": text(
        "INSERT INTO aqa.aqa_risk_decisions (risk_decision_id) "
        "SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_execution": text(
        "INSERT INTO aqa.aqa_decision_slots (slot_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_control": text(
        "INSERT INTO aqa.aqa_fills (fill_id) SELECT 'permission_probe' WHERE FALSE"
    ),
    "aqa_readonly": text("INSERT INTO aqa.aqa_jobs (job_id) SELECT 'permission_probe' WHERE FALSE"),
}
_AUDIT_EVENT_TYPE_BY_ROLE = {
    "aqa_collector": "bar.persisted",
    "aqa_scheduler": "slot.claimed",
    "aqa_strategy": "signal.emitted",
    "aqa_execution": "order.submitted",
    "aqa_control": "job.started",
}
_SERVICE_WRITER_BY_ROLE = {
    "aqa_collector": AuditWriter.COLLECTOR,
    "aqa_scheduler": AuditWriter.SCHEDULER,
    "aqa_strategy": AuditWriter.STRATEGY,
    "aqa_execution": AuditWriter.EXECUTION,
    "aqa_control": AuditWriter.CONTROL,
}


def _grant_set(
    *,
    select_from: tuple[str, ...] = (),
    insert_into: tuple[str, ...] = (),
    update: tuple[str, ...] = (),
) -> frozenset[tuple[str, str]]:
    return frozenset(
        [(name, "SELECT") for name in select_from]
        + [(name, "INSERT") for name in insert_into]
        + [(name, "UPDATE") for name in update]
    )


_EXPECTED_GRANTS = {
    "aqa_collector": _grant_set(
        select_from=(
            "aqa_experiments",
            "aqa_experiment_symbols",
            "aqa_security_metadata_events",
            *_COLLECTOR_TABLES[:-1],
            _AUDIT_READ_VIEW_BY_ROLE["aqa_collector"],
        ),
        insert_into=_COLLECTOR_TABLES,
        update=(
            "aqa_bar_latest",
            "aqa_data_gaps",
            "aqa_symbol_watermarks",
            "aqa_basket_watermarks",
        ),
    ),
    "aqa_scheduler": _grant_set(
        select_from=(
            "aqa_decision_slots",
            "aqa_experiment_context_v",
            "aqa_data_gaps_v",
            "aqa_symbol_watermarks_v",
            "aqa_basket_watermarks_v",
            "aqa_datasets_v",
            _AUDIT_READ_VIEW_BY_ROLE["aqa_scheduler"],
        ),
        insert_into=("aqa_decision_slots", "aqa_audit_events"),
        update=("aqa_decision_slots",),
    ),
    "aqa_strategy": _grant_set(
        select_from=(
            "aqa_signal_envelopes",
            "aqa_experiment_context_v",
            "aqa_security_metadata_v",
            "aqa_effective_bars_v",
            "aqa_data_gaps_v",
            "aqa_symbol_watermarks_v",
            "aqa_basket_watermarks_v",
            "aqa_datasets_v",
            "aqa_decision_slots_v",
            _AUDIT_READ_VIEW_BY_ROLE["aqa_strategy"],
        ),
        insert_into=("aqa_signal_envelopes", "aqa_audit_events"),
    ),
    "aqa_execution": _grant_set(
        select_from=(
            *_EXECUTION_READ_INPUTS,
            *_EXECUTION_TABLES[:-1],
            _AUDIT_READ_VIEW_BY_ROLE["aqa_execution"],
        ),
        insert_into=_EXECUTION_TABLES,
        update=("aqa_broker_orders", "aqa_incidents"),
    ),
    "aqa_control": _grant_set(
        select_from=(
            "aqa_jobs",
            "aqa_job_attempts",
            "aqa_outbox_events",
            *_ALL_SAFE_VIEWS,
        ),
        insert_into=(
            "aqa_audit_events",
            "aqa_jobs",
            "aqa_job_attempts",
            "aqa_outbox_events",
            "aqa_risk_latch_events",
        ),
        update=("aqa_jobs", "aqa_outbox_events"),
    ),
    "aqa_readonly": _grant_set(select_from=_ALL_SAFE_VIEWS),
}


def _engine(*, application_name: str) -> Engine:
    return create_engine(
        _TEST_DATABASE,
        hide_parameters=True,
        connect_args=postgres_connect_args(application_name, migration=True),
    )


def _migration_revision_sets() -> tuple[frozenset[str], frozenset[str]]:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return migration_role_revision_sets(ScriptDirectory.from_config(config))


def _drop_disposable_test_schemas() -> None:
    engine = _engine(application_name="platform-role-test-reset")
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(COLLECTION_SCHEMA, cascade=True, if_exists=True))
    finally:
        engine.dispose()


def _write_owner_private(path: Path, value: str) -> None:
    path.write_text(f"{value}\n", encoding="utf-8")
    path.chmod(0o600)


def _role_bootstrap_root(
    root: Path,
    login_database_urls: Mapping[str, str],
) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    secret_directory = root / "secrets"
    secret_directory.mkdir(mode=0o700)
    secret_directory.chmod(0o700)
    for role in AUTHORIZATION_ROLES:
        password = normalize_postgres_url(login_database_urls[role]).password
        if type(password) is not str or not password:
            raise RuntimeError("disposable platform login URL has no password")
        _write_owner_private(
            secret_directory / ROLE_PASSWORD_FILE_BY_AUTHORIZATION_ROLE[role],
            password,
        )
    return root


def _temporary_descendant_migrations(root: Path) -> Path:
    migration_root = root / "migrations"
    shutil.copytree(_PROJECT_ROOT / "migrations", migration_root)
    descendant = migration_root / "versions" / "20260905_0007_probe.py"
    descendant.write_text(
        '''"""Exercise a governed descendant through the deployment login."""

from alembic import op
import sqlalchemy as sa

revision = "20260905_0007_probe"
down_revision = "20260905_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "aqa_transition_probe",
        sa.Column("session_identity", sa.String(length=64), nullable=False),
        sa.Column("effective_identity", sa.String(length=64), nullable=False),
        schema="aqa",
    )
    connection = op.get_bind()
    connection.execute(sa.text(
        "GRANT INSERT ON TABLE aqa.aqa_transition_probe TO aqa_migrate"
    ))
    connection.execute(sa.text(
        "INSERT INTO aqa.aqa_transition_probe "
        "(session_identity, effective_identity) SELECT session_user, current_user"
    ))
    connection.execute(sa.text(
        "REVOKE INSERT ON TABLE aqa.aqa_transition_probe FROM aqa_migrate"
    ))


def downgrade() -> None:
    raise RuntimeError("test descendant is intentionally irreversible")
''',
        encoding="utf-8",
    )
    return migration_root


@pytest.fixture
def provisioned_engine(platform_migration_database_url: str) -> Iterator[Engine]:
    """Yield only the positively guarded disposable database after all migrations."""

    _drop_disposable_test_schemas()
    upgrade_database(platform_migration_database_url)
    engine = _engine(application_name="platform-role-test")
    try:
        yield engine
    finally:
        engine.dispose()
        _drop_disposable_test_schemas()


@contextmanager
def _connection_as(engine: Engine, role: str) -> Iterator[Connection]:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(_SET_ROLE[role])
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def _effective_table_grants(connection: Connection, role: str) -> frozenset[tuple[str, str]]:
    rows = connection.execute(
        text(
            """
            SELECT relation.relname, privilege.name
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN unnest(
                ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']
            ) AS privilege(name)
            WHERE namespace.nspname = 'aqa'
              AND relation.relkind IN ('r', 'p', 'v')
              AND has_table_privilege(:role_name, relation.oid, privilege.name)
            ORDER BY relation.relname, privilege.name
            """
        ),
        {"role_name": role},
    )
    return frozenset((row[0], row[1]) for row in rows)


def _assert_permission_denied(engine: Engine, role: str, statement: Any) -> None:
    with _connection_as(engine, role) as connection:
        with pytest.raises(DBAPIError) as exc_info:
            connection.execute(statement)
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


def _audit_insert(*, actor: str, stream_actor: str, event_type: str) -> Any:
    return text(
        """
        INSERT INTO aqa.aqa_audit_events (
            audit_event_id, stream_id, sequence, previous_hash, event_type, actor,
            occurred_at, payload, payload_hash, event_hash, content_hash
        ) VALUES (
            :audit_event_id, :stream_id, 1, :previous_hash, :event_type, :actor,
            statement_timestamp(), '{}'::jsonb, :payload_hash, :event_hash, :event_hash
        )
        """
    ).bindparams(
        audit_event_id=f"audit-{stream_actor}-{event_type}",
        stream_id=f"{stream_actor}:permission-probe",
        previous_hash="0" * 64,
        event_type=event_type,
        actor=actor,
        payload_hash="1" * 64,
        event_hash="2" * 64,
    )


def test_empty_database_activates_the_prebootstrapped_migration_role() -> None:
    _drop_disposable_test_schemas()
    engine = _engine(application_name="platform-role-bootstrap-test")
    known_revisions, governed_revisions = _migration_revision_sets()
    try:
        with engine.connect() as connection:
            assert activate_platform_migration_role(
                connection,
                known_revisions=known_revisions,
                governed_revisions=governed_revisions,
            )
            assert connection.scalar(text("SELECT current_user = 'aqa_migrate'"))
    finally:
        engine.dispose()
        _drop_disposable_test_schemas()


def test_upgrade_normalizes_preexisting_column_acls_in_both_managed_schemas() -> None:
    _drop_disposable_test_schemas()
    config = _alembic_config(_DATABASE_URL)
    command.upgrade(config, "20260905_0003")
    engine = _engine(application_name="platform-column-acl-seed")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "GRANT SELECT (configuration), UPDATE (configuration) "
                    "ON TABLE aqa.aqa_experiments TO aqa_readonly"
                )
            )
            connection.execute(
                text(
                    "GRANT SELECT (members) ON TABLE market_data.collection_universes "
                    "TO aqa_readonly"
                )
            )
            connection.execute(
                text("GRANT SELECT (configuration) ON TABLE aqa.aqa_experiments TO PUBLIC")
            )
            assert connection.scalar(
                text(
                    "SELECT has_any_column_privilege("
                    "'aqa_readonly', 'aqa.aqa_experiments', 'SELECT,UPDATE')"
                )
            )
            assert connection.scalar(
                text(
                    "SELECT has_any_column_privilege("
                    "'aqa_readonly', 'market_data.collection_universes', 'SELECT')"
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert not connection.scalar(
                text(
                    "SELECT has_any_column_privilege("
                    "'aqa_readonly', 'aqa.aqa_experiments', 'SELECT,UPDATE')"
                )
            )
            assert not connection.scalar(
                text(
                    "SELECT has_any_column_privilege("
                    "'aqa_readonly', 'market_data.collection_universes', 'SELECT')"
                )
            )
            assert not connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.column_privileges "
                    "WHERE grantee = 'PUBLIC' AND table_schema = 'aqa' "
                    "AND table_name = 'aqa_experiments' "
                    "AND column_name = 'configuration' AND privilege_type = 'SELECT')"
                )
            )
    finally:
        engine.dispose()
        _drop_disposable_test_schemas()


def test_role_migration_preserves_legacy_collector_connectivity_via_public_connect() -> None:
    legacy_role = "aqa_test_legacy_collector_login"
    password = secrets.token_urlsafe(48)
    normalized = normalize_postgres_url(_DATABASE_URL)
    conninfo = normalized.set(drivername="postgresql").render_as_string(hide_password=False)
    created = False
    _drop_disposable_test_schemas()
    try:
        with psycopg.connect(
            conninfo,
            **postgres_connect_args("platform-legacy-role-setup", migration=True),
        ) as admin_connection:
            exists = admin_connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (legacy_role,),
            ).fetchone()
            if exists is None or exists[0] is not False:
                raise RuntimeError("legacy collector role test requires an unused fixed role")
            admin_connection.execute(
                "CREATE ROLE aqa_test_legacy_collector_login "
                "LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            )
            admin_connection.pgconn.change_password(
                legacy_role.encode("utf-8"),
                password.encode("utf-8"),
            )
            admin_connection.commit()
            created = True

        config = _alembic_config(_DATABASE_URL)
        command.upgrade(config, "20260905_0003")
        admin_engine = _engine(application_name="platform-legacy-role-grants")
        try:
            with admin_engine.begin() as connection:
                connection.execute(
                    text("GRANT USAGE ON SCHEMA market_data TO aqa_test_legacy_collector_login")
                )
                connection.execute(
                    text(
                        "GRANT SELECT ON TABLE market_data.collection_universes "
                        "TO aqa_test_legacy_collector_login"
                    )
                )
            command.upgrade(config, "head")
        finally:
            admin_engine.dispose()

        legacy_url = normalized.set(username=legacy_role, password=password)
        legacy_engine = create_engine(
            legacy_url,
            hide_parameters=True,
            connect_args=postgres_connect_args("platform-legacy-role-connectivity"),
        )
        try:
            with legacy_engine.connect() as connection:
                assert connection.scalar(text("SELECT current_user")) == legacy_role
                assert connection.scalar(
                    text(
                        "SELECT has_database_privilege(current_user, current_database(), 'CONNECT')"
                    )
                )
                assert not connection.scalar(
                    text("SELECT has_database_privilege(current_user, current_database(), 'TEMP')")
                )
                connection.execute(
                    text("SELECT universe_hash FROM market_data.collection_universes LIMIT 0")
                )
        finally:
            legacy_engine.dispose()
    finally:
        if created:
            with psycopg.connect(
                conninfo,
                **postgres_connect_args("platform-legacy-role-cleanup", migration=True),
            ) as admin_connection:
                admin_connection.execute("DROP OWNED BY aqa_test_legacy_collector_login CASCADE")
                admin_connection.execute("DROP ROLE aqa_test_legacy_collector_login")
                admin_connection.commit()
        _drop_disposable_test_schemas()


def test_non_superuser_legacy_owner_hands_off_0004_then_descendants_use_migration_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_login_database_urls: Mapping[str, str],
) -> None:
    legacy_role = "aqa_test_legacy_migration_owner"
    password = secrets.token_urlsafe(48)
    normalized = normalize_postgres_url(_DATABASE_URL)
    admin_conninfo = normalized.set(drivername="postgresql").render_as_string(hide_password=False)
    legacy_url = str(
        normalized.set(username=legacy_role, password=password).render_as_string(
            hide_password=False
        )
    )
    root = _role_bootstrap_root(tmp_path / "role-bootstrap", platform_login_database_urls)
    _write_owner_private(root / "admin_database_url", _DATABASE_URL)
    _write_owner_private(root / "legacy_database_url", legacy_url)
    admin_secret = load_secret_file(
        root / "admin_database_url",
        source=SecretFileVariable.DATABASE_URL,
    )
    legacy_secret = load_secret_file(
        root / "legacy_database_url",
        source=SecretFileVariable.DATABASE_URL,
    )
    migration_root = _temporary_descendant_migrations(tmp_path)
    original_config = _alembic_config

    def temporary_config(database_url: str) -> Config:
        config = original_config(database_url)
        config.set_main_option("script_location", str(migration_root))
        return config

    monkeypatch.setattr(migration_runner, "_alembic_config", temporary_config)
    created = False
    _drop_disposable_test_schemas()
    try:
        with psycopg.connect(
            admin_conninfo,
            **postgres_connect_args("platform-legacy-owner-setup", migration=True),
        ) as admin_connection:
            exists = admin_connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (legacy_role,),
            ).fetchone()
            if exists is None or exists[0] is not False:
                raise RuntimeError("legacy owner transition test requires an unused fixed role")
            admin_connection.execute(
                "CREATE ROLE aqa_test_legacy_migration_owner "
                "LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            )
            admin_connection.pgconn.change_password(
                legacy_role.encode("utf-8"),
                password.encode("utf-8"),
            )
            admin_connection.commit()
            created = True

        command.upgrade(_alembic_config(_DATABASE_URL), "20260905_0003")
        with psycopg.connect(
            admin_conninfo,
            **postgres_connect_args("platform-legacy-owner-reassign", migration=True),
        ) as admin_connection:
            admin_connection.execute(
                "REASSIGN OWNED BY aqa_migrate TO aqa_test_legacy_migration_owner"
            )
            admin_connection.execute(
                "GRANT USAGE ON SCHEMA market_data TO aqa_test_legacy_migration_owner"
            )
            admin_connection.execute(
                "GRANT SELECT ON TABLE market_data.alembic_version "
                "TO aqa_test_legacy_migration_owner"
            )
            admin_connection.commit()

        first_bootstrap = bootstrap_platform_database_roles(
            admin_secret,
            application_root=root,
        )
        second_bootstrap = bootstrap_platform_database_roles(
            admin_secret,
            application_root=root,
        )
        assert first_bootstrap.created_authorization_roles == ()
        assert first_bootstrap.created_login_roles == ()
        assert second_bootstrap == first_bootstrap

        with psycopg.connect(
            admin_conninfo,
            **postgres_connect_args("platform-legacy-owner-preflight", migration=True),
        ) as admin_connection:
            assert admin_connection.execute(
                "SELECT has_database_privilege("
                "'aqa_migrate', current_database(), 'CREATE'), "
                "pg_has_role(%s, 'aqa_migrate', 'SET'), "
                "has_database_privilege(%s, current_database(), 'CREATE')",
                (legacy_role, legacy_role),
            ).fetchone() == (True, True, True)

        migration_runner.migrate_platform_database(
            legacy_secret,
            application_root=root,
            bootstrap_admin_database_url=admin_secret,
        )
        migration_runner.migrate_platform_database(legacy_secret, application_root=root)

        with psycopg.connect(
            admin_conninfo,
            **postgres_connect_args("platform-legacy-owner-verify", migration=True),
        ) as admin_connection:
            assert admin_connection.execute(
                "SELECT version_num FROM market_data.alembic_version"
            ).fetchone() == ("20260905_0007_probe",)
            assert admin_connection.execute(
                "SELECT session_identity, effective_identity FROM aqa.aqa_transition_probe"
            ).fetchone() == ("aqa_migrate_login", "aqa_migrate")
            assert admin_connection.execute(
                "SELECT array_agg(DISTINCT owner_name ORDER BY owner_name) FROM ("
                "SELECT pg_get_userbyid(nspowner) AS owner_name FROM pg_namespace "
                "WHERE nspname IN ('aqa', 'market_data') UNION ALL "
                "SELECT pg_get_userbyid(relowner) FROM pg_class relation "
                "JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname IN ('aqa', 'market_data') UNION ALL "
                "SELECT pg_get_userbyid(proowner) FROM pg_proc routine "
                "JOIN pg_namespace namespace ON namespace.oid = routine.pronamespace "
                "WHERE namespace.nspname IN ('aqa', 'market_data')) managed"
            ).fetchone() == (["aqa_migrate"],)
            assert admin_connection.execute(
                "SELECT NOT EXISTS ("
                "SELECT 1 FROM pg_auth_members membership "
                "JOIN pg_roles granted ON granted.oid = membership.roleid "
                "JOIN pg_roles member ON member.oid = membership.member "
                "WHERE granted.rolname = 'aqa_migrate' "
                "AND member.rolname = %s)",
                (legacy_role,),
            ).fetchone() == (True,)
            assert frozenset(
                (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
                for row in admin_connection.execute(
                    """
                    SELECT namespace.nspname,
                           relation.relname,
                           attribute.attname,
                           privilege.privilege_type
                    FROM pg_catalog.pg_attribute AS attribute
                    JOIN pg_catalog.pg_class AS relation
                      ON relation.oid = attribute.attrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS privilege
                    JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = privilege.grantee
                    WHERE namespace.nspname IN ('aqa', 'market_data')
                      AND relation.relkind IN ('r', 'p')
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                      AND grantee.rolname = 'aqa_migrate'
                    ORDER BY namespace.nspname, relation.relname, attribute.attname,
                             privilege.privilege_type
                    """
                ).fetchall()
            ) == frozenset(
                (*target, "UPDATE") for target in REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
            )
            assert admin_connection.execute(
                "SELECT NOT has_table_privilege("
                "'aqa_migrate', 'aqa.aqa_transition_probe', 'INSERT')"
            ).fetchone() == (True,)

        legacy_engine = create_engine(
            normalize_postgres_url(legacy_url),
            hide_parameters=True,
            connect_args=postgres_connect_args("platform-former-owner-denial"),
        )
        try:
            with legacy_engine.connect() as connection, pytest.raises(DBAPIError) as exc_info:
                connection.execute(text("SELECT * FROM aqa.aqa_transition_probe"))
            assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"
        finally:
            legacy_engine.dispose()
    finally:
        if created:
            with psycopg.connect(
                admin_conninfo,
                **postgres_connect_args("platform-legacy-owner-cleanup", migration=True),
            ) as admin_connection:
                admin_connection.execute("SET ROLE aqa_migrate")
                admin_connection.execute(
                    "REVOKE CREATE ON DATABASE collector_test FROM aqa_test_legacy_migration_owner"
                )
                admin_connection.execute("RESET ROLE")
                admin_connection.execute("REVOKE aqa_migrate FROM aqa_test_legacy_migration_owner")
                admin_connection.execute("DROP OWNED BY aqa_test_legacy_migration_owner CASCADE")
                admin_connection.execute("DROP ROLE aqa_test_legacy_migration_owner")
                admin_connection.commit()
        _drop_disposable_test_schemas()


def test_superuser_owned_legacy_database_installs_exact_fk_owner_privileges(
    tmp_path: Path,
    platform_login_database_urls: Mapping[str, str],
) -> None:
    root = _role_bootstrap_root(tmp_path / "superuser-role-bootstrap", platform_login_database_urls)
    _write_owner_private(root / "admin_database_url", _DATABASE_URL)
    admin_secret = load_secret_file(
        root / "admin_database_url",
        source=SecretFileVariable.DATABASE_URL,
    )
    _drop_disposable_test_schemas()
    collector_engine: Engine | None = None
    try:
        command.upgrade(_alembic_config(_DATABASE_URL), "20260905_0003")
        admin_conninfo = (
            normalize_postgres_url(_DATABASE_URL)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        with psycopg.connect(
            admin_conninfo,
            **postgres_connect_args("platform-superuser-owner-setup", migration=True),
        ) as admin_connection:
            admin_connection.execute("REASSIGN OWNED BY aqa_migrate TO CURRENT_USER")
            admin_connection.commit()
        bootstrap_platform_database_roles(admin_secret, application_root=root)
        migration_runner.migrate_platform_database(
            admin_secret,
            application_root=root,
            bootstrap_admin_database_url=admin_secret,
        )

        collector_engine = create_engine(
            normalize_postgres_url(platform_login_database_urls["aqa_collector"]),
            hide_parameters=True,
            connect_args=postgres_connect_args("platform-superuser-handoff-collector"),
        )
        identity = BarIdentity(
            provider="alpaca",
            feed="iex",
            adjustment="raw",
            symbol="AAOI",
            timeframe="1Min",
            start_at=datetime(2026, 9, 5, 20, 30, tzinfo=UTC),
            end_at=datetime(2026, 9, 5, 20, 31, tzinfo=UTC),
        )
        result = MarketDataRepository(collector_engine).append(
            _platform_bar(identity=identity, close="100", delivery="original")
        )
        assert result.status is BarWriteStatus.INSERTED

        legacy = PostgresMarketDataRepository(
            platform_login_database_urls["aqa_collector"],
            application_name="platform-superuser-handoff-legacy",
        )
        try:
            legacy.register_universe()
            lease = legacy.try_acquire_lease(
                lease_name="platform-superuser-handoff",
                holder_id="integration-test",
                ttl_seconds=120,
            )
            assert lease is not None
            run_id = legacy.start_run(mode="backfill", lease=lease)
            write = legacy.append_batch(
                (_collector_observation(close="100", correction=False),),
                lease=lease,
            )
            legacy.finish_run(run_id, lease=lease, status="completed", counters={"rows": 1})
            assert write.observations_inserted == 1
        finally:
            legacy.close()

        with _engine(application_name="platform-superuser-handoff-verify").connect() as connection:
            actual = frozenset(
                (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
                for row in connection.execute(
                    text(
                        """
                        SELECT namespace.nspname, relation.relname, attribute.attname,
                               privilege.privilege_type
                        FROM pg_catalog.pg_attribute AS attribute
                        JOIN pg_catalog.pg_class AS relation
                          ON relation.oid = attribute.attrelid
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS privilege
                        JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = privilege.grantee
                        WHERE namespace.nspname IN ('aqa', 'market_data')
                          AND relation.relkind IN ('r', 'p')
                          AND attribute.attnum > 0
                          AND NOT attribute.attisdropped
                          AND grantee.rolname = 'aqa_migrate'
                        """
                    )
                )
            )
        assert actual == frozenset(
            (*target, "UPDATE") for target in REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
        )
    finally:
        if collector_engine is not None:
            collector_engine.dispose()
        _drop_disposable_test_schemas()


def test_governed_database_activates_migration_role_for_future_objects(
    provisioned_engine: Engine,
) -> None:
    known_revisions, governed_revisions = _migration_revision_sets()
    with provisioned_engine.connect() as connection:
        transaction = connection.begin()
        try:
            assert activate_platform_migration_role(
                connection,
                known_revisions=known_revisions,
                governed_revisions=governed_revisions,
            )
            assert connection.scalar(text("SELECT current_user = 'aqa_migrate'"))
            connection.execute(
                text("CREATE TABLE market_data.aqa_future_parent_probe (value integer PRIMARY KEY)")
            )
            connection.execute(
                text(
                    "CREATE TABLE aqa.aqa_future_child_probe "
                    "(value integer REFERENCES market_data.aqa_future_parent_probe(value))"
                )
            )
            connection.execute(
                text(
                    "CREATE FUNCTION market_data.aqa_future_trigger_probe() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RETURN NEW; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER aqa_future_trigger_probe "
                    "BEFORE INSERT ON market_data.aqa_future_parent_probe "
                    "FOR EACH ROW EXECUTE FUNCTION market_data.aqa_future_trigger_probe()"
                )
            )
            owners = frozenset(
                connection.scalars(
                    text(
                        """
                        SELECT owner.rolname
                        FROM pg_catalog.pg_class AS relation
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
                        WHERE (namespace.nspname, relation.relname) IN (
                            ('aqa', 'aqa_future_child_probe'),
                            ('market_data', 'aqa_future_parent_probe')
                        )
                        """
                    )
                )
            )
            assert owners == {"aqa_migrate"}
            for qualified_name in (
                "aqa.aqa_future_child_probe",
                "market_data.aqa_future_parent_probe",
            ):
                assert connection.scalar(
                    text("SELECT has_table_privilege('aqa_migrate', :table_name, 'REFERENCES')"),
                    {"table_name": qualified_name},
                )
                assert connection.scalar(
                    text("SELECT has_table_privilege('aqa_migrate', :table_name, 'TRIGGER')"),
                    {"table_name": qualified_name},
                )
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    assert not connection.scalar(
                        text("SELECT has_table_privilege('aqa_migrate', :table_name, :privilege)"),
                        {"table_name": qualified_name, "privilege": privilege},
                    )
            function_owner = connection.scalar(
                text(
                    """
                    SELECT owner.rolname
                    FROM pg_catalog.pg_proc AS routine
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = routine.pronamespace
                    JOIN pg_catalog.pg_roles AS owner ON owner.oid = routine.proowner
                    WHERE namespace.nspname = 'market_data'
                      AND routine.proname = 'aqa_future_trigger_probe'
                    """
                )
            )
            assert function_owner == "aqa_migrate"
            for role in _RUNTIME_ROLES:
                for qualified_name in (
                    "aqa.aqa_future_child_probe",
                    "market_data.aqa_future_parent_probe",
                ):
                    assert not connection.scalar(
                        text("SELECT has_table_privilege(:role, :table_name, 'SELECT')"),
                        {"role": role, "table_name": qualified_name},
                    )
        finally:
            transaction.rollback()


def test_deployable_migration_login_is_non_superuser_and_assumes_only_migration_role(
    provisioned_engine: Engine,
    platform_migration_database_url: str,
) -> None:
    del provisioned_engine
    known_revisions, governed_revisions = _migration_revision_sets()
    migration_engine = create_engine(
        normalize_postgres_url(platform_migration_database_url),
        hide_parameters=True,
        connect_args=postgres_connect_args("platform-migration-login-test", migration=True),
    )
    try:
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT session_user")) == "aqa_migrate_login"
            assert connection.scalar(
                text(
                    "SELECT NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb "
                    "FROM pg_roles WHERE rolname = session_user"
                )
            )
            assert activate_platform_migration_role(
                connection,
                known_revisions=known_revisions,
                governed_revisions=governed_revisions,
            )
            assert connection.scalar(text("SELECT current_user")) == "aqa_migrate"
    finally:
        migration_engine.dispose()


def test_migration_owner_has_only_the_exact_foreign_key_row_lock_column_acls(
    provisioned_engine: Engine,
) -> None:
    expected_columns = frozenset(
        (*target, "UPDATE") for target in REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
    )
    expected_relations = frozenset(
        (schema_name, table_name)
        for schema_name, table_name, _column_name in REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
    )
    with provisioned_engine.connect() as connection:
        referenced_relations = frozenset(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                text(
                    """
                    SELECT DISTINCT referenced_namespace.nspname,
                                    referenced_relation.relname
                    FROM pg_catalog.pg_constraint AS constraint_definition
                    JOIN pg_catalog.pg_class AS source_relation
                      ON source_relation.oid = constraint_definition.conrelid
                    JOIN pg_catalog.pg_namespace AS source_namespace
                      ON source_namespace.oid = source_relation.relnamespace
                    JOIN pg_catalog.pg_class AS referenced_relation
                      ON referenced_relation.oid = constraint_definition.confrelid
                    JOIN pg_catalog.pg_namespace AS referenced_namespace
                      ON referenced_namespace.oid = referenced_relation.relnamespace
                    WHERE constraint_definition.contype = 'f'
                      AND source_namespace.nspname IN ('aqa', 'market_data')
                    ORDER BY referenced_namespace.nspname, referenced_relation.relname
                    """
                )
            )
        )
        observed_columns = frozenset(
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                text(
                    """
                    SELECT namespace.nspname,
                           relation.relname,
                           attribute.attname,
                           privilege.privilege_type
                    FROM pg_catalog.pg_attribute AS attribute
                    JOIN pg_catalog.pg_class AS relation
                      ON relation.oid = attribute.attrelid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS privilege
                    JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = privilege.grantee
                    WHERE namespace.nspname IN ('aqa', 'market_data')
                      AND relation.relkind IN ('r', 'p')
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                      AND grantee.rolname = 'aqa_migrate'
                    ORDER BY namespace.nspname, relation.relname, attribute.attname,
                             privilege.privilege_type
                    """
                )
            )
        )

        assert referenced_relations == expected_relations
        assert observed_columns == expected_columns
        for schema_name, table_name in expected_relations:
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not connection.scalar(
                    text("SELECT has_table_privilege('aqa_migrate', :qualified_name, :privilege)"),
                    {
                        "qualified_name": f"{schema_name}.{table_name}",
                        "privilege": privilege,
                    },
                )


def test_nonmember_cannot_bypass_governed_migration_role(
    provisioned_engine: Engine,
) -> None:
    known_revisions, governed_revisions = _migration_revision_sets()
    with provisioned_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("GRANT USAGE ON SCHEMA market_data TO aqa_readonly"))
            connection.execute(
                text("GRANT SELECT ON TABLE market_data.alembic_version TO aqa_readonly")
            )
            connection.execute(text("SET LOCAL SESSION AUTHORIZATION aqa_readonly"))
            assert connection.scalar(text("SELECT session_user = 'aqa_readonly'"))

            with pytest.raises(MigrationRoleActivationError, match="cannot assume"):
                activate_platform_migration_role(
                    connection,
                    known_revisions=known_revisions,
                    governed_revisions=governed_revisions,
                )

            assert connection.scalar(text("SELECT current_user = 'aqa_readonly'"))
        finally:
            transaction.rollback()


def test_control_can_append_audit_prerequisites_but_cannot_mutate_history(
    provisioned_engine: Engine,
) -> None:
    with _connection_as(provisioned_engine, "aqa_control") as connection:
        connection.execute(text("SELECT event_hash FROM aqa.aqa_audit_events_v LIMIT 0"))
        connection.execute(
            text(
                "INSERT INTO aqa.aqa_audit_events (audit_event_id) "
                "SELECT 'permission_probe' WHERE FALSE"
            )
        )

    _assert_permission_denied(
        provisioned_engine,
        "aqa_control",
        text("SELECT event_hash FROM aqa.aqa_audit_events LIMIT 0"),
    )
    _assert_permission_denied(
        provisioned_engine,
        "aqa_control",
        text("UPDATE aqa.aqa_audit_events SET actor = actor WHERE FALSE"),
    )
    _assert_permission_denied(
        provisioned_engine,
        "aqa_control",
        text("DELETE FROM aqa.aqa_audit_events WHERE FALSE"),
    )


def test_readonly_can_verify_audit_view_without_base_table_authority(
    provisioned_engine: Engine,
) -> None:
    with _connection_as(provisioned_engine, "aqa_readonly") as connection:
        connection.execute(
            text(
                "SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor, "
                "occurred_at, payload, payload_hash, event_hash, content_hash "
                "FROM aqa.aqa_audit_events_v LIMIT 0"
            )
        )

    _assert_permission_denied(
        provisioned_engine,
        "aqa_readonly",
        text("SELECT event_hash FROM aqa.aqa_audit_events LIMIT 0"),
    )
    _assert_permission_denied(
        provisioned_engine,
        "aqa_readonly",
        text(
            "INSERT INTO aqa.aqa_audit_events (audit_event_id) "
            "SELECT 'permission_probe' WHERE FALSE"
        ),
    )


@pytest.mark.parametrize("role", _RUNTIME_ROLES)
def test_each_runtime_role_can_read_only_its_authorized_audit_view(
    provisioned_engine: Engine,
    role: str,
) -> None:
    with _connection_as(provisioned_engine, role) as connection:
        connection.execute(
            text(
                "SELECT audit_event_id, payload, payload_hash, event_hash "
                f"FROM aqa.{_AUDIT_READ_VIEW_BY_ROLE[role]} LIMIT 0"
            )
        )

    if role not in {"aqa_control", "aqa_readonly"}:
        _assert_permission_denied(
            provisioned_engine,
            role,
            text("SELECT event_hash FROM aqa.aqa_audit_events_v LIMIT 0"),
        )


@pytest.mark.parametrize("role", _RUNTIME_ROLES)
def test_each_real_service_login_can_read_authorized_audit_view_but_not_base_table(
    provisioned_engine: Engine,
    platform_login_database_urls: Mapping[str, str],
    role: str,
) -> None:
    del provisioned_engine
    login_engine = create_engine(
        normalize_postgres_url(platform_login_database_urls[role]),
        hide_parameters=True,
        connect_args=postgres_connect_args(f"platform-{role}-login-test"),
    )
    try:
        with login_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT audit_event_id, payload, payload_hash, event_hash "
                    f"FROM aqa.{_AUDIT_READ_VIEW_BY_ROLE[role]} LIMIT 0"
                )
            )
        with login_engine.connect() as connection, pytest.raises(DBAPIError) as exc_info:
            connection.execute(text("SELECT event_hash FROM aqa.aqa_audit_events LIMIT 0"))
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"
    finally:
        login_engine.dispose()


@pytest.mark.parametrize("role", tuple(_SERVICE_WRITER_BY_ROLE))
def test_each_real_service_login_appends_and_verifies_through_audit_repository(
    provisioned_engine: Engine,
    platform_login_database_urls: Mapping[str, str],
    role: str,
) -> None:
    del provisioned_engine
    writer = _SERVICE_WRITER_BY_ROLE[role]
    stream_id = f"{role}:repository-login"
    login_engine = create_engine(
        normalize_postgres_url(platform_login_database_urls[role]),
        hide_parameters=True,
        connect_args=postgres_connect_args(f"platform-{role}-repository-test"),
    )
    try:
        repository = AuditRepository(login_engine, writer=writer)
        event = repository.append(
            stream_id=stream_id,
            event_type=_AUDIT_EVENT_TYPE_BY_ROLE[role],
            occurred_at=datetime(2026, 9, 5, 20, 0, tzinfo=UTC),
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"service_{sha256_hex(('service-login', role))}",
                    "state": "verified",
                }
            ),
        )
        report = repository.verify(
            stream_id=stream_id,
            expected_sequence=1,
            expected_hash=event.event_hash,
        )
    finally:
        login_engine.dispose()

    assert report.event_count == 1
    assert report.stream_heads[0].stream_id == stream_id


def test_readonly_login_verifies_repository_evidence_without_write_authority(
    provisioned_engine: Engine,
    platform_login_database_urls: Mapping[str, str],
) -> None:
    writer_engine = create_engine(
        normalize_postgres_url(platform_login_database_urls["aqa_control"]),
        hide_parameters=True,
        connect_args=postgres_connect_args("platform-control-audit-seed"),
    )
    stream_id = "aqa_control:readonly-repository-verification"
    try:
        event = AuditRepository(writer_engine, writer=AuditWriter.CONTROL).append(
            stream_id=stream_id,
            event_type="job.started",
            occurred_at=datetime(2026, 9, 5, 20, 1, tzinfo=UTC),
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"readonly_{sha256_hex(('readonly-login', stream_id))}",
                    "state": "running",
                }
            ),
        )
    finally:
        writer_engine.dispose()

    readonly_engine = create_engine(
        normalize_postgres_url(platform_login_database_urls["aqa_readonly"]),
        hide_parameters=True,
        connect_args=postgres_connect_args("platform-readonly-audit-verify"),
    )
    try:
        report = AuditRepository(readonly_engine).verify(
            stream_id=stream_id,
            expected_sequence=1,
            expected_hash=event.event_hash,
        )
        with pytest.raises(AuditPersistenceError):
            AuditRepository(readonly_engine, writer=AuditWriter.CONTROL).append(
                stream_id="aqa_control:readonly-forbidden",
                event_type="job.started",
                occurred_at=datetime(2026, 9, 5, 20, 2, tzinfo=UTC),
                payload=AuditPayload.from_mapping(
                    {"idempotency_key": (f"readonly_{sha256_hex(('readonly-login', 'forbidden'))}")}
                ),
            )
    finally:
        readonly_engine.dispose()

    assert report.event_count == 1


def _collector_observation(*, close: str, correction: bool) -> RawBarObservationV1:
    timestamp = datetime(2026, 9, 5, 20, 10, tzinfo=UTC)
    close_value = Decimal(close)
    source = "iex_updated_bar" if correction else "iex_bar"
    bar = MarketBarV1(
        provider="alpaca",
        feed="IEX",
        adjustment="raw",
        symbol="AAOI",
        timeframe="1m",
        bar_timestamp_utc=timestamp,
        provider_event_timestamp_utc=timestamp + timedelta(seconds=59),
        receipt_timestamp_utc=timestamp + timedelta(seconds=65 if not correction else 75),
        open=Decimal("100"),
        high=max(Decimal("101"), close_value),
        low=min(Decimal("99"), close_value),
        close=close_value,
        volume=1_000,
        trade_count=20,
        vwap=Decimal("100.25"),
        quality_flags=frozenset(),
        source=source,
    )
    return RawBarObservationV1(
        bar=bar,
        is_correction=correction,
        raw_payload_json=f'{{"close":"{close}","source":"{source}"}}',
    )


def _platform_bar(*, identity: BarIdentity, close: str, delivery: str) -> BarWrite:
    close_value = Decimal(close)
    return BarWrite(
        identity=identity,
        received_at=identity.end_at + timedelta(seconds=30 if delivery == "original" else 45),
        provider_timestamp=identity.end_at,
        open=Decimal("100"),
        high=max(Decimal("101"), close_value),
        low=min(Decimal("99"), close_value),
        close=close_value,
        volume=Decimal("1000"),
        trade_count=20,
        vwap=Decimal("100.25"),
        quality_flags=("complete",),
        source="alpaca",
        source_event_id=f"service-login-{delivery}",
        source_payload_hash=sha256_hex(("platform-service-login", delivery)),
    )


def test_real_collector_login_runs_the_platform_market_data_repository_flow(
    provisioned_engine: Engine,
    platform_login_database_urls: Mapping[str, str],
) -> None:
    del provisioned_engine
    engine = create_engine(
        normalize_postgres_url(platform_login_database_urls["aqa_collector"]),
        hide_parameters=True,
        connect_args=postgres_connect_args("platform-market-data-repository-login-test"),
    )
    identity = BarIdentity(
        provider="alpaca",
        feed="iex",
        adjustment="raw",
        symbol="AAOI",
        timeframe="1Min",
        start_at=datetime(2026, 9, 5, 20, 20, tzinfo=UTC),
        end_at=datetime(2026, 9, 5, 20, 21, tzinfo=UTC),
    )
    repository = MarketDataRepository(engine)
    try:
        inserted = repository.append(
            _platform_bar(identity=identity, close="100", delivery="original")
        )
        corrected = repository.append(
            _platform_bar(identity=identity, close="102", delivery="correction")
        )
        history = repository.list_events(identity)
        latest = repository.latest(identity)
    finally:
        engine.dispose()

    assert inserted.status is BarWriteStatus.INSERTED
    assert corrected.status is BarWriteStatus.CORRECTED
    assert tuple(event.revision for event in history) == (1, 2)
    assert history[1].correction_of_event_id == history[0].bar_event_id
    assert latest == corrected.event


def test_real_collector_login_runs_the_legacy_market_data_repository_flow(
    provisioned_engine: Engine,
    platform_login_database_urls: Mapping[str, str],
) -> None:
    del provisioned_engine
    repository = PostgresMarketDataRepository(
        platform_login_database_urls["aqa_collector"],
        application_name="platform-collector-repository-test",
    )
    original = _collector_observation(close="100", correction=False)
    correction = _collector_observation(close="102", correction=True)
    key = CheckpointKey("historical", "alpaca", "IEX", "raw", "AAOI", "1m")
    try:
        repository.verify_schema()
        repository.register_universe()
        lease = repository.try_acquire_lease(
            lease_name="platform-collector-repository",
            holder_id="service-login-test",
            ttl_seconds=120,
        )
        assert lease is not None
        run_id = repository.start_run(mode="backfill", lease=lease)
        first = repository.append_batch(
            (original,),
            lease=lease,
            coverage_advances=(
                CoverageAdvance(
                    key=key,
                    committed_through_utc=original.bar.bar_timestamp_utc + timedelta(minutes=1),
                    metadata={"source": "service-login-test"},
                ),
            ),
        )
        corrected = repository.append_batch((correction,), lease=lease)
        checkpoint = repository.checkpoints(checkpoint_name="historical")["AAOI"]
        event_id = repository.record_event(
            event_type="collector.repository_verified",
            lease=lease,
            run_id=run_id,
            symbol="AAOI",
            details={"checkpoint_version": checkpoint.version},
        )
        repository.finish_run(
            run_id,
            lease=lease,
            status="completed",
            counters={"observations": 2},
        )
        assert repository.release_lease(lease) is True
        with repository.engine.connect() as connection:
            persisted = connection.scalar(
                select(bar_observations.c.observation_id).where(
                    bar_observations.c.observation_id == correction.observation_id
                )
            )
            current = connection.execute(
                select(current_bars.c.current_observation_id, current_bars.c.revision).where(
                    current_bars.c.identity_hash == original.identity_hash
                )
            ).one()
    finally:
        repository.close()

    assert first.observations_inserted == 1
    assert corrected.current_rows_revised == 1
    assert checkpoint.committed_through_utc == original.bar.bar_timestamp_utc + timedelta(minutes=1)
    assert event_id
    assert persisted == correction.observation_id
    assert current == (correction.observation_id, 2)


@pytest.mark.parametrize("role", tuple(_AUDIT_EVENT_TYPE_BY_ROLE))
def test_audit_writer_policy_accepts_own_actor_stream_and_event_family(
    provisioned_engine: Engine,
    role: str,
) -> None:
    with _connection_as(provisioned_engine, role) as connection:
        connection.execute(
            _audit_insert(
                actor=role,
                stream_actor=role,
                event_type=_AUDIT_EVENT_TYPE_BY_ROLE[role],
            )
        )


@pytest.mark.parametrize("role", tuple(_AUDIT_EVENT_TYPE_BY_ROLE))
@pytest.mark.parametrize("forgery", ("actor", "stream", "event-family"))
def test_audit_writer_policy_rejects_cross_boundary_forgery(
    provisioned_engine: Engine,
    role: str,
    forgery: str,
) -> None:
    actor = "aqa_control" if role != "aqa_control" else "aqa_execution"
    stream_actor = "aqa_control" if role != "aqa_control" else "aqa_execution"
    event_type = "job.started" if role != "aqa_control" else "order.submitted"
    statement = _audit_insert(
        actor=actor if forgery == "actor" else role,
        stream_actor=stream_actor if forgery == "stream" else role,
        event_type=event_type if forgery == "event-family" else _AUDIT_EVENT_TYPE_BY_ROLE[role],
    )

    _assert_permission_denied(provisioned_engine, role, statement)


def test_roles_are_non_login_unprivileged_groups_without_inherited_authority(
    provisioned_engine: Engine,
) -> None:
    with provisioned_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                       rolcreaterole, rolreplication, rolbypassrls, rolconfig,
                       rolconnlimit, rolvaliduntil
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(:role_names)
                ORDER BY rolname
                """
            ),
            {"role_names": list(_ROLES)},
        ).mappings()
        observed = {row["rolname"]: row for row in rows}
        memberships = connection.execute(
            text(
                """
                SELECT member_role.rolname, inherited_role.rolname
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS inherited_role ON inherited_role.oid = membership.roleid
                WHERE member_role.rolname = ANY(:role_names)
                """
            ),
            {"role_names": list(_ROLES)},
        ).all()
        login_rows = connection.execute(
            text(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb,
                       rolcreaterole, rolreplication, rolbypassrls, rolconfig,
                       rolconnlimit, rolvaliduntil
                FROM pg_catalog.pg_roles
                WHERE rolname = ANY(:role_names)
                ORDER BY rolname
                """
            ),
            {"role_names": list(LOGIN_ROLES)},
        ).mappings()
        observed_logins = {row["rolname"]: row for row in login_rows}
        login_memberships = connection.execute(
            text(
                """
                SELECT granted_role.rolname, member_role.rolname,
                       membership.admin_option, membership.inherit_option,
                       membership.set_option
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
                JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
                WHERE member_role.rolname = ANY(:role_names)
                ORDER BY granted_role.rolname
                """
            ),
            {"role_names": list(LOGIN_ROLES)},
        ).all()
        dashboard_exists = connection.scalar(
            text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aqa_dashboard')")
        )

    assert frozenset(observed) == frozenset(_ROLES)
    for role in _ROLES:
        attributes = observed[role]
        assert attributes["rolcanlogin"] is False
        assert attributes["rolsuper"] is False
        assert attributes["rolinherit"] is True
        assert attributes["rolcreatedb"] is False
        assert attributes["rolcreaterole"] is False
        assert attributes["rolreplication"] is False
        assert attributes["rolbypassrls"] is False
        assert attributes["rolconfig"] is None
        assert attributes["rolconnlimit"] == -1
        assert attributes["rolvaliduntil"] is None
    assert memberships == []
    assert frozenset(observed_logins) == frozenset(LOGIN_ROLES)
    for role in LOGIN_ROLES:
        attributes = observed_logins[role]
        assert attributes["rolcanlogin"] is True
        assert attributes["rolsuper"] is False
        assert attributes["rolinherit"] is True
        assert attributes["rolcreatedb"] is False
        assert attributes["rolcreaterole"] is False
        assert attributes["rolreplication"] is False
        assert attributes["rolbypassrls"] is False
        assert attributes["rolconfig"] is None
        assert attributes["rolconnlimit"] == -1
        assert attributes["rolvaliduntil"] is None
    assert frozenset(login_memberships) == frozenset(
        (
            role,
            LOGIN_ROLE_BY_AUTHORIZATION_ROLE[role],
            False,
            True,
            True,
        )
        for role in _ROLES
    )
    assert dashboard_exists is False


def test_roles_have_only_the_explicit_relation_grants(provisioned_engine: Engine) -> None:
    with provisioned_engine.connect() as connection:
        for role in _RUNTIME_ROLES:
            assert _effective_table_grants(connection, role) == _EXPECTED_GRANTS[role]


def test_schema_database_and_ownership_boundaries_are_fail_closed(
    provisioned_engine: Engine,
) -> None:
    inspector = inspect(provisioned_engine)
    with provisioned_engine.connect() as connection:
        schema_owner = connection.scalar(
            text(
                """
                SELECT owner.rolname
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
                WHERE namespace.nspname = 'aqa'
                """
            )
        )
        market_data_schema_owner = connection.scalar(
            text(
                """
                SELECT owner.rolname
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
                WHERE namespace.nspname = 'market_data'
                """
            )
        )
        object_owners = connection.execute(
            text(
                """
                SELECT DISTINCT owner.rolname
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = 'aqa'
                  AND relation.relkind IN ('r', 'p', 'v', 'S')
                """
            )
        ).scalars()
        market_data_object_owners = connection.execute(
            text(
                """
                SELECT DISTINCT owner.rolname
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
                WHERE namespace.nspname = 'market_data'
                  AND relation.relkind IN ('r', 'p', 'v', 'S')
                """
            )
        ).scalars()
        market_data_function_owners = connection.execute(
            text(
                """
                SELECT DISTINCT owner.rolname
                FROM pg_catalog.pg_proc AS routine
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = routine.pronamespace
                JOIN pg_catalog.pg_roles AS owner ON owner.oid = routine.proowner
                WHERE namespace.nspname = 'market_data'
                """
            )
        ).scalars()

        public_database_acl = text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS database
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(database.datacl, pg_catalog.acldefault('d', database.datdba))
                ) AS privilege
                WHERE database.datname = current_database()
                  AND privilege.grantee = 0
                  AND privilege.privilege_type = :privilege
            )
            """
        )
        assert connection.scalar(public_database_acl, {"privilege": "CONNECT"})
        assert not connection.scalar(public_database_acl, {"privilege": "TEMPORARY"})

        for role in _ROLES:
            assert connection.scalar(
                text("SELECT has_database_privilege(:role, current_database(), 'CONNECT')"),
                {"role": role},
            )
            assert not connection.scalar(
                text("SELECT has_database_privilege(:role, current_database(), 'TEMP')"),
                {"role": role},
            )
            assert not connection.scalar(
                text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"),
                {"role": role},
            )
            assert not connection.scalar(
                text("SELECT has_schema_privilege(:role, 'public', 'USAGE')"),
                {"role": role},
            )

        assert connection.scalar(
            text("SELECT has_database_privilege('aqa_migrate', current_database(), 'CREATE')")
        )
        assert connection.scalar(
            text("SELECT has_schema_privilege('aqa_migrate', 'aqa', 'CREATE')")
        )
        assert connection.scalar(
            text("SELECT has_schema_privilege('aqa_migrate', 'market_data', 'CREATE')")
        )
        for table_name in PLATFORM_TABLE_NAMES:
            qualified_name = f"aqa.{table_name}"
            assert connection.scalar(
                text("SELECT has_table_privilege('aqa_migrate', :table_name, 'SELECT')"),
                {"table_name": qualified_name},
            )
            for privilege in (
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
            ):
                assert not connection.scalar(
                    text("SELECT has_table_privilege('aqa_migrate', :table_name, :privilege)"),
                    {"table_name": qualified_name, "privilege": privilege},
                )
            for privilege in ("REFERENCES", "TRIGGER"):
                assert connection.scalar(
                    text("SELECT has_table_privilege('aqa_migrate', :table_name, :privilege)"),
                    {"table_name": qualified_name, "privilege": privilege},
                )
        for role in _RUNTIME_ROLES:
            assert not connection.scalar(
                text("SELECT has_database_privilege(:role, current_database(), 'CREATE')"),
                {"role": role},
            )
            assert connection.scalar(
                text("SELECT has_schema_privilege(:role, 'aqa', 'USAGE')"),
                {"role": role},
            )
            assert not connection.scalar(
                text("SELECT has_schema_privilege(:role, 'aqa', 'CREATE')"),
                {"role": role},
            )
            has_market_data_usage = connection.scalar(
                text("SELECT has_schema_privilege(:role, 'market_data', 'USAGE')"),
                {"role": role},
            )
            has_legacy_select = connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    ":role, 'market_data.collection_universes', 'SELECT')"
                ),
                {"role": role},
            )
            assert has_market_data_usage is (role == "aqa_collector")
            assert has_legacy_select is (role == "aqa_collector")

    assert schema_owner == "aqa_migrate"
    assert market_data_schema_owner == "aqa_migrate"
    assert frozenset(object_owners) == {"aqa_migrate"}
    assert frozenset(market_data_object_owners) == {"aqa_migrate"}
    assert frozenset(market_data_function_owners) == {"aqa_migrate"}
    assert frozenset(inspector.get_table_names(schema=PLATFORM_SCHEMA)) == PLATFORM_TABLE_NAMES
    assert frozenset(inspector.get_view_names(schema=PLATFORM_SCHEMA)) == _VIEWS
    assert inspector.get_sequence_names(schema=PLATFORM_SCHEMA) == []


def test_migration_role_can_apply_ddl_and_maintain_alembic_revision(
    provisioned_engine: Engine,
) -> None:
    with _connection_as(provisioned_engine, "aqa_migrate") as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM market_data.alembic_version"))
            == "20260905_0006"
        )
        assert connection.scalar(
            text("SELECT has_schema_privilege('aqa_migrate', 'market_data', 'CREATE')")
        )
        assert connection.scalar(
            text(
                "SELECT has_table_privilege("
                "'aqa_migrate', 'market_data.collection_universes', 'SELECT')"
            )
        )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'aqa_migrate', 'market_data.alembic_version', :privilege)"
                ),
                {"privilege": privilege},
            )
        assert not connection.scalar(
            text(
                "SELECT has_table_privilege("
                "'aqa_migrate', 'market_data.alembic_version', 'TRUNCATE')"
            )
        )
        for privilege in ("REFERENCES", "TRIGGER"):
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'aqa_migrate', 'market_data.alembic_version', :privilege)"
                ),
                {"privilege": privilege},
            )

        connection.execute(text("CREATE SCHEMA aqa_migration_probe"))
        for role in _RUNTIME_ROLES:
            assert not connection.scalar(
                text("SELECT has_schema_privilege(:role, 'aqa_migration_probe', 'USAGE')"),
                {"role": role},
            )
            assert not connection.scalar(
                text("SELECT has_schema_privilege(:role, 'aqa_migration_probe', 'CREATE')"),
                {"role": role},
            )
        connection.execute(text("CREATE TABLE aqa.aqa_migration_probe (value integer)"))
        assert connection.scalar(
            text("SELECT has_table_privilege('aqa_migrate', 'aqa.aqa_migration_probe', 'SELECT')")
        )
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'aqa_migrate', 'aqa.aqa_migration_probe', :privilege)"
                ),
                {"privilege": privilege},
            )
        connection.execute(
            text(
                "ALTER TABLE aqa.aqa_migration_probe ADD COLUMN version integer NOT NULL DEFAULT 1"
            )
        )
        connection.execute(text("GRANT SELECT ON TABLE aqa.aqa_migration_probe TO aqa_readonly"))
        connection.execute(
            text("UPDATE market_data.alembic_version SET version_num = version_num WHERE FALSE")
        )
        connection.execute(
            text(
                "INSERT INTO market_data.alembic_version (version_num) "
                "SELECT 'permission_probe' WHERE FALSE"
            )
        )
        connection.execute(text("DELETE FROM market_data.alembic_version WHERE FALSE"))

    _assert_permission_denied(
        provisioned_engine,
        "aqa_migrate",
        text("CREATE TABLE public.aqa_unauthorized_probe (value integer)"),
    )


def test_migration_role_default_privileges_deny_future_public_access(
    provisioned_engine: Engine,
) -> None:
    with _connection_as(provisioned_engine, "aqa_migrate") as connection:
        connection.execute(text("CREATE TABLE aqa.aqa_default_table_probe (value integer)"))
        connection.execute(text("CREATE TABLE market_data.aqa_default_table_probe (value integer)"))
        connection.execute(text("CREATE SEQUENCE aqa.aqa_default_sequence_probe"))
        connection.execute(text("CREATE TYPE aqa.aqa_default_type_probe AS ENUM ('value')"))
        connection.execute(
            text(
                "CREATE FUNCTION aqa.aqa_default_function_probe() "
                "RETURNS integer LANGUAGE SQL AS 'SELECT 1'"
            )
        )

        for table_name in (
            "aqa.aqa_default_table_probe",
            "market_data.aqa_default_table_probe",
        ):
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not connection.scalar(
                    text("SELECT has_table_privilege('aqa_migrate', :table_name, :privilege)"),
                    {"table_name": table_name, "privilege": privilege},
                )

        for privilege in ("USAGE", "SELECT", "UPDATE"):
            assert connection.scalar(
                text(
                    "SELECT has_sequence_privilege("
                    "'aqa_migrate', 'aqa.aqa_default_sequence_probe', :privilege)"
                ),
                {"privilege": privilege},
            )

        for role in _RUNTIME_ROLES:
            for table_name in (
                "aqa.aqa_default_table_probe",
                "market_data.aqa_default_table_probe",
            ):
                assert not connection.scalar(
                    text("SELECT has_table_privilege(:role, :table_name, 'SELECT')"),
                    {"role": role, "table_name": table_name},
                )
            assert not connection.scalar(
                text(
                    "SELECT has_sequence_privilege("
                    ":role, 'aqa.aqa_default_sequence_probe', 'USAGE')"
                ),
                {"role": role},
            )
            assert not connection.scalar(
                text("SELECT has_type_privilege(:role, 'aqa.aqa_default_type_probe', 'USAGE')"),
                {"role": role},
            )
            assert not connection.scalar(
                text(
                    "SELECT has_function_privilege("
                    ":role, 'aqa.aqa_default_function_probe()', 'EXECUTE')"
                ),
                {"role": role},
            )


@pytest.mark.parametrize("role", _ROLES)
def test_each_role_allows_one_bounded_operation_and_denies_one_out_of_boundary_operation(
    provisioned_engine: Engine,
    role: str,
) -> None:
    with _connection_as(provisioned_engine, role) as connection:
        connection.execute(_ALLOWED_STATEMENTS[role])

    _assert_permission_denied(provisioned_engine, role, _DENIED_STATEMENTS[role])


@pytest.mark.parametrize("role", _RUNTIME_ROLES)
def test_runtime_roles_cannot_create_schema_objects_or_temporary_tables(
    provisioned_engine: Engine,
    role: str,
) -> None:
    _assert_permission_denied(
        provisioned_engine,
        role,
        text("CREATE TABLE aqa.aqa_unauthorized_probe (value integer)"),
    )
    _assert_permission_denied(
        provisioned_engine,
        role,
        text("CREATE TEMPORARY TABLE aqa_unauthorized_temp_probe (value integer)"),
    )
