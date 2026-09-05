"""Zero-network tests for the closed provider and credential boundaries."""

from __future__ import annotations

import ast
import pickle
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from adaptive_trader.platform.data.credentials import (
    AlpacaDataCredentialError,
    AlpacaDataCredentials,
)
from adaptive_trader.platform.data.provider import (
    ALPACA_DATA_BARS_PATH,
    ALPACA_DATA_REST_ORIGIN,
    ALPACA_DATA_STREAM_URL,
    AlpacaHistoricalTransportRequest,
    AlpacaHistoricalTransportResponse,
    AlpacaMarketDataProvider,
    AlpacaStreamTransportRequest,
    FixtureMarketDataProvider,
    HistoricalRequest,
    HistoricalStatus,
    MarketDataProviderError,
    RawBarEnvelope,
    RequestsAlpacaHistoricalTransport,
    StreamEvent,
    StreamEventType,
    StreamSubscription,
    WebsocketsAlpacaTransport,
)
from adaptive_trader.platform.security import SecretFileReference, SecretFileVariable

_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_RECEIVED = _START + timedelta(minutes=5)


def _credentials(tmp_path: Path) -> AlpacaDataCredentials:
    api_key = tmp_path / "data_api_key"
    secret_key = tmp_path / "data_secret_key"
    api_key.write_text("fixture-api-value\n", encoding="utf-8")
    secret_key.write_text("fixture-secret-value\n", encoding="utf-8")
    api_key.chmod(0o600)
    secret_key.chmod(0o600)
    return AlpacaDataCredentials.load(
        api_key_file=SecretFileReference.from_path(
            api_key,
            source=SecretFileVariable.ALPACA_DATA_API_KEY,
            application_root=tmp_path,
        ),
        secret_key_file=SecretFileReference.from_path(
            secret_key,
            source=SecretFileVariable.ALPACA_DATA_SECRET_KEY,
            application_root=tmp_path,
        ),
    )


def _bar(symbol: str = "AMD", minute: int = 0) -> dict[str, object]:
    timestamp = _START + timedelta(minutes=minute)
    return {
        "S": symbol,
        "t": timestamp.isoformat().replace("+00:00", "Z"),
        "o": 100,
        "h": 102,
        "l": 99,
        "c": 101,
        "v": 1_000,
        "n": 20,
        "vw": 100.5,
    }


def test_credentials_are_file_backed_immutable_redacted_and_nonserializable(
    tmp_path: Path,
) -> None:
    credentials = _credentials(tmp_path)

    assert "fixture-api-value" not in repr(credentials)
    assert "fixture-secret-value" not in str(credentials)
    assert all("fixture" not in repr(value) for value in credentials.transport_material())
    with pytest.raises(AttributeError, match="immutable"):
        credentials.extra = "value"
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(credentials)


def test_credentials_reject_paper_trading_secret_sources(tmp_path: Path) -> None:
    value = tmp_path / "paper_api_key"
    value.write_text("fixture-paper-value\n", encoding="utf-8")
    value.chmod(0o600)
    paper_reference = SecretFileReference.from_path(
        value,
        source=SecretFileVariable.ALPACA_PAPER_API_KEY,
        application_root=tmp_path,
    )
    data_reference = SecretFileReference.from_path(
        value,
        source=SecretFileVariable.ALPACA_DATA_SECRET_KEY,
        application_root=tmp_path,
    )

    with pytest.raises(AlpacaDataCredentialError, match="data API key"):
        AlpacaDataCredentials.load(
            api_key_file=paper_reference,
            secret_key_file=data_reference,
        )


