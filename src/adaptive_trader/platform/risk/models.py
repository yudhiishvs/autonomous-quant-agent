"""Immutable inputs and deterministic receipts for the signed-risk boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
from enum import StrEnum

from adaptive_trader.platform.domain import (
    DeterministicId,
    require_finite_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.latches import RiskLatchEvent, RiskLatchKind, RiskLatchState
from adaptive_trader.platform.risk.policy import AppliedRiskControl, ExposureSnapshot
from adaptive_trader.platform.risk.statistics import RiskStatistics
from adaptive_trader.platform.signals.models import DecisionContext, SignalEnvelope

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_RISK_DECISION_ID = re.compile(r"^risk_[0-9a-f]{64}$", flags=re.ASCII)
_SLOT_ID = re.compile(r"^slot_[0-9a-f]{64}$", flags=re.ASCII)
_SIGNAL_ID = re.compile(r"^signal_[0-9a-f]{64}$", flags=re.ASCII)
_CORRELATION_ID = re.compile(r"^correlation_[0-9a-f]{64}$", flags=re.ASCII)
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_ARITHMETIC = Context(prec=50)


class SignedRiskValidationError(DomainValidationError):
    """Raised when signed-risk inputs or receipts violate their closed contract."""


class RiskExecutionScope(StrEnum):
    """Maximum execution authority granted by a risk decision."""

    FULL = "FULL"
    NONE = "NONE"
    RISK_REDUCING_ONLY = "RISK_REDUCING_ONLY"


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Point-in-time account inputs required by signed risk."""

    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        equity = _decimal(self.equity, field_name="account_equity")
        _decimal(self.cash, field_name="account_cash")
        buying_power = _decimal(self.buying_power, field_name="buying_power")
        if equity < 0:
            raise SignedRiskValidationError("account equity cannot be negative")
        if buying_power < 0:
            raise SignedRiskValidationError("buying power cannot be negative")
        _instant(self.observed_at, field_name="account_observed_at")


@dataclass(frozen=True, slots=True)
class SignedPosition:
    """Current signed quantity for one exact active symbol."""

    symbol: str
    quantity: Decimal

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        _decimal(self.quantity, field_name="position_quantity")


@dataclass(frozen=True, slots=True)
class PlanningPrice:
    """Current validated positive price and its source timestamp."""

    symbol: str
    price: Decimal
    observed_at: datetime
    validated: bool

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        price = _decimal(self.price, field_name="planning_price")
        if price <= 0:
            raise SignedRiskValidationError("planning price must be positive")
        _instant(self.observed_at, field_name="price_observed_at")
        if type(self.validated) is not bool:
            raise SignedRiskValidationError("planning-price validation flag must be boolean")


@dataclass(frozen=True, slots=True)
class SecurityMetadataSnapshot:
    """Tradability, short, listing, and broker capability state for one symbol."""

    symbol: str
    asset_active: bool
    tradable: bool
    shortable: bool
    easy_to_borrow: bool
    primary_listing_eligible: bool
    broker_capability_known: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        flags = (
            self.asset_active,
            self.tradable,
            self.shortable,
            self.easy_to_borrow,
            self.primary_listing_eligible,
            self.broker_capability_known,
        )
        if any(type(flag) is not bool for flag in flags):
            raise SignedRiskValidationError("security metadata flags must be boolean")
        _instant(self.observed_at, field_name="security_observed_at")


