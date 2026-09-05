"""Shared, broker-free normalization for historical and real-time Alpaca bars."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Self, cast

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.constants import MAX_SIGNED_64_BIT_INTEGER
from adaptive_trader.platform.domain import require_finite_decimal, require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

_SUPPORTED_SERIES = frozenset(
    {
        ("alpaca", "iex", "raw", "1Min", "external_provider"),
        ("alpaca", "iex", "raw", "15Min", "external_provider"),
        ("fixture", "iex", "raw", "1Min", "offline_fixture"),
        ("fixture", "iex", "raw", "15Min", "offline_fixture"),
    }
)
_TIMEFRAME_DURATIONS = {
    "1Min": timedelta(minutes=1),
    "15Min": timedelta(minutes=15),
}
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_QUALITY_FLAG_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", flags=re.ASCII)
_SOURCE_EVENT_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    flags=re.ASCII,
)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_PLAIN_DECIMAL_PATTERN = re.compile(
    r"^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$",
    flags=re.ASCII,
)
_MAX_RAW_FIELDS = 20
_ALPACA_FIELDS = frozenset(
    {
        "S",
        "T",
        "adjustment",
        "c",
        "close",
        "feed",
        "h",
        "high",
        "l",
        "low",
        "n",
        "o",
        "open",
        "provider",
        "schema_version",
        "symbol",
        "t",
        "timeframe",
        "timestamp",
        "trade_count",
        "v",
        "volume",
        "vw",
        "vwap",
    }
)


class MarketDataNormalizationError(DomainValidationError):
    """Raised when a provider event cannot enter the canonical data boundary."""


@dataclass(frozen=True, slots=True)
class NormalizationPolicy:
    """Exact series identity and immutable universe authority for one normalizer.

    The collection allowlist is the union of active, benchmark, and context symbols. Explicitly
    excluded symbols are kept separately so an exclusion can never be confused with absence.
    """

    collection_allowlist: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    provider: str = "alpaca"
    feed: str = "iex"
    adjustment: str = "raw"
    timeframe: str = "1Min"
    source_mode: str = "external_provider"

    def __post_init__(self) -> None:
        _require_series(
            self.provider,
            self.feed,
            self.adjustment,
            self.timeframe,
            self.source_mode,
        )
        if self.timeframe != "1Min":
            raise MarketDataNormalizationError("normalization source timeframe must be 1Min")
        allowed = _require_symbol_tuple(
            self.collection_allowlist,
            field_name="collection allowlist",
            require_nonempty=True,
        )
        excluded = _require_symbol_tuple(
            self.excluded_symbols,
            field_name="excluded symbols",
            require_nonempty=False,
        )
        if set(allowed) & set(excluded):
            raise MarketDataNormalizationError(
                "collection allowlist and excluded symbols must be disjoint"
            )
        object.__setattr__(self, "collection_allowlist", allowed)
        object.__setattr__(self, "excluded_symbols", excluded)

    @classmethod
    def for_external_experiment(cls, experiment: ExperimentDefinition) -> Self:
        """Bind an Alpaca normalizer to the exact immutable external data contract."""

        if type(experiment) is not ExperimentDefinition:
            raise MarketDataNormalizationError("experiment definition is invalid")
        market_data = experiment.market_data
        return cls(
            collection_allowlist=experiment.collection_allowlist,
            excluded_symbols=experiment.excluded,
            provider=market_data.provider,
            feed=market_data.feed,
            adjustment=market_data.adjustment,
            timeframe=market_data.source_timeframe,
            source_mode="external_provider",
        )

    @classmethod
    def for_offline_fixture(cls, experiment: ExperimentDefinition) -> Self:
        """Preserve fixture provenance while reusing an experiment's series and universe."""

        if type(experiment) is not ExperimentDefinition:
            raise MarketDataNormalizationError("experiment definition is invalid")
        market_data = experiment.market_data
        if (
            market_data.feed != "iex"
            or market_data.adjustment != "raw"
            or market_data.source_timeframe != "1Min"
        ):
            raise MarketDataNormalizationError(
                "offline fixture cannot represent the configured external series"
            )
        return cls(
            collection_allowlist=experiment.collection_allowlist,
            excluded_symbols=experiment.excluded,
            provider="fixture",
            feed=market_data.feed,
            adjustment=market_data.adjustment,
            timeframe=market_data.source_timeframe,
            source_mode="offline_fixture",
        )

    def permits(self, symbol: str) -> bool:
        """Return exact collection authority without aliases or provider fallback."""

        return symbol in self.collection_allowlist and symbol not in self.excluded_symbols


