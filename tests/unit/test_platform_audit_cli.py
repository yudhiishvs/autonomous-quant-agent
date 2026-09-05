"""Offline CLI coverage for independent audit-chain verification."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, update
from typer.testing import CliRunner

from adaptive_trader.platform import cli as platform_cli
from adaptive_trader.platform.config import (
    RuntimeService,
    RuntimeSettings,
    load_runtime_settings,
)
from adaptive_trader.platform.domain import AuditEvent, AuditPayload, AuditWriter
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage import AuditRepository, create_platform_engine
from adaptive_trader.platform.storage.tables import aqa_audit_events, metadata

_OCCURRED_AT = datetime(2026, 9, 5, 18, 30, tzinfo=UTC)


@pytest.fixture
def application_root(tmp_path: Path, project_root: Path) -> Path:
    root = tmp_path / "application"
    root.mkdir()
    shutil.copytree(project_root / "configs", root / "configs")
    return root


def _engine(application_root: Path) -> Engine:
    settings = load_runtime_settings(
        {"AQA_CONFIG": "configs/platform/offline.yaml"},
        service=RuntimeService.AUDIT_VERIFIER,
        application_root=application_root,
    )
    return create_platform_engine(settings, application_name="aqa-audit-cli-test")


def _audit_id(label: str) -> str:
    return f"cli_{sha256_hex(('audit-cli-id', label))}"


@pytest.fixture
def audit_database(application_root: Path) -> Iterator[tuple[tuple[AuditEvent, ...], Engine]]:
    engine = _engine(application_root)
    metadata.create_all(engine)
    repository = AuditRepository(engine, writer=AuditWriter.CONTROL)
    events = (
        repository.append(
            stream_id="aqa_control:job:one",
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {"idempotency_key": _audit_id("job-one-start"), "state": "running"}
            ),
        ),
        repository.append(
            stream_id="aqa_control:job:one",
            event_type="job.completed",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": _audit_id("job-one-complete"),
                    "state": "completed",
                }
            ),
        ),
        repository.append(
            stream_id="aqa_control:job:two",
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=AuditPayload.from_mapping(
                {"idempotency_key": _audit_id("job-two-start"), "state": "running"}
            ),
        ),
    )
    try:
        yield events, engine
    finally:
        engine.dispose()


def test_audit_verify_reports_stable_verified_heads_and_uses_database_only_scope(
    application_root: Path,
    audit_database: tuple[tuple[AuditEvent, ...], Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, _ = audit_database
    captured: dict[str, object] = {}
    original_loader = platform_cli.load_runtime_settings

    def capture_settings(
        environment: Mapping[str, str],
        *,
        service: RuntimeService,
        application_root: Path,
    ) -> RuntimeSettings:
        captured.update(
            {
                "application_root": application_root,
                "environment": dict(environment),
                "service": service,
            }
        )
        return original_loader(
            environment,
            service=service,
            application_root=application_root,
        )

    monkeypatch.setattr(platform_cli, "load_runtime_settings", capture_settings)
    for name in (
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "AQA_ALPACA_DATA_API_KEY_FILE",
        "AQA_ALPACA_DATA_SECRET_KEY_FILE",
        "AQA_ALPACA_PAPER_API_KEY_FILE",
        "AQA_ALPACA_PAPER_SECRET_KEY_FILE",
    ):
        monkeypatch.setenv(name, "must-not-be-read")

    result = CliRunner().invoke(
        platform_cli.app,
        ["audit", "verify", "--application-root", str(application_root), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "check": "audit",
        "event_count": 3,
        "status": "ok",
        "stream_count": 2,
        "stream_heads": [
            {
                "event_hash": events[1].event_hash,
                "sequence": 2,
                "stream_id": "aqa_control:job:one",
            },
            {
                "event_hash": events[2].event_hash,
                "sequence": 1,
                "stream_id": "aqa_control:job:two",
            },
        ],
    }
    assert captured == {
        "application_root": application_root,
        "environment": {"AQA_CONFIG": "configs/platform/offline.yaml"},
        "service": RuntimeService.AUDIT_VERIFIER,
    }


def test_audit_verify_can_select_one_complete_stream(
    application_root: Path,
    audit_database: tuple[tuple[AuditEvent, ...], Engine],
) -> None:
    events, _ = audit_database
    result = CliRunner().invoke(
        platform_cli.app,
        [
            "audit",
            "verify",
            "--application-root",
            str(application_root),
            "--stream",
            "aqa_control:job:two",
            "--expected-sequence",
            "1",
            "--expected-hash",
            events[2].event_hash,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "check": "audit",
        "event_count": 1,
        "status": "ok",
        "stream_count": 1,
        "stream_heads": [
            {
                "event_hash": events[2].event_hash,
                "sequence": 1,
                "stream_id": "aqa_control:job:two",
            }
        ],
    }


def test_audit_verify_fails_closed_without_creating_the_schema(application_root: Path) -> None:
    database_path = application_root / "runtime" / "aqa-offline.sqlite3"
    assert not database_path.parent.exists()

    result = CliRunner().invoke(
        platform_cli.app,
        ["audit", "verify", "--application-root", str(application_root), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert not database_path.parent.exists()
    assert not database_path.exists()
    assert json.loads(result.stderr) == {
        "check": "audit",
        "error": "audit verification failed",
        "status": "error",
    }


def test_audit_verify_detects_tampering_without_disclosing_persisted_content(
    application_root: Path,
    audit_database: tuple[tuple[AuditEvent, ...], Engine],
) -> None:
    events, engine = audit_database
    sentinel = "private-audit-payload-must-not-escape"
    with engine.begin() as connection:
        connection.execute(
            update(aqa_audit_events)
            .where(aqa_audit_events.c.audit_event_id == events[0].audit_event_id)
            .values(payload={"authorization": sentinel})
        )

    result = CliRunner().invoke(
        platform_cli.app,
        ["audit", "verify", "--application-root", str(application_root), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert sentinel not in result.output
    assert json.loads(result.stderr) == {
        "check": "audit",
        "error": "audit verification failed",
        "status": "error",
    }


def test_audit_verify_normalizes_result_processor_failure_without_disclosing_value(
    application_root: Path,
    audit_database: tuple[tuple[AuditEvent, ...], Engine],
) -> None:
    events, engine = audit_database
    sentinel = "private-cli-timestamp-sentinel"
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE aqa_audit_events SET occurred_at = ? WHERE audit_event_id = ?",
            (sentinel, events[0].audit_event_id),
        )

    result = CliRunner().invoke(
        platform_cli.app,
        ["audit", "verify", "--application-root", str(application_root), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert sentinel not in result.output
    assert json.loads(result.stderr) == {
        "check": "audit",
        "error": "audit verification failed",
        "status": "error",
    }


def test_audit_verify_rejects_invalid_stream_without_echoing_it(
    application_root: Path,
    audit_database: tuple[tuple[AuditEvent, ...], Engine],
) -> None:
    del audit_database
    sentinel = "private invalid stream"
    result = CliRunner().invoke(
        platform_cli.app,
        [
            "audit",
            "verify",
            "--application-root",
            str(application_root),
            "--stream",
            sentinel,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 2
    assert sentinel not in result.output
    assert json.loads(result.stderr) == {
        "check": "audit",
        "error": "audit verification failed",
        "status": "error",
    }


def test_audit_verify_fails_for_a_well_formed_missing_stream_and_expected_tail(
    application_root: Path,
    audit_database: tuple[tuple[AuditEvent, ...], Engine],
) -> None:
    events, _ = audit_database
    runner = CliRunner()
    missing = runner.invoke(
        platform_cli.app,
        [
            "audit",
            "verify",
            "--application-root",
            str(application_root),
            "--stream",
            "aqa_control:job:missing",
            "--json",
        ],
        catch_exceptions=False,
    )
    wrong_head = runner.invoke(
        platform_cli.app,
        [
            "audit",
            "verify",
            "--application-root",
            str(application_root),
            "--stream",
            "aqa_control:job:two",
            "--expected-sequence",
            "2",
            "--expected-hash",
            events[2].event_hash,
            "--json",
        ],
        catch_exceptions=False,
    )
    untied_head = runner.invoke(
        platform_cli.app,
        [
            "audit",
            "verify",
            "--application-root",
            str(application_root),
            "--expected-sequence",
            "1",
            "--expected-hash",
            events[2].event_hash,
            "--json",
        ],
        catch_exceptions=False,
    )

    for result in (missing, wrong_head, untied_head):
        assert result.exit_code == 2
        assert json.loads(result.stderr) == {
            "check": "audit",
            "error": "audit verification failed",
            "status": "error",
        }
