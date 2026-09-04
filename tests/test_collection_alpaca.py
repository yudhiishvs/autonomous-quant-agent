"""Offline tests for the dedicated Alpaca market-data transports."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import pytest

import adaptive_trader.collection.alpaca as alpaca_module
from adaptive_trader.collection.alpaca import (
    AlpacaDataSourceError,
    AlpacaHistoricalBarSource,
    AlpacaLiveBarSource,
)
from adaptive_trader.collection.contracts import RawBarObservationV1
from adaptive_trader.collection.credentials import AlpacaDataCredentials

RECEIVED_AT = datetime(2026, 9, 3, 15, 1, tzinfo=UTC)


def _credentials() -> AlpacaDataCredentials:
    return AlpacaDataCredentials("private-data-key", "private-data-secret")


def _raw_bar(
    symbol: str = "AAPL",
    timestamp: str = "2026-09-03T15:00:00Z",
) -> dict[str, object]:
    return {
        "T": "b",
        "S": symbol,
        "t": timestamp,
        "o": 101.25,
        "h": 102.5,
        "l": 100.75,
        "c": 102.0,
        "v": 1200,
        "n": 87,
        "vw": 101.82,
        "x": "V",
    }


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        json_error: Exception | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error
        self.headers = dict(headers or {})

    def json(self) -> object:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeHistoricalClient:
    def __init__(
        self,
        responses: tuple[FakeResponse, ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.trust_env = True

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> FakeResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "params": dict(params),
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected historical request")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _historical_source(
    payload: object,
    *,
    clock: Callable[[], datetime] = lambda: RECEIVED_AT,
) -> tuple[AlpacaHistoricalBarSource, FakeHistoricalClient]:
    client = FakeHistoricalClient((FakeResponse(payload),))
    return AlpacaHistoricalBarSource(_credentials(), client=client, clock=clock), client


def test_historical_source_requests_only_raw_iex_minute_bars() -> None:
    first = _raw_bar("AAPL", "2026-09-03T15:00:00Z")
    first.pop("S")
    second = _raw_bar("TSLA", "2026-09-03T14:59:00Z")
    second.pop("S")
    source, client = _historical_source(
        {"bars": {"AAPL": [first], "TSLA": [second]}, "next_page_token": None}
    )

    observations = source.fetch(
        (" aapl ", "tsla"),
        start=datetime(2026, 9, 3, 14, 58, tzinfo=UTC),
        end=datetime(2026, 9, 3, 15, 1, tzinfo=UTC),
    )

    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["url"] == "https://data.alpaca.markets/v2/stocks/bars"
    assert request["params"] == {
        "symbols": "AAPL,TSLA",
        "timeframe": "1Min",
        "start": "2026-09-03T14:58:00.000000Z",
        "end": "2026-09-03T15:00:59.999999Z",
        "limit": 10_000,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
    }
    assert request["timeout"] == (5.0, 20.0)
    assert request["allow_redirects"] is False
    assert request["headers"] == {
        "Accept": "application/json",
        "APCA-API-KEY-ID": "private-data-key",
        "APCA-API-SECRET-KEY": "private-data-secret",
    }
    assert "private-data" not in str(request["url"])
    assert "private-data" not in str(request["params"])
    assert [item.bar.symbol for item in observations] == ["TSLA", "AAPL"]
    assert all(item.bar.provider == "alpaca" for item in observations)
    assert all(item.bar.feed == "IEX" for item in observations)
    assert all(item.bar.adjustment == "raw" for item in observations)
    assert all(item.bar.timeframe == "1m" for item in observations)
    assert all(item.bar.source == "historical_backfill" for item in observations)
    assert all(not item.is_correction for item in observations)

    raw_payload = json.loads(observations[1].raw_payload_json or "")
    assert raw_payload["S"] == "AAPL"
    assert raw_payload["x"] == "V"
    assert observations[1].raw_payload_sha256 is not None


def test_default_historical_session_disables_environment_proxies_and_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeHistoricalClient((FakeResponse({"bars": {}, "next_page_token": None}),))
    monkeypatch.setattr(alpaca_module.requests, "Session", lambda: session)

    source = AlpacaHistoricalBarSource(_credentials())

    assert session.trust_env is False
    source.close()
    assert session.closed is True


def test_injected_historical_client_is_not_owned_or_closed() -> None:
    source, client = _historical_source({"bars": {}, "next_page_token": None})

    source.close()

    assert client.closed is False


def test_historical_source_follows_pagination_and_deduplicates_exact_overlap() -> None:
    repeated = _raw_bar("AAPL", "2026-09-03T15:00:00Z")
    client = FakeHistoricalClient(
        (
            FakeResponse({"bars": {"AAPL": [repeated]}, "next_page_token": "page-two"}),
            FakeResponse(
                {
                    "bars": {
                        "AAPL": [repeated],
                        "TSLA": [_raw_bar("TSLA", "2026-09-03T15:00:00Z")],
                    },
                    "next_page_token": None,
                }
            ),
        )
    )
    source = AlpacaHistoricalBarSource(_credentials(), client=client, clock=lambda: RECEIVED_AT)

    observations = source.fetch(
        ("AAPL", "TSLA"),
        start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
        end=RECEIVED_AT,
    )

    assert len(client.calls) == 2
    assert "page_token" not in client.calls[0]["params"]  # type: ignore[operator]
    assert client.calls[1]["params"]["page_token"] == "page-two"  # type: ignore[index]
    assert [item.bar.symbol for item in observations] == ["AAPL", "TSLA"]


def test_historical_source_rejects_repeated_page_token() -> None:
    client = FakeHistoricalClient(
        (
            FakeResponse({"bars": {}, "next_page_token": "repeat"}),
            FakeResponse({"bars": {}, "next_page_token": "repeat"}),
        )
    )
    source = AlpacaHistoricalBarSource(_credentials(), client=client)

    with pytest.raises(AlpacaDataSourceError, match="invalid page token"):
        source.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )


def test_historical_source_bounds_pagination_and_observation_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(alpaca_module, "_MAX_HISTORICAL_PAGES", 2)
    paginated = AlpacaHistoricalBarSource(
        _credentials(),
        client=FakeHistoricalClient(
            (
                FakeResponse({"bars": {}, "next_page_token": "page-2"}),
                FakeResponse({"bars": {}, "next_page_token": "page-3"}),
            )
        ),
    )
    with pytest.raises(AlpacaDataSourceError, match="page limit") as page_error:
        paginated.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )
    assert page_error.value.retryable is False

    monkeypatch.setattr(alpaca_module, "_MAX_HISTORICAL_OBSERVATIONS", 1)
    oversized, _ = _historical_source(
        {
            "bars": {"AAPL": [_raw_bar(), _raw_bar(timestamp="2026-09-03T14:59:00Z")]},
            "next_page_token": None,
        }
    )
    with pytest.raises(AlpacaDataSourceError, match="observation limit") as volume_error:
        oversized.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )
    assert volume_error.value.retryable is False


def test_historical_payload_is_canonical_across_mapping_order() -> None:
    original = _raw_bar()
    reordered = dict(reversed(tuple(original.items())))
    first_source, _ = _historical_source({"bars": {"AAPL": [original]}, "next_page_token": None})
    second_source, _ = _historical_source({"bars": {"AAPL": [reordered]}, "next_page_token": None})

    first = first_source.fetch(
        ("AAPL",),
        start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
        end=RECEIVED_AT,
    )[0]
    second = second_source.fetch(
        ("AAPL",),
        start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
        end=RECEIVED_AT,
    )[0]

    assert first.raw_payload_json == second.raw_payload_json
    assert first.raw_payload_sha256 == second.raw_payload_sha256
    assert first.observation_id == second.observation_id


def test_historical_source_records_reconciliation_provenance() -> None:
    source, _ = _historical_source({"bars": {"AAPL": [_raw_bar()]}, "next_page_token": None})

    observation = source.fetch(
        ("AAPL",),
        start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
        end=RECEIVED_AT,
        source="historical_reconciliation",
    )[0]

    assert observation.bar.source == "historical_reconciliation"
    with pytest.raises(ValueError, match="Unsupported historical collection source"):
        source.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
            source="user_supplied",
        )


def test_historical_source_rejects_unknown_duplicate_and_unrequested_symbols() -> None:
    source, _ = _historical_source({"bars": {"AAPL": [_raw_bar("AAPL")]}, "next_page_token": None})
    start = datetime(2026, 9, 3, 14, 59, tzinfo=UTC)

    with pytest.raises(ValueError, match=r"not in collection-universe\.v1"):
        source.fetch(("MSFT",), start=start, end=RECEIVED_AT)
    with pytest.raises(ValueError, match="Duplicate symbol"):
        source.fetch(("AAPL", "AAPL"), start=start, end=RECEIVED_AT)

    unrequested, _ = _historical_source(
        {"bars": {"TSLA": [_raw_bar("TSLA")]}, "next_page_token": None}
    )
    with pytest.raises(AlpacaDataSourceError, match="unrequested symbol"):
        unrequested.fetch(("AAPL",), start=start, end=RECEIVED_AT)


def test_historical_source_redacts_transport_failures_and_classifies_statuses() -> None:
    failing = FakeHistoricalClient(
        (),
        error=RuntimeError("private-data-key rejected private-data-secret"),
    )
    source = AlpacaHistoricalBarSource(_credentials(), client=failing)

    with pytest.raises(AlpacaDataSourceError) as captured:
        source.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )
    rendered = str(captured.value)
    assert "private-data-key" not in rendered
    assert "private-data-secret" not in rendered
    assert rendered.count("<redacted>") == 2
    assert captured.value.retryable is True

    unauthorized = AlpacaHistoricalBarSource(
        _credentials(),
        client=FakeHistoricalClient(
            (FakeResponse({"private-data-secret": True}, status_code=401),)
        ),
    )
    with pytest.raises(AlpacaDataSourceError) as denied:
        unauthorized.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )
    assert str(denied.value).endswith("HTTP status 401")
    assert "private-data-secret" not in str(denied.value)
    assert denied.value.retryable is False
    assert denied.value.provider_status == 401

    throttled = AlpacaHistoricalBarSource(
        _credentials(),
        client=FakeHistoricalClient(
            (
                FakeResponse(
                    {},
                    status_code=429,
                    headers={"X-RateLimit-Reset": str(RECEIVED_AT.timestamp() + 47)},
                ),
            )
        ),
        clock=lambda: RECEIVED_AT,
    )
    with pytest.raises(AlpacaDataSourceError) as limited:
        throttled.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )
    assert limited.value.retryable is True
    assert limited.value.retry_after_seconds == 47


def test_provider_retry_delay_is_sanitized_and_bounded() -> None:
    throttled = AlpacaHistoricalBarSource(
        _credentials(),
        client=FakeHistoricalClient(
            (
                FakeResponse(
                    {},
                    status_code=429,
                    headers={
                        "Retry-After": "999999",
                        "X-RateLimit-Limit": "private-data-secret",
                    },
                ),
            )
        ),
    )

    with pytest.raises(AlpacaDataSourceError) as captured:
        throttled.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )

    assert captured.value.retry_after_seconds == 300
    assert "private-data-secret" not in str(captured.value)


def test_historical_source_rejects_invalid_json_shape_and_out_of_range_bar() -> None:
    invalid_json = AlpacaHistoricalBarSource(
        _credentials(),
        client=FakeHistoricalClient((FakeResponse({}, json_error=ValueError("bad")),)),
    )
    with pytest.raises(AlpacaDataSourceError, match="valid JSON") as invalid_json_error:
        invalid_json.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )

    assert invalid_json_error.value.retryable is False

    invalid_shape, _ = _historical_source({"bars": [], "next_page_token": None})
    with pytest.raises(AlpacaDataSourceError, match="invalid bars") as invalid_shape_error:
        invalid_shape.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )

    assert invalid_shape_error.value.retryable is False

    outside, _ = _historical_source(
        {
            "bars": {"AAPL": [_raw_bar(timestamp="2026-09-03T14:58:00Z")]},
            "next_page_token": None,
        }
    )
    with pytest.raises(
        AlpacaDataSourceError, match="outside the requested half-open interval"
    ) as outside_error:
        outside.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )
    assert outside_error.value.retryable is False


@pytest.mark.parametrize(
    "malformed",
    (
        {key: value for key, value in _raw_bar().items() if key != "v"},
        _raw_bar(timestamp="2026-09-03T15:00:30Z"),
    ),
)
def test_historical_source_rejects_missing_volume_and_unaligned_minutes(
    malformed: dict[str, object],
) -> None:
    source, _ = _historical_source({"bars": {"AAPL": [malformed]}, "next_page_token": None})

    with pytest.raises(AlpacaDataSourceError) as captured:
        source.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )

    assert captured.value.retryable is False


def test_sources_reject_naive_ranges_and_future_receipts() -> None:
    source, _ = _historical_source(
        {"bars": {"AAPL": [_raw_bar()]}, "next_page_token": None},
        clock=lambda: datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        source.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59),
            end=RECEIVED_AT,
        )
    with pytest.raises(AlpacaDataSourceError, match="cannot precede"):
        source.fetch(
            ("AAPL",),
            start=datetime(2026, 9, 3, 14, 59, tzinfo=UTC),
            end=RECEIVED_AT,
        )


def _control_frames(symbols: tuple[str, ...]) -> list[list[dict[str, object]]]:
    return [
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "bars": list(symbols), "updatedBars": list(symbols)}],
    ]


class FakeConnection:
    def __init__(self, frames: list[list[dict[str, object]]]) -> None:
        self.frames = [json.dumps(frame) for frame in frames]
        self.sent: list[str] = []
        self.receive_timeouts: list[float | None] = []
        self.closed = False
        self.on_empty: Callable[[], None] | None = None

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str:
        self.receive_timeouts.append(timeout)
        if self.frames:
            return self.frames.pop(0)
        if self.on_empty is not None:
            self.on_empty()
        raise OSError("synthetic stream closed")

    def close(self) -> None:
        self.closed = True


class BlockingConnection(FakeConnection):
    def __init__(self, symbols: tuple[str, ...]) -> None:
        super().__init__(_control_frames(symbols))
        self.blocked = threading.Event()
        self.released = threading.Event()

    def recv(self, timeout: float | None = None) -> str:
        if self.frames:
            return super().recv(timeout)
        self.blocked.set()
        if not self.released.wait(timeout=5):
            raise RuntimeError("test stream was not stopped")
        raise OSError("synthetic stream stopped")

    def close(self) -> None:
        super().close()
        self.released.set()


def _live_source(
    connection: FakeConnection,
    *,
    clock: Callable[[], datetime] = lambda: RECEIVED_AT,
) -> tuple[AlpacaLiveBarSource, list[tuple[str, dict[str, object]]]]:
    calls: list[tuple[str, dict[str, object]]] = []

    def factory(url: str, **kwargs: object) -> FakeConnection:
        calls.append((url, kwargs))
        return connection

    source = AlpacaLiveBarSource(
        _credentials(),
        connection_factory=factory,
        clock=clock,
    )
    return source, calls


def test_live_source_authenticates_and_subscribes_to_bars_and_updates() -> None:
    symbols = ("AAPL",)
    connection = FakeConnection(
        [
            *_control_frames(symbols),
            [
                _raw_bar("AAPL", "2026-09-03T15:00:00Z"),
                {**_raw_bar("AAPL", "2026-09-03T15:00:00Z"), "T": "u", "c": 102.1},
            ],
        ]
    )
    source, factory_calls = _live_source(connection)
    connection.on_empty = source.stop
    received: list[RawBarObservationV1] = []

    source.run(("aapl",), received.append)

    assert len(factory_calls) == 1
    url, options = factory_calls[0]
    assert url == "wss://stream.data.alpaca.markets/v2/iex"
    assert options == {
        "additional_headers": {"Content-Type": "application/json"},
        "proxy": None,
        "compression": None,
        "open_timeout": 10,
        "ping_interval": 20,
        "ping_timeout": 20,
        "close_timeout": 5,
        "max_size": 2 * 1024 * 1024,
        "max_queue": 64,
    }
    assert "private-data" not in str(factory_calls)
    assert json.loads(connection.sent[0]) == {
        "action": "auth",
        "key": "private-data-key",
        "secret": "private-data-secret",
    }
    assert json.loads(connection.sent[1]) == {
        "action": "subscribe",
        "bars": ["AAPL"],
        "updatedBars": ["AAPL"],
    }
    assert len(received) == 2
    assert received[0].bar.source == "iex_bar"
    assert received[0].is_correction is False
    assert received[1].bar.source == "iex_updated_bar"
    assert received[1].is_correction is True
    assert received[0].identity_hash == received[1].identity_hash
    assert received[0].content_hash != received[1].content_hash
    assert connection.closed is True
    assert source.running is False


def test_live_source_accepts_async_handlers() -> None:
    symbols = ("AAPL",)
    connection = FakeConnection([*_control_frames(symbols), [_raw_bar()]])
    source, _ = _live_source(connection)
    connection.on_empty = source.stop
    received: list[RawBarObservationV1] = []

    async def handler(observation: RawBarObservationV1) -> None:
        await asyncio.sleep(0)
        received.append(observation)

    source.run(symbols, handler)

    assert len(received) == 1


def test_live_source_stop_is_idempotent_and_closes_an_active_connection() -> None:
    connection = BlockingConnection(("AAPL",))
    source, _ = _live_source(connection)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            source.run(("AAPL",), lambda observation: None)
        except BaseException as exc:  # pragma: no cover - assertion captures thread failures
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert connection.blocked.wait(timeout=2)
    assert source.running is True

    source.stop()
    source.stop()
    thread.join(timeout=2)

    assert errors == []
    assert connection.closed is True
    assert thread.is_alive() is False
    assert source.running is False


def test_live_source_honors_external_signal_safe_stop_flag() -> None:
    connection = FakeConnection(_control_frames(("AAPL",)))
    source, _ = _live_source(connection)
    stop_requested = False

    def request_stop() -> None:
        nonlocal stop_requested
        stop_requested = True

    connection.on_empty = request_stop

    source.run(
        ("AAPL",),
        lambda observation: None,
        stop_requested=lambda: stop_requested,
    )

    assert stop_requested is True
    assert connection.closed is True
    assert source.running is False


def test_construction_is_network_free_and_connection_creation_is_deferred() -> None:
    factory_called = False

    def factory(*args: object, **kwargs: object) -> FakeConnection:
        del args, kwargs
        nonlocal factory_called
        factory_called = True
        return FakeConnection([])

    source = AlpacaLiveBarSource(_credentials(), connection_factory=factory)

    assert source.running is False
    assert factory_called is False


def test_live_source_propagates_one_sanitized_connection_failure() -> None:
    calls = 0

    def fail(*args: object, **kwargs: object) -> FakeConnection:
        del args, kwargs
        nonlocal calls
        calls += 1
        raise RuntimeError("private-data-key rejected private-data-secret")

    source = AlpacaLiveBarSource(_credentials(), connection_factory=fail)

    with pytest.raises(AlpacaDataSourceError) as captured:
        source.run(("AAPL",), lambda observation: None)

    assert calls == 1
    assert "private-data-key" not in str(captured.value)
    assert "private-data-secret" not in str(captured.value)
    assert captured.value.retryable is True


@pytest.mark.parametrize(
    ("code", "retryable"),
    ((402, False), (404, True), (405, False), (406, True), (409, False), (500, True)),
)
def test_live_source_classifies_provider_error_frames(code: int, retryable: bool) -> None:
    connection = FakeConnection(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "error", "code": code, "msg": "private-data-secret"}],
        ]
    )
    source, _ = _live_source(connection)

    with pytest.raises(AlpacaDataSourceError) as captured:
        source.run(("AAPL",), lambda observation: None)

    assert captured.value.provider_status == code
    assert captured.value.retryable is retryable
    assert "private-data-secret" not in str(captured.value)


def test_live_source_rejects_incomplete_subscription_and_unrequested_data() -> None:
    incomplete = FakeConnection(
        [
            *_control_frames(("AAPL",))[:2],
            [{"T": "subscription", "bars": ["AAPL"], "updatedBars": []}],
        ]
    )
    incomplete_source, _ = _live_source(incomplete)
    with pytest.raises(AlpacaDataSourceError, match="incomplete subscription") as incomplete_error:
        incomplete_source.run(("AAPL",), lambda observation: None)
    assert incomplete_error.value.retryable is False

    unrequested = FakeConnection([*_control_frames(("AAPL",)), [_raw_bar("TSLA")]])
    unrequested_source, _ = _live_source(unrequested)
    with pytest.raises(AlpacaDataSourceError, match="unrequested symbol") as unrequested_error:
        unrequested_source.run(("AAPL",), lambda observation: None)
    assert unrequested_error.value.retryable is False


@pytest.mark.parametrize(
    "malformed",
    (
        {key: value for key, value in _raw_bar().items() if key != "v"},
        _raw_bar(timestamp="2026-09-03T15:00:30Z"),
    ),
)
def test_live_source_rejects_missing_volume_and_unaligned_minutes(
    malformed: dict[str, object],
) -> None:
    source, _ = _live_source(FakeConnection([*_control_frames(("AAPL",)), [malformed]]))

    with pytest.raises(AlpacaDataSourceError) as captured:
        source.run(("AAPL",), lambda observation: None)

    assert captured.value.retryable is False


def test_live_source_honors_stop_requested_before_connection_starts() -> None:
    connected = False

    def factory(*args: object, **kwargs: object) -> FakeConnection:
        del args, kwargs
        nonlocal connected
        connected = True
        return FakeConnection([])

    source = AlpacaLiveBarSource(_credentials(), connection_factory=factory)
    source.stop()

    source.run(("AAPL",), lambda observation: None)

    assert connected is False


@pytest.mark.parametrize(
    ("constant_name", "value"),
    (
        ("OFFICIAL_ALPACA_DATA_BARS_URL", "https://example.invalid/v2/stocks/bars"),
        ("OFFICIAL_ALPACA_IEX_STREAM_URL", "wss://example.invalid/v2/iex"),
    ),
)
def test_non_data_endpoints_are_rejected_before_client_use(
    monkeypatch: pytest.MonkeyPatch,
    constant_name: str,
    value: str,
) -> None:
    monkeypatch.setattr(alpaca_module, constant_name, value)

    with pytest.raises(AlpacaDataSourceError, match="not the approved data host") as captured:
        AlpacaHistoricalBarSource(_credentials(), client=FakeHistoricalClient(()))

    assert captured.value.retryable is False
