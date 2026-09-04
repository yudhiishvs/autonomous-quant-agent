"""Observer local-environment checks must fail closed without exposing credentials."""

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
        ".local/operator-settings.json",
        ".unreviewed-tool/session.json",
        ".agents/session/transcript.txt",
        ".agents/skills/example/__pycache__/module.pyc",
        ".agents/skills/example/local.log",
        ".agents/skills/example/.agents/session.json",
        ".agents/skills/example/.git/config",
        ".agents/skills/example/.vscode/settings.json",
        "runtime/observer.db",
        "runtime/observer.log",
        "local-observer.db",
        "local-observer.sqlite",
        "local-observer.sqlite3",
        "local-observer.bak",
        "local-observer.backup",
        "credentials.json",
        "local_credentials.json",
        "generated_secrets.json",
        "credentials/paper.key.txt",
        ".credentials/paper.secret",
        "local_credentials/paper.txt",
        "secrets/database_url",
        "certificates/client.p12",
        "certificates/client.pfx",
        "data/cache/market_data.csv",
        "data/raw/provider-bars.jsonl",
        "data/provider-export/bars.csv",
        "data/processed/private.parquet",
        "data/fixtures/.env",
        "data/fixtures/private.key",
        "data/fixtures/secrets/api-token",
        "data/fixtures/local.db",
        "data/fixtures/session.log",
        "data/fixtures/__pycache__/fixture.pyc",
        "data/fixtures/.agents/session.json",
        "data/fixtures/.vscode/settings.json",
        "data/fixtures/.git/config",
        "tmp/observer.tmp",
        "tmp/notes.txt",
        "scratch/local-analysis.md",
        "transcripts/local-session.txt",
        "tmp/arbitrary.partial",
        "merge-conflict.orig",
        "Thumbs.db",
        "Desktop.ini",
        "observer-session.partial",
        ".idea/workspace.xml",
        ".vscode/local-overrides.json",
        "__pycache__/module.pyc",
    )
    for candidate in candidates:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", candidate],
            cwd=project_root,
            check=False,
        )
        assert result.returncode == 0, f"sensitive local artifact is not ignored: {candidate}"

    for candidate in (
        ".env.example",
        ".vscode/extensions.json",
        ".vscode/settings.json",
        ".agents/skills/example/SKILL.md",
        "data/fixtures/synthetic-bars.jsonl",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", candidate],
            cwd=project_root,
            check=False,
        )
        assert result.returncode == 1, f"tracked project artifact is ignored: {candidate}"


def test_docker_context_excludes_credentials_and_mutable_state(project_root: Path) -> None:
    ordered_rules = [
        line.strip()
        for line in (project_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    required_recursive_exclusions = {
        "**/.agents",
        "**/.env*",
        "**/.git",
        "**/.git/**",
        "**/.idea",
        "**/.local",
        "**/.venv",
        "**/.vscode",
        "**/.*",
        "**/__pycache__",
        "**/*.backup",
        "**/*.db",
        "**/*.egg-info",
        "**/*.key",
        "**/*.log",
        "**/*.orig",
        "**/*.p12",
        "**/*.pfx",
        "**/*.pem",
        "**/*.pid",
        "**/*.py[cod]",
        "**/*.secret",
        "**/*.sqlite",
        "**/*.sqlite3",
        "**/*.swp",
        "**/*.tmp",
        "**/*.token",
        "**/*~",
        "**/credentials",
        "**/local_credentials",
        "**/outputs",
        "**/runtime",
        "**/scratch",
        "**/secrets",
        "**/tmp",
        "**/transcripts",
    }
    expected_inclusions = [
        "!.dockerignore",
        "!Dockerfile",
        "!README.md",
        "!alembic.ini",
        "!app.py",
        "!pyproject.toml",
        "!uv.lock",
        "!configs/",
        "!configs/**",
        "!data/",
        "!data/fixtures/",
        "!data/fixtures/**",
        "!docs/",
        "!docs/**",
        "!migrations/",
        "!migrations/**",
        "!scripts/",
        "!scripts/**",
        "!src/",
        "!src/**",
        "!/.env.example",
    ]
    assert ordered_rules[0] == "**"
    assert [rule for rule in ordered_rules if rule.startswith("!")] == expected_inclusions
    assert required_recursive_exclusions <= set(ordered_rules)
    content_inclusions = expected_inclusions[:-1]
    assert max(ordered_rules.index(rule) for rule in content_inclusions) < min(
        ordered_rules.index(rule) for rule in required_recursive_exclusions
    )
    assert ordered_rules[-1] == "!/.env.example"


def test_quality_generator_uses_active_virtual_environment_tools(project_root: Path) -> None:
    script = (project_root / "scripts" / "generate_phase2_quality_evidence.py").read_text(
        encoding="utf-8"
    )

    assert "Path(sys.executable).resolve()" not in script
    assert "python_bin = Path(sys.executable)" in script
    for tool in ("ruff", "mypy", "pytest"):
        assert (Path(sys.executable).parent / tool).is_file()
