"""Read-only Alpaca IEX adapters for historical and streaming minute bars."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import requests
from websockets.sync.client import connect as websocket_connect

from adaptive_trader.collection.contracts import MarketBarV1, RawBarObservationV1
from adaptive_trader.collection.credentials import AlpacaDataCredentials
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1

OFFICIAL_ALPACA_DATA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
OFFICIAL_ALPACA_IEX_STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"
_HISTORICAL_SOURCES = frozenset({"historical_backfill", "historical_reconciliation"})
_REST_TIMEOUT_SECONDS = (5.0, 20.0)
_STREAM_CONTROL_TIMEOUT_SECONDS = 10.0
_STREAM_RECEIVE_TIMEOUT_SECONDS = 5.0
_MAX_PAGE_TOKEN_LENGTH = 4096
_MAX_HISTORICAL_PAGES = 100
_MAX_HISTORICAL_OBSERVATIONS = 1_000_000
_MAX_PROVIDER_RETRY_SECONDS = 300.0

ObservationHandler = Callable[[RawBarObservationV1], Awaitable[None] | None]


class AlpacaDataSourceError(RuntimeError):
    """Raised when Alpaca data cannot be requested, decoded, or streamed."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        provider_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.provider_status = provider_status
        self.retry_after_seconds = retry_after_seconds


class _HistoricalResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class _HistoricalClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> _HistoricalResponse: ...

    def close(self) -> None: ...


class _LiveConnection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


LiveConnectionFactory = Callable[..., _LiveConnection]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _never_stop() -> bool:
    return False


