"""Session-aware collector expectations at calendar and delivery-lag boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from adaptive_trader.collection.operations import collection_experiment, expected_completed_bar


@pytest.mark.parametrize(
    ("instant", "expected", "market_closed"),
    [
        ("2026-09-06T15:00:00", "2026-09-04T20:00:00", True),
        ("2026-09-07T15:00:00", "2026-09-04T20:00:00", True),
        ("2026-09-08T13:29:59", "2026-09-04T20:00:00", True),
        ("2026-09-08T13:30:00", "2026-09-04T20:00:00", False),
        ("2026-09-08T13:31:59", "2026-09-04T20:00:00", False),
        ("2026-09-08T13:32:00", "2026-09-08T13:31:00", False),
        ("2026-09-09T03:00:00", "2026-09-08T20:00:00", True),
        ("2026-03-06T14:32:00", "2026-03-06T14:31:00", False),
        ("2026-03-09T13:32:00", "2026-03-09T13:31:00", False),
        ("2026-11-02T14:32:00", "2026-11-02T14:31:00", False),
        ("2026-11-27T17:59:59", "2026-11-27T17:58:00", False),
        ("2026-11-27T18:00:00", "2026-11-27T17:59:00", True),
        ("2026-11-27T18:01:00", "2026-11-27T18:00:00", True),
        ("2026-11-27T20:00:00", "2026-11-27T18:00:00", True),
    ],
)
def test_expected_completed_minute_respects_session_and_delivery_lag(
    instant: str,
    expected: str,
    market_closed: bool,
) -> None:
    result = expected_completed_bar(datetime.fromisoformat(instant).replace(tzinfo=UTC))
    assert result == (datetime.fromisoformat(expected).replace(tzinfo=UTC), market_closed)


def test_freshness_clock_normalizes_an_aware_new_york_instant() -> None:
    instant = datetime(2026, 9, 8, 9, 32, tzinfo=ZoneInfo("America/New_York"))
    assert expected_completed_bar(instant) == (datetime(2026, 9, 8, 13, 31, tzinfo=UTC), False)


def test_freshness_clock_rejects_naive_wall_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        expected_completed_bar(datetime(2026, 9, 8, 13, 32))


def test_collection_experiment_preserves_eleven_research_members_and_eighteen_exclusions() -> None:
    experiment = collection_experiment()
    assert experiment.active_tradable == (
        "AAOI",
        "AMD",
        "AXTI",
        "CSCO",
        "HLIT",
        "INSG",
        "NVDA",
        "SNDK",
    )
    assert set(experiment.collection_allowlist) == {
        "AAOI",
        "AMD",
        "AXTI",
        "CSCO",
        "HLIT",
        "INSG",
        "NVDA",
        "SNDK",
        "SOXX",
        "QQQ",
        "SPY",
    }
    assert len(experiment.excluded) == 18
    assert "TSLA" in experiment.excluded
