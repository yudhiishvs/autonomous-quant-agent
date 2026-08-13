"""Long-only order planning, enablement gates, and idempotent paper execution."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from adaptive_trader.broker import Broker
from adaptive_trader.clock import as_utc
from adaptive_trader.constants import (
    MAX_CLIENT_ORDER_ID_LENGTH,
    NORMAL_ORDER_MODES,
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    PROJECT_ORDER_PREFIX,
)
from adaptive_trader.exceptions import (
    AmbiguousSubmissionError,
    InvalidOrderTransition,
    SafetyViolation,
)
from adaptive_trader.live_models import (
    AccountState,
    BrokerOrderState,
    DataFreshnessState,
    LocalOrderState,
    MarketClockState,
    OrderIntent,
    PlanningResult,
    PositionState,
    RunMode,
    Side,
    SubmissionGateResult,
    TradeUpdate,
    decimal_value,
    finite_weight_map,
    normalize_symbol,
)
from adaptive_trader.logging_config import redact
from adaptive_trader.persistence import AuditRepository

TERMINAL_STATES = frozenset(
    {
        LocalOrderState.FILLED,
        LocalOrderState.CANCELED,
        LocalOrderState.REJECTED,
        LocalOrderState.EXPIRED,
        LocalOrderState.REPLACED,
    }
)


class OrderStateMachine:
    """Explicit validator for every local order-state transition."""

    _TRANSITIONS: Mapping[LocalOrderState, frozenset[LocalOrderState]] = {
        LocalOrderState.PLANNED: frozenset({LocalOrderState.LOCALLY_RESERVED}),
        LocalOrderState.LOCALLY_RESERVED: frozenset(
            {
                LocalOrderState.SUBMISSION_STARTED,
                LocalOrderState.SUBMITTED,
                LocalOrderState.ACCEPTED,
                LocalOrderState.PENDING,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.SUBMISSION_STARTED: frozenset(
            {
                LocalOrderState.SUBMITTED,
                LocalOrderState.ACCEPTED,
                LocalOrderState.PENDING,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.SUBMISSION_UNKNOWN,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.SUBMITTED: frozenset(
            {
                LocalOrderState.SUBMITTED,
                LocalOrderState.ACCEPTED,
                LocalOrderState.PENDING,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCEL_REQUESTED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.REPLACED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.ACCEPTED: frozenset(
            {
                LocalOrderState.ACCEPTED,
                LocalOrderState.PENDING,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCEL_REQUESTED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.REPLACED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.PENDING: frozenset(
            {
                LocalOrderState.PENDING,
                LocalOrderState.ACCEPTED,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCEL_REQUESTED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.REPLACED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.PARTIALLY_FILLED: frozenset(
            {
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCEL_REQUESTED,
                LocalOrderState.CANCELED,
                LocalOrderState.EXPIRED,
                LocalOrderState.REPLACED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.CANCEL_REQUESTED: frozenset(
            {
                LocalOrderState.CANCEL_REQUESTED,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.SUBMISSION_UNKNOWN: frozenset(
            {
                LocalOrderState.SUBMITTED,
                LocalOrderState.ACCEPTED,
                LocalOrderState.PENDING,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.RECONCILIATION_REQUIRED: frozenset(
            {
                LocalOrderState.SUBMITTED,
                LocalOrderState.ACCEPTED,
                LocalOrderState.PENDING,
                LocalOrderState.PARTIALLY_FILLED,
                LocalOrderState.FILLED,
                LocalOrderState.CANCELED,
                LocalOrderState.REJECTED,
                LocalOrderState.EXPIRED,
                LocalOrderState.REPLACED,
                LocalOrderState.RECONCILIATION_REQUIRED,
            }
        ),
        LocalOrderState.FILLED: frozenset({LocalOrderState.FILLED}),
        LocalOrderState.CANCELED: frozenset({LocalOrderState.CANCELED}),
        LocalOrderState.REJECTED: frozenset({LocalOrderState.REJECTED}),
        LocalOrderState.EXPIRED: frozenset({LocalOrderState.EXPIRED}),
        LocalOrderState.REPLACED: frozenset({LocalOrderState.REPLACED}),
    }

    @classmethod
    def validate(cls, current: LocalOrderState, target: LocalOrderState) -> None:
        if target not in cls._TRANSITIONS.get(current, frozenset()):
            raise InvalidOrderTransition(
                f"Invalid local order transition: {current.value} -> {target.value}"
            )

    @classmethod
    def can_transition(cls, current: LocalOrderState, target: LocalOrderState) -> bool:
        return target in cls._TRANSITIONS.get(current, frozenset())


def state_for_broker_status(status: str) -> LocalOrderState:
    normalized = str(status).strip().lower()
    mapping = {
        "new": LocalOrderState.SUBMITTED,
        "submitted": LocalOrderState.SUBMITTED,
        "accepted": LocalOrderState.ACCEPTED,
        "pending": LocalOrderState.PENDING,
        "pending_new": LocalOrderState.PENDING,
        "pending_cancel": LocalOrderState.PENDING,
        "pending_replace": LocalOrderState.PENDING,
        "pending_review": LocalOrderState.PENDING,
        "accepted_for_bidding": LocalOrderState.PENDING,
        "held": LocalOrderState.PENDING,
        "calculated": LocalOrderState.PENDING,
        "stopped": LocalOrderState.PENDING,
        "suspended": LocalOrderState.PENDING,
        "done_for_day": LocalOrderState.PENDING,
        "partial_fill": LocalOrderState.PARTIALLY_FILLED,
        "partially_filled": LocalOrderState.PARTIALLY_FILLED,
        "fill": LocalOrderState.FILLED,
        "filled": LocalOrderState.FILLED,
        "canceled": LocalOrderState.CANCELED,
        "cancelled": LocalOrderState.CANCELED,
        "rejected": LocalOrderState.REJECTED,
        "expired": LocalOrderState.EXPIRED,
        "replaced": LocalOrderState.REPLACED,
        "order_replace_rejected": LocalOrderState.RECONCILIATION_REQUIRED,
        "order_cancel_rejected": LocalOrderState.RECONCILIATION_REQUIRED,
    }
    return mapping.get(normalized, LocalOrderState.RECONCILIATION_REQUIRED)


def deterministic_client_order_id(
    *,
    session_date: date,
    decision_id: str,
    symbol: str,
    side: Side | str,
    sequence: int,
) -> str:
    """Create a stable, readable Alpaca client ID no longer than 48 characters."""

    normalized_symbol = re.sub(r"[^A-Z0-9]", "", normalize_symbol(symbol))[:10] or "ASSET"
    side_value = Side(side).value[0]
    digest = hashlib.sha256(str(decision_id).encode()).hexdigest()[:10]
    result = (
        f"{PROJECT_ORDER_PREFIX}-{session_date:%Y%m%d}-{digest}-"
        f"{normalized_symbol}-{side_value}-{int(sequence):02d}"
    )
    if len(result) > MAX_CLIENT_ORDER_ID_LENGTH:
        result = result[:MAX_CLIENT_ORDER_ID_LENGTH]
    return result


def _price_and_time(value: Any) -> tuple[Decimal, datetime | None]:
    if hasattr(value, "close") and hasattr(value, "start"):
        return decimal_value(value.close, field_name="price", nonnegative=True), as_utc(value.start)
    if isinstance(value, tuple) and len(value) == 2:
        return decimal_value(value[0], field_name="price", nonnegative=True), as_utc(value[1])
    return decimal_value(value, field_name="price", nonnegative=True), None


class OrderPlanner:
    """Convert a permitted target into sell-first, cash-constrained paper intents."""

    def __init__(
        self,
        *,
        minimum_order_notional: Decimal | str | float = Decimal("25"),
        max_single_order_fraction_of_equity: Decimal | str | float = Decimal("0.20"),
        required_cash_buffer: Decimal | str | float = Decimal("0.02"),
        maximum_orders: int = 20,
        stale_after_seconds: int = 180,
    ) -> None:
        self.minimum_order_notional = decimal_value(
            minimum_order_notional, field_name="minimum_order_notional", nonnegative=True
        )
        self.max_single_order_fraction = decimal_value(
            max_single_order_fraction_of_equity,
            field_name="max_single_order_fraction_of_equity",
            nonnegative=True,
        )
        self.required_cash_buffer = decimal_value(
            required_cash_buffer, field_name="required_cash_buffer", nonnegative=True
        )
        if not Decimal("0") <= self.required_cash_buffer < Decimal("1"):
            raise ValueError("required_cash_buffer must be below one")
        if not Decimal("0") < self.max_single_order_fraction <= Decimal("1"):
            raise ValueError("max_single_order_fraction_of_equity must be in (0, 1]")
        if maximum_orders <= 0 or stale_after_seconds <= 0:
            raise ValueError("maximum_orders and stale_after_seconds must be positive")
        self.maximum_orders = int(maximum_orders)
        self.stale_after_seconds = int(stale_after_seconds)

    @classmethod
    def from_config(cls, config: Any) -> OrderPlanner:
        execution_config = getattr(config, "execution", config)
        market_config = getattr(config, "market_data", config)
        return cls(
            minimum_order_notional=getattr(
                execution_config, "minimum_order_notional", Decimal("25")
            ),
            max_single_order_fraction_of_equity=getattr(
                execution_config, "max_single_order_fraction_of_equity", Decimal("0.20")
            ),
            required_cash_buffer=getattr(execution_config, "required_cash_buffer", Decimal("0.02")),
            maximum_orders=getattr(execution_config, "maximum_orders_per_rebalance", 20),
            stale_after_seconds=getattr(market_config, "stale_after_seconds", 180),
        )

    def plan(
        self,
        *,
        decision_id: str,
        session_date: date,
        created_at: datetime,
        target_weights: Mapping[str, float],
        account: AccountState,
        positions: Sequence[PositionState],
        latest_prices: Mapping[str, Any],
        universe: Sequence[str],
        open_orders: Sequence[BrokerOrderState] = (),
    ) -> PlanningResult:
        now = as_utc(created_at)
        weights = finite_weight_map(target_weights)
        allowed_symbols = {normalize_symbol(symbol) for symbol in universe}
        unsupported = sorted(set(weights).difference(allowed_symbols))
        if unsupported:
            raise SafetyViolation(f"Target contains symbols outside the universe: {unsupported}")

        skips: list[dict[str, Any]] = []
        max_gross = Decimal("1") - self.required_cash_buffer
        gross = sum(weights.values(), Decimal("0"))
        if gross > max_gross and gross > 0:
            scale = max_gross / gross
            weights = {symbol: weight * scale for symbol, weight in weights.items()}
            skips.append(
                {
                    "reason": "cash_buffer_scaled",
                    "gross_before": str(gross),
                    "gross_after": str(sum(weights.values(), Decimal("0"))),
                }
            )

        positions_by_symbol = {position.symbol: position for position in positions}
        for position in positions:
            if position.quantity < 0:
                raise SafetyViolation(f"Negative broker position detected for {position.symbol}")
            if position.symbol not in allowed_symbols:
                raise SafetyViolation(f"Unexpected broker position detected: {position.symbol}")

        conflicting_symbols = {
            order.symbol
            for order in open_orders
            if state_for_broker_status(order.status) not in TERMINAL_STATES
        }
        max_order_notional = account.equity * self.max_single_order_fraction
        deltas: list[tuple[Side, str, Decimal, Decimal, datetime | None]] = []
        for symbol in sorted(allowed_symbols):
            if symbol not in latest_prices:
                skips.append({"symbol": symbol, "reason": "missing_price"})
                continue
            price, price_time = _price_and_time(latest_prices[symbol])
            if price <= 0:
                raise SafetyViolation(f"Price for {symbol} must be positive")
            if (
                price_time is not None
                and (now - price_time).total_seconds() > self.stale_after_seconds
            ):
                skips.append({"symbol": symbol, "reason": "stale_price"})
                continue
            current_position = positions_by_symbol.get(symbol)
            current_value = (
                Decimal("0") if current_position is None else current_position.market_value
            )
            target_value = weights.get(symbol, Decimal("0")) * account.equity
            delta = target_value - current_value
            if abs(delta) < self.minimum_order_notional:
                if delta:
                    skips.append(
                        {"symbol": symbol, "reason": "below_minimum_notional", "delta": str(delta)}
                    )
                continue
            if symbol in conflicting_symbols:
                skips.append({"symbol": symbol, "reason": "conflicting_open_order"})
                continue
            side = Side.BUY if delta > 0 else Side.SELL
            deltas.append((side, symbol, min(abs(delta), max_order_notional), price, price_time))

        # A pending sale cannot fund a buy.  Reserve the configured cash buffer
        # and only plan buys against cash already present at the broker.
        reserved_open_buys = sum(
            (order.requested_notional or Decimal("0"))
            for order in open_orders
            if order.side is Side.BUY
            and state_for_broker_status(order.status) not in TERMINAL_STATES
        )
        buy_budget = max(
            Decimal("0"),
            account.cash - account.equity * self.required_cash_buffer - reserved_open_buys,
        )
        ordered_deltas = sorted(deltas, key=lambda row: (row[0] is Side.BUY, row[1]))
        intents: list[OrderIntent] = []
        for side, symbol, notional, price, _ in ordered_deltas:
            if len(intents) >= self.maximum_orders:
                skips.append({"symbol": symbol, "reason": "maximum_order_count"})
                continue
            if side is Side.BUY:
                permitted = min(notional, buy_budget)
                if permitted < self.minimum_order_notional:
                    skips.append({"symbol": symbol, "reason": "insufficient_cash"})
                    continue
                buy_budget -= permitted
                amount: dict[str, Decimal | None] = {"notional": permitted, "quantity": None}
            else:
                sell_position = positions_by_symbol.get(symbol)
                if sell_position is None or sell_position.quantity <= 0:
                    skips.append({"symbol": symbol, "reason": "no_long_position_to_sell"})
                    continue
                quantity = min(sell_position.quantity, notional / price).quantize(
                    Decimal("0.000000001"), rounding=ROUND_DOWN
                )
                if quantity <= 0:
                    skips.append({"symbol": symbol, "reason": "sell_rounds_to_zero"})
                    continue
                amount = {"notional": None, "quantity": quantity}
            sequence = len(intents)
            intents.append(
                OrderIntent(
                    decision_id=decision_id,
                    client_order_id=deterministic_client_order_id(
                        session_date=session_date,
                        decision_id=decision_id,
                        symbol=symbol,
                        side=side,
                        sequence=sequence,
                    ),
                    session_date=session_date,
                    symbol=symbol,
                    side=side,
                    sequence=sequence,
                    reference_price=price,
                    created_at=now,
                    notional=amount["notional"],
                    quantity=amount["quantity"],
                )
            )
        return PlanningResult(intents=tuple(intents), skipped=tuple(skips))


def _config_value(config: Any, section: str, name: str, default: Any) -> Any:
    target = getattr(config, section, config)
    return getattr(target, name, default)


def evaluate_order_enablement(
    config: Any,
    *,
    mode: RunMode | str,
    environment: Mapping[str, str] | None,
    broker_paper_only: bool,
    credentials_verified: bool,
    provider_authority_verified: bool,
    startup_preflight_verified: bool,
    account: AccountState | None,
    market_clock: MarketClockState | None,
    freshness: DataFreshnessState | None,
    reconciliation_clean: bool,
    halt_active: bool,
    before_cutoff: bool = True,
    dry_run: bool = False,
) -> SubmissionGateResult:
    """Evaluate every normal paper-order enablement condition in one place."""

    mode_value = str(getattr(mode, "value", mode)).replace("-", "_")
    values = os.environ if environment is None else environment
    reasons: list[str] = []
    if mode_value not in NORMAL_ORDER_MODES:
        reasons.append("the invoked command is not a paper-order command")
    if dry_run:
        reasons.append("dry-run mode never submits orders")
    if not bool(_config_value(config, "execution", "paper_only", True)):
        reasons.append("configuration does not enforce paper-only execution")
    if not bool(_config_value(config, "execution", "paper_order_submission_enabled", False)):
        reasons.append("execution.paper_order_submission_enabled is false")
    if values.get(PAPER_ORDER_ENABLEMENT_ENV) != PAPER_ORDER_ACKNOWLEDGEMENT:
        reasons.append(f"{PAPER_ORDER_ENABLEMENT_ENV} does not contain the exact acknowledgement")
    if not broker_paper_only:
        reasons.append("broker adapter is not structurally paper-only")
    if not credentials_verified:
        reasons.append("dedicated paper credentials were not explicitly verified")
    if not provider_authority_verified:
        reasons.append("real Alpaca paper broker/data-provider authority was not verified")
    if not startup_preflight_verified:
        reasons.append("feed, asset, and completed-history startup preflights are incomplete")
    if account is None:
        reasons.append("paper-account credential check did not succeed")
    else:
        if account.status.upper() != "ACTIVE":
            reasons.append(f"paper account status is {account.status}")
        if account.trading_blocked:
            reasons.append("paper account is trading-blocked")
    if market_clock is None:
        reasons.append("market clock is unavailable")
    elif not market_clock.is_open:
        reasons.append("regular US equity market is closed")
    if not before_cutoff:
        reasons.append("the configured strategy-order cutoff has passed")
    if freshness is None or not freshness.fresh:
        reasons.append("required market data is not fresh and healthy")
    if not reconciliation_clean:
        reasons.append("the latest reconciliation is blocking or unavailable")
    if halt_active:
        reasons.append("a persistent halt latch is active")
    allowed = not reasons
    effective = mode_value if allowed else ("dry_run" if dry_run else "observer")
    return SubmissionGateResult(allowed=allowed, reasons=tuple(reasons), effective_mode=effective)


SubmissionGate = Callable[..., SubmissionGateResult]


class OrderManager:
    """Persist-first order manager resilient to duplicate events and restarts."""

    def __init__(
        self,
        *,
        repository: AuditRepository,
        broker: Broker,
        run_id: str,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.run_id = run_id

    def _transition(
        self,
        client_order_id: str,
        target: LocalOrderState,
        *,
        event_type: str,
        event_key: str,
        created_at: datetime,
        broker_order: BrokerOrderState | None = None,
        payload: Mapping[str, Any] | None = None,
        error_message: str | None = None,
    ) -> bool:
        row = self.repository.get_order(client_order_id)
        if row is None:
            raise SafetyViolation(f"Unknown local order: {client_order_id}")
        current = LocalOrderState(str(row["state"]))
        OrderStateMachine.validate(current, target)
        return self.repository.transition_order(
            client_order_id=client_order_id,
            to_state=target,
            event_type=event_type,
            event_key=event_key,
            allowed_from={current},
            created_at=created_at,
            broker_order_id=None if broker_order is None else broker_order.broker_order_id,
            raw_status=None if broker_order is None else broker_order.status,
            filled_quantity=None if broker_order is None else broker_order.filled_quantity,
            average_fill_price=(None if broker_order is None else broker_order.average_fill_price),
            payload=payload,
            error_message=error_message,
        )

    def submit_intent(self, intent: OrderIntent) -> BrokerOrderState | None:
        """Submit at most once; ambiguous outcomes are reconciled, never retried."""

        reserved = self.repository.reserve_order_intent(run_id=self.run_id, intent=intent)
        if not reserved:
            # A prior process already reserved or submitted this exact intent.
            return next(
                (
                    order
                    for order in self.broker.get_orders(include_closed=True)
                    if order.client_order_id == intent.client_order_id
                ),
                None,
            )

        existing_broker = next(
            (
                order
                for order in self.broker.get_orders(include_closed=True, after=intent.created_at)
                if order.client_order_id == intent.client_order_id
            ),
            None,
        )
        if existing_broker is not None:
            target = state_for_broker_status(existing_broker.status)
            self._transition(
                intent.client_order_id,
                target,
                event_type="pre_submission_reconciliation",
                event_key=f"broker-existing:{existing_broker.broker_order_id}:{existing_broker.status}",
                created_at=existing_broker.updated_at,
                broker_order=existing_broker,
            )
            return existing_broker

        self._transition(
            intent.client_order_id,
            LocalOrderState.SUBMISSION_STARTED,
            event_type="submission_started",
            event_key=f"submission-started:{intent.client_order_id}",
            created_at=intent.created_at,
        )
        try:
            broker_order = self.broker.submit_order(intent)
        except AmbiguousSubmissionError as exc:
            self._transition(
                intent.client_order_id,
                LocalOrderState.SUBMISSION_UNKNOWN,
                event_type="submission_unknown",
                event_key=f"submission-unknown:{intent.client_order_id}",
                created_at=intent.created_at,
                error_message=redact(str(exc)),
            )
            return None
        target = state_for_broker_status(broker_order.status)
        self._transition(
            intent.client_order_id,
            target,
            event_type="broker_submission_response",
            event_key=(
                f"submission-response:{broker_order.broker_order_id}:"
                f"{broker_order.status}:{broker_order.filled_quantity}"
            ),
            created_at=broker_order.updated_at,
            broker_order=broker_order,
        )
        return broker_order

    def process_trade_update(self, update: TradeUpdate) -> bool:
        # Event identity is checked before state validation.  A delayed duplicate
        # of an earlier state (for example ``accepted`` after a fill) is a no-op,
        # not an invalid backwards transition.
        if self.repository.has_order_event(update.fingerprint):
            return False
        row = self.repository.get_order(update.order.client_order_id)
        if row is None:
            self.repository.record_incident(
                run_id=self.run_id,
                incident_type="fill_or_order_update_without_known_order",
                severity="critical",
                message="Paper trade update referenced an unknown client order ID",
                details={
                    "client_order_id": update.order.client_order_id,
                    "broker_order_id": update.order.broker_order_id,
                    "event": update.event,
                },
            )
            return False
        target = state_for_broker_status(update.event or update.order.status)
        current = LocalOrderState(str(row["state"]))
        prior_filled = Decimal(str(row.get("filled_quantity") or "0"))
        OrderStateMachine.validate(current, target)
        changed = self.repository.transition_order(
            client_order_id=update.order.client_order_id,
            to_state=target,
            event_type="trade_update",
            event_key=update.fingerprint,
            allowed_from={current},
            created_at=update.timestamp,
            broker_order_id=update.order.broker_order_id,
            raw_status=update.order.status,
            filled_quantity=update.order.filled_quantity,
            average_fill_price=update.order.average_fill_price,
            payload={"event": update.event, "execution_id": update.execution_id},
        )
        fill_delta = update.order.filled_quantity - prior_filled
        if (
            changed
            and target in {LocalOrderState.PARTIALLY_FILLED, LocalOrderState.FILLED}
            and fill_delta > 0
            and update.fill_price is not None
        ):
            self.repository.record_fill(
                TradeUpdate(
                    event=update.event,
                    order=update.order,
                    timestamp=update.timestamp,
                    event_id=update.event_id,
                    execution_id=update.execution_id,
                    fill_quantity=fill_delta,
                    fill_price=update.fill_price,
                )
            )
        return changed

    def execute(
        self,
        intents: Iterable[OrderIntent],
        *,
        reconcile_after_sells: Callable[[], Any] | None = None,
    ) -> tuple[BrokerOrderState | None, ...]:
        ordered = sorted(intents, key=lambda intent: (intent.side is Side.BUY, intent.sequence))
        results: list[BrokerOrderState | None] = []
        sells = [intent for intent in ordered if intent.side is Side.SELL]
        buys = [intent for intent in ordered if intent.side is Side.BUY]
        for intent in sells:
            results.append(self.submit_intent(intent))
        sell_reconciliation_blocking = False
        if sells and reconcile_after_sells is not None:
            reconciliation = reconcile_after_sells()
            sell_reconciliation_blocking = bool(getattr(reconciliation, "blocking", True))
        unresolved_sell = False
        for intent in sells:
            row = self.repository.get_order(intent.client_order_id)
            if row is not None and LocalOrderState(str(row["state"])) not in TERMINAL_STATES:
                unresolved_sell = True
        if unresolved_sell or sell_reconciliation_blocking:
            # Cash from an unfilled reduction is never presumed available.
            return tuple(results)
        for intent in buys:
            results.append(self.submit_intent(intent))
        return tuple(results)
