"""Exact data-series authority and bounded HTTP failures at the credential boundary."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from adaptive_trader.platform.data.provider import (
    ALPACA_DATA_BARS_PATH,
    ALPACA_DATA_REST_ORIGIN,
    AlpacaHistoricalTransportRequest,
    AlpacaHistoricalTransportResponse,
    AlpacaMarketDataProvider,
    HistoricalRequest,
    MarketDataProviderError,
    RequestsAlpacaHistoricalTransport,
)
from tests.unit.test_platform_data_provider import (
    _RECEIVED,
    _START,
    _Connection,
    _credentials,
    _HistoricalTransport,
    _WebSocketTransport,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"symbols": ()},
        {"symbols": ["AMD"]},
        {"symbols": (1,)},
        {"symbols": ("NVDA", "AMD")},
        {"symbols": ("AMD", "AMD")},
        {"symbols": ("amd",)},
        {"symbols": ("../AMD",)},
        {"symbols": ("ÄMD",)},
        {"limit": True},
        {"limit": 0},
        {"limit": -1},
        {"limit": 10_001},
        {"page_token": ""},
        {"page_token": "unsafe\npage"},
        {"page_token": 1},
        {"page_token": "x" * 4097},
        {"start_at": _START.replace(tzinfo=None)},
        {"end_at": _START},
        {"end_at": _START - timedelta(minutes=1)},
        {"timeframe": "1Hour"},
        {"feed": "sip"},
        {"adjustment": "all"},
    ],
)
def test_historical_contract_rejects_unbounded_or_alternative_series(changes):
    values = dict(symbols=("AMD",), start_at=_START, end_at=_RECEIVED)
    with pytest.raises(MarketDataProviderError):
        HistoricalRequest(**(values | changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "https://example.invalid"),
        ("path", "/v2/orders"),
        ("feed", "sip"),
        ("adjustment", "all"),
        ("timeframe", "1Hour"),
    ],
)
def test_transport_rejects_changed_authority_before_http(tmp_path, field, value):
    calls = []
    session = SimpleNamespace(get=lambda *args, **kwargs: calls.append((args, kwargs)))
    transport = RequestsAlpacaHistoricalTransport(session=session, clock=lambda: _RECEIVED)
    query = dict(feed="iex", adjustment="raw", timeframe="1Min")
    values = dict(origin=ALPACA_DATA_REST_ORIGIN, path=ALPACA_DATA_BARS_PATH, query=query)
    if field in query:
        query[field] = value
    else:
        values[field] = value
    with pytest.raises(MarketDataProviderError):
        transport.send(AlpacaHistoricalTransportRequest(**values), _credentials(tmp_path))
    assert calls == []


@pytest.mark.parametrize(
    "header,expected",
    [
        ("1.1", 2),
        ("100000", 300),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("not-seconds", None),
    ],
)
def test_retry_after_is_bounded_and_nonfinite_values_do_not_escape(tmp_path, header, expected):
    response = SimpleNamespace(status_code=429, headers={"Retry-After": header}, json=lambda: {})
    transport = RequestsAlpacaHistoricalTransport(
        session=SimpleNamespace(get=lambda *a, **k: response), clock=lambda: _RECEIVED
    )
    request = AlpacaHistoricalTransportRequest(
        ALPACA_DATA_REST_ORIGIN,
        ALPACA_DATA_BARS_PATH,
        dict(feed="iex", adjustment="raw", timeframe="1Min"),
    )
    result = transport.send(request, _credentials(tmp_path))
    assert result.retry_after_seconds == expected


@pytest.mark.parametrize(
    "status,retryable",
    [
        (301, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (425, True),
        (500, True),
        (503, True),
    ],
)
def test_http_failure_classification_never_exposes_payload(tmp_path, status, retryable):
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(status, {"diagnostic": "PRIVATE_SENTINEL"}, _RECEIVED)
        ),
        websocket_transport=_WebSocketTransport(_Connection([])),
    )
    with pytest.raises(MarketDataProviderError) as captured:
        provider.fetch_historical(HistoricalRequest(("AMD",), _START, _RECEIVED))
    assert captured.value.retryable is retryable
    assert captured.value.provider_status == status
    assert "PRIVATE_SENTINEL" not in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"bars": []},
        {"bars": {"UNREQUESTED": []}},
        {"bars": {"AMD": "bad"}},
        {"bars": {"AMD": [None]}},
        {"bars": {}, "next_page_token": "unsafe\npage"},
    ],
)
def test_malformed_historical_response_cannot_produce_a_successful_page(tmp_path, payload):
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(200, payload, _RECEIVED)
        ),
        websocket_transport=_WebSocketTransport(_Connection([])),
    )
    with pytest.raises(MarketDataProviderError):
        provider.fetch_historical(HistoricalRequest(("AMD",), _START, _RECEIVED))
