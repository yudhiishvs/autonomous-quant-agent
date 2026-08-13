"""Broker abstraction with an Alpaca paper-only adapter and deterministic fake."""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    OrderSide,
    OrderType,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest
from alpaca.trading.stream import TradingStream

from adaptive_trader.clock import as_utc
from adaptive_trader.constants import NEW_YORK, SUPPORTED_US_EQUITY_EXCHANGES, UTC
from adaptive_trader.exceptions import (
    AmbiguousSubmissionError,
    BrokerConnectionError,
    SafetyViolation,
)
from adaptive_trader.live_models import (
    AccountState,
    AssetInfo,
    BrokerOrderState,
    MarketClockState,
    MarketSession,
    OrderIntent,
    PaperCredentials,
    PositionState,
    Side,
    TradeUpdate,
    decimal_value,
)
from adaptive_trader.logging_config import redact

TradeUpdateHandler = Callable[[TradeUpdate], Any]


class Broker(Protocol):
    """The only broker capabilities the application is allowed to use."""

    @property
    def paper_only(self) -> bool: ...

    def get_account(self) -> AccountState: ...

    def get_clock(self) -> MarketClockState: ...

    def get_calendar(self, start: date, end: date) -> Sequence[MarketSession]: ...

    def get_asset(self, symbol: str) -> AssetInfo: ...

    def get_positions(self) -> Sequence[PositionState]: ...

    def get_orders(
        self, *, include_closed: bool = True, after: datetime | None = None
    ) -> Sequence[BrokerOrderState]: ...

    def submit_order(self, intent: OrderIntent) -> BrokerOrderState: ...

    def cancel_all_orders(self) -> None: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def start_trade_updates(self, handler: TradeUpdateHandler) -> None: ...

    def stop_trade_updates(self) -> None: ...


def validate_asset(
    asset: AssetInfo, configured_universe: Sequence[str]
) -> tuple[bool, tuple[str, ...]]:
    """Validate the long-only US-listed/fractionable asset boundary."""

    reasons: list[str] = []
    if asset.symbol not in {str(symbol).upper() for symbol in configured_universe}:
        reasons.append("symbol is outside the configured universe")
    if asset.asset_class != "us_equity":
        reasons.append("asset class is not US equity")
    if asset.exchange not in SUPPORTED_US_EQUITY_EXCHANGES:
        reasons.append("asset is not listed on a supported US exchange")
    if not asset.active:
        reasons.append("asset is inactive")
    if not asset.tradable:
        reasons.append("asset is not tradable")
    if not asset.fractionable:
        reasons.append("asset is not fractionable")
    return not reasons, tuple(reasons)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
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