@dataclass(frozen=True, slots=True)
class OpenOrderSnapshot:
    """Reserved signed exposure and deterministic conflict/ambiguity classification."""

    reserved_signed_notional: tuple[tuple[str, Decimal], ...]
    conflicting_symbols: tuple[str, ...]
    ambiguous_order_exists: bool
    observed_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        symbols = _named_decimals(self.reserved_signed_notional, field_name="reserved exposure")
        _symbol_tuple(self.conflicting_symbols, allow_empty=True)
        if not set(self.conflicting_symbols).issubset(symbols):
            raise SignedRiskValidationError("open-order conflicts must reference reserved symbols")
        if type(self.ambiguous_order_exists) is not bool:
            raise SignedRiskValidationError("open-order ambiguity flag must be boolean")
        observed_at = _instant(self.observed_at, field_name="open_orders_observed_at")
        _hash(self.content_hash, field_name="open_order_hash")
        if self.content_hash != sha256_hex(
            {
                "ambiguous_order_exists": self.ambiguous_order_exists,
                "conflicting_symbols": self.conflicting_symbols,
                "observed_at": observed_at,
                "reserved_signed_notional": self.reserved_signed_notional,
                "schema": "open-order-risk-snapshot-v1",
            }
        ):
            raise SignedRiskValidationError("open-order snapshot hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        reserved_signed_notional: Mapping[str, Decimal],
        conflicting_symbols: tuple[str, ...],
        ambiguous_order_exists: bool,
        observed_at: datetime,
    ) -> OpenOrderSnapshot:
        """Freeze and hash a complete reserved-exposure snapshot."""

        if not isinstance(reserved_signed_notional, Mapping):
            raise SignedRiskValidationError("reserved exposure must be a mapping")
        reserved = tuple(
            sorted(
                (
                    (_symbol(symbol), _decimal(value, field_name="reserved_notional"))
                    for symbol, value in reserved_signed_notional.items()
                ),
                key=lambda item: item[0],
            )
        )
        instant = _instant(observed_at, field_name="open_orders_observed_at")
        content_hash = sha256_hex(
            {
                "ambiguous_order_exists": ambiguous_order_exists,
                "conflicting_symbols": conflicting_symbols,
                "observed_at": instant,
                "reserved_signed_notional": reserved,
                "schema": "open-order-risk-snapshot-v1",
            }
        )
        return cls(
            reserved_signed_notional=reserved,
            conflicting_symbols=conflicting_symbols,
            ambiguous_order_exists=ambiguous_order_exists,
            observed_at=instant,
            content_hash=content_hash,
        )


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    """Current reconciliation classification required before any exposure change."""

    reconciled: bool
    ambiguous_order_exists: bool
    observed_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        if type(self.reconciled) is not bool or type(self.ambiguous_order_exists) is not bool:
            raise SignedRiskValidationError("reconciliation flags must be boolean")
        instant = _instant(self.observed_at, field_name="reconciliation_observed_at")
        _hash(self.content_hash, field_name="reconciliation_hash")
        if self.content_hash != sha256_hex(
            {
                "ambiguous_order_exists": self.ambiguous_order_exists,
                "observed_at": instant,
                "reconciled": self.reconciled,
                "schema": "risk-reconciliation-snapshot-v1",
            }
        ):
            raise SignedRiskValidationError("reconciliation snapshot hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        reconciled: bool,
        ambiguous_order_exists: bool,
        observed_at: datetime,
    ) -> ReconciliationSnapshot:
        """Create a hash-bound reconciliation snapshot."""

        instant = _instant(observed_at, field_name="reconciliation_observed_at")
        content_hash = sha256_hex(
            {
                "ambiguous_order_exists": ambiguous_order_exists,
                "observed_at": instant,
                "reconciled": reconciled,
                "schema": "risk-reconciliation-snapshot-v1",
            }
        )
        return cls(
            reconciled=reconciled,
            ambiguous_order_exists=ambiguous_order_exists,
            observed_at=instant,
            content_hash=content_hash,
        )


