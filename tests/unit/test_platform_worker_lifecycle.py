"""Worker lifecycle failure boundaries using injected domain cycles."""

import threading
from types import SimpleNamespace

import pytest

from adaptive_trader.platform import worker_runtime as runtime
from adaptive_trader.platform.config import RuntimeService


@pytest.mark.parametrize("state", ["blocked", "progressed", "idle", "raises"])
def test_once_worker_health_matches_domain_outcome_and_always_disposes(
    monkeypatch, tmp_path, state
):
    calls = []

    def run():
        if state == "raises":
            raise RuntimeError("private provider detail")
        return SimpleNamespace(state=SimpleNamespace(value=state), reason_code="test_outcome")

    cycle = SimpleNamespace(run_cycle=run, close=lambda: calls.append("closed"))
    engine = SimpleNamespace(dispose=lambda: calls.append("disposed"))
    monkeypatch.setattr(runtime, "load_runtime_settings", lambda *a, **k: object())
    monkeypatch.setattr(runtime, "create_platform_engine", lambda *a, **k: engine)
    monkeypatch.setattr(runtime, "_prepare_offline_database", lambda *a: None)
    monkeypatch.setattr(runtime, "build_worker_cycle", lambda *a: cycle)
    marker = runtime.ServiceHealthMarker(
        service=RuntimeService.SCHEDULER_WORKER, root=tmp_path / "health"
    )
    marker.record_ready()
    kwargs = dict(
        service=RuntimeService.SCHEDULER_WORKER,
        application_root=tmp_path,
        environment={},
        once=True,
        stop=threading.Event(),
        health_root=tmp_path / "health",
    )
    if state == "raises":
        with pytest.raises(runtime.ServiceRuntimeError, match="worker cycle failed"):
            runtime.run_service_worker(**kwargs)
    else:
        runtime.run_service_worker(**kwargs)
    assert marker.is_ready() is (state in ("progressed", "idle"))
    assert calls == ["closed", "disposed"]


def test_shutdown_failure_is_sanitized_and_still_restores_handlers_and_disposes(
    monkeypatch, tmp_path
):
    calls = []

    def close():
        raise RuntimeError("private shutdown detail")

    cycle = SimpleNamespace(
        run_cycle=lambda: SimpleNamespace(state=SimpleNamespace(value="idle"), reason_code="idle"),
        close=close,
    )
    monkeypatch.setattr(runtime, "load_runtime_settings", lambda *a, **k: object())
    monkeypatch.setattr(
        runtime,
        "create_platform_engine",
        lambda *a, **k: SimpleNamespace(dispose=lambda: calls.append("disposed")),
    )
    monkeypatch.setattr(runtime, "_prepare_offline_database", lambda *a: None)
    monkeypatch.setattr(runtime, "build_worker_cycle", lambda *a: cycle)
    monkeypatch.setattr(
        runtime, "_install_stop_handlers", lambda *a: lambda: calls.append("restored")
    )
    with pytest.raises(runtime.ServiceRuntimeError, match="worker shutdown failed"):
        runtime.run_service_worker(
            service=RuntimeService.SCHEDULER_WORKER,
            application_root=tmp_path,
            environment={},
            once=True,
            health_root=tmp_path / "health",
        )
    assert calls == ["restored", "disposed"]


def test_sql_slot_timestamp_normalizes_driver_timezone_without_relaxing_canonical_contract():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from adaptive_trader.platform.service_cycles import (
        WorkerCycleError,
        _optional_slot_timestamp,
        _slot_timestamp,
    )

    value = datetime(2026, 7, 6, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert _slot_timestamp(value) == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    assert _slot_timestamp(value).tzinfo is UTC
    assert _optional_slot_timestamp(None) is None
    for invalid in (None, "2026-07-06", value.replace(tzinfo=None)):
        with pytest.raises(WorkerCycleError, match="timestamp is invalid"):
            _slot_timestamp(invalid)
