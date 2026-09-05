"""Offline tests for the one-time PostgreSQL cluster-role bootstrap."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import adaptive_trader.platform.storage.role_bootstrap as role_bootstrap
from adaptive_trader.platform.security import RedactedSecret, bootstrap_local_secrets
from adaptive_trader.platform.storage.role_bootstrap import (
    ALL_PLATFORM_ROLES,
    AUTHORIZATION_ROLES,
    LOGIN_ROLE_BY_AUTHORIZATION_ROLE,
    LOGIN_ROLES,
    PlatformRoleBootstrapError,
    load_platform_role_password,
)


def _local_secret_root(tmp_path: Path) -> Path:
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    bootstrap_local_secrets(root)
    return root


def test_role_inventory_has_separate_fixed_authorization_and_login_principals() -> None:
    assert AUTHORIZATION_ROLES == (
        "aqa_migrate",
        "aqa_collector",
        "aqa_scheduler",
        "aqa_strategy",
        "aqa_execution",
        "aqa_control",
        "aqa_readonly",
    )
    assert tuple(f"{role}_login" for role in AUTHORIZATION_ROLES) == LOGIN_ROLES
    assert ALL_PLATFORM_ROLES == AUTHORIZATION_ROLES + LOGIN_ROLES
    assert set(LOGIN_ROLE_BY_AUTHORIZATION_ROLE) == set(AUTHORIZATION_ROLES)


def test_role_passwords_load_only_from_generated_owner_private_files(tmp_path: Path) -> None:
    root = _local_secret_root(tmp_path)

    loaded = load_platform_role_password(root, "aqa_migrate")

    assert type(loaded) is RedactedSecret
    assert str(loaded) == "<redacted>"
    assert repr(loaded) == "<redacted>"
    assert len(loaded.reveal().encode("utf-8")) >= 32


def test_role_password_loader_rejects_unknown_role_without_rendering_input(tmp_path: Path) -> None:
    root = _local_secret_root(tmp_path)
    unknown = "UNSUPPORTED_ROLE_INPUT"

    with pytest.raises(PlatformRoleBootstrapError) as captured:
        load_platform_role_password(root, unknown)

    assert unknown not in str(captured.value)
    assert str(root) not in str(captured.value)


def test_create_role_uses_fixed_identifier_quoting_and_removes_creator_membership() -> None:
    connection = MagicMock()

    role_bootstrap._create_role(connection, "aqa_migrate", login=False)

    rendered = [call.args[0].as_string() for call in connection.execute.call_args_list]
    assert rendered == [
        'CREATE ROLE "aqa_migrate" NOLOGIN NOSUPERUSER INHERIT NOCREATEDB '
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        'REVOKE "aqa_migrate" FROM CURRENT_USER',
    ]
    assert all("PASSWORD" not in statement for statement in rendered)


def test_initial_migration_authority_is_scoped_to_the_connected_database() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = ('database "fixture"',)

    role_bootstrap._grant_initial_migration_database_authority(connection)

    rendered = [
        call.args[0].as_string()
        for call in connection.execute.call_args_list
        if hasattr(call.args[0], "as_string")
    ]
    assert rendered == [
        'REVOKE ALL PRIVILEGES ON DATABASE "database ""fixture""" FROM '
        "aqa_migrate GRANTED BY aqa_migrate",
        'REVOKE ALL PRIVILEGES ON DATABASE "database ""fixture""" FROM '
        '"aqa_migrate", "aqa_collector", "aqa_scheduler", "aqa_strategy", '
        '"aqa_execution", "aqa_control", "aqa_readonly", "aqa_migrate_login", '
        '"aqa_collector_login", "aqa_scheduler_login", "aqa_strategy_login", '
        '"aqa_execution_login", "aqa_control_login", "aqa_readonly_login"',
        'REVOKE CREATE, TEMPORARY ON DATABASE "database ""fixture""" FROM PUBLIC',
        'GRANT CONNECT ON DATABASE "database ""fixture""" TO aqa_migrate',
        'GRANT CREATE ON DATABASE "database ""fixture""" TO aqa_migrate WITH GRANT OPTION',
    ]
    executed = [call.args[0] for call in connection.execute.call_args_list]
    assert executed[1] == "SET ROLE aqa_migrate"
    assert executed[3] == "RESET ROLE"


def test_public_schema_bootstrap_removes_implicit_and_direct_platform_access() -> None:
    connection = MagicMock()

    role_bootstrap._harden_public_schema(connection)

    rendered = connection.execute.call_args.args[0].as_string()
    assert rendered == (
        'REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC, "aqa_migrate", '
        '"aqa_collector", "aqa_scheduler", "aqa_strategy", "aqa_execution", '
        '"aqa_control", "aqa_readonly", "aqa_migrate_login", "aqa_collector_login", '
        '"aqa_scheduler_login", "aqa_strategy_login", "aqa_execution_login", '
        '"aqa_control_login", "aqa_readonly_login"'
    )


def test_password_change_uses_libpq_boundary_and_never_executes_password_sql(
    tmp_path: Path,
) -> None:
    password = load_platform_role_password(_local_secret_root(tmp_path), "aqa_migrate")
    connection = MagicMock()

    role_bootstrap._change_password(connection, "aqa_migrate_login", password)

    connection.pgconn.change_password.assert_called_once()
    username, supplied_password = connection.pgconn.change_password.call_args.args
    assert username == b"aqa_migrate_login"
    assert supplied_password.decode("utf-8") == password.reveal()
    connection.execute.assert_not_called()