def _validated_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _validate_endpoint(value: str, *, expected: str, kind: str) -> None:
    parsed = urlsplit(value)
    expected_parsed = urlsplit(expected)
    if (
        value != expected
        or parsed.scheme != expected_parsed.scheme
        or parsed.hostname != expected_parsed.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AlpacaDataSourceError(
            f"The Alpaca {kind} endpoint is not the approved data host",
            retryable=False,
        )


def _validate_official_data_endpoints() -> None:
    _validate_endpoint(
        OFFICIAL_ALPACA_DATA_BARS_URL,
        expected="https://data.alpaca.markets/v2/stocks/bars",
        kind="REST",
    )
    _validate_endpoint(
        OFFICIAL_ALPACA_IEX_STREAM_URL,
        expected="wss://stream.data.alpaca.markets/v2/iex",
        kind="WebSocket",
    )


def _normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    if isinstance(symbols, (str, bytes)):
        raise ValueError("symbols must be a sequence of explicit ticker symbols")
    normalized: list[str] = []
    allowed = frozenset(COLLECTION_UNIVERSE_V1.symbols)
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbols must contain nonempty strings")
        candidate = symbol.strip().upper()
        if candidate not in allowed:
            raise ValueError(f"Symbol is not in {COLLECTION_UNIVERSE_V1.SCHEMA_VERSION}")
        if candidate in normalized:
            raise ValueError("Duplicate symbol requested")
        normalized.append(candidate)
    if not normalized:
        raise ValueError("At least one collection symbol is required")
    return tuple(normalized)


def _value(raw: Any, *names: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        for name in names:
            if name in raw:
                return raw[name]
        return default
    for name in names:
        if hasattr(raw, name):
            return getattr(raw, name)
    return default


def _timestamp(value: Any, *, field_name: str) -> datetime:
    candidate = value
    if not isinstance(candidate, (str, datetime)):
        converter = getattr(candidate, "to_datetime", None)
        if callable(converter):
            candidate = converter()
    if isinstance(candidate, str):
        normalized = candidate.strip()
        if normalized.endswith(("Z", "z")):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            candidate = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise AlpacaDataSourceError(f"{field_name} is not a valid timestamp") from exc
    if not isinstance(candidate, datetime):
        raise AlpacaDataSourceError(f"{field_name} is not a valid timestamp")
    try:
        return _validated_utc(candidate, field_name=field_name)
    except ValueError as exc:
        raise AlpacaDataSourceError(str(exc)) from None


def _whole_number(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise AlpacaDataSourceError(f"{field_name} must be a whole number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AlpacaDataSourceError(f"{field_name} must be a whole number") from None
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise AlpacaDataSourceError(f"{field_name} must be a nonnegative whole number")
    return int(number)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AlpacaDataSourceError("Alpaca raw payload contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AlpacaDataSourceError("Alpaca raw payload contains a non-finite number")
        return format(value, "f")
    if isinstance(value, datetime):
        return (
            _validated_utc(value, field_name="Alpaca raw payload timestamp")
            .isoformat()
            .replace("+00:00", "Z")
        )
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    converter = getattr(value, "to_datetime", None)
    if callable(converter):
        return _json_value(converter())
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json"))
    raise AlpacaDataSourceError(f"Unsupported value in Alpaca raw payload: {type(value).__name__}")


def _raw_event(raw: Any, *, symbol: str) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        payload = cast(dict[str, Any], _json_value(raw))
    else:
        model_dump = getattr(raw, "model_dump", None)
        if callable(model_dump):
            payload = cast(dict[str, Any], _json_value(model_dump(mode="json")))
        else:
            payload = {
                "symbol": _json_value(_value(raw, "symbol", default=symbol)),
                "timestamp": _json_value(_value(raw, "timestamp")),
                "open": _json_value(_value(raw, "open")),
                "high": _json_value(_value(raw, "high")),
                "low": _json_value(_value(raw, "low")),
                "close": _json_value(_value(raw, "close")),
                "volume": _json_value(_value(raw, "volume")),
                "trade_count": _json_value(_value(raw, "trade_count")),
                "vwap": _json_value(_value(raw, "vwap")),
            }
    if _value(payload, "symbol", "S") is None:
        payload["S"] = symbol
    return payload


def _observation(
    raw: Any,
    *,
    expected_symbol: str | None,
    received_at: datetime,
    source: str,
    is_correction: bool,
) -> RawBarObservationV1:
    raw_symbol = _value(raw, "symbol", "S", default=expected_symbol)
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise AlpacaDataSourceError("Alpaca bar is missing its symbol")
    symbol = raw_symbol.strip().upper()
    if expected_symbol is not None and symbol != expected_symbol:
        raise AlpacaDataSourceError("Alpaca bar symbol does not match its response group")
    if symbol not in COLLECTION_UNIVERSE_V1.symbols:
        raise AlpacaDataSourceError(f"Alpaca returned an unrequested collection symbol: {symbol}")
    event_timestamp = _timestamp(
        _value(raw, "timestamp", "t"),
        field_name="Alpaca bar timestamp",
    )
    if event_timestamp.second != 0 or event_timestamp.microsecond != 0:
        raise AlpacaDataSourceError("Alpaca one-minute bar timestamp is not minute-aligned")
    raw_payload_json = json.dumps(
        _raw_event(raw, symbol=symbol),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    try:
        bar = MarketBarV1(
            provider="alpaca",
            feed="IEX",
            adjustment="raw",
            symbol=symbol,
            timeframe="1m",
            bar_timestamp_utc=event_timestamp,
            provider_event_timestamp_utc=event_timestamp,
            receipt_timestamp_utc=_validated_utc(
                received_at,
                field_name="receipt timestamp",
            ),
            open=_value(raw, "open", "o"),
            high=_value(raw, "high", "h"),
            low=_value(raw, "low", "l"),
            close=_value(raw, "close", "c"),
            volume=_whole_number(_value(raw, "volume", "v"), field_name="volume"),
            trade_count=(
                None
                if _value(raw, "trade_count", "n") is None
                else _whole_number(_value(raw, "trade_count", "n"), field_name="trade_count")
            ),
            vwap=_value(raw, "vwap", "vw"),
            source=source,
        )
        return RawBarObservationV1(
            bar=bar,
            is_correction=is_correction,
            raw_payload_json=raw_payload_json,
        )
    except (TypeError, ValueError) as exc:
        raise AlpacaDataSourceError(f"Invalid Alpaca bar: {exc}") from None


def _safe_error(prefix: str, exc: Exception, credentials: AlpacaDataCredentials) -> str:
    detail = credentials.redact(str(exc) or type(exc).__name__)
    return f"{prefix}: {detail}"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _rate_limit_retry_seconds(response: _HistoricalResponse, *, now: datetime) -> float | None:
    """Return a bounded provider-directed delay without retaining response headers."""

    headers = {str(key).lower(): str(value).strip() for key, value in response.headers.items()}
    candidates: list[float] = []
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        with suppress(ValueError):
            candidates.append(float(retry_after))
    rate_limit_reset = headers.get("x-ratelimit-reset")
    if rate_limit_reset is not None:
        with suppress(ValueError):
            candidates.append(float(rate_limit_reset) - now.timestamp())
    nonnegative = [value for value in candidates if math.isfinite(value) and value >= 0]
    if not nonnegative:
        return None
    return min(_MAX_PROVIDER_RETRY_SECONDS, max(nonnegative))


def _decode_stream_frame(payload: str | bytes) -> tuple[Mapping[str, Any], ...]:
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        decoded = json.loads(text)
    except (UnicodeDecodeError, TypeError, ValueError):
        raise AlpacaDataSourceError("Alpaca data stream returned invalid JSON") from None
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(message, Mapping) for message in decoded)
    ):
        raise AlpacaDataSourceError("Alpaca data stream returned an invalid message batch")
    return tuple(cast(Mapping[str, Any], message) for message in decoded)


def _raise_for_stream_error(message: Mapping[str, Any]) -> None:
    if message.get("T") != "error":
        return
    code = message.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        raise AlpacaDataSourceError(
            f"Alpaca data stream returned error code {code}",
            retryable=code in {404, 406, 407, 429, 500, 502, 503, 504},
            provider_status=code,
        )
    raise AlpacaDataSourceError("Alpaca data stream returned an error response")


def _receive_messages(
    connection: _LiveConnection,
    *,
    timeout: float,
) -> tuple[Mapping[str, Any], ...]:
    messages = _decode_stream_frame(connection.recv(timeout=timeout))
    for message in messages:
        _raise_for_stream_error(message)
    return messages


def _expect_success(connection: _LiveConnection, expected_message: str) -> None:
    messages = _receive_messages(connection, timeout=_STREAM_CONTROL_TIMEOUT_SECONDS)
    if len(messages) != 1 or messages[0].get("T") != "success":
        raise AlpacaDataSourceError("Alpaca data stream returned an unexpected control message")
    if messages[0].get("msg") != expected_message:
        raise AlpacaDataSourceError("Alpaca data stream did not complete its handshake")


def _subscription_set(message: Mapping[str, Any], channel: str) -> frozenset[str]:
    raw_symbols = message.get(channel)
    if not isinstance(raw_symbols, list) or any(not isinstance(item, str) for item in raw_symbols):
        raise AlpacaDataSourceError("Alpaca data stream returned an invalid subscription")
    return frozenset(item.strip().upper() for item in raw_symbols)


def _expect_subscription(
    connection: _LiveConnection,
    requested: frozenset[str],
) -> None:
    messages = _receive_messages(connection, timeout=_STREAM_CONTROL_TIMEOUT_SECONDS)
    if len(messages) != 1 or messages[0].get("T") != "subscription":
        raise AlpacaDataSourceError("Alpaca data stream did not confirm its subscription")
    message = messages[0]
    if (
        _subscription_set(message, "bars") != requested
        or _subscription_set(message, "updatedBars") != requested
    ):
        raise AlpacaDataSourceError("Alpaca data stream confirmed an incomplete subscription")


class AlpacaHistoricalBarSource:
    """Fetch raw, one-minute IEX bars without exposing endpoint configuration."""

    def __init__(
        self,
        credentials: AlpacaDataCredentials,
        *,
        client: _HistoricalClient | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        if not isinstance(credentials, AlpacaDataCredentials):
            raise TypeError("credentials must be AlpacaDataCredentials")
        _validate_official_data_endpoints()
        self._credentials = credentials
        self._clock = clock
        if client is not None:
            self._client = client
            self._owns_client = False
            return
        try:
            session = requests.Session()
            session.trust_env = False
            self._client = cast(_HistoricalClient, session)
            self._owns_client = True
        except Exception as exc:
            raise AlpacaDataSourceError(
                _safe_error("Unable to create Alpaca historical data client", exc, credentials)
            ) from None

    def close(self) -> None:
        """Release the owned HTTPS connection pool."""

        if self._owns_client:
            self._client.close()

    def _request_page(self, params: Mapping[str, str | int]) -> Mapping[str, Any]:
        try:
            response = self._client.get(
                OFFICIAL_ALPACA_DATA_BARS_URL,
                headers={
                    "Accept": "application/json",
                    "APCA-API-KEY-ID": self._credentials.api_key,
                    "APCA-API-SECRET-KEY": self._credentials.secret_key,
                },
                params=params,
                timeout=_REST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except Exception as exc:
            raise AlpacaDataSourceError(
                _safe_error("Alpaca historical data request failed", exc, self._credentials),
                retryable=True,
            ) from None
        if response.status_code != 200:
            status = response.status_code if isinstance(response.status_code, int) else "unknown"
            retry_after_seconds = (
                _rate_limit_retry_seconds(response, now=self._clock()) if status == 429 else None
            )
            raise AlpacaDataSourceError(
                f"Alpaca historical data request returned HTTP status {status}",
                retryable=(
                    isinstance(status, int) and (status in {408, 425, 429} or status >= 500)
                ),
                provider_status=status if isinstance(status, int) else None,
                retry_after_seconds=retry_after_seconds,
            )
        try:
            payload = response.json()
        except Exception:
            raise AlpacaDataSourceError(
                "Alpaca historical data response did not contain valid JSON"
            ) from None
        if not isinstance(payload, Mapping):
            raise AlpacaDataSourceError("Alpaca historical response has an invalid shape")
        return cast(Mapping[str, Any], payload)

    def fetch(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        source: str = "historical_backfill",
    ) -> tuple[RawBarObservationV1, ...]:
        """Fetch the requested half-open UTC range of raw, one-minute IEX bars."""

        requested = _normalize_symbols(symbols)
        if source not in _HISTORICAL_SOURCES:
            raise ValueError("Unsupported historical collection source")
        start_utc = _validated_utc(start, field_name="start")
        end_utc = _validated_utc(end, field_name="end")
        if start_utc >= end_utc:
            raise ValueError("start must precede end")
        base_params: dict[str, str | int] = {
            "symbols": ",".join(requested),
            "timeframe": "1Min",
            "start": _rfc3339(start_utc),
            "end": _rfc3339(end_utc - timedelta(microseconds=1)),
            "limit": 10_000,
            "adjustment": "raw",
            "feed": "iex",
            "sort": "asc",
        }
        observations: dict[str, RawBarObservationV1] = {}
        requested_set = frozenset(requested)
        seen_page_tokens: set[str] = set()
        page_token: str | None = None
        page_count = 0
        observation_count = 0
        while True:
            page_count += 1
            if page_count > _MAX_HISTORICAL_PAGES:
                raise AlpacaDataSourceError("Alpaca historical response exceeded the page limit")
            params = dict(base_params)
            if page_token is not None:
                params["page_token"] = page_token
            payload = self._request_page(params)
            raw_data = payload.get("bars")
            if not isinstance(raw_data, Mapping):
                raise AlpacaDataSourceError("Alpaca historical response has invalid bars")
            received_at = _validated_utc(self._clock(), field_name="clock result")
            try:
                for response_symbol, bars in raw_data.items():
                    symbol = str(response_symbol).strip().upper()
                    if symbol not in requested_set:
                        raise AlpacaDataSourceError(
                            f"Alpaca returned data for an unrequested symbol: {symbol}"
                        )
                    if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes, bytearray)):
                        raise AlpacaDataSourceError(
                            "Alpaca historical response contains an invalid bar list"
                        )
                    for raw in bars:
                        observation_count += 1
                        if observation_count > _MAX_HISTORICAL_OBSERVATIONS:
                            raise AlpacaDataSourceError(
                                "Alpaca historical response exceeded the observation limit"
                            )
                        if not isinstance(raw, Mapping):
                            raise AlpacaDataSourceError(
                                "Alpaca historical response contains an invalid bar"
                            )
                        observation = _observation(
                            raw,
                            expected_symbol=symbol,
                            received_at=received_at,
                            source=source,
                            is_correction=False,
                        )
                        if not start_utc <= observation.bar.bar_timestamp_utc < end_utc:
                            raise AlpacaDataSourceError(
                                "Alpaca returned a bar outside the requested half-open interval"
                            )
                        observations.setdefault(observation.observation_id, observation)
            except AlpacaDataSourceError:
                raise
            except (TypeError, ValueError) as exc:
                raise AlpacaDataSourceError(f"Invalid Alpaca historical response: {exc}") from None

            next_page_token = payload.get("next_page_token")
            if next_page_token is None:
                break
            if (
                not isinstance(next_page_token, str)
                or not next_page_token
                or len(next_page_token) > _MAX_PAGE_TOKEN_LENGTH
                or next_page_token in seen_page_tokens
            ):
                raise AlpacaDataSourceError("Alpaca historical response has an invalid page token")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
        return tuple(
            sorted(
                observations.values(),
                key=lambda item: (item.bar.bar_timestamp_utc, item.bar.symbol),
            )
        )


class AlpacaLiveBarSource:
    """Blocking IEX stream that emits both minute bars and their updates."""

    def __init__(
        self,
        credentials: AlpacaDataCredentials,
        *,
        connection_factory: LiveConnectionFactory | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        if not isinstance(credentials, AlpacaDataCredentials):
            raise TypeError("credentials must be AlpacaDataCredentials")
        _validate_official_data_endpoints()
        self._credentials = credentials
        self._clock = clock
        self._connection_factory = connection_factory or cast(
            LiveConnectionFactory,
            websocket_connect,
        )
        self._connection: _LiveConnection | None = None
        self._stop_requested = threading.Event()
        self._running = False
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _deliver(
        self,
        raw: Any,
        *,
        requested: frozenset[str],
        handler: ObservationHandler,
        is_correction: bool,
    ) -> None:
        try:
            observation = _observation(
                raw,
                expected_symbol=None,
                received_at=self._clock(),
                source=("iex_updated_bar" if is_correction else "iex_bar"),
                is_correction=is_correction,
            )
            if observation.bar.symbol not in requested:
                raise AlpacaDataSourceError("Alpaca streamed data for an unrequested symbol")
            result = handler(observation)
            if inspect.isawaitable(result):
                asyncio.run(result)
        except AlpacaDataSourceError:
            raise
        except Exception as exc:
            raise AlpacaDataSourceError(
                _safe_error("Alpaca bar handler failed", exc, self._credentials)
            ) from None

    def run(
        self,
        symbols: Sequence[str],
        handler: ObservationHandler,
        *,
        stop_requested: Callable[[], bool] = _never_stop,
    ) -> None:
        """Run the subscribed stream until it stops or ``stop`` is called."""

        requested_symbols = _normalize_symbols(symbols)
        if not callable(handler):
            raise TypeError("handler must be callable")
        if not callable(stop_requested):
            raise TypeError("stop_requested must be callable")
        if self._stop_requested.is_set() or stop_requested():
            return
        with self._lock:
            if self._running:
                raise RuntimeError("Alpaca live bar source is already running")
            self._running = True
        requested = frozenset(requested_symbols)
        connection: _LiveConnection | None = None
        try:
            connection = self._connection_factory(
                OFFICIAL_ALPACA_IEX_STREAM_URL,
                additional_headers={"Content-Type": "application/json"},
                proxy=None,
                compression=None,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=2 * 1024 * 1024,
                max_queue=64,
            )
            with self._lock:
                self._connection = connection
            if self._stop_requested.is_set() or stop_requested():
                connection.close()
                return

            _expect_success(connection, "connected")
            if self._stop_requested.is_set() or stop_requested():
                return
            connection.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self._credentials.api_key,
                        "secret": self._credentials.secret_key,
                    },
                    separators=(",", ":"),
                )
            )
            _expect_success(connection, "authenticated")
            if self._stop_requested.is_set() or stop_requested():
                return
            connection.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "bars": list(requested_symbols),
                        "updatedBars": list(requested_symbols),
                    },
                    separators=(",", ":"),
                )
            )
            _expect_subscription(connection, requested)

            while not self._stop_requested.is_set() and not stop_requested():
                try:
                    messages = _receive_messages(
                        connection,
                        timeout=_STREAM_RECEIVE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    continue
                for message in messages:
                    message_type = message.get("T")
                    if message_type == "b":
                        self._deliver(
                            message,
                            requested=requested,
                            handler=handler,
                            is_correction=False,
                        )
                    elif message_type == "u":
                        self._deliver(
                            message,
                            requested=requested,
                            handler=handler,
                            is_correction=True,
                        )
                    elif message_type == "subscription":
                        if (
                            _subscription_set(message, "bars") != requested
                            or _subscription_set(message, "updatedBars") != requested
                        ):
                            raise AlpacaDataSourceError(
                                "Alpaca data stream changed its subscription unexpectedly"
                            )
                    else:
                        raise AlpacaDataSourceError(
                            "Alpaca data stream returned an unexpected message type"
                        )
        except AlpacaDataSourceError:
            raise
        except Exception as exc:
            if self._stop_requested.is_set() or stop_requested():
                return
            raise AlpacaDataSourceError(
                _safe_error("Alpaca data stream failed", exc, self._credentials),
                retryable=True,
            ) from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                self._running = False

    def stop(self) -> None:
        """Ask the active data stream to close; repeated calls are harmless."""

        self._stop_requested.set()
        with self._lock:
            connection = self._connection
        if connection is None:
            return
        try:
            connection.close()
        except Exception as exc:
            raise AlpacaDataSourceError(
                _safe_error("Unable to stop Alpaca data stream", exc, self._credentials)
            ) from None
