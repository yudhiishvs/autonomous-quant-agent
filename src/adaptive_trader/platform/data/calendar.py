"""Narrow, deterministic exchange-calendar boundary for market-data readiness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import exchange_calendars

from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError

_NEW_YORK = ZoneInfo("America/New_York")
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_TIMEFRAME_DURATIONS = {
    "1Min": timedelta(minutes=1),
    "15Min": timedelta(minutes=15),
}


class CalendarValidationError(DomainValidationError):
    """Raised when a calendar request is malformed or outside supported data."""


class NonTradingSessionError(CalendarValidationError):
    """Raised when a strategy-entry request names a scheduled market closure."""


class UnsupportedEntrySessionError(CalendarValidationError):
    """Raised when new entries are requested for a nonstandard XNAS session."""


@dataclass(frozen=True, order=True, slots=True)
class TradingInterval:
    """One start-inclusive, end-exclusive expected market-data interval."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        try:
            start_at = require_utc_instant(self.start_at, field_name="interval_start")
            end_at = require_utc_instant(self.end_at, field_name="interval_end")
        except DomainValidationError:
            raise CalendarValidationError("trading interval must use UTC instants") from None
        if start_at >= end_at:
            raise CalendarValidationError("trading interval must have positive duration")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)


@dataclass(frozen=True, slots=True)
class TradingSession:
    """One scheduled XNAS session expressed only with stable Python primitives."""

    session_date: date
    open_at: datetime
    close_at: datetime
    is_standard_full_session: bool

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise CalendarValidationError("session date must be a date")
        try:
            open_at = require_utc_instant(self.open_at, field_name="session_open")
            close_at = require_utc_instant(self.close_at, field_name="session_close")
        except DomainValidationError:
            raise CalendarValidationError("session boundaries must use UTC instants") from None
        if open_at >= close_at:
            raise CalendarValidationError("session boundaries are invalid")
        if type(self.is_standard_full_session) is not bool:
            raise CalendarValidationError("session classification is invalid")
        if open_at.astimezone(_NEW_YORK).date() != self.session_date:
            raise CalendarValidationError("session label does not match its market date")
        object.__setattr__(self, "open_at", open_at)
        object.__setattr__(self, "close_at", close_at)

    @property
    def supports_strategy_entries(self) -> bool:
        """Only the configured 09:30-16:00 New York session permits new entries."""

        return self.is_standard_full_session


@runtime_checkable
class ExchangeCalendar(Protocol):
    """Minimal injected calendar contract used by collection and scheduling code."""

    @property
    def name(self) -> str:
        """Return the canonical configured calendar identifier."""

    def session(self, session_date: date) -> TradingSession | None:
        """Return a scheduled session, or ``None`` for a closed market date."""

    def require_entry_session(self, session_date: date) -> TradingSession:
        """Return a full entry-capable session or fail closed."""

    def expected_intervals(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        timeframe: str,
    ) -> tuple[TradingInterval, ...]:
        """Return expected regular-session intervals inside a UTC half-open range."""


