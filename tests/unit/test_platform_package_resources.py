"""Installed-resource and stable API compatibility contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adaptive_trader.platform.storage.migration_runner as migration_runner
from adaptive_trader.platform.api import create_control_app
from adaptive_trader.platform.api.app import create_control_app as public_app_factory
from adaptive_trader.platform.api.auth import OperatorTokenAuthenticator
from adaptive_trader.platform.api.schemas import StrictApiModel
from adaptive_trader.platform.cli import app
from adaptive_trader.platform.config import ExperimentDefinition, load_platform_config
from adaptive_trader.platform.control.api import create_control_app as control_app_factory
from adaptive_trader.platform.control.auth import OperatorTokenAuthenticator as ControlAuthenticator
from adaptive_trader.platform.control.models import StrictApiModel as ControlApiModel
from adaptive_trader.platform.package_resources import (
    packaged_alembic_ini,
    packaged_config_root,
    packaged_migration_root,
)
from adaptive_trader.platform.storage.migration_runner import platform_migration_head


def test_shipped_configuration_loads_through_resource_boundary() -> None:
    config_root = packaged_config_root()
    config = load_platform_config(Path("platform/offline.yaml"), config_root=config_root)

    assert config_root.is_absolute()
    assert config.profile.profile_id == "offline"
    assert config.profile.execution.submission_enabled is False
    assert (
        config.experiment.definition_hash
        == (
            "c4e66f5a4886215306f3d25c98676ecf48479fac41db9b67848e445c1a46e431"  # pragma: allowlist secret
        )
    )


def test_doctor_default_is_independent_of_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert '"status":"ok"' in result.stdout


def test_shipped_migration_resources_expose_the_single_head() -> None:
    assert packaged_alembic_ini().is_file()
    assert (packaged_migration_root() / "env.py").is_file()
    assert platform_migration_head() == "20260913_0017"


def test_public_api_package_preserves_control_plane_contracts() -> None:
    assert create_control_app is control_app_factory
    assert public_app_factory is control_app_factory
    assert OperatorTokenAuthenticator is ControlAuthenticator
    assert StrictApiModel is ControlApiModel


def test_migration_cli_uses_explicit_validated_runtime_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_root = tmp_path / "application"
    application_root.mkdir()
    shutil.copytree(packaged_config_root(), application_root / "configs")
    database_url_file = application_root / "database-url"
    database_url_file.write_text(
        "postgresql+psycopg://migration:fixture@127.0.0.1:5432/fixture\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    database_url_file.chmod(0o600)
    observed: dict[str, object] = {}

    def migrate(database_url: object, **options: object) -> None:
        observed["database_url"] = database_url
        observed.update(options)

    monkeypatch.setattr(migration_runner, "migrate_platform_database", migrate)
    monkeypatch.setenv("AQA_CONFIG", "configs/platform/paper.yaml")

    result = CliRunner().invoke(
        app,
        [
            "db",
            "migrate",
            "--database-url-file",
            str(database_url_file),
            "--application-root",
            str(application_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "db migrate: ok; revision=head\n"
    assert observed["application_root"] == application_root
    experiment = observed["experiment"]
    assert type(experiment) is ExperimentDefinition
    assert experiment.experiment_id == "semiconductor_network_intraday_v1"
