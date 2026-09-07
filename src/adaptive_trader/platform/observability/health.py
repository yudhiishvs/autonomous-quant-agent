"""Bounded liveness and dependency readiness reporting."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError

_CHECK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$", re.ASCII)


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    probe: Callable[[], bool]

    def __post_init__(self) -> None:
        if type(self.name) is not str or _CHECK_NAME.fullmatch(self.name) is None:
            raise ValueError("readiness check name is invalid")
        if not callable(self.probe):
            raise TypeError("readiness check probe is invalid")


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    checked_at: datetime
    checks: tuple[tuple[str, bool], ...]

    @property
    def healthy(self) -> bool:
        return self.status in {"live", "ready"}

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checked_at": self.checked_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "checks": {name: passed for name, passed in self.checks},
        }


class HealthService:
    """Separate process liveness from authoritative dependency readiness."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        readiness_checks: Sequence[ReadinessCheck],
    ) -> None:
        if not callable(clock):
            raise TypeError("health service requires an injected clock")
        if not isinstance(readiness_checks, Sequence) or isinstance(readiness_checks, (str, bytes)):
            raise TypeError("readiness checks must use a concrete sequence")
        checks = tuple(readiness_checks)
        if any(type(check) is not ReadinessCheck for check in checks):
            raise TypeError("readiness checks must use the typed contract")
        if len({check.name for check in checks}) != len(checks):
            raise ValueError("readiness check names must be unique")
        self._clock = clock
        self._checks = checks

    def live(self) -> HealthReport:
        return HealthReport(status="live", checked_at=self._now(), checks=())

    def ready(self) -> HealthReport:
        outcomes: list[tuple[str, bool]] = []
        for check in self._checks:
            passed = False
            try:
                passed = check.probe() is True
            except Exception:
                passed = False
            outcomes.append((check.name, passed))
        return HealthReport(
            status="ready" if all(passed for _, passed in outcomes) else "not_ready",
            checked_at=self._now(),
            checks=tuple(outcomes),
        )

    def _now(self) -> datetime:
        try:
            return require_utc_instant(self._clock(), field_name="health.checked_at")
        except DomainValidationError:
            raise RuntimeError("health clock returned an invalid instant") from None
