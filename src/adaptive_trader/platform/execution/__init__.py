"""Signed execution, deterministic broker simulation, and reconciliation."""

from typing import TYPE_CHECKING, Any

from adaptive_trader.platform.execution.authorization import (
    PAPER_ACKNOWLEDGEMENT,
    PaperGateContext,
    SubmissionAuthoritySnapshot,
    SubmissionAuthorization,
    SubmissionLedgerSnapshot,
    SubmissionSafetySnapshot,
    authorize_intent,
    authorize_paper_intent,
)
from adaptive_trader.platform.execution.models import (
    AccountState,
    BrokerOrder,
    DiscrepancyCode,
    ExecutionPlan,
    ExecutionValidationError,
    Fill,
    Incident,
    IntentPhase,
    OrderEvent,
    OrderIntent,
    OrderSide,
    OrderState,
    Position,
    PositionEffect,
    ReconciliationDiscrepancy,
    ReconciliationReceipt,
    ReconciliationSeverity,
    ReconciliationStatus,
    quantities_match,
)
from adaptive_trader.platform.execution.planner import (
    LONG_QUANTUM,
    MAXIMUM_INTENTS_PER_PLAN,
    MAXIMUM_SINGLE_INTENT_EQUITY_FRACTION,
    MINIMUM_ORDER_NOTIONAL,
    SHORT_QUANTUM,
    ExecutionPlanningRequest,
    ExecutionPlanningResult,
    plan_signed_orders,
    signed_target_quantity,
)
from adaptive_trader.platform.execution.reconciliation import (
    CASH_TOLERANCE,
    EQUITY_TOLERANCE,
    QUANTITY_TOLERANCE,
    ReconciliationOutcome,
    ReconciliationRequest,
    reconcile,
    reconcile_and_persist,
    reconciliation_input_hash,
    reconstruct_signed_positions,
)
from adaptive_trader.platform.execution.repository import (
    ExecutionLedgerState,
    ExecutionRepository,
    MemoryExecutionRepository,
)
from adaptive_trader.platform.execution.state_machine import (
    transition_is_permitted,
    validate_order_transition,
)

if TYPE_CHECKING:
    from adaptive_trader.platform.execution.alpaca_paper import create_alpaca_paper_broker
    from adaptive_trader.platform.execution.broker import (
        AlpacaPaperBrokerAdapter,
        Broker,
        BrokerSubmissionUncertain,
        BrokerUpdate,
        DeterministicFakePaperBroker,
        DuplicateClientOrderId,
        FakeBrokerScenario,
        PaperClient,
        PaperClientOrder,
    )
    from adaptive_trader.platform.execution.flatten import (
        ForcedFlattenRequest,
        ForcedFlattenResult,
        ForcedFlattenService,
    )
    from adaptive_trader.platform.execution.service import ExecutionService, SubmissionResult

__all__ = [
    "CASH_TOLERANCE",
    "EQUITY_TOLERANCE",
    "LONG_QUANTUM",
    "MAXIMUM_INTENTS_PER_PLAN",
    "MAXIMUM_SINGLE_INTENT_EQUITY_FRACTION",
    "MINIMUM_ORDER_NOTIONAL",
    "PAPER_ACKNOWLEDGEMENT",
    "QUANTITY_TOLERANCE",
    "SHORT_QUANTUM",
    "AccountState",
    "AlpacaPaperBrokerAdapter",
    "Broker",
    "BrokerOrder",
    "BrokerSubmissionUncertain",
    "BrokerUpdate",
    "DeterministicFakePaperBroker",
    "DiscrepancyCode",
    "DuplicateClientOrderId",
    "ExecutionLedgerState",
    "ExecutionPlan",
    "ExecutionPlanningRequest",
    "ExecutionPlanningResult",
    "ExecutionRepository",
    "ExecutionService",
    "ExecutionValidationError",
    "FakeBrokerScenario",
    "Fill",
    "ForcedFlattenRequest",
    "ForcedFlattenResult",
    "ForcedFlattenService",
    "Incident",
    "IntentPhase",
    "MemoryExecutionRepository",
    "OrderEvent",
    "OrderIntent",
    "OrderSide",
    "OrderState",
    "PaperClient",
    "PaperClientOrder",
    "PaperGateContext",
    "Position",
    "PositionEffect",
    "ReconciliationDiscrepancy",
    "ReconciliationOutcome",
    "ReconciliationReceipt",
    "ReconciliationRequest",
    "ReconciliationSeverity",
    "ReconciliationStatus",
    "SubmissionAuthoritySnapshot",
    "SubmissionAuthorization",
    "SubmissionLedgerSnapshot",
    "SubmissionResult",
    "SubmissionSafetySnapshot",
    "authorize_intent",
    "authorize_paper_intent",
    "create_alpaca_paper_broker",
    "plan_signed_orders",
    "quantities_match",
    "reconcile",
    "reconcile_and_persist",
    "reconciliation_input_hash",
    "reconstruct_signed_positions",
    "signed_target_quantity",
    "transition_is_permitted",
    "validate_order_transition",
]


def __getattr__(name: str) -> Any:
    """Load only explicitly requested broker capabilities; pure planning stays inert."""
    if name in ("create_alpaca_paper_broker",):
        from adaptive_trader.platform.execution import alpaca_paper

        value = getattr(alpaca_paper, name)
        globals()[name] = value
        return value
    if name in (
        "AlpacaPaperBrokerAdapter",
        "Broker",
        "BrokerSubmissionUncertain",
        "BrokerUpdate",
        "DeterministicFakePaperBroker",
        "DuplicateClientOrderId",
        "FakeBrokerScenario",
        "PaperClient",
        "PaperClientOrder",
    ):
        from adaptive_trader.platform.execution import broker

        value = getattr(broker, name)
        globals()[name] = value
        return value
    if name in ("ForcedFlattenRequest", "ForcedFlattenResult", "ForcedFlattenService"):
        from adaptive_trader.platform.execution import flatten

        value = getattr(flatten, name)
        globals()[name] = value
        return value
    if name in ("ExecutionService", "SubmissionResult"):
        from adaptive_trader.platform.execution import service

        value = getattr(service, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