class AlpacaPaperBroker:
    """Alpaca adapter whose construction cannot be redirected to live trading.

    There is deliberately no ``paper`` or URL argument.  Both trading clients
    receive the literal safety flag here, and explicit APA credentials are
    always passed so the SDK cannot discover generic live credentials.
    """

    def __init__(self, credentials: PaperCredentials) -> None:
        self._credentials = credentials
        self._client = TradingClient(credentials.api_key, credentials.secret_key, paper=True)
        self._stream = TradingStream(credentials.api_key, credentials.secret_key, paper=True)
        self._stream_thread: threading.Thread | None = None
        self._stream_handler: TradeUpdateHandler | None = None
        self._stream_stop = threading.Event()
        self._trade_updates_healthy = False
        self._trade_updates_status = "not_started"
        self._stream_lock = threading.RLock()

    @property
    def paper_only(self) -> bool:
        return True

    @property
    def trading_client(self) -> Any:
        """Expose the SDK client for read-only diagnostics and test inspection."""

        return self._client

    @property
    def trade_updates_healthy(self) -> bool:
        with self._stream_lock:
            return self._trade_updates_healthy

    @property
    def trade_updates_status(self) -> str:
        with self._stream_lock:
            return self._trade_updates_status

    def _read(self, operation: Callable[[], Any], label: str) -> Any:
        try:
            return operation()
        except Exception as exc:
            safe = redact(str(exc), (self._credentials.api_key, self._credentials.secret_key))
            raise BrokerConnectionError(f"Alpaca paper {label} failed: {safe}") from None

    def get_account(self) -> AccountState:
        raw = self._read(self._client.get_account, "account check")
        timestamp = datetime.now(tz=UTC)
        return AccountState(
            timestamp=timestamp,
            account_id=str(_attribute(raw, "id", "paper-account")),
            status=_enum_value(_attribute(raw, "status", "unknown")),
            equity=_attribute(raw, "equity", "0"),
            cash=_attribute(raw, "cash", "0"),
            buying_power=_attribute(raw, "buying_power", "0"),
            last_equity=_attribute(raw, "last_equity"),
            trading_blocked=bool(_attribute(raw, "trading_blocked", False)),
        )

    def get_clock(self) -> MarketClockState:
        raw = self._read(self._client.get_clock, "market clock")
        return MarketClockState(
            timestamp=_attribute(raw, "timestamp"),
            is_open=bool(_attribute(raw, "is_open")),
            next_open=_attribute(raw, "next_open"),
            next_close=_attribute(raw, "next_close"),
        )

    def get_calendar(self, start: date, end: date) -> Sequence[MarketSession]:
        request = GetCalendarRequest(start=start, end=end)
        raw_sessions = self._read(lambda: self._client.get_calendar(request), "calendar")
        sessions: list[MarketSession] = []
        for raw in raw_sessions:
            session_date = _attribute(raw, "date")
            open_value = _attribute(raw, "open")
            close_value = _attribute(raw, "close")
            if isinstance(open_value, time):
                open_value = datetime.combine(session_date, open_value, tzinfo=NEW_YORK)
            if isinstance(close_value, time):
                close_value = datetime.combine(session_date, close_value, tzinfo=NEW_YORK)
            sessions.append(
                MarketSession(
                    session_date=session_date,
                    open_at=open_value,
                    close_at=close_value,
                )
            )
        return sessions

    def get_asset(self, symbol: str) -> AssetInfo:
        raw = self._read(lambda: self._client.get_asset(symbol), f"asset lookup for {symbol}")
        status = _attribute(raw, "status")
        return AssetInfo(
            symbol=str(_attribute(raw, "symbol", symbol)),
            asset_class=_enum_value(_attribute(raw, "asset_class")),
            exchange=_enum_value(_attribute(raw, "exchange")),
            active=status == AssetStatus.ACTIVE or _enum_value(status).lower() == "active",
            tradable=bool(_attribute(raw, "tradable", False)),
            fractionable=bool(_attribute(raw, "fractionable", False)),
        )

    def get_positions(self) -> Sequence[PositionState]:
        raw_positions = self._read(self._client.get_all_positions, "positions")
        timestamp = datetime.now(tz=UTC)
        return [
            PositionState(
                timestamp=timestamp,
                symbol=str(_attribute(raw, "symbol")),
                quantity=_attribute(raw, "qty", "0"),
                market_value=decimal_value(
                    _attribute(raw, "market_value", "0") or "0",
                    field_name="market_value",
                ),
                average_entry_price=_attribute(raw, "avg_entry_price"),
                current_price=_attribute(raw, "current_price"),
                unrealized_pl=_attribute(raw, "unrealized_pl"),
            )
            for raw in raw_positions
        ]

    def _map_order(self, raw: Any) -> BrokerOrderState:
        now = datetime.now(tz=UTC)
        submitted = _attribute(raw, "submitted_at", now) or now
        updated = _attribute(raw, "updated_at", submitted) or submitted
        return BrokerOrderState(
            client_order_id=str(_attribute(raw, "client_order_id")),
            broker_order_id=str(_attribute(raw, "id")),
            symbol=str(_attribute(raw, "symbol")),
            side=Side(_enum_value(_attribute(raw, "side")).lower()),
            status=_enum_value(_attribute(raw, "status")).lower(),
            submitted_at=submitted,
            updated_at=updated,
            requested_notional=_attribute(raw, "notional"),
            requested_quantity=_attribute(raw, "qty"),
            filled_quantity=decimal_value(
                _attribute(raw, "filled_qty", "0") or "0",
                field_name="filled_quantity",
                nonnegative=True,
            ),
            average_fill_price=_attribute(raw, "filled_avg_price"),
            extended_hours=bool(_attribute(raw, "extended_hours", False)),
            time_in_force=_enum_value(_attribute(raw, "time_in_force", "day")),
            order_type=_enum_value(
                _attribute(raw, "order_type", _attribute(raw, "type", "market"))
            ),
        )

    def get_orders(
        self, *, include_closed: bool = True, after: datetime | None = None
    ) -> Sequence[BrokerOrderState]:
        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL if include_closed else QueryOrderStatus.OPEN,
            limit=500,
            after=None if after is None else as_utc(after),
        )
        raw_orders = self._read(lambda: self._client.get_orders(request), "orders")
        return [self._map_order(order) for order in raw_orders]

    def submit_order(self, intent: OrderIntent) -> BrokerOrderState:
        side = OrderSide.BUY if intent.side is Side.BUY else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=intent.symbol,
            qty=None if intent.quantity is None else float(intent.quantity),
            notional=None if intent.notional is None else float(intent.notional),
            side=side,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            extended_hours=False,
            client_order_id=intent.client_order_id,
        )
        try:
            raw = self._client.submit_order(order_data=request)
        except Exception as exc:
            safe = redact(str(exc), (self._credentials.api_key, self._credentials.secret_key))
            # A transport failure can occur after the paper endpoint accepted the
            # request.  Callers must reconcile the deterministic client ID.
            raise AmbiguousSubmissionError(
                f"Paper order submission outcome unknown: {safe}"
            ) from None
        return self._map_order(raw)

    def cancel_all_orders(self) -> None:
        self._read(self._client.cancel_orders, "cancel all orders")

    def cancel_order(self, broker_order_id: str) -> None:
        self._read(
            lambda: self._client.cancel_order_by_id(broker_order_id),
            f"cancel order {broker_order_id}",
        )

    def start_trade_updates(self, handler: TradeUpdateHandler) -> None:
        if self._stream_thread and self._stream_thread.is_alive():
            return
        self._stream_handler = handler
        self._stream_stop.clear()

        async def update_handler(raw: Any) -> None:
            with self._stream_lock:
                self._trade_updates_healthy = True
                self._trade_updates_status = "connected"
            order = self._map_order(_attribute(raw, "order"))
            update = TradeUpdate(
                event=_enum_value(_attribute(raw, "event")),
                order=order,
                timestamp=_attribute(raw, "timestamp", datetime.now(tz=UTC)),
                execution_id=(
                    None
                    if _attribute(raw, "execution_id") is None
                    else str(_attribute(raw, "execution_id"))
                ),
                fill_quantity=_attribute(raw, "qty"),
                fill_price=_attribute(raw, "price"),
            )
            result = handler(update)
            if inspect.isawaitable(result):
                await result

        def supervisor() -> None:
            attempt = 0
            stream = self._stream
            while not self._stream_stop.is_set():
                attempt += 1
                if attempt > 1:
                    stream = TradingStream(
                        self._credentials.api_key,
                        self._credentials.secret_key,
                        paper=True,
                    )
                self._stream = stream
                with self._stream_lock:
                    self._trade_updates_healthy = False
                    self._trade_updates_status = "connecting" if attempt == 1 else "reconnecting"
                stream.subscribe_trade_updates(update_handler)
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
                    name="alpaca-paper-trade-updates-socket",
                    daemon=True,
                )
                runner.start()
                while runner.is_alive() and not self._stream_stop.is_set():
                    authenticated = bool(getattr(stream, "_running", False))
                    with self._stream_lock:
                        self._trade_updates_healthy = authenticated
                        self._trade_updates_status = "connected" if authenticated else "connecting"
                    self._stream_stop.wait(0.25)
                if self._stream_stop.is_set() and runner.is_alive():
                    with suppress(Exception):
                        stream.stop()
                runner.join(timeout=10)
                with self._stream_lock:
                    self._trade_updates_healthy = False
                    self._trade_updates_status = (
                        "stopped"
                        if self._stream_stop.is_set()
                        else (f"error:{runner_errors[-1]}" if runner_errors else "disconnected")
                    )
                if self._stream_stop.wait(min(30.0, float(2 ** min(attempt - 1, 5)))):
                    break

        self._stream_thread = threading.Thread(
            target=supervisor,
            name="alpaca-paper-trade-updates-supervisor",
            daemon=True,
        )
        self._stream_thread.start()

    def stop_trade_updates(self) -> None:
        self._stream_stop.set()
        if self._stream_thread and self._stream_thread.is_alive():
            with suppress(Exception):
                self._stream.stop()
            self._stream_thread.join(timeout=10)
        with self._stream_lock:
            self._trade_updates_healthy = False
            self._trade_updates_status = "stopped"


