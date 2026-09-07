"""Malformed upstream pages and immutable canonical state fail closed at public boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from adaptive_trader.platform.data.provider import (
    AlpacaHistoricalTransportResponse,
    AlpacaMarketDataProvider,
    HistoricalRequest,
    HistoricalResult,
    HistoricalStatus,
    MarketDataProviderError,
    RawBarEnvelope,
    StreamEvent,
    StreamEventType,
)
from adaptive_trader.platform.storage.market_data import (
    MarketDataIntegrityError,
    MarketDataRepository,
    MarketDataValidationError,
)
from adaptive_trader.platform.storage.tables import aqa_bar_events
from tests.unit.test_platform_data_provider import (
    _RECEIVED,
    _START,
    _Connection,
    _credentials,
    _HistoricalTransport,
    _WebSocketTransport,
)
from tests.unit.test_platform_market_data_repository import _bar, repository, sqlite_engine

__all__ = ["repository", "sqlite_engine"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"bars": []},
        {"bars": {}, "extra": 1},
        {"bars": {"QQQ": []}},
        {"bars": {"AMD": {}}},
        {"bars": {"AMD": [None]}},
        {"bars": {"AMD": [{"S": "QQQ"}]}},
        {"bars": {"AMD": [{"symbol": "QQQ"}]}},
        {"bars": {"AMD": [{"S": "AMD", "symbol": "QQQ"}]}},
        {"bars": {"AMD": [{"S": "QQQ", "symbol": "AMD"}]}},
        {"bars": {"AMD": [{"S": None}]}},
        {"bars": {}, "next_page_token": 1},
        {"bars": {}, "next_page_token": ""},
    ],
)
def test_malformed_historical_response_cannot_become_success(tmp_path, payload):
    transport = _HistoricalTransport(AlpacaHistoricalTransportResponse(200, payload, _RECEIVED))
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=transport,
        websocket_transport=_WebSocketTransport(_Connection([])),
    )
    with pytest.raises(MarketDataProviderError):
        provider.fetch_historical(HistoricalRequest(("AMD",), _START, _RECEIVED))
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "status,retryable",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (425, True),
        (500, True),
        (503, True),
    ],
)
def test_http_failure_classification_retains_bounded_retry_authority(tmp_path, status, retryable):
    provider = AlpacaMarketDataProvider(
        _credentials(tmp_path),
        historical_transport=_HistoricalTransport(
            AlpacaHistoricalTransportResponse(status, {}, _RECEIVED)
        ),
        websocket_transport=_WebSocketTransport(_Connection([])),
    )
    with pytest.raises(MarketDataProviderError) as failure:
        provider.fetch_historical(HistoricalRequest(("AMD",), _START, _RECEIVED))
    assert failure.value.retryable is retryable
    assert failure.value.provider_status == status


@pytest.mark.parametrize(
    "change",
    [
        {"limit": 0},
        {"limit": 10001},
        {"limit": True},
        {"start_at": _RECEIVED},
        {"end_at": _START},
        {"feed": "sip"},
        {"adjustment": "all"},
        {"timeframe": "15Min"},
        {"symbols": ()},
        {"symbols": ("AMD", "AMD")},
        {"symbols": ("amd",)},
        {"page_token": "x" * 4097},
    ],
)
def test_historical_request_cannot_expand_series_or_resource_authority(change):
    with pytest.raises(MarketDataProviderError):
        replace(HistoricalRequest(("AMD",), _START, _RECEIVED), **change)


@pytest.mark.parametrize("delay", [-1, True, 1.0, 3601])
def test_rate_limit_delay_is_bounded_exact_integer(delay):
    with pytest.raises(MarketDataProviderError):
        HistoricalResult(HistoricalStatus.RATE_LIMITED, retry_after_seconds=delay)


@pytest.mark.parametrize(
    "change", [{"status": "ok"}, {"bars": []}, {"bars": (object(),)}, {"retry_after_seconds": 1}]
)
def test_successful_page_cannot_encode_retry_or_unvalidated_bars(change):
    with pytest.raises(MarketDataProviderError):
        replace(HistoricalResult(HistoricalStatus.OK), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"event_type": "bar"},
        {"event_type": StreamEventType.BAR},
        {"bar": RawBarEnvelope({}, _RECEIVED)},
        {"retry_after_seconds": 1},
    ],
)
def test_control_stream_event_cannot_carry_fabricated_data(change):
    with pytest.raises(MarketDataProviderError):
        replace(StreamEvent(StreamEventType.CONNECTED, _RECEIVED), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"received_at": _bar().identity.start_at},
        {"provider_timestamp": _START.replace(tzinfo=None)},
        {"open": Decimal(0)},
        {"high": Decimal(1)},
        {"low": Decimal(20)},
        {"volume": Decimal(-1)},
        {"volume": Decimal("0.5")},
        {"trade_count": True},
        {"trade_count": -1},
        {"trade_count": 2**63},
        {"vwap": Decimal(-1)},
        {"schema_version": True},
        {"schema_version": 2},
        {"source_mode": "untrusted"},
        {"source_event_id": "x" * 129},
        {"is_correction": 1},
        {"correction_of_source_event_id": "other"},
        {"is_correction": True, "correction_of_source_event_id": "fixture-event"},
        {"quality_flags": ("complete", "complete")},
        {"lineage_hash": "bad"},
    ],
)
def test_canonical_write_rejects_invalid_numeric_and_correction_provenance(change):
    with pytest.raises(MarketDataValidationError):
        replace(_bar(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"source_payload_hash": "f" * 64},
        {"content_hash": "f" * 64},
        {"normalized_payload_hash": "f" * 64},
        {"source_event_id": "other-event"},
        {"received_at": _bar().received_at + timedelta(seconds=1)},
        {"quality_flags": ["different"]},
    ],
)
def test_restart_detects_canonical_evidence_tampering(repository, sqlite_engine, change):
    inserted = repository.append(_bar())
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_bar_events)
            .where(aqa_bar_events.c.bar_event_id == inserted.event.bar_event_id)
            .values(**change)
        )
    with pytest.raises((MarketDataIntegrityError, MarketDataValidationError)):
        MarketDataRepository(sqlite_engine).latest(inserted.event.identity)
