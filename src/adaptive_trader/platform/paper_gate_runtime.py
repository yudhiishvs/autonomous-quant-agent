"""Runnable default-deny boundary for the isolated paper-execution container."""

from __future__ import annotations

import json
import logging
import os
import signal
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import FrameType

from adaptive_trader.platform.config import (
    ExecutionMode,
    RuntimeService,
    RuntimeSettings,
)
from adaptive_trader.platform.paper_cycle import PaperCycle

_HEALTH_ROOT = Path("/tmp/aqa-service-health")  # nosec B108
_HEALTH_FILE = "paper-execution-worker.json"
_HEALTH_MAX_AGE_NS = 90_000_000_000
_POLL_SECONDS = 1.0


class PaperGateRuntimeError(RuntimeError):
    """The paper boundary could not preserve its explicit default-deny state."""


def run_paper_gate(
    settings: RuntimeSettings,
    *,
    once: bool = False,
    stop: threading.Event | None = None,
    health_root: Path = _HEALTH_ROOT,
    cycle: PaperCycle | None = None,
) -> None:
    """Consume bounded proposals; mark ready only after durable authorization work."""

    if (
        type(settings) is not RuntimeSettings
        or settings.service is not RuntimeService.PAPER_EXECUTION_WORKER
        or settings.platform.profile.mode is not ExecutionMode.PAPER
        or settings.platform.profile.execution.broker != "alpaca_paper"
    ):
        raise PaperGateRuntimeError("paper runtime configuration is invalid")
    selected_stop = threading.Event() if stop is None else stop
    if not isinstance(selected_stop, threading.Event):
        raise TypeError("paper runtime stop boundary must be a threading Event")
    restore = _install_stop_handlers(selected_stop) if stop is None else None
    engine = None
    selected_cycle = cycle
    try:
        _clear_health(health_root)
        if selected_cycle is None:
            from adaptive_trader.platform.paper_cycle import (
                AuditPaperDenialRecorder,
                PaperExecutionCycle,
                SQLPaperCandidateSource,
            )
            from adaptive_trader.platform.storage.engine import create_platform_engine

            engine = create_platform_engine(settings, application_name="aqa-paper-execution")
            selected_cycle = PaperExecutionCycle(
                settings,
                candidates=SQLPaperCandidateSource(settings, engine),
                denials=AuditPaperDenialRecorder(engine),
            )
        while not selected_stop.is_set():
            result = selected_cycle.run_cycle()
            logging.getLogger(__name__).info(
                "paper cycle state=%s reason=%s units=%d",
                result.state.value,
                result.reason_code,
                result.work_units,
            )
            _record_health(health_root)
            if once:
                return
            selected_stop.wait(_POLL_SECONDS)
    except Exception:
        with suppress(OSError, PaperGateRuntimeError):
            _clear_health(health_root)
        raise PaperGateRuntimeError("paper authorization cycle failed") from None
    finally:
        try:
            try:
                if selected_cycle is not None:
                    selected_cycle.close()
            finally:
                if engine is not None:
                    engine.dispose()
        except Exception:
            with suppress(OSError, PaperGateRuntimeError):
                _clear_health(health_root)
            raise PaperGateRuntimeError("paper authorization cleanup failed") from None
        finally:
            if restore is not None:
                restore()


def paper_gate_is_healthy(*, health_root: Path = _HEALTH_ROOT) -> bool:
    """Accept only a fresh owner-private marker belonging to a live paper-gate process."""

    path = health_root / _HEALTH_FILE
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= 1_024
        ):
            return False
        payload = json.loads(path.read_text(encoding="ascii"))
        if type(payload) is not dict or frozenset(payload) != {
            "monotonic_ns",
            "pid",
            "ready",
            "service",
        }:
            return False
        recorded = payload["monotonic_ns"]
        pid = payload["pid"]
        if (
            payload["ready"] is not True
            or payload["service"] != RuntimeService.PAPER_EXECUTION_WORKER.value
            or type(recorded) is not int
            or type(pid) is not int
            or pid < 1
            or not 0 <= time.monotonic_ns() - recorded <= _HEALTH_MAX_AGE_NS
        ):
            return False
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, UnicodeError, ValueError):
        return False


def _clear_health(root: Path) -> None:
    """Remove only this service's marker through an owned, non-symlink directory."""
    if (
        type(root) is not type(Path())
        or not root.is_absolute()
        or root.is_symlink()
        or (root.exists() and root.resolve(strict=True) != root)
    ):
        raise PaperGateRuntimeError("paper health root is unsafe")
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise PaperGateRuntimeError("paper health root is unsafe")
        with suppress(FileNotFoundError):
            os.unlink(_HEALTH_FILE, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def _record_health(root: Path) -> None:
    temporary: Path | None = None
    try:
        if type(root) is not type(Path()) or not root.is_absolute():
            raise PaperGateRuntimeError("paper health root is unsafe")
        if root.is_symlink():
            raise PaperGateRuntimeError("paper health root is unsafe")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            root.is_symlink()
            or root.resolve(strict=True) != root
            or not stat.S_ISDIR(root.stat(follow_symlinks=False).st_mode)
        ):
            raise PaperGateRuntimeError("paper health root is unsafe")
        root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(root_descriptor).st_uid != os.geteuid():
                raise PaperGateRuntimeError("paper health root is unsafe")
            os.fchmod(root_descriptor, 0o700)
        finally:
            os.close(root_descriptor)
        payload = {
            "monotonic_ns": time.monotonic_ns(),
            "pid": os.getpid(),
            "ready": True,
            "service": RuntimeService.PAPER_EXECUTION_WORKER.value,
        }
        encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(
            "ascii"
        )
        temporary = root / f".{_HEALTH_FILE}.{os.getpid()}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise PaperGateRuntimeError("paper health marker write did not progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, root / _HEALTH_FILE)
    except PaperGateRuntimeError:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
        raise
    except (OSError, TypeError, ValueError):
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
        raise PaperGateRuntimeError("paper health marker could not be updated") from None


def _install_stop_handlers(stop: threading.Event) -> Callable[[], None]:
    previous = {
        selected: signal.getsignal(selected) for selected in (signal.SIGINT, signal.SIGTERM)
    }

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    for selected in previous:
        signal.signal(selected, request_stop)

    def restore() -> None:
        for selected, handler in previous.items():
            signal.signal(selected, handler)

    return restore


__all__ = [
    "PaperGateRuntimeError",
    "paper_gate_is_healthy",
    "run_paper_gate",
]
