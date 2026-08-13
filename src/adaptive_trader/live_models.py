"""Broker-neutral, finite, timezone-aware models for forward paper operation."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import isfinite
from typing import Any

from adaptive_trader.clock import as_utc
from adaptive_trader.constants import (
    MAX_CLIENT_ORDER_ID_LENGTH,
    PAPER_API_KEY_ENV,
    PAPER_SECRET_KEY_ENV,
)
from adaptive_trader.exceptions import CredentialError, SafetyViolation


class RunMode(StrEnum):
    DOCTOR = "doctor"
    BACKTEST = "backtest"
    REPLAY = "replay"
    OBSERVE = "observe"
    PAPER_ONCE = "paper_once"
    PAPER_RUN = "paper_run"
    STATUS = "status"
    RECONCILE = "reconcile"
    HALT = "halt"
    RESUME = "resume"
    REPORT = "report"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class LocalOrderState(StrEnum):
    PLANNED = "planned"
    LOCALLY_RESERVED = "locally_reserved"
    SUBMISSION_STARTED = "submission_started"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REPLACED = "replaced"
    SUBMISSION_UNKNOWN = "submission_unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DiscrepancySeverity(StrEnum):
    INFORMATIONAL = "informational"
    RECOVERABLE = "recoverable"
    BLOCKING = "blocking"
    CRITICAL = "critical"


class ReplayEventType(StrEnum):
    BAR = "bar"
    TRADE_UPDATE = "trade_update"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    ADVANCE = "advance"
    RESTART = "restart"
    RECONCILE = "reconcile"
    EVALUATE = "evaluate"
    BROKER_MUTATION = "broker_mutation"


def decimal_value(value: Any, *, field_name: str, nonnegative: bool = False) -> Decimal:
    """Convert a finite numeric value to ``Decimal`` and enforce its sign."""

    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if nonnegative and result < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return result


def normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a nonempty string")
    normalized = symbol.strip().upper()
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for character in normalized):
        raise ValueError(f"Unsupported symbol format: {symbol!r}")
    return normalized


@dataclass(frozen=True, slots=True, repr=False)
class PaperCredentials:
    """Explicit paper-account credentials that never fall back to SDK env names."""

    api_key: str
    secret_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise CredentialError(f"Missing {PAPER_API_KEY_ENV}")
        if not isinstance(self.secret_key, str) or not self.secret_key.strip():
            raise CredentialError(f"Missing {PAPER_SECRET_KEY_ENV}")

    def __repr__(self) -> str:
        return "PaperCredentials(api_key='[REDACTED]', secret_key='[REDACTED]')"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> PaperCredentials:
        values = os.environ if environment is None else environment
        return cls(
            api_key=str(values.get(PAPER_API_KEY_ENV, "")),
            secret_key=str(values.get(PAPER_SECRET_KEY_ENV, "")),
        )


@dataclass(frozen=True, slots=True)
class MarketBar:
    symbol: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    feed: str
    received_at: datetime
    end: datetime | None = None
    trade_count: int | None = None
    vwap: Decimal | None = None
    source: str = "unknown"
    is_correction: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "start", as_utc(self.start, field="MarketBar.start"))
        object.__setattr__(
            self, "received_at", as_utc(self.received_at, field="MarketBar.received_at")
        )
        if self.end is not None:
            end = as_utc(self.end, field="MarketBar.end")
            if end <= self.start:
                raise ValueError("MarketBar.end must follow start")
            object.__setattr__(self, "end", end)
        for name in ("open", "high", "low", "close"):
            price = decimal_value(getattr(self, name), field_name=f"MarketBar.{name}")
            if price <= 0:
                raise ValueError(f"MarketBar.{name} must be positive")
            object.__setattr__(self, name, price)
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close, self.high
        ):
            raise ValueError("MarketBar OHLC values are internally inconsistent")
        if isinstance(self.volume, bool) or int(self.volume) < 0:
            raise ValueError("MarketBar.volume must be nonnegative")
        object.__setattr__(self, "volume", int(self.volume))
        if self.trade_count is not None and int(self.trade_count) < 0:
            raise ValueError("MarketBar.trade_count must be nonnegative")
        if self.trade_count is not None:
            object.__setattr__(self, "trade_count", int(self.trade_count))
        if self.vwap is not None:
            vwap = decimal_value(self.vwap, field_name="MarketBar.vwap")
            if vwap <= 0:
                raise ValueError("MarketBar.vwap must be positive")
            object.__setattr__(self, "vwap", vwap)
        feed = str(self.feed).strip().upper()
        if feed not in {"IEX", "SIP", "SYNTHETIC", "REPLAY"}:
            raise ValueError(f"Unsupported MarketBar.feed: {self.feed!r}")
        object.__setattr__(self, "feed", feed)


@dataclass(frozen=True, slots=True)
class AssetInfo:
    symbol: str
    asset_class: str
    exchange: str
    active: bool
    tradable: bool
    fractionable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "asset_class", str(self.asset_class).lower())
        object.__setattr__(self, "exchange", str(self.exchange).upper())


@dataclass(frozen=True, slots=True)
class MarketClockState:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        object.__setattr__(self, "next_open", as_utc(self.next_open))
        object.__setattr__(self, "next_close", as_utc(self.next_close))


@dataclass(frozen=True, slots=True)
class MarketSession:
    session_date: date
    open_at: datetime
    close_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_at", as_utc(self.open_at))
        object.__setattr__(self, "close_at", as_utc(self.close_at))
        if self.close_at <= self.open_at:
            raise ValueError("MarketSession.close_at must follow open_at")


@dataclass(frozen=True, slots=True)
class AccountState:
    timestamp: datetime
    account_id: str
    status: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    last_equity: Decimal | None = None
    trading_blocked: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        for name in ("equity", "cash", "buying_power"):
            object.__setattr__(
                self, name, decimal_value(getattr(self, name), field_name=name, nonnegative=True)
            )
        if self.last_equity is not None:
            object.__setattr__(
                self,
                "last_equity",
                decimal_value(self.last_equity, field_name="last_equity", nonnegative=True),
            )


@dataclass(frozen=True, slots=True)
class PositionState:
    timestamp: datetime
    symbol: str
    quantity: Decimal
    market_value: Decimal
    average_entry_price: Decimal | None = None
    current_price: Decimal | None = None
    unrealized_pl: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        for name in ("quantity", "market_value"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), field_name=name))
        for name in ("average_entry_price", "current_price", "unrealized_pl"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_value(value, field_name=name))


@dataclass(frozen=True, slots=True)
class BrokerOrderState:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Side
    status: str
    submitted_at: datetime
    updated_at: datetime
    requested_notional: Decimal | None = None
    requested_quantity: Decimal | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    extended_hours: bool = False
    time_in_force: str = "day"
    order_type: str = "market"

    def __post_init__(self) -> None:
        if not self.client_order_id or len(self.client_order_id) > MAX_CLIENT_ORDER_ID_LENGTH:
            raise ValueError("invalid client_order_id")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "submitted_at", as_utc(self.submitted_at))
        object.__setattr__(self, "updated_at", as_utc(self.updated_at))
        if self.extended_hours:
            raise SafetyViolation("Extended-hours orders are prohibited")
        if str(self.time_in_force).lower() != "day" or str(self.order_type).lower() != "market":
            raise SafetyViolation("Only DAY market orders are supported")
        for name in ("requested_notional", "requested_quantity", "average_fill_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, decimal_value(value, field_name=name, nonnegative=True)
                )
        object.__setattr__(
            self,
            "filled_quantity",
            decimal_value(self.filled_quantity, field_name="filled_quantity", nonnegative=True),
        )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    decision_id: str
    client_order_id: str
    session_date: date
    symbol: str
    side: Side
    sequence: int
    reference_price: Decimal
    created_at: datetime
    notional: Decimal | None = None
    quantity: Decimal | None = None
    reason: str = "rebalance"

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("OrderIntent.decision_id is required")
        if not self.client_order_id or len(self.client_order_id) > MAX_CLIENT_ORDER_ID_LENGTH:
            raise ValueError("OrderIntent.client_order_id is invalid")
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("OrderIntent.sequence must be nonnegative")
        object.__setattr__(self, "created_at", as_utc(self.created_at))
        reference = decimal_value(self.reference_price, field_name="reference_price")
        if reference <= 0:
            raise ValueError("OrderIntent.reference_price must be positive")
        object.__setattr__(self, "reference_price", reference)
        present = int(self.notional is not None) + int(self.quantity is not None)
        if present != 1:
            raise ValueError("OrderIntent must specify exactly one of notional or quantity")
        if self.notional is not None:
            amount = decimal_value(self.notional, field_name="notional", nonnegative=True)
            if amount <= 0:
                raise ValueError("OrderIntent.notional must be positive")
            object.__setattr__(self, "notional", amount)
        if self.quantity is not None:
            quantity = decimal_value(self.quantity, field_name="quantity", nonnegative=True)
            if quantity <= 0:
                raise ValueError("OrderIntent.quantity must be positive")
            object.__setattr__(self, "quantity", quantity)

    @property
    def estimated_notional(self) -> Decimal:
        if self.notional is not None:
            return self.notional
        if self.quantity is None:  # Defensive guard for objects bypassing dataclass validation.
            raise ValueError("OrderIntent has neither notional nor quantity")
        return self.quantity * self.reference_price


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    event: str
    order: BrokerOrderState
    timestamp: datetime
    event_id: str | None = None
    execution_id: str | None = None
    fill_quantity: Decimal | None = None
    fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event", str(self.event).lower())
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        for name in ("fill_quantity", "fill_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, decimal_value(value, field_name=name, nonnegative=True)
                )

    @property
    def fingerprint(self) -> str:
        if self.event_id:
            return f"event:{self.event_id}"
        if self.execution_id:
            return f"execution:{self.execution_id}:{self.event}"
        payload = {
            "broker_order_id": self.order.broker_order_id,
            "client_order_id": self.order.client_order_id,
            "event": self.event,
            "timestamp": self.timestamp.isoformat(),
            "filled_quantity": str(self.order.filled_quantity),
            "average_fill_price": str(self.order.average_fill_price),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"hash:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class DataFreshnessState:
    checked_at: datetime
    stream_healthy: bool
    stale_after_seconds: int
    last_bar_by_symbol: Mapping[str, datetime] = field(default_factory=dict)
    missing_symbols: tuple[str, ...] = ()
    stale_symbols: tuple[str, ...] = ()
    unresolved_gap: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked_at", as_utc(self.checked_at))
        normalized = {
            normalize_symbol(symbol): as_utc(timestamp)
            for symbol, timestamp in self.last_bar_by_symbol.items()
        }
        object.__setattr__(self, "last_bar_by_symbol", normalized)
        object.__setattr__(
            self,
            "missing_symbols",
            tuple(sorted(normalize_symbol(s) for s in self.missing_symbols)),
        )
        object.__setattr__(
            self, "stale_symbols", tuple(sorted(normalize_symbol(s) for s in self.stale_symbols))
        )
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")

    @property
    def fresh(self) -> bool:
        return (
            self.stream_healthy
            and not self.missing_symbols
            and not self.stale_symbols
            and not self.unresolved_gap
        )


@dataclass(frozen=True, slots=True)
class SubmissionGateResult:
    allowed: bool
    reasons: tuple[str, ...]
    effective_mode: str


@dataclass(frozen=True, slots=True)
class PlanningResult:
    intents: tuple[OrderIntent, ...]
    skipped: tuple[dict[str, Any], ...] = ()

    @property
    def sells(self) -> tuple[OrderIntent, ...]:
        return tuple(intent for intent in self.intents if intent.side is Side.SELL)

    @property
    def buys(self) -> tuple[OrderIntent, ...]:
        return tuple(intent for intent in self.intents if intent.side is Side.BUY)


@dataclass(frozen=True, slots=True)
class ReconciliationDiscrepancy:
    kind: str
    severity: DiscrepancySeverity
    message: str
    symbol: str | None = None
    client_order_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    reconciliation_id: str
    started_at: datetime
    completed_at: datetime
    discrepancies: tuple[ReconciliationDiscrepancy, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", as_utc(self.started_at))
        object.__setattr__(self, "completed_at", as_utc(self.completed_at))

    @property
    def blocking(self) -> bool:
        return any(
            item.severity in {DiscrepancySeverity.BLOCKING, DiscrepancySeverity.CRITICAL}
            for item in self.discrepancies
        )

    @property
    def clean(self) -> bool:
        return not self.discrepancies


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    sequence: int
    event_type: ReplayEventType
    timestamp: datetime
    payload: Any = None

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("ReplayEvent.sequence must be nonnegative")
        object.__setattr__(self, "event_type", ReplayEventType(self.event_type))
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))


def finite_weight_map(values: Mapping[str, float]) -> dict[str, Decimal]:
    """Normalize a long-only target weight mapping for order planning."""

    normalized: dict[str, Decimal] = {}
    for symbol, raw in values.items():
        if isinstance(raw, bool):
            raise ValueError(f"Weight for {symbol} must be finite")
        try:
            numeric = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Weight for {symbol} must be finite") from exc
        if not isfinite(numeric) or numeric < 0:
            raise SafetyViolation(f"Weight for {symbol} must be finite and nonnegative")
        normalized[normalize_symbol(symbol)] = Decimal(str(numeric))
    return normalized
