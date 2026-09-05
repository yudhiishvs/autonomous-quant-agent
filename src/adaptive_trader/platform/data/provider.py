"""Closed, read-only market-data provider and transport boundaries."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

import requests

from adaptive_trader.platform.data.credentials import AlpacaDataCredentials
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError

ALPACA_DATA_REST_ORIGIN = "https://data.alpaca.markets"
ALPACA_DATA_BARS_PATH = "/v2/stocks/bars"
ALPACA_DATA_STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"

_FEED = "iex"
_ADJUSTMENT = "raw"
_TIMEFRAME = "1Min"
_MAX_PAGE_TOKEN_LENGTH = 4096
_MAX_RETRY_AFTER_SECONDS = 300
_REST_TIMEOUT_SECONDS = (5.0, 20.0)
_STREAM_CONTROL_TIMEOUT_SECONDS = 10.0
_MAX_CONTROL_ONLY_FRAMES = 32


class MarketDataProviderError(RuntimeError):
    """Raised when provider data cannot safely cross the adapter boundary."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        provider_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.provider_status = provider_status


class HistoricalStatus(StrEnum):
    """Exhaustive historical request outcome visible to the collector."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"


class StreamEventType(StrEnum):
    """Exhaustive stream event type visible to the collector."""

    CONNECTED = "connected"
    BAR = "bar"
    DISCONNECTED = "disconnected"
    RATE_LIMITED = "rate_limited"


def _utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name.replace(" ", "_"))
    except DomainValidationError:
        raise MarketDataProviderError(f"{field_name} must be a UTC instant") from None


def _symbols(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise MarketDataProviderError("provider symbols must be a nonempty immutable tuple")
    symbols = cast(tuple[object, ...], value)
    if any(type(symbol) is not str for symbol in symbols):
        raise MarketDataProviderError("provider symbols are invalid")
    normalized = cast(tuple[str, ...], symbols)
    if tuple(sorted(set(normalized))) != normalized:
        raise MarketDataProviderError("provider symbols must be sorted and unique")
    if any(
        not symbol.isascii()
        or not 1 <= len(symbol) <= 10
        or not symbol[0].isalpha()
        or symbol != symbol.upper()
        or any(not (character.isalnum() or character == ".") for character in symbol)
        for symbol in normalized
    ):
        raise MarketDataProviderError("provider symbols are invalid")
    return normalized


def _page_token(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > _MAX_PAGE_TOKEN_LENGTH:
        raise MarketDataProviderError("historical page token is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise MarketDataProviderError("historical page token is invalid")
    return value


@dataclass(frozen=True, slots=True)
class HistoricalRequest:
    """One bounded, exact-series historical query."""

    symbols: tuple[str, ...]
    start_at: datetime
    end_at: datetime
    page_token: str | None = None
    limit: int = 10_000
    timeframe: str = _TIMEFRAME
    feed: str = _FEED
    adjustment: str = _ADJUSTMENT

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _symbols(self.symbols))
        start_at = _utc(self.start_at, field_name="historical start")
        end_at = _utc(self.end_at, field_name="historical end")
        if start_at >= end_at:
            raise MarketDataProviderError("historical start must precede end")
        if self.timeframe != _TIMEFRAME or self.feed != _FEED or self.adjustment != _ADJUSTMENT:
            raise MarketDataProviderError("historical request must use exact IEX/raw/1Min")
        if type(self.limit) is not int or not 1 <= self.limit <= 10_000:
            raise MarketDataProviderError("historical request limit is invalid")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)
        object.__setattr__(self, "page_token", _page_token(self.page_token))


@dataclass(frozen=True, slots=True)
class StreamSubscription:
    """Exact IEX one-minute subscription for an immutable symbol set."""

    symbols: tuple[str, ...]
    timeframe: str = _TIMEFRAME
    feed: str = _FEED
    adjustment: str = _ADJUSTMENT

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _symbols(self.symbols))
        if self.timeframe != _TIMEFRAME or self.feed != _FEED or self.adjustment != _ADJUSTMENT:
            raise MarketDataProviderError("stream subscription must use exact IEX/raw/1Min")


@dataclass(frozen=True, slots=True)
class RawBarEnvelope:
    """Provider payload with immutable receipt and correction provenance."""

    payload: Mapping[str, object]
    received_at: datetime
    is_correction: bool = False

    def __post_init__(self) -> None:
        if type(self.payload) is not dict:
            raise MarketDataProviderError("raw bar payload must be an exact dictionary")
        if any(type(key) is not str for key in self.payload):
            raise MarketDataProviderError("raw bar payload keys must be strings")
        if type(self.is_correction) is not bool:
            raise MarketDataProviderError("raw bar correction flag is invalid")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(
            self,
            "received_at",
            _utc(self.received_at, field_name="bar receipt timestamp"),
        )

    def payload_copy(self) -> dict[str, object]:
        """Return the exact mutable mapping required by the canonical normalizer."""

        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class HistoricalResult:
    """One page or an explicit rate-limit result; exceptions represent other failures."""

    status: HistoricalStatus
    bars: tuple[RawBarEnvelope, ...] = ()
    next_page_token: str | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not HistoricalStatus:
            raise MarketDataProviderError("historical result status is invalid")
        if type(self.bars) is not tuple or any(
            type(bar) is not RawBarEnvelope for bar in self.bars
        ):
            raise MarketDataProviderError("historical result bars are invalid")
        object.__setattr__(self, "next_page_token", _page_token(self.next_page_token))
        if self.status is HistoricalStatus.OK:
            if self.retry_after_seconds is not None:
                raise MarketDataProviderError("successful historical result has a retry delay")
        elif self.bars or self.next_page_token is not None:
            raise MarketDataProviderError("rate-limited historical result cannot contain data")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int
            or not 0 <= self.retry_after_seconds <= _MAX_RETRY_AFTER_SECONDS
        ):
            raise MarketDataProviderError("historical retry delay is invalid")


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One decoded control or bar event from a provider stream."""

    event_type: StreamEventType
    occurred_at: datetime
    bar: RawBarEnvelope | None = None
    retry_after_seconds: int | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.event_type) is not StreamEventType:
            raise MarketDataProviderError("stream event type is invalid")
        object.__setattr__(
            self, "occurred_at", _utc(self.occurred_at, field_name="stream event time")
        )
        if self.event_type is StreamEventType.BAR:
            if type(self.bar) is not RawBarEnvelope:
                raise MarketDataProviderError("bar stream event requires a bar")
        elif self.bar is not None:
            raise MarketDataProviderError("control stream event cannot contain a bar")
        if (
            self.event_type is not StreamEventType.RATE_LIMITED
            and self.retry_after_seconds is not None
        ):
            raise MarketDataProviderError("non-rate-limit stream event has a retry delay")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int
            or not 0 <= self.retry_after_seconds <= _MAX_RETRY_AFTER_SECONDS
        ):
            raise MarketDataProviderError("stream retry delay is invalid")


