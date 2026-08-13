"""Live, replay, and synthetic market-data providers with durable bar handling."""

from __future__ import annotations

import inspect
import random
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Protocol

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from adaptive_trader.clock import Clock, SystemClock, as_utc
from adaptive_trader.constants import NEW_YORK, SUPPORTED_DATA_FEEDS, UTC
from adaptive_trader.exceptions import BrokerConnectionError
from adaptive_trader.live_models import DataFreshnessState, MarketBar, PaperCredentials
from adaptive_trader.logging_config import redact
from adaptive_trader.persistence import AuditRepository

BarHandler = Callable[[MarketBar], Any]
HealthHandler = Callable[[bool, str], Any]


class MarketDataProvider(Protocol):
    """Broker-independent historical and real-time stock-data contract."""

    @property
    def feed(self) -> str: ...

    @property
    def connected(self) -> bool: ...

    def get_bars(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "minute",
    ) -> Sequence[MarketBar]: ...

    def start_stream(
        self,
        symbols: Sequence[str],
        handler: BarHandler,
        health_handler: HealthHandler | None = None,
    ) -> None: ...

    def stop_stream(self) -> None: ...


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _run_stream_safely(
    stream: Any,
    errors: list[str],
    secrets: tuple[str, ...],
) -> None:
    """Prevent credential-bearing SDK exceptions from escaping a thread."""

    try:
        stream.run()
    except Exception as exc:
        errors.append(redact(str(exc) or type(exc).__name__, secrets))


