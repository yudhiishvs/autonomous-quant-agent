"""Offline orchestration tests for the bounded platform migration runner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, call

import pytest

import adaptive_trader.platform.storage.migration_runner as migration_runner
from adaptive_trader.platform.security import RedactedSecret, SecretFileVariable, load_secret_file
from adaptive_trader.platform.storage.migration_roles import MIGRATION_ROLE_REVISION
from adaptive_trader.platform.storage.role_bootstrap import PlatformRoleBootstrapError

_BASE_URL = "postgresql+psycopg://legacy:secret@127.0.0.1:5432/collector_test"
_MIGRATION_URL = "postgresql+psycopg://aqa_migrate_login:secret@127.0.0.1:5432/collector_test"
_DESCENDANT = "test_governed_descendant"


def _database_secret(
    tmp_path: Path,
    *,
    name: str = "database_url",
    value: str = _BASE_URL,
) -> RedactedSecret:
    path = tmp_path / name
    path.write_text(f"{value}\n", encoding="utf-8")
    path.chmod(0o600)
    return load_secret_file(path, source=SecretFileVariable.DATABASE_URL)


def _configure_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock]:
    script_directory = MagicMock()
    script_directory.get_current_head.return_value = _DESCENDANT
    config = MagicMock()
    monkeypatch.setattr(
        migration_runner,
        "_alembic_config",
        lambda database_url: (config, database_url),
    )
    monkeypatch.setattr(
        migration_runner.ScriptDirectory,
        "from_config",
        lambda _config: script_directory,
    )
    monkeypatch.setattr(
        migration_runner,
        "migration_role_revision_sets",
        lambda _script: (
            frozenset(
                {
                    "20260903_0001",
                    "20260905_0002",
                    "20260905_0003",
                    MIGRATION_ROLE_REVISION,
                    _DESCENDANT,
                }
            ),
            frozenset({MIGRATION_ROLE_REVISION, _DESCENDANT}),
        ),
    )
    monkeypatch.setattr(
        migration_runner,
        "migration_login_database_url",
        lambda _secret, *, application_root: _MIGRATION_URL,
    )
    upgrade = MagicMock()
    monkeypatch.setattr(migration_runner.command, "upgrade", upgrade)
    return script_directory, upgrade


def test_fresh_database_uses_only_the_migration_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _database_secret(tmp_path)
    admin_secret = _database_secret(tmp_path, name="admin_url", value=_BASE_URL)
    _script, upgrade = _configure_runner(monkeypatch)
    monkeypatch.setattr(
        migration_runner,
        "_current_revision",
        MagicMock(side_effect=[None, None, _DESCENDANT]),
    )
    require_transition = MagicMock()
    finalize_transition = MagicMock()
    monkeypatch.setattr(migration_runner, "require_legacy_role_transition", require_transition)
    monkeypatch.setattr(migration_runner, "finalize_legacy_role_transition", finalize_transition)

    migration_runner.migrate_platform_database(
        secret,
        application_root=tmp_path,
        bootstrap_admin_database_url=admin_secret,
    )

    upgrade.assert_called_once_with((ANY, _MIGRATION_URL), "head")
    require_transition.assert_not_called()
    finalize_transition.assert_not_called()


def test_legacy_database_uses_owner_only_through_0004_then_reconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _database_secret(tmp_path)
    admin_secret = _database_secret(tmp_path, name="admin_url", value=_BASE_URL)
    _script, upgrade = _configure_runner(monkeypatch)
    monkeypatch.setattr(
        migration_runner,
        "_current_revision",
        MagicMock(side_effect=["20260905_0003", _DESCENDANT]),
    )
    require_transition = MagicMock()
    finalize_transition = MagicMock()
    monkeypatch.setattr(migration_runner, "require_legacy_role_transition", require_transition)
    monkeypatch.setattr(migration_runner, "finalize_legacy_role_transition", finalize_transition)

    migration_runner.migrate_platform_database(
        secret,
        application_root=tmp_path,
        bootstrap_admin_database_url=admin_secret,
    )

    assert upgrade.call_args_list == [
        call((ANY, _BASE_URL), MIGRATION_ROLE_REVISION),
        call((ANY, _MIGRATION_URL), "head"),
    ]
    require_transition.assert_called_once_with(secret)
    finalize_transition.assert_called_once_with(
        admin_secret,
        governed_revisions=frozenset({MIGRATION_ROLE_REVISION, _DESCENDANT}),
    )


def test_legacy_database_requires_admin_cleanup_before_any_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _database_secret(tmp_path)
    _script, upgrade = _configure_runner(monkeypatch)
    monkeypatch.setattr(migration_runner, "_current_revision", lambda _url: "20260905_0003")
    require_transition = MagicMock()
    monkeypatch.setattr(migration_runner, "require_legacy_role_transition", require_transition)

    with pytest.raises(RuntimeError, match="separate bootstrap administrator"):
        migration_runner.migrate_platform_database(secret, application_root=tmp_path)

    require_transition.assert_not_called()
    upgrade.assert_not_called()


def test_governed_database_cleans_interrupted_transition_before_login_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _database_secret(tmp_path)
    _script, upgrade = _configure_runner(monkeypatch)
    monkeypatch.setattr(
        migration_runner,
        "_current_revision",
        MagicMock(side_effect=[MIGRATION_ROLE_REVISION, _DESCENDANT]),
    )
    require_transition = MagicMock()
    finalize_transition = MagicMock()
    monkeypatch.setattr(migration_runner, "require_legacy_role_transition", require_transition)
    monkeypatch.setattr(migration_runner, "finalize_legacy_role_transition", finalize_transition)

    migration_runner.migrate_platform_database(secret, application_root=tmp_path)

    upgrade.assert_called_once_with((ANY, _MIGRATION_URL), "head")
    require_transition.assert_not_called()
    finalize_transition.assert_called_once()
    finalize_transition.assert_called_once_with(
        secret,
        governed_revisions=frozenset({MIGRATION_ROLE_REVISION, _DESCENDANT}),
        application_root=tmp_path,
    )


def test_governed_interrupted_cleanup_uses_explicit_admin_only_after_login_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _database_secret(tmp_path)
    admin_secret = _database_secret(tmp_path, name="admin_url", value=_BASE_URL)
    _script, upgrade = _configure_runner(monkeypatch)
    monkeypatch.setattr(
        migration_runner,
        "_current_revision",
        MagicMock(side_effect=[MIGRATION_ROLE_REVISION, _DESCENDANT]),
    )
    finalize_transition = MagicMock(
        side_effect=[PlatformRoleBootstrapError("migration login denied"), None]
    )
    monkeypatch.setattr(migration_runner, "finalize_legacy_role_transition", finalize_transition)

    migration_runner.migrate_platform_database(
        secret,
        application_root=tmp_path,
        bootstrap_admin_database_url=admin_secret,
    )

    assert finalize_transition.call_args_list == [
        call(
            secret,
            governed_revisions=frozenset({MIGRATION_ROLE_REVISION, _DESCENDANT}),
            application_root=tmp_path,
        ),
        call(
            admin_secret,
            governed_revisions=frozenset({MIGRATION_ROLE_REVISION, _DESCENDANT}),
        ),
    ]
    upgrade.assert_called_once_with((ANY, _MIGRATION_URL), "head")


def test_unknown_revision_fails_before_any_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _database_secret(tmp_path)
    _script, upgrade = _configure_runner(monkeypatch)
    monkeypatch.setattr(migration_runner, "_current_revision", lambda _url: "unknown")

    with pytest.raises(RuntimeError, match="not recognized"):
        migration_runner.migrate_platform_database(secret, application_root=tmp_path)

    upgrade.assert_not_called()