class FakePaperBroker:
    """In-memory paper broker for deterministic tests and market replay."""

    def __init__(
        self,
        *,
        now: datetime | None = None,
        initial_cash: Decimal | str | float = Decimal("100000"),
        auto_fill: bool = False,
    ) -> None:
        timestamp = as_utc(now or datetime.now(tz=UTC))
        cash = decimal_value(initial_cash, field_name="initial_cash", nonnegative=True)
        self._account_id = "fake-paper-account"
        self._cash = cash
        self._positions: dict[str, PositionState] = {}
        self._orders: dict[str, BrokerOrderState] = {}
        self._assets: dict[str, AssetInfo] = {}
        self._clock = MarketClockState(
            timestamp=timestamp,
            is_open=True,
            next_open=timestamp + timedelta(days=1),
            next_close=timestamp + timedelta(hours=6),
        )
        self._calendar: list[MarketSession] = []
        self._handler: TradeUpdateHandler | None = None
        self._lock = threading.RLock()
        self.auto_fill = auto_fill
        self.timeout_after_accept: set[str] = set()
        self.reject_client_ids: set[str] = set()
        self.submit_calls = 0
        self._pending_callbacks: set[asyncio.Task[Any]] = set()
        self._trade_updates_healthy = False

    @property
    def paper_only(self) -> bool:
        return True

    @property
    def trade_updates_healthy(self) -> bool:
        return self._trade_updates_healthy

    @property
    def trade_updates_status(self) -> str:
        return "connected" if self._trade_updates_healthy else "disconnected"

    def set_trade_updates_healthy(self, healthy: bool) -> None:
        self._trade_updates_healthy = bool(healthy)

    def set_clock(self, state: MarketClockState) -> None:
        with self._lock:
            self._clock = state

    def set_calendar(self, sessions: Sequence[MarketSession]) -> None:
        with self._lock:
            self._calendar = list(sessions)

    def add_asset(self, asset: AssetInfo) -> None:
        with self._lock:
            self._assets[asset.symbol] = asset

    def set_position(
        self,
        symbol: str,
        quantity: Decimal | str | float,
        price: Decimal | str | float,
    ) -> None:
        timestamp = self._clock.timestamp
        qty = decimal_value(quantity, field_name="quantity")
        px = decimal_value(price, field_name="price", nonnegative=True)
        with self._lock:
            self._positions[str(symbol).upper()] = PositionState(
                timestamp=timestamp,
                symbol=symbol,
                quantity=qty,
                market_value=qty * px,
                current_price=px,
                average_entry_price=px,
            )

    def set_cash(self, value: Decimal | str | float) -> None:
        """Set deterministic paper cash for reconciliation and risk tests."""

        cash = decimal_value(value, field_name="cash", nonnegative=True)
        with self._lock:
            self._cash = cash

    def inject_order(self, order: BrokerOrderState) -> None:
        with self._lock:
            self._orders[order.client_order_id] = order

    def get_account(self) -> AccountState:
        with self._lock:
            positions_value = sum(
                (position.market_value for position in self._positions.values()), Decimal("0")
            )
            equity = self._cash + positions_value
            return AccountState(
                timestamp=self._clock.timestamp,
                account_id=self._account_id,
                status="ACTIVE",
                equity=equity,
                cash=self._cash,
                buying_power=self._cash,
                last_equity=equity,
                trading_blocked=False,
            )

    def get_clock(self) -> MarketClockState:
        with self._lock:
            return self._clock

    def get_calendar(self, start: date, end: date) -> Sequence[MarketSession]:
        with self._lock:
            if self._calendar:
                return [s for s in self._calendar if start <= s.session_date <= end]
        sessions: list[MarketSession] = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                open_at = datetime.combine(cursor, time(9, 30), tzinfo=NEW_YORK)
                close_at = datetime.combine(cursor, time(16, 0), tzinfo=NEW_YORK)
                sessions.append(MarketSession(cursor, open_at, close_at))
            cursor += timedelta(days=1)
        return sessions

    def get_asset(self, symbol: str) -> AssetInfo:
        normalized = str(symbol).upper()
        with self._lock:
            return self._assets.get(
                normalized,
                AssetInfo(
                    symbol=normalized,
                    asset_class="us_equity",
                    exchange="NYSE",
                    active=True,
                    tradable=True,
                    fractionable=True,
                ),
            )

    def get_positions(self) -> Sequence[PositionState]:
        with self._lock:
            return list(self._positions.values())

    def get_orders(
        self, *, include_closed: bool = True, after: datetime | None = None
    ) -> Sequence[BrokerOrderState]:
        terminal = {"filled", "canceled", "rejected", "expired", "replaced"}
        with self._lock:
            orders = list(self._orders.values())
        if not include_closed:
            orders = [order for order in orders if order.status not in terminal]
        if after is not None:
            cutoff = as_utc(after)
            orders = [order for order in orders if order.updated_at >= cutoff]
        return sorted(orders, key=lambda order: (order.submitted_at, order.client_order_id))

    def _make_order(self, intent: OrderIntent, status: str) -> BrokerOrderState:
        now = self._clock.timestamp
        filled_qty = Decimal("0")
        avg_price: Decimal | None = None
        if status == "filled":
            filled_qty = (
                intent.quantity
                if intent.quantity is not None
                else intent.estimated_notional / intent.reference_price
            )
            avg_price = intent.reference_price
        return BrokerOrderState(
            client_order_id=intent.client_order_id,
            broker_order_id=str(uuid.uuid5(uuid.NAMESPACE_URL, intent.client_order_id)),
            symbol=intent.symbol,
            side=intent.side,
            status=status,
            submitted_at=now,
            updated_at=now,
            requested_notional=intent.notional,
            requested_quantity=intent.quantity,
            filled_quantity=filled_qty,
            average_fill_price=avg_price,
        )

    def submit_order(self, intent: OrderIntent) -> BrokerOrderState:
        with self._lock:
            existing = self._orders.get(intent.client_order_id)
            if existing is not None:
                return existing
            self.submit_calls += 1
            if intent.side is Side.SELL:
                position = self._positions.get(intent.symbol)
                held = Decimal("0") if position is None else position.quantity
                requested = intent.quantity or (intent.estimated_notional / intent.reference_price)
                if requested > held:
                    raise SafetyViolation("Fake broker refused a sell that could create a short")
            status = (
                "rejected"
                if intent.client_order_id in self.reject_client_ids
                else ("filled" if self.auto_fill else "accepted")
            )
            order = self._make_order(intent, status)
            self._orders[intent.client_order_id] = order
            if status == "filled":
                self._apply_fill(
                    order, order.filled_quantity, order.average_fill_price or Decimal("0")
                )
            if intent.client_order_id in self.timeout_after_accept:
                raise AmbiguousSubmissionError("Synthetic timeout after paper acceptance")
        self._emit(
            TradeUpdate(
                event=status,
                order=order,
                timestamp=order.updated_at,
                event_id=f"fake:{intent.client_order_id}:{status}",
                fill_quantity=order.filled_quantity if status == "filled" else None,
                fill_price=order.average_fill_price if status == "filled" else None,
            )
        )
        return order

    def _apply_fill(self, order: BrokerOrderState, quantity: Decimal, price: Decimal) -> None:
        notional = quantity * price
        current = self._positions.get(order.symbol)
        current_qty = Decimal("0") if current is None else current.quantity
        new_qty = current_qty + quantity if order.side is Side.BUY else current_qty - quantity
        if new_qty < 0:
            raise SafetyViolation("Fill would create a negative paper position")
        if order.side is Side.BUY:
            if notional > self._cash:
                raise SafetyViolation("Fill would use margin buying power")
            self._cash -= notional
        else:
            self._cash += notional
        if new_qty == 0:
            self._positions.pop(order.symbol, None)
        else:
            self._positions[order.symbol] = PositionState(
                timestamp=order.updated_at,
                symbol=order.symbol,
                quantity=new_qty,
                market_value=new_qty * price,
                average_entry_price=price,
                current_price=price,
            )

    def emit_trade_update(
        self,
        client_order_id: str,
        event: str,
        *,
        fill_quantity: Decimal | str | float | None = None,
        fill_price: Decimal | str | float | None = None,
        event_id: str | None = None,
    ) -> TradeUpdate:
        with self._lock:
            prior = self._orders[client_order_id]
            quantity = (
                None
                if fill_quantity is None
                else decimal_value(fill_quantity, field_name="fill_quantity", nonnegative=True)
            )
            price = (
                None
                if fill_price is None
                else decimal_value(fill_price, field_name="fill_price", nonnegative=True)
            )
            cumulative = prior.filled_quantity + (quantity or Decimal("0"))
            order = BrokerOrderState(
                client_order_id=prior.client_order_id,
                broker_order_id=prior.broker_order_id,
                symbol=prior.symbol,
                side=prior.side,
                status=event,
                submitted_at=prior.submitted_at,
                updated_at=self._clock.timestamp,
                requested_notional=prior.requested_notional,
                requested_quantity=prior.requested_quantity,
                filled_quantity=cumulative,
                average_fill_price=price or prior.average_fill_price,
            )
            self._orders[client_order_id] = order
            if (
                event in {"partial_fill", "partially_filled", "fill", "filled"}
                and quantity
                and price
            ):
                self._apply_fill(order, quantity, price)
        update = TradeUpdate(
            event=event,
            order=order,
            timestamp=order.updated_at,
            event_id=event_id or f"fake:{client_order_id}:{event}:{cumulative}",
            execution_id=(
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_order_id}:{event}:{cumulative}"))
                if quantity
                else None
            ),
            fill_quantity=quantity,
            fill_price=price,
        )
        self._emit(update)
        return update

    def cancel_all_orders(self) -> None:
        for order in list(self.get_orders(include_closed=False)):
            self.cancel_order(order.broker_order_id)

    def cancel_order(self, broker_order_id: str) -> None:
        with self._lock:
            for client_id, prior in self._orders.items():
                if prior.broker_order_id != broker_order_id:
                    continue
                canceled = BrokerOrderState(
                    client_order_id=prior.client_order_id,
                    broker_order_id=prior.broker_order_id,
                    symbol=prior.symbol,
                    side=prior.side,
                    status="canceled",
                    submitted_at=prior.submitted_at,
                    updated_at=self._clock.timestamp,
                    requested_notional=prior.requested_notional,
                    requested_quantity=prior.requested_quantity,
                    filled_quantity=prior.filled_quantity,
                    average_fill_price=prior.average_fill_price,
                )
                self._orders[client_id] = canceled
                break
            else:
                return
        self._emit(
            TradeUpdate(
                event="canceled",
                order=canceled,
                timestamp=canceled.updated_at,
                event_id=f"fake:{client_id}:canceled",
            )
        )

    def start_trade_updates(self, handler: TradeUpdateHandler) -> None:
        self._handler = handler
        self._trade_updates_healthy = True

    def stop_trade_updates(self) -> None:
        self._handler = None
        self._trade_updates_healthy = False

    def _emit(self, update: TradeUpdate) -> None:
        handler = self._handler
        if handler is None:
            return
        result = handler(update)
        if inspect.isawaitable(result):
            coroutine = cast(Coroutine[Any, Any, Any], result)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(coroutine)
            else:
                task: asyncio.Task[Any] = loop.create_task(coroutine)
                self._pending_callbacks.add(task)
                task.add_done_callback(self._pending_callbacks.discard)
