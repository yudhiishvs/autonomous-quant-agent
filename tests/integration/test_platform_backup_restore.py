"""Guarded end-to-end PostgreSQL logical backup and restore proof."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

_DATABASE_URL = os.environ.get("APA_TEST_POSTGRES_URL", "").strip()
if not _DATABASE_URL:
    pytest.skip(
        "APA_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
        allow_module_level=True,
    )
if os.environ.get("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE") != "YES":
    raise RuntimeError(
        "PostgreSQL integration tests require APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES"
    )
_MISSING_UTILITIES = tuple(
    name for name in ("createdb", "dropdb", "pg_dump", "psql") if shutil.which(name) is None
)
if _MISSING_UTILITIES:
    pytest.skip(
        "PostgreSQL backup utilities are unavailable: " + ", ".join(_MISSING_UTILITIES),
        allow_module_level=True,
    )


def test_deterministic_platform_state_survives_logical_backup_and_restore() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, str(repository_root / "scripts" / "postgres_backup_restore_smoke.py")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "ok"
    assert report["source_and_restore_identical"] is True
    assert report["privileges_preserved"] is True
    assert report["credential_values_in_fixture"] is False
    assert report["backup_format"] == "postgresql_plain_logical"
    assert len(report["content_hash"]) == 64
    assert all(count >= 1 for count in report["required_relations"].values())
    assert completed.stderr == ""
