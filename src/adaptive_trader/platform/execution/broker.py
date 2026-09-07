"""Broker boundary, deterministic fake paper broker, and injected paper adapter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from adaptive_trader.platform.risk.models import SecurityMetadataSnapshot

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
_SNAPSHOT_SCHEMA = "deterministic-fake-paper-broker-snapshot-v2"
_STATE_SCHEMA = "deterministic-fake-paper-broker-state-v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$", flags=re.ASCII)
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
    created_sequence: int
    last_event_sequence: int
    created_at: datetime
    updated_at: datetime


class DeterministicFakePaperBroker:
    """Network-free fake broker with byte-stable restart snapshots."""

    INITIAL_ACCOUNT_ID = "fake-paper-account-v1"
    INITIAL_CASH = Decimal("100000.00")

    def __init__(
        self,
        *,
        initial_time: datetime,
        initial_cash: Decimal = INITIAL_CASH,
        default_scenario: FakeBrokerScenario = FakeBrokerScenario.FULL_FILL,
    ) -> None:
        if initial_time.tzinfo is not UTC:
            raise ExecutionValidationError("fake broker initial time must be UTC")
        if type(default_scenario) is not FakeBrokerScenario:
            raise ExecutionValidationError("fake broker scenario is invalid")
        if type(initial_cash) is not Decimal or not initial_cash.is_finite() or initial_cash <= 0:
            raise ExecutionValidationError("fake broker initial cash must be positive and finite")
        self._cash = initial_cash
        self._initial_cash = initial_cash
        self._initial_time = initial_time
        self._restricted_by_symbol: dict[str, Decimal] = {}
        self._positions: dict[str, Decimal] = {}
        self._marks: dict[str, Decimal] = {}
        self._orders: dict[str, _FakeOrder] = {}
        self._fills: dict[str, Fill] = {}
        self._fill_sequences: dict[str, int] = {}
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
            created_sequence=self._event_sequence,
            last_event_sequence=self._event_sequence,
            created_at=submitted_at,
            updated_at=submitted_at,
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
            fill = self._fill(
                intent,
                quantity=quantity,
                occurred_at=submitted_at,
                event_sequence=self._event_sequence,
            )
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
            self._fill_sequences.setdefault(fill.broker_execution_id, self._event_sequence)
            record.cumulative_filled_quantity = record.requested_quantity
            record.average_fill_price = record.reference_price
            record.state = OrderState.FILLED
            record.broker_event_id = _derived_id(
                "fake_event",
                client_order_id,
                self._event_sequence,
            )
            record.last_event_sequence = self._event_sequence
            record.updated_at = observed_at
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
        record.last_event_sequence = self._event_sequence
        record.updated_at = canceled_at
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
        """Serialize a digest-bound, replay-verifiable restart image."""

        state = {
            "cash": self._cash,
            "default_scenario": self._default_scenario,
            "event_sequence": self._event_sequence,
            "fills": tuple(
                _fill_payload(fill, event_sequence=self._fill_sequences[fill.broker_execution_id])
                for fill in self.fills()
            ),
            "initial_cash": self._initial_cash,
            "initial_time": self._initial_time,
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
            "schema": _STATE_SCHEMA,
        }
        return canonical_json_bytes(
            {
                "payload": state,
                "payload_sha256": sha256_hex(state),
                "schema": _SNAPSHOT_SCHEMA,
            }
        )

    @classmethod
    def from_snapshot(cls, payload: bytes) -> Self:
        """Restore only after digest, semantic, and accounting replay validation."""

        if type(payload) is not bytes or not payload or len(payload) > _MAX_SNAPSHOT_BYTES:
            raise ExecutionValidationError("fake broker snapshot size is invalid")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ExecutionValidationError("fake broker snapshot is malformed") from None
        try:
            if canonical_json_bytes(decoded) != payload:
                raise ExecutionValidationError("fake broker snapshot is not canonical")
            if (
                type(decoded) is not dict
                or set(decoded) != {"payload", "payload_sha256", "schema"}
                or decoded.get("schema") != _SNAPSHOT_SCHEMA
            ):
                raise ExecutionValidationError("fake broker snapshot envelope is invalid")
            state = decoded["payload"]
            payload_hash = decoded["payload_sha256"]
            if (
                type(state) is not dict
                or type(payload_hash) is not str
                or _SHA256.fullmatch(payload_hash) is None
                or sha256_hex(state) != payload_hash
            ):
                raise ExecutionValidationError("fake broker snapshot digest is invalid")
            expected_keys = {
                "cash",
                "default_scenario",
                "event_sequence",
                "fills",
                "initial_cash",
                "initial_time",
                "marks",
                "observed_at",
                "orders",
                "positions",
                "restricted",
                "scenarios",
                "schema",
            }
            if set(state) != expected_keys or state.get("schema") != _STATE_SCHEMA:
                raise ExecutionValidationError("fake broker snapshot state is invalid")
            initial_time = _parse_instant(state["initial_time"])
            observed_at = _parse_instant(state["observed_at"])
            initial_cash = _parse_decimal(state["initial_cash"])
            restored = cls(
                initial_time=initial_time,
                initial_cash=initial_cash,
                default_scenario=_scenario(state["default_scenario"]),
            )
            restored._cash = _parse_decimal(state["cash"])
            restored._event_sequence = _parse_nonnegative_int(state["event_sequence"])
            restored._observed_at = observed_at
            restored._marks = _parse_decimal_pairs(
                state["marks"],
                strictly_positive=True,
            )
            restored._positions = _parse_decimal_pairs(
                state["positions"],
                nonzero=True,
            )
            restored._restricted_by_symbol = _parse_decimal_pairs(
                state["restricted"],
                strictly_positive=True,
            )
            restored._scenarios = _parse_scenarios(state["scenarios"])
            restored._fills, restored._fill_sequences = _parse_fills(state["fills"])
            restored._orders = _parse_fake_orders(state["orders"])
            _validate_restored_fake_broker(restored)
            if restored.snapshot() != payload:
                raise ExecutionValidationError("fake broker snapshot did not round-trip")
            return restored
        except ExecutionValidationError:
            raise
        except (DecimalException, KeyError, TypeError, ValueError):
            raise ExecutionValidationError("fake broker snapshot semantics are invalid") from None

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

    def _fill(
        self,
        intent: OrderIntent,
        *,
        quantity: Decimal,
        occurred_at: datetime,
        event_sequence: int,
    ) -> Fill:
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
        self._fill_sequences.setdefault(fill.broker_execution_id, event_sequence)
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

    def security_metadata(
        self, symbols: tuple[str, ...], *, observed_at: datetime
    ) -> tuple[SecurityMetadataSnapshot, ...]:
        """Return exact sanitized asset eligibility for the requested active symbols."""


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

    def security_metadata(
        self, symbols: tuple[str, ...], *, observed_at: datetime
    ) -> tuple[SecurityMetadataSnapshot, ...]:
        return self._client.security_metadata(symbols, observed_at=observed_at)


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


def _fill_payload(fill: Fill, *, event_sequence: int) -> dict[str, object]:
    return {
        "broker_execution_id": fill.broker_execution_id,
        "client_order_id": fill.client_order_id,
        "content_hash": fill.content_hash,
        "event_sequence": event_sequence,
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
        "created_at": order.created_at,
        "created_sequence": order.created_sequence,
        "effect": order.effect,
        "intent_hash": order.intent_hash,
        "last_event_sequence": order.last_event_sequence,
        "reference_price": order.reference_price,
        "requested_quantity": order.requested_quantity,
        "safe_error_code": order.safe_error_code,
        "side": order.side,
        "state": order.state,
        "symbol": order.symbol,
        "updated_at": order.updated_at,
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


def _parse_positive_int(value: object) -> int:
    parsed = _parse_nonnegative_int(value)
    if parsed == 0:
        raise ExecutionValidationError("snapshot sequence must be positive")
    return parsed


def _snapshot_text(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise ExecutionValidationError(f"snapshot {field_name} is invalid")
    return value


def _optional_snapshot_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _snapshot_text(value, field_name=field_name)


def _snapshot_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ExecutionValidationError(f"snapshot {field_name} is invalid")
    return value


def _snapshot_symbol(value: object) -> str:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot symbol is invalid")
    Position(symbol=value, quantity=Decimal(0))
    return value


def _snapshot_client_id(value: object) -> str:
    if type(value) is not str or _CLIENT_ORDER_ID.fullmatch(value) is None:
        raise ExecutionValidationError("snapshot client order ID is invalid")
    return value


def _order_side(value: object) -> OrderSide:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot order side is invalid")
    try:
        return OrderSide(value)
    except ValueError:
        raise ExecutionValidationError("snapshot order side is invalid") from None


def _position_effect(value: object) -> PositionEffect:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot position effect is invalid")
    try:
        return PositionEffect(value)
    except ValueError:
        raise ExecutionValidationError("snapshot position effect is invalid") from None


def _order_state(value: object) -> OrderState:
    if type(value) is not str:
        raise ExecutionValidationError("snapshot order state is invalid")
    try:
        state = OrderState(value)
    except ValueError:
        raise ExecutionValidationError("snapshot order state is invalid") from None
    if state not in _BROKER_REPORTED_STATES or state is OrderState.SUBMITTED:
        raise ExecutionValidationError("snapshot order state is not produced by the fake broker")
    return state


def _parse_decimal_pairs(
    value: object,
    *,
    strictly_positive: bool = False,
    nonzero: bool = False,
) -> dict[str, Decimal]:
    if type(value) is not list:
        raise ExecutionValidationError("snapshot decimal pairs are invalid")
    result: dict[str, Decimal] = {}
    for item in value:
        if type(item) is not list or len(item) != 2:
            raise ExecutionValidationError("snapshot decimal pair is invalid")
        symbol = _snapshot_symbol(item[0])
        number = _parse_decimal(item[1])
        if strictly_positive and number <= 0:
            raise ExecutionValidationError("snapshot amount must be positive")
        if nonzero and number == 0:
            raise ExecutionValidationError("snapshot position cannot be zero")
        if symbol in result:
            raise ExecutionValidationError("snapshot contains a duplicate symbol")
        result[symbol] = number
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


def _parse_fills(value: object) -> tuple[dict[str, Fill], dict[str, int]]:
    if type(value) is not list:
        raise ExecutionValidationError("snapshot fills are invalid")
    result: dict[str, Fill] = {}
    sequences: dict[str, int] = {}
    expected_keys = {
        "broker_execution_id",
        "client_order_id",
        "content_hash",
        "event_sequence",
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
            fill_id=_snapshot_text(raw["fill_id"], field_name="fill ID"),
            client_order_id=_snapshot_client_id(raw["client_order_id"]),
            broker_execution_id=_snapshot_text(
                raw["broker_execution_id"],
                field_name="broker execution ID",
            ),
            symbol=_snapshot_symbol(raw["symbol"]),
            side=_order_side(raw["side"]),
            quantity=_parse_decimal(raw["quantity"]),
            price=_parse_decimal(raw["price"]),
            fee=_parse_decimal(raw["fee"]),
            occurred_at=_parse_instant(raw["occurred_at"]),
            content_hash=_snapshot_hash(raw["content_hash"], field_name="fill content hash"),
        )
        if fill.broker_execution_id in result:
            raise ExecutionValidationError("snapshot contains duplicate execution ID")
        result[fill.broker_execution_id] = fill
        sequences[fill.broker_execution_id] = _parse_positive_int(raw["event_sequence"])
    if tuple(result) != tuple(sorted(result)):
        raise ExecutionValidationError("snapshot fills are not ordered")
    return result, sequences


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
        "created_at",
        "created_sequence",
        "effect",
        "intent_hash",
        "last_event_sequence",
        "reference_price",
        "requested_quantity",
        "safe_error_code",
        "side",
        "state",
        "symbol",
        "updated_at",
    }
    for raw in value:
        if type(raw) is not dict or set(raw) != keys:
            raise ExecutionValidationError("snapshot order fields are invalid")
        client_id = _snapshot_client_id(raw["client_order_id"])
        if client_id in result:
            raise ExecutionValidationError("snapshot contains duplicate client order ID")
        average = raw["average_fill_price"]
        result[client_id] = _FakeOrder(
            intent_hash=_snapshot_hash(raw["intent_hash"], field_name="intent hash"),
            symbol=_snapshot_symbol(raw["symbol"]),
            side=_order_side(raw["side"]),
            effect=_position_effect(raw["effect"]),
            requested_quantity=_parse_decimal(raw["requested_quantity"]),
            reference_price=_parse_decimal(raw["reference_price"]),
            broker_order_id=_snapshot_text(raw["broker_order_id"], field_name="broker order ID"),
            broker_event_id=_snapshot_text(raw["broker_event_id"], field_name="broker event ID"),
            state=_order_state(raw["state"]),
            cumulative_filled_quantity=_parse_decimal(raw["cumulative_filled_quantity"]),
            average_fill_price=None if average is None else _parse_decimal(average),
            safe_error_code=_optional_snapshot_text(
                raw["safe_error_code"],
                field_name="safe error code",
            ),
            created_sequence=_parse_positive_int(raw["created_sequence"]),
            last_event_sequence=_parse_positive_int(raw["last_event_sequence"]),
            created_at=_parse_instant(raw["created_at"]),
            updated_at=_parse_instant(raw["updated_at"]),
        )
    if tuple(result) != tuple(sorted(result)):
        raise ExecutionValidationError("snapshot orders are not ordered")
    return result


def _validate_restored_fake_broker(broker: DeterministicFakePaperBroker) -> None:
    """Verify every redundant projection against ordered immutable fill evidence."""

    if broker._observed_at < broker._initial_time:
        raise ExecutionValidationError("snapshot observation precedes broker initialization")
    event_owners: dict[int, str] = {}
    event_times: dict[int, datetime] = {}
    created_orders: dict[int, tuple[str, _FakeOrder]] = {}
    fills_by_sequence: dict[int, Fill] = {}
    fills_by_client: dict[str, list[Fill]] = {}
    broker_order_ids: set[str] = set()
    broker_event_ids: set[str] = set()

    def claim_event(sequence: int, client_id: str, occurred_at: datetime) -> None:
        if not 1 <= sequence <= broker._event_sequence:
            raise ExecutionValidationError("snapshot event sequence is out of range")
        owner = event_owners.setdefault(sequence, client_id)
        event_time = event_times.setdefault(sequence, occurred_at)
        if owner != client_id or event_time != occurred_at:
            raise ExecutionValidationError("snapshot event sequence has conflicting evidence")

    for client_id, order in broker._orders.items():
        if (
            order.created_sequence > order.last_event_sequence
            or order.created_at > order.updated_at
            or order.created_at < broker._initial_time
            or order.updated_at > broker._observed_at
        ):
            raise ExecutionValidationError("snapshot order chronology is invalid")
        if order.created_sequence in created_orders:
            raise ExecutionValidationError("snapshot reuses an order creation sequence")
        created_orders[order.created_sequence] = (client_id, order)
        claim_event(order.created_sequence, client_id, order.created_at)
        if order.last_event_sequence != order.created_sequence:
            claim_event(order.last_event_sequence, client_id, order.updated_at)
        elif order.updated_at != order.created_at:
            raise ExecutionValidationError("snapshot order timestamp changed without an event")
        if (
            order.requested_quantity <= 0
            or not order.requested_quantity.is_finite()
            or order.reference_price <= 0
            or not order.reference_price.is_finite()
            or order.cumulative_filled_quantity < 0
            or not order.cumulative_filled_quantity.is_finite()
            or order.cumulative_filled_quantity > order.requested_quantity
        ):
            raise ExecutionValidationError("snapshot order quantities are invalid")
        expected_side = _side_for_effect(order.effect)
        if order.side is not expected_side:
            raise ExecutionValidationError("snapshot order side and position effect disagree")
        if order.broker_order_id != _derived_id(
            "fake_order",
            client_id,
            order.created_sequence,
        ) or order.broker_event_id != _derived_id(
            "fake_event",
            client_id,
            order.last_event_sequence,
        ):
            raise ExecutionValidationError("snapshot broker identity is invalid")
        if order.broker_order_id in broker_order_ids or order.broker_event_id in broker_event_ids:
            raise ExecutionValidationError("snapshot broker identity is not unique")
        broker_order_ids.add(order.broker_order_id)
        broker_event_ids.add(order.broker_event_id)

    for execution_id, fill in broker._fills.items():
        sequence = broker._fill_sequences.get(execution_id)
        if sequence is None or sequence in fills_by_sequence:
            raise ExecutionValidationError("snapshot fill sequence is invalid")
        owning_order = broker._orders.get(fill.client_order_id)
        if owning_order is None:
            raise ExecutionValidationError("snapshot fill has no owning order")
        if (
            fill.symbol != owning_order.symbol
            or fill.side is not owning_order.side
            or fill.price != owning_order.reference_price
            or fill.fee != 0
            or execution_id != _derived_id("fake_execution", fill.client_order_id, 1)
            or sequence not in {owning_order.created_sequence, owning_order.last_event_sequence}
        ):
            raise ExecutionValidationError("snapshot fill does not match its order")
        event_time = (
            owning_order.created_at
            if sequence == owning_order.created_sequence
            else owning_order.updated_at
        )
        if fill.occurred_at != event_time or fill.occurred_at > broker._observed_at:
            raise ExecutionValidationError("snapshot fill chronology is invalid")
        claim_event(sequence, fill.client_order_id, fill.occurred_at)
        fills_by_sequence[sequence] = fill
        fills_by_client.setdefault(fill.client_order_id, []).append(fill)

    for client_id, order in broker._orders.items():
        fills = fills_by_client.get(client_id, [])
        if len(fills) > 1:
            raise ExecutionValidationError("snapshot fake order has multiple fill identities")
        filled = sum((fill.quantity for fill in fills), start=Decimal(0))
        if filled != order.cumulative_filled_quantity:
            raise ExecutionValidationError("snapshot order fill total is inconsistent")
        weighted_average = (
            None
            if filled == 0
            else sum((fill.quantity * fill.price for fill in fills), start=Decimal(0)) / filled
        )
        if order.average_fill_price != weighted_average:
            raise ExecutionValidationError("snapshot order average fill price is inconsistent")
        _validate_fake_order_lifecycle(order, filled=filled)

    expected_sequences = set(range(1, broker._event_sequence + 1))
    if set(event_owners) != expected_sequences:
        raise ExecutionValidationError("snapshot event sequence is not contiguous")
    ordered_event_times = tuple(
        event_times[index] for index in range(1, broker._event_sequence + 1)
    )
    if ordered_event_times != tuple(sorted(ordered_event_times)):
        raise ExecutionValidationError("snapshot event timestamps are not monotonic")
    expected_observed_at = (
        broker._initial_time if not ordered_event_times else ordered_event_times[-1]
    )
    if broker._observed_at != expected_observed_at:
        raise ExecutionValidationError("snapshot observation time does not match its event history")

    replay = DeterministicFakePaperBroker(
        initial_time=broker._initial_time,
        initial_cash=broker._initial_cash,
    )
    for sequence in range(1, broker._event_sequence + 1):
        created = created_orders.get(sequence)
        if created is not None:
            _, created_order = created
            _validate_fake_effect(
                created_order,
                current=replay._positions.get(created_order.symbol, Decimal(0)),
            )
        sequence_fill = fills_by_sequence.get(sequence)
        if sequence_fill is not None:
            replay._apply_fill(sequence_fill)
    if (
        broker._cash != replay._cash
        or broker._positions != replay._positions
        or broker._restricted_by_symbol != replay._restricted_by_symbol
    ):
        raise ExecutionValidationError("snapshot accounting does not replay from fill evidence")
    required_marks = set(broker._positions) | {order.symbol for order in broker._orders.values()}
    if not required_marks.issubset(broker._marks):
        raise ExecutionValidationError("snapshot marks do not cover broker state")


def _side_for_effect(effect: PositionEffect) -> OrderSide:
    if effect in {
        PositionEffect.OPEN_LONG,
        PositionEffect.INCREASE_LONG,
        PositionEffect.REDUCE_SHORT,
        PositionEffect.CLOSE_SHORT,
        PositionEffect.FORCED_FLAT_SHORT,
    }:
        return OrderSide.BUY
    return OrderSide.SELL


def _validate_fake_effect(order: _FakeOrder, *, current: Decimal) -> None:
    valid = {
        PositionEffect.OPEN_LONG: current == 0,
        PositionEffect.INCREASE_LONG: current > 0,
        PositionEffect.REDUCE_LONG: current > 0 and order.requested_quantity < current,
        PositionEffect.CLOSE_LONG: current > 0 and order.requested_quantity == current,
        PositionEffect.OPEN_SHORT: current == 0,
        PositionEffect.INCREASE_SHORT: current < 0,
        PositionEffect.REDUCE_SHORT: current < 0 and order.requested_quantity < abs(current),
        PositionEffect.CLOSE_SHORT: current < 0 and order.requested_quantity == abs(current),
        PositionEffect.FORCED_FLAT_LONG: current > 0 and order.requested_quantity <= current,
        PositionEffect.FORCED_FLAT_SHORT: current < 0 and order.requested_quantity <= abs(current),
    }[order.effect]
    if not valid:
        raise ExecutionValidationError("snapshot order effect does not match replayed position")


def _validate_fake_order_lifecycle(order: _FakeOrder, *, filled: Decimal) -> None:
    if order.state is OrderState.FILLED:
        valid_quantity = filled == order.requested_quantity
    elif order.state is OrderState.PARTIALLY_FILLED:
        valid_quantity = Decimal(0) < filled < order.requested_quantity
    elif order.state is OrderState.CANCELED:
        valid_quantity = Decimal(0) <= filled < order.requested_quantity
    else:
        valid_quantity = filled == 0
    if not valid_quantity:
        raise ExecutionValidationError("snapshot order lifecycle disagrees with fills")
    if order.last_event_sequence != order.created_sequence and order.state not in {
        OrderState.FILLED,
        OrderState.CANCELED,
    }:
        raise ExecutionValidationError("snapshot order has an unsupported later event")
    expected_error: str | None = None
    if order.state is OrderState.REJECTED:
        expected_error = "fake_rejection"
    elif order.state is OrderState.EXPIRED:
        expected_error = "fake_expiration"
    elif order.state is OrderState.CANCELED:
        expected_error = (
            "fake_cancellation"
            if order.last_event_sequence == order.created_sequence
            else "cancel_confirmed"
        )
    if order.safe_error_code != expected_error:
        raise ExecutionValidationError("snapshot order error state is inconsistent")
