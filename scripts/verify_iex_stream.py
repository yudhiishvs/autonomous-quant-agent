#!/usr/bin/env python3
"""Explicit, bounded, data-only verification of the deployed IEX adapter."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from adaptive_trader.collection.alpaca import AlpacaLiveBarSource
from adaptive_trader.collection.credentials import AlpacaDataCredentials


def verify(
    factory: Callable[..., Any], *, seconds: int, monotonic: Callable[[], float] = time.monotonic
) -> dict[str, object]:
    """Require current bars on two independent connections; never persist market payloads."""
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError("stream observation duration must be between 1 and 300 seconds")
    attempts = []
    for _ in range(2):
        started = monotonic()
        states: set[str] = set()
        bars = 0
        stale = 0
        latest = None
        maximum_lag = 0.0

        def receive(observation: Any) -> None:
            nonlocal bars, stale, latest, maximum_lag
            bar = observation.bar
            lag = (bar.receipt_timestamp_utc - bar.bar_timestamp_utc).total_seconds()
            maximum_lag = max(maximum_lag, lag)
            if lag < 60 or lag > 180:
                stale += 1
            bars += 1
            latest = bar.bar_timestamp_utc

        source = factory(state_handler=states.add)
        error = False
        try:
            source.run(
                ("AMD",),
                receive,
                stop_requested=lambda began=started: monotonic() - began >= seconds,
            )
        except Exception:
            # Provider exceptions are deliberately not serialized into operator evidence.
            error = True
        finally:
            source.stop()
        attempts.append(
            {
                "authenticated": "authenticated" in states,
                "subscribed": "subscribed" in states,
                "bars": bars,
                "stale_or_future_bars": stale,
                "latest_bar_at": None if latest is None else latest.isoformat(),
                "maximum_lag_seconds": maximum_lag,
                "failed": error,
            }
        )
    passed = all(
        row["authenticated"]
        and row["subscribed"]
        and row["bars"] > 0
        and row["stale_or_future_bars"] == 0
        and not row["failed"]
        for row in attempts
    )
    return {
        "status": "IMPLEMENTED_AND_VERIFIED" if passed else "IMPLEMENTED_NOT_EXTERNALLY_VALIDATED",
        "scope": "bounded_iex_adapter_receipt_and_reconnect",
        "observed_at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "attempts": attempts,
        "database_persistence_verified": False,
        "unattended_operation_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-provider", action="store_true", help="Authorize data-only IEX access."
    )
    parser.add_argument(
        "--seconds", type=int, default=120, help="Observation seconds per connection (1-300)."
    )
    args = parser.parse_args()
    if not args.allow_provider:
        parser.error("explicit --allow-provider acknowledgement is required")
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    try:
        credentials = AlpacaDataCredentials.from_environment()
        report = verify(
            lambda **kwargs: AlpacaLiveBarSource(credentials, **kwargs), seconds=args.seconds
        )
    except Exception:
        print(json.dumps({"passed": False, "reason": "stream_verification_unavailable"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