class _HistoricalTransport:
    def __init__(self, response: AlpacaHistoricalTransportResponse) -> None:
        self.response = response
        self.calls: list[tuple[AlpacaHistoricalTransportRequest, AlpacaDataCredentials]] = []

    def send(
        self,
        request: AlpacaHistoricalTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> AlpacaHistoricalTransportResponse:
        self.calls.append((request, credentials))
        return self.response


class _Connection:
    def __init__(self, frames: list[object | None]) -> None:
        self.frames = frames
        self.closed = False

    def receive_json(self) -> object | None:
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


class _WebSocketTransport:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.calls: list[tuple[AlpacaStreamTransportRequest, AlpacaDataCredentials]] = []

    def open(
        self,
        request: AlpacaStreamTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> _Connection:
        self.calls.append((request, credentials))
        return self.connection


def test_alpaca_historical_adapter_uses_only_fixed_exact_data_contract(tmp_path: Path) -> None:
    credentials = _credentials(tmp_path)
    historical = _HistoricalTransport(
        AlpacaHistoricalTransportResponse(
            status_code=200,
            payload={"bars": {"AMD": [_bar()]}, "next_page_token": "next-page"},
            received_at=_RECEIVED,
        )
    )
    websocket = _WebSocketTransport(_Connection([]))
    provider = AlpacaMarketDataProvider(
        credentials,
        historical_transport=historical,
        websocket_transport=websocket,
        clock=lambda: _RECEIVED,
    )

    result = provider.fetch_historical(
        HistoricalRequest(
            symbols=("AMD", "NVDA"),
            start_at=_START,
            end_at=_START + timedelta(minutes=5),
        )
    )

    assert result.status is HistoricalStatus.OK
    assert len(result.bars) == 1
    assert result.next_page_token == "next-page"
    request, passed_credentials = historical.calls[0]
    assert request.origin == ALPACA_DATA_REST_ORIGIN
    assert request.path == ALPACA_DATA_BARS_PATH
    assert request.query["symbols"] == "AMD,NVDA"
    assert request.query["timeframe"] == "1Min"
    assert request.query["feed"] == "iex"
    assert request.query["adjustment"] == "raw"
    assert passed_credentials is credentials


def test_alpaca_historical_adapter_returns_rate_limit_explicitly(tmp_path: Path) -> None:
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(
                status_code=429,
                payload={},
                received_at=_RECEIVED,
                retry_after_seconds=47,
            )
        ),
        websocket_transport=_WebSocketTransport(_Connection([])),
        clock=lambda: _RECEIVED,
    )

    result = provider.fetch_historical(
        HistoricalRequest(
            symbols=("AMD",),
            start_at=_START,
            end_at=_START + timedelta(minutes=1),
        )
    )

    assert result.status is HistoricalStatus.RATE_LIMITED
    assert result.retry_after_seconds == 47
    assert result.bars == ()


def test_alpaca_stream_adapter_decodes_bars_updates_rate_limits_and_disconnects(
    tmp_path: Path,
) -> None:
    connection = _Connection(
        [
            [{**_bar(), "T": "b"}],
            [{**_bar(), "T": "u"}],
            [{"T": "error", "code": 429, "msg": "rate limit"}],
            None,
        ]
    )
    websocket = _WebSocketTransport(connection)
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(200, {"bars": {}}, _RECEIVED)
        ),
        websocket_transport=websocket,
        clock=lambda: _RECEIVED,
    )
    stream = provider.open_stream(StreamSubscription(symbols=("AMD",)))

    assert stream.receive().event_type is StreamEventType.CONNECTED
    first = stream.receive()
    update = stream.receive()
    assert first.event_type is StreamEventType.BAR
    assert update.event_type is StreamEventType.BAR
    assert update.bar is not None and update.bar.is_correction is True
    assert stream.receive().event_type is StreamEventType.RATE_LIMITED
    assert stream.receive().event_type is StreamEventType.DISCONNECTED
    request, _credentials_passed = websocket.calls[0]
    assert request.url == ALPACA_DATA_STREAM_URL
    assert request.symbols == ("AMD",)
    assert request.channels == ("bars", "updatedBars")
    stream.close()
    assert connection.closed is True


def test_fixture_provider_is_deterministic_and_enforces_subscription() -> None:
    inside = RawBarEnvelope(payload=_bar(), received_at=_RECEIVED)
    outside = RawBarEnvelope(payload=_bar(minute=6), received_at=_RECEIVED + timedelta(minutes=2))
    fixture = FixtureMarketDataProvider(
        bars=(outside, inside),
        stream_events=(
            StreamEvent(
                event_type=StreamEventType.BAR,
                occurred_at=_RECEIVED,
                bar=inside,
            ),
        ),
    )

    result = fixture.fetch_historical(
        HistoricalRequest(
            symbols=("AMD",),
            start_at=_START,
            end_at=_START + timedelta(minutes=5),
        )
    )
    stream = fixture.open_stream(StreamSubscription(symbols=("AMD",)))

    assert result.bars == (inside,)
    assert stream.receive().bar is inside
    with pytest.raises(StopIteration):
        stream.receive()


class _Response:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {}

    def json(self) -> object:
        return {"bars": {"AMD": [_bar()]}, "next_page_token": None}


class _Session:
    trust_env = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> _Response:
        self.calls.append(
            (
                url,
                {
                    "headers": headers,
                    "params": params,
                    "timeout": timeout,
                    "allow_redirects": allow_redirects,
                },
            )
        )
        return _Response()

    def close(self) -> None:
        raise AssertionError("injected sessions are not owned")