@dataclass(frozen=True, slots=True)
class MarketIntegritySnapshot:
    """Decision-time basket, gap, correction, and session eligibility state."""

    active_basket_complete: bool
    unresolved_gap: bool
    correction_uncertainty: bool
    supported_session: bool
    content_hash: str

    def __post_init__(self) -> None:
        flags = (
            self.active_basket_complete,
            self.unresolved_gap,
            self.correction_uncertainty,
            self.supported_session,
        )
        if any(type(flag) is not bool for flag in flags):
            raise SignedRiskValidationError("market-integrity flags must be boolean")
        if self.content_hash != sha256_hex(
            {
                "active_basket_complete": self.active_basket_complete,
                "correction_uncertainty": self.correction_uncertainty,
                "schema": "market-integrity-risk-snapshot-v1",
                "supported_session": self.supported_session,
                "unresolved_gap": self.unresolved_gap,
            }
        ):
            raise SignedRiskValidationError("market-integrity snapshot hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        active_basket_complete: bool,
        unresolved_gap: bool,
        correction_uncertainty: bool,
        supported_session: bool,
    ) -> MarketIntegritySnapshot:
        """Create a hash-bound market-integrity snapshot."""

        content_hash = sha256_hex(
            {
                "active_basket_complete": active_basket_complete,
                "correction_uncertainty": correction_uncertainty,
                "schema": "market-integrity-risk-snapshot-v1",
                "supported_session": supported_session,
                "unresolved_gap": unresolved_gap,
            }
        )
        return cls(
            active_basket_complete=active_basket_complete,
            unresolved_gap=unresolved_gap,
            correction_uncertainty=correction_uncertainty,
            supported_session=supported_session,
            content_hash=content_hash,
        )


