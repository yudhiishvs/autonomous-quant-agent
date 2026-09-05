"""Persistence and correction tests for fifteen-minute materialization."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, insert, select

from adaptive_trader.platform.data.aggregation import (
    AggregationError,
    EffectiveBar,
    SessionWindow,
)
from adaptive_trader.platform.data.materialization import FifteenMinuteMaterializer
from adaptive_trader.platform.data.normalization import CanonicalBar
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWriteStatus,
    EligibleWatermark,
    MarketDataRepository,
)
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_experiments,
    metadata,
)

_OPEN = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_CLOSE = datetime(2026, 7, 6, 20, 0, tzinfo=UTC)
_EXPERIMENT_HASH = "a" * 64


def _engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{path}").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    metadata.create_all(engine)
    return engine


def _effective(minute: int, *, revision: int = 1) -> EffectiveBar:
    start = _OPEN + timedelta(minutes=minute)
    return EffectiveBar(
        bar_event_id=f"bar_event_{minute + 1:064x}",
        revision=revision,
        bar=CanonicalBar(
            provider="fixture",
            feed="iex",
            adjustment="raw",
            symbol="NVDA",
            timeframe="1Min",
            source_mode="offline_fixture",
            interval_start_utc=start,
            interval_end_utc=start + timedelta(minutes=1),
            receipt_timestamp_utc=start + timedelta(minutes=1, seconds=30),
            provider_event_timestamp_utc=start,
            open=Decimal(100 + minute),
            high=Decimal(101 + minute),
            low=Decimal(99 + minute),
            close=Decimal("100.5") + minute,
            volume=Decimal(100 + minute),
            trade_count=10 + minute,
            vwap=Decimal("100.25") + minute,
            schema_version=1,
            source_event_id=f"fixture_{minute:02d}",
            quality_flags=("complete",),
            is_correction=False,
            correction_of_source_event_id=None,
        ),
    )


def _constituents() -> tuple[EffectiveBar, ...]:
    return tuple(_effective(minute) for minute in range(15))


def _identity() -> BarIdentity:
    return BarIdentity(
        provider="fixture",
        feed="iex",
        adjustment="raw",
        symbol="NVDA",
        timeframe="15Min",
        start_at=_OPEN,
        end_at=_OPEN + timedelta(minutes=15),
    )


def _session() -> SessionWindow:
    return SessionWindow(session_open_utc=_OPEN, session_close_utc=_CLOSE)


def test_materialization_persists_one_deterministic_aggregate(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "materialization.sqlite3")
    try:
        repository = MarketDataRepository(engine)
        materializer = FifteenMinuteMaterializer.from_repository(repository)

        first = materializer.materialize(_constituents(), session=_session())
        retry = materializer.materialize(tuple(reversed(_constituents())), session=_session())

        assert first.write_result.status is BarWriteStatus.INSERTED
        assert retry.write_result.status is BarWriteStatus.DUPLICATE
        assert retry.aggregate == first.aggregate
        assert retry.write_result.event == first.write_result.event
        assert first.write_result.event.bar.lineage_hash == first.aggregate.lineage_hash
        assert first.write_result.event.bar.source_payload_hash == first.aggregate.result_hash
        assert first.write_result.event.bar.source == "aggregate"
        assert repository.latest(_identity()) == first.write_result.event
        assert len(repository.list_events(_identity())) == 1
    finally:
        engine.dispose()


def test_effective_constituent_revision_appends_aggregate_correction(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "aggregate-correction.sqlite3")
    try:
        repository = MarketDataRepository(engine)
        materializer = FifteenMinuteMaterializer.from_repository(repository)
        original = _constituents()
        corrected = list(original)
        corrected[7] = replace(corrected[7], revision=2)

        first = materializer.materialize(original, session=_session())
        second = materializer.materialize(tuple(corrected), session=_session())

        assert first.write_result.status is BarWriteStatus.INSERTED
        assert second.write_result.status is BarWriteStatus.CORRECTED
        assert second.aggregate.lineage_hash != first.aggregate.lineage_hash
        history = repository.list_events(_identity())
        assert len(history) == 2
        assert history[1].correction_of_event_id == history[0].bar_event_id
        assert tuple(item.bar.lineage_hash for item in history) == (
            first.aggregate.lineage_hash,
            second.aggregate.lineage_hash,
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(aqa_bar_events.c.revision).order_by(aqa_bar_events.c.revision.desc())
                )
                == 2
            )
    finally:
        engine.dispose()


def test_incomplete_bucket_fails_before_persistence(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "incomplete.sqlite3")
    try:
        repository = MarketDataRepository(engine)
        materializer = FifteenMinuteMaterializer.from_repository(repository)

        with pytest.raises(AggregationError, match="exactly 15"):
            materializer.materialize(_constituents()[:-1], session=_session())

        assert repository.latest(_identity()) is None
    finally:
        engine.dispose()


def test_materialization_can_advance_fifteen_minute_watermark(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "watermark.sqlite3")
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(aqa_experiments).values(
                    experiment_hash=_EXPERIMENT_HASH,
                    experiment_id="materialization_test",
                    experiment_version=1,
                    schema_version=1,
                    configuration={},
                    content_hash="b" * 64,
                    registered_at=_OPEN,
                )
            )
        repository = MarketDataRepository(engine)
        materializer = FifteenMinuteMaterializer.from_repository(repository)
        watermark_request = EligibleWatermark(
            experiment_hash=_EXPERIMENT_HASH,
            quality_hash=sha256_hex(("aggregate-quality-v1", "NVDA")),
            updated_at=_OPEN + timedelta(minutes=15, seconds=31),
        )

        receipt = materializer.materialize(
            _constituents(),
            session=_session(),
            eligible_watermark=watermark_request,
        )

        assert receipt.write_result.watermark_changed is True
        assert receipt.write_result.watermark is not None
        assert receipt.write_result.watermark.contiguous_through == _OPEN + timedelta(minutes=15)
        assert (
            repository.watermark(
                experiment_hash=_EXPERIMENT_HASH,
                identity=_identity(),
            )
            == receipt.write_result.watermark
        )
    finally:
        engine.dispose()
