"""Deterministic XNAS calendar-boundary tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from adaptive_trader.platform.data.calendar import (
    CalendarValidationError,
    NonTradingSessionError,
    UnsupportedEntrySessionError,
    XnasExchangeCalendar,
)


@pytest.fixture(scope="module")
def calendar() -> XnasExchangeCalendar:
    return XnasExchangeCalendar()


def test_calendar_uses_new_york_dst_without_local_machine_time(
    calendar: XnasExchangeCalendar,
) -> None:
    before = calendar.require_entry_session(date(2024, 3, 8))
    after = calendar.require_entry_session(date(2024, 3, 11))

    assert before.open_at == datetime(2024, 3, 8, 14, 30, tzinfo=UTC)
    assert before.close_at == datetime(2024, 3, 8, 21, 0, tzinfo=UTC)
    assert after.open_at == datetime(2024, 3, 11, 13, 30, tzinfo=UTC)
    assert after.close_at == datetime(2024, 3, 11, 20, 0, tzinfo=UTC)
    assert before.supports_strategy_entries is True
    assert after.supports_strategy_entries is True


def test_calendar_excludes_nights_weekends_holidays_and_scheduled_closures(
    calendar: XnasExchangeCalendar,
) -> None:
    intervals = calendar.expected_intervals(
        start_at=datetime(2024, 7, 3, 0, 0, tzinfo=UTC),
        end_at=datetime(2024, 7, 8, 0, 0, tzinfo=UTC),
        timeframe="1Min",
    )

    assert len(intervals) == 600  # 210-minute July 3 close + 390-minute July 5 session.
    assert intervals[0].start_at == datetime(2024, 7, 3, 13, 30, tzinfo=UTC)
    assert intervals[209].end_at == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)
    assert intervals[210].start_at == datetime(2024, 7, 5, 13, 30, tzinfo=UTC)
    assert all(interval.start_at.date() != date(2024, 7, 4) for interval in intervals)
    assert all(interval.start_at.weekday() < 5 for interval in intervals)


def test_shortened_session_is_collected_but_entries_fail_closed(
    calendar: XnasExchangeCalendar,
) -> None:
    shortened = calendar.session(date(2024, 7, 3))

    assert shortened is not None
    assert shortened.open_at == datetime(2024, 7, 3, 13, 30, tzinfo=UTC)
    assert shortened.close_at == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)
    assert shortened.supports_strategy_entries is False
    with pytest.raises(UnsupportedEntrySessionError, match="unsupported"):
        calendar.require_entry_session(date(2024, 7, 3))

    buckets = calendar.expected_intervals(
        start_at=shortened.open_at,
        end_at=shortened.close_at,
        timeframe="15Min",
    )
    assert len(buckets) == 14
    assert buckets[0].start_at == shortened.open_at
    assert buckets[-1].end_at == shortened.close_at


def test_closed_session_entry_and_invalid_calendar_inputs_fail_closed(
    calendar: XnasExchangeCalendar,
) -> None:
    assert calendar.session(date(2024, 7, 4)) is None
    with pytest.raises(NonTradingSessionError, match="scheduled"):
        calendar.require_entry_session(date(2024, 7, 4))
    with pytest.raises(CalendarValidationError, match="date"):
        calendar.session(datetime(2024, 7, 5, tzinfo=UTC))  # type: ignore[arg-type]
    with pytest.raises(CalendarValidationError, match="UTC"):
        calendar.expected_intervals(
            start_at=datetime(2024, 7, 5, 13, 30),
            end_at=datetime(2024, 7, 5, 13, 31),
            timeframe="1Min",
        )
    with pytest.raises(CalendarValidationError, match="minute-aligned"):
        calendar.expected_intervals(
            start_at=datetime(2024, 7, 5, 13, 30, 1, tzinfo=UTC),
            end_at=datetime(2024, 7, 5, 13, 31, 1, tzinfo=UTC),
            timeframe="1Min",
        )
    with pytest.raises(CalendarValidationError, match="unsupported"):
        calendar.expected_intervals(
            start_at=datetime(2024, 7, 5, 13, 30, tzinfo=UTC),
            end_at=datetime(2024, 7, 5, 13, 31, tzinfo=UTC),
            timeframe="5Min",
        )
