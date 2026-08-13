"""First-class reconciliation between durable intent and paper-broker truth."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from adaptive_trader.broker import Broker
from adaptive_trader.clock import Clock, SystemClock
from adaptive_trader.execution import TERMINAL_STATES, OrderStateMachine, state_for_broker_status
from adaptive_trader.live_models import (
    DiscrepancySeverity,
    LocalOrderState,
    ReconciliationDiscrepancy,
    ReconciliationResult,
    TradeUpdate,
)
from adaptive_trader.persistence import AuditRepository


class Reconciler:
    """Compare local projections with the authoritative simulated paper account."""

    def __init__(
        self,
        *,
        repository: AuditRepository,
        broker: Broker,
        universe: Sequence[str],
        run_id: str | None = None,
        clock: Clock | None = None,
        quantity_tolerance: Decimal = Decimal("0.000000001"),
        cash_tolerance: Decimal = Decimal("0.01"),
        equity_tolerance: Decimal = Decimal("0.01"),
        recent_order_lookback: timedelta = timedelta(days=7),
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.universe = {str(symbol).upper() for symbol in universe}
        self.run_id = run_id
        self.clock = clock or SystemClock()
        self.quantity_tolerance = quantity_tolerance
        self.cash_tolerance = cash_tolerance
        self.equity_tolerance = equity_tolerance
        self.recent_order_lookback = recent_order_lookback

    def run(self) -> ReconciliationResult:
        started = self.clock.now()
        discrepancies: list[ReconciliationDiscrepancy] = []
        account = self.broker.get_account()
        broker_positions = list(self.broker.get_positions())
        broker_orders = list(
            self.broker.get_orders(
                include_closed=True,
                after=started - self.recent_order_lookback,
            )
        )
        local_orders = self.repository.list_orders(open_only=False)
        local_by_client = {str(row["client_order_id"]): row for row in local_orders}

        duplicate_ids = [
            client_id
            for client_id, count in Counter(
                order.client_order_id for order in broker_orders
            ).items()
            if count > 1
        ]
        for client_id in sorted(duplicate_ids):
            discrepancies.append(
                ReconciliationDiscrepancy(
                    kind="duplicate_client_order_id",
                    severity=DiscrepancySeverity.CRITICAL,
                    message="Paper broker returned more than one order for a client order ID",
                    client_order_id=client_id,
                )
            )

        broker_by_client = {order.client_order_id: order for order in broker_orders}
        for client_id, broker_order in broker_by_client.items():
            local = local_by_client.get(client_id)
            if local is None:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="unknown_broker_order",
                        severity=DiscrepancySeverity.BLOCKING,
                        message="Paper broker has an order that is absent from local audit state",
                        symbol=broker_order.symbol,
                        client_order_id=client_id,
                        details={"broker_order_id": broker_order.broker_order_id},
                    )
                )
                continue
            local_state = LocalOrderState(str(local["state"]))
            broker_state = state_for_broker_status(broker_order.status)
            local_filled = Decimal(str(local.get("filled_quantity") or "0"))
            projection_fill_delta = broker_order.filled_quantity - local_filled
            fill_mismatch = abs(projection_fill_delta) > self.quantity_tolerance
            if fill_mismatch:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="filled_quantity_mismatch",
                        severity=DiscrepancySeverity.BLOCKING,
                        message="Local and paper-broker filled quantities differ",
                        symbol=broker_order.symbol,
                        client_order_id=client_id,
                        details={
                            "local": str(local_filled),
                            "broker": str(broker_order.filled_quantity),
                        },
                    )
                )
            should_advance = False
            if local_state != broker_state:
                if local_state in TERMINAL_STATES:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            kind="terminal_order_state_mismatch",
                            severity=DiscrepancySeverity.BLOCKING,
                            message="A terminal local order conflicts with paper-broker state",
                            symbol=broker_order.symbol,
                            client_order_id=client_id,
                            details={
                                "local": local_state.value,
                                "broker": broker_state.value,
                            },
                        )
                    )
                elif OrderStateMachine.can_transition(local_state, broker_state):
                    should_advance = True
                else:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            kind="invalid_reconciliation_transition",
                            severity=DiscrepancySeverity.BLOCKING,
                            message="Paper-broker state cannot safely advance local order state",
                            symbol=broker_order.symbol,
                            client_order_id=client_id,
                            details={
                                "local": local_state.value,
                                "broker": broker_state.value,
                            },
                        )
                    )
            elif fill_mismatch and OrderStateMachine.can_transition(local_state, broker_state):
                # Same-state partial-fill progress still advances the mutable
                # order projection and receives a unique reconciliation event.
                should_advance = True
            if should_advance:
                self.repository.transition_order(
                    client_order_id=client_id,
                    to_state=broker_state,
                    event_type="reconciliation",
                    event_key=(
                        f"reconcile:{broker_order.broker_order_id}:"
                        f"{broker_order.status}:{broker_order.filled_quantity}:"
                        f"{broker_order.updated_at.isoformat()}"
                    ),
                    allowed_from={local_state},
                    created_at=broker_order.updated_at,
                    broker_order_id=broker_order.broker_order_id,
                    raw_status=broker_order.status,
                    filled_quantity=broker_order.filled_quantity,
                    average_fill_price=broker_order.average_fill_price,
                    payload={"authoritative": "paper_broker"},
                )
            durable_filled, durable_fill_notional = self.repository.fill_ledger_totals(client_id)
            ledger_fill_delta = broker_order.filled_quantity - durable_filled
            repair_price: Decimal | None = None
            if (
                ledger_fill_delta > self.quantity_tolerance
                and broker_order.average_fill_price is not None
            ):
                missing_notional = (
                    broker_order.filled_quantity * broker_order.average_fill_price
                    - durable_fill_notional
                )
                implied_price = missing_notional / ledger_fill_delta
                if implied_price > 0:
                    repair_price = implied_price
            if abs(ledger_fill_delta) > self.quantity_tolerance:
                repairable = ledger_fill_delta > 0 and repair_price is not None
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind=(
                            "missing_fill_event"
                            if ledger_fill_delta > 0
                            else "fill_ledger_exceeds_broker"
                        ),
                        severity=(
                            DiscrepancySeverity.RECOVERABLE
                            if repairable
                            else DiscrepancySeverity.BLOCKING
                        ),
                        message=(
                            "Immutable fill ledger trails paper-broker cumulative fills"
                            if ledger_fill_delta > 0
                            else "Immutable fill ledger exceeds paper-broker cumulative fills"
                        ),
                        symbol=broker_order.symbol,
                        client_order_id=client_id,
                        details={
                            "durable_fill_quantity": str(durable_filled),
                            "broker_fill_quantity": str(broker_order.filled_quantity),
                            "repair_quantity": str(max(Decimal("0"), ledger_fill_delta)),
                            "durable_fill_notional": str(durable_fill_notional),
                            "broker_cumulative_average_price": (
                                None
                                if broker_order.average_fill_price is None
                                else str(broker_order.average_fill_price)
                            ),
                            "repair_price": (None if repair_price is None else str(repair_price)),
                        },
                    )
                )
            if ledger_fill_delta > self.quantity_tolerance and repair_price is not None:
                self.repository.record_fill(
                    TradeUpdate(
                        event=broker_order.status,
                        order=broker_order,
                        timestamp=broker_order.updated_at,
                        execution_id=(
                            f"reconciliation:{broker_order.broker_order_id}:"
                            f"{broker_order.filled_quantity}"
                        ),
                        fill_quantity=ledger_fill_delta,
                        fill_price=repair_price,
                    ),
                    source="reconciliation",
                )

        for client_id, local in local_by_client.items():
            local_state = LocalOrderState(str(local["state"]))
            if local_state in TERMINAL_STATES or client_id in broker_by_client:
                continue
            severity = (
                DiscrepancySeverity.RECOVERABLE
                if local_state is LocalOrderState.LOCALLY_RESERVED
                else DiscrepancySeverity.BLOCKING
            )
            discrepancies.append(
                ReconciliationDiscrepancy(
                    kind="local_order_missing_at_broker",
                    severity=severity,
                    message="A nonterminal local order was not found at the paper broker",
                    symbol=str(local["symbol"]),
                    client_order_id=client_id,
                    details={"local_state": local_state.value},
                )
            )

        expected_positions = self.repository.latest_positions()
        broker_by_symbol = {position.symbol: position for position in broker_positions}
        for position in broker_positions:
            if position.quantity < 0:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="negative_position",
                        severity=DiscrepancySeverity.CRITICAL,
                        message="Paper broker reports a negative position",
                        symbol=position.symbol,
                        details={"quantity": str(position.quantity)},
                    )
                )
            if self.universe and position.symbol not in self.universe:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="unexpected_symbol",
                        severity=DiscrepancySeverity.CRITICAL,
                        message="Paper broker holds a symbol outside the configured universe",
                        symbol=position.symbol,
                    )
                )
        if expected_positions:
            for symbol in sorted(set(expected_positions) | set(broker_by_symbol)):
                expected = Decimal(str(expected_positions.get(symbol, {}).get("quantity", "0")))
                actual = broker_by_symbol.get(symbol)
                actual_quantity = Decimal("0") if actual is None else actual.quantity
                if abs(expected - actual_quantity) > self.quantity_tolerance:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            kind="position_quantity_mismatch",
                            severity=DiscrepancySeverity.BLOCKING,
                            message="Local expected quantity differs from paper-broker position",
                            symbol=symbol,
                            details={"local": str(expected), "broker": str(actual_quantity)},
                        )
                    )

        prior_account = self.repository.latest_account_state()
        if prior_account is not None:
            expected_cash = Decimal(str(prior_account["cash"]))
            if abs(expected_cash - account.cash) > self.cash_tolerance:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="cash_mismatch",
                        severity=DiscrepancySeverity.BLOCKING,
                        message="Local expected cash differs from the paper account",
                        details={"local": str(expected_cash), "broker": str(account.cash)},
                    )
                )
            expected_equity = Decimal(str(prior_account["equity"]))
            if abs(expected_equity - account.equity) > self.equity_tolerance:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        kind="equity_changed_since_snapshot",
                        severity=DiscrepancySeverity.INFORMATIONAL,
                        message="Paper-account equity changed since the last local snapshot",
                        details={"local": str(expected_equity), "broker": str(account.equity)},
                    )
                )

        completed = self.clock.now()
        result = ReconciliationResult(
            reconciliation_id=uuid.uuid4().hex,
            started_at=started,
            completed_at=completed,
            discrepancies=tuple(discrepancies),
        )
        self.repository.record_reconciliation(result, run_id=self.run_id)
        # Snapshot only after recording the comparison, preserving the evidence
        # rather than silently overwriting the expected state first.
        self.repository.record_account_state(account, broker_positions, run_id=self.run_id)
        return result


def reconcile(
    repository: AuditRepository,
    broker: Broker,
    *,
    run_id: str | None = None,
    universe: Sequence[str] = (),
    clock: Clock | None = None,
) -> ReconciliationResult:
    """Convenience entry point used by the CLI and one-shot service."""

    return Reconciler(
        repository=repository,
        broker=broker,
        universe=universe,
        run_id=run_id,
        clock=clock,
    ).run()
