"""Immutable signed-execution, order, fill, and reconciliation contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self, cast

from adaptive_trader.platform.domain import (
    DeterministicId,
    require_finite_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_HASH = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_CLIENT_ORDER_ID = re.compile(
    r"^aqa-[0-9a-f]{5}-[0-9a-f]{5}-[A-Z][A-Z0-9.]{0,9}-[xef]-[0-9a-f]{2}-[0-9]{1,6}-[0-9a-f]{6}$",
    flags=re.ASCII,
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", flags=re.ASCII)
_CONTENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}_[0-9a-f]{64}$", flags=re.ASCII)
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", flags=re.ASCII)
_QUANTITY_TOLERANCE = Decimal("0.000001")


class ExecutionValidationError(DomainValidationError):
    """Raised when execution state violates the closed signed contract."""


class OrderSide(StrEnum):
    """Broker side; position effect carries the actual semantic authority."""

    BUY = "BUY"
    SELL = "SELL"


class PositionEffect(StrEnum):
    """Explicit signed-position transition semantics."""

    OPEN_LONG = "OPEN_LONG"
    INCREASE_LONG = "INCREASE_LONG"
    REDUCE_LONG = "REDUCE_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    INCREASE_SHORT = "INCREASE_SHORT"
    REDUCE_SHORT = "REDUCE_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    FORCED_FLAT_LONG = "FORCED_FLAT_LONG"
    FORCED_FLAT_SHORT = "FORCED_FLAT_SHORT"

    @property
    def opens_exposure(self) -> bool:
        """Return whether this effect can increase absolute exposure."""

        return self in {
            PositionEffect.OPEN_LONG,
            PositionEffect.INCREASE_LONG,
            PositionEffect.OPEN_SHORT,
            PositionEffect.INCREASE_SHORT,
        }

    @property
    def reduces_exposure(self) -> bool:
        """Return whether this effect can only move a position toward zero."""

        return not self.opens_exposure


class IntentPhase(StrEnum):
    """Close-first order phases."""

    EXIT = "EXIT"
    ENTRY = "ENTRY"
    FLATTEN = "FLATTEN"

    @property
    def client_id_code(self) -> str:
        """Return the compact, stable phase fragment used in client order IDs."""

        return {
            IntentPhase.EXIT: "x",
            IntentPhase.ENTRY: "e",
            IntentPhase.FLATTEN: "f",
        }[self]


class OrderState(StrEnum):
    """Exact durable broker-order state machine."""

    PLANNED = "PLANNED"
    INTENT_COMMITTED = "INTENT_COMMITTED"
    SUBMISSION_STARTED = "SUBMISSION_STARTED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    @property
    def terminal(self) -> bool:
        """Return whether no ordinary broker update may follow this state."""

        return self in {
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }

    @property
    def ambiguous(self) -> bool:
        """Return whether reconciliation is required before new exposure."""

        return self in {
            OrderState.SUBMISSION_UNKNOWN,
            OrderState.RECONCILIATION_REQUIRED,
        }


class ReconciliationSeverity(StrEnum):
    """Closed severity classification for reconciliation discrepancies."""

    BLOCKING = "BLOCKING"
    CRITICAL = "CRITICAL"


class ReconciliationStatus(StrEnum):
    """Signed reconciliation result."""

    CLEAN = "CLEAN"
    BLOCKING = "BLOCKING"


class DiscrepancyCode(StrEnum):
    """Closed fail-closed reconciliation discrepancy taxonomy."""

    DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
    UNKNOWN_BROKER_ORDER = "unknown_broker_order"
    MISSING_BROKER_ORDER = "missing_broker_order"
    FILLED_QUANTITY_MISMATCH = "filled_quantity_mismatch"
    ORDER_STATE_MISMATCH = "order_state_mismatch"
    UNEXPECTED_POSITION = "unexpected_position"
    POSITION_SIGN_MISMATCH = "position_sign_mismatch"
    POSITION_QUANTITY_MISMATCH = "position_quantity_mismatch"
    UNTRACEABLE_SHORT_POSITION = "untraceable_short_position"
    INELIGIBLE_SHORT_INCREASE = "ineligible_short_increase"
    ACCOUNT_ID_MISMATCH = "account_id_mismatch"
    CASH_MISMATCH = "cash_mismatch"
    EQUITY_MISMATCH = "equity_mismatch"
    SUBMISSION_UNKNOWN = "submission_unknown"
    LIVE_ENDPOINT_DETECTED = "live_endpoint_detected"
    PAPER_FALSE_DETECTED = "paper_false_detected"
    NON_ACTIVE_SYMBOL_POSITION = "non_active_symbol_position"
    NON_ACTIVE_SYMBOL_ORDER = "non_active_symbol_order"
    DUPLICATE_EXECUTION_ID = "duplicate_execution_id"
    REQUIRED_FLAT_NOT_PROVEN = "required_flat_not_proven"
    FORCED_FLAT_DEADLINE_MISSED = "forced_flat_deadline_missed"

    @property
    def severity(self) -> ReconciliationSeverity:
        """Return the specification-defined severity."""

        if self in {
            DiscrepancyCode.LIVE_ENDPOINT_DETECTED,
            DiscrepancyCode.PAPER_FALSE_DETECTED,
            DiscrepancyCode.NON_ACTIVE_SYMBOL_POSITION,
            DiscrepancyCode.NON_ACTIVE_SYMBOL_ORDER,
            DiscrepancyCode.DUPLICATE_EXECUTION_ID,
        }:
            return ReconciliationSeverity.CRITICAL
        return ReconciliationSeverity.BLOCKING


@dataclass(frozen=True, slots=True)
class Position:
    """One exact signed position."""

    symbol: str
    quantity: Decimal

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _decimal(self.quantity, field_name="position quantity")


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Immutable execution plan bound to one signed risk receipt."""

    execution_plan_id: str
    risk_decision_id: str
    risk_decision_hash: str
    experiment_hash: str
    correlation_id: str
    target_version: int
    forced_flat: bool
    target_quantities: tuple[Position, ...]
    created_at: datetime
    deadline_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        _content_id(self.execution_plan_id, prefix="execution")
        _identifier(self.risk_decision_id, field_name="risk decision ID")
        _hash(self.risk_decision_hash, field_name="risk decision hash")
        _hash(self.experiment_hash, field_name="experiment hash")
        _identifier(self.correlation_id, field_name="correlation ID")
        if type(self.target_version) is not int or not 1 <= self.target_version <= 999_999:
            raise ExecutionValidationError("target version must be between one and 999999")
        if type(self.forced_flat) is not bool:
            raise ExecutionValidationError("forced-flat flag must be boolean")
        _positions(self.target_quantities, allow_empty=False)
        created_at = _instant(self.created_at, field_name="plan creation time")
        deadline_at = _instant(self.deadline_at, field_name="plan deadline")
        if created_at >= deadline_at:
            raise ExecutionValidationError("execution plan deadline must follow creation")
        expected = _execution_plan_digest(
            risk_decision_id=self.risk_decision_id,
            risk_decision_hash=self.risk_decision_hash,
            experiment_hash=self.experiment_hash,
            correlation_id=self.correlation_id,
            target_version=self.target_version,
            forced_flat=self.forced_flat,
            target_quantities=self.target_quantities,
            created_at=created_at,
            deadline_at=deadline_at,
        )
        if self.content_hash != expected or self.execution_plan_id != f"execution_{expected}":
            raise ExecutionValidationError("execution plan identity or hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        risk_decision_id: str,
        risk_decision_hash: str,
        experiment_hash: str,
        correlation_id: str,
        target_version: int,
        forced_flat: bool,
        target_quantities: tuple[Position, ...],
        created_at: datetime,
        deadline_at: datetime,
    ) -> ExecutionPlan:
        """Create a content-addressed signed plan."""

        digest = _execution_plan_digest(
            risk_decision_id=risk_decision_id,
            risk_decision_hash=risk_decision_hash,
            experiment_hash=experiment_hash,
            correlation_id=correlation_id,
            target_version=target_version,
            forced_flat=forced_flat,
            target_quantities=target_quantities,
            created_at=created_at,
            deadline_at=deadline_at,
        )
        return cls(
            execution_plan_id=DeterministicId(prefix="execution", digest=digest).value,
            risk_decision_id=risk_decision_id,
            risk_decision_hash=risk_decision_hash,
            experiment_hash=experiment_hash,
            correlation_id=correlation_id,
            target_version=target_version,
            forced_flat=forced_flat,
            target_quantities=target_quantities,
            created_at=created_at,
            deadline_at=deadline_at,
            content_hash=digest,
        )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Durable broker side-effect intent persisted before submission starts."""

    order_intent_id: str
    execution_plan_id: str
    risk_decision_id: str
    experiment_hash: str
    correlation_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    position_effect: PositionEffect
    phase: IntentPhase
    sequence: int
    target_version: int
    quantity: Decimal
    notional: Decimal
    reference_price: Decimal
    final_target_quantity: Decimal
    forced_flat: bool
    created_at: datetime
    deadline_at: datetime
    target_hash: str
    content_hash: str

    def __post_init__(self) -> None:
        _content_id(self.order_intent_id, prefix="intent")
        _content_id(self.execution_plan_id, prefix="execution")
        _identifier(self.risk_decision_id, field_name="risk decision ID")
        _hash(self.experiment_hash, field_name="experiment hash")
        _identifier(self.correlation_id, field_name="correlation ID")
        if (
            type(self.client_order_id) is not str
            or len(self.client_order_id) > 48
            or _CLIENT_ORDER_ID.fullmatch(self.client_order_id) is None
        ):
            raise ExecutionValidationError("client order ID violates the deterministic contract")
        _symbol(self.symbol)
        if type(self.side) is not OrderSide:
            raise ExecutionValidationError("order side is invalid")
        if type(self.position_effect) is not PositionEffect:
            raise ExecutionValidationError("position effect is invalid")
        if type(self.phase) is not IntentPhase:
            raise ExecutionValidationError("intent phase is invalid")
        if type(self.sequence) is not int or not 0 <= self.sequence < 16:
            raise ExecutionValidationError("intent sequence must be between zero and 15")
        if type(self.target_version) is not int or not 1 <= self.target_version <= 999_999:
            raise ExecutionValidationError("target version must be between one and 999999")
        quantity = _positive_decimal(self.quantity, field_name="order quantity")
        notional = _positive_decimal(self.notional, field_name="order notional")
        price = _positive_decimal(self.reference_price, field_name="reference price")
        _decimal(self.final_target_quantity, field_name="final target quantity")
        if notional != quantity * price:
            raise ExecutionValidationError("order notional must equal quantity times price")
        if type(self.forced_flat) is not bool:
            raise ExecutionValidationError("forced-flat flag must be boolean")
        created_at = _instant(self.created_at, field_name="intent creation time")
        deadline_at = _instant(self.deadline_at, field_name="intent deadline")
        if created_at >= deadline_at:
            raise ExecutionValidationError("intent deadline must follow creation")
        _hash(self.target_hash, field_name="target hash")
        _validate_effect(self)
        expected = _intent_digest(self)
        if self.content_hash != expected or self.order_intent_id != f"intent_{expected}":
            raise ExecutionValidationError("order intent identity or hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        execution_plan_id: str,
        risk_decision_id: str,
        experiment_hash: str,
        correlation_id: str,
        symbol: str,
        side: OrderSide,
        position_effect: PositionEffect,
        phase: IntentPhase,
        sequence: int,
        target_version: int,
        quantity: Decimal,
        reference_price: Decimal,
        final_target_quantity: Decimal,
        forced_flat: bool,
        created_at: datetime,
        deadline_at: datetime,
        target_hash: str,
    ) -> OrderIntent:
        """Create a deterministic intent and bounded client order ID."""

        _hash(experiment_hash, field_name="experiment hash")
        _hash(target_hash, field_name="target hash")
        decision_fragment = _identifier_digest(risk_decision_id)[:5]
        client_order_id = (
            f"aqa-{experiment_hash[:5]}-{decision_fragment}-{symbol}-"
            f"{phase.client_id_code}-{sequence:02x}-{target_version}-{target_hash[:6]}"
        )
        values: dict[str, object] = {
            "execution_plan_id": execution_plan_id,
            "risk_decision_id": risk_decision_id,
            "experiment_hash": experiment_hash,
            "correlation_id": correlation_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "position_effect": position_effect,
            "phase": phase,
            "sequence": sequence,
            "target_version": target_version,
            "quantity": quantity,
            "notional": quantity * reference_price,
            "reference_price": reference_price,
            "final_target_quantity": final_target_quantity,
            "forced_flat": forced_flat,
            "created_at": created_at,
            "deadline_at": deadline_at,
            "target_hash": target_hash,
        }
        digest = sha256_hex({"schema": "order-intent-v1", **values})
        return cls(
            order_intent_id=DeterministicId(prefix="intent", digest=digest).value,
            content_hash=digest,
            **values,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """Versioned durable projection of one intent's broker state."""

    client_order_id: str
    order_intent_id: str
    broker_order_id: str | None
    state: OrderState
    submitted_at: datetime | None
    accepted_at: datetime | None
    updated_at: datetime
    cumulative_filled_quantity: Decimal
    average_fill_price: Decimal | None
    last_event_sequence: int
    safe_error_code: str | None
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        _client_order_id(self.client_order_id)
        _content_id(self.order_intent_id, prefix="intent")
        if self.broker_order_id is not None:
            _identifier(self.broker_order_id, field_name="broker order ID")
        if type(self.state) is not OrderState:
            raise ExecutionValidationError("broker order state is invalid")
        submitted_at = _optional_instant(self.submitted_at, field_name="submitted time")
        accepted_at = _optional_instant(self.accepted_at, field_name="accepted time")
        updated_at = _instant(self.updated_at, field_name="order update time")
        filled = _nonnegative_decimal(
            self.cumulative_filled_quantity,
            field_name="cumulative filled quantity",
        )
        if self.average_fill_price is not None:
            _positive_decimal(self.average_fill_price, field_name="average fill price")
            if filled == 0:
                raise ExecutionValidationError("zero-filled order cannot have an average price")
        elif filled != 0:
            raise ExecutionValidationError("filled order must have an average price")
        if type(self.last_event_sequence) is not int or self.last_event_sequence < 0:
            raise ExecutionValidationError("order event sequence cannot be negative")
        if self.safe_error_code is not None:
            _safe_code(self.safe_error_code)
        if type(self.version) is not int or self.version < 1:
            raise ExecutionValidationError("broker order version must be positive")
        if self.state is OrderState.INTENT_COMMITTED and (
            self.broker_order_id is not None or self.submitted_at is not None
        ):
            raise ExecutionValidationError("committed intent cannot already contain broker state")
        if accepted_at is not None and submitted_at is None:
            raise ExecutionValidationError("accepted order requires submission time")
        if accepted_at is not None and submitted_at is not None and accepted_at < submitted_at:
            raise ExecutionValidationError("acceptance time cannot precede submission time")
        expected = _broker_order_digest(self)
        if self.content_hash != expected:
            raise ExecutionValidationError("broker order content hash is invalid")
        if submitted_at is not None and submitted_at > updated_at:
            raise ExecutionValidationError("submission time cannot follow update time")
        if accepted_at is not None and accepted_at > updated_at:
            raise ExecutionValidationError("acceptance time cannot follow update time")

    @classmethod
    def committed(cls, intent: OrderIntent) -> BrokerOrder:
        """Create the initial durable projection atomically with an intent."""

        values: dict[str, object] = {
            "client_order_id": intent.client_order_id,
            "order_intent_id": intent.order_intent_id,
            "broker_order_id": None,
            "state": OrderState.INTENT_COMMITTED,
            "submitted_at": None,
            "accepted_at": None,
            "updated_at": intent.created_at,
            "cumulative_filled_quantity": Decimal(0),
            "average_fill_price": None,
            "last_event_sequence": 0,
            "safe_error_code": None,
            "version": 1,
        }
        return cls(content_hash=sha256_hex({"schema": "broker-order-v1", **values}), **values)  # type: ignore[arg-type]

    def evolve(
        self,
        *,
        state: OrderState,
        updated_at: datetime,
        broker_order_id: str | None = None,
        submitted_at: datetime | None = None,
        accepted_at: datetime | None = None,
        cumulative_filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        safe_error_code: str | None = None,
    ) -> Self:
        """Apply one validated monotonic state transition."""

        from adaptive_trader.platform.execution.state_machine import validate_order_transition

        validate_order_transition(self.state, state)
        next_updated_at = _instant(updated_at, field_name="order update time")
        if next_updated_at < self.updated_at:
            raise ExecutionValidationError("order update time cannot move backward")
        next_sequence = self.last_event_sequence + 1
        next_broker_order_id = (
            broker_order_id if broker_order_id is not None else self.broker_order_id
        )
        next_submitted_at = submitted_at if submitted_at is not None else self.submitted_at
        next_accepted_at = accepted_at if accepted_at is not None else self.accepted_at
        next_filled_quantity = (
            cumulative_filled_quantity
            if cumulative_filled_quantity is not None
            else self.cumulative_filled_quantity
        )
        next_average_price = (
            average_fill_price if average_fill_price is not None else self.average_fill_price
        )
        next_version = self.version + 1
        values: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "order_intent_id": self.order_intent_id,
            "broker_order_id": next_broker_order_id,
            "state": state,
            "submitted_at": next_submitted_at,
            "accepted_at": next_accepted_at,
            "updated_at": next_updated_at,
            "cumulative_filled_quantity": next_filled_quantity,
            "average_fill_price": next_average_price,
            "last_event_sequence": next_sequence,
            "safe_error_code": safe_error_code,
            "version": next_version,
        }
        if next_filled_quantity < self.cumulative_filled_quantity:
            raise ExecutionValidationError("cumulative fill quantity cannot decrease")
        return replace(
            self,
            broker_order_id=next_broker_order_id,
            state=state,
            submitted_at=next_submitted_at,
            accepted_at=next_accepted_at,
            updated_at=next_updated_at,
            cumulative_filled_quantity=next_filled_quantity,
            average_fill_price=next_average_price,
            last_event_sequence=next_sequence,
            safe_error_code=safe_error_code,
            version=next_version,
            content_hash=sha256_hex({"schema": "broker-order-v1", **values}),
        )


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Append-only evidence for one validated broker-order transition."""

    order_event_id: str
    client_order_id: str
    sequence: int
    from_state: OrderState
    to_state: OrderState
    broker_event_id: str | None
    occurred_at: datetime
    safe_error_code: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _content_id(self.order_event_id, prefix="order_event")
        _client_order_id(self.client_order_id)
        if type(self.sequence) is not int or self.sequence < 1:
            raise ExecutionValidationError("order event sequence must be positive")
        if type(self.from_state) is not OrderState or type(self.to_state) is not OrderState:
            raise ExecutionValidationError("order event states are invalid")
        if self.broker_event_id is not None:
            _identifier(self.broker_event_id, field_name="broker event ID")
        _instant(self.occurred_at, field_name="order event time")
        if self.safe_error_code is not None:
            _safe_code(self.safe_error_code)
        expected = _order_event_digest(self)
        if self.content_hash != expected or self.order_event_id != f"order_event_{expected}":
            raise ExecutionValidationError("order event identity or hash is invalid")

    @classmethod
    def from_orders(
        cls,
        previous: BrokerOrder,
        current: BrokerOrder,
        *,
        broker_event_id: str | None = None,
    ) -> OrderEvent:
        """Create append-only evidence from adjacent projection versions."""

        if previous.client_order_id != current.client_order_id:
            raise ExecutionValidationError("order event cannot cross client order IDs")
        if current.version != previous.version + 1:
            raise ExecutionValidationError("order event requires adjacent order versions")
        values: dict[str, object] = {
            "client_order_id": current.client_order_id,
            "sequence": current.last_event_sequence,
            "from_state": previous.state,
            "to_state": current.state,
            "broker_event_id": broker_event_id,
            "occurred_at": current.updated_at,
            "safe_error_code": current.safe_error_code,
        }
        digest = sha256_hex({"schema": "order-event-v1", **values})
        return cls(
            order_event_id=DeterministicId(prefix="order_event", digest=digest).value,
            content_hash=digest,
            **values,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """Append-only idempotent broker execution."""

    fill_id: str
    client_order_id: str
    broker_execution_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    occurred_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        _content_id(self.fill_id, prefix="fill")
        _client_order_id(self.client_order_id)
        _identifier(self.broker_execution_id, field_name="broker execution ID")
        _symbol(self.symbol)
        if type(self.side) is not OrderSide:
            raise ExecutionValidationError("fill side is invalid")
        _positive_decimal(self.quantity, field_name="fill quantity")
        _positive_decimal(self.price, field_name="fill price")
        _nonnegative_decimal(self.fee, field_name="fill fee")
        _instant(self.occurred_at, field_name="fill time")
        expected = _fill_digest(self)
        if self.content_hash != expected or self.fill_id != f"fill_{expected}":
            raise ExecutionValidationError("fill identity or hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        client_order_id: str,
        broker_execution_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        occurred_at: datetime,
    ) -> Fill:
        """Create deterministic fill evidence."""

        values: dict[str, object] = {
            "client_order_id": client_order_id,
            "broker_execution_id": broker_execution_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "occurred_at": occurred_at,
        }
        digest = sha256_hex({"schema": "fill-v1", **values})
        return cls(
            fill_id=DeterministicId(prefix="fill", digest=digest).value,
            content_hash=digest,
            **values,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class AccountState:
    """Broker account values used by execution and reconciliation."""

    account_id: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    restricted_short_proceeds: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if type(self.account_id) is not str or _ACCOUNT_ID.fullmatch(self.account_id) is None:
            raise ExecutionValidationError("broker account ID is invalid")
        _decimal(self.cash, field_name="account cash")
        _decimal(self.equity, field_name="account equity")
        _nonnegative_decimal(self.buying_power, field_name="buying power")
        _nonnegative_decimal(
            self.restricted_short_proceeds,
            field_name="restricted short proceeds",
        )
        _instant(self.observed_at, field_name="account observation time")

    @property
    def account_id_hash(self) -> str:
        """Return the non-reversible account identity used by durable receipts."""

        return sha256_hex(("broker-account-v1", self.account_id))


@dataclass(frozen=True, slots=True)
class ReconciliationDiscrepancy:
    """One closed, hash-safe reconciliation difference."""

    code: DiscrepancyCode
    symbol: str | None
    client_order_id: str | None
    expected: Decimal | None
    observed: Decimal | None

    def __post_init__(self) -> None:
        if type(self.code) is not DiscrepancyCode:
            raise ExecutionValidationError("reconciliation discrepancy code is invalid")
        if self.symbol is not None:
            _symbol(self.symbol)
        if self.client_order_id is not None:
            _client_order_id(self.client_order_id)
        if self.expected is not None:
            _decimal(self.expected, field_name="expected reconciliation value")
        if self.observed is not None:
            _decimal(self.observed, field_name="observed reconciliation value")


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    """Immutable reconciliation proof binding fills, broker state, and discrepancies."""

    reconciliation_id: str
    experiment_hash: str
    slot_id: str | None
    execution_plan_id: str | None
    correlation_id: str
    account_id_hash: str
    started_at: datetime
    completed_at: datetime
    status: ReconciliationStatus
    expected_positions: tuple[Position, ...]
    observed_positions: tuple[Position, ...]
    expected_cash: Decimal
    observed_cash: Decimal
    expected_equity: Decimal
    observed_equity: Decimal
    fill_hashes: tuple[str, ...]
    order_hashes: tuple[str, ...]
    discrepancies: tuple[ReconciliationDiscrepancy, ...]
    content_hash: str

    def __post_init__(self) -> None:
        _content_id(self.reconciliation_id, prefix="reconciliation")
        _hash(self.experiment_hash, field_name="experiment hash")
        if self.slot_id is not None:
            _identifier(self.slot_id, field_name="slot ID")
        if self.execution_plan_id is not None:
            _content_id(self.execution_plan_id, prefix="execution")
        _identifier(self.correlation_id, field_name="correlation ID")
        _hash(self.account_id_hash, field_name="account ID hash")
        started_at = _instant(self.started_at, field_name="reconciliation start")
        completed_at = _instant(self.completed_at, field_name="reconciliation completion")
        if completed_at < started_at:
            raise ExecutionValidationError("reconciliation completion precedes start")
        if type(self.status) is not ReconciliationStatus:
            raise ExecutionValidationError("reconciliation status is invalid")
        _positions(self.expected_positions, allow_empty=True)
        _positions(self.observed_positions, allow_empty=True)
        for value, field_name in (
            (self.expected_cash, "expected cash"),
            (self.observed_cash, "observed cash"),
            (self.expected_equity, "expected equity"),
            (self.observed_equity, "observed equity"),
        ):
            _decimal(value, field_name=field_name)
        _hash_tuple(self.fill_hashes, field_name="fill hashes")
        _hash_tuple(self.order_hashes, field_name="order hashes")
        if type(self.discrepancies) is not tuple or any(
            type(item) is not ReconciliationDiscrepancy for item in self.discrepancies
        ):
            raise ExecutionValidationError("reconciliation discrepancies must be immutable")
        if tuple(sorted(self.discrepancies, key=_discrepancy_key)) != self.discrepancies:
            raise ExecutionValidationError("reconciliation discrepancies must be ordered")
        expected_status = (
            ReconciliationStatus.CLEAN if not self.discrepancies else ReconciliationStatus.BLOCKING
        )
        if self.status is not expected_status:
            raise ExecutionValidationError("reconciliation status disagrees with discrepancies")
        expected = _reconciliation_digest(self)
        if self.content_hash != expected or self.reconciliation_id != f"reconciliation_{expected}":
            raise ExecutionValidationError("reconciliation identity or hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        experiment_hash: str,
        slot_id: str | None,
        execution_plan_id: str | None,
        correlation_id: str,
        account_id_hash: str,
        started_at: datetime,
        completed_at: datetime,
        expected_positions: tuple[Position, ...],
        observed_positions: tuple[Position, ...],
        expected_cash: Decimal,
        observed_cash: Decimal,
        expected_equity: Decimal,
        observed_equity: Decimal,
        fill_hashes: tuple[str, ...],
        order_hashes: tuple[str, ...],
        discrepancies: tuple[ReconciliationDiscrepancy, ...],
    ) -> ReconciliationReceipt:
        """Create a deterministic signed reconciliation receipt."""

        ordered = tuple(sorted(discrepancies, key=_discrepancy_key))
        status = ReconciliationStatus.CLEAN if not ordered else ReconciliationStatus.BLOCKING
        values: dict[str, object] = {
            "experiment_hash": experiment_hash,
            "slot_id": slot_id,
            "execution_plan_id": execution_plan_id,
            "correlation_id": correlation_id,
            "account_id_hash": account_id_hash,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
            "expected_positions": expected_positions,
            "observed_positions": observed_positions,
            "expected_cash": expected_cash,
            "observed_cash": observed_cash,
            "expected_equity": expected_equity,
            "observed_equity": observed_equity,
            "fill_hashes": fill_hashes,
            "order_hashes": order_hashes,
            "discrepancies": ordered,
        }
        digest = sha256_hex({"schema": "reconciliation-v1", **_receipt_values(values)})
        return cls(
            reconciliation_id=DeterministicId(prefix="reconciliation", digest=digest).value,
            content_hash=digest,
            **values,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class Incident:
    """Durable blocking incident; success cannot conceal a flatten failure."""

    incident_id: str
    idempotency_key: str
    experiment_hash: str
    correlation_id: str
    reason_code: str
    opened_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        _content_id(self.incident_id, prefix="incident")
        _identifier(self.idempotency_key, field_name="incident idempotency key")
        _hash(self.experiment_hash, field_name="experiment hash")
        _identifier(self.correlation_id, field_name="correlation ID")
        _safe_code(self.reason_code)
        _instant(self.opened_at, field_name="incident open time")
        expected = sha256_hex(
            {
                "correlation_id": self.correlation_id,
                "experiment_hash": self.experiment_hash,
                "idempotency_key": self.idempotency_key,
                "opened_at": self.opened_at,
                "reason_code": self.reason_code,
                "schema": "execution-incident-v1",
            }
        )
        if self.content_hash != expected or self.incident_id != f"incident_{expected}":
            raise ExecutionValidationError("incident identity or hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        idempotency_key: str,
        experiment_hash: str,
        correlation_id: str,
        reason_code: str,
        opened_at: datetime,
    ) -> Incident:
        """Create one retry-stable incident."""

        values = {
            "correlation_id": correlation_id,
            "experiment_hash": experiment_hash,
            "idempotency_key": idempotency_key,
            "opened_at": opened_at,
            "reason_code": reason_code,
            "schema": "execution-incident-v1",
        }
        digest = sha256_hex(values)
        return cls(
            incident_id=DeterministicId(prefix="incident", digest=digest).value,
            idempotency_key=idempotency_key,
            experiment_hash=experiment_hash,
            correlation_id=correlation_id,
            reason_code=reason_code,
            opened_at=opened_at,
            content_hash=digest,
        )


def _execution_plan_digest(
    *,
    risk_decision_id: str,
    risk_decision_hash: str,
    experiment_hash: str,
    correlation_id: str,
    target_version: int,
    forced_flat: bool,
    target_quantities: tuple[Position, ...],
    created_at: datetime,
    deadline_at: datetime,
) -> str:
    return sha256_hex(
        {
            "correlation_id": correlation_id,
            "created_at": created_at,
            "deadline_at": deadline_at,
            "experiment_hash": experiment_hash,
            "forced_flat": forced_flat,
            "risk_decision_hash": risk_decision_hash,
            "risk_decision_id": risk_decision_id,
            "schema": "execution-plan-v1",
            "target_quantities": tuple(
                (position.symbol, position.quantity) for position in target_quantities
            ),
            "target_version": target_version,
        }
    )


def _intent_digest(intent: OrderIntent) -> str:
    return sha256_hex(
        {
            "client_order_id": intent.client_order_id,
            "correlation_id": intent.correlation_id,
            "created_at": intent.created_at,
            "deadline_at": intent.deadline_at,
            "execution_plan_id": intent.execution_plan_id,
            "experiment_hash": intent.experiment_hash,
            "final_target_quantity": intent.final_target_quantity,
            "forced_flat": intent.forced_flat,
            "notional": intent.notional,
            "phase": intent.phase,
            "position_effect": intent.position_effect,
            "quantity": intent.quantity,
            "reference_price": intent.reference_price,
            "risk_decision_id": intent.risk_decision_id,
            "schema": "order-intent-v1",
            "sequence": intent.sequence,
            "side": intent.side,
            "symbol": intent.symbol,
            "target_hash": intent.target_hash,
            "target_version": intent.target_version,
        }
    )


def _broker_order_digest(order: BrokerOrder) -> str:
    return sha256_hex(
        {
            "accepted_at": order.accepted_at,
            "average_fill_price": order.average_fill_price,
            "broker_order_id": order.broker_order_id,
            "client_order_id": order.client_order_id,
            "cumulative_filled_quantity": order.cumulative_filled_quantity,
            "last_event_sequence": order.last_event_sequence,
            "order_intent_id": order.order_intent_id,
            "safe_error_code": order.safe_error_code,
            "schema": "broker-order-v1",
            "state": order.state,
            "submitted_at": order.submitted_at,
            "updated_at": order.updated_at,
            "version": order.version,
        }
    )


def _order_event_digest(event: OrderEvent) -> str:
    return sha256_hex(
        {
            "broker_event_id": event.broker_event_id,
            "client_order_id": event.client_order_id,
            "from_state": event.from_state,
            "occurred_at": event.occurred_at,
            "safe_error_code": event.safe_error_code,
            "schema": "order-event-v1",
            "sequence": event.sequence,
            "to_state": event.to_state,
        }
    )


def _fill_digest(fill: Fill) -> str:
    return sha256_hex(
        {
            "broker_execution_id": fill.broker_execution_id,
            "client_order_id": fill.client_order_id,
            "fee": fill.fee,
            "occurred_at": fill.occurred_at,
            "price": fill.price,
            "quantity": fill.quantity,
            "schema": "fill-v1",
            "side": fill.side,
            "symbol": fill.symbol,
        }
    )


def _reconciliation_digest(receipt: ReconciliationReceipt) -> str:
    return sha256_hex(
        {
            "schema": "reconciliation-v1",
            **_receipt_values(
                {
                    field: getattr(receipt, field)
                    for field in receipt.__dataclass_fields__
                    if field not in {"reconciliation_id", "content_hash"}
                }
            ),
        }
    )


def _receipt_values(values: dict[str, object]) -> dict[str, object]:
    raw_discrepancies = values["discrepancies"]
    raw_expected_positions = values["expected_positions"]
    raw_observed_positions = values["observed_positions"]
    if not isinstance(raw_discrepancies, tuple):
        raise ExecutionValidationError("reconciliation discrepancies must be immutable")
    if not isinstance(raw_expected_positions, tuple) or not isinstance(
        raw_observed_positions,
        tuple,
    ):
        raise ExecutionValidationError("reconciliation positions must be immutable")
    discrepancies = cast(tuple[ReconciliationDiscrepancy, ...], raw_discrepancies)
    expected_positions = cast(tuple[Position, ...], raw_expected_positions)
    observed_positions = cast(tuple[Position, ...], raw_observed_positions)
    return {
        **values,
        "expected_positions": tuple(
            (position.symbol, position.quantity) for position in expected_positions
        ),
        "observed_positions": tuple(
            (position.symbol, position.quantity) for position in observed_positions
        ),
        "discrepancies": tuple(
            (
                discrepancy.code,
                discrepancy.symbol,
                discrepancy.client_order_id,
                discrepancy.expected,
                discrepancy.observed,
            )
            for discrepancy in discrepancies
        ),
    }


def _validate_effect(intent: OrderIntent) -> None:
    expected: dict[PositionEffect, tuple[OrderSide, IntentPhase]] = {
        PositionEffect.OPEN_LONG: (OrderSide.BUY, IntentPhase.ENTRY),
        PositionEffect.INCREASE_LONG: (OrderSide.BUY, IntentPhase.ENTRY),
        PositionEffect.REDUCE_LONG: (OrderSide.SELL, IntentPhase.EXIT),
        PositionEffect.CLOSE_LONG: (OrderSide.SELL, IntentPhase.EXIT),
        PositionEffect.OPEN_SHORT: (OrderSide.SELL, IntentPhase.ENTRY),
        PositionEffect.INCREASE_SHORT: (OrderSide.SELL, IntentPhase.ENTRY),
        PositionEffect.REDUCE_SHORT: (OrderSide.BUY, IntentPhase.EXIT),
        PositionEffect.CLOSE_SHORT: (OrderSide.BUY, IntentPhase.EXIT),
        PositionEffect.FORCED_FLAT_LONG: (OrderSide.SELL, IntentPhase.FLATTEN),
        PositionEffect.FORCED_FLAT_SHORT: (OrderSide.BUY, IntentPhase.FLATTEN),
    }
    if (intent.side, intent.phase) != expected[intent.position_effect]:
        raise ExecutionValidationError("side or phase disagrees with position effect")
    if intent.phase is IntentPhase.FLATTEN and not intent.forced_flat:
        raise ExecutionValidationError("flatten intent requires forced-flat authority")
    if intent.forced_flat and intent.phase is not IntentPhase.FLATTEN:
        raise ExecutionValidationError("forced-flat plan can contain only flatten intents")


def _positions(value: object, *, allow_empty: bool) -> tuple[Position, ...]:
    if type(value) is not tuple or any(type(position) is not Position for position in value):
        raise ExecutionValidationError("positions must be an immutable validated tuple")
    positions = value
    symbols = tuple(position.symbol for position in positions)
    if symbols != tuple(sorted(set(symbols))):
        raise ExecutionValidationError("positions must be unique and alphabetically ordered")
    if not allow_empty and not positions:
        raise ExecutionValidationError("positions cannot be empty")
    return positions


def _discrepancy_key(
    discrepancy: ReconciliationDiscrepancy,
) -> tuple[str, str, str]:
    return (
        discrepancy.code.value,
        discrepancy.symbol or "",
        discrepancy.client_order_id or "",
    )


def _hash_tuple(value: object, *, field_name: str) -> None:
    if type(value) is not tuple:
        raise ExecutionValidationError(f"{field_name} must be immutable")
    hashes = value
    if hashes != tuple(sorted(set(hashes))):
        raise ExecutionValidationError(f"{field_name} must be unique and ordered")
    for digest in hashes:
        _hash(digest, field_name=field_name)


def _symbol(value: object) -> str:
    if type(value) is not str or _SYMBOL.fullmatch(value) is None:
        raise ExecutionValidationError("symbol is invalid")
    return value


def _hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise ExecutionValidationError(f"{field_name} is invalid")
    return value


def _identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ExecutionValidationError(f"{field_name} is invalid")
    return value


def _safe_code(value: object) -> str:
    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        raise ExecutionValidationError("safe error or reason code is invalid")
    return value


def _content_id(value: object, *, prefix: str) -> str:
    if (
        type(value) is not str
        or _CONTENT_ID.fullmatch(value) is None
        or not value.startswith(f"{prefix}_")
    ):
        raise ExecutionValidationError(f"{prefix} ID is invalid")
    return value


def _client_order_id(value: object) -> str:
    if type(value) is not str or len(value) > 48 or _CLIENT_ORDER_ID.fullmatch(value) is None:
        raise ExecutionValidationError("client order ID is invalid")
    return value


def _identifier_digest(value: str) -> str:
    _identifier(value, field_name="identity")
    if "_" in value:
        candidate = value.rsplit("_", maxsplit=1)[-1]
        if _HASH.fullmatch(candidate) is not None:
            return candidate
    return sha256_hex(("execution-identifier-v1", value))


def _instant(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name="execution_timestamp")
    except DomainValidationError:
        raise ExecutionValidationError(f"{field_name} must be timezone-aware UTC") from None


def _optional_instant(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _instant(value, field_name=field_name)


def _decimal(value: object, *, field_name: str) -> Decimal:
    try:
        return require_finite_decimal(value, field_name="execution_value")
    except DomainValidationError:
        raise ExecutionValidationError(f"{field_name} must be a finite Decimal") from None


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    number = _decimal(value, field_name=field_name)
    if number <= 0:
        raise ExecutionValidationError(f"{field_name} must be positive")
    return number


def _nonnegative_decimal(value: object, *, field_name: str) -> Decimal:
    number = _decimal(value, field_name=field_name)
    if number < 0:
        raise ExecutionValidationError(f"{field_name} cannot be negative")
    return number


def quantities_match(left: Decimal, right: Decimal) -> bool:
    """Return whether signed quantities match the execution tolerance."""

    return abs(left - right) <= _QUANTITY_TOLERANCE
