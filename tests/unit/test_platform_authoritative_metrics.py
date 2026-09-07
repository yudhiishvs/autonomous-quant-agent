"""Durable metric scrapes must survive restarts and distinguish failure from zero."""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

from adaptive_trader.platform.observability.metrics import PlatformMetrics
from adaptive_trader.platform.observability.operational import (
    OperationalMetricsReadError,
    SQLAlchemyOperationalMetricsReader,
)
from adaptive_trader.platform.storage.market_data import MarketDataRepository
from adaptive_trader.platform.storage.tables import aqa_bar_events
from tests.unit.test_platform_market_data_repository import _bar, _sqlite_engine


def test_scrapes_requery_durable_corrections_and_survive_reader_restart(tmp_path: Path) -> None:
    engine = _sqlite_engine(tmp_path / "metrics.sqlite3")
    try:
        reader = SQLAlchemyOperationalMetricsReader(engine)
        metrics = PlatformMetrics(authoritative_reader=reader)
        first = metrics.render().decode()
        assert "aqa_authoritative_metrics_up 1.0" in first
        assert "aqa_persisted_bar_events_total 0.0" in first

        repository = MarketDataRepository(engine)
        repository.append(_bar())
        repository.append(_bar(close=Decimal("10.30"), received_offset=2))
        snapshot = reader.read()
        assert (snapshot.bar_events, snapshot.bar_corrections) == (2, 1)
        assert snapshot.orders[-1] == ("intent_only", 0)
        assert dict(snapshot.active_latches)["operator_halt"] == 0
        after = metrics.render()
        assert b"aqa_persisted_bar_events_total 2.0" in after
        assert b"aqa_persisted_bar_corrections_total 1.0" in after
        restarted = SQLAlchemyOperationalMetricsReader(engine)
        assert restarted.read() == snapshot
    finally:
        engine.dispose()


def test_unavailable_scrape_does_not_publish_false_zero_state(tmp_path: Path) -> None:
    engine = _sqlite_engine(tmp_path / "unavailable.sqlite3")
    try:
        reader = SQLAlchemyOperationalMetricsReader(engine)
        metrics = PlatformMetrics(authoritative_reader=reader)
        aqa_bar_events.drop(engine)
        with pytest.raises(OperationalMetricsReadError) as captured:
            reader.read()
        assert str(captured.value) == "authoritative operational metrics are unavailable"
        rendered = metrics.render()
        assert b"aqa_authoritative_metrics_up 0.0" in rendered
        assert b"aqa_persisted_bar_events_total" not in rendered
        assert b"SELECT" not in rendered
        assert b"sqlite3" not in rendered
    finally:
        engine.dispose()


def test_metric_registration_performs_no_database_io(tmp_path: Path) -> None:
    engine = _sqlite_engine(tmp_path / "registration.sqlite3")
    queries: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    try:
        event.listen(engine, "before_cursor_execute", record)
        metrics = PlatformMetrics(authoritative_reader=SQLAlchemyOperationalMetricsReader(engine))
        assert queries == []
        assert b"aqa_authoritative_metrics_up 1.0" in metrics.render()
        assert queries
    finally:
        engine.dispose()
