"""Broker boundary, deterministic fake paper broker, and injected paper adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, Self, cast

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.execution.models import (
    AccountState,
    ExecutionValidationError,
    Fill,
    OrderIntent,
    OrderSide,
    OrderState,
    Position,
    PositionEffect,
)
from adaptive_trader.platform.hashing import sha256_hex

_MAX_SNAPSHOT_BYTES = 4_194_304
_BROKER_REPORTED_STATES = frozenset(
    {
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PENDING,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


class FakeBrokerScenario(StrEnum):
    """Deterministic broker and state failure scenarios."""

    FULL_FILL = "FULL_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECTION = "REJECTION"
    CANCELLATION = "CANCELLATION"
    EXPIRATION = "EXPIRATION"
    TIMEOUT_BEFORE_ACCEPTANCE = "TIMEOUT_BEFORE_ACCEPTANCE"
    TIMEOUT_AFTER_ACCEPTANCE = "TIMEOUT_AFTER_ACCEPTANCE"
    DELAYED_UPDATE = "DELAYED_UPDATE"
    DUPLICATE_EXECUTION_UPDATE = "DUPLICATE_EXECUTION_UPDATE"
    DISCONNECT = "DISCONNECT"
    STALE_ACCOUNT_STATE = "STALE_ACCOUNT_STATE"
    STALE_SECURITY_METADATA = "STALE_SECURITY_METADATA"
    SHORTABILITY_CHANGE = "SHORTABILITY_CHANGE"


class BrokerSubmissionUncertain(RuntimeError):
    """A broker call began but its durable outcome is not known locally."""

    def __init__(self, *, reason_code: str, acceptance_possible: bool) -> None:
        self.reason_code = reason_code
        self.acceptance_possible = acceptance_possible
        super().__init__("broker submission outcome is uncertain")


class DuplicateClientOrderId(RuntimeError):
    """The broker observed one client ID with different immutable intent content."""


@dataclass(frozen=True, slots=True)
class BrokerUpdate:
    """One typed broker response without raw or credential-bearing payloads."""

    client_order_id: str
    broker_order_id: str
    broker_event_id: str
    state: OrderState
    occurred_at: datetime
    cumulative_filled_quantity: Decimal
    average_fill_price: Decimal | None
    fills: tuple[Fill, ...]
    safe_error_code: str | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        if type(self.state) is not OrderState:
            raise ExecutionValidationError("broker update state is invalid")
        if self.state not in _BROKER_REPORTED_STATES:
            raise ExecutionValidationError("broker cannot report a local-only order state")
        if (
            type(self.cumulative_filled_quantity) is not Decimal
            or not self.cumulative_filled_quantity.is_finite()
            or self.cumulative_filled_quantity < 0
        ):
            raise ExecutionValidationError("broker cumulative fill is invalid")
        if self.average_fill_price is not None and (
            type(self.average_fill_price) is not Decimal
            or not self.average_fill_price.is_finite()
            or self.average_fill_price <= 0
        ):
            raise ExecutionValidationError("broker average fill price is invalid")
        if bool(self.cumulative_filled_quantity) != (self.average_fill_price is not None):
            raise ExecutionValidationError("broker fill quantity and price disagree")
        if type(self.fills) is not tuple or any(type(fill) is not Fill for fill in self.fills):
            raise ExecutionValidationError("broker fills must be immutable")
        if type(self.duplicate) is not bool:
            raise ExecutionValidationError("broker duplicate flag must be boolean")


class Broker(Protocol):
    """Narrow broker capability available to the execution worker."""

    @property
    def paper_only(self) -> bool:
        """Return whether this adapter is permanently limited to paper semantics."""

    def submit(self, intent: OrderIntent, *, submitted_at: datetime) -> BrokerUpdate:
        """Submit one already-durable intent."""

    def lookup(self, client_order_id: str, *, observed_at: datetime) -> BrokerUpdate | None:
        """Resolve an intent by its deterministic client ID."""

    def cancel(self, client_order_id: str, *, canceled_at: datetime) -> BrokerUpdate:
        """Cancel one known nonterminal broker order."""

    def account(self, *, observed_at: datetime) -> AccountState:
        """Return current account state."""

    def positions(self) -> tuple[Position, ...]:
        """Return current signed positions."""

    def open_client_order_ids(self) -> tuple[str, ...]:
        """Return deterministic client IDs of nonterminal orders."""


@dataclass(slots=True)
class _FakeOrder:
    intent_hash: str
    symbol: str
    side: OrderSide
    effect: PositionEffect
    requested_quantity: Decimal
    reference_price: Decimal
    broker_order_id: str
    broker_event_id: str
    state: OrderState
    cumulative_filled_quantity: Decimal
    average_fill_price: Decimal | None
    safe_error_code: str | None


class DeterministicFakePaperBroker:
    """Network-free fake broker with byte-stable restart snapshots."""

    INITIAL_ACCOUNT_ID = "fake-paper-account-v1"
    INITIAL_CASH = Decimal("100000.00")

    def __init__(
        self,
        *,
        initial_time: datetime,
        default_scenario: FakeBrokerScenario = FakeBrokerScenario.FULL_FILL,
    ) -> None:
        if initial_time.tzinfo is not UTC:
            raise ExecutionValidationError("fake broker initial time must be UTC")
        if type(default_scenario) is not FakeBrokerScenario:
            raise ExecutionValidationError("fake broker scenario is invalid")
        self._cash = self.INITIAL_CASH
        self._restricted_by_symbol: dict[str, Decimal] = {}
        self._positions: dict[str, Decimal] = {}
        self._marks: dict[str, Decimal] = {}
        self._orders: dict[str, _FakeOrder] = {}
        self._fills: dict[str, Fill] = {}
        self._scenarios: dict[str, FakeBrokerScenario] = {}
        self._default_scenario = default_scenario
        self._event_sequence = 0
        self._observed_at = initial_time

    @property
    def paper_only(self) -> bool:
        """The fake broker is permanently paper-like and network-free."""

        return True

    def set_scenario(self, client_order_id: str, scenario: FakeBrokerScenario) -> None:
        """Select one deterministic response before submitting an intent."""

        if type(scenario) is not FakeBrokerScenario:
            raise ExecutionValidationError("fake broker scenario is invalid")
        self._scenarios[client_order_id] = scenario

    def set_mark_prices(self, marks: tuple[tuple[str, Decimal], ...]) -> None:
        """Replace marks used by the exact account-equity identity."""

        if type(marks) is not tuple or tuple(symbol for symbol, _ in marks) != tuple(
            sorted({symbol for symbol, _ in marks})
        ):
            raise ExecutionValidationError("fake broker marks must be unique and ordered")
        if any(
            type(price) is not Decimal or not price.is_finite() or price <= 0 for _, price in marks
        ):
            raise ExecutionValidationError("fake broker marks must be positive finite Decimals")
        self._marks = dict(marks)

    def submit(self, intent: OrderIntent, *, submitted_at: datetime) -> BrokerUpdate:
        """Apply one deterministic scenario after validating the position effect."""

        if type(intent) is not OrderIntent:
            raise ExecutionValidationError("fake broker requires a validated intent")
        _utc(submitted_at)
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            if existing.intent_hash != intent.content_hash:
                raise DuplicateClientOrderId("client order ID was reused")
            return self._update_from_record(
                intent.client_order_id,
                existing,
                observed_at=submitted_at,
                fills=(),
                duplicate=True,
            )
        scenario = self._scenarios.get(intent.client_order_id, self._default_scenario)
        if scenario in {
            FakeBrokerScenario.DISCONNECT,
            FakeBrokerScenario.TIMEOUT_BEFORE_ACCEPTANCE,
        }:
            raise BrokerSubmissionUncertain(
                reason_code=(
                    "broker_disconnected"
                    if scenario is FakeBrokerScenario.DISCONNECT
                    else "submission_timeout"
                ),
                acceptance_possible=True,
            )
        if scenario in {
            FakeBrokerScenario.STALE_ACCOUNT_STATE,
            FakeBrokerScenario.STALE_SECURITY_METADATA,
            FakeBrokerScenario.SHORTABILITY_CHANGE,
        }:
            raise ExecutionValidationError("broker safety state changed before submission")
        self._validate_effect(intent)
        self._event_sequence += 1
        broker_order_id = _derived_id("fake_order", intent.client_order_id, self._event_sequence)
        broker_event_id = _derived_id("fake_event", intent.client_order_id, self._event_sequence)
        record = _FakeOrder(
            intent_hash=intent.content_hash,
            symbol=intent.symbol,
            side=intent.side,
            effect=intent.position_effect,
            requested_quantity=intent.quantity,
            reference_price=intent.reference_price,
            broker_order_id=broker_order_id,
            broker_event_id=broker_event_id,
            state=OrderState.ACCEPTED,
            cumulative_filled_quantity=Decimal(0),
            average_fill_price=None,
            safe_error_code=None,
        )
        self._orders[intent.client_order_id] = record
        self._marks[intent.symbol] = intent.reference_price
        self._observed_at = submitted_at
        if scenario is FakeBrokerScenario.TIMEOUT_AFTER_ACCEPTANCE:
            raise BrokerSubmissionUncertain(
                reason_code="submission_timeout",
                acceptance_possible=True,
            )
        if scenario is FakeBrokerScenario.REJECTION:
            record.state = OrderState.REJECTED
            record.safe_error_code = "fake_rejection"
        elif scenario is FakeBrokerScenario.CANCELLATION:
            record.state = OrderState.CANCELED
            record.safe_error_code = "fake_cancellation"
        elif scenario is FakeBrokerScenario.EXPIRATION:
            record.state = OrderState.EXPIRED
            record.safe_error_code = "fake_expiration"
        elif scenario is FakeBrokerScenario.DELAYED_UPDATE:
            record.state = OrderState.PENDING
        else:
            quantity = (
                intent.quantity
                if scenario
                in {
                    FakeBrokerScenario.FULL_FILL,
                    FakeBrokerScenario.DUPLICATE_EXECUTION_UPDATE,
                }
                else _partial_quantity(intent.quantity)
            )
            fill = self._fill(intent, quantity=quantity, occurred_at=submitted_at)
            record.cumulative_filled_quantity = quantity
            record.average_fill_price = intent.reference_price
            record.state = (
                OrderState.FILLED if quantity == intent.quantity else OrderState.PARTIALLY_FILLED
            )
            fills = (
                (fill, fill)
                if scenario is FakeBrokerScenario.DUPLICATE_EXECUTION_UPDATE
                else (fill,)
            )
            return self._update_from_record(
                intent.client_order_id,
                record,
                observed_at=submitted_at,
                fills=fills,
            )
        return self._update_from_record(
            intent.client_order_id,
            record,
            observed_at=submitted_at,
            fills=(),
        )

    def lookup(self, client_order_id: str, *, observed_at: datetime) -> BrokerUpdate | None:
        """Resolve one known deterministic ID without creating broker state."""

        _utc(observed_at)
        record = self._orders.get(client_order_id)
        if record is None:
            return None
        if (
            record.state is OrderState.PENDING
            and self._scenarios.get(client_order_id, self._default_scenario)
            is FakeBrokerScenario.DELAYED_UPDATE
        ):
            self._event_sequence += 1
            fill = Fill.create(
                client_order_id=client_order_id,
                broker_execution_id=_derived_id("fake_execution", client_order_id, 1),
                symbol=record.symbol,
                side=record.side,
                quantity=record.requested_quantity,
                price=record.reference_price,
                fee=Decimal(0),
                occurred_at=observed_at,
            )
            self._apply_fill(fill)
            self._fills.setdefault(fill.broker_execution_id, fill)
            record.cumulative_filled_quantity = record.requested_quantity
            record.average_fill_price = record.reference_price
            record.state = OrderState.FILLED
            record.broker_event_id = _derived_id(
                "fake_event",
                client_order_id,
                self._event_sequence,
            )
            self._observed_at = observed_at
        return self._update_from_record(
            client_order_id,
            record,
            observed_at=observed_at,
            fills=tuple(fill for fill in self.fills() if fill.client_order_id == client_order_id),
            duplicate=True,
        )

    def cancel(self, client_order_id: str, *, canceled_at: datetime) -> BrokerUpdate:
        """Deterministically cancel one nonterminal order."""

        _utc(canceled_at)
        record = self._orders.get(client_order_id)
        if record is None:
            raise ExecutionValidationError("cannot cancel an unknown fake order")
        if record.state.terminal:
            return self._update_from_record(
                client_order_id,
                record,
                observed_at=canceled_at,
                fills=(),
                duplicate=True,
            )
        self._event_sequence += 1
        record.state = OrderState.CANCELED
        record.safe_error_code = "cancel_confirmed"
        record.broker_event_id = _derived_id(
            "fake_event",
            client_order_id,
            self._event_sequence,
        )
        self._observed_at = canceled_at
        return self._update_from_record(
            client_order_id,
            record,
            observed_at=canceled_at,
            fills=(),
        )

    def account(self, *, observed_at: datetime) -> AccountState:
        """Return exact cash/equity/buying-power accounting."""

        _utc(observed_at)
        equity = self._cash + sum(
            quantity * self._marks.get(symbol, Decimal(0))
            for symbol, quantity in self._positions.items()
        )
        restricted = sum(self._restricted_by_symbol.values(), start=Decimal(0))
        buying_power = max(Decimal(0), self._cash - restricted)
        return AccountState(
            account_id=self.INITIAL_ACCOUNT_ID,
            cash=self._cash,
            equity=equity,
            buying_power=buying_power,
            restricted_short_proceeds=restricted,
            observed_at=observed_at,
        )

    def positions(self) -> tuple[Position, ...]:
        """Return nonzero signed positions in alphabetical order."""

        return tuple(
            Position(symbol=symbol, quantity=quantity)
            for symbol, quantity in sorted(self._positions.items())
            if quantity != 0
        )

    def open_client_order_ids(self) -> tuple[str, ...]:
        """Return nonterminal client IDs in deterministic order."""

        return tuple(
            sorted(
                client_id for client_id, order in self._orders.items() if not order.state.terminal
            )
        )

    def fills(self) -> tuple[Fill, ...]:
        """Return unique fake fills ordered by execution ID."""

        return tuple(self._fills[key] for key in sorted(self._fills))

    def snapshot(self) -> bytes:
        """Serialize all restart-relevant fake state using canonical JSON."""

        return canonical_json_bytes(
            {
                "cash": self._cash,
                "default_scenario": self._default_scenario,
                "event_sequence": self._event_sequence,
                "fills": tuple(_fill_payload(fill) for fill in self.fills()),
                "marks": tuple(sorted(self._marks.items())),
                "observed_at": self._observed_at,
                "orders": tuple(
                    _fake_order_payload(client_id, order)
                    for client_id, order in sorted(self._orders.items())
                ),
                "positions": tuple(sorted(self._positions.items())),
                "restricted": tuple(sorted(self._restricted_by_symbol.items())),
                "scenarios": tuple(
                    (client_id, scenario) for client_id, scenario in sorted(self._scenarios.items())
                ),
                "schema": "deterministic-fake-paper-broker-v1",
            }
        )

    @classmethod
    def from_snapshot(cls, payload: bytes) -> Self:
        """Restore a fake broker only after strict shape and hash validation."""

        if type(payload) is not bytes or not payload or len(payload) > _MAX_SNAPSHOT_BYTES:
            raise ExecutionValidationError("fake broker snapshot size is invalid")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ExecutionValidationError("fake broker snapshot is malformed") from None
        if (
            type(decoded) is not dict
            or decoded.get("schema") != "deterministic-fake-paper-broker-v1"
        ):
            raise ExecutionValidationError("fake broker snapshot schema is invalid")
        expected_keys = {
            "cash",
            "default_scenario",
            "event_sequence",
            "fills",
            "marks",
            "observed_at",
            "orders",
            "positions",
            "restricted",
            "scenarios",
            "schema",
        }
        if set(decoded) != expected_keys:
            raise ExecutionValidationError("fake broker snapshot fields are invalid")
        restored = cls(
            initial_time=_parse_instant(decoded["observed_at"]),
            default_scenario=_scenario(decoded["default_scenario"]),
        )
        restored._cash = _parse_decimal(decoded["cash"])
        restored._event_sequence = _parse_nonnegative_int(decoded["event_sequence"])
        restored._marks = _parse_decimal_pairs(decoded["marks"], positive=True)
        restored._positions = _parse_decimal_pairs(decoded["positions"], positive=False)
        restored._restricted_by_symbol = _parse_decimal_pairs(
            decoded["restricted"],
            positive=False,
        )
        if any(value < 0 for value in restored._restricted_by_symbol.values()):
            raise ExecutionValidationError("snapshot restricted proceeds cannot be negative")
        restored._scenarios = _parse_scenarios(decoded["scenarios"])
        restored._fills = _parse_fills(decoded["fills"])
        restored._orders = _parse_fake_orders(decoded["orders"])
        if restored.snapshot() != payload:
            raise ExecutionValidationError("fake broker snapshot is not canonical")
        return restored

    def _validate_effect(self, intent: OrderIntent) -> None:
        current = self._positions.get(intent.symbol, Decimal(0))
        effect = intent.position_effect
        valid = {
            PositionEffect.OPEN_LONG: current == 0,
            PositionEffect.INCREASE_LONG: current > 0,
            PositionEffect.REDUCE_LONG: current > 0 and intent.quantity < current,
            PositionEffect.CLOSE_LONG: current > 0 and intent.quantity == current,
            PositionEffect.OPEN_SHORT: current == 0,
            PositionEffect.INCREASE_SHORT: current < 0,
            PositionEffect.REDUCE_SHORT: current < 0 and intent.quantity < abs(current),
            PositionEffect.CLOSE_SHORT: current < 0 and intent.quantity == abs(current),
            PositionEffect.FORCED_FLAT_LONG: current > 0 and intent.quantity <= current,
            PositionEffect.FORCED_FLAT_SHORT: current < 0 and intent.quantity <= abs(current),
        }[effect]
        if not valid:
            raise ExecutionValidationError("intent effect does not match fake broker position")

    def _fill(self, intent: OrderIntent, *, quantity: Decimal, occurred_at: datetime) -> Fill:
        execution_id = _derived_id("fake_execution", intent.client_order_id, 1)
        fill = Fill.create(
            client_order_id=intent.client_order_id,
            broker_execution_id=execution_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=quantity,
            price=intent.reference_price,
            fee=Decimal(0),
            occurred_at=occurred_at,
        )
        self._apply_fill(fill)
        self._fills.setdefault(fill.broker_execution_id, fill)
        return fill

    def _apply_fill(self, fill: Fill) -> None:
        current = self._positions.get(fill.symbol, Decimal(0))
        notional = fill.quantity * fill.price
        if fill.side is OrderSide.SELL:
            if current > 0 and fill.quantity > current:
                raise ExecutionValidationError("one fake sell cannot cross through zero")
            self._cash += notional - fill.fee
            if current <= 0:
                self._restricted_by_symbol[fill.symbol] = (
                    self._restricted_by_symbol.get(fill.symbol, Decimal(0)) + notional
                )
            updated = current - fill.quantity
        else:
            if current < 0 and fill.quantity > abs(current):
                raise ExecutionValidationError("one fake buy cannot cross through zero")
            self._cash -= notional + fill.fee
            if current < 0:
                restricted = self._restricted_by_symbol.get(fill.symbol, Decimal(0))
                released = restricted * (fill.quantity / abs(current))
                remainder = max(Decimal(0), restricted - released)
                if remainder == 0:
                    self._restricted_by_symbol.pop(fill.symbol, None)
                else:
                    self._restricted_by_symbol[fill.symbol] = remainder
            updated = current + fill.quantity
        if updated == 0:
            self._positions.pop(fill.symbol, None)
            self._restricted_by_symbol.pop(fill.symbol, None)
        else:
            self._positions[fill.symbol] = updated

    def _update_from_record(
        self,
        client_order_id: str,
        record: _FakeOrder,
        *,
        observed_at: datetime,
        fills: tuple[Fill, ...],
        duplicate: bool = False,
    ) -> BrokerUpdate:
        return BrokerUpdate(
            client_order_id=client_order_id,
            broker_order_id=record.broker_order_id,
            broker_event_id=record.broker_event_id,
            state=record.state,
            occurred_at=observed_at,
            cumulative_filled_quantity=record.cumulative_filled_quantity,
            average_fill_price=record.average_fill_price,
            fills=fills,
            safe_error_code=record.safe_error_code,
            duplicate=duplicate,
        )


@dataclass(frozen=True, slots=True)
class PaperClientOrder:
    """Sanitized response contract implemented by an injected paper SDK facade."""

    client_order_id: str
    broker_order_id: str
    broker_event_id: str
    state: OrderState
    occurred_at: datetime
    cumulative_filled_quantity: Decimal
    average_fill_price: Decimal | None
    fills: tuple[Fill, ...]
    safe_error_code: str | None = None


class PaperClient(Protocol):
    """Injected paper-only SDK facade; this module imports no brokerage SDK."""

    def submit_market_order(self, intent: OrderIntent) -> PaperClientOrder:
        """Submit one market intent using a fixed paper-only client."""

    def lookup_by_client_order_id(self, client_order_id: str) -> PaperClientOrder | None:
        """Look up one order by deterministic client ID."""

    def cancel_by_client_order_id(self, client_order_id: str) -> PaperClientOrder:
        """Cancel one known paper order."""

    def account_state(self, observed_at: datetime) -> AccountState:
        """Return a sanitized account snapshot."""

    def signed_positions(self) -> tuple[Position, ...]:
        """Return sanitized signed positions."""

    def open_client_order_ids(self) -> tuple[str, ...]:
        """Return nonterminal deterministic IDs."""


class AlpacaPaperBrokerAdapter:
    """Minimal fixed paper adapter around an already-authenticated injected client.

    It contains no credential loading, endpoint selection, network library, or trading SDK
    import. Construction alone has no side effects. The execution service invokes it only after
    the independent paper authorization gates have approved an already-persisted intent.
    """

    def __init__(self, client: PaperClient) -> None:
        if client is None:
            raise ExecutionValidationError("paper adapter requires an injected client")
        self._client = client

    @property
    def paper_only(self) -> bool:
        """This adapter has no alternate endpoint or real-money mode."""

        return True

    def submit(self, intent: OrderIntent, *, submitted_at: datetime) -> BrokerUpdate:
        del submitted_at
        return _paper_update(self._client.submit_market_order(intent))

    def lookup(self, client_order_id: str, *, observed_at: datetime) -> BrokerUpdate | None:
        del observed_at
        result = self._client.lookup_by_client_order_id(client_order_id)
        return None if result is None else _paper_update(result)

    def cancel(self, client_order_id: str, *, canceled_at: datetime) -> BrokerUpdate:
        del canceled_at
        return _paper_update(self._client.cancel_by_client_order_id(client_order_id))

    def account(self, *, observed_at: datetime) -> AccountState:
        return self._client.account_state(observed_at)

    def positions(self) -> tuple[Position, ...]:
        return self._client.signed_positions()

    def open_client_order_ids(self) -> tuple[str, ...]:
        return self._client.open_client_order_ids()


def _paper_update(result: PaperClientOrder) -> BrokerUpdate:
    if type(result) is not PaperClientOrder:
        raise ExecutionValidationError("paper client returned an invalid response")
    return BrokerUpdate(
        client_order_id=result.client_order_id,
        broker_order_id=result.broker_order_id,
        broker_event_id=result.broker_event_id,
        state=result.state,
        occurred_at=result.occurred_at,
        cumulative_filled_quantity=result.cumulative_filled_quantity,
        average_fill_price=result.average_fill_price,
        fills=result.fills,
        safe_error_code=result.safe_error_code,
    )


def _partial_quantity(quantity: Decimal) -> Decimal:
    half = quantity / Decimal(2)
    return half if half > 0 else quantity


def _derived_id(prefix: str, client_order_id: str, sequence: int) -> str:
    return f"{prefix}_{sha256_hex((prefix, client_order_id, sequence))}"


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is not UTC:
        raise ExecutionValidationError("broker timestamp must be timezone-aware UTC")
    return value


def _fill_payload(fill: Fill) -> dict[str, object]:
    return {
        "broker_execution_id": fill.broker_execution_id,
        "client_order_id": fill.client_order_id,
        "content_hash": fill.content_hash,
        "fee": fill.fee,
        "fill_id": fill.fill_id,
        "occurred_at": fill.occurred_at,
        "price": fill.price,
        "quantity": fill.quantity,
        "side": fill.side,
        "symbol": fill.symbol,
    }


def _fake_order_payload(client_id: str, order: _FakeOrder) -> dict[str, object]:
    return {
        "average_fill_price": order.average_fill_price,
        "broker_event_id": order.broker_event_id,
        "broker_order_id": order.broker_order_id,
        "client_order_id": client_id,
        "cumulative_filled_quantity": order.cumulative_filled_quantity,
        "effect": order.effect,
        "intent_hash": order.intent_hash,
        "reference_price": order.reference_price,
        "requested_quantity": order.requested_quantity,
        "safe_error_code": order.safe_error_code,
        "side": order.side,
        "state": order.state,
        "symbol": order.symbol,
    }


def _parse_instant(value: object) -> datetime:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExecutionValidationError("snapshot timestamp is invalid") from None
    return _utc(parsed)


def _parse_decimal(value: object) -> Decimal:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot Decimal is invalid")
    try:
        parsed = Decimal(value)
    except Exception:
        raise ExecutionValidationError("snapshot Decimal is invalid") from None
    if not parsed.is_finite():
        raise ExecutionValidationError("snapshot Decimal is invalid")
    return parsed


def _parse_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ExecutionValidationError("snapshot sequence is invalid")
    return value


def _parse_decimal_pairs(value: object, *, positive: bool) -> dict[str, Decimal]:
    if type(value) is not list:
        raise ExecutionValidationError("snapshot decimal pairs are invalid")
    result: dict[str, Decimal] = {}
    for item in value:
        if type(item) is not list or len(item) != 2 or type(item[0]) is not str:
            raise ExecutionValidationError("snapshot decimal pair is invalid")
        number = _parse_decimal(item[1])
        if positive and number <= 0:
            raise ExecutionValidationError("snapshot mark must be positive")
        if item[0] in result:
            raise ExecutionValidationError("snapshot contains a duplicate symbol")
        result[item[0]] = number
    if tuple(result) != tuple(sorted(result)):
        raise ExecutionValidationError("snapshot symbol pairs are not ordered")
    return result


def _scenario(value: object) -> FakeBrokerScenario:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot scenario is invalid")
    try:
        return FakeBrokerScenario(value)
    except ValueError:
        raise ExecutionValidationError("snapshot scenario is invalid") from None


def _parse_scenarios(value: object) -> dict[str, FakeBrokerScenario]:
    if type(value) is not list:
        raise ExecutionValidationError("snapshot scenarios are invalid")
    result: dict[str, FakeBrokerScenario] = {}
    for item in value:
        if type(item) is not list or len(item) != 2 or type(item[0]) is not str:
            raise ExecutionValidationError("snapshot scenario entry is invalid")
        if item[0] in result:
            raise ExecutionValidationError("snapshot contains duplicate scenario")
        result[item[0]] = _scenario(item[1])
    if tuple(result) != tuple(sorted(result)):
        raise ExecutionValidationError("snapshot scenarios are not ordered")
    return result


def _parse_fills(value: object) -> dict[str, Fill]:
    if type(value) is not list:
        raise ExecutionValidationError("snapshot fills are invalid")
    result: dict[str, Fill] = {}
    expected_keys = {
        "broker_execution_id",
        "client_order_id",
        "content_hash",
        "fee",
        "fill_id",
        "occurred_at",
        "price",
        "quantity",
        "side",
        "symbol",
    }
    for raw in value:
        if type(raw) is not dict or set(raw) != expected_keys:
            raise ExecutionValidationError("snapshot fill fields are invalid")
        fill = Fill(
            fill_id=cast(str, raw["fill_id"]),
            client_order_id=cast(str, raw["client_order_id"]),
            broker_execution_id=cast(str, raw["broker_execution_id"]),
            symbol=cast(str, raw["symbol"]),
            side=OrderSide(cast(str, raw["side"])),
            quantity=_parse_decimal(raw["quantity"]),
            price=_parse_decimal(raw["price"]),
            fee=_parse_decimal(raw["fee"]),
            occurred_at=_parse_instant(raw["occurred_at"]),
            content_hash=cast(str, raw["content_hash"]),
        )
        if fill.broker_execution_id in result:
            raise ExecutionValidationError("snapshot contains duplicate execution ID")
        result[fill.broker_execution_id] = fill
    return result


def _parse_fake_orders(value: object) -> dict[str, _FakeOrder]:
    if type(value) is not list:
        raise ExecutionValidationError("snapshot orders are invalid")
    result: dict[str, _FakeOrder] = {}
    keys = {
        "average_fill_price",
        "broker_event_id",
        "broker_order_id",
        "client_order_id",
        "cumulative_filled_quantity",
        "effect",
        "intent_hash",
        "reference_price",
        "requested_quantity",
        "safe_error_code",
        "side",
        "state",
        "symbol",
    }
    for raw in value:
        if type(raw) is not dict or set(raw) != keys:
            raise ExecutionValidationError("snapshot order fields are invalid")
        client_id = cast(str, raw["client_order_id"])
        if client_id in result:
            raise ExecutionValidationError("snapshot contains duplicate client order ID")
        average = raw["average_fill_price"]
        result[client_id] = _FakeOrder(
            intent_hash=cast(str, raw["intent_hash"]),
            symbol=cast(str, raw["symbol"]),
            side=OrderSide(cast(str, raw["side"])),
            effect=PositionEffect(cast(str, raw["effect"])),
            requested_quantity=_parse_decimal(raw["requested_quantity"]),
            reference_price=_parse_decimal(raw["reference_price"]),
            broker_order_id=cast(str, raw["broker_order_id"]),
            broker_event_id=cast(str, raw["broker_event_id"]),
            state=OrderState(cast(str, raw["state"])),
            cumulative_filled_quantity=_parse_decimal(raw["cumulative_filled_quantity"]),
            average_fill_price=None if average is None else _parse_decimal(average),
            safe_error_code=cast(str | None, raw["safe_error_code"]),
        )
    if tuple(result) != tuple(sorted(result)):
        raise ExecutionValidationError("snapshot orders are not ordered")
    return result
