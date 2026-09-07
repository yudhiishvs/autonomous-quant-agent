"""Minimal shared worker health boundary without API, broker or provider imports."""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.config import RuntimeService

_HEALTH_MAX_AGE_NS = 90_000_000_000
DOMAIN_WORKER_SERVICES = frozenset(
    {
        RuntimeService.MARKET_DATA_WORKER,
        RuntimeService.SCHEDULER_WORKER,
        RuntimeService.STRATEGY_WORKER,
        RuntimeService.EXECUTION_WORKER,
        RuntimeService.MARKET_DATA_LIVE,
    }
)
_HEALTH_WORKERS = DOMAIN_WORKER_SERVICES | {RuntimeService.JOB_WORKER}
# The directory is created owner-private and all marker access uses no-follow dir fds.
_HEALTH_ROOT = Path("/tmp/aqa-service-health")  # nosec B108
_MARKER_KEYS = frozenset({"monotonic_ns", "pid", "ready", "service"})
_MARKER_LIMIT = 4_096
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class ServiceRuntimeError(RuntimeError):
    """A runtime boundary failed without exposing sensitive diagnostics."""


class ServiceHealthMarker:
    """Owner-private, atomic heartbeat shared with an in-container health command."""

    def __init__(self, *, service: RuntimeService, root: Path = _HEALTH_ROOT) -> None:
        if type(service) is not RuntimeService or service not in _HEALTH_WORKERS:
            raise ValueError("health markers are unavailable for this service")
        if type(root) is not type(Path()) or not root.is_absolute():
            raise ValueError("health marker root must be an absolute Path")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.resolve(strict=True) != root:
                raise ServiceRuntimeError("health marker root is unsafe")
            descriptor = os.open(root, _DIRECTORY_FLAGS)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                    raise ServiceRuntimeError("health marker root is unsafe")
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
        except ServiceRuntimeError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ServiceRuntimeError("health marker root could not be prepared") from None
        self._service = service
        self._root = root
        self._filename = f"{service.value}.json"

    def record_ready(self, *, monotonic_ns: int | None = None, pid: int | None = None) -> None:
        instant = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        process_id = os.getpid() if pid is None else pid
        if type(instant) is not int or instant < 0 or type(process_id) is not int or process_id < 1:
            raise ServiceRuntimeError("health marker values are invalid")
        payload: dict[str, JsonValue] = {
            "monotonic_ns": instant,
            "pid": process_id,
            "ready": True,
            "service": self._service.value,
        }
        encoded = canonical_json_bytes(payload) + b"\n"
        root_fd = -1
        temporary = f".{self._service.value}.{process_id}.{instant}.tmp"
        temporary_created = False
        try:
            root_fd = os.open(self._root, _DIRECTORY_FLAGS)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
            temporary_created = True
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise ServiceRuntimeError("health marker write did not make progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary,
                self._filename,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            temporary_created = False
            os.fsync(root_fd)
        except ServiceRuntimeError:
            raise
        except (OSError, TypeError, ValueError):
            raise ServiceRuntimeError("health marker could not be updated") from None
        finally:
            if root_fd >= 0:
                if temporary_created:
                    with suppress(OSError):
                        os.unlink(temporary, dir_fd=root_fd)
                os.close(root_fd)

    def is_ready(
        self,
        *,
        monotonic_ns: int | None = None,
        process_probe: Callable[[int], bool] | None = None,
    ) -> bool:
        instant = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        if type(instant) is not int or instant < 0:
            return False
        probe = _process_exists if process_probe is None else process_probe
        if not callable(probe):
            return False
        root_fd = -1
        descriptor = -1
        try:
            root_fd = os.open(self._root, _DIRECTORY_FLAGS)
            descriptor = os.open(
                self._filename,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=root_fd,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or not 0 < metadata.st_size <= _MARKER_LIMIT
            ):
                return False
            payload = os.read(descriptor, _MARKER_LIMIT + 1)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if root_fd >= 0:
                os.close(root_fd)
        try:
            decoded = json.loads(payload)
            if (
                type(decoded) is not dict
                or frozenset(decoded) != _MARKER_KEYS
                or canonical_json_bytes(decoded) + b"\n" != payload
                or decoded.get("service") != self._service.value
                or decoded.get("ready") is not True
            ):
                return False
            recorded = decoded.get("monotonic_ns")
            pid = decoded.get("pid")
            if (
                type(recorded) is not int
                or recorded < 0
                or type(pid) is not int
                or pid < 1
                or not 0 <= instant - recorded <= _HEALTH_MAX_AGE_NS
            ):
                return False
            return probe(pid) is True
        except (TypeError, UnicodeError, ValueError):
            return False

    def invalidate(self) -> None:
        """Withdraw readiness without following a replaced root directory."""
        descriptor = -1
        try:
            descriptor = os.open(self._root, _DIRECTORY_FLAGS)
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.geteuid():
                raise ServiceRuntimeError("health marker root is unsafe")
            with suppress(FileNotFoundError):
                os.unlink(self._filename, dir_fd=descriptor)
        except FileNotFoundError:
            return
        except OSError:
            raise ServiceRuntimeError("health marker could not be invalidated") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _process_exists(pid: int) -> bool:
    if type(pid) is not int or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True
