"""Static contracts for the validation-only GitHub Actions workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def _offline_quality_job(project_root: Path) -> dict[str, Any]:
    workflow = yaml.safe_load(
        (project_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    assert isinstance(workflow, dict)
    job = workflow["jobs"]["offline-quality"]
    assert isinstance(job, dict)
    return job


def test_ci_uses_immutable_postgresql_16_and_checks_runtime_major(
    project_root: Path,
) -> None:
    job = _offline_quality_job(project_root)
    image = job["services"]["postgres"]["image"]

    assert isinstance(image, str)
    assert re.fullmatch(r"postgres:16@sha256:[0-9a-f]{64}", image)

    steps = job["steps"]
    names = [step["name"] for step in steps]
    version_index = names.index("Verify PostgreSQL server major version")
    migration_index = names.index("Validate PostgreSQL role bootstrap and migrations")
    assert version_index < migration_index

    version_command = steps[version_index]["run"]
    assert "${{ job.services.postgres.id }}" in version_command
    assert "server_version_num" in version_command
    assert 'test "${actual_major}" = "16"' in version_command

    assert job["env"]["APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE"] == "YES"
    assert job["env"]["APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES"] == "YES"
    migration_command = steps[migration_index]["run"]
    assert "tests/integration/test_platform_postgres_roles.py" in migration_command
    assert "alembic upgrade" not in migration_command


def test_ci_role_lifecycle_uses_generated_ephemeral_test_credentials(
    project_root: Path,
) -> None:
    job = _offline_quality_job(project_root)
    rendered = yaml.safe_dump(job, sort_keys=True)

    assert "bootstrap_postgres_roles.py" not in rendered
    assert "aqa_migrate_password" not in rendered
    assert "APA_ALPACA_DATA_API_KEY: ''" in rendered
    assert "APA_ALPACA_DATA_SECRET_KEY: ''" in rendered