def test_provisioning_creates_exact_roles_and_reconciles_exact_memberships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _local_secret_root(tmp_path)
    passwords = {role: load_platform_role_password(root, role) for role in AUTHORIZATION_ROLES}
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (True, True, True)
    observed_roles: set[str] = set()
    created: list[tuple[str, bool]] = []
    changed: list[str] = []
    membership_reads = iter((set(), role_bootstrap._expected_memberships()))

    def attributes(_connection: object, role: str) -> object:
        return None if role not in observed_roles else (False,)

    def create(
        _connection: object,
        role: str,
        *,
        login: bool,
    ) -> None:
        observed_roles.add(role)
        created.append((role, login))

    monkeypatch.setattr(role_bootstrap, "_role_attributes", attributes)
    monkeypatch.setattr(role_bootstrap, "_create_role", create)
    monkeypatch.setattr(role_bootstrap, "_require_safe_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        role_bootstrap,
        "_managed_memberships",
        lambda _connection: next(membership_reads),
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_grant_missing_memberships",
        lambda _connection, existing, *, expected: (
            assert_empty_memberships(existing),
            assert_fixed_memberships(expected),
        ),
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_grant_initial_migration_database_authority",
        lambda _connection: None,
    )
    monkeypatch.setattr(role_bootstrap, "_harden_public_schema", lambda _connection: None)
    monkeypatch.setattr(
        role_bootstrap,
        "_change_password",
        lambda _connection, login_role, _password: changed.append(login_role),
    )

    result = role_bootstrap._provision_roles(connection, passwords)

    assert created == [
        *((role, False) for role in AUTHORIZATION_ROLES),
        *((role, True) for role in LOGIN_ROLES),
    ]
    assert changed == list(LOGIN_ROLES)
    assert result.created_authorization_roles == AUTHORIZATION_ROLES
    assert result.created_login_roles == LOGIN_ROLES
    assert result.reconciled_login_roles == LOGIN_ROLES


def assert_empty_memberships(existing: object) -> None:
    """Assert the orchestrator observed no pre-bootstrap managed membership."""

    assert existing == set()


def assert_fixed_memberships(existing: object) -> None:
    """Assert only the fixed login memberships are expected."""

    assert existing == role_bootstrap._expected_memberships()


def test_provisioning_rejects_incomplete_password_inventory_before_role_creation() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (True, True, True)

    with pytest.raises(PlatformRoleBootstrapError, match="inventory is incomplete"):
        role_bootstrap._provision_roles(connection, {})

    assert connection.execute.call_count == 1


def test_provisioning_requires_superuser_authority() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (False, True, True)

    with pytest.raises(PlatformRoleBootstrapError, match="superuser authority"):
        role_bootstrap._provision_roles(connection, {})

    assert connection.execute.call_count == 1


def test_non_superuser_legacy_owner_gets_only_bounded_transition_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _local_secret_root(tmp_path)
    passwords = {role: load_platform_role_password(root, role) for role in AUTHORIZATION_ROLES}
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (True, True, True)
    fixed = role_bootstrap._expected_memberships()
    transition_expected = role_bootstrap._expected_memberships("legacy_owner")
    membership_reads = iter((fixed, transition_expected))
    granted: list[str] = []
    revoked_database_authority: list[str] = []
    granted_database_authority: list[str] = []

    monkeypatch.setattr(role_bootstrap, "_role_attributes", lambda *_args: (False,))
    monkeypatch.setattr(role_bootstrap, "_require_safe_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(role_bootstrap, "_role_is_superuser", lambda *_args: False)
    monkeypatch.setattr(
        role_bootstrap,
        "_managed_memberships",
        lambda _connection: next(membership_reads),
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_grant_missing_memberships",
        lambda _connection, existing, *, expected: assert_existing_and_expected(
            existing, fixed, expected, transition_expected
        ),
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_grant_legacy_transition_membership",
        lambda _connection, owner: granted.append(owner),
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_revoke_legacy_transition_database_authority",
        lambda _connection, owner: revoked_database_authority.append(owner),
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_grant_initial_migration_database_authority",
        lambda _connection: None,
    )
    monkeypatch.setattr(
        role_bootstrap,
        "_grant_legacy_transition_database_authority",
        lambda _connection, owner: granted_database_authority.append(owner),
    )
    monkeypatch.setattr(role_bootstrap, "_harden_public_schema", lambda _connection: None)
    monkeypatch.setattr(role_bootstrap, "_change_password", lambda *_args: None)

    role_bootstrap._provision_roles(connection, passwords, transition_owner="legacy_owner")

    assert granted == ["legacy_owner"]
    assert revoked_database_authority == ["legacy_owner"]
    assert granted_database_authority == ["legacy_owner"]


def assert_existing_and_expected(
    actual_existing: object,
    expected_existing: object,
    actual_expected: object,
    expected_memberships: object,
) -> None:
    """Assert a transition reconciliation boundary without mutating the mock."""

    assert actual_existing == expected_existing
    assert actual_expected == expected_memberships
