"""Read-only safe-view query boundary tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
)

from adaptive_trader.platform.control import ReadResource, SQLAlchemyControlQueryService
from adaptive_trader.platform.control.queries import ControlQueryError
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA


@pytest.fixture
def query_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'control-views.sqlite3'}"
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})
    metadata = MetaData(schema=PLATFORM_SCHEMA)
    gaps = Table(
        "aqa_data_gaps_v",
        metadata,
        Column("gap_id", String, primary_key=True),
        Column("experiment_hash", String),
        Column("provider", String),
        Column("feed", String),
        Column("adjustment", String),
        Column("symbol", String),
        Column("timeframe", String),
        Column("gap_start_at", DateTime(timezone=True)),
        Column("gap_end_at", DateTime(timezone=True)),
        Column("status", String),
        Column("reason_code", String),
        Column("attempt_count", Integer),
        Column("detected_at", DateTime(timezone=True)),
        Column("last_attempt_at", DateTime(timezone=True)),
        Column("resolved_at", DateTime(timezone=True)),
        Column("version", Integer),
    )
    metadata.create_all(engine)
    base = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
    with engine.begin() as connection:
        for index, symbol in enumerate(("NVDA", "AMD", "CSCO")):
            connection.execute(
                insert(gaps).values(
                    gap_id=f"gap-{index}",
                    experiment_hash="a" * 64,
                    provider="fixture",
                    feed="iex",
                    adjustment="raw",
                    symbol=symbol,
                    timeframe="1Min",
                    gap_start_at=base,
                    gap_end_at=base,
                    status="open",
                    reason_code="missing_bar",
                    attempt_count=0,
                    detected_at=base,
                    last_attempt_at=None,
                    resolved_at=None,
                    version=1,
                )
            )
    try:
        yield engine
    finally:
        engine.dispose()


def test_safe_view_queries_are_explicit_deterministic_and_bounded(query_engine: Engine) -> None:
    service = SQLAlchemyControlQueryService(query_engine)
    page = service.page(ReadResource.DATA_GAPS, limit=2, offset=1)

    assert service.ready() is True
    assert page.count == 2
    assert page.limit == 2
    assert page.offset == 1
    assert [item["gap_id"] for item in page.items] == ["gap-2", "gap-0"]
    assert all("payload" not in item for item in page.items)


def test_system_status_discloses_no_connection_details(query_engine: Engine) -> None:
    page = SQLAlchemyControlQueryService(query_engine).page(
        ReadResource.SYSTEM_STATUS,
        limit=50,
        offset=0,
    )
    assert page.items == (
        {
            "service": "control_api",
            "database": "reachable",
            "broker_authority": "none",
        },
    )


def test_unknown_or_missing_safe_view_fails_without_database_details(query_engine: Engine) -> None:
    service = SQLAlchemyControlQueryService(query_engine)
    with pytest.raises(ControlQueryError, match="unavailable") as captured:
        service.page(ReadResource.ORDERS, limit=50, offset=0)
    assert "sqlite" not in str(captured.value).lower()
