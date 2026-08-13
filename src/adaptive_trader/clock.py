"""Injectable timezone-aware clocks for live operation and deterministic replay."""

from __future__ import annotations

import threading
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Protocol

from adaptive_trader.constants import UTC


def as_utc(value: datetime, *, field: str = "timestamp") -> datetime:
    """Validate that ``value`` is timezone-aware and normalize it to UTC."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


class Clock(Protocol):
    """Minimal clock contract used by services and replay."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""

    async def sleep(self, seconds: float) -> None:
        """Wait or advance by ``seconds``."""


class SystemClock:
    """Wall clock used by real observer and paper services."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(max(0.0, float(seconds)))


class FakeClock:
    """Thread-safe deterministic clock whose sleeps advance simulated time."""

    def __init__(self, start: datetime) -> None:
        self._now = as_utc(start, field="start")
        self._lock = threading.RLock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, value: datetime) -> datetime:
        with self._lock:
            self._now = as_utc(value)
            return self._now

    def advance(self, seconds: float | timedelta) -> datetime:
        delta = seconds if isinstance(seconds, timedelta) else timedelta(seconds=float(seconds))
        if delta.total_seconds() < 0:
            raise ValueError("FakeClock cannot move backward")
        with self._lock:
            self._now += delta
            return self._now

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


SleepFunction = Awaitable[None]