class AlpacaMarketDataProvider:
    """Official Alpaca stock-data adapter with an explicit, non-fallback feed."""

    def __init__(
        self,
        credentials: PaperCredentials,
        *,
        feed: str = "IEX",
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 60.0,
        max_reconnect_attempts: int = 10,
    ) -> None:
        normalized = str(feed).strip().upper()
        if normalized not in SUPPORTED_DATA_FEEDS:
            raise ValueError(f"Unsupported stock data feed: {feed!r}")
        if reconnect_initial_seconds <= 0 or reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("Invalid reconnect interval configuration")
        if max_reconnect_attempts <= 0:
            raise ValueError("max_reconnect_attempts must be positive")
        self._credentials = credentials
        self._feed = normalized
        self._data_feed = DataFeed.IEX if normalized == "IEX" else DataFeed.SIP
        self._historical = StockHistoricalDataClient(
            credentials.api_key,
            credentials.secret_key,
        )
        self._reconnect_initial = float(reconnect_initial_seconds)
        self._reconnect_max = float(reconnect_max_seconds)
        self._max_reconnect_attempts = int(max_reconnect_attempts)
        self._connected = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_stream: StockDataStream | None = None
        self._last_seen: dict[str, datetime] = {}
        self._lock = threading.RLock()

    @property
    def feed(self) -> str:
        return self._feed

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _to_bar(
        self,
        raw: Any,
        *,
        source: str,
        received_at: datetime,
        is_correction: bool = False,
        timeframe: str = "minute",
    ) -> MarketBar:
        start = as_utc(_attribute(raw, "timestamp"), field="bar.timestamp")
        duration = timedelta(days=1) if timeframe == "day" else timedelta(minutes=1)
        return MarketBar(
            symbol=str(_attribute(raw, "symbol")),
            start=start,
            end=start + duration,
            open=_attribute(raw, "open"),
            high=_attribute(raw, "high"),
            low=_attribute(raw, "low"),
            close=_attribute(raw, "close"),
            volume=int(_attribute(raw, "volume", 0)),
            trade_count=_attribute(raw, "trade_count"),
            vwap=_attribute(raw, "vwap"),
            feed=self._feed,
            received_at=received_at,
            source=source,
            is_correction=is_correction,
        )

    def get_bars(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "minute",
    ) -> Sequence[MarketBar]:
        normalized_timeframe = str(timeframe).lower()
        if normalized_timeframe not in {"minute", "day", "daily"}:
            raise ValueError("timeframe must be 'minute' or 'day'")
        request = StockBarsRequest(
            symbol_or_symbols=[str(symbol).upper() for symbol in symbols],
            start=as_utc(start),
            end=as_utc(end),
            timeframe=TimeFrame.Day
            if normalized_timeframe in {"day", "daily"}
            else TimeFrame.Minute,
            adjustment=Adjustment.ALL,
            feed=self._data_feed,
        )
        try:
            response = self._historical.get_stock_bars(request)
        except Exception as exc:
            safe = redact(str(exc), (self._credentials.api_key, self._credentials.secret_key))
            raise BrokerConnectionError(
                f"Alpaca {self._feed} historical bar request failed: {safe}"
            ) from None
        received = datetime.now(tz=UTC)
        raw_data = _attribute(response, "data", response)
        bars: list[MarketBar] = []
        if isinstance(raw_data, dict):
            for symbol_bars in raw_data.values():
                for raw in symbol_bars:
                    bars.append(
                        self._to_bar(
                            raw,
                            source="alpaca_historical",
                            received_at=received,
                            timeframe=(
                                "day" if normalized_timeframe in {"day", "daily"} else "minute"
                            ),
                        )
                    )
        else:
            for raw in raw_data:
                bars.append(
                    self._to_bar(
                        raw,
                        source="alpaca_historical",
                        received_at=received,
                        timeframe=("day" if normalized_timeframe in {"day", "daily"} else "minute"),
                    )
                )
        return tuple(sorted(bars, key=lambda bar: (bar.start, bar.symbol)))

    def check_feed_entitlement(
        self,
        symbol: str = "SPY",
        *,
        now: datetime | None = None,
    ) -> bool:
        """Confirm the configured feed with one harmless historical request.

        The request always uses this provider's explicit feed.  Failures are
        surfaced to the caller and never cause an IEX/SIP fallback.
        """

        end = as_utc(now or datetime.now(tz=UTC))
        # A five-minute probe produces false negatives outside regular hours.
        # Completed daily bars keep this read-only check small while still
        # proving that the explicitly configured feed returned the symbol.
        normalized_symbol = str(symbol).upper()
        bars = self.get_bars(
            (normalized_symbol,),
            start=end - timedelta(days=10),
            end=end,
            timeframe="day",
        )
        if not any(bar.symbol == normalized_symbol for bar in bars):
            raise BrokerConnectionError(
                f"Alpaca {self._feed} entitlement probe returned no {normalized_symbol} bars"
            )
        return True

    @staticmethod
    def _deliver(callback: Callable[..., Any] | None, *args: Any) -> Any:
        if callback is None:
            return None
        return callback(*args)

    def start_stream(
        self,
        symbols: Sequence[str],
        handler: BarHandler,
        health_handler: HealthHandler | None = None,
    ) -> None:
        if self._thread and self._thread.is_alive():
            return
        requested = tuple(str(symbol).upper() for symbol in symbols)
        self._stop_event.clear()

        def supervisor() -> None:
            backoff = self._reconnect_initial
            rng = random.Random(0xA1A)
            attempts = 0
            while not self._stop_event.is_set() and attempts < self._max_reconnect_attempts:
                attempts += 1
                stream = StockDataStream(
                    self._credentials.api_key,
                    self._credentials.secret_key,
                    feed=self._data_feed,
                )
                self._active_stream = stream
                with self._lock:
                    self._connected = False
                    last_seen = dict(self._last_seen)
                health_reason = "connected"
                if attempts > 1 and last_seen:
                    backfill_start = min(last_seen.values()) + timedelta(minutes=1)
                    backfill_end = datetime.now(tz=UTC)
                    backfill = tuple(
                        self.get_bars(
                            requested,
                            start=backfill_start,
                            end=backfill_end,
                            timeframe="minute",
                        )
                    )
                    for bar in backfill:
                        handler(bar)
                    latest_backfill = {
                        symbol: max(
                            (bar.start for bar in backfill if bar.symbol == symbol),
                            default=None,
                        )
                        for symbol in requested
                    }
                    nontrivial_window = backfill_end >= backfill_start + timedelta(minutes=1)
                    coverage_cutoff = backfill_end - timedelta(minutes=2)
                    covered_through_reconnect = all(
                        timestamp is not None and timestamp >= coverage_cutoff
                        for timestamp in latest_backfill.values()
                    )
                    health_reason = (
                        "reconnect_backfill_complete"
                        if nontrivial_window and covered_through_reconnect
                        else "reconnect_backfill_incomplete"
                    )
                    if health_reason == "reconnect_backfill_complete":
                        with self._lock:
                            self._last_seen.update(
                                {
                                    symbol: timestamp
                                    for symbol, timestamp in latest_backfill.items()
                                    if timestamp is not None
                                }
                            )
                health_announced = False

                def announce_authenticated_stream(
                    reason: str = health_reason,
                ) -> None:
                    nonlocal health_announced
                    if health_announced:
                        return
                    health_announced = True
                    with self._lock:
                        self._connected = reason != "reconnect_backfill_incomplete"
                    self._deliver(health_handler, True, reason)

                async def on_bar(raw: Any) -> None:
                    bar = self._to_bar(
                        raw,
                        source="alpaca_stream",
                        received_at=datetime.now(tz=UTC),
                    )
                    with self._lock:
                        self._last_seen[bar.symbol] = bar.start
                    result = handler(bar)
                    if inspect.isawaitable(result):
                        await result
                    # The first successfully decoded subscribed message proves
                    # that stream.run authenticated and reached the feed.
                    announce_authenticated_stream()

                async def on_updated_bar(raw: Any) -> None:
                    bar = self._to_bar(
                        raw,
                        source="alpaca_stream_update",
                        received_at=datetime.now(tz=UTC),
                        is_correction=True,
                    )
                    with self._lock:
                        self._last_seen[bar.symbol] = bar.start
                    result = handler(bar)
                    if inspect.isawaitable(result):
                        await result
                    announce_authenticated_stream()

                stream.subscribe_bars(on_bar, *requested)
                stream.subscribe_updated_bars(on_updated_bar, *requested)
                try:
                    runner_errors: list[str] = []
                    runner = threading.Thread(
                        target=_run_stream_safely,
                        args=(
                            stream,
                            runner_errors,
                            (
                                self._credentials.api_key,
                                self._credentials.secret_key,
                            ),
                        ),
                        name="alpaca-stock-data-socket",
                        daemon=True,
                    )
                    runner.start()
                    transport_lost = False
                    while runner.is_alive() and not self._stop_event.is_set():
                        authenticated = bool(getattr(stream, "_running", False))
                        if authenticated:
                            # The SDK sets _running only after websocket auth
                            # and subscription complete successfully.
                            announce_authenticated_stream()
                        elif health_announced:
                            transport_lost = True
                            with self._lock:
                                self._connected = False
                            self._deliver(
                                health_handler,
                                False,
                                "authenticated_stream_disconnected",
                            )
                            with suppress(Exception):
                                stream.stop()
                            break
                        self._stop_event.wait(0.25)
                    if self._stop_event.is_set() and runner.is_alive():
                        with suppress(Exception):
                            stream.stop()
                    runner.join(timeout=10)
                    if self._stop_event.is_set():
                        break
                    if transport_lost:
                        raise BrokerConnectionError("Authenticated Alpaca data stream disconnected")
                    if runner_errors:
                        raise BrokerConnectionError(
                            f"Alpaca data stream failed: {runner_errors[-1]}"
                        )
                    raise BrokerConnectionError("Alpaca data stream ended unexpectedly")
                except Exception as exc:
                    with self._lock:
                        self._connected = False
                    self._deliver(
                        health_handler,
                        False,
                        redact(
                            str(exc),
                            (
                                self._credentials.api_key,
                                self._credentials.secret_key,
                            ),
                        ),
                    )
                    if self._stop_event.is_set():
                        break
                    delay = min(backoff, self._reconnect_max)
                    delay += rng.uniform(0.0, min(1.0, delay * 0.2))
                    self._stop_event.wait(delay)
                    backoff = min(backoff * 2.0, self._reconnect_max)
            with self._lock:
                self._connected = False
            if not self._stop_event.is_set() and attempts >= self._max_reconnect_attempts:
                self._deliver(health_handler, False, "reconnect_attempts_exhausted")

        self._thread = threading.Thread(
            target=supervisor,
            name="alpaca-stock-data-supervisor",
            daemon=True,
        )
        self._thread.start()

    def stop_stream(self) -> None:
        self._stop_event.set()
        stream = self._active_stream
        if stream is not None:
            with suppress(Exception):
                stream.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)


