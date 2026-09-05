"""Signed fill reconstruction, broker comparison, and blocking reconciliation controls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.execution.models import (
    AccountState,
    BrokerOrder,
    DiscrepancyCode,
    ExecutionValidationError,
    Fill,
    Incident,
    OrderIntent,
    OrderSide,
    OrderState,
    Position,
    PositionEffect,
    ReconciliationDiscrepancy,
    ReconciliationReceipt,
)
from adaptive_trader.platform.execution.repository import ExecutionRepository
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.latches import (
    RiskLatchEvent,
    RiskLatchKind,
    RiskLatchState,
    create_latch_engagement,
)

QUANTITY_TOLERANCE = Decimal("0.000001")
CASH_TOLERANCE = Decimal("0.01")
EQUITY_TOLERANCE = Decimal("0.01")
_HASH = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)


@dataclass(frozen=True, slots=True)
class ReconciliationRequest:
    """Complete trusted local and sanitized broker state for one comparison."""

    experiment_hash: str
    slot_id: str | None
    execution_plan_id: str | None
    correlation_id: str
    active_symbols: tuple[str, ...]
    short_eligible_symbols: tuple[str, ...]
    baseline_positions: tuple[Position, ...]
    baseline_cash: Decimal
    fills: tuple[Fill, ...]
    intents: tuple[OrderIntent, ...]
    durable_orders: tuple[BrokerOrder, ...]
    broker_orders: tuple[BrokerOrder, ...]
    broker_positions: tuple[Position, ...]
    broker_account: AccountState
    expected_account_id_hash: str
    mark_prices: tuple[tuple[str, Decimal], ...]
    started_at: datetime
    completed_at: datetime
    live_endpoint_detected: bool = False
    paper_false_detected: bool = False
    require_flat: bool = False
    required_flat_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.active_symbols) is not tuple or self.active_symbols != tuple(
            sorted(set(self.active_symbols))
        ):
            raise ExecutionValidationError("active reconciliation symbols must be ordered")
        if type(self.short_eligible_symbols) is not tuple or self.short_eligible_symbols != tuple(
            sorted(set(self.short_eligible_symbols))
        ):
            raise ExecutionValidationError("short eligibility symbols must be ordered")
        if not set(self.short_eligible_symbols).issubset(self.active_symbols):
            raise ExecutionValidationError("short eligibility must be a subset of active symbols")
        _tuple_of(self.baseline_positions, Position, "baseline positions")
        _tuple_of(self.fills, Fill, "fills")
        _tuple_of(self.intents, OrderIntent, "intents")
        _tuple_of(self.durable_orders, BrokerOrder, "durable orders")
        _tuple_of(self.broker_orders, BrokerOrder, "broker orders")
        _tuple_of(self.broker_positions, Position, "broker positions")
        if type(self.broker_account) is not AccountState:
            raise ExecutionValidationError("broker account state is invalid")
        if type(self.baseline_cash) is not Decimal or not self.baseline_cash.is_finite():
            raise ExecutionValidationError("baseline cash must be a finite Decimal")
        if type(self.mark_prices) is not tuple or any(
            type(item) is not tuple or len(item) != 2 for item in self.mark_prices
        ):
            raise ExecutionValidationError("mark prices must be immutable symbol-price pairs")
        mark_symbols = tuple(symbol for symbol, _ in self.mark_prices)
        if mark_symbols != tuple(sorted(set(mark_symbols))):
            raise ExecutionValidationError("mark prices must be unique and ordered")
        if any(
            type(price) is not Decimal or price <= 0 or not price.is_finite()
            for _, price in self.mark_prices
        ):
            raise ExecutionValidationError("mark prices must be positive finite Decimals")
        if set(mark_symbols) != set(self.active_symbols):
            raise ExecutionValidationError("mark prices must contain exactly active symbols")
        if type(self.expected_account_id_hash) is not str or (
            _HASH.fullmatch(self.expected_account_id_hash) is None
        ):
            raise ExecutionValidationError("expected account ID hash is invalid")
        try:
            started_at = require_utc_instant(self.started_at, field_name="started_at")
            completed_at = require_utc_instant(self.completed_at, field_name="completed_at")
            required_flat_at = (
                None
                if self.required_flat_at is None
                else require_utc_instant(self.required_flat_at, field_name="required_flat_at")
            )
        except DomainValidationError:
            raise ExecutionValidationError(
                "reconciliation timestamps must be timezone-aware UTC"
            ) from None
        if completed_at < started_at:
            raise ExecutionValidationError("reconciliation completion precedes start")
        if (
            type(self.live_endpoint_detected) is not bool
            or type(self.paper_false_detected) is not bool
        ):
            raise ExecutionValidationError("reconciliation safety flags must be boolean")
        if type(self.require_flat) is not bool:
            raise ExecutionValidationError("required-flat flag must be boolean")
        if self.require_flat != (required_flat_at is not None):
            raise ExecutionValidationError("required-flat deadline and flag disagree")


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """Receipt plus append-only controls required by a blocking result."""

    receipt: ReconciliationReceipt
    latch_event: RiskLatchEvent | None
    incident: Incident | None


def reconstruct_signed_positions(
    *,
    baseline_positions: tuple[Position, ...],
    fills: tuple[Fill, ...],
) -> tuple[Position, ...]:
    """Reconstruct signed quantities using BUY=positive and SELL=negative deltas."""

    _tuple_of(baseline_positions, Position, "baseline positions")
    _tuple_of(fills, Fill, "fills")
    quantities: dict[str, Decimal] = {}
    for position in baseline_positions:
        if position.symbol in quantities:
            raise ExecutionValidationError("baseline contains a duplicate position")
        quantities[position.symbol] = position.quantity
    execution_ids: set[str] = set()
    for fill in fills:
        if fill.broker_execution_id in execution_ids:
            continue
        execution_ids.add(fill.broker_execution_id)
        delta = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        quantities[fill.symbol] = quantities.get(fill.symbol, Decimal(0)) + delta
    return tuple(
        Position(symbol=symbol, quantity=quantity)
        for symbol, quantity in sorted(quantities.items())
        if quantity != 0
    )


def reconcile(request: ReconciliationRequest) -> ReconciliationReceipt:
    """Produce a complete immutable comparison without mutating any durable state."""

    if type(request) is not ReconciliationRequest:
        raise ExecutionValidationError("reconciliation requires a validated request")
    discrepancies: list[ReconciliationDiscrepancy] = []
    if request.live_endpoint_detected:
        discrepancies.append(_difference(DiscrepancyCode.LIVE_ENDPOINT_DETECTED))
    if request.paper_false_detected:
        discrepancies.append(_difference(DiscrepancyCode.PAPER_FALSE_DETECTED))

    duplicate_execution_ids = _duplicates(tuple(fill.broker_execution_id for fill in request.fills))
    discrepancies.extend(
        _difference(DiscrepancyCode.DUPLICATE_EXECUTION_ID) for _ in duplicate_execution_ids
    )
    duplicate_intent_ids = _duplicates(tuple(intent.client_order_id for intent in request.intents))
    discrepancies.extend(
        _difference(DiscrepancyCode.DUPLICATE_CLIENT_ORDER_ID, client_order_id=client_id)
        for client_id in duplicate_intent_ids
    )

    expected_positions = reconstruct_signed_positions(
        baseline_positions=request.baseline_positions,
        fills=request.fills,
    )
    expected_map = {position.symbol: position.quantity for position in expected_positions}
    observed_map = _position_map(request.broker_positions, discrepancies=discrepancies)
    for symbol in sorted(set(expected_map) | set(observed_map)):
        expected = expected_map.get(symbol, Decimal(0))
        observed = observed_map.get(symbol, Decimal(0))
        if symbol not in request.active_symbols and (expected != 0 or observed != 0):
            discrepancies.append(
                _difference(
                    DiscrepancyCode.NON_ACTIVE_SYMBOL_POSITION,
                    symbol=symbol,
                    expected=expected,
                    observed=observed,
                )
            )
            continue
        if expected == 0 and observed != 0:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.UNEXPECTED_POSITION,
                    symbol=symbol,
                    expected=expected,
                    observed=observed,
                )
            )
        elif expected * observed < 0:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.POSITION_SIGN_MISMATCH,
                    symbol=symbol,
                    expected=expected,
                    observed=observed,
                )
            )
        elif abs(expected - observed) > QUANTITY_TOLERANCE:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.POSITION_QUANTITY_MISMATCH,
                    symbol=symbol,
                    expected=expected,
                    observed=observed,
                )
            )

    if request.require_flat:
        for symbol, observed in sorted(observed_map.items()):
            if abs(observed) > QUANTITY_TOLERANCE:
                discrepancies.append(
                    _difference(
                        DiscrepancyCode.REQUIRED_FLAT_NOT_PROVEN,
                        symbol=symbol,
                        expected=Decimal(0),
                        observed=observed,
                    )
                )
        if request.required_flat_at is not None and request.completed_at > request.required_flat_at:
            discrepancies.append(_difference(DiscrepancyCode.FORCED_FLAT_DEADLINE_MISSED))

    intent_map = _intent_map(request.intents)
    durable = _order_map(
        request.durable_orders,
        duplicate_code=DiscrepancyCode.DUPLICATE_CLIENT_ORDER_ID,
        discrepancies=discrepancies,
    )
    for symbol, quantity in sorted(expected_map.items()):
        if quantity >= 0:
            continue
        traceable = any(
            intent.symbol == symbol
            and intent.position_effect in {PositionEffect.OPEN_SHORT, PositionEffect.INCREASE_SHORT}
            and intent.client_order_id in durable
            and any(fill.client_order_id == intent.client_order_id for fill in request.fills)
            for intent in request.intents
        )
        if not traceable:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.UNTRACEABLE_SHORT_POSITION,
                    symbol=symbol,
                    expected=quantity,
                    observed=observed_map.get(symbol),
                )
            )
    for intent in request.intents:
        if intent.symbol not in request.active_symbols:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.NON_ACTIVE_SYMBOL_ORDER,
                    symbol=intent.symbol,
                    client_order_id=intent.client_order_id,
                )
            )
        if (
            intent.position_effect in {PositionEffect.OPEN_SHORT, PositionEffect.INCREASE_SHORT}
            and intent.symbol not in request.short_eligible_symbols
        ):
            discrepancies.append(
                _difference(
                    DiscrepancyCode.INELIGIBLE_SHORT_INCREASE,
                    symbol=intent.symbol,
                    client_order_id=intent.client_order_id,
                )
            )

    observed_orders = _order_map(
        request.broker_orders,
        duplicate_code=DiscrepancyCode.DUPLICATE_CLIENT_ORDER_ID,
        discrepancies=discrepancies,
    )
    for client_id, unknown_candidate in observed_orders.items():
        if client_id not in intent_map:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.UNKNOWN_BROKER_ORDER,
                    client_order_id=client_id,
                )
            )
            if unknown_candidate.state.ambiguous:
                discrepancies.append(
                    _difference(
                        DiscrepancyCode.SUBMISSION_UNKNOWN,
                        client_order_id=client_id,
                    )
                )
    for client_id, local_order in durable.items():
        persisted_fill_quantity = sum(
            (
                fill.quantity
                for fill in {
                    fill.broker_execution_id: fill
                    for fill in request.fills
                    if fill.client_order_id == client_id
                }.values()
            ),
            start=Decimal(0),
        )
        if local_order.cumulative_filled_quantity != persisted_fill_quantity:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.FILLED_QUANTITY_MISMATCH,
                    client_order_id=client_id,
                    expected=local_order.cumulative_filled_quantity,
                    observed=persisted_fill_quantity,
                )
            )
        matching_broker_order = observed_orders.get(client_id)
        if local_order.state is OrderState.INTENT_COMMITTED:
            continue
        if local_order.state.ambiguous:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.SUBMISSION_UNKNOWN,
                    client_order_id=client_id,
                )
            )
        if matching_broker_order is None:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.MISSING_BROKER_ORDER,
                    client_order_id=client_id,
                )
            )
            continue
        if (
            local_order.cumulative_filled_quantity
            != matching_broker_order.cumulative_filled_quantity
        ):
            discrepancies.append(
                _difference(
                    DiscrepancyCode.FILLED_QUANTITY_MISMATCH,
                    client_order_id=client_id,
                    expected=local_order.cumulative_filled_quantity,
                    observed=matching_broker_order.cumulative_filled_quantity,
                )
            )
        if local_order.state is not matching_broker_order.state:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.ORDER_STATE_MISMATCH,
                    client_order_id=client_id,
                )
            )

    expected_cash = request.baseline_cash
    unique_fills = {fill.broker_execution_id: fill for fill in request.fills}
    for fill in unique_fills.values():
        notional = fill.quantity * fill.price
        expected_cash += (
            notional - fill.fee if fill.side is OrderSide.SELL else -(notional + fill.fee)
        )
    if request.broker_account.account_id_hash != request.expected_account_id_hash:
        discrepancies.append(_difference(DiscrepancyCode.ACCOUNT_ID_MISMATCH))
    if abs(expected_cash - request.broker_account.cash) > CASH_TOLERANCE:
        discrepancies.append(
            _difference(
                DiscrepancyCode.CASH_MISMATCH,
                expected=expected_cash,
                observed=request.broker_account.cash,
            )
        )
    marks = dict(request.mark_prices)
    expected_equity = expected_cash + sum(
        quantity * marks.get(symbol, Decimal(0)) for symbol, quantity in expected_map.items()
    )
    if abs(expected_equity - request.broker_account.equity) > EQUITY_TOLERANCE:
        discrepancies.append(
            _difference(
                DiscrepancyCode.EQUITY_MISMATCH,
                expected=expected_equity,
                observed=request.broker_account.equity,
            )
        )

    ordered = tuple(sorted(set(discrepancies), key=_difference_key))
    return ReconciliationReceipt.create(
        experiment_hash=request.experiment_hash,
        slot_id=request.slot_id,
        execution_plan_id=request.execution_plan_id,
        correlation_id=request.correlation_id,
        account_id_hash=request.broker_account.account_id_hash,
        started_at=request.started_at,
        completed_at=request.completed_at,
        expected_positions=expected_positions,
        observed_positions=tuple(
            Position(symbol=symbol, quantity=quantity)
            for symbol, quantity in sorted(observed_map.items())
            if quantity != 0
        ),
        expected_cash=expected_cash,
        observed_cash=request.broker_account.cash,
        expected_equity=expected_equity,
        observed_equity=request.broker_account.equity,
        fill_hashes=tuple(sorted({fill.content_hash for fill in request.fills})),
        order_hashes=tuple(
            sorted(
                {order.content_hash for order in (*request.durable_orders, *request.broker_orders)}
            )
        ),
        discrepancies=ordered,
    )


def reconcile_and_persist(
    *,
    repository: ExecutionRepository,
    request: ReconciliationRequest,
    latch_state: RiskLatchState,
) -> ReconciliationOutcome:
    """Persist receipt and blocking controls as one repository transaction."""

    receipt = reconcile(request)
    latch_event: RiskLatchEvent | None = None
    incident: Incident | None = None
    if receipt.discrepancies:
        if not latch_state.is_active(RiskLatchKind.RECONCILIATION):
            latch_event = create_latch_engagement(
                latch_state=latch_state,
                latch_type=RiskLatchKind.RECONCILIATION,
                reason_code="reconciliation_blocking",
                actor="execution_worker",
                occurred_at=request.completed_at,
                correlation_id=request.correlation_id,
                idempotency_key=f"reconciliation_{receipt.content_hash[:32]}",
            )
        incident = Incident.create(
            idempotency_key=f"reconciliation:{receipt.content_hash[:32]}",
            experiment_hash=request.experiment_hash,
            correlation_id=request.correlation_id,
            reason_code="reconciliation_blocking",
            opened_at=request.completed_at,
        )
    repository.record_reconciliation_bundle(
        receipt,
        latch_event=latch_event,
        incident=incident,
    )
    return ReconciliationOutcome(receipt=receipt, latch_event=latch_event, incident=incident)


def _position_map(
    positions: tuple[Position, ...],
    *,
    discrepancies: list[ReconciliationDiscrepancy],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for position in positions:
        if position.symbol in result:
            discrepancies.append(
                _difference(
                    DiscrepancyCode.UNEXPECTED_POSITION,
                    symbol=position.symbol,
                    expected=result[position.symbol],
                    observed=position.quantity,
                )
            )
        else:
            result[position.symbol] = position.quantity
    return result


def _intent_map(intents: tuple[OrderIntent, ...]) -> dict[str, OrderIntent]:
    result: dict[str, OrderIntent] = {}
    for intent in intents:
        result.setdefault(intent.client_order_id, intent)
    return result


def _order_map(
    orders: tuple[BrokerOrder, ...],
    *,
    duplicate_code: DiscrepancyCode | None,
    discrepancies: list[ReconciliationDiscrepancy],
) -> dict[str, BrokerOrder]:
    result: dict[str, BrokerOrder] = {}
    for order in orders:
        if order.client_order_id in result:
            if duplicate_code is not None:
                discrepancies.append(
                    _difference(duplicate_code, client_order_id=order.client_order_id)
                )
        else:
            result[order.client_order_id] = order
    return result


def _difference(
    code: DiscrepancyCode,
    *,
    symbol: str | None = None,
    client_order_id: str | None = None,
    expected: Decimal | None = None,
    observed: Decimal | None = None,
) -> ReconciliationDiscrepancy:
    return ReconciliationDiscrepancy(
        code=code,
        symbol=symbol,
        client_order_id=client_order_id,
        expected=expected,
        observed=observed,
    )


def _difference_key(item: ReconciliationDiscrepancy) -> tuple[str, str, str, str, str]:
    return (
        item.code.value,
        item.symbol or "",
        item.client_order_id or "",
        "" if item.expected is None else format(item.expected, "f"),
        "" if item.observed is None else format(item.observed, "f"),
    )


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _tuple_of(value: object, expected_type: type[object], description: str) -> None:
    if type(value) is not tuple or any(type(item) is not expected_type for item in value):
        raise ExecutionValidationError(f"{description} must be an immutable validated tuple")


def reconciliation_input_hash(request: ReconciliationRequest) -> str:
    """Hash a complete sanitized reconciliation request for diagnostics."""

    if type(request) is not ReconciliationRequest:
        raise ExecutionValidationError("reconciliation hash requires a validated request")
    return sha256_hex(
        {
            "account_id_hash": request.broker_account.account_id_hash,
            "broker_order_hashes": tuple(order.content_hash for order in request.broker_orders),
            "completed_at": request.completed_at,
            "durable_order_hashes": tuple(order.content_hash for order in request.durable_orders),
            "fill_hashes": tuple(fill.content_hash for fill in request.fills),
            "schema": "reconciliation-input-v1",
            "started_at": request.started_at,
        }
    )