@dataclass(frozen=True, slots=True)
class RiskEvaluationRequest:
    """Complete immutable input set for one non-bypassable signed-risk evaluation."""

    signal: SignalEnvelope
    decision_context: DecisionContext
    positions: tuple[SignedPosition, ...]
    open_orders: OpenOrderSnapshot
    account: AccountSnapshot
    prices: tuple[PlanningPrice, ...]
    security_metadata: tuple[SecurityMetadataSnapshot, ...]
    reconciliation: ReconciliationSnapshot
    market_integrity: MarketIntegritySnapshot
    session_start_equity: Decimal
    deployment_high_water_equity: Decimal
    statistics: RiskStatistics
    latch_state: RiskLatchState
    operator_halt: bool
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.signal) is not SignalEnvelope
            or type(self.decision_context) is not DecisionContext
        ):
            raise SignedRiskValidationError("risk evaluation requires signal and decision context")
        active_symbols = self.signal.active_symbols
        _complete_symbol_records(self.positions, active_symbols, SignedPosition, "positions")
        _complete_symbol_records(self.prices, active_symbols, PlanningPrice, "prices")
        _complete_symbol_records(
            self.security_metadata,
            active_symbols,
            SecurityMetadataSnapshot,
            "security metadata",
        )
        if (
            type(self.open_orders) is not OpenOrderSnapshot
            or tuple(symbol for symbol, _ in self.open_orders.reserved_signed_notional)
            != active_symbols
        ):
            raise SignedRiskValidationError("open orders must contain exactly active symbols")
        if type(self.account) is not AccountSnapshot:
            raise SignedRiskValidationError("risk evaluation account snapshot is invalid")
        if type(self.reconciliation) is not ReconciliationSnapshot:
            raise SignedRiskValidationError("risk evaluation reconciliation snapshot is invalid")
        if type(self.market_integrity) is not MarketIntegritySnapshot:
            raise SignedRiskValidationError("risk evaluation market integrity is invalid")
        _positive_decimal(self.session_start_equity, field_name="session_start_equity")
        _positive_decimal(
            self.deployment_high_water_equity,
            field_name="deployment_high_water_equity",
        )
        if type(self.statistics) is not RiskStatistics:
            raise SignedRiskValidationError("risk evaluation statistics are invalid")
        if type(self.latch_state) is not RiskLatchState:
            raise SignedRiskValidationError("risk evaluation latch state is invalid")
        if type(self.operator_halt) is not bool:
            raise SignedRiskValidationError("operator halt flag must be boolean")
        _instant(self.evaluated_at, field_name="evaluated_at")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Immutable risk receipt binding all inputs, controls, latches, and signed targets."""

    risk_decision_id: str
    slot_id: str
    signal_id: str
    signal_hash: str
    experiment_hash: str
    policy_id: str
    policy_version: int
    policy_hash: str
    correlation_id: str
    decided_at: datetime
    input_hash: str
    statistics_hash: str
    original_proposal: tuple[tuple[str, str, Decimal | None, Decimal | None], ...]
    proposed_targets: tuple[tuple[str, Decimal], ...]
    final_targets: tuple[tuple[str, Decimal], ...]
    before_exposure: ExposureSnapshot
    after_exposure: ExposureSnapshot
    ordered_controls: tuple[AppliedRiskControl, ...]
    block_reasons: tuple[str, ...]
    flatten_reasons: tuple[str, ...]
    source_timestamps: tuple[tuple[str, datetime], ...]
    latch_state_hash: str
    active_latches: tuple[RiskLatchKind, ...]
    required_latch_events: tuple[RiskLatchEvent, ...]
    execution_scope: RiskExecutionScope
    content_hash: str

    def __post_init__(self) -> None:
        _validate_risk_decision(self)

    @property
    def cash_weight(self) -> Decimal:
        """Return the unallocated gross-exposure fraction for persistence/read models."""

        with localcontext(_ARITHMETIC):
            return Decimal(1) - self.after_exposure.gross

    @classmethod
    def create(
        cls,
        *,
        slot_id: str,
        signal_id: str,
        signal_hash: str,
        experiment_hash: str,
        policy_id: str,
        policy_version: int,
        policy_hash: str,
        correlation_id: str,
        decided_at: datetime,
        input_hash: str,
        statistics_hash: str,
        original_proposal: tuple[tuple[str, str, Decimal | None, Decimal | None], ...],
        proposed_targets: tuple[tuple[str, Decimal], ...],
        final_targets: tuple[tuple[str, Decimal], ...],
        before_exposure: ExposureSnapshot,
        after_exposure: ExposureSnapshot,
        ordered_controls: tuple[AppliedRiskControl, ...],
        block_reasons: tuple[str, ...],
        flatten_reasons: tuple[str, ...],
        source_timestamps: tuple[tuple[str, datetime], ...],
        latch_state_hash: str,
        active_latches: tuple[RiskLatchKind, ...],
        required_latch_events: tuple[RiskLatchEvent, ...],
        execution_scope: RiskExecutionScope,
    ) -> RiskDecision:
        """Create a deterministic receipt and derive its content-addressed ID."""

        digest = _risk_decision_digest(
            slot_id=slot_id,
            signal_id=signal_id,
            signal_hash=signal_hash,
            experiment_hash=experiment_hash,
            policy_id=policy_id,
            policy_version=policy_version,
            policy_hash=policy_hash,
            correlation_id=correlation_id,
            decided_at=decided_at,
            input_hash=input_hash,
            statistics_hash=statistics_hash,
            original_proposal=original_proposal,
            proposed_targets=proposed_targets,
            final_targets=final_targets,
            before_exposure=before_exposure,
            after_exposure=after_exposure,
            ordered_controls=ordered_controls,
            block_reasons=block_reasons,
            flatten_reasons=flatten_reasons,
            source_timestamps=source_timestamps,
            latch_state_hash=latch_state_hash,
            active_latches=active_latches,
            required_latch_events=required_latch_events,
            execution_scope=execution_scope,
        )
        return cls(
            risk_decision_id=DeterministicId(prefix="risk", digest=digest).value,
            slot_id=slot_id,
            signal_id=signal_id,
            signal_hash=signal_hash,
            experiment_hash=experiment_hash,
            policy_id=policy_id,
            policy_version=policy_version,
            policy_hash=policy_hash,
            correlation_id=correlation_id,
            decided_at=decided_at,
            input_hash=input_hash,
            statistics_hash=statistics_hash,
            original_proposal=original_proposal,
            proposed_targets=proposed_targets,
            final_targets=final_targets,
            before_exposure=before_exposure,
            after_exposure=after_exposure,
            ordered_controls=ordered_controls,
            block_reasons=block_reasons,
            flatten_reasons=flatten_reasons,
            source_timestamps=source_timestamps,
            latch_state_hash=latch_state_hash,
            active_latches=active_latches,
            required_latch_events=required_latch_events,
            execution_scope=execution_scope,
            content_hash=digest,
        )


def risk_input_hash(request: RiskEvaluationRequest, *, policy_hash: str) -> str:
    """Hash every required decision input before policy evaluation."""

    if type(request) is not RiskEvaluationRequest:
        raise SignedRiskValidationError("risk input hash requires a validated request")
    _hash(policy_hash, field_name="policy_hash")
    return sha256_hex(
        {
            "account": {
                "buying_power": request.account.buying_power,
                "cash": request.account.cash,
                "equity": request.account.equity,
                "observed_at": request.account.observed_at,
            },
            "decision_context": {
                "data_contract_hash": request.decision_context.data_contract_hash,
                "policy_hash": request.decision_context.policy_hash,
                "slot_hash": request.decision_context.slot.content_hash,
            },
            "deployment_high_water_equity": request.deployment_high_water_equity,
            "evaluated_at": request.evaluated_at,
            "latch_state_hash": request.latch_state.content_hash,
            "market_integrity_hash": request.market_integrity.content_hash,
            "open_orders_hash": request.open_orders.content_hash,
            "operator_halt": request.operator_halt,
            "policy_hash": policy_hash,
            "positions": tuple(
                (position.symbol, position.quantity) for position in request.positions
            ),
            "prices": tuple(
                (price.symbol, price.price, price.observed_at, price.validated)
                for price in request.prices
            ),
            "reconciliation_hash": request.reconciliation.content_hash,
            "schema": "signed-risk-input-v1",
            "security_metadata": tuple(
                (
                    security.symbol,
                    security.asset_active,
                    security.tradable,
                    security.shortable,
                    security.easy_to_borrow,
                    security.primary_listing_eligible,
                    security.broker_capability_known,
                    security.observed_at,
                )
                for security in request.security_metadata
            ),
            "session_start_equity": request.session_start_equity,
            "signal_hash": request.signal.content_hash,
            "statistics_hash": request.statistics.output_hash,
        }
    )


def _validate_risk_decision(decision: RiskDecision) -> None:
    if (
        type(decision.risk_decision_id) is not str
        or _RISK_DECISION_ID.fullmatch(decision.risk_decision_id) is None
    ):
        raise SignedRiskValidationError("risk decision ID is invalid")
    if type(decision.slot_id) is not str or _SLOT_ID.fullmatch(decision.slot_id) is None:
        raise SignedRiskValidationError("risk decision slot ID is invalid")
    if type(decision.signal_id) is not str or _SIGNAL_ID.fullmatch(decision.signal_id) is None:
        raise SignedRiskValidationError("risk decision signal ID is invalid")
    if (
        type(decision.correlation_id) is not str
        or _CORRELATION_ID.fullmatch(decision.correlation_id) is None
    ):
        raise SignedRiskValidationError("risk decision correlation ID is invalid")
    for value, field_name in (
        (decision.signal_hash, "signal_hash"),
        (decision.experiment_hash, "experiment_hash"),
        (decision.policy_hash, "policy_hash"),
        (decision.input_hash, "input_hash"),
        (decision.statistics_hash, "statistics_hash"),
        (decision.latch_state_hash, "latch_state_hash"),
        (decision.content_hash, "content_hash"),
    ):
        _hash(value, field_name=field_name)
    if type(decision.policy_id) is not str or not decision.policy_id:
        raise SignedRiskValidationError("risk policy ID is invalid")
    if type(decision.policy_version) is not int or decision.policy_version < 1:
        raise SignedRiskValidationError("risk policy version is invalid")
    _instant(decision.decided_at, field_name="decided_at")
    _proposal_tuple(decision.original_proposal)
    proposed_symbols = _named_decimals(decision.proposed_targets, field_name="proposed targets")
    final_symbols = _named_decimals(decision.final_targets, field_name="final targets")
    if proposed_symbols != final_symbols:
        raise SignedRiskValidationError("risk target symbols do not match")
    if (
        type(decision.before_exposure) is not ExposureSnapshot
        or type(decision.after_exposure) is not ExposureSnapshot
    ):
        raise SignedRiskValidationError("risk decision exposures are invalid")
    if type(decision.ordered_controls) is not tuple or any(
        type(control) is not AppliedRiskControl for control in decision.ordered_controls
    ):
        raise SignedRiskValidationError("risk controls must be immutable")
    if tuple(control.ordinal for control in decision.ordered_controls) != tuple(
        range(1, len(decision.ordered_controls) + 1)
    ):
        raise SignedRiskValidationError("risk control ordinals must be contiguous")
    _reason_tuple(decision.block_reasons)
    _reason_tuple(decision.flatten_reasons)
    _source_timestamps(decision.source_timestamps)
    if type(decision.active_latches) is not tuple or decision.active_latches != tuple(
        sorted(set(decision.active_latches), key=str)
    ):
        raise SignedRiskValidationError("active latches must be unique and ordered")
    if any(type(latch_type) is not RiskLatchKind for latch_type in decision.active_latches):
        raise SignedRiskValidationError("active latch type is invalid")
    if type(decision.required_latch_events) is not tuple or any(
        type(event) is not RiskLatchEvent for event in decision.required_latch_events
    ):
        raise SignedRiskValidationError("required latch events must be immutable")
    for event in decision.required_latch_events:
        if (
            event.experiment_hash != decision.experiment_hash
            or event.correlation_id != decision.correlation_id
            or event.action.value != "ENGAGED"
            or event.latch_type not in decision.active_latches
        ):
            raise SignedRiskValidationError("required latch event does not match the decision")
    if type(decision.execution_scope) is not RiskExecutionScope:
        raise SignedRiskValidationError("risk execution scope is invalid")
    _validate_exposure_matches(decision.proposed_targets, decision.before_exposure)
    _validate_exposure_matches(decision.final_targets, decision.after_exposure)
    final_is_flat = all(weight == 0 for _, weight in decision.final_targets)
    if decision.execution_scope is RiskExecutionScope.FULL:
        if decision.block_reasons or decision.active_latches:
            raise SignedRiskValidationError("full execution scope cannot contain a block or latch")
    elif decision.execution_scope is RiskExecutionScope.NONE:
        if not decision.block_reasons or decision.flatten_reasons or not final_is_flat:
            raise SignedRiskValidationError("no-execution receipt is internally inconsistent")
    elif not decision.block_reasons or not decision.flatten_reasons or not final_is_flat:
        raise SignedRiskValidationError("risk-reducing receipt is internally inconsistent")
    expected_digest = _risk_decision_digest(
        slot_id=decision.slot_id,
        signal_id=decision.signal_id,
        signal_hash=decision.signal_hash,
        experiment_hash=decision.experiment_hash,
        policy_id=decision.policy_id,
        policy_version=decision.policy_version,
        policy_hash=decision.policy_hash,
        correlation_id=decision.correlation_id,
        decided_at=decision.decided_at,
        input_hash=decision.input_hash,
        statistics_hash=decision.statistics_hash,
        original_proposal=decision.original_proposal,
        proposed_targets=decision.proposed_targets,
        final_targets=decision.final_targets,
        before_exposure=decision.before_exposure,
        after_exposure=decision.after_exposure,
        ordered_controls=decision.ordered_controls,
        block_reasons=decision.block_reasons,
        flatten_reasons=decision.flatten_reasons,
        source_timestamps=decision.source_timestamps,
        latch_state_hash=decision.latch_state_hash,
        active_latches=decision.active_latches,
        required_latch_events=decision.required_latch_events,
        execution_scope=decision.execution_scope,
    )
    expected_id = DeterministicId(prefix="risk", digest=expected_digest).value
    if decision.risk_decision_id != expected_id or decision.content_hash != expected_digest:
        raise SignedRiskValidationError("risk decision identity or content hash is invalid")


def _risk_decision_digest(
    *,
    slot_id: str,
    signal_id: str,
    signal_hash: str,
    experiment_hash: str,
    policy_id: str,
    policy_version: int,
    policy_hash: str,
    correlation_id: str,
    decided_at: datetime,
    input_hash: str,
    statistics_hash: str,
    original_proposal: tuple[tuple[str, str, Decimal | None, Decimal | None], ...],
    proposed_targets: tuple[tuple[str, Decimal], ...],
    final_targets: tuple[tuple[str, Decimal], ...],
    before_exposure: ExposureSnapshot,
    after_exposure: ExposureSnapshot,
    ordered_controls: tuple[AppliedRiskControl, ...],
    block_reasons: tuple[str, ...],
    flatten_reasons: tuple[str, ...],
    source_timestamps: tuple[tuple[str, datetime], ...],
    latch_state_hash: str,
    active_latches: tuple[RiskLatchKind, ...],
    required_latch_events: tuple[RiskLatchEvent, ...],
    execution_scope: RiskExecutionScope,
) -> str:
    return sha256_hex(
        {
            "active_latches": active_latches,
            "after_exposure": _exposure_payload(after_exposure),
            "before_exposure": _exposure_payload(before_exposure),
            "block_reasons": block_reasons,
            "correlation_id": correlation_id,
            "decided_at": decided_at,
            "execution_scope": execution_scope,
            "experiment_hash": experiment_hash,
            "final_targets": final_targets,
            "flatten_reasons": flatten_reasons,
            "input_hash": input_hash,
            "latch_state_hash": latch_state_hash,
            "ordered_controls": _controls_payload(ordered_controls),
            "original_proposal": original_proposal,
            "policy_hash": policy_hash,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "proposed_targets": proposed_targets,
            "required_latch_events": tuple(event.content_hash for event in required_latch_events),
            "schema": "signed-risk-decision-v1",
            "signal_hash": signal_hash,
            "signal_id": signal_id,
            "slot_id": slot_id,
            "source_timestamps": source_timestamps,
            "statistics_hash": statistics_hash,
        }
    )


def _exposure_payload(exposure: ExposureSnapshot) -> dict[str, object]:
    return {
        "cluster_gross": exposure.cluster_gross,
        "gross": exposure.gross,
        "group_gross": exposure.group_gross,
        "net": exposure.net,
        "positive": exposure.positive,
        "short_abs": exposure.short_abs,
    }


def _validate_exposure_matches(
    weights: tuple[tuple[str, Decimal], ...],
    exposure: ExposureSnapshot,
) -> None:
    with localcontext(_ARITHMETIC):
        positive = sum((weight for _, weight in weights if weight > 0), Decimal(0))
        short_abs = sum((-weight for _, weight in weights if weight < 0), Decimal(0))
        if (
            exposure.positive != positive
            or exposure.short_abs != short_abs
            or exposure.gross != positive + short_abs
            or exposure.net != positive - short_abs
        ):
            raise SignedRiskValidationError("risk exposure does not match its target weights")


def _controls_payload(controls: tuple[AppliedRiskControl, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "after": control.after,
            "before": control.before,
            "control": control.control,
            "factor": control.factor,
            "ordinal": control.ordinal,
            "pass_number": control.pass_number,
            "scope": control.scope,
        }
        for control in controls
    )


def _complete_symbol_records(
    records: object,
    symbols: tuple[str, ...],
    record_type: type[object],
    field_name: str,
) -> None:
    if type(records) is not tuple or any(type(record) is not record_type for record in records):
        raise SignedRiskValidationError(f"{field_name} must be an immutable validated tuple")
    typed_records = records
    if tuple(record.symbol for record in typed_records) != symbols:
        raise SignedRiskValidationError(f"{field_name} must contain exactly active symbols")


def _named_decimals(value: object, *, field_name: str) -> set[str]:
    if type(value) is not tuple:
        raise SignedRiskValidationError(f"{field_name} must be immutable")
    symbols: list[str] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise SignedRiskValidationError(f"{field_name} entries are invalid")
        symbol, amount = item
        symbols.append(_symbol(symbol))
        _decimal(amount, field_name=field_name.replace(" ", "_"))
    if tuple(symbols) != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
        raise SignedRiskValidationError(f"{field_name} symbols must be unique and ordered")
    return set(symbols)


def _proposal_tuple(value: object) -> None:
    if type(value) is not tuple:
        raise SignedRiskValidationError("original proposal must be immutable")
    symbols: list[str] = []
    for item in value:
        if type(item) is not tuple or len(item) != 4:
            raise SignedRiskValidationError("original proposal entry is invalid")
        symbol, action, edge, target = item
        symbols.append(_symbol(symbol))
        if type(action) is not str or action not in {"LONG", "SHORT", "FLAT"}:
            raise SignedRiskValidationError("original proposal action is invalid")
        for amount in (edge, target):
            if amount is not None:
                _decimal(amount, field_name="proposal_value")
    if tuple(symbols) != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
        raise SignedRiskValidationError("original proposal symbols must be unique and ordered")


def _reason_tuple(value: object) -> None:
    if type(value) is not tuple or any(
        type(reason) is not str or _REASON.fullmatch(reason) is None for reason in value
    ):
        raise SignedRiskValidationError("risk reasons must be immutable reason codes")
    if len(value) != len(set(value)):
        raise SignedRiskValidationError("risk reasons must not contain duplicates")


def _source_timestamps(value: object) -> None:
    if type(value) is not tuple:
        raise SignedRiskValidationError("risk source timestamps must be immutable")
    previous: str | None = None
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise SignedRiskValidationError("risk source timestamp entry is invalid")
        name, instant = item
        if type(name) is not str or not name or (previous is not None and name <= previous):
            raise SignedRiskValidationError(
                "risk source timestamp names must be unique and ordered"
            )
        _instant(instant, field_name="source_timestamp")
        previous = name


def _symbol_tuple(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise SignedRiskValidationError("symbols must be immutable")
    symbols = tuple(_symbol(symbol) for symbol in value)
    if not allow_empty and not symbols:
        raise SignedRiskValidationError("symbols must not be empty")
    if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
        raise SignedRiskValidationError("symbols must be unique and alphabetically ordered")
    return symbols


def _decimal(value: object, *, field_name: str) -> Decimal:
    try:
        return require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise SignedRiskValidationError(f"{field_name} must be a finite exact decimal") from None


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    number = _decimal(value, field_name=field_name)
    if number <= 0:
        raise SignedRiskValidationError(f"{field_name} must be positive")
    return number


def _symbol(value: object) -> str:
    if type(value) is not str or _SYMBOL.fullmatch(value) is None:
        raise SignedRiskValidationError("symbol is invalid")
    return value


def _hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise SignedRiskValidationError(f"{field_name} must be lowercase SHA-256")
    return value


def _instant(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise SignedRiskValidationError(f"{field_name} must be a UTC instant") from None
