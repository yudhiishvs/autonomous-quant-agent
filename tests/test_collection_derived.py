"""Durable session replay, corrections, exact gaps, and bounded derived work."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Engine,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.collection.derived import DerivedDataProcessor, DerivedProcessingError
from adaptive_trader.collection.schema import SCHEMA_NAME, canonical_work, collector_checkpoints
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition, load_experiment
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.watermarks import (
    STRICT_COMPLETE_QUALITY,
    DataSeries,
    GapDetection,
    GapRepository,
    GapStatus,
    ReadinessValidationError,
    compute_symbol_readiness,
    compute_symbol_readiness_from_batches,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    MarketDataIntegrityError,
    MarketDataRepository,
    MarketDataValidationError,
    read_effective_bars,
)
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_basket_watermarks,
    aqa_experiments,
    aqa_symbol_watermarks,
    metadata,
)

_OPEN = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_MINUTE = timedelta(minutes=1)


class _Clock:
    def __init__(self, now: datetime = _OPEN + timedelta(days=5)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        self.now += timedelta(microseconds=1)
        return self.now


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=Path(__file__).resolve().parents[1] / "configs",
    )


@pytest.fixture
def engine(tmp_path: Path, experiment: ExperimentDefinition) -> Iterator[Engine]:
    selected = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'derived.sqlite3'}"
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None, SCHEMA_NAME: None})

    @event.listens_for(selected, "connect")
    def configure_sqlite(connection: Any, record: object) -> None:
        del record
        connection.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(selected)
    canonical_work.create(selected)
    # This suite exercises the derived reader; the collector checkpoint's complete
    # PostgreSQL schema/JSONB behavior belongs to the guarded integration suite.
    Table(
        collector_checkpoints.name,
        MetaData(),
        *(
            Column(name, String)
            for name in ("checkpoint_name", "provider", "feed", "adjustment", "symbol", "timeframe")
        ),
        Column("committed_through_utc", DateTime(timezone=True)),
    ).create(selected)
    with selected.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=experiment.content_hash,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.experiment_version,
                schema_version=1,
                configuration=json.loads(canonical_json_bytes(experiment.hash_payload())),
                content_hash=experiment.content_hash,
                registered_at=_OPEN,
            )
        )
    try:
        yield selected
    finally:
        selected.dispose()


def _bar(
    minute: int, *, start: datetime = _OPEN, symbol: str = "AMD", close: int = 101
) -> BarWrite:
    timestamp = start + minute * _MINUTE
    return BarWrite(
        identity=BarIdentity(
            "alpaca", "iex", "raw", symbol, "1Min", timestamp, timestamp + _MINUTE
        ),
        received_at=timestamp + _MINUTE + timedelta(seconds=1 if close == 101 else 2),
        provider_timestamp=timestamp,
        open=Decimal(100),
        high=Decimal(max(102, close)),
        low=Decimal(99),
        close=Decimal(close),
        volume=Decimal(100),
        trade_count=10,
        vwap=Decimal(100),
        quality_flags=("complete",),
        source="collection_projection",
        source_mode="external_provider",
        source_event_id=f"minute_{symbol}_{timestamp:%Y%m%d%H%M}_{close}",
        source_payload_hash=sha256_hex((symbol, timestamp, close)),
    )


def _seed(
    engine: Engine,
    minutes: int,
    *,
    missing: tuple[int, ...] = (),
    start: datetime = _OPEN,
    symbol: str = "AMD",
) -> None:
    repository = MarketDataRepository(engine)
    bars = tuple(
        _bar(minute, start=start, symbol=symbol)
        for minute in range(minutes)
        if minute not in missing
    )
    with repository.transaction() as connection:
        repository.append_selected_batch(bars, connection=connection)


def _dirty(
    engine: Engine, *, through: datetime, symbol: str = "AMD", session_start: datetime = _OPEN
) -> None:
    with engine.begin() as connection:
        existing = connection.scalar(
            select(canonical_work.c.generation).where(
                canonical_work.c.symbol == symbol,
                canonical_work.c.session_date == session_start.date(),
            )
        )
        if existing is None:
            connection.execute(
                insert(canonical_work).values(
                    symbol=symbol,
                    session_date=session_start.date(),
                    generation=1,
                    through_at=through,
                    updated_at=_OPEN,
                )
            )
        else:
            connection.execute(
                update(canonical_work)
                .where(
                    canonical_work.c.symbol == symbol,
                    canonical_work.c.session_date == session_start.date(),
                )
                .values(generation=existing + 1, through_at=through)
            )


def _processor(
    engine: Engine, experiment: ExperimentDefinition, *, start: datetime = _OPEN
) -> DerivedDataProcessor:
    return DerivedDataProcessor(engine, experiment, start, _Clock())


def _count(engine: Engine, table: Table = canonical_work) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def _aggregate(engine: Engine, minute: int = 0, *, start: datetime = _OPEN, symbol: str = "AMD"):
    timestamp = start + minute * _MINUTE
    return MarketDataRepository(engine).latest(
        BarIdentity(
            "alpaca", "iex", "raw", symbol, "15Min", timestamp, timestamp + timedelta(minutes=15)
        )
    )


def test_complete_session_is_replayable_after_acknowledgement_crash(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 30)
    _dirty(engine, through=_OPEN + 30 * _MINUTE)
    processor = _processor(engine, experiment)

    def crash(work: object) -> None:
        del work
        raise RuntimeError("injected crash after derived commits")

    monkeypatch.setattr(processor, "_acknowledge", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        processor.drain()
    before = _aggregate(engine)
    assert before is not None and before.bar.volume == Decimal(1500)
    assert _count(engine) == 1
    event_count = _count(engine, aqa_bar_events)
    assert _processor(engine, experiment).drain() == 1
    assert _aggregate(engine) == before
    assert _count(engine, aqa_bar_events) == event_count
    assert _count(engine) == 0


def test_generation_retains_concurrent_correction_then_converges(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15)
    through = _OPEN + 15 * _MINUTE
    _dirty(engine, through=through)
    processor = _processor(engine, experiment)
    acknowledge = processor._acknowledge

    def intake_then_acknowledge(work: Any) -> None:
        MarketDataRepository(engine).append(_bar(14, close=110))
        _dirty(engine, through=through)
        acknowledge(work)

    monkeypatch.setattr(processor, "_acknowledge", intake_then_acknowledge)
    assert processor.drain() == 1
    first = _aggregate(engine)
    assert first is not None and first.bar.close == Decimal(101)
    assert _count(engine) == 1
    restarted = DerivedDataProcessor(engine, experiment, _OPEN, _Clock(_OPEN + timedelta(days=6)))
    assert restarted.drain() == 1
    revised = _aggregate(engine)
    assert revised is not None and revised.bar.close == Decimal(110)
    assert revised.revision == 2
    assert revised.correction_of_event_id == first.bar_event_id
    assert revised.bar.lineage_hash != first.bar.lineage_hash
    assert _count(engine) == 0


def test_interrupted_gap_repair_claim_is_completed_from_verified_minutes(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15, missing=(7,))
    through = _OPEN + 15 * _MINUTE
    processor = _processor(engine, experiment)
    _dirty(engine, through=through)
    processor.drain()
    repository = GapRepository(engine, calendar=XnasExchangeCalendar())
    gap = repository.list_unresolved(
        experiment_hash=experiment.content_hash,
        series=DataSeries("alpaca", "iex", "raw", "AMD", "1Min"),
    )[0]
    MarketDataRepository(engine).append(_bar(7))
    _dirty(engine, through=_OPEN + 8 * _MINUTE)

    def crash(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected crash after gap repair claim")

    monkeypatch.setattr(processor._gaps, "complete_repair", crash)
    with pytest.raises(RuntimeError, match="after gap repair claim"):
        processor.drain()
    claimed = repository.get(gap.gap_id)
    assert claimed is not None and claimed.status is GapStatus.REPAIRING
    assert claimed.attempt_count == 1 and _count(engine) == 1
    restarted = DerivedDataProcessor(engine, experiment, _OPEN, _Clock(_OPEN + timedelta(days=6)))
    assert restarted.drain() == 1
    repaired = repository.get(gap.gap_id)
    assert repaired is not None and repaired.status is GapStatus.RESOLVED
    assert repaired.attempt_count == 1
    assert _aggregate(engine) is not None and _count(engine) == 0


def test_missing_minutes_stay_missing_until_exact_prior_gaps_are_repaired(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 60, missing=(20, 21))
    through = _OPEN + 60 * _MINUTE
    _dirty(engine, through=through)
    processor = _processor(engine, experiment)
    assert processor.drain() == 1
    assert _aggregate(engine, 15) is None
    gaps = GapRepository(engine, calendar=XnasExchangeCalendar())
    minute_series = DataSeries("alpaca", "iex", "raw", "AMD", "1Min")
    original = gaps.list_unresolved(experiment_hash=experiment.content_hash, series=minute_series)
    assert len(original) == 1
    assert (original[0].start_at, original[0].end_at) == (
        _OPEN + 20 * _MINUTE,
        _OPEN + 22 * _MINUTE,
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(aqa_symbol_watermarks.c.contiguous_through).where(
                    aqa_symbol_watermarks.c.symbol == "AMD",
                    aqa_symbol_watermarks.c.timeframe == "15Min",
                )
            )
            == _OPEN + 15 * _MINUTE
        )
    MarketDataRepository(engine).append(_bar(21))
    _dirty(engine, through=_OPEN + 22 * _MINUTE)
    processor.drain()
    assert gaps.get(original[0].gap_id).status is GapStatus.OPEN
    assert _aggregate(engine, 15) is None
    MarketDataRepository(engine).append(_bar(20))
    _dirty(engine, through=_OPEN + 21 * _MINUTE)
    processor.drain()
    assert gaps.get(original[0].gap_id).status is GapStatus.RESOLVED
    assert not gaps.list_unresolved(experiment_hash=experiment.content_hash)
    assert _aggregate(engine, 15) is not None


def test_more_than_1024_minutes_are_not_lost_by_bounded_drains(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    for symbol in ("AAOI", "AMD", "NVDA"):
        _seed(engine, 390, symbol=symbol)
        _dirty(engine, symbol=symbol, through=_OPEN + 390 * _MINUTE)
    processor = _processor(engine, experiment)
    assert processor.drain(limit=2) == 2
    assert _count(engine) == 1
    assert processor.drain(limit=2) == 1
    assert _count(engine) == 0
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(aqa_bar_identities)
                .where(aqa_bar_identities.c.timeframe == "15Min")
            )
            == 78
        )
    assert _aggregate(engine, 375, symbol="NVDA") is not None


def test_early_close_aggregates_actual_session_without_fabricating_closed_minutes(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    start = datetime(2024, 7, 3, 13, 30, tzinfo=UTC)
    # Exercise an installed-calendar early close, including its afternoon closure.
    session = XnasExchangeCalendar().session(start.date())
    assert session is not None and session.close_at == start + 210 * _MINUTE
    _seed(engine, 210, start=start)
    _dirty(engine, through=start + 390 * _MINUTE, session_start=start)
    assert _processor(engine, experiment, start=start).drain() == 1
    assert _aggregate(engine, 195, start=start) is not None
    assert _aggregate(engine, 210, start=start) is None
    assert not GapRepository(engine, calendar=XnasExchangeCalendar()).list_unresolved(
        experiment_hash=experiment.content_hash
    )


def test_collection_only_symbol_never_changes_experiment_readiness(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 15, symbol="TSLA")
    _dirty(engine, symbol="TSLA", through=_OPEN + 15 * _MINUTE)
    assert _processor(engine, experiment).drain() == 1
    assert _aggregate(engine, symbol="TSLA") is None
    assert _count(engine, aqa_symbol_watermarks) == 0
    assert _count(engine, aqa_basket_watermarks) == 0


def test_later_complete_session_cannot_jump_an_earlier_inspected_gap(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    next_open = _OPEN + timedelta(days=1)
    _seed(engine, 15, start=next_open)
    _dirty(engine, through=_OPEN + 390 * _MINUTE)
    _dirty(engine, through=next_open + 15 * _MINUTE, session_start=next_open)
    assert _processor(engine, experiment).drain() == 2
    assert _aggregate(engine, start=next_open) is not None
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(aqa_symbol_watermarks).where(aqa_symbol_watermarks.c.symbol == "AMD")
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    assert all(row["contiguous_through"] == _OPEN for row in rows)
    assert all(row["latest_bar_event_id"] is None for row in rows)
    gaps = GapRepository(engine, calendar=XnasExchangeCalendar()).list_unresolved(
        experiment_hash=experiment.content_hash
    )
    assert len(gaps) == 2
    assert all(gap.start_at == _OPEN and gap.end_at == _OPEN + 390 * _MINUTE for gap in gaps)


def test_old_correction_recomputes_through_later_known_coverage(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    next_open = _OPEN + timedelta(days=1)
    through = next_open + 15 * _MINUTE
    _seed(engine, 390)
    _seed(engine, 15, start=next_open)
    _dirty(engine, through=_OPEN + 390 * _MINUTE)
    _dirty(engine, through=through, session_start=next_open)
    processor = _processor(engine, experiment)
    assert processor.drain() == 2
    MarketDataRepository(engine).append(_bar(14, close=110))
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    assert processor.drain() == 1
    assert _aggregate(engine).revision == 2
    with engine.connect() as connection:
        values = tuple(
            connection.scalars(
                select(aqa_symbol_watermarks.c.contiguous_through).where(
                    aqa_symbol_watermarks.c.symbol == "AMD"
                )
            )
        )
    assert values == (through, through)


def test_rest_inspected_tail_blocks_readiness_beyond_latest_observation(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 15)
    through = _OPEN + 30 * _MINUTE
    # Only REST coverage knows about the empty tail. A replayed older correction
    # can carry an earlier through_at than both the checkpoint and prior gaps.
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    with engine.begin() as connection:
        connection.execute(
            insert(collector_checkpoints).values(
                checkpoint_name="rest_coverage",
                provider="alpaca",
                feed="IEX",
                adjustment="raw",
                symbol="AMD",
                timeframe="1m",
                committed_through_utc=through,
            )
        )
    assert _processor(engine, experiment).drain() == 1
    with engine.connect() as connection:
        values = tuple(
            connection.scalars(
                select(aqa_symbol_watermarks.c.contiguous_through).where(
                    aqa_symbol_watermarks.c.symbol == "AMD"
                )
            )
        )
    assert values == (_OPEN + 15 * _MINUTE,) * 2
    assert _aggregate(engine, 15) is None
    gaps = GapRepository(engine, calendar=XnasExchangeCalendar()).list_unresolved(
        experiment_hash=experiment.content_hash
    )
    assert len(gaps) == 2
    assert all(gap.start_at == _OPEN + 15 * _MINUTE and gap.end_at == through for gap in gaps)


def test_streamed_quality_hash_preserves_existing_canonical_preimage(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 3, missing=(1,))
    processor = _processor(engine, experiment)
    series = DataSeries("alpaca", "iex", "raw", "AMD", "1Min")
    expected = processor._calendar.expected_intervals(
        start_at=_OPEN, end_at=_OPEN + 3 * _MINUTE, timeframe="1Min"
    )
    events = processor._read_intervals(series, expected)
    gap = processor._gaps.record(
        GapDetection(
            experiment.content_hash,
            series,
            _OPEN + _MINUTE,
            _OPEN + 2 * _MINUTE,
            "missing_expected_bar",
            _OPEN + 3 * _MINUTE,
        )
    )
    first, last = events
    components = (
        (
            _OPEN,
            _OPEN + _MINUTE,
            first.bar_event_id,
            1,
            first.normalized_payload_hash,
            ("complete",),
            True,
            (),
        ),
        (_OPEN + _MINUTE, _OPEN + 2 * _MINUTE, None, None, None, None, False, (gap.gap_id,)),
        (
            _OPEN + 2 * _MINUTE,
            _OPEN + 3 * _MINUTE,
            last.bar_event_id,
            1,
            last.normalized_payload_hash,
            ("complete",),
            True,
            (),
        ),
    )
    expected_hash = sha256_hex(
        ("symbol_readiness_v1", series.hash_input, STRICT_COMPLETE_QUALITY.policy_hash, components)
    )
    streamed = compute_symbol_readiness_from_batches(
        series=series,
        expected_intervals=expected,
        effective_event_batches=((first,), (), (last,)),
        unresolved_gaps=(gap,),
    )
    assert streamed.quality_hash == expected_hash
    assert streamed.contiguous_through == _OPEN + _MINUTE
    assert streamed.blocking_gap_ids == (gap.gap_id,)
    assert streamed == compute_symbol_readiness(
        series=series, expected_intervals=expected, effective_events=events, unresolved_gaps=(gap,)
    )
    with pytest.raises(ReadinessValidationError, match="uniquely ordered"):
        compute_symbol_readiness_from_batches(
            series=series,
            expected_intervals=expected,
            effective_event_batches=((first, last), (last,)),
            unresolved_gaps=(gap,),
        )


@pytest.mark.parametrize("changed_timeframe", ("1Min", "15Min"))
def test_snapshot_change_before_either_publication_preserves_work_and_replays(
    engine: Engine,
    experiment: ExperimentDefinition,
    monkeypatch: pytest.MonkeyPatch,
    changed_timeframe: str,
) -> None:
    _seed(engine, 15)
    through = _OPEN + 15 * _MINUTE
    _dirty(engine, through=through)
    processor = _processor(engine, experiment)
    publish = processor._watermarks.publish_symbol_snapshot
    injected = False

    def change_after_snapshot(**kwargs: Any) -> Any:
        nonlocal injected
        if not injected and kwargs["readiness"].series.timeframe == changed_timeframe:
            injected = True
            MarketDataRepository(engine).append(_bar(14, close=110))
            _dirty(engine, through=through)
        return publish(**kwargs)

    monkeypatch.setattr(processor._watermarks, "publish_symbol_snapshot", change_after_snapshot)
    assert processor.drain() == 0
    assert _count(engine) == 1
    assert _count(engine, aqa_symbol_watermarks) == (0 if changed_timeframe == "1Min" else 1)
    assert processor.drain() == 1
    assert _count(engine) == 0
    assert _aggregate(engine).revision == 2
    assert _aggregate(engine).bar.close == Decimal(110)


def test_snapshot_fence_includes_sessions_outside_the_selected_drain_limit(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    next_open = _OPEN + timedelta(days=1)
    _seed(engine, 390)
    _seed(engine, 15, start=next_open)
    _dirty(engine, through=_OPEN + 390 * _MINUTE)
    _dirty(engine, through=next_open + 15 * _MINUTE, session_start=next_open)
    processor = _processor(engine, experiment)
    publish = processor._watermarks.publish_symbol_snapshot
    injected = False

    def change_unselected_session(**kwargs: Any) -> Any:
        nonlocal injected
        if not injected:
            injected = True
            MarketDataRepository(engine).append(_bar(14, start=next_open, close=110))
            _dirty(engine, through=next_open + 15 * _MINUTE, session_start=next_open)
        return publish(**kwargs)

    monkeypatch.setattr(processor._watermarks, "publish_symbol_snapshot", change_unselected_session)
    assert processor.drain(limit=1) == 0
    assert _count(engine) == 2 and _count(engine, aqa_symbol_watermarks) == 0
    assert processor.drain() == 2
    assert _count(engine) == 0
    assert _aggregate(engine, start=next_open).bar.close == Decimal(110)


def test_snapshot_gap_state_change_aborts_publication_without_losing_work(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15, missing=(7,))
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    processor = _processor(engine, experiment)
    publish = processor._watermarks.publish_symbol_snapshot
    injected = False

    def claim_gap_after_snapshot(**kwargs: Any) -> Any:
        nonlocal injected
        if not injected:
            injected = True
            gap = processor._gaps.list_unresolved(
                experiment_hash=experiment.content_hash,
                series=DataSeries("alpaca", "iex", "raw", "AMD", "1Min"),
            )[0]
            processor._gaps.begin_repair(gap.gap_id, attempted_at=processor._now())
        return publish(**kwargs)

    monkeypatch.setattr(processor._watermarks, "publish_symbol_snapshot", claim_gap_after_snapshot)
    assert processor.drain() == 0
    assert _count(engine) == 1 and _count(engine, aqa_symbol_watermarks) == 0
    assert processor.drain() == 1
    assert _aggregate(engine) is None
    assert processor._gaps.list_unresolved(experiment_hash=experiment.content_hash)


def test_snapshot_rejects_changed_registered_configuration(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15)
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    processor = _processor(engine, experiment)
    publish = processor._watermarks.publish_symbol_snapshot

    def change_configuration_after_snapshot(**kwargs: Any) -> Any:
        with engine.begin() as connection:
            connection.execute(update(aqa_experiments).values(configuration={}))
        return publish(**kwargs)

    monkeypatch.setattr(
        processor._watermarks, "publish_symbol_snapshot", change_configuration_after_snapshot
    )
    with pytest.raises(DerivedProcessingError, match="configuration is inconsistent"):
        processor.drain()
    assert _count(engine) == 1 and _count(engine, aqa_symbol_watermarks) == 0


@pytest.mark.parametrize("tamper", ("historical_revision", "latest_projection"))
def test_bulk_read_verifies_full_revision_history_and_current_projection(
    engine: Engine, experiment: ExperimentDefinition, tamper: str
) -> None:
    _seed(engine, 15)
    repository = MarketDataRepository(engine)
    original = repository.latest(_bar(7).identity)
    assert original is not None
    repository.append(_bar(7, close=110))
    with engine.begin() as connection:
        if tamper == "historical_revision":
            connection.execute(
                update(aqa_bar_events)
                .where(aqa_bar_events.c.bar_event_id == original.bar_event_id)
                .values(content_hash="0" * 64)
            )
        else:
            connection.execute(
                update(aqa_bar_latest)
                .where(aqa_bar_latest.c.bar_identity_id == original.identity.bar_identity_id)
                .values(version=99)
            )
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    with pytest.raises((MarketDataIntegrityError, DerivedProcessingError)):
        _processor(engine, experiment).drain()
    assert _count(engine) == 1 and _count(engine, aqa_symbol_watermarks) == 0


def test_shared_bulk_reader_preserves_requested_order_and_fails_on_revision_overflow(
    engine: Engine,
) -> None:
    _seed(engine, 3, missing=(1,))
    repository = MarketDataRepository(engine)
    repository.append(_bar(0, close=110))
    identities = tuple(_bar(minute).identity for minute in (2, 1, 0))
    with engine.connect() as connection:
        selected = read_effective_bars(connection, identities)
        assert tuple(item.identity for item in selected) == (identities[0], identities[2])
        assert tuple(item.revision for item in selected) == (1, 2)
        with pytest.raises(MarketDataIntegrityError, match="bounded verification"):
            read_effective_bars(connection, identities, max_revisions=2)
        with pytest.raises(MarketDataValidationError, match="duplicate identities"):
            read_effective_bars(connection, (identities[0], identities[0]))
        assert read_effective_bars(connection, ()) == ()


def test_ten_session_history_uses_bounded_reads_and_one_readiness_scan(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    calendar = XnasExchangeCalendar()
    sessions = []
    day = _OPEN.date()
    while len(sessions) < 10:
        session = calendar.session(day)
        if session is not None:
            sessions.append(session)
        day += timedelta(days=1)
    for session in sessions:
        assert session.close_at - session.open_at == 390 * _MINUTE
        _seed(engine, 390, start=session.open_at)
        _dirty(engine, through=session.close_at, session_start=session.open_at)
    processor = DerivedDataProcessor(
        engine, experiment, _OPEN, _Clock(sessions[-1].close_at + timedelta(days=1))
    )
    queries = 0
    transaction_starts: dict[int, float] = {}
    durations: list[float] = []

    @event.listens_for(engine, "before_cursor_execute")
    def count_queries(*args: Any) -> None:
        nonlocal queries
        queries += 1

    @event.listens_for(engine, "begin")
    def start_transaction(connection: Connection) -> None:
        transaction_starts[id(connection)] = time.perf_counter()

    def finish_transaction(connection: Connection) -> None:
        started = transaction_starts.pop(id(connection), None)
        if started is not None:
            durations.append(time.perf_counter() - started)

    event.listen(engine, "commit", finish_transaction)
    event.listen(engine, "rollback", finish_transaction)
    refresh = processor._refresh_readiness
    readiness_queries: list[int] = []

    def measured_refresh(work: Any) -> None:
        initial = queries
        refresh(work)
        readiness_queries.append(queries - initial)

    monkeypatch.setattr(processor, "_refresh_readiness", measured_refresh)
    started = time.perf_counter()
    assert processor.drain() == 10
    elapsed = time.perf_counter() - started
    assert len(readiness_queries) == 1
    # This budgets SQL statements rather than machine speed. The prior path did
    # several per-minute round trips for every queued session (>80,000 queries).
    assert readiness_queries[0] < 250
    assert durations and max(durations) < 30
    assert _count(engine) == 0
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(aqa_bar_identities)
                .where(aqa_bar_identities.c.timeframe == "15Min")
            )
            == 260
        )
        assert (
            tuple(connection.scalars(select(aqa_symbol_watermarks.c.contiguous_through)))
            == (sessions[-1].close_at,) * 2
        )
    assert not processor._gaps.list_unresolved(experiment_hash=experiment.content_hash)
    print(
        json.dumps(
            {
                "benchmark": "derived_ten_session_offline",
                "sessions": 10,
                "source_bars": 3900,
                "aggregate_bars": 260,
                "readiness_queries": readiness_queries[0],
                "drain_seconds": round(elapsed, 3),
                "max_transaction_seconds": round(max(durations), 3),
            },
            sort_keys=True,
        )
    )


def test_authoritative_earlier_receipt_correction_preserves_aggregate_lineage(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 14)
    repository = MarketDataRepository(engine)
    repository.append(replace(_bar(14), received_at=_OPEN + timedelta(hours=2)))
    through = _OPEN + 15 * _MINUTE
    _dirty(engine, through=through)
    processor = _processor(engine, experiment)
    processor.drain()
    before = _aggregate(engine)
    with repository.transaction() as connection:
        repository.append_selected_batch((_bar(14, close=110),), connection=connection)
    _dirty(engine, through=through)
    processor.drain()
    after = _aggregate(engine)
    assert before is not None and after is not None
    assert after.revision == 2 and after.bar.close == Decimal(110)
    assert after.bar.received_at < before.bar.received_at
    assert after.bar.lineage_hash != before.bar.lineage_hash
    assert after.correction_of_event_id == before.bar_event_id
    assert _count(engine) == 0


def test_missing_source_identity_retains_work_without_aggregate(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 14)
    MarketDataRepository(engine).append(replace(_bar(14), source_event_id=None))
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    with pytest.raises(DerivedProcessingError, match="source identity"):
        _processor(engine, experiment).drain()
    assert _count(engine) == 1
    assert _aggregate(engine) is None


def test_lost_lease_stops_before_the_next_aggregate_and_keeps_work(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 30)
    _dirty(engine, through=_OPEN + 30 * _MINUTE)
    lease_current = True

    def validate_lease() -> None:
        if not lease_current:
            raise DerivedProcessingError("collector lease is no longer current")

    processor = DerivedDataProcessor(
        engine, experiment, _OPEN, _Clock(), lease_validator=validate_lease
    )
    materialize = processor._materializer.materialize

    def lose_lease_after_first_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal lease_current
        result = materialize(*args, **kwargs)
        lease_current = False
        return result

    monkeypatch.setattr(processor._materializer, "materialize", lose_lease_after_first_write)
    with pytest.raises(DerivedProcessingError, match="no longer current"):
        processor.drain()
    assert _aggregate(engine) is not None
    assert _aggregate(engine, 15) is None
    assert _count(engine) == 1
    with pytest.raises(DerivedProcessingError, match="no longer current"):
        processor.drain()


def test_stopped_collector_cannot_acknowledge_completed_derived_work(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15)
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    stopped = False

    def validate_lease() -> None:
        if stopped:
            raise DerivedProcessingError("collector stopped")

    processor = DerivedDataProcessor(
        engine, experiment, _OPEN, _Clock(), lease_validator=validate_lease
    )
    acknowledge = processor._acknowledge

    def stop_before_acknowledgement(work: Any) -> None:
        nonlocal stopped
        stopped = True
        acknowledge(work)

    monkeypatch.setattr(processor, "_acknowledge", stop_before_acknowledgement)
    with pytest.raises(DerivedProcessingError, match="collector stopped"):
        processor.drain()
    assert _aggregate(engine) is not None and _count(engine) == 1


def test_same_processor_drain_is_nonblocking_when_already_running(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    processor = _processor(engine, experiment)
    with processor._local_drain_lock:
        assert processor.drain() == 0
    assert _count(engine) == 1


def test_backfill_zero_progress_with_pending_work_cannot_report_completion(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    checked: list[Connection] = []

    def transaction_guard(connection: Connection) -> None:
        assert connection.engine is engine
        assert connection.in_transaction()
        assert connection.scalar(select(42)) == 42
        checked.append(connection)

    processor = DerivedDataProcessor(
        engine, experiment, _OPEN, _Clock(), transaction_guard=transaction_guard
    )
    # Exercise advisory/instance contention without entering a derived transaction:
    # the finite completion check must fence the actual parent-engine transaction.
    with (
        processor._local_drain_lock,
        pytest.raises(DerivedProcessingError, match="pending derived work"),
    ):
        processor.drain_backfill()
    assert checked and _count(engine) == 1


def test_backfill_invalidated_snapshot_fails_then_replay_finishes_empty(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15)
    through = _OPEN + 15 * _MINUTE
    _dirty(engine, through=through)
    processor = _processor(engine, experiment)
    publish = processor._watermarks.publish_symbol_snapshot
    injected = False

    def invalidate_snapshot_once(**kwargs: Any) -> Any:
        nonlocal injected
        if not injected:
            injected = True
            MarketDataRepository(engine).append(_bar(14, close=110))
            _dirty(engine, through=through)
        return publish(**kwargs)

    monkeypatch.setattr(processor._watermarks, "publish_symbol_snapshot", invalidate_snapshot_once)
    with pytest.raises(DerivedProcessingError, match="pending derived work"):
        processor.drain_backfill()
    assert _count(engine) == 1
    assert processor.drain_backfill() == 1
    assert processor.drain_backfill() == 0
    assert _count(engine) == 0 and _aggregate(engine).bar.close == Decimal(110)


def test_transaction_fence_uses_actual_derived_connection_without_touching_parent_engine(
    engine: Engine, experiment: ExperimentDefinition
) -> None:
    _seed(engine, 15)
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    checked_connections: list[Connection] = []

    def transaction_guard(connection: Connection) -> None:
        # The production callback SELECTs the fenced collector lease FOR UPDATE.
        # This offline boundary verifies executable SQL uses the actual connection.
        assert connection.engine is processor._engine
        assert connection.scalar(select(42)) == 42
        checked_connections.append(connection)

    processor = DerivedDataProcessor(
        engine, experiment, _OPEN, _Clock(), transaction_guard=transaction_guard
    )
    assert processor._engine is not engine
    assert processor.drain() == 1
    assert checked_connections
    checked = len(checked_connections)
    with engine.begin() as connection:
        assert connection.scalar(select(43)) == 43
    assert len(checked_connections) == checked
    assert _count(engine) == 0


def test_transaction_fence_blocks_ack_even_if_phase_lease_check_passed(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(engine, 15)
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    taken_over = False

    def transaction_guard(connection: Connection) -> None:
        del connection
        if taken_over:
            raise DerivedProcessingError("transaction lease fence changed")

    processor = DerivedDataProcessor(
        engine, experiment, _OPEN, _Clock(), transaction_guard=transaction_guard
    )
    acknowledge = processor._acknowledge

    def takeover_before_acknowledgement(work: Any) -> None:
        nonlocal taken_over
        taken_over = True
        acknowledge(work)

    monkeypatch.setattr(processor, "_acknowledge", takeover_before_acknowledgement)
    with pytest.raises(DerivedProcessingError, match="transaction lease fence changed"):
        processor.drain()
    assert _aggregate(engine) is not None and _count(engine) == 1


class _LockConnection:
    """Only the PG lock transport is replaced in lifecycle-specific unit tests."""

    def __init__(self, *, available: bool = True, lose_acquisition: bool = False) -> None:
        self.available = available
        self.lose_acquisition = lose_acquisition
        self.backend_pid = 1234
        self.acquired_backend_pid: int | None = None
        self.invalidated = False
        self.closed = False
        self.autocommit = False
        self.unlock_calls = 0

    def execution_options(self, *, isolation_level: str) -> _LockConnection:
        self.autocommit = isolation_level == "AUTOCOMMIT"
        return self

    def __enter__(self) -> _LockConnection:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.closed = True

    def scalar(self, statement: Any) -> object:
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            if self.lose_acquisition:
                raise SQLAlchemyError("injected uncertain lock acquisition")
            if self.available:
                self.acquired_backend_pid = self.backend_pid
            return self.available
        if "pg_backend_pid" in sql:
            return self.backend_pid
        if "pg_advisory_unlock" in sql:
            self.unlock_calls += 1
            return self.backend_pid == self.acquired_backend_pid
        raise AssertionError("unexpected lock transport operation")

    def invalidate(self) -> None:
        self.invalidated = True


def _inject_lock_transport(
    processor: DerivedDataProcessor, connection: _LockConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        processor,
        "_engine",
        SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=lambda: connection,
        ),
    )


def test_pg_lock_uses_autocommit_and_releases_on_phase_failure(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = _processor(engine, experiment)
    transport = _LockConnection()
    _inject_lock_transport(processor, transport, monkeypatch)
    with (
        pytest.raises(RuntimeError, match="phase failure"),
        processor._exclusive_drain() as acquired,
    ):
        assert acquired and transport.autocommit
        processor._guard()
        raise RuntimeError("phase failure")
    assert transport.unlock_calls == 1 and transport.closed
    assert not transport.invalidated
    assert processor._drain_connection is None


def test_pg_held_lock_skips_work_without_mutation(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    processor = _processor(engine, experiment)
    transport = _LockConnection(available=False)
    _inject_lock_transport(processor, transport, monkeypatch)
    assert processor.drain() == 0
    assert transport.unlock_calls == 0 and transport.closed
    assert _count(engine) == 1


def test_pg_backend_change_stops_and_discards_uncertain_lock_connection(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = _processor(engine, experiment)
    transport = _LockConnection()
    _inject_lock_transport(processor, transport, monkeypatch)
    with (
        pytest.raises(DerivedProcessingError, match="backend changed"),
        processor._exclusive_drain() as acquired,
    ):
        assert acquired
        transport.backend_pid += 1
        processor._guard()
    assert transport.invalidated and transport.closed


def test_uncertain_pg_lock_acquisition_discards_the_connection(
    engine: Engine, experiment: ExperimentDefinition, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = _processor(engine, experiment)
    transport = _LockConnection(lose_acquisition=True)
    _inject_lock_transport(processor, transport, monkeypatch)
    with pytest.raises(DerivedProcessingError, match="database operation failed"):
        processor.drain()
    assert transport.invalidated and transport.closed


@pytest.mark.parametrize("limit", [0, -1, True, 1025, 1.5])
def test_invalid_drain_bound_leaves_queue_untouched(
    engine: Engine, experiment: ExperimentDefinition, limit: Any
) -> None:
    _dirty(engine, through=_OPEN + 15 * _MINUTE)
    with pytest.raises(ValueError, match="limit"):
        _processor(engine, experiment).drain(limit=limit)
    assert _count(engine) == 1