class BarStore:
    """Validate stream ordering/gaps and expose a conservative freshness gate."""

    def __init__(
        self,
        repository: AuditRepository,
        *,
        universe: Sequence[str],
        stale_after_seconds: int = 180,
        run_id: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.repository = repository
        self.universe = tuple(str(symbol).upper() for symbol in universe)
        self.stale_after_seconds = int(stale_after_seconds)
        self.run_id = run_id
        self.clock = clock or SystemClock()
        self._connected = False
        self._last_seen = repository.latest_bar_times(self.universe)
        self._unresolved_gap = bool(repository.unresolved_gaps(self.universe))
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def mark_health(self, healthy: bool, reason: str) -> None:
        recovered = healthy and reason == "reconnect_backfill_complete"
        incomplete_recovery = healthy and reason == "reconnect_backfill_incomplete"
        with self._lock:
            if recovered:
                # The provider emits this reason only after proving nontrivial
                # backfill coverage for every configured symbol.  Persist the
                # exact covered intervals before allowing the in-memory gate
                # to reopen; older uncovered gaps remain latched across runs.
                self.repository.resolve_covered_gaps(
                    symbols=self.universe,
                    resolved_at=self.clock.now(),
                )
            # A transport reconnect without verified backfill coverage remains
            # fail-closed.  It is neither fresh nor gap-free until a later
            # complete recovery explicitly proves coverage.
            self._connected = bool(healthy) and not incomplete_recovery
            if recovered:
                self._unresolved_gap = bool(self.repository.unresolved_gaps(self.universe))
            elif incomplete_recovery:
                self._unresolved_gap = True
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="market_data",
            event_type=(
                "recovery_complete"
                if recovered
                else (
                    "recovery_incomplete"
                    if incomplete_recovery
                    else ("connected" if healthy else "disconnected")
                )
            ),
            payload={"reason": reason},
            created_at=self.clock.now(),
        )

    @staticmethod
    def _is_intraday_gap(previous: datetime, current: datetime) -> bool:
        previous_et = previous.astimezone(NEW_YORK)
        current_et = current.astimezone(NEW_YORK)
        return previous_et.date() == current_et.date() and current > previous + timedelta(
            seconds=90
        )

    def ingest(self, bar: MarketBar) -> str:
        with self._lock:
            previous = self._last_seen.get(bar.symbol)
            if previous is not None and bar.start < previous:
                self.repository.record_stream_event(
                    run_id=self.run_id,
                    stream="market_data",
                    event_type="out_of_order_bar",
                    symbol=bar.symbol,
                    payload={"previous": previous.isoformat(), "received": bar.start.isoformat()},
                    created_at=bar.received_at,
                )
            if previous is not None and self._is_intraday_gap(previous, bar.start):
                self.repository.record_gap(
                    run_id=self.run_id,
                    symbol=bar.symbol,
                    start=previous + timedelta(minutes=1),
                    end=bar.start,
                    feed=bar.feed,
                    details={"detected_by": "stream"},
                )
                self._unresolved_gap = True
            self._last_seen[bar.symbol] = max(previous or bar.start, bar.start)
        return self.repository.store_market_bar(bar, run_id=self.run_id)

    def freshness(self, now: datetime | None = None) -> DataFreshnessState:
        checked_at = self.clock.now() if now is None else as_utc(now)
        with self._lock:
            last_seen = dict(self._last_seen)
            connected = self._connected
            unresolved = self._unresolved_gap
        missing = tuple(symbol for symbol in self.universe if symbol not in last_seen)
        stale = tuple(
            symbol
            for symbol in self.universe
            if symbol in last_seen
            and (checked_at - last_seen[symbol]).total_seconds() > self.stale_after_seconds
        )
        return DataFreshnessState(
            checked_at=checked_at,
            stream_healthy=connected,
            stale_after_seconds=self.stale_after_seconds,
            last_bar_by_symbol=last_seen,
            missing_symbols=missing,
            stale_symbols=stale,
            unresolved_gap=unresolved,
        )

    def resolve_gaps_after_backfill(self) -> None:
        with self._lock:
            self.repository.resolve_covered_gaps(
                symbols=self.universe,
                resolved_at=self.clock.now(),
            )
            self._unresolved_gap = bool(self.repository.unresolved_gaps(self.universe))

    def refresh_gap_state(self) -> bool:
        """Reload the durable unresolved-gap projection and return its latch."""

        with self._lock:
            self._unresolved_gap = bool(self.repository.unresolved_gaps(self.universe))
            return self._unresolved_gap


