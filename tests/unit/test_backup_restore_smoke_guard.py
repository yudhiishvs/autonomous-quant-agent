"""Static and preflight safety checks for the backup/restore smoke script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "postgres_backup_restore_smoke.py"


def test_backup_restore_smoke_requires_explicit_destructive_test_database_guard() -> None:
    environment = dict(os.environ)
    environment.pop("APA_TEST_POSTGRES_URL", None)
    environment.pop("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE", None)

    completed = subprocess.run(
        [sys.executable, str(_script())],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "error": "APA_TEST_POSTGRES_URL is required",
        "status": "failed",
    }
    assert completed.stderr == ""


def test_backup_restore_smoke_rejects_every_database_except_collector_test() -> None:
    environment = {
        **os.environ,
        "APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE": "YES",
        "APA_TEST_POSTGRES_URL": "postgresql://fixture:do-not-print@127.0.0.1/not-disposable",  # pragma: allowlist secret
    }

    completed = subprocess.run(
        [sys.executable, str(_script())],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report == {
        "error": "backup/restore smoke requires the collector_test database",
        "status": "failed",
    }
    assert "do-not-print" not in completed.stdout + completed.stderr


def test_backup_restore_uses_argument_vectors_and_plain_logical_dump() -> None:
    source = _script().read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert '"--format=plain"' in source
    assert '"--no-owner"' in source
    assert '"--no-privileges"' not in source
    assert '"--no-psqlrc"' in source
    assert "privileges_preserved" in source
    assert '"ON_ERROR_STOP=1"' in source
    assert "PGPASSWORD" in source
    assert "source_and_restore_identical" in source


def test_backup_restore_rejects_inherited_libpq_routing_before_connecting() -> None:
    for variable in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        environment = {
            **os.environ,
            "APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE": "YES",
            "APA_TEST_POSTGRES_URL": "postgresql://fixture:fixture@127.0.0.1/collector_test",  # pragma: allowlist secret -- synthetic loopback rejection fixture
            variable: "untrusted-routing",
        }
        result = subprocess.run(
            [sys.executable, str(_script())],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 1
        assert (
            json.loads(result.stdout)["error"]
            == "backup/restore smoke rejects inherited PostgreSQL routing settings"
        )
        assert result.stderr == ""


def test_backup_subprocess_environment_removes_libpq_inheritance(monkeypatch) -> None:
    from sqlalchemy.engine import make_url

    from scripts import postgres_backup_restore_smoke as smoke

    monkeypatch.setenv("PGHOSTADDR", "untrusted")
    monkeypatch.setenv("PGSERVICE", "untrusted")
    monkeypatch.setenv("PGPASSFILE", "/untrusted")
    monkeypatch.setenv("PGPASSWORD", "untrusted")
    environment = smoke._pg_environment(
        make_url(
            "postgresql://fixture:fixture@127.0.0.1/collector_test"  # pragma: allowlist secret
        )
    )
    assert {key for key in environment if key.startswith("PG")} == {
        "PGPASSWORD",
        "PGCONNECT_TIMEOUT",
    }
    # Synthetic password assertion, not an operational credential.
    assert environment["PGPASSWORD"] == "fixture"  # pragma: allowlist secret


def test_backup_portable_dates_preserve_instant_and_reject_naive_values() -> None:
    from datetime import UTC, date, datetime, timedelta, timezone

    import pytest

    from scripts import postgres_backup_restore_smoke as smoke

    assert smoke._portable_value(date(2026, 7, 6)) == "2026-07-06"
    assert smoke._portable_value(
        datetime(2026, 7, 6, 10, tzinfo=timezone(timedelta(hours=-4)))
    ) == datetime(2026, 7, 6, 14, tzinfo=UTC)
    with pytest.raises(smoke.BackupRestoreError, match="naive"):
        smoke._portable_value(datetime(2026, 7, 6, 10))


def test_backup_acl_comparison_preserves_effective_grant_option_across_grantors() -> None:
    from scripts.postgres_backup_restore_smoke import _effective_privileges

    plain = ("database", "current", "aqa_migrate", "CREATE", False)
    grantable = (*plain[:-1], True)
    assert _effective_privileges([plain, grantable, plain]) == (grantable,)
    assert _effective_privileges([plain, plain]) == (plain,)
    assert _effective_privileges([plain]) != _effective_privileges([grantable])
    assert _effective_privileges([plain]) != _effective_privileges([])
