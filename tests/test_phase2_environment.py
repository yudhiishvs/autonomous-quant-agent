"""Phase 2 local-environment checks must fail closed without exposing credentials."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from adaptive_trader.config import load_config
from adaptive_trader.constants import PAPER_ORDER_ACKNOWLEDGEMENT


def _run_check(
    project_root: Path, config_path: Path, **updates: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in (
        "APA_ALPACA_PAPER_API_KEY",
        "APA_ALPACA_PAPER_SECRET_KEY",
        "APA_ENABLE_PAPER_ORDERS",
    ):
        environment.pop(name, None)
    environment.update(updates)
    environment["APA_PYTHON_BIN"] = sys.executable
    return subprocess.run(
        ["bash", "scripts/check_local_paper_environment.sh", str(config_path)],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_paper_environment_reports_presence_without_values(project_root: Path) -> None:
    api_marker = "phase2-api-marker-never-print"
    secret_marker = "phase2-secret-marker-never-print"

    result = _run_check(
        project_root,
        project_root / "configs" / "paper.yaml",
        APA_ALPACA_PAPER_API_KEY=api_marker,
        APA_ALPACA_PAPER_SECRET_KEY=secret_marker,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "APA_ALPACA_PAPER_API_KEY: PRESENT" in result.stdout
    assert "APA_ALPACA_PAPER_SECRET_KEY: PRESENT" in result.stdout
    assert "Paper-order submission: DISABLED" in result.stdout
    assert "Paper acknowledgement token: INACTIVE" in result.stdout
    assert api_marker not in result.stdout + result.stderr
    assert secret_marker not in result.stdout + result.stderr


def test_check_paper_environment_rejects_active_acknowledgement(project_root: Path) -> None:
    result = _run_check(
        project_root,
        project_root / "configs" / "paper.yaml",
        APA_ENABLE_PAPER_ORDERS=PAPER_ORDER_ACKNOWLEDGEMENT,
    )

    assert result.returncode != 0
    assert "Paper acknowledgement token: UNSAFE" in result.stderr
    assert PAPER_ORDER_ACKNOWLEDGEMENT not in result.stdout + result.stderr


def test_check_paper_environment_rejects_enabled_configuration(
    project_root: Path,
    tmp_path: Path,
) -> None:
    values = load_config(project_root / "configs" / "paper.yaml").to_canonical_dict()
    values["execution"]["paper_order_submission_enabled"] = True
    config_path = tmp_path / "unsafe-paper.yaml"
    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")

    result = _run_check(project_root, config_path)

    assert result.returncode != 0
    assert "Paper-order submission: UNSAFE" in result.stderr


def test_sensitive_local_artifact_patterns_are_gitignored(project_root: Path) -> None:
    candidates = (
        ".env",
        "runtime/observer.db",
        "runtime/observer.log",
        "credentials.json",
        "local_credentials.json",
        "generated_secrets.json",
        "credentials/paper.key.txt",
        ".credentials/paper.secret",
        "local_credentials/paper.txt",
        "tmp/observer.tmp",
        "observer-session.partial",
        "__pycache__/module.pyc",
    )
    for candidate in candidates:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", candidate],
            cwd=project_root,
            check=False,
        )
        assert result.returncode == 0, f"sensitive local artifact is not ignored: {candidate}"


def test_quality_generator_uses_active_virtual_environment_tools(project_root: Path) -> None:
    script = (project_root / "scripts" / "generate_phase2_quality_evidence.py").read_text(
        encoding="utf-8"
    )

    assert "Path(sys.executable).resolve()" not in script
    assert "python_bin = Path(sys.executable)" in script
    for tool in ("ruff", "mypy", "pytest"):
        assert (Path(sys.executable).parent / tool).is_file()
