"""Transport corruption cannot masquerade as subscribed canonical observations."""

import pytest

from adaptive_trader.platform.data.provider import (
    ALPACA_DATA_STREAM_URL,
    AlpacaHistoricalTransportResponse,
    AlpacaMarketDataProvider,
    AlpacaStreamTransportRequest,
    MarketDataProviderError,
    StreamEventType,
    StreamSubscription,
    WebsocketsAlpacaTransport,
)
from tests.unit.test_platform_data_provider import (
    _RECEIVED,
    _Connection,
    _credentials,
    _HistoricalTransport,
    _RawConnection,
    _WebSocketTransport,
)


@pytest.mark.parametrize(
    "frame",
    [
        [],
        [1],
        "invalid",
        {"T": "b", "S": "QQQ"},
        {"T": "u", "S": None},
        {"T": "trade"},
        {"T": "error", "code": 401},
    ],
)
def test_bad_stream_frame_does_not_emit_a_bar_and_can_be_closed(tmp_path, frame):
    connection = _Connection([frame])
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(200, {"bars": {}}, _RECEIVED)
        ),
        websocket_transport=_WebSocketTransport(connection),
        clock=lambda: _RECEIVED,
    )
    stream = provider.open_stream(StreamSubscription(symbols=("AMD",)))
    assert stream.receive().event_type is StreamEventType.CONNECTED
    with pytest.raises(MarketDataProviderError):
        stream.receive()
    stream.close()
    stream.close()
    assert connection.closed
    assert stream.receive().event_type is StreamEventType.DISCONNECTED


@pytest.mark.parametrize("frames", [[None], []])
def test_remote_close_and_transport_exception_release_connection(tmp_path, frames):
    connection = _Connection(frames)
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(200, {"bars": {}}, _RECEIVED)
        ),
        websocket_transport=_WebSocketTransport(connection),
        clock=lambda: _RECEIVED,
    )
    stream = provider.open_stream(StreamSubscription(symbols=("AMD",)))
    assert stream.receive().event_type is StreamEventType.CONNECTED
    assert stream.receive().event_type is StreamEventType.DISCONNECTED
    assert connection.closed
    assert stream.receive().event_type is StreamEventType.DISCONNECTED


@pytest.mark.parametrize(
    "index, frame",
    [
        (0, "not json"),
        (0, "[]"),
        (0, '[{"T":"error","code":401}]'),
        (1, '[{"T":"success","msg":"connected"}]'),
        (1, '[{"T":"error","code":402}]'),
        (2, '[{"T":"subscription","bars":["AMD"],"updatedBars":[]}]'),
        (2, '[{"T":"subscription","bars":["AMD","QQQ"],"updatedBars":["AMD"]}]'),
    ],
)
def test_failed_handshake_closes_socket_without_exposing_authenticated_stream(
    tmp_path, index, frame
):
    connection = _RawConnection()
    connection.frames[index] = frame
    transport = WebsocketsAlpacaTransport(connection_factory=lambda *_args, **_kwargs: connection)
    with pytest.raises(MarketDataProviderError):
        transport.open(
            AlpacaStreamTransportRequest(url=ALPACA_DATA_STREAM_URL, symbols=("AMD",)),
            _credentials(tmp_path),
        )
    assert connection.closed
    if index == 0:
        assert connection.sent == []
