"""Health evidence must not mutate symlink targets or accept forged process state."""

import json
import os
import signal
import threading
from pathlib import Path

import pytest

import adaptive_trader.platform.runtime as runtime
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import RuntimeService

SERVICE = RuntimeService.SCHEDULER_WORKER


def test_symlink_health_root_is_rejected_without_chmod_of_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(runtime.ServiceRuntimeError):
        runtime.ServiceHealthMarker(service=SERVICE, root=link)
    assert target.stat().st_mode & 0o777 == 0o755
    assert tuple(target.iterdir()) == ()


@pytest.mark.parametrize("value", [-1, 0, True, "123", None])
def test_forged_pid_is_rejected_before_process_probe(tmp_path: Path, value: object) -> None:
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=tmp_path)
    payload = {"monotonic_ns": 100, "pid": value, "ready": True, "service": SERVICE.value}
    path = tmp_path / f"{SERVICE.value}.json"
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    path.chmod(0o600)
    probes: list[int] = []
    assert (
        marker.is_ready(monotonic_ns=101, process_probe=lambda pid: probes.append(pid) or True)
        is False
    )
    assert probes == []


@pytest.mark.parametrize(
    "kind",
    [
        "future",
        "stale",
        "extra",
        "not_ready",
        "different_service",
        "malformed",
        "oversized",
        "public",
        "hardlink",
    ],
)
def test_invalid_or_untrusted_heartbeat_never_claims_readiness(tmp_path: Path, kind: str) -> None:
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=tmp_path)
    marker.record_ready(monotonic_ns=100, pid=123)
    path = tmp_path / f"{SERVICE.value}.json"
    payload = json.loads(path.read_bytes())
    now = 101
    if kind == "future":
        payload["monotonic_ns"] = 102
    elif kind == "stale":
        now = 100 + 90_000_000_001
    elif kind == "extra":
        payload["unexpected"] = True
    elif kind == "not_ready":
        payload["ready"] = False
    elif kind == "different_service":
        payload["service"] = RuntimeService.STRATEGY_WORKER.value
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    if kind == "malformed":
        path.write_bytes(b"not-json")
    elif kind == "oversized":
        path.write_bytes(b"x" * 4097)
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "hardlink":
        os.link(path, tmp_path / "second-link")
    assert marker.is_ready(monotonic_ns=now, process_probe=lambda _pid: True) is False


@pytest.mark.parametrize("failure", ["zero_write", "disk_error"])
def test_failed_publication_preserves_prior_marker_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    marker = runtime.ServiceHealthMarker(service=SERVICE, root=tmp_path)
    marker.record_ready(monotonic_ns=100, pid=123)
    path = tmp_path / f"{SERVICE.value}.json"
    original = path.read_bytes()

    def fail_write(*_args):
        if failure == "zero_write":
            return 0
        raise OSError("synthetic disk error")

    monkeypatch.setattr(runtime.os, "write", fail_write)
    with pytest.raises(runtime.ServiceRuntimeError):
        marker.record_ready(monotonic_ns=101, pid=123)
    assert path.read_bytes() == original
    assert tuple(tmp_path.iterdir()) == (path,)


def test_partial_signal_installation_restores_original_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {selected: object() for selected in (signal.SIGINT, signal.SIGTERM)}
    current = dict(original)
    calls = 0
    monkeypatch.setattr(runtime.signal, "getsignal", original.__getitem__)

    def install(selected, handler):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic registration failure")
        current[selected] = handler

    monkeypatch.setattr(runtime.signal, "signal", install)
    with pytest.raises(runtime.ServiceRuntimeError, match="signal handling"):
        runtime._install_stop_handlers(threading.Event())
    assert current == original
