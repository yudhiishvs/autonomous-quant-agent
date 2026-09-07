"""Runtime environment parsing for the standalone market-data process."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from adaptive_trader.collection.postgres import normalize_postgres_url
from adaptive_trader.platform.config import _FORBIDDEN_ALPACA_VARIABLES
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file

MARKET_DATA_DATABASE_URL_ENV = "APA_MARKET_DATA_DATABASE_URL"
MARKET_DATA_MIGRATION_DATABASE_URL_ENV = "APA_MARKET_DATA_MIGRATION_DATABASE_URL"
MARKET_DATA_HISTORY_START_ENV = "APA_MARKET_DATA_HISTORY_START"
CANONICAL_HISTORY_START_ENV = "AQA_MARKET_DATA_HISTORY_START"
MARKET_DATA_DATABASE_URL_FILE_ENV = SecretFileVariable.DATABASE_URL.value


def require_file_backed_data_environment(environment: Mapping[str, str] | None = None) -> None:
    """Validate the new one-shot process authority before opening any secret or connection.

    Compatibility commands retain their legacy inputs. Scheduled collection accepts only the
    three data-service secret references and its immutable history setting.
    """

    values = os.environ if environment is None else environment
    required = {
        SecretFileVariable.DATABASE_URL.value,
        SecretFileVariable.ALPACA_DATA_API_KEY.value,
        SecretFileVariable.ALPACA_DATA_SECRET_KEY.value,
    }
    allowed = required | {
        CANONICAL_HISTORY_START_ENV,
        "AQA_ENABLE_PAPER_ORDERS",
        "APA_ENABLE_PAPER_ORDERS",
    }
    if any(
        value.strip()
        and (name.startswith(("AQA_", "APA_")) or name in _FORBIDDEN_ALPACA_VARIABLES)
        and name not in allowed
        for name, value in values.items()
    ):
        raise ValueError("collect-once received unsupported configuration or credential authority")
    if any(
        values.get(name, "NO") != "NO"
        for name in ("AQA_ENABLE_PAPER_ORDERS", "APA_ENABLE_PAPER_ORDERS")
    ):
        raise ValueError("collect-once requires paper submission to be disabled")
    if any(not values.get(name, "").strip() for name in required):
        raise ValueError("collect-once requires all three data-service secret file references")


def parse_utc_boundary(value: str, *, field_name: str) -> datetime:
    """Parse an ISO date or timezone-aware timestamp into UTC."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    try:
        if "T" not in normalized and " " not in normalized:
            return datetime.combine(date.fromisoformat(normalized), time.min, tzinfo=UTC)
        if normalized.endswith(("Z", "z")):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 date or timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class CollectorEnvironment:
    """Process configuration containing a private database URL."""

    database_url: str
    history_start: datetime | None = None

    def __post_init__(self) -> None:
        normalized_url = normalize_postgres_url(self.database_url).render_as_string(
            hide_password=False
        )
        object.__setattr__(self, "database_url", normalized_url)
        if self.history_start is not None:
            if self.history_start.tzinfo is None or self.history_start.utcoffset() is None:
                raise ValueError("history_start must be timezone-aware")
            object.__setattr__(self, "history_start", self.history_start.astimezone(UTC))

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> CollectorEnvironment:
        values = os.environ if environment is None else environment
        legacy_database_url = values.get(MARKET_DATA_DATABASE_URL_ENV, "").strip()
        database_url_file = values.get(MARKET_DATA_DATABASE_URL_FILE_ENV, "").strip()
        if legacy_database_url and database_url_file:
            raise ValueError("market-data database credential sources are ambiguous")
        database_url = (
            load_secret_file(
                Path(database_url_file),
                source=SecretFileVariable.DATABASE_URL,
            ).reveal()
            if database_url_file
            else legacy_database_url
        )
        if not database_url:
            raise ValueError(
                f"{MARKET_DATA_DATABASE_URL_FILE_ENV} or {MARKET_DATA_DATABASE_URL_ENV} must be set"
            )
        legacy_start = values.get(MARKET_DATA_HISTORY_START_ENV, "").strip()
        canonical_start = values.get(CANONICAL_HISTORY_START_ENV, "").strip()
        if legacy_start and canonical_start:
            raise ValueError("market-data history-start sources are ambiguous")
        raw_start = canonical_start or legacy_start
        history_start = (
            None
            if not raw_start
            else parse_utc_boundary(raw_start, field_name=MARKET_DATA_HISTORY_START_ENV)
        )
        return cls(database_url=database_url, history_start=history_start)

    def __repr__(self) -> str:
        return (
            f"CollectorEnvironment(database_url=<redacted>, history_start={self.history_start!r})"
        )

    __str__ = __repr__


def migration_database_url_from_environment(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Load the separate schema-owner URL used only by migration commands."""

    values = os.environ if environment is None else environment
    database_url = values.get(MARKET_DATA_MIGRATION_DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise ValueError(f"{MARKET_DATA_MIGRATION_DATABASE_URL_ENV} must be set")
    return normalize_postgres_url(database_url).render_as_string(hide_password=False)
