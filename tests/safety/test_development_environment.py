"""Keep editor conveniences aligned with the command-line toolchain."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_vscode_recommendations_are_small_and_stack_specific(project_root: Path) -> None:
    configuration = _load_json(project_root / ".vscode" / "extensions.json")
    recommendations = configuration["recommendations"]

    assert set(configuration) == {"recommendations"}
    assert isinstance(recommendations, list)
    assert len(recommendations) == len(set(recommendations))
    assert set(recommendations) == {
        "EditorConfig.EditorConfig",
        "charliermarsh.ruff",
        "github.vscode-github-actions",
        "ms-azuretools.vscode-containers",
        "ms-python.python",
        "ms-python.vscode-pylance",
    }


def test_vscode_settings_use_repository_tools(project_root: Path) -> None:
    settings = _load_json(project_root / ".vscode" / "settings.json")
    python_settings = settings["[python]"]

    assert set(settings) == {
        "[python]",
        "python.defaultInterpreterPath",
        "python.testing.pytestArgs",
        "python.testing.pytestEnabled",
        "python.testing.unittestEnabled",
    }
    assert set(python_settings) == {
        "editor.codeActionsOnSave",
        "editor.defaultFormatter",
        "editor.formatOnSave",
    }
    assert settings["python.defaultInterpreterPath"] == "${workspaceFolder}/.venv/bin/python"
    assert settings["python.testing.pytestArgs"] == ["tests"]
    assert settings["python.testing.pytestEnabled"] is True
    assert settings["python.testing.unittestEnabled"] is False
    assert python_settings["editor.defaultFormatter"] == "charliermarsh.ruff"
    assert python_settings["editor.formatOnSave"] is True
    assert python_settings["editor.codeActionsOnSave"] == {
        "source.fixAll.ruff": "explicit",
        "source.organizeImports.ruff": "explicit",
    }


def test_editorconfig_matches_repository_formatting_basics(project_root: Path) -> None:
    contents = (project_root / ".editorconfig").read_text(encoding="utf-8")

    assert (
        contents
        == """root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
indent_style = space
indent_size = 4
trim_trailing_whitespace = true

[*.py]
max_line_length = 100

[{*.json,*.yaml,*.yml}]
indent_size = 2

[Makefile]
indent_style = tab

[*.md]
trim_trailing_whitespace = false
"""
    )


def test_python_tools_target_the_supported_language_floor(project_root: Path) -> None:
    configuration = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert (project_root / ".python-version").read_text(encoding="utf-8") == "3.11\n"
    assert configuration["project"]["requires-python"] == ">=3.11"
    assert configuration["tool"]["mypy"]["python_version"] == "3.11"
    assert configuration["tool"]["ruff"]["target-version"] == "py311"
