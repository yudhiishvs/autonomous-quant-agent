"""Offline tests for fail-closed Alembic migration-role activation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event

from adaptive_trader.platform.storage.migration_roles import (
    MIGRATION_ROLE_REVISION,
    MigrationRoleActivationError,
    activate_platform_migration_role,
    migration_role_revision_sets,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRE_GOVERNANCE_REVISIONS = frozenset({"20260903_0001", "20260905_0002", "20260905_0003"})


def _revision_sets() -> tuple[frozenset[str], frozenset[str]]:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    return migration_role_revision_sets(ScriptDirectory.from_config(config))


def _postgres_connection(
    *,
    version_table_exists: bool,
    revisions: tuple[str, ...] = (),
    role_is_safe: bool = True,
    can_set_role: bool = False,
) -> MagicMock:
    connection = MagicMock()
    connection.dialect.name = "postgresql"
    connection.scalar.side_effect = [version_table_exists, role_is_safe, can_set_role]
    connection.scalars.return_value = revisions
    return connection


def test_revision_policy_uses_graph_ancestry_for_governed_revisions() -> None:
    known_revisions, governed_revisions = _revision_sets()

    assert MIGRATION_ROLE_REVISION in governed_revisions
    assert known_revisions >= _PRE_GOVERNANCE_REVISIONS
    assert governed_revisions == known_revisions - _PRE_GOVERNANCE_REVISIONS


def test_non_postgresql_connections_are_untouched() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _connection, _cursor, statement, _parameters, _context, _many: statements.append(
            statement
        ),
    )
    known_revisions, governed_revisions = _revision_sets()

    try:
        with engine.connect() as connection:
            activated = activate_platform_migration_role(
                connection,
                known_revisions=known_revisions,
                governed_revisions=governed_revisions,
            )
    finally:
        engine.dispose()

    assert activated is False
    assert statements == []


def test_empty_database_activates_the_prebootstrapped_migration_role() -> None:
    known_revisions, governed_revisions = _revision_sets()
    connection = _postgres_connection(version_table_exists=False, can_set_role=True)
    connection.scalar.side_effect = [False, True, True, True]

    activated = activate_platform_migration_role(
        connection,
        known_revisions=known_revisions,
        governed_revisions=governed_revisions,
    )

    assert activated is True
    connection.scalars.assert_not_called()
    assert str(connection.execute.call_args.args[0]) == "SET ROLE aqa_migrate"


def test_empty_database_rejects_a_missing_or_unsafe_bootstrap_role() -> None:
    known_revisions, governed_revisions = _revision_sets()
    connection = _postgres_connection(
        version_table_exists=False,
        role_is_safe=False,
    )

    with pytest.raises(MigrationRoleActivationError, match="safely bootstrapped"):
        activate_platform_migration_role(
            connection,
            known_revisions=known_revisions,
            governed_revisions=governed_revisions,
        )

    connection.execute.assert_not_called()


def test_governed_revision_activates_only_the_static_migration_role() -> None:
    known_revisions, governed_revisions = _revision_sets()
    connection = _postgres_connection(
        version_table_exists=True,
        revisions=(MIGRATION_ROLE_REVISION,),
        can_set_role=True,
    )
    connection.scalar.side_effect = [True, True, True, True]

    activated = activate_platform_migration_role(
        connection,
        known_revisions=known_revisions,
        governed_revisions=governed_revisions,
    )

    assert activated is True
    assert connection.execute.call_count == 1
    assert str(connection.execute.call_args.args[0]) == "SET ROLE aqa_migrate"


def test_known_pre_governance_revision_retains_owner_after_validating_role_boundary() -> None:
    known_revisions, governed_revisions = _revision_sets()
    connection = _postgres_connection(
        version_table_exists=True,
        revisions=("20260905_0003",),
        can_set_role=True,
    )

    activated = activate_platform_migration_role(
        connection,
        known_revisions=known_revisions,
        governed_revisions=governed_revisions,
    )

    assert activated is False
    connection.execute.assert_not_called()


def test_governed_revision_rejects_a_connection_that_cannot_set_the_role() -> None:
    known_revisions, governed_revisions = _revision_sets()
    connection = _postgres_connection(
        version_table_exists=True,
        revisions=(MIGRATION_ROLE_REVISION,),
        can_set_role=False,
    )

    with pytest.raises(MigrationRoleActivationError, match="cannot assume"):
        activate_platform_migration_role(
            connection,
            known_revisions=known_revisions,
            governed_revisions=governed_revisions,
        )

    connection.execute.assert_not_called()


def test_unknown_database_revision_fails_closed_before_role_activation() -> None:
    known_revisions, governed_revisions = _revision_sets()
    connection = _postgres_connection(
        version_table_exists=True,
        revisions=("unknown_revision",),
        can_set_role=True,
    )

    with pytest.raises(MigrationRoleActivationError, match="not recognized"):
        activate_platform_migration_role(
            connection,
            known_revisions=known_revisions,
            governed_revisions=governed_revisions,
        )

    connection.execute.assert_not_called()
