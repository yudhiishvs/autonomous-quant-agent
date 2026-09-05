"""Validated fail-closed order-state transitions."""

from __future__ import annotations

from adaptive_trader.platform.execution.models import (
    ExecutionValidationError,
    OrderState,
)

_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PLANNED: frozenset({OrderState.INTENT_COMMITTED}),
    OrderState.INTENT_COMMITTED: frozenset({OrderState.SUBMISSION_STARTED}),
    OrderState.SUBMISSION_STARTED: frozenset(
        {
            OrderState.SUBMITTED,
            OrderState.ACCEPTED,
            OrderState.PENDING,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.SUBMISSION_UNKNOWN,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PENDING,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PENDING,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.PENDING: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.CANCEL_REQUESTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.SUBMISSION_UNKNOWN: frozenset(
        {
            OrderState.SUBMITTED,
            OrderState.ACCEPTED,
            OrderState.PENDING,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.RECONCILIATION_REQUIRED,
        }
    ),
    OrderState.RECONCILIATION_REQUIRED: frozenset(
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
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


def validate_order_transition(previous: OrderState, current: OrderState) -> None:
    """Reject unknown, backward, and post-terminal transitions."""

    if type(previous) is not OrderState or type(current) is not OrderState:
        raise ExecutionValidationError("order transition requires closed states")
    if current not in _TRANSITIONS[previous]:
        raise ExecutionValidationError(
            f"order transition {previous.value} to {current.value} is not permitted"
        )


def transition_is_permitted(previous: OrderState, current: OrderState) -> bool:
    """Return whether the closed transition graph contains one edge."""

    if type(previous) is not OrderState or type(current) is not OrderState:
        return False
    return current in _TRANSITIONS[previous]
