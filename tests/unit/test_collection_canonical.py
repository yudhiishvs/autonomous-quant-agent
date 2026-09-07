"""Canonical intake preserves raw lineage, selection order, and economic idempotency."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, func, select

from adaptive_trader.collection.canonical import _canonical_write, _coverage_sessions
from adaptive_trader.collection.repository import CheckpointKey, CoverageAdvance
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.normalization import MarketDataNormalizationError
from adaptive_trader.platform.storage.market_data import (
    BarWriteStatus,
    MarketDataRepository,
    MarketDataValidationError,
)
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, aqa_bar_events, metadata

_START = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)


def _row(*, symbol: str = "AAOI", minute: int = 0) -> dict[str, Any]:
    start = _START + timedelta(minutes=minute)
    return {
        "provider": "alpaca",
        "feed": "IEX",
        "adjustment": "raw",
        "timeframe": "1m",
        "symbol": symbol,
        "bar_timestamp_utc": start,
        "provider_event_timestamp_utc": start,
        "receipt_timestamp_utc": start + timedelta(minutes=2),
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "close": Decimal("101"),
        "volume": 1234,
        "trade_count": 25,
        "vwap": Decimal("100.5"),
        "quality_flags": [],
        "is_correction": False,
        "current_observation_id": "a" * 64,
        "raw_payload_sha256": "b" * 64,
        "content_hash": "c" * 64,
    }


@pytest.fixture
def engine() -> Iterator[Engine]:
    selected = create_engine("sqlite+pysqlite:///:memory:").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )
    metadata.create_all(selected)
    try:
        yield selected
    finally:
        selected.dispose()


def test_every_collection_symbol_maps_without_granting_research_authority() -> None:
    assert len(COLLECTION_UNIVERSE_V1.symbols) == 29
    for symbol in COLLECTION_UNIVERSE_V1.symbols:
        bar = _canonical_write(_row(symbol=symbol))
        assert bar.identity.symbol == symbol
        assert (bar.identity.provider, bar.identity.feed, bar.identity.timeframe) == (
            "alpaca",
            "iex",
            "1Min",
        )
        assert bar.source_event_id == "observation_" + "a" * 64
        assert bar.source_payload_hash == "b" * 64
        assert bar.received_at == _START + timedelta(minutes=2)
        assert bar.provider_timestamp == _START
        assert bar.quality_flags == ("complete",)


def test_missing_raw_evidence_remains_diagnostic_and_incomplete_bar_rejected() -> None:
    row = _row()
    row["raw_payload_sha256"] = None
    assert _canonical_write(row).quality_flags == ("raw_payload_unavailable",)
    row["receipt_timestamp_utc"] = _START + timedelta(seconds=59)
    with pytest.raises(MarketDataNormalizationError, match="precedes interval end"):
        _canonical_write(row)


def test_selected_batch_deduplicates_deliveries_and_preserves_late_receipt(engine: Engine) -> None:
    repository = MarketDataRepository(engine)
    initial = _canonical_write(_row())
    with repository.transaction() as connection:
        first = repository.append_selected_batch((initial,), connection=connection)[0]
    replay = replace(
        initial,
        received_at=initial.received_at + timedelta(seconds=2),
        source_event_id="observation_" + "d" * 64,
        source_payload_hash="e" * 64,
        is_correction=True,
    )
    with repository.transaction() as connection:
        duplicate = repository.append_selected_batch((replay,), connection=connection)[0]
    correction = replace(
        initial,
        close=Decimal("101.5"),
        received_at=initial.received_at - timedelta(seconds=15),
        source_event_id="observation_" + "f" * 64,
    )
    with repository.transaction() as connection:
        corrected = repository.append_selected_batch((correction,), connection=connection)[0]
    with repository.transaction() as connection:
        restored = repository.append_selected_batch((initial,), connection=connection)[0]

    assert first.status is BarWriteStatus.INSERTED
    assert duplicate.status is BarWriteStatus.DUPLICATE
    assert corrected.status is BarWriteStatus.CORRECTED
    assert restored.status is BarWriteStatus.CORRECTED
    history = repository.list_events(initial.identity)
    assert [event.revision for event in history] == [1, 2, 3]
    assert [event.bar.close for event in history] == [
        Decimal("101"),
        Decimal("101.5"),
        Decimal("101"),
    ]
    assert history[1].bar.received_at == _START + timedelta(seconds=105)
    assert repository.latest(initial.identity) == restored.event


def test_ordinary_append_retains_receipt_order_guard(engine: Engine) -> None:
    repository = MarketDataRepository(engine)
    initial = _canonical_write(_row())
    repository.append(initial)
    with pytest.raises(MarketDataValidationError, match="receipt precedes"):
        repository.append(
            replace(initial, close=Decimal("101.5"), received_at=_START + timedelta(seconds=90))
        )
    assert len(repository.list_events(initial.identity)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("open", Decimal("100.1")),
        ("high", Decimal("103")),
        ("low", Decimal("98")),
        ("close", Decimal("101.5")),
        ("volume", Decimal("1300")),
        ("trade_count", 30),
        ("vwap", Decimal("100.6")),
        ("quality_flags", ("complete", "late")),
    ),
)
def test_selected_field_corrections_create_effective_revisions(
    engine: Engine, field: str, value: object
) -> None:
    repository = MarketDataRepository(engine)
    initial = _canonical_write(_row())
    correction = replace(initial, **{field: value})
    with repository.transaction() as connection:
        repository.append_selected_batch((initial,), connection=connection)
    with repository.transaction() as connection:
        changed = repository.append_selected_batch((correction,), connection=connection)[0]
    assert changed.status is BarWriteStatus.CORRECTED
    assert changed.event.revision == 2
    assert getattr(repository.latest(initial.identity).bar, field) == value


def test_selected_batch_rolls_back_and_rejects_unselected_provider_authority(
    engine: Engine,
) -> None:
    repository = MarketDataRepository(engine)
    bars = tuple(_canonical_write(_row(symbol=symbol)) for symbol in ("NVDA", "AMD", "AAOI"))
    with (
        pytest.raises(RuntimeError, match="checkpoint failure"),
        repository.transaction() as connection,
    ):
        results = repository.append_selected_batch(bars, connection=connection)
        assert all(result.status is BarWriteStatus.INSERTED for result in results)
        raise RuntimeError("checkpoint failure")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 0
    with repository.transaction() as connection:
        with pytest.raises(MarketDataValidationError, match="collector boundary"):
            repository.append_selected_batch(
                (replace(bars[0], source="alpaca"),), connection=connection
            )
        with pytest.raises(MarketDataValidationError, match="duplicate bar identities"):
            repository.append_selected_batch((bars[0], bars[0]), connection=connection)


def test_repair_queue_uses_queried_interval_instead_of_later_checkpoint() -> None:
    advance = CoverageAdvance(
        CheckpointKey("rest_coverage", "alpaca", "IEX", "raw", "AAOI", "1m"),
        committed_through_utc=_START + timedelta(days=5),
        metadata={
            "window_start": _START.isoformat(),
            "window_end": (_START + timedelta(minutes=30)).isoformat(),
        },
    )
    work = _coverage_sessions((advance,), calendar=XnasExchangeCalendar())
    assert work == {("AAOI", date(2026, 9, 3)): _START + timedelta(minutes=30)}


def test_repair_queue_preserves_early_close_and_skips_market_closure() -> None:
    start = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    end = datetime(2026, 11, 29, 20, 0, tzinfo=UTC)
    advance = CoverageAdvance(
        CheckpointKey("rest_coverage", "alpaca", "IEX", "raw", "AAOI", "1m"),
        end,
        metadata={"window_start": start.isoformat(), "window_end": end.isoformat()},
    )
    calendar = XnasExchangeCalendar()
    assert _coverage_sessions((advance,), calendar=calendar) == {
        ("AAOI", date(2026, 11, 27)): datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    }
    closed = replace(advance, metadata={"source": "exchange_calendar_non_trading_interval"})
    assert _coverage_sessions((closed,), calendar=calendar) == {}