class XnasExchangeCalendar:
    """Deterministic adapter around ``exchange-calendars``' XNAS schedule.

    The third-party calendar object and pandas timestamps never escape this class. Consumers see
    immutable UTC ``datetime`` values and therefore cannot accidentally depend on pandas parsing,
    local machine time, or the library's minute-label convention.
    """

    def __init__(self) -> None:
        self._calendar: Any = exchange_calendars.get_calendar("XNAS")

    @property
    def name(self) -> str:
        return "XNAS"

    def session(self, session_date: date) -> TradingSession | None:
        _require_date(session_date)
        try:
            if not bool(self._calendar.is_session(session_date)):
                return None
            open_at = _to_utc_datetime(self._calendar.session_open(session_date))
            close_at = _to_utc_datetime(self._calendar.session_close(session_date))
        except Exception as error:
            raise CalendarValidationError(
                "XNAS session is outside the installed calendar range"
            ) from error
        local_open = open_at.astimezone(_NEW_YORK)
        local_close = close_at.astimezone(_NEW_YORK)
        is_standard = (
            local_open.date() == session_date
            and local_close.date() == session_date
            and local_open.time().replace(tzinfo=None) == _REGULAR_OPEN
            and local_close.time().replace(tzinfo=None) == _REGULAR_CLOSE
        )
        return TradingSession(
            session_date=session_date,
            open_at=open_at,
            close_at=close_at,
            is_standard_full_session=is_standard,
        )

    def require_entry_session(self, session_date: date) -> TradingSession:
        selected = self.session(session_date)
        if selected is None:
            raise NonTradingSessionError("strategy entries require a scheduled XNAS session")
        if not selected.supports_strategy_entries:
            raise UnsupportedEntrySessionError(
                "strategy entries are unsupported for an early-close or nonstandard XNAS session"
            )
        return selected

    def expected_intervals(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        timeframe: str,
    ) -> tuple[TradingInterval, ...]:
        start_at, end_at = _require_utc_range(start_at, end_at)
        duration = _timeframe_duration(timeframe)
        first_date = start_at.astimezone(_NEW_YORK).date() - timedelta(days=1)
        last_date = end_at.astimezone(_NEW_YORK).date() + timedelta(days=1)
        try:
            labels = self._calendar.sessions_in_range(first_date, last_date)
        except Exception as error:
            raise CalendarValidationError(
                "calendar range is outside the installed XNAS schedule"
            ) from error

        intervals: list[TradingInterval] = []
        for raw_label in labels:
            label_value = getattr(raw_label, "date", None)
            if not callable(label_value):
                raise CalendarValidationError("XNAS returned an invalid session label")
            label = label_value()
            if type(label) is not date:
                raise CalendarValidationError("XNAS returned an invalid session label")
            selected = self.session(label)
            if selected is None:  # pragma: no cover - defensive third-party consistency check
                raise CalendarValidationError("XNAS schedule returned a closed session")
            cursor = selected.open_at
            while cursor + duration <= selected.close_at:
                interval_end = cursor + duration
                if cursor >= start_at and interval_end <= end_at:
                    intervals.append(TradingInterval(cursor, interval_end))
                cursor = interval_end
        return tuple(intervals)


def _require_date(value: object) -> date:
    if type(value) is not date:
        raise CalendarValidationError("session date must be a date")
    return value


def _to_utc_datetime(value: object) -> datetime:
    converter = getattr(value, "to_pydatetime", None)
    if not callable(converter):
        raise CalendarValidationError("XNAS returned an invalid schedule timestamp")
    converted = converter()
    if type(converted) is not datetime or converted.tzinfo is None:
        raise CalendarValidationError("XNAS returned an invalid schedule timestamp")
    return converted.astimezone(UTC)


def _require_utc_range(start_at: object, end_at: object) -> tuple[datetime, datetime]:
    try:
        normalized_start = require_utc_instant(start_at, field_name="range_start")
        normalized_end = require_utc_instant(end_at, field_name="range_end")
    except DomainValidationError:
        raise CalendarValidationError("calendar range must use UTC instants") from None
    if normalized_start >= normalized_end:
        raise CalendarValidationError("calendar range must have positive duration")
    if any(
        value.second != 0 or value.microsecond != 0 for value in (normalized_start, normalized_end)
    ):
        raise CalendarValidationError("calendar range boundaries must be minute-aligned")
    return normalized_start, normalized_end


def _timeframe_duration(timeframe: object) -> timedelta:
    if type(timeframe) is not str or timeframe not in _TIMEFRAME_DURATIONS:
        raise CalendarValidationError("calendar timeframe is unsupported")
    return _TIMEFRAME_DURATIONS[timeframe]


__all__ = [
    "CalendarValidationError",
    "ExchangeCalendar",
    "NonTradingSessionError",
    "TradingInterval",
    "TradingSession",
    "UnsupportedEntrySessionError",
    "XnasExchangeCalendar",
]
