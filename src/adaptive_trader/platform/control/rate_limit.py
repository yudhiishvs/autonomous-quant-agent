"""Single-operator in-process request limiting with an injected monotonic clock."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from collections.abc import Callable
from enum import StrEnum


class RateClass(StrEnum):
    READ = "read"
    MUTATION = "mutation"


class OperatorRateLimiter:
    """Sliding-window limiter; intentionally not distributed DDoS protection."""

    def __init__(self, *, monotonic: Callable[[], float]) -> None:
        if not callable(monotonic):
            raise TypeError("rate limiter requires an injected monotonic clock")
        self._monotonic = monotonic
        self._events: dict[tuple[str, RateClass], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, *, fingerprint: str, rate_class: RateClass) -> bool:
        if type(fingerprint) is not str or len(fingerprint) != 64:
            raise ValueError("rate-limit principal fingerprint is invalid")
        if type(rate_class) is not RateClass:
            raise TypeError("rate-limit class must use the closed contract")
        now = self._monotonic()
        if type(now) is not float or now < 0:
            raise RuntimeError("rate-limit clock returned an invalid value")
        limit = 120 if rate_class is RateClass.READ else 10
        key = (fingerprint, rate_class)
        with self._lock:
            events = self._events[key]
            cutoff = now - 60.0
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