@dataclass(frozen=True, slots=True)
class CanonicalBar:
    """Immutable start-inclusive, end-exclusive market-bar payload.

    ``payload_hash`` covers normalized provider semantics and correction metadata. It excludes
    receipt time so a retry through another transport remains the same effective payload. The
    complete canonical bytes still retain receipt provenance.
    """

    provider: str
    feed: str
    adjustment: str
    symbol: str
    timeframe: str
    source_mode: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    receipt_timestamp_utc: datetime
    provider_event_timestamp_utc: datetime | None
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None
    vwap: Decimal | None
    schema_version: int
    source_event_id: str
    quality_flags: tuple[str, ...]
    is_correction: bool
    correction_of_source_event_id: str | None
    payload_hash: str = ""

    def __post_init__(self) -> None:
        _require_series(
            self.provider,
            self.feed,
            self.adjustment,
            self.timeframe,
            self.source_mode,
        )
        _require_symbol(self.symbol, field_name="bar symbol")

        start = _require_utc(self.interval_start_utc, field_name="interval_start_utc")
        end = _require_utc(self.interval_end_utc, field_name="interval_end_utc")
        receipt = _require_utc(
            self.receipt_timestamp_utc,
            field_name="receipt_timestamp_utc",
        )
        duration = _TIMEFRAME_DURATIONS[self.timeframe]
        if start.second != 0 or start.microsecond != 0:
            raise MarketDataNormalizationError("bar interval start must be minute-aligned")
        if end <= start or end - start != duration:
            raise MarketDataNormalizationError("bar interval does not match its timeframe")
        if receipt < end:
            raise MarketDataNormalizationError("bar receipt timestamp precedes interval end")
        object.__setattr__(self, "interval_start_utc", start)
        object.__setattr__(self, "interval_end_utc", end)
        object.__setattr__(self, "receipt_timestamp_utc", receipt)

        if self.provider_event_timestamp_utc is not None:
            provider_timestamp = _require_utc(
                self.provider_event_timestamp_utc,
                field_name="provider_event_timestamp_utc",
            )
            if provider_timestamp > receipt:
                raise MarketDataNormalizationError(
                    "bar provider event timestamp follows its receipt timestamp"
                )
            object.__setattr__(
                self,
                "provider_event_timestamp_utc",
                provider_timestamp,
            )

        prices = {
            field_name: _require_decimal(getattr(self, field_name), field_name=field_name)
            for field_name in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in prices.values()):
            raise MarketDataNormalizationError("bar prices must be positive")
        if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
            raise MarketDataNormalizationError("bar OHLC values are incoherent")
        for field_name, value in prices.items():
            object.__setattr__(self, field_name, value)

        volume = _require_decimal(self.volume, field_name="volume")
        if volume < 0:
            raise MarketDataNormalizationError("bar volume must be nonnegative")
        if volume != volume.to_integral_value():
            raise MarketDataNormalizationError("Alpaca bar volume must be a whole number")
        if volume.is_zero():
            volume = volume.copy_abs()
        object.__setattr__(self, "volume", volume)

        if self.trade_count is not None and (
            type(self.trade_count) is not int
            or not 0 <= self.trade_count <= MAX_SIGNED_64_BIT_INTEGER
        ):
            raise MarketDataNormalizationError("bar trade count is invalid")

        if self.vwap is not None:
            vwap = _require_decimal(self.vwap, field_name="vwap")
            if vwap < 0:
                raise MarketDataNormalizationError("bar VWAP must be nonnegative")
            if vwap.is_zero():
                vwap = vwap.copy_abs()
            object.__setattr__(self, "vwap", vwap)

        if type(self.schema_version) is not int or self.schema_version != 1:
            raise MarketDataNormalizationError("bar schema version is unsupported")
        _require_source_event_id(self.source_event_id, field_name="source event ID")
        flags = _require_quality_flags(self.quality_flags)
        object.__setattr__(self, "quality_flags", flags)
        if type(self.is_correction) is not bool:
            raise MarketDataNormalizationError("bar correction flag must be boolean")
        if self.correction_of_source_event_id is not None:
            _require_source_event_id(
                self.correction_of_source_event_id,
                field_name="correction source event ID",
            )
            if not self.is_correction:
                raise MarketDataNormalizationError(
                    "bar correction reference requires correction metadata"
                )
            if self.correction_of_source_event_id == self.source_event_id:
                raise MarketDataNormalizationError("bar correction cannot reference itself")

        computed_hash = sha256_hex(self.hash_payload())
        if self.payload_hash == "":
            object.__setattr__(self, "payload_hash", computed_hash)
        elif (
            type(self.payload_hash) is not str
            or _HASH_PATTERN.fullmatch(self.payload_hash) is None
            or self.payload_hash != computed_hash
        ):
            raise MarketDataNormalizationError("bar payload hash is invalid")

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, datetime]:
        """Return the exact canonical identity required by storage."""

        return (
            self.provider,
            self.feed,
            self.adjustment,
            self.symbol,
            self.timeframe,
            self.interval_start_utc,
        )

    @property
    def identity_hash(self) -> str:
        """Return the deterministic digest of ``identity_key``."""

        return sha256_hex(self.identity_key)

    @property
    def has_promotable_provenance(self) -> bool:
        """Return whether the bar carries external-provider rather than fixture provenance."""

        return self.source_mode == "external_provider"

    def hash_payload(self) -> dict[str, Any]:
        """Return retry-stable normalized semantics covered by ``payload_hash``."""

        return {
            "adjustment": self.adjustment,
            "close": self.close,
            "correction": {
                "correction_of_source_event_id": self.correction_of_source_event_id,
                "is_correction": self.is_correction,
            },
            "feed": self.feed,
            "high": self.high,
            "interval_end_utc": self.interval_end_utc,
            "interval_start_utc": self.interval_start_utc,
            "low": self.low,
            "open": self.open,
            "provider": self.provider,
            "provider_event_timestamp_utc": self.provider_event_timestamp_utc,
            "quality_flags": self.quality_flags,
            "schema_version": self.schema_version,
            "source_event_id": self.source_event_id,
            "source_mode": self.source_mode,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "trade_count": self.trade_count,
            "volume": self.volume,
            "vwap": self.vwap,
        }

    def canonical_payload(self) -> dict[str, Any]:
        """Return complete immutable-value content for canonical persistence or comparison."""

        return {
            **self.hash_payload(),
            "payload_hash": self.payload_hash,
            "receipt_timestamp_utc": self.receipt_timestamp_utc,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return byte-stable canonical JSON containing semantic and receipt provenance."""

        return canonical_json_bytes(self.canonical_payload())

    @property
    def normalized_bytes(self) -> bytes:
        """Return retry-stable canonical JSON used by the payload digest."""

        return canonical_json_bytes(self.hash_payload())


def normalize_alpaca_bar(
    payload: Mapping[str, object],
    *,
    policy: NormalizationPolicy,
    receipt_timestamp_utc: datetime,
    symbol: str | None = None,
    execution_reference: bool = False,
    quality_flags: Sequence[str] = (),
    is_correction: bool = False,
    correction_of_source_event_id: str | None = None,
    source_event_id: str | None = None,
    expected_payload_hash: str | None = None,
) -> CanonicalBar:
    """Normalize either a historical REST or real-time WebSocket Alpaca bar.

    The function has no transport/source-mode parameter: both delivery paths enter this exact
    boundary. Route-only Alpaca ``T`` metadata is validated and then excluded from canonical
    semantics. No provider, feed, adjustment, or timeframe fallback is performed.
    """

    if type(policy) is not NormalizationPolicy or (
        policy.provider != "alpaca" or policy.source_mode != "external_provider"
    ):
        raise MarketDataNormalizationError("Alpaca normalization policy is invalid")
    return _normalize_provider_bar(
        payload,
        policy=policy,
        receipt_timestamp_utc=receipt_timestamp_utc,
        symbol=symbol,
        execution_reference=execution_reference,
        quality_flags=quality_flags,
        is_correction=is_correction,
        correction_of_source_event_id=correction_of_source_event_id,
        source_event_id=source_event_id,
        expected_payload_hash=expected_payload_hash,
        allow_alpaca_event_type=True,
    )


def normalize_fixture_bar(
    payload: Mapping[str, object],
    *,
    policy: NormalizationPolicy,
    receipt_timestamp_utc: datetime,
    symbol: str | None = None,
    execution_reference: bool = False,
    quality_flags: Sequence[str] = (),
    source_event_id: str | None = None,
    expected_payload_hash: str | None = None,
) -> CanonicalBar:
    """Normalize a deterministic offline bar without claiming external-provider provenance."""

    if type(policy) is not NormalizationPolicy or (
        policy.provider != "fixture" or policy.source_mode != "offline_fixture"
    ):
        raise MarketDataNormalizationError("fixture normalization policy is invalid")
    return _normalize_provider_bar(
        payload,
        policy=policy,
        receipt_timestamp_utc=receipt_timestamp_utc,
        symbol=symbol,
        execution_reference=execution_reference,
        quality_flags=quality_flags,
        is_correction=False,
        correction_of_source_event_id=None,
        source_event_id=source_event_id,
        expected_payload_hash=expected_payload_hash,
        allow_alpaca_event_type=False,
    )


def _normalize_provider_bar(
    payload: Mapping[str, object],
    *,
    policy: NormalizationPolicy,
    receipt_timestamp_utc: datetime,
    symbol: str | None,
    execution_reference: bool,
    quality_flags: Sequence[str],
    is_correction: bool,
    correction_of_source_event_id: str | None,
    source_event_id: str | None,
    expected_payload_hash: str | None,
    allow_alpaca_event_type: bool,
) -> CanonicalBar:
    if type(policy) is not NormalizationPolicy:
        raise MarketDataNormalizationError("normalization policy is invalid")
    if type(payload) is not dict:
        raise MarketDataNormalizationError("provider bar payload must be an exact dictionary")
    raw = payload
    if len(raw) > _MAX_RAW_FIELDS:
        raise MarketDataNormalizationError("Alpaca bar payload contains too many fields")
    if any(type(key) is not str for key in raw):
        raise MarketDataNormalizationError("Alpaca bar payload keys must be strings")
    unknown = set(raw) - _ALPACA_FIELDS
    if unknown:
        raise MarketDataNormalizationError("Alpaca bar payload contains unsupported fields")

    _require_optional_exact_metadata(raw, "provider", policy.provider)
    _require_optional_exact_metadata(raw, "feed", policy.feed)
    _require_optional_exact_metadata(raw, "adjustment", policy.adjustment)
    _require_optional_exact_metadata(raw, "timeframe", policy.timeframe)
    if "schema_version" in raw and raw["schema_version"] != 1:
        raise MarketDataNormalizationError("Alpaca bar schema version is unsupported")

    event_type = raw.get("T")
    if event_type is not None:
        if not allow_alpaca_event_type:
            raise MarketDataNormalizationError("fixture bar payload contains transport metadata")
        if type(event_type) is not str or event_type not in {"b", "u"}:
            raise MarketDataNormalizationError("Alpaca bar event type is unsupported")
        if event_type == "u" and is_correction is not True:
            raise MarketDataNormalizationError("updated Alpaca bar requires correction metadata")
    if type(execution_reference) is not bool or type(is_correction) is not bool:
        raise MarketDataNormalizationError("bar normalization flags must be boolean")

    raw_symbol = _single_alias(raw, "S", "symbol", required=False)
    if raw_symbol is None and symbol is None:
        raise MarketDataNormalizationError("Alpaca bar symbol is missing")
    if symbol is not None:
        expected_symbol = _require_symbol(symbol, field_name="expected symbol")
        if raw_symbol is not None:
            actual_symbol = _normalize_provider_symbol(raw_symbol)
            if actual_symbol != expected_symbol:
                raise MarketDataNormalizationError(
                    "Alpaca bar symbol does not match its response group"
                )
        normalized_symbol = expected_symbol
    else:
        normalized_symbol = _normalize_provider_symbol(raw_symbol)
    if normalized_symbol in policy.excluded_symbols:
        raise MarketDataNormalizationError("Alpaca bar symbol is explicitly excluded")
    if not policy.permits(normalized_symbol):
        raise MarketDataNormalizationError("Alpaca bar symbol is outside the collection allowlist")

    interval_start = _parse_timestamp(
        _single_alias(raw, "t", "timestamp", required=True),
        field_name="event_timestamp",
    )
    duration = _TIMEFRAME_DURATIONS[policy.timeframe]
    interval_end = interval_start + duration
    receipt = _require_utc(receipt_timestamp_utc, field_name="receipt_timestamp_utc")

    open_price = _parse_decimal(
        _single_alias(raw, "o", "open", required=True),
        field_name="open",
    )
    high_price = _parse_decimal(
        _single_alias(raw, "h", "high", required=True),
        field_name="high",
    )
    low_price = _parse_decimal(
        _single_alias(raw, "l", "low", required=True),
        field_name="low",
    )
    close_price = _parse_decimal(
        _single_alias(raw, "c", "close", required=True),
        field_name="close",
    )
    volume = _parse_decimal(
        _single_alias(raw, "v", "volume", required=True),
        field_name="volume",
    )
    raw_trade_count = _single_alias(raw, "n", "trade_count", required=False)
    trade_count = (
        None
        if raw_trade_count is None
        else _parse_nonnegative_integer(raw_trade_count, field_name="trade_count")
    )
    raw_vwap = _single_alias(raw, "vw", "vwap", required=False)
    vwap = None if raw_vwap is None else _parse_decimal(raw_vwap, field_name="vwap")
    if execution_reference and policy.timeframe == "1Min" and vwap is None:
        raise MarketDataNormalizationError("one-minute execution-reference bar requires VWAP")

    if type(quality_flags) not in {list, tuple}:
        raise MarketDataNormalizationError("bar quality flags must be a sequence of flags")
    if len(quality_flags) > 64:
        raise MarketDataNormalizationError("bar quality flag limit exceeded")
    flags = _require_quality_flags(tuple(quality_flags))
    selected_source_id = source_event_id
    if selected_source_id is None:
        selected_source_id = _derived_source_event_id(
            provider=policy.provider,
            feed=policy.feed,
            adjustment=policy.adjustment,
            symbol=normalized_symbol,
            timeframe=policy.timeframe,
            interval_start_utc=interval_start,
            provider_event_timestamp_utc=interval_start,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            trade_count=trade_count,
            vwap=vwap,
        )

    return CanonicalBar(
        provider=policy.provider,
        feed=policy.feed,
        adjustment=policy.adjustment,
        symbol=normalized_symbol,
        timeframe=policy.timeframe,
        source_mode=policy.source_mode,
        interval_start_utc=interval_start,
        interval_end_utc=interval_end,
        receipt_timestamp_utc=receipt,
        provider_event_timestamp_utc=interval_start,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        trade_count=trade_count,
        vwap=vwap,
        schema_version=1,
        source_event_id=selected_source_id,
        quality_flags=flags,
        is_correction=is_correction,
        correction_of_source_event_id=correction_of_source_event_id,
        payload_hash="" if expected_payload_hash is None else expected_payload_hash,
    )


def _require_series(
    provider: object,
    feed: object,
    adjustment: object,
    timeframe: object,
    source_mode: object,
) -> None:
    series = (provider, feed, adjustment, timeframe, source_mode)
    if any(type(value) is not str for value in series) or series not in _SUPPORTED_SERIES:
        raise MarketDataNormalizationError(
            "market-data provider/feed/adjustment/timeframe is unsupported"
        )


def _require_symbol(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SYMBOL_PATTERN.fullmatch(value) is None:
        raise MarketDataNormalizationError(f"{field_name} is invalid")
    return value


def _normalize_provider_symbol(value: object) -> str:
    if type(value) is not str:
        raise MarketDataNormalizationError("provider bar symbol is invalid")
    if len(value) > 10 or not value.isascii():
        raise MarketDataNormalizationError("provider bar symbol is invalid")
    normalized = value.upper()
    return _require_symbol(normalized, field_name="provider bar symbol")


def _require_symbol_tuple(
    value: object,
    *,
    field_name: str,
    require_nonempty: bool,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise MarketDataNormalizationError(f"{field_name} must be an immutable tuple")
    symbols = cast(tuple[object, ...], value)
    normalized = tuple(_require_symbol(symbol, field_name=field_name) for symbol in symbols)
    if require_nonempty and not normalized:
        raise MarketDataNormalizationError(f"{field_name} must not be empty")
    if normalized != tuple(sorted(set(normalized))):
        raise MarketDataNormalizationError(f"{field_name} must be sorted and unique")
    return normalized


def _require_utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise MarketDataNormalizationError(f"{field_name} must be timezone-aware UTC") from None


def _require_decimal(value: object, *, field_name: str) -> Decimal:
    try:
        return require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise MarketDataNormalizationError(f"bar {field_name} must be a finite Decimal") from None


def _parse_decimal(value: object, *, field_name: str) -> Decimal:
    parsed: Decimal
    try:
        if type(value) is Decimal:
            parsed = value
        elif type(value) is int:
            parsed = Decimal(value)
        elif type(value) is float:
            numeric = value
            if not math.isfinite(numeric):
                raise MarketDataNormalizationError(f"Alpaca bar {field_name} must be finite")
            parsed = Decimal(str(numeric))
        elif type(value) is str and len(value) <= 128 and _PLAIN_DECIMAL_PATTERN.fullmatch(value):
            parsed = Decimal(value)
        else:
            raise MarketDataNormalizationError(f"Alpaca bar {field_name} must be numeric")
    except InvalidOperation:
        raise MarketDataNormalizationError(f"Alpaca bar {field_name} is invalid") from None
    try:
        return require_finite_decimal(parsed, field_name=field_name)
    except DomainValidationError:
        raise MarketDataNormalizationError(
            f"Alpaca bar {field_name} exceeds its numeric limits"
        ) from None


def _parse_nonnegative_integer(value: object, *, field_name: str) -> int:
    if type(value) is int:
        parsed = value
    elif type(value) is float:
        numeric = value
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise MarketDataNormalizationError(f"Alpaca bar {field_name} is invalid")
        parsed = int(numeric)
    elif type(value) is Decimal:
        numeric_decimal = value
        if (
            not numeric_decimal.is_finite()
            or numeric_decimal != numeric_decimal.to_integral_value()
        ):
            raise MarketDataNormalizationError(f"Alpaca bar {field_name} is invalid")
        parsed = int(numeric_decimal)
    else:
        raise MarketDataNormalizationError(f"Alpaca bar {field_name} is invalid")
    if not 0 <= parsed <= MAX_SIGNED_64_BIT_INTEGER:
        raise MarketDataNormalizationError(f"Alpaca bar {field_name} is invalid")
    return parsed


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if type(value) is datetime:
        timestamp = value
    elif type(value) is str:
        text = value
        if len(text) > 40 or not text.endswith("Z"):
            raise MarketDataNormalizationError(f"{field_name} must be an RFC 3339 UTC instant")
        try:
            timestamp = datetime.fromisoformat(f"{text[:-1]}+00:00")
        except ValueError:
            raise MarketDataNormalizationError(
                f"{field_name} must be an RFC 3339 UTC instant"
            ) from None
    else:
        raise MarketDataNormalizationError(f"{field_name} must be a UTC timestamp")
    timestamp = _require_utc(timestamp, field_name=field_name)
    if timestamp.second != 0 or timestamp.microsecond != 0:
        raise MarketDataNormalizationError("provider one-minute bar timestamp is not aligned")
    return timestamp


def _single_alias(
    payload: Mapping[str, object],
    compact: str,
    verbose: str,
    *,
    required: bool,
) -> object | None:
    if compact in payload and verbose in payload:
        raise MarketDataNormalizationError("Alpaca bar payload contains ambiguous aliases")
    if compact in payload:
        return payload[compact]
    if verbose in payload:
        return payload[verbose]
    if required:
        raise MarketDataNormalizationError("Alpaca bar payload is missing a required field")
    return None


def _require_optional_exact_metadata(
    payload: Mapping[str, object],
    field_name: str,
    expected: str,
) -> None:
    if field_name in payload and payload[field_name] != expected:
        raise MarketDataNormalizationError(
            f"Alpaca bar {field_name} does not match the configured series"
        )


def _require_source_event_id(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SOURCE_EVENT_ID_PATTERN.fullmatch(value) is None:
        raise MarketDataNormalizationError(f"bar {field_name} is invalid")
    return value


def _require_quality_flags(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise MarketDataNormalizationError("bar quality flags must be an immutable tuple")
    flags = cast(tuple[object, ...], value)
    if len(flags) > 64:
        raise MarketDataNormalizationError("bar quality flag limit exceeded")
    if any(
        type(flag) is not str or _QUALITY_FLAG_PATTERN.fullmatch(flag) is None for flag in flags
    ):
        raise MarketDataNormalizationError("bar quality flag is invalid")
    normalized = cast(tuple[str, ...], flags)
    if normalized != tuple(sorted(set(normalized))):
        raise MarketDataNormalizationError("bar quality flags must be sorted and unique")
    return normalized


def _derived_source_event_id(**payload: object) -> str:
    provider = payload.get("provider")
    if provider not in {"alpaca", "fixture"}:
        raise MarketDataNormalizationError("source event provider is invalid")
    return f"{provider}_bar_{sha256_hex(('canonical_source_event_v1', payload))}"
