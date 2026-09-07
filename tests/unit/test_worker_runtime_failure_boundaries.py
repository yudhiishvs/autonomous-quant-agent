"""Failure, recovery and resource ownership at the public worker boundary."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from adaptive_trader.platform import worker_runtime as runtime
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import RuntimeService

SERVICE = RuntimeService.SCHEDULER_WORKER


@pytest.mark.parametrize("state", ["blocked", "completed", "raise"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_worker_withdraws_health_and_disposes_after_domain_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, close_fails: bool
) -> None:
    events: list[str] = []
    stop = threading.Event()
    marker_root = tmp_path / "health"
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=marker_root)
    marker.record_ready()

    class Cycle:
        def run_cycle(self):
            events.append("run")
            if state == "raise":
                raise RuntimeError("private-database-password")
            return SimpleNamespace(
                state=SimpleNamespace(value=state), reason_code="fixture_outcome"
            )

        def close(self):
            events.append("close")
            if close_fails:
                raise RuntimeError("private-shutdown-password")

    monkeypatch.setattr(runtime, "load_runtime_settings", lambda *a, **k: object())
    monkeypatch.setattr(
        runtime,
        "create_platform_engine",
        lambda *a, **k: SimpleNamespace(dispose=lambda: events.append("dispose")),
    )
    monkeypatch.setattr(runtime, "_prepare_offline_database", lambda *a: None)
    monkeypatch.setattr(runtime, "build_worker_cycle", lambda *a: Cycle())
    if state == "raise" or close_fails:
        with pytest.raises(runtime.ServiceRuntimeError) as failure:
            runtime.run_service_worker(
                service=SERVICE,
                application_root=tmp_path,
                environment={},
                once=True,
                stop=stop,
                health_root=marker_root,
            )
        assert "private" not in str(failure.value)
    else:
        runtime.run_service_worker(
            service=SERVICE,
            application_root=tmp_path,
            environment={},
            once=True,
            stop=stop,
            health_root=marker_root,
        )
    assert events == ["run", "close", "dispose"]
    assert marker.is_ready() is (state == "completed")


@pytest.mark.parametrize(
    "mutation",
    [
        {"monotonic_ns": True},
        {"monotonic_ns": -1},
        {"monotonic_ns": 101},
        {"monotonic_ns": 100 - 90_000_000_001},
        {"pid": True},
        {"pid": "1"},
        {"ready": 1},
        {"service": "other"},
        {"extra": 1},
    ],
)
def test_health_rejects_corrupt_values_without_probing_process(
    tmp_path: Path, mutation: dict
) -> None:
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=tmp_path / "health")
    payload = {"monotonic_ns": 100, "pid": os.getpid(), "ready": True, "service": SERVICE.value}
    payload.update(mutation)
    target = tmp_path / "health" / f"{SERVICE.value}.json"
    target.write_bytes(canonical_json_bytes(payload) + b"\n")
    target.chmod(0o600)
    probes = []
    assert not marker.is_ready(
        monotonic_ns=100, process_probe=lambda pid: probes.append(pid) or True
    )
    assert probes == []


@pytest.mark.parametrize("fault", ["zero-write", "fsync"])
def test_failed_health_publish_leaves_no_ready_or_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = tmp_path / "health"
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=root)
    if fault == "zero-write":
        monkeypatch.setattr(runtime.os, "write", lambda *args: 0)
    else:

        def fail(*args):
            raise OSError("private-filesystem-detail")

        monkeypatch.setattr(runtime.os, "fsync", fail)
    with pytest.raises(runtime.ServiceRuntimeError) as error:
        marker.record_ready()
    assert "private" not in str(error.value)
    assert list(root.iterdir()) == []


def test_symlink_application_root_rejected_before_settings_or_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe root crossed settings boundary")

    monkeypatch.setattr(runtime, "load_runtime_settings", forbidden)
    with pytest.raises(runtime.ServiceRuntimeError, match="application root is unavailable"):
        runtime.run_service_worker(service=SERVICE, application_root=link, once=True)


def test_shared_marker_rejects_symlink_root_without_changing_target(tmp_path: Path) -> None:
    from adaptive_trader.platform import runtime as api_runtime

    assert api_runtime.ServiceHealthMarker is runtime.ServiceHealthMarker
    assert api_runtime.ServiceRuntimeError is runtime.ServiceRuntimeError
    target = tmp_path / "protected"
    target.mkdir(mode=0o750)
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    before = target.stat().st_mode
    with pytest.raises(runtime.ServiceRuntimeError):
        runtime.ServiceHealthMarker(service=SERVICE, root=link)
    assert target.stat().st_mode == before


def test_marker_invalidation_refuses_replaced_root_symlink(tmp_path: Path) -> None:
    root = tmp_path / "health"
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=root)
    marker.record_ready()
    protected = tmp_path / "protected"
    root.rename(protected)
    root.symlink_to(protected, target_is_directory=True)
    with pytest.raises(runtime.ServiceRuntimeError):
        marker.invalidate()
    assert (protected / f"{SERVICE.value}.json").exists()
