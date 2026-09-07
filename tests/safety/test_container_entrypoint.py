"""Runtime tests for one-way container credential materialization."""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote, urlsplit

import pytest

from adaptive_trader.platform.control import derive_dashboard_read_token
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file

_OPERATOR_TOKEN = "TEST_CONTAINER_OPERATOR_TOKEN_0123456789_ABCDE"


def _entrypoint(project_root: Path) -> ModuleType:
    path = project_root / "docker" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("_aqa_container_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_file(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def _configure_paths(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operator_path: Path,
    output_directory: Path,
) -> None:
    monkeypatch.setattr(module, "_OPERATOR_TOKEN_PATH", operator_path)
    monkeypatch.setattr(module, "_DASHBOARD_TOKEN_DIRECTORY", output_directory)
    monkeypatch.setenv("AQA_OPERATOR_TOKEN_FILE", operator_path.as_posix())


def test_database_url_is_materialized_once_with_owner_private_permissions(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _entrypoint(project_root)
    secret_root = tmp_path / "secrets"
    output = tmp_path / "runtime" / "database-url"
    secret_root.mkdir(mode=0o700)
    _private_file(secret_root / "aqa_collector_password", "safe local:@/")
    monkeypatch.setattr(module, "_SECRET_ROOT", secret_root)
    monkeypatch.setattr(module, "_DATABASE_URL_PATH", output)
    monkeypatch.setenv("AQA_DATABASE_URL_FILE", "")

    module._write_database_url("aqa_collector")  # type: ignore[attr-defined]

    status = output.stat(follow_symlinks=False)
    parsed = urlsplit(output.read_text(encoding="utf-8"))
    assert parsed.scheme == "postgresql+psycopg"
    assert parsed.username == "aqa_collector_login"
    assert unquote(parsed.password or "") == "safe local:@/"
    assert parsed.hostname == "postgres"
    assert parsed.port == 5432
    assert parsed.path == "/aqa"
    assert os.environ["AQA_DATABASE_URL_FILE"] == output.as_posix()
    assert status.st_uid == os.geteuid()
    assert stat.S_IMODE(status.st_mode) == 0o600

    with pytest.raises(FileExistsError):
        module._write_database_url("aqa_collector")  # type: ignore[attr-defined]


def test_dashboard_token_is_derived_into_owner_private_file(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _entrypoint(project_root)
    operator_path = tmp_path / "operator-token"
    output_directory = tmp_path / "dashboard-auth"
    output_directory.mkdir(mode=0o700)
    _private_file(operator_path, _OPERATOR_TOKEN)
    _configure_paths(
        module,
        monkeypatch,
        operator_path=operator_path,
        output_directory=output_directory,
    )

    module._materialize_dashboard_read_token()  # type: ignore[attr-defined]
    module._materialize_dashboard_read_token()  # type: ignore[attr-defined]

    output = output_directory / "read-token"
    status = output.stat(follow_symlinks=False)
    operator_secret = load_secret_file(
        operator_path,
        source=SecretFileVariable.OPERATOR_TOKEN,
    )
    assert output.read_text(encoding="ascii") == derive_dashboard_read_token(operator_secret)
    assert output.read_text(encoding="ascii") != _OPERATOR_TOKEN
    assert status.st_uid == os.geteuid()
    assert stat.S_IMODE(status.st_mode) == 0o600
    assert not (output_directory / ".read-token.pending").exists()


def test_dashboard_token_materialization_rejects_permissive_directory(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _entrypoint(project_root)
    operator_path = tmp_path / "operator-token"
    output_directory = tmp_path / "dashboard-auth"
    output_directory.mkdir(mode=0o755)
    _private_file(operator_path, _OPERATOR_TOKEN)
    _configure_paths(
        module,
        monkeypatch,
        operator_path=operator_path,
        output_directory=output_directory,
    )

    with pytest.raises(SystemExit, match="credential directory is not private") as failure:
        module._materialize_dashboard_read_token()  # type: ignore[attr-defined]

    assert _OPERATOR_TOKEN not in str(failure.value)
    assert not (output_directory / "read-token").exists()


def test_dashboard_token_materialization_rejects_existing_symlink(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _entrypoint(project_root)
    operator_path = tmp_path / "operator-token"
    output_directory = tmp_path / "dashboard-auth"
    output_directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    _private_file(operator_path, _OPERATOR_TOKEN)
    _private_file(outside, "must-remain-unchanged")
    (output_directory / "read-token").symlink_to(outside)
    _configure_paths(
        module,
        monkeypatch,
        operator_path=operator_path,
        output_directory=output_directory,
    )

    with pytest.raises(SystemExit, match="existing dashboard credential is not private"):
        module._materialize_dashboard_read_token()  # type: ignore[attr-defined]

    assert outside.read_text(encoding="utf-8") == "must-remain-unchanged"
