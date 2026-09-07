"""Static contracts for the offline quality and PostgreSQL workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def _workflow(project_root: Path) -> dict[str, Any]:
    value = yaml.safe_load(
        (project_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def _job(project_root: Path, name: str) -> dict[str, Any]:
    value = _workflow(project_root)["jobs"][name]
    assert isinstance(value, dict)
    return value


def test_ci_uses_locked_complete_offline_quality_gates(project_root: Path) -> None:
    job = _job(project_root, "quality")
    commands = "\n".join(str(step.get("run", "")) for step in job["steps"])

    assert "uv sync --locked --all-extras" in commands
    assert "ruff format --check ." in commands
    assert "ruff check ." in commands
    assert "mypy src docker" in commands
    assert "make coverage-offline" in commands
    makefile = (project_root / "Makefile").read_text()
    assert (
        "scripts/verify_no_network.py pytest -q -m 'not postgres' --cov=adaptive_trader --cov-branch"
        in makefile
    )
    assert "backtest" in commands and "--synthetic" in commands
    assert "replay" in commands
    assert "docker compose" in commands and "config --quiet" in commands
    assert "git diff --check" in commands
    assert "git diff --exit-code" in commands
    assert "git status --porcelain --untracked-files=all" in commands

    checkout = next(step for step in job["steps"] if step["name"] == "Check out repository")
    assert checkout["with"]["persist-credentials"] is False


def test_postgres_job_uses_immutable_postgresql_16_and_runs_full_marker(
    project_root: Path,
) -> None:
    job = _job(project_root, "postgres-integration")
    image = job["services"]["postgres"]["image"]
    assert isinstance(image, str)
    assert re.fullmatch(r"postgres:16@sha256:[0-9a-f]{64}", image)

    steps = job["steps"]
    names = [step["name"] for step in steps]
    version_index = names.index("Verify PostgreSQL server major version")
    integration_index = names.index("Validate migrations roles repositories and concurrency")
    assert version_index < integration_index
    assert "server_version_num" in steps[version_index]["run"]
    assert 'test "${actual_major}" = "16"' in steps[version_index]["run"]
    assert steps[integration_index]["run"] == "make coverage-postgres"
    utilities = next(
        step for step in steps if step["name"] == "Require logical backup and restore utilities"
    )
    assert all(
        f"command -v {name}" in utilities["run"]
        for name in ("createdb", "dropdb", "pg_dump", "psql")
    )

    assert job["env"]["APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE"] == "YES"
    assert job["env"]["APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES"] == "YES"


def test_ci_has_no_alpaca_authority_and_submission_is_disabled(project_root: Path) -> None:
    workflow = _workflow(project_root)
    environment = workflow["env"]
    secret_names = {
        "APCA_API_BASE_URL",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APA_ALPACA_PAPER_API_KEY",
        "APA_ALPACA_PAPER_SECRET_KEY",
        "APA_ALPACA_DATA_API_KEY",
        "APA_ALPACA_DATA_SECRET_KEY",
        "APA_MARKET_DATA_DATABASE_URL",
        "APA_MARKET_DATA_MIGRATION_DATABASE_URL",
        "AQA_ALPACA_DATA_API_KEY_FILE",
        "AQA_ALPACA_DATA_SECRET_KEY_FILE",
        "AQA_ALPACA_PAPER_API_KEY_FILE",
        "AQA_ALPACA_PAPER_SECRET_KEY_FILE",
        "AQA_PAPER_ACCOUNT_ID_HASH_FILE",
        "AQA_OPERATOR_TOKEN_FILE",
        "AQA_DATABASE_URL_FILE",
    }
    assert {name: environment[name] for name in secret_names} == {name: "" for name in secret_names}
    assert environment["APA_ENABLE_PAPER_ORDERS"] == "NO"
    assert environment["AQA_ENABLE_PAPER_ORDERS"] == "NO"
    assert "bootstrap_postgres_roles.py" not in yaml.safe_dump(
        workflow["jobs"]["postgres-integration"], sort_keys=True
    )


def test_coverage_gate_requires_both_real_data_artifacts_and_preserves_floors(
    project_root: Path,
) -> None:
    job = _job(project_root, "coverage-gate")
    assert set(job["needs"]) == {"quality", "postgres-integration"}
    commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "test -s coverage-input/offline/.coverage" in commands
    assert "test -s coverage-input/postgres/.coverage" in commands
    assert (
        "coverage combine coverage-input/offline/.coverage coverage-input/postgres/.coverage"
        in commands
    )
    assert "make coverage-report" in commands
    for name in ("quality", "postgres-integration"):
        uploads = [
            step
            for step in _job(project_root, name)["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        ]
        assert len(uploads) == 1
        assert uploads[0]["with"]["path"] == ".coverage"
        assert uploads[0]["with"]["include-hidden-files"] is True
        assert uploads[0]["with"]["if-no-files-found"] == "error"
    makefile = (project_root / "Makefile").read_text()
    assert "coverage: postgres-preflight" in makefile
    assert "coverage-postgres: postgres-preflight" in makefile
    assert "--cov-append" in makefile
    assert "coverage report --fail-under=74" in makefile
    assert "--include='src/adaptive_trader/platform/*' --fail-under=85" in makefile
    check = next(line for line in makefile.splitlines() if line.startswith("check:"))
    assert "coverage" in check.split() and "integration" not in check.split()


def test_offline_make_recipes_remove_inherited_postgres_authority(project_root: Path) -> None:
    makefile = (project_root / "Makefile").read_text()
    for target in ("test", "unit", "coverage-offline"):
        recipe = makefile.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        for variable in (
            "APA_TEST_POSTGRES_URL",
            "APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE",
            "APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES",
            "APA_TEST_POSTGRES_CONTAINER",
        ):
            assert f"-u {variable}" in recipe
        assert "scripts/verify_no_network.py pytest" in recipe
    postgres = makefile.split("coverage-postgres: postgres-preflight\n", 1)[1].split("\n\n", 1)[0]
    assert "env -u" not in postgres
