#!/usr/bin/env python3
"""Destructive, explicitly disposable PostgreSQL readiness capacity measurement.

Seeds synthetic immutable rows in bounded batches, then uses the actual validating
reader and quality hash. Fixture insertion bypasses intake/audit and is not an intake
throughput claim. No provider credentials, operational data or order authority is used.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert, text

from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.watermarks import (
    DataSeries,
    compute_symbol_readiness_from_batches,
)
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    _bar_event_content_hash,
    _bar_event_id,
    read_effective_bars,
)
from adaptive_trader.platform.storage.tables import (
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
)
from scripts.postgres_backup_restore_smoke import _engine, _reset_and_migrate, _source_url


def run(years: int) -> dict[str, object]:
    if type(years) is not int or not 1 <= years <= 10:
        raise ValueError("years must be between 1 and 10")
    if os.environ.get("APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES") != "YES":
        raise ValueError("disposable cluster acknowledgement required")
    url = _source_url()
    _reset_and_migrate(url)
    engine = _engine(url, application_name="readiness-capacity-proof")
    calendar = XnasExchangeCalendar()
    start = datetime(2026 - years, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)

    def batches(begin=start):
        day = begin
        while day < end:
            following = day + timedelta(days=1)
            batch = calendar.expected_intervals(start_at=day, end_at=following, timeframe="1Min")
            if batch:
                yield batch
            day = following

    def identity(interval):
        return BarIdentity(
            "alpaca", "iex", "raw", "AMD", "1Min", interval.start_at, interval.end_at
        )

    count = 0
    begun = time.monotonic()
    try:
        for batch in batches():
            identities, events, latest = [], [], []
            for interval in batch:
                bar = BarWrite(
                    identity(interval),
                    end,
                    *(Decimal(100) for _ in range(4)),
                    Decimal(1000),
                    ("complete",),
                    "capacity_fixture",
                    "0" * 64,
                )
                ident = bar.identity
                event_id = _bar_event_id(ident.bar_identity_id, 1, bar.normalized_payload_hash)
                digest = _bar_event_content_hash(
                    bar_event_id=event_id, revision=1, correction_of_event_id=None, bar=bar
                )
                identities.append(
                    asdict(ident)
                    | dict(
                        bar_identity_id=ident.bar_identity_id,
                        content_hash=ident.identity_hash,
                        created_at=end,
                    )
                )
                payload = asdict(bar)
                del payload["identity"]
                events.append(
                    payload
                    | dict(
                        bar_event_id=event_id,
                        bar_identity_id=ident.bar_identity_id,
                        revision=1,
                        normalized_payload_hash=bar.normalized_payload_hash,
                        correction_of_event_id=None,
                        content_hash=digest,
                        created_at=end,
                    )
                )
                latest.append(
                    dict(
                        bar_identity_id=ident.bar_identity_id,
                        bar_event_id=event_id,
                        revision=1,
                        content_hash=digest,
                        version=1,
                        projected_at=end,
                    )
                )
            with engine.begin() as connection:
                connection.execute(insert(aqa_bar_identities), identities)
                connection.execute(insert(aqa_bar_events), events)
                connection.execute(insert(aqa_bar_latest), latest)
            count += len(batch)
        seed_seconds = time.monotonic() - begun
        with engine.begin() as connection:
            for table in (aqa_bar_identities, aqa_bar_events, aqa_bar_latest):
                connection.execute(text(f"ANALYZE aqa.{table.name}"))
        queries = 0

        def event_batches(begin=start):
            nonlocal queries
            for batch in batches(begin):
                with engine.begin() as connection:
                    rows = read_effective_bars(connection, tuple(identity(i) for i in batch))
                assert len(rows) == len(batch)
                queries += 1
                yield rows

        prefixes = []
        begun = time.monotonic()
        result = compute_symbol_readiness_from_batches(
            series=DataSeries("alpaca", "iex", "raw", "AMD", "1Min"),
            expected_intervals=(interval for batch in batches() for interval in batch),
            effective_event_batches=event_batches(),
            unresolved_gaps=(),
            checkpoint_at=end - timedelta(days=1),
            checkpoint_sink=prefixes.append,
        )
        elapsed = time.monotonic() - begun
        assert result.contiguous_through == result.range_end_at
        cold_batches = queries
        queries = 0
        assert len(prefixes) == 1
        prefix = prefixes[0]
        begun = time.monotonic()
        warm = compute_symbol_readiness_from_batches(
            series=DataSeries("alpaca", "iex", "raw", "AMD", "1Min"),
            expected_intervals=(i for batch in batches(prefix.resume_at) for i in batch),
            effective_event_batches=event_batches(prefix.resume_at),
            unresolved_gaps=(),
            prefix=prefix,
        )
        warm_elapsed = time.monotonic() - begun
        assert warm == result
        return dict(
            years=years,
            symbols=1,
            bars=count,
            session_batches=cold_batches,
            warm_session_batches=queries,
            warm_readiness_seconds=warm_elapsed,
            cached_and_full_results_identical=True,
            fixture_seed_seconds=seed_seconds,
            readiness_seconds=elapsed,
            quality_hash=result.quality_hash,
            peak_rss_native_units=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            scope="populated_synthetic_readiness_scan_only",
            passed=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run(args.years), sort_keys=True))