def test_concrete_historical_transport_is_fixed_host_and_read_only(tmp_path: Path) -> None:
    session = _Session()
    transport = RequestsAlpacaHistoricalTransport(session=session, clock=lambda: _RECEIVED)
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=transport,
        websocket_transport=_WebSocketTransport(_Connection([])),
        clock=lambda: _RECEIVED,
    )

    result = provider.fetch_historical(
        HistoricalRequest(
            symbols=("AMD",),
            start_at=_START,
            end_at=_START + timedelta(minutes=1),
        )
    )

    assert result.status is HistoricalStatus.OK
    url, kwargs = session.calls[0]
    assert url == f"{ALPACA_DATA_REST_ORIGIN}{ALPACA_DATA_BARS_PATH}"
    assert kwargs["allow_redirects"] is False
    assert kwargs["params"] == {
        "symbols": "AMD",
        "timeframe": "1Min",
        "start": "2026-07-06T13:30:00.000000Z",
        "end": "2026-07-06T13:31:00.000000Z",
        "limit": 10_000,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    }


class _RawConnection:
    def __init__(self) -> None:
        self.frames = [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","bars":["AMD"],"updatedBars":["AMD"]}]',
            '[{"T":"b","S":"AMD"}]',
        ]
        self.sent: list[str] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


def test_concrete_websocket_transport_authenticates_only_fixed_iex_data_host(
    tmp_path: Path,
) -> None:
    connection = _RawConnection()
    calls: list[tuple[str, dict[str, object]]] = []

    def factory(url: str, **kwargs: object) -> _RawConnection:
        calls.append((url, kwargs))
        return connection

    transport = WebsocketsAlpacaTransport(connection_factory=factory)
    opened = transport.open(
        AlpacaStreamTransportRequest(url=ALPACA_DATA_STREAM_URL, symbols=("AMD",)),
        _credentials(tmp_path),
    )

    assert calls == [(ALPACA_DATA_STREAM_URL, {"open_timeout": 10.0, "proxy": None})]
    assert '"action":"auth"' in connection.sent[0]
    assert connection.sent[1] == ('{"action":"subscribe","bars":["AMD"],"updatedBars":["AMD"]}')
    assert opened.receive_json() == [{"T": "b", "S": "AMD"}]
    opened.close()
    assert connection.closed is True


def test_concrete_websocket_transport_rejects_missing_connection_greeting(
    tmp_path: Path,
) -> None:
    connection = _RawConnection()
    connection.frames[0] = '[{"T":"success","msg":"authenticated"}]'
    transport = WebsocketsAlpacaTransport(connection_factory=lambda *_args, **_kwargs: connection)

    with pytest.raises(MarketDataProviderError, match="connection greeting"):
        transport.open(
            AlpacaStreamTransportRequest(url=ALPACA_DATA_STREAM_URL, symbols=("AMD",)),
            _credentials(tmp_path),
        )

    assert connection.sent == []
    assert connection.closed is True


class _FailingHistoricalTransport:
    def __init__(self, error: MarketDataProviderError) -> None:
        self.error = error

    def send(
        self,
        request: AlpacaHistoricalTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> AlpacaHistoricalTransportResponse:
        del request, credentials
        raise self.error


class _FailingWebSocketTransport:
    def __init__(self, error: MarketDataProviderError) -> None:
        self.error = error

    def open(
        self,
        request: AlpacaStreamTransportRequest,
        credentials: AlpacaDataCredentials,
    ) -> _Connection:
        del request, credentials
        raise self.error


def test_provider_preserves_nonretryable_transport_errors(tmp_path: Path) -> None:
    historical_error = MarketDataProviderError("historical contract rejected")
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_FailingHistoricalTransport(historical_error),
        websocket_transport=_WebSocketTransport(_Connection([])),
        clock=lambda: _RECEIVED,
    )

    with pytest.raises(MarketDataProviderError) as historical_failure:
        provider.fetch_historical(
            HistoricalRequest(
                symbols=("AMD",),
                start_at=_START,
                end_at=_START + timedelta(minutes=1),
            )
        )
    assert historical_failure.value is historical_error
    assert historical_failure.value.retryable is False

    stream_error = MarketDataProviderError("stream authentication rejected")
    stream_provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(200, {"bars": {}}, _RECEIVED)
        ),
        websocket_transport=_FailingWebSocketTransport(stream_error),
        clock=lambda: _RECEIVED,
    )

    with pytest.raises(MarketDataProviderError) as stream_failure:
        stream_provider.open_stream(StreamSubscription(symbols=("AMD",)))
    assert stream_failure.value is stream_error
    assert stream_failure.value.retryable is False


def test_new_provider_modules_have_no_network_or_trading_client_imports() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = (
        root / "src/adaptive_trader/platform/data/provider.py",
        root / "src/adaptive_trader/platform/data/collector.py",
        root / "src/adaptive_trader/platform/data/credentials.py",
    )
    forbidden = (
        "alpaca.trading",
        "adaptive_trader.broker",
        "adaptive_trader.execution",
    )

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        )
        assert all(not imported.startswith(prefix) for imported in imports for prefix in forbidden)
