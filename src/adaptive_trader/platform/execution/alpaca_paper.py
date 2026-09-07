"""Factory-confined Alpaca paper SDK construction and sanitized facade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from functools import partial
from typing import Any, TypeVar
from urllib.parse import urlsplit
from weakref import WeakSet

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from requests import Response, Session

from adaptive_trader.platform.execution.broker import (
    AlpacaPaperBrokerAdapter,
    PaperClient,
    PaperClientOrder,
)
from adaptive_trader.platform.execution.models import (
    AccountState,
    ExecutionValidationError,
    Fill,
    OrderIntent,
    OrderSide,
    OrderState,
    Position,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.models import SecurityMetadataSnapshot
from adaptive_trader.platform.security import RedactedSecret

_T = TypeVar("_T")
_TRUSTED_ADAPTERS: WeakSet[AlpacaPaperBrokerAdapter] = WeakSet()
_STATUS_MAP = {
    "accepted": OrderState.ACCEPTED,
    "accepted_for_bidding": OrderState.ACCEPTED,
    "calculated": OrderState.PENDING,
    "canceled": OrderState.CANCELED,
    "done_for_day": OrderState.PENDING,
    "expired": OrderState.EXPIRED,
    "filled": OrderState.FILLED,
    "held": OrderState.PENDING,
    "new": OrderState.ACCEPTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "pending_cancel": OrderState.PENDING,
    "pending_new": OrderState.PENDING,
    "pending_replace": OrderState.PENDING,
    "rejected": OrderState.REJECTED,
    "replaced": OrderState.CANCELED,
    "stopped": OrderState.PENDING,
    "suspended": OrderState.PENDING,
}


def create_alpaca_paper_broker(
    *,
    api_key: RedactedSecret,
    secret_key: RedactedSecret,
) -> AlpacaPaperBrokerAdapter:
    """Create the only Alpaca adapter trusted by the execution service."""

    if type(api_key) is not RedactedSecret or type(secret_key) is not RedactedSecret:
        raise ExecutionValidationError("paper credentials must come from secret files")
    sdk = TradingClient(
        api_key=api_key.reveal(),
        secret_key=secret_key.reveal(),
        paper=True,
    )
    previous_session = getattr(sdk, "_session", None)
    if previous_session is not None:
        previous_session.close()
    sdk._session = _FixedPaperSession()
    sdk._retry = 0
    adapter = AlpacaPaperBrokerAdapter(_AlpacaSdkPaperClient(sdk))
    _TRUSTED_ADAPTERS.add(adapter)
    return adapter


def is_factory_created_paper_adapter(adapter: object) -> bool:
    """Return whether this exact adapter was created by the fixed paper factory."""

    return type(adapter) is AlpacaPaperBrokerAdapter and adapter in _TRUSTED_ADAPTERS


class _FixedPaperSession(Session):
    """No redirects/proxies/retries; every SDK request has finite socket deadlines."""

    def __init__(self) -> None:
        super().__init__()
        self.trust_env = False

    def request(self, method: str, url: str | bytes, *args: Any, **kwargs: Any) -> Response:
        if type(url) is not str or args:
            raise ExecutionValidationError("paper transport request is invalid")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "paper-api.alpaca.markets":
            raise ExecutionValidationError("paper transport host is invalid")
        kwargs["timeout"] = (3.05, 10)
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


class _AlpacaSdkPaperClient(PaperClient):
    """Translate SDK models to the closed execution contracts without leaking errors."""

    def __init__(self, client: TradingClient) -> None:
        self._client = client

    def submit_market_order(self, intent: OrderIntent) -> PaperClientOrder:
        request = MarketOrderRequest(
            symbol=intent.symbol,
            qty=float(intent.quantity),
            side=(AlpacaOrderSide.BUY if intent.side is OrderSide.BUY else AlpacaOrderSide.SELL),
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            extended_hours=False,
            client_order_id=intent.client_order_id,
        )
        raw = _safe_sdk_call(lambda: self._client.submit_order(order_data=request))
        return self._decode_order(raw)

    def lookup_by_client_order_id(self, client_order_id: str) -> PaperClientOrder | None:
        try:
            raw = self._client.get_order_by_client_id(client_order_id)
        except APIError as error:
            if error.status_code == 404:
                return None
            raise ExecutionValidationError("Alpaca paper operation failed") from None
        except Exception:
            raise ExecutionValidationError("Alpaca paper operation failed") from None
        if _required_text(raw, "client_order_id") != client_order_id:
            raise ExecutionValidationError("paper order identity differs from request")
        return self._decode_order(raw)

    def cancel_by_client_order_id(self, client_order_id: str) -> PaperClientOrder:
        raw = _safe_sdk_call(lambda: self._client.get_order_by_client_id(client_order_id))
        if _required_text(raw, "client_order_id") != client_order_id:
            raise ExecutionValidationError("paper order identity differs from request")
        broker_order_id = _required_text(raw, "id")
        _safe_sdk_call(lambda: self._client.cancel_order_by_id(broker_order_id))
        canceled = _safe_sdk_call(lambda: self._client.get_order_by_client_id(client_order_id))
        if _required_text(canceled, "client_order_id") != client_order_id:
            raise ExecutionValidationError("paper order identity differs from request")
        return self._decode_order(canceled)

    def _decode_order(self, raw: object) -> PaperClientOrder:
        if _decimal(raw, "filled_qty", default="0") == 0:
            return _paper_order(raw)
        activities: list[object] = []
        token: str | None = None
        tokens: set[str] = set()
        for _ in range(10):
            parameters = {
                "order_id": _required_text(raw, "id"),
                "direction": "asc",
                "page_size": 100,
            }
            if token is not None:
                parameters["page_token"] = token
            page = _safe_sdk_call(
                partial(
                    self._client.get,
                    "/account/activities/FILL",
                    data=parameters,
                    base_url="https://paper-api.alpaca.markets",
                    api_version="v2",
                )
            )
            if type(page) is not list or len(page) > 100:
                raise ExecutionValidationError("paper fill activity page is invalid")
            activities.extend(page)
            if len(page) < 100:
                return _paper_order(raw, fills=_activity_fills(raw, activities))
            token = _required_text(page[-1], "id")
            if token in tokens:
                raise ExecutionValidationError("paper fill activity pagination repeated")
            tokens.add(token)
        raise ExecutionValidationError("paper fill activity pagination bound exceeded")

    def account_state(self, observed_at: datetime) -> AccountState:
        raw = _safe_sdk_call(self._client.get_account)
        return AccountState(
            account_id=_required_text(raw, "id"),
            cash=_decimal(raw, "cash"),
            equity=_decimal(raw, "equity"),
            buying_power=_decimal(raw, "buying_power"),
            restricted_short_proceeds=Decimal(0),
            observed_at=_utc(observed_at),
        )

    def signed_positions(self) -> tuple[Position, ...]:
        raw = _safe_sdk_call(self._client.get_all_positions)
        if not isinstance(raw, list):
            raise ExecutionValidationError("paper broker positions response is invalid")
        return tuple(
            sorted(
                (
                    Position(_required_text(item, "symbol"), _decimal(item, "qty"))
                    for item in raw
                    if _decimal(item, "qty") != 0
                ),
                key=lambda item: item.symbol,
            )
        )

    def open_client_order_ids(self) -> tuple[str, ...]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        raw = _safe_sdk_call(lambda: self._client.get_orders(filter=request))
        if not isinstance(raw, list):
            raise ExecutionValidationError("paper broker orders response is invalid")
        if len(raw) >= 500:
            raise ExecutionValidationError("paper open-order snapshot may be truncated")
        return tuple(sorted({_required_text(item, "client_order_id") for item in raw}))

    def security_metadata(
        self, symbols: tuple[str, ...], *, observed_at: datetime
    ) -> tuple[SecurityMetadataSnapshot, ...]:
        if (
            type(symbols) is not tuple
            or symbols != tuple(sorted(set(symbols)))
            or not 0 < len(symbols) <= 100
        ):
            raise ExecutionValidationError("paper asset symbols are invalid")
        result: list[SecurityMetadataSnapshot] = []
        for symbol in symbols:
            if (
                type(symbol) is not str
                or not symbol.isascii()
                or not symbol.replace(".", "").isalpha()
                or symbol != symbol.upper()
            ):
                raise ExecutionValidationError("paper asset symbol is invalid")
            raw = _safe_sdk_call(partial(self._client.get_asset, symbol))
            if _required_text(raw, "symbol") != symbol:
                raise ExecutionValidationError("paper asset identity differs from request")
            flags = {key: _field(raw, key) for key in ("tradable", "shortable", "easy_to_borrow")}
            if any(type(value) is not bool for value in flags.values()):
                raise ExecutionValidationError("paper asset eligibility is invalid")
            result.append(
                SecurityMetadataSnapshot(
                    symbol=symbol,
                    asset_active=_enum_text(_field(raw, "status")) == "active",
                    tradable=flags["tradable"] is True,
                    shortable=flags["shortable"] is True,
                    easy_to_borrow=flags["easy_to_borrow"] is True,
                    primary_listing_eligible=_enum_text(_field(raw, "exchange")) == "nasdaq",
                    broker_capability_known=_enum_text(_field(raw, "asset_class")) == "us_equity",
                    observed_at=observed_at,
                )
            )
        return tuple(result)


def _paper_order(raw: object, *, fills: tuple[Fill, ...] = ()) -> PaperClientOrder:
    status_text = _enum_text(_field(raw, "status"))
    state = _STATUS_MAP.get(status_text)
    if state is None:
        raise ExecutionValidationError("paper broker returned an unsupported order state")
    cumulative = _decimal(raw, "filled_qty", default="0")
    average_raw = _field(raw, "filled_avg_price", None)
    average = None if average_raw is None else _decimal_value(average_raw)
    if cumulative != sum((fill.quantity for fill in fills), Decimal(0)):
        raise ExecutionValidationError("paper fills require independently sourced execution IDs")
    if (
        cumulative
        and average != sum((fill.quantity * fill.price for fill in fills), Decimal(0)) / cumulative
    ):
        raise ExecutionValidationError("paper order average differs from execution evidence")
    occurred_at = _utc(_field(raw, "updated_at", _field(raw, "submitted_at")))
    broker_order_id = _required_text(raw, "id")
    event_hash = sha256_hex(
        (
            "alpaca-paper-order-v2",
            broker_order_id,
            _required_text(raw, "client_order_id"),
            status_text,
            state,
            occurred_at,
            cumulative,
            average,
            tuple(fill.content_hash for fill in fills),
        )
    )
    return PaperClientOrder(
        client_order_id=_required_text(raw, "client_order_id"),
        broker_order_id=broker_order_id,
        broker_event_id=f"paper_event_{event_hash}",
        state=state,
        occurred_at=occurred_at,
        cumulative_filled_quantity=cumulative,
        average_fill_price=average,
        fills=fills,
    )


def _activity_fills(raw: object, activities: list[object]) -> tuple[Fill, ...]:
    """Bind actual activity IDs to one equity order; never infer fills from order totals.

    Fee policy: paper-equity-zero-simulated-fees-v1. Trading FILL activities do not
    expose fees; Alpaca's paper specification excludes regulatory and borrow fees.
    This simulation policy is confined to this fixed paper host and US equities.
    Unexpected fee fields fail closed; subsequent cash reconciliation remains mandatory.
    Sources: https://docs.alpaca.markets/us/docs/account-activities
    https://docs.alpaca.markets/us/docs/paper-trading
    """
    if _enum_text(_field(raw, "asset_class")) != "us_equity":
        raise ExecutionValidationError("paper fill fee policy requires US equities")
    order_id = _required_text(raw, "id")
    symbol = _required_text(raw, "symbol")
    side_text = _enum_text(_field(raw, "side"))
    if side_text not in {"buy", "sell"}:
        raise ExecutionValidationError("paper order side is invalid")
    side = OrderSide.BUY if side_text == "buy" else OrderSide.SELL
    total = _decimal(raw, "qty")
    reported = _decimal(raw, "filled_qty")
    status = _enum_text(_field(raw, "status"))
    if (
        total <= 0
        or not 0 < reported <= total
        or (status == "filled" and reported != total)
        or (status == "partially_filled" and reported >= total)
    ):
        raise ExecutionValidationError("paper order fill quantity is inconsistent")
    submitted = _utc(_field(raw, "submitted_at"))
    updated = _utc(_field(raw, "updated_at"))
    unique: dict[str, tuple[Decimal, Decimal, Fill]] = {}
    for activity in activities:
        if (
            _required_text(activity, "order_id") != order_id
            or _required_text(activity, "symbol") != symbol
            or _enum_text(_field(activity, "side")) != side_text
            or _field(activity, "activity_type") != "FILL"
            or _field(activity, "type") not in {"fill", "partial_fill"}
        ):
            raise ExecutionValidationError("paper fill activity differs from its order")
        for fee_field in ("fee", "commission"):
            if _field(activity, fee_field) is not None and _decimal(activity, fee_field) != 0:
                raise ExecutionValidationError("paper fill violates simulated fee policy")
        instant = _activity_timestamp(_field(activity, "transaction_time"))
        if not submitted <= instant <= updated:
            raise ExecutionValidationError("paper fill timestamp is outside order lifecycle")
        fill = Fill.create(
            client_order_id=_required_text(raw, "client_order_id"),
            broker_execution_id=_required_text(activity, "id"),
            symbol=symbol,
            side=side,
            quantity=_decimal(activity, "qty"),
            price=_decimal(activity, "price"),
            fee=Decimal(0),
            occurred_at=instant,
        )
        evidence = (_decimal(activity, "cum_qty"), _decimal(activity, "leaves_qty"), fill)
        previous = unique.setdefault(fill.broker_execution_id, evidence)
        if previous != evidence:
            raise ExecutionValidationError("paper execution ID has conflicting evidence")
    ordered = sorted(unique.values(), key=lambda item: (item[0], item[2].broker_execution_id))
    cumulative = Decimal(0)
    fills = []
    for declared, leaves, fill in ordered:
        cumulative += fill.quantity
        if declared != cumulative or leaves != total - cumulative or leaves < 0:
            raise ExecutionValidationError("paper activity cumulative evidence is incomplete")
        fills.append(fill)
    return tuple(fills)


def _activity_timestamp(value: object) -> datetime:
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ExecutionValidationError("paper activity timestamp is invalid") from None
    return _utc(value)


def _safe_sdk_call(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except Exception:
        raise ExecutionValidationError("Alpaca paper operation failed") from None


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _required_text(value: object, name: str) -> str:
    result = _field(value, name)
    if result is None:
        raise ExecutionValidationError("paper broker response is missing required data")
    text = str(result)
    if not text or len(text) > 128:
        raise ExecutionValidationError("paper broker response contains invalid text")
    return text


def _decimal(value: object, name: str, *, default: object = None) -> Decimal:
    return _decimal_value(_field(value, name, default))


def _decimal_value(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (DecimalException, ValueError):
        raise ExecutionValidationError("paper broker response contains an invalid number") from None
    if not result.is_finite():
        raise ExecutionValidationError("paper broker response contains an invalid number")
    return result


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ExecutionValidationError("paper broker response contains an invalid timestamp")
    return value.astimezone(UTC)
