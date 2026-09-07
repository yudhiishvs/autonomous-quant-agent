"""Production service-entry wiring and heartbeat tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from typer.testing import CliRunner

import adaptive_trader.platform.runtime as platform_runtime
from adaptive_trader.platform.cli import app
from adaptive_trader.platform.config import RuntimeService
from adaptive_trader.platform.runtime import (
    ServiceHealthMarker,
    ServiceRuntimeError,
    serve_control_api,
    serve_dashboard,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _application_root(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "application"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    token = root / "operator-token"
    token.write_text("T" * 32, encoding="ascii")
    token.chmod(0o600)
    return root, {"AQA_OPERATOR_TOKEN_FILE": token.as_posix()}


def test_control_api_entry_builds_private_app_with_bounded_server_options(
    tmp_path: Path,
) -> None:
    root, environment = _application_root(tmp_path)
    captured: list[tuple[FastAPI, dict[str, object]]] = []

    def runner(api: FastAPI, **options: object) -> None:
        captured.append((api, options))

    serve_control_api(
        host="0.0.0.0",
        port=8000,
        application_root=root,
        environment=environment,
        runner=runner,
    )

    api, options = captured[0]
    paths = {route.path for route in api.routes}
    assert {"/health/live", "/health/ready", "/v1/jobs/offline-demo"} <= paths
    assert "/docs" not in paths
    assert options == {
        "access_log": False,
        "host": "0.0.0.0",
        "log_config": None,
        "port": 8000,
        "proxy_headers": False,
        "server_header": False,
        "timeout_graceful_shutdown": 30,
        "workers": 1,
    }


def test_dashboard_entry_uses_in_process_streamlit_with_defensive_options(
    tmp_path: Path,
) -> None:
    root, environment = _application_root(tmp_path)
    captured: list[tuple[str, bool, list[str], dict[str, Any]]] = []

    def runner(
        script: str,
        is_hello: bool,
        args: list[str],
        options: dict[str, Any],
    ) -> None:
        captured.append((script, is_hello, args, options))

    serve_dashboard(
        host="0.0.0.0",
        port=8501,
        application_root=root,
        environment=environment,
        runner=runner,
    )

    script, is_hello, args, options = captured[0]
    assert script.endswith("/adaptive_trader/platform/dashboard/app.py")
    assert is_hello is False
    assert args == [str(root)]
    assert options["server.address"] == "0.0.0.0"
    assert options["server.port"] == 8501
    assert options["server.enableXsrfProtection"] is True
    assert options["server.enableCORS"] is True
    assert options["server.headless"] is True


def test_server_entries_reject_unapproved_addresses_before_running(tmp_path: Path) -> None:
    root, environment = _application_root(tmp_path)

    for call in (
        lambda: serve_control_api(
            host="192.0.2.1",
            port=8000,
            application_root=root,
            environment=environment,
            runner=lambda _api, **_options: None,
        ),
        lambda: serve_dashboard(
            host="127.0.0.1",
            port=8502,
            application_root=root,
            environment=environment,
            runner=lambda _script, _hello, _args, _options: None,
        ),
    ):
        try:
            call()
        except ServiceRuntimeError as error:
            assert "not allowed" in str(error)
        else:
            raise AssertionError("unapproved server address was accepted")


def test_health_marker_requires_owner_private_fresh_live_process(tmp_path: Path) -> None:
    marker = ServiceHealthMarker(
        service=RuntimeService.JOB_WORKER,
        root=(tmp_path / "health").resolve(),
    )
    marker.record_ready(monotonic_ns=1_000, pid=42)

    assert marker.is_ready(
        monotonic_ns=2_000,
        process_probe=lambda pid: pid == 42,
    )
    assert not marker.is_ready(
        monotonic_ns=90_000_002_000,
        process_probe=lambda _pid: True,
    )
    assert not marker.is_ready(
        monotonic_ns=2_000,
        process_probe=lambda _pid: False,
    )

    marker_path = tmp_path / "health" / "job-worker.json"
    marker_path.write_text('{"ready":true}', encoding="utf-8")
    assert not marker.is_ready(
        monotonic_ns=2_000,
        process_probe=lambda _pid: True,
    )


def test_cli_exposes_only_the_implemented_background_runtime(
    monkeypatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[RuntimeService, Path, bool]] = []

    def run_platform_worker(*, service: RuntimeService, application_root: Path, once: bool) -> None:
        calls.append((service, application_root, once))

    monkeypatch.setattr(platform_runtime, "run_platform_worker", run_platform_worker)
    implemented = (
        RuntimeService.JOB_WORKER,
        RuntimeService.MARKET_DATA_WORKER,
        RuntimeService.SCHEDULER_WORKER,
        RuntimeService.STRATEGY_WORKER,
        RuntimeService.EXECUTION_WORKER,
        RuntimeService.MARKET_DATA_LIVE,
    )
    successes = tuple(
        runner.invoke(app, ["service", "run", service.value, "--once"]) for service in implemented
    )
    rejected = tuple(
        runner.invoke(app, ["service", "run", service, "--once"])
        for service in ("arbitrary-worker", "paper-execution-worker", "control-api")
    )

    assert all(result.exit_code == 0 for result in successes)
    assert calls == [(service, Path("."), True) for service in implemented]
    assert all(result.exit_code == 2 for result in rejected)
    assert all("not an implemented runtime" in result.stderr for result in rejected)


def test_cli_service_health_preserves_healthcheck_exit_status(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(platform_runtime, "job_worker_is_healthy", lambda **_kwargs: True)
    ready = runner.invoke(app, ["service", "health", "job-worker"])
    monkeypatch.setattr(platform_runtime, "job_worker_is_healthy", lambda **_kwargs: False)
    unavailable = runner.invoke(app, ["service", "health", "job-worker"])

    assert ready.exit_code == 0
    assert ready.stdout == "service health: ready\n"
    assert unavailable.exit_code == 1
    assert unavailable.stderr == "service health: not ready\n"