class ReplayMarketDataProvider:
    """Manually driven provider that preserves supplied event order."""

    def __init__(self, bars: Sequence[MarketBar] = (), *, feed: str = "REPLAY") -> None:
        self._bars = list(bars)
        self._feed = feed.upper()
        self._connected = False
        self._handler: BarHandler | None = None
        self._health_handler: HealthHandler | None = None

    @property
    def feed(self) -> str:
        return self._feed

    @property
    def connected(self) -> bool:
        return self._connected

    def get_bars(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "minute",
    ) -> Sequence[MarketBar]:
        del timeframe
        allowed = {str(symbol).upper() for symbol in symbols}
        start_utc, end_utc = as_utc(start), as_utc(end)
        return tuple(
            bar for bar in self._bars if bar.symbol in allowed and start_utc <= bar.start <= end_utc
        )

    def start_stream(
        self,
        symbols: Sequence[str],
        handler: BarHandler,
        health_handler: HealthHandler | None = None,
    ) -> None:
        del symbols
        self._handler = handler
        self._health_handler = health_handler
        self.set_connected(True, "replay_connected")

    def stop_stream(self) -> None:
        self.set_connected(False, "replay_stopped")
        self._handler = None

    def set_connected(self, connected: bool, reason: str = "replay") -> None:
        self._connected = connected
        if self._health_handler is not None:
            self._health_handler(connected, reason)

    def emit(self, bar: MarketBar) -> Any:
        self._bars.append(bar)
        if self._handler is None:
            return None
        return self._handler(bar)


class SyntheticMarketDataProvider(ReplayMarketDataProvider):
    """Offline provider whose events are explicitly labeled synthetic."""

    def __init__(self, bars: Sequence[MarketBar] = ()) -> None:
        super().__init__(bars, feed="SYNTHETIC")