class ProviderStream(Protocol):
    """Pull boundary used by collectors without assuming a network implementation."""

    def receive(self) -> StreamEvent: ...

    def close(self) -> None: ...


class MarketDataProvider(Protocol):
    """Generic historical and real-time market-data source."""

    @property
    def name(self) -> str: ...

    def fetch_historical(self, request: HistoricalRequest) -> HistoricalResult: ...

    def open_stream(self, subscription: StreamSubscription) -> ProviderStream: ...


@dataclass(frozen=True, slots=True)
class AlpacaHistoricalTransportRequest:
    """Fixed-host HTTP request description consumed by an authorized transport."""

    origin: str
    path: str
    query: Mapping[str, str | int]


@dataclass(frozen=True, slots=True)
class AlpacaHistoricalTransportResponse:
    """Minimal response returned by an injected HTTP transport."""

    status_code: int
    payload: object
    received_at: datetime
    retry_after_seconds: int | None = None


class AlpacaHistoricalTransport(Protocol):
    """Only boundary authorized to turn wrapped data credentials into HTTP headers."""

    def send(
        self,
        request: AlpacaHistoricalTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> AlpacaHistoricalTransportResponse: ...


class _HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object: ...


class _HttpSession(Protocol):
    trust_env: bool

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> _HttpResponse: ...

    def close(self) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = next(
        (str(item).strip() for key, item in headers.items() if str(key).lower() == "retry-after"),
        None,
    )
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(_MAX_RETRY_AFTER_SECONDS, math.ceil(seconds))


class RequestsAlpacaHistoricalTransport:
    """Concrete HTTPS transport restricted to Alpaca's official data-bars resource."""

    __slots__ = ("_clock", "_owns_session", "_session")

    def __init__(
        self,
        *,
        session: object | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not callable(clock):
            raise TypeError("historical transport requires a clock")
        if session is None:
            owned = requests.Session()
            owned.trust_env = False
            self._session = cast(_HttpSession, owned)
            self._owns_session = True
        else:
            if not callable(getattr(session, "get", None)):
                raise TypeError("historical transport session is invalid")
            self._session = cast(_HttpSession, session)
            self._owns_session = False
        self._clock = clock

    def send(
        self,
        request: AlpacaHistoricalTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> AlpacaHistoricalTransportResponse:
        if type(request) is not AlpacaHistoricalTransportRequest or (
            request.origin != ALPACA_DATA_REST_ORIGIN or request.path != ALPACA_DATA_BARS_PATH
        ):
            raise MarketDataProviderError("historical transport rejected a nonofficial resource")
        if request.query.get("feed") != _FEED or request.query.get("adjustment") != _ADJUSTMENT:
            raise MarketDataProviderError("historical transport rejected series fallback")
        if type(credentials) is not AlpacaDataCredentials:
            raise TypeError("historical transport requires data-only credentials")
        api_key, secret_key = credentials.transport_material()
        try:
            response = self._session.get(
                f"{ALPACA_DATA_REST_ORIGIN}{ALPACA_DATA_BARS_PATH}",
                headers={
                    "Accept": "application/json",
                    "APCA-API-KEY-ID": api_key.reveal(),
                    "APCA-API-SECRET-KEY": secret_key.reveal(),
                },
                params=request.query,
                timeout=_REST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except Exception as exc:
            raise MarketDataProviderError(
                f"Alpaca historical HTTPS request failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        try:
            payload = response.json()
        except Exception:
            raise MarketDataProviderError("Alpaca historical response was not valid JSON") from None
        return AlpacaHistoricalTransportResponse(
            status_code=response.status_code,
            payload=payload,
            received_at=_utc(self._clock(), field_name="historical transport clock"),
            retry_after_seconds=(
                _retry_after(response.headers) if response.status_code == 429 else None
            ),
        )

    def close(self) -> None:
        """Release the connection pool only when this transport created it."""

        if self._owns_session:
            self._session.close()


@dataclass(frozen=True, slots=True)
class AlpacaStreamTransportRequest:
    """Fixed-host WebSocket connection/subscription description."""

    url: str
    symbols: tuple[str, ...]
    channels: tuple[str, ...] = ("bars", "updatedBars")


class AlpacaStreamConnection(Protocol):
    """Authenticated connection supplied by an injected WebSocket transport."""

    def receive_json(self) -> object | None: ...

    def close(self) -> None: ...


class AlpacaWebSocketTransport(Protocol):
    """Only boundary authorized to authenticate an Alpaca data WebSocket."""

    def open(
        self,
        request: AlpacaStreamTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> AlpacaStreamConnection: ...


class _RawWebSocket(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


WebSocketConnectionFactory = Callable[..., _RawWebSocket]


def _decode_websocket_frame(value: str | bytes) -> object:
    try:
        text = value.decode("utf-8") if type(value) is bytes else value
        return json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise MarketDataProviderError("Alpaca data stream returned invalid JSON") from None


def _single_control_message(connection: _RawWebSocket) -> dict[str, object]:
    decoded = _decode_websocket_frame(connection.recv(timeout=_STREAM_CONTROL_TIMEOUT_SECONDS))
    if type(decoded) is not list or len(decoded) != 1 or type(decoded[0]) is not dict:
        raise MarketDataProviderError("Alpaca data stream returned an invalid control frame")
    return cast(dict[str, object], decoded[0])


class WebsocketsAlpacaTransport:
    """Concrete authenticated WebSocket transport pinned to Alpaca's IEX data host."""

    __slots__ = ("_connection_factory",)

    def __init__(self, *, connection_factory: WebSocketConnectionFactory | None = None) -> None:
        self._connection_factory = connection_factory

    def open(
        self,
        request: AlpacaStreamTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> AlpacaStreamConnection:
        if (
            type(request) is not AlpacaStreamTransportRequest
            or request.url != ALPACA_DATA_STREAM_URL
        ):
            raise MarketDataProviderError("stream transport rejected a nonofficial data endpoint")
        if (
            request.channels != ("bars", "updatedBars")
            or _symbols(request.symbols) != request.symbols
        ):
            raise MarketDataProviderError("stream transport rejected an invalid subscription")
        if type(credentials) is not AlpacaDataCredentials:
            raise TypeError("stream transport requires data-only credentials")
        factory = self._connection_factory
        if factory is None:
            from websockets.sync.client import connect

            factory = cast(WebSocketConnectionFactory, connect)
        try:
            connection = factory(
                ALPACA_DATA_STREAM_URL,
                open_timeout=_STREAM_CONTROL_TIMEOUT_SECONDS,
                proxy=None,
            )
            connected = _single_control_message(connection)
            if connected != {"T": "success", "msg": "connected"}:
                raise MarketDataProviderError("Alpaca data stream connection greeting failed")
            api_key, secret_key = credentials.transport_material()
            connection.send(
                json.dumps(
                    {
                        "action": "auth",
                        "key": api_key.reveal(),
                        "secret": secret_key.reveal(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            authenticated = _single_control_message(connection)
            if authenticated != {"T": "success", "msg": "authenticated"}:
                raise MarketDataProviderError("Alpaca data stream authentication failed")
            connection.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "bars": list(request.symbols),
                        "updatedBars": list(request.symbols),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            subscribed = _single_control_message(connection)
            if (
                subscribed.get("T") != "subscription"
                or subscribed.get("bars") != list(request.symbols)
                or subscribed.get("updatedBars") != list(request.symbols)
            ):
                raise MarketDataProviderError("Alpaca data stream subscription was incomplete")
        except MarketDataProviderError:
            if "connection" in locals():
                connection.close()
            raise
        except Exception as exc:
            if "connection" in locals():
                connection.close()
            raise MarketDataProviderError(
                f"Alpaca WebSocket connection failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return _JsonAlpacaStreamConnection(connection)


class _JsonAlpacaStreamConnection:
    __slots__ = ("_connection",)

    def __init__(self, connection: _RawWebSocket) -> None:
        self._connection = connection

    def receive_json(self) -> object | None:
        try:
            return _decode_websocket_frame(self._connection.recv())
        except EOFError:
            return None

    def close(self) -> None:
        self._connection.close()


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class AlpacaMarketDataProvider:
    """Read-only Alpaca adapter with fixed official data endpoints and injected I/O."""

    __slots__ = ("_clock", "_credentials", "_historical_transport", "_stream_transport")

    def __init__(
        self,
        credentials: AlpacaDataCredentials,
        *,
        historical_transport: AlpacaHistoricalTransport,
        websocket_transport: AlpacaWebSocketTransport,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(credentials) is not AlpacaDataCredentials:
            raise TypeError("Alpaca provider requires data-only credentials")
        if not callable(getattr(historical_transport, "send", None)):
            raise TypeError("Alpaca provider requires an injected historical transport")
        if not callable(getattr(websocket_transport, "open", None)):
            raise TypeError("Alpaca provider requires an injected WebSocket transport")
        if not callable(clock):
            raise TypeError("Alpaca provider requires an injected clock")
        self._credentials = credentials
        self._historical_transport = historical_transport
        self._stream_transport = websocket_transport
        self._clock = clock

    @property
    def name(self) -> str:
        return "alpaca"

    def fetch_historical(self, request: HistoricalRequest) -> HistoricalResult:
        if type(request) is not HistoricalRequest:
            raise MarketDataProviderError("historical request contract is invalid")
        query: dict[str, str | int] = {
            "symbols": ",".join(request.symbols),
            "timeframe": _TIMEFRAME,
            "start": _rfc3339(request.start_at),
            "end": _rfc3339(request.end_at),
            "limit": request.limit,
            "adjustment": _ADJUSTMENT,
            "feed": _FEED,
            "sort": "asc",
        }
        if request.page_token is not None:
            query["page_token"] = request.page_token
        try:
            response = self._historical_transport.send(
                AlpacaHistoricalTransportRequest(
                    origin=ALPACA_DATA_REST_ORIGIN,
                    path=ALPACA_DATA_BARS_PATH,
                    query=query,
                ),
                self._credentials,
            )
        except MarketDataProviderError:
            raise
        except Exception as exc:
            raise MarketDataProviderError(
                f"Alpaca historical transport failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        if type(response) is not AlpacaHistoricalTransportResponse:
            raise MarketDataProviderError(
                "Alpaca historical transport returned an invalid response"
            )
        received_at = _utc(response.received_at, field_name="historical response receipt")
        if type(response.status_code) is not int:
            raise MarketDataProviderError("Alpaca historical response status is invalid")
        if response.status_code == 429:
            return HistoricalResult(
                status=HistoricalStatus.RATE_LIMITED,
                retry_after_seconds=response.retry_after_seconds,
            )
        if response.status_code != 200:
            raise MarketDataProviderError(
                f"Alpaca historical request returned HTTP {response.status_code}",
                retryable=response.status_code in {408, 425} or response.status_code >= 500,
                provider_status=response.status_code,
            )
        return self._decode_historical(response.payload, request=request, received_at=received_at)

    def _decode_historical(
        self,
        payload: object,
        *,
        request: HistoricalRequest,
        received_at: datetime,
    ) -> HistoricalResult:
        if type(payload) is not dict or set(payload) - {"bars", "next_page_token"}:
            raise MarketDataProviderError("Alpaca historical response has an invalid shape")
        body = cast(dict[object, object], payload)
        raw_groups = body.get("bars")
        if type(raw_groups) is not dict:
            raise MarketDataProviderError("Alpaca historical bars have an invalid shape")
        groups = cast(dict[object, object], raw_groups)
        if any(type(symbol) is not str or symbol not in request.symbols for symbol in groups):
            raise MarketDataProviderError("Alpaca returned an unrequested symbol")
        bars: list[RawBarEnvelope] = []
        for symbol in request.symbols:
            raw_bars = groups.get(symbol, [])
            if type(raw_bars) is not list:
                raise MarketDataProviderError("Alpaca historical symbol group is invalid")
            for raw_bar in cast(list[object], raw_bars):
                if type(raw_bar) is not dict:
                    raise MarketDataProviderError("Alpaca historical bar is invalid")
                copied = cast(dict[str, object], dict(cast(dict[object, object], raw_bar)))
                if "S" not in copied and "symbol" not in copied:
                    copied["S"] = symbol
                bars.append(RawBarEnvelope(payload=copied, received_at=received_at))
        return HistoricalResult(
            status=HistoricalStatus.OK,
            bars=tuple(bars),
            next_page_token=_page_token(body.get("next_page_token")),
        )

    def open_stream(self, subscription: StreamSubscription) -> ProviderStream:
        if type(subscription) is not StreamSubscription:
            raise MarketDataProviderError("stream subscription contract is invalid")
        try:
            connection = self._stream_transport.open(
                AlpacaStreamTransportRequest(
                    url=ALPACA_DATA_STREAM_URL,
                    symbols=subscription.symbols,
                ),
                self._credentials,
            )
        except MarketDataProviderError:
            raise
        except Exception as exc:
            raise MarketDataProviderError(
                f"Alpaca stream transport failed ({type(exc).__name__})",
                retryable=True,
            ) from None
        return _AlpacaProviderStream(connection, subscription.symbols, self._clock)


class _AlpacaProviderStream:
    __slots__ = ("_clock", "_closed", "_connection", "_pending", "_symbols")

    def __init__(
        self,
        connection: AlpacaStreamConnection,
        symbols: tuple[str, ...],
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(getattr(connection, "receive_json", None)) or not callable(
            getattr(connection, "close", None)
        ):
            raise MarketDataProviderError("Alpaca stream connection is invalid")
        self._connection = connection
        self._symbols = frozenset(symbols)
        self._clock = clock
        self._pending: list[StreamEvent] = [
            StreamEvent(
                event_type=StreamEventType.CONNECTED, occurred_at=_utc(clock(), field_name="clock")
            )
        ]
        self._closed = False

    def receive(self) -> StreamEvent:
        if self._pending:
            return self._pending.pop(0)
        if self._closed:
            return StreamEvent(
                event_type=StreamEventType.DISCONNECTED,
                occurred_at=_utc(self._clock(), field_name="clock"),
                reason_code="closed",
            )
        for _frame_count in range(_MAX_CONTROL_ONLY_FRAMES):
            try:
                frame = self._connection.receive_json()
            except Exception as exc:
                self._closed = True
                with suppress(Exception):
                    self._connection.close()
                return StreamEvent(
                    event_type=StreamEventType.DISCONNECTED,
                    occurred_at=_utc(self._clock(), field_name="clock"),
                    reason_code=f"transport_{type(exc).__name__.lower()}",
                )
            if frame is None:
                self._closed = True
                with suppress(Exception):
                    self._connection.close()
                return StreamEvent(
                    event_type=StreamEventType.DISCONNECTED,
                    occurred_at=_utc(self._clock(), field_name="clock"),
                    reason_code="remote_closed",
                )
            messages = frame if type(frame) is list else [frame]
            if not messages or any(type(message) is not dict for message in messages):
                raise MarketDataProviderError("Alpaca stream frame has an invalid shape")
            now = _utc(self._clock(), field_name="clock")
            for raw_message in cast(list[dict[object, object]], messages):
                message = cast(dict[str, object], dict(raw_message))
                event_type = message.get("T")
                if event_type in {"b", "u"}:
                    symbol = message.get("S")
                    if type(symbol) is not str or symbol not in self._symbols:
                        raise MarketDataProviderError(
                            "Alpaca stream returned an unrequested symbol"
                        )
                    self._pending.append(
                        StreamEvent(
                            event_type=StreamEventType.BAR,
                            occurred_at=now,
                            bar=RawBarEnvelope(
                                payload=message,
                                received_at=now,
                                is_correction=event_type == "u",
                            ),
                        )
                    )
                elif event_type == "error" and message.get("code") == 429:
                    self._pending.append(
                        StreamEvent(
                            event_type=StreamEventType.RATE_LIMITED,
                            occurred_at=now,
                            reason_code="provider_rate_limit",
                        )
                    )
                elif event_type not in {"success", "subscription"}:
                    raise MarketDataProviderError("Alpaca stream returned an unsupported message")
            if self._pending:
                return self._pending.pop(0)
        raise MarketDataProviderError("Alpaca stream control-frame limit exceeded")

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True


class FixtureMarketDataProvider:
    """Deterministic zero-I/O provider for ordinary tests and offline profiles."""

    __slots__ = ("_bars", "_stream_events")

    def __init__(
        self,
        *,
        bars: tuple[RawBarEnvelope, ...] = (),
        stream_events: tuple[StreamEvent, ...] = (),
    ) -> None:
        if type(bars) is not tuple or any(type(bar) is not RawBarEnvelope for bar in bars):
            raise MarketDataProviderError("fixture bars are invalid")
        if type(stream_events) is not tuple or any(
            type(event) is not StreamEvent for event in stream_events
        ):
            raise MarketDataProviderError("fixture stream events are invalid")
        self._bars = bars
        self._stream_events = stream_events

    @property
    def name(self) -> str:
        return "fixture"

    def fetch_historical(self, request: HistoricalRequest) -> HistoricalResult:
        if type(request) is not HistoricalRequest:
            raise MarketDataProviderError("historical request contract is invalid")
        selected: list[RawBarEnvelope] = []
        for envelope in self._bars:
            payload = envelope.payload
            symbol = payload.get("S", payload.get("symbol"))
            raw_timestamp = payload.get("t", payload.get("timestamp"))
            if type(symbol) is not str or symbol not in request.symbols:
                continue
            if type(raw_timestamp) is datetime:
                timestamp = _utc(raw_timestamp, field_name="fixture timestamp")
            elif type(raw_timestamp) is str:
                try:
                    timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                except ValueError:
                    raise MarketDataProviderError("fixture timestamp is invalid") from None
                timestamp = _utc(timestamp, field_name="fixture timestamp")
            else:
                raise MarketDataProviderError("fixture timestamp is invalid")
            if request.start_at <= timestamp < request.end_at:
                selected.append(envelope)
        selected.sort(
            key=lambda envelope: (
                str(envelope.payload.get("t", envelope.payload.get("timestamp"))),
                str(envelope.payload.get("S", envelope.payload.get("symbol"))),
            )
        )
        return HistoricalResult(status=HistoricalStatus.OK, bars=tuple(selected))

    def open_stream(self, subscription: StreamSubscription) -> ProviderStream:
        if type(subscription) is not StreamSubscription:
            raise MarketDataProviderError("stream subscription contract is invalid")
        for event in self._stream_events:
            if event.bar is not None:
                symbol = event.bar.payload.get("S", event.bar.payload.get("symbol"))
                if symbol not in subscription.symbols:
                    raise MarketDataProviderError("fixture stream contains an unrequested symbol")
        return _FixtureProviderStream(self._stream_events)


class _FixtureProviderStream:
    __slots__ = ("_closed", "_events", "_offset")

    def __init__(self, events: tuple[StreamEvent, ...]) -> None:
        self._events = events
        self._offset = 0
        self._closed = False

    def receive(self) -> StreamEvent:
        if self._closed or self._offset >= len(self._events):
            raise StopIteration
        event = self._events[self._offset]
        self._offset += 1
        return event

    def close(self) -> None:
        self._closed = True


__all__ = [
    "ALPACA_DATA_BARS_PATH",
    "ALPACA_DATA_REST_ORIGIN",
    "ALPACA_DATA_STREAM_URL",
    "AlpacaHistoricalTransport",
    "AlpacaHistoricalTransportRequest",
    "AlpacaHistoricalTransportResponse",
    "AlpacaMarketDataProvider",
    "AlpacaStreamConnection",
    "AlpacaStreamTransportRequest",
    "AlpacaWebSocketTransport",
    "FixtureMarketDataProvider",
    "HistoricalRequest",
    "HistoricalResult",
    "HistoricalStatus",
    "MarketDataProvider",
    "MarketDataProviderError",
    "ProviderStream",
    "RawBarEnvelope",
    "RequestsAlpacaHistoricalTransport",
    "StreamEvent",
    "StreamEventType",
    "StreamSubscription",
    "WebSocketConnectionFactory",
    "WebsocketsAlpacaTransport",
]
