"""Immutable, broker-free market-bar and raw-observation contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from numbers import Integral
from typing import Any

_SUPPORTED_FEEDS = frozenset({"IEX", "SIP", "REPLAY", "SYNTHETIC"})
_SUPPORTED_ADJUSTMENTS = frozenset({"raw", "split", "all"})
_SUPPORTED_TIMEFRAMES = frozenset({"1m", "15m", "1d"})


def _nonempty_text(value: str, *, field_name: str, casing: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    normalized = value.strip()
    if casing == "lower":
        return normalized.lower()
    if casing == "upper":
        return normalized.upper()
    return normalized


def _symbol(value: str) -> str:
    normalized = _nonempty_text(value, field_name="MarketBarV1.symbol", casing="upper")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for character in normalized):
        raise ValueError(f"Unsupported symbol format: {value!r}")
    return normalized


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _decimal(value: Any, *, field_name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def _nonnegative_integer(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{field_name} must be a nonnegative integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketBarV1:
    """Canonical current-value projection for one versioned OHLCV interval."""

    provider: str
    feed: str
    adjustment: str
    symbol: str
    timeframe: str
    bar_timestamp_utc: datetime
    receipt_timestamp_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int | None = None
    vwap: Decimal | None = None
    provider_event_timestamp_utc: datetime | None = None
    quality_flags: frozenset[str] = frozenset()
    source: str = "unknown"

    SCHEMA_VERSION = "market-bar.v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _nonempty_text(self.provider, field_name="MarketBarV1.provider", casing="lower"),
        )
        feed = _nonempty_text(self.feed, field_name="MarketBarV1.feed", casing="upper")
        if feed not in _SUPPORTED_FEEDS:
            raise ValueError(f"Unsupported MarketBarV1.feed: {self.feed!r}")
        object.__setattr__(self, "feed", feed)
        adjustment = _nonempty_text(
            self.adjustment,
            field_name="MarketBarV1.adjustment",
            casing="lower",
        )
        if adjustment not in _SUPPORTED_ADJUSTMENTS:
            raise ValueError(f"Unsupported MarketBarV1.adjustment: {self.adjustment!r}")
        object.__setattr__(self, "adjustment", adjustment)
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        timeframe = _nonempty_text(
            self.timeframe,
            field_name="MarketBarV1.timeframe",
            casing="lower",
        )
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported MarketBarV1.timeframe: {self.timeframe!r}")
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(
            self,
            "bar_timestamp_utc",
            _utc(self.bar_timestamp_utc, field_name="MarketBarV1.bar_timestamp_utc"),
        )
        object.__setattr__(
            self,
            "receipt_timestamp_utc",
            _utc(self.receipt_timestamp_utc, field_name="MarketBarV1.receipt_timestamp_utc"),
        )
        if self.receipt_timestamp_utc < self.bar_timestamp_utc:
            raise ValueError("MarketBarV1.receipt_timestamp_utc cannot precede the bar timestamp")
        if self.provider_event_timestamp_utc is not None:
            object.__setattr__(
                self,
                "provider_event_timestamp_utc",
                _utc(
                    self.provider_event_timestamp_utc,
                    field_name="MarketBarV1.provider_event_timestamp_utc",
                ),
            )
        for field_name in ("open", "high", "low", "close"):
            object.__setattr__(
                self,
                field_name,
                _decimal(
                    getattr(self, field_name),
                    field_name=f"MarketBarV1.{field_name}",
                    positive=True,
                ),
            )
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close, self.high
        ):
            raise ValueError("MarketBarV1 OHLC values are internally inconsistent")
        object.__setattr__(
            self,
            "volume",
            _nonnegative_integer(self.volume, field_name="MarketBarV1.volume"),
        )
        if self.trade_count is not None:
            object.__setattr__(
                self,
                "trade_count",
                _nonnegative_integer(
                    self.trade_count,
                    field_name="MarketBarV1.trade_count",
                ),
            )
        if self.vwap is not None:
            object.__setattr__(
                self,
                "vwap",
                _decimal(self.vwap, field_name="MarketBarV1.vwap", positive=True),
            )
        if isinstance(self.quality_flags, (str, bytes)):
            raise ValueError("MarketBarV1.quality_flags must be a collection of strings")
        flags: set[str] = set()
        for flag in self.quality_flags:
            flags.add(
                _nonempty_text(
                    flag,
                    field_name="MarketBarV1.quality_flags item",
                    casing="lower",
                )
            )
        object.__setattr__(self, "quality_flags", frozenset(flags))
        object.__setattr__(
            self,
            "source",
            _nonempty_text(self.source, field_name="MarketBarV1.source", casing="lower"),
        )

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, datetime]:
        """Return the canonical fields that identify one logical bar."""

        return (
            self.provider,
            self.feed,
            self.adjustment,
            self.symbol,
            self.timeframe,
            self.bar_timestamp_utc,
        )

    @property
    def identity_hash(self) -> str:
        """Return the SHA-256 identity used to group duplicates and corrections."""

        return _sha256(
            {
                "adjustment": self.adjustment,
                "bar_timestamp_utc": _timestamp_text(self.bar_timestamp_utc),
                "feed": self.feed,
                "provider": self.provider,
                "schema_version": self.SCHEMA_VERSION,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
            }
        )

    @property
    def content_hash(self) -> str:
        """Return the stable digest used to distinguish duplicates from corrections.

        Receipt time, source, and derived quality flags are excluded so that the same
        provider event received through a retry remains an exact content duplicate.
        """

        return _sha256(
            {
                "close": _decimal_text(self.close),
                "high": _decimal_text(self.high),
                "identity_hash": self.identity_hash,
                "low": _decimal_text(self.low),
                "open": _decimal_text(self.open),
                "provider_event_timestamp_utc": _timestamp_text(self.provider_event_timestamp_utc),
                "trade_count": self.trade_count,
                "volume": self.volume,
                "vwap": None if self.vwap is None else _decimal_text(self.vwap),
            }
        )


@dataclass(frozen=True, slots=True)
class RawBarObservationV1:
    """Append-only provenance envelope for one received canonical bar payload."""

    bar: MarketBarV1
    is_correction: bool = False
    provider_event_id: str | None = None
    raw_payload_sha256: str | None = None
    raw_payload_json: str | None = None

    SCHEMA_VERSION = "raw-bar-observation.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.bar, MarketBarV1):
            raise ValueError("RawBarObservationV1.bar must be a MarketBarV1")
        if not isinstance(self.is_correction, bool):
            raise ValueError("RawBarObservationV1.is_correction must be boolean")
        if self.provider_event_id is not None:
            object.__setattr__(
                self,
                "provider_event_id",
                _nonempty_text(
                    self.provider_event_id,
                    field_name="RawBarObservationV1.provider_event_id",
                ),
            )
        if self.raw_payload_json is not None:
            raw_payload_json = _nonempty_text(
                self.raw_payload_json,
                field_name="RawBarObservationV1.raw_payload_json",
            )
            try:
                raw_payload = json.loads(raw_payload_json)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "RawBarObservationV1.raw_payload_json must contain valid JSON"
                ) from exc
            if not isinstance(raw_payload, dict):
                raise ValueError("RawBarObservationV1.raw_payload_json must contain a JSON object")
            canonical_payload = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"))
            object.__setattr__(self, "raw_payload_json", canonical_payload)
            computed_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
            if self.raw_payload_sha256 is None:
                object.__setattr__(self, "raw_payload_sha256", computed_digest)
            elif self.raw_payload_sha256.lower() != computed_digest:
                raise ValueError(
                    "RawBarObservationV1.raw_payload_sha256 does not match raw_payload_json"
                )
        if self.raw_payload_sha256 is not None:
            digest = _nonempty_text(
                self.raw_payload_sha256,
                field_name="RawBarObservationV1.raw_payload_sha256",
                casing="lower",
            )
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(
                    "RawBarObservationV1.raw_payload_sha256 must be a SHA-256 hex digest"
                )
            object.__setattr__(self, "raw_payload_sha256", digest)

    @property
    def observation_id(self) -> str:
        """Return a retry-stable identifier for idempotent raw-observation writes."""

        return _sha256(
            {
                "bar_content_hash": self.bar.content_hash,
                "bar_identity_hash": self.bar.identity_hash,
                "is_correction": self.is_correction,
                "provider_event_id": self.provider_event_id,
                "raw_payload_sha256": self.raw_payload_sha256,
                "schema_version": self.SCHEMA_VERSION,
                "source": self.bar.source,
            }
        )

    @property
    def identity_hash(self) -> str:
        """Forward the enclosed logical bar identity."""

        return self.bar.identity_hash

    @property
    def content_hash(self) -> str:
        """Forward the enclosed logical bar content digest."""

        return self.bar.content_hash
