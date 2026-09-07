"""Atomic adapter from the fenced collector projection to canonical market data.

The established collector owns provider transport, raw observations, source precedence, and its
singleton lease. This module gives its selected values canonical identities and enqueues durable
session work in that same transaction. It grants no research or execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from adaptive_trader.collection.repository import CoverageAdvance
from adaptive_trader.collection.schema import bar_observations, current_bars
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.normalization import NormalizationPolicy, normalize_alpaca_bar
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteStatus,
    MarketDataRepository,
)

_POLICY = NormalizationPolicy(
    collection_allowlist=tuple(sorted(COLLECTION_UNIVERSE_V1.symbols)), excluded_symbols=()
)


def _canonical_write(row: Mapping[str, Any]) -> BarWrite:
    """Normalize validated projection values while retaining exact raw-observation lineage."""

    if (row["provider"], row["feed"], row["adjustment"], row["timeframe"]) != (
        "alpaca",
        "IEX",
        "raw",
        "1m",
    ):
        raise ValueError("collector projection has an unsupported series")
    raw_hash = row["raw_payload_sha256"]
    # Old compatibility fixtures may have no provider payload. Their canonical values remain
    # diagnostic and cannot satisfy the strict complete-data policy.
    flags = tuple(
        sorted(
            set(row["quality_flags"])
            | {"complete" if raw_hash is not None else "raw_payload_unavailable"}
        )
    )
    canonical = normalize_alpaca_bar(
        {
            "S": row["symbol"],
            "t": row["bar_timestamp_utc"],
            "o": row["open"],
            "h": row["high"],
            "l": row["low"],
            "c": row["close"],
            "v": row["volume"],
            "n": row["trade_count"],
            "vw": row["vwap"],
        },
        policy=_POLICY,
        receipt_timestamp_utc=row["receipt_timestamp_utc"],
        source_event_id=f"observation_{row['current_observation_id']}",
        quality_flags=flags,
        is_correction=row["is_correction"],
    )
    canonical = replace(
        canonical,
        provider_event_timestamp_utc=row["provider_event_timestamp_utc"],
        payload_hash="",
    )
    return BarWrite(
        identity=BarIdentity(
            provider=canonical.provider,
            feed=canonical.feed,
            adjustment=canonical.adjustment,
            symbol=canonical.symbol,
            timeframe=canonical.timeframe,
            start_at=canonical.interval_start_utc,
            end_at=canonical.interval_end_utc,
        ),
        received_at=canonical.receipt_timestamp_utc,
        provider_timestamp=canonical.provider_event_timestamp_utc,
        open=canonical.open,
        high=canonical.high,
        low=canonical.low,
        close=canonical.close,
        volume=canonical.volume,
        trade_count=canonical.trade_count,
        vwap=canonical.vwap,
        quality_flags=canonical.quality_flags,
        source="collection_projection",
        source_mode=canonical.source_mode,
        source_event_id=canonical.source_event_id,
        source_payload_hash=raw_hash if raw_hash is not None else row["content_hash"],
        is_correction=canonical.is_correction,
    )


def _coverage_sessions(
    advances: Sequence[CoverageAdvance], *, calendar: XnasExchangeCalendar
) -> dict[tuple[str, date], datetime]:
    work: dict[tuple[str, date], datetime] = {}
    for advance in advances:
        if advance.metadata.get("source") == "exchange_calendar_non_trading_interval":
            continue
        if (
            advance.key.provider,
            advance.key.feed,
            advance.key.adjustment,
            advance.key.timeframe,
        ) != ("alpaca", "IEX", "raw", "1m"):
            continue
        if advance.key.symbol not in _POLICY.collection_allowlist:
            raise ValueError("coverage symbol is outside the collection universe")
        raw_end = advance.metadata.get("window_end")
        end = (
            datetime.fromisoformat(raw_end)
            if isinstance(raw_end, str)
            else advance.committed_through_utc
        )
        raw_start = advance.metadata.get("window_start")
        start = (
            datetime.fromisoformat(raw_start)
            if isinstance(raw_start, str)
            else end - timedelta(minutes=1)
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
            raise ValueError("coverage work boundaries must be timezone-aware")
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if start >= end or end - start > timedelta(days=366):
            raise ValueError("coverage work range is invalid or unbounded")
        current = start.date()
        while current <= end.date():
            session = calendar.session(current)
            if session is not None and start < session.close_at and end > session.open_at:
                key = (advance.key.symbol, current)
                through = min(end, session.close_at)
                work[key] = max(work.get(key, through), through)
            current += timedelta(days=1)
    return work


def mirror_current(
    connection: Connection,
    engine: Engine,
    identity_hashes: Sequence[str],
    coverage_advances: Sequence[CoverageAdvance],
    *,
    enqueue_unchanged: bool = False,
) -> None:
    """Atomically mirror winning projections and enqueue idempotent session recomputation.

    The caller must already own the collector lease row lock. Raw history, selected canonical
    changes, work generations, and coverage checkpoints therefore commit or roll back together.
    """

    from adaptive_trader.collection.schema import canonical_work

    if connection.engine is not engine or not connection.in_transaction():
        raise ValueError("canonical mirroring requires the collector's active transaction")
    calendar = XnasExchangeCalendar()
    selected = tuple(sorted(set(identity_hashes)))
    rows = (
        connection.execute(
            select(current_bars, bar_observations.c.raw_payload_sha256)
            .join(
                bar_observations,
                current_bars.c.current_observation_id == bar_observations.c.observation_id,
            )
            .where(current_bars.c.identity_hash.in_(selected))
            .order_by(current_bars.c.identity_hash)
        )
        .mappings()
        .all()
        if selected
        else ()
    )
    if len(rows) != len(selected):
        raise ValueError("a selected collector projection is missing")
    writes = tuple(_canonical_write(dict(row)) for row in rows)
    results = MarketDataRepository(engine).append_selected_batch(writes, connection=connection)
    work = _coverage_sessions(coverage_advances, calendar=calendar)
    for result in results:
        if result.status is BarWriteStatus.DUPLICATE and not enqueue_unchanged:
            continue
        identity = result.event.identity
        session = calendar.session(identity.start_at.date())
        if session is None or not session.open_at <= identity.start_at < session.close_at:
            continue
        key = (identity.symbol, session.session_date)
        work[key] = max(work.get(key, identity.end_at), identity.end_at)
    if not work:
        return
    statement = pg_insert(canonical_work).values(
        [
            {"symbol": symbol, "session_date": session_date, "generation": 1, "through_at": through}
            for (symbol, session_date), through in sorted(work.items())
        ]
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[canonical_work.c.symbol, canonical_work.c.session_date],
            set_={
                "generation": canonical_work.c.generation + 1,
                "through_at": func.greatest(
                    canonical_work.c.through_at, statement.excluded.through_at
                ),
                "updated_at": func.statement_timestamp(),
            },
        )
    )


def rebuild_current_batch(
    connection: Connection,
    engine: Engine,
    *,
    after_identity_hash: str | None = None,
    limit: int = 750,
) -> tuple[int, str | None]:
    """Reindex one bounded keyset page of existing selected raw projections.

    The caller verifies the collector lease in this transaction and commits before advancing
    its cursor. This imports V1 and V2 winners without rewriting raw IDs, raw history, or
    checkpoints. Retrying a page creates no economic revisions; it does enqueue fresh derived
    work even for unchanged canonical rows. A crash can safely restart from the first page.
    The result is the processed row count and last identity, or ``(0, None)`` at exhaustion.
    """

    if connection.engine is not engine or not connection.in_transaction():
        raise ValueError("canonical rebuild requires the collector's active transaction")
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("canonical rebuild page limit must be between 1 and 1000")
    if after_identity_hash is not None and (
        type(after_identity_hash) is not str
        or len(after_identity_hash) != 64
        or any(character not in "0123456789abcdef" for character in after_identity_hash)
    ):
        raise ValueError("canonical rebuild cursor must be a lowercase SHA-256 identity")
    statement = (
        select(current_bars.c.identity_hash).order_by(current_bars.c.identity_hash).limit(limit)
    )
    if after_identity_hash is not None:
        statement = statement.where(current_bars.c.identity_hash > after_identity_hash)
    identities = tuple(connection.execute(statement).scalars())
    if not identities:
        return 0, None
    mirror_current(connection, engine, identities, (), enqueue_unchanged=True)
    return len(identities), identities[-1]


__all__ = ["mirror_current", "rebuild_current_batch"]
