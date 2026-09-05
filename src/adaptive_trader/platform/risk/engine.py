"""Non-bypassable, fail-closed orchestration for the signed platform risk policy."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Context, Decimal, DecimalException, localcontext
from typing import Final

from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.risk.latches import (
    RiskLatchEvent,
    RiskLatchKind,
    assess_financial_latches,
    create_latch_engagement,
)
from adaptive_trader.platform.risk.models import (
    RiskDecision,
    RiskEvaluationRequest,
    RiskExecutionScope,
    SignedRiskValidationError,
    risk_input_hash,
)
from adaptive_trader.platform.risk.policy import (
    AppliedRiskControl,
    ExposureSnapshot,
    SignalDirection,
    SignedRiskPolicyError,
    SymbolSignal,
    apply_rebalance_band,
    apply_signed_constraints,
    calculate_initial_targets,
    correlation_components,
    exposure_snapshot,
    policy_hash,
)
from adaptive_trader.platform.signals.models import SignalAction, SignalValidationError

ACCOUNT_FRESHNESS: Final = timedelta(seconds=30)
SECURITY_FRESHNESS: Final = timedelta(seconds=300)
RECONCILIATION_FRESHNESS: Final = timedelta(seconds=60)
PLANNING_PRICE_FRESHNESS: Final = timedelta(seconds=120)
_ARITHMETIC: Final = Context(prec=50)


def evaluate_signed_risk(
    *,
    request: RiskEvaluationRequest,
    experiment: ExperimentDefinition,
) -> RiskDecision:
    """Evaluate one complete signal and return a deterministic, default-deny receipt.

    A decision grants full execution scope only after every identity, freshness, integrity,
    eligibility, latch, and numeric check passes. If critical state is stale or ambiguous, the
    result grants no order authority. Other failures produce explicit zero targets with only
    risk-reducing authority, allowing a later execution layer to flatten after revalidation.
    """

    if type(request) is not RiskEvaluationRequest:
        raise SignedRiskValidationError("signed risk requires a validated evaluation request")
    if type(experiment) is not ExperimentDefinition:
        raise SignedRiskValidationError("signed risk requires a validated experiment")
    expected_policy_hash = policy_hash(experiment.risk_policy, experiment.risk_groups)
    input_hash = risk_input_hash(request, policy_hash=expected_policy_hash)
    symbols = experiment.active_tradable
    zero_targets = tuple((symbol, Decimal(0)) for symbol in symbols)
    components = _safe_components(request=request, experiment=experiment)
    zero_exposure = exposure_snapshot(
        weights=zero_targets,
        risk_groups=experiment.risk_groups,
        correlation_components_=components,
    )
    source_timestamps = _source_timestamps(request)
    original_proposal = tuple(
        (
            symbol,
            request.signal.action_for(symbol).value,
            request.signal.edge_for(symbol),
            request.signal.target_input_for(symbol),
        )
        for symbol in symbols
    )

    reasons: list[str] = []
    no_execution_reasons: set[str] = set()
    _identity_gates(
        request=request,
        experiment=experiment,
        expected_policy_hash=expected_policy_hash,
        reasons=reasons,
    )
    _freshness_gates(request=request, reasons=reasons, no_execution_reasons=no_execution_reasons)
    _integrity_gates(request=request, reasons=reasons)
    _eligibility_gates(request=request, reasons=reasons, no_execution_reasons=no_execution_reasons)

    required_latch_events = _required_latch_events(
        request=request,
        experiment=experiment,
        account_is_fresh="account_stale" not in reasons,
        reconciliation_is_fresh="reconciliation_stale" not in reasons,
    )
    effective_latches = tuple(
        sorted(
            {
                *request.latch_state.active,
                *(event.latch_type for event in required_latch_events),
            },
            key=str,
        )
    )
    _latch_gates(
        request=request,
        active_latches=effective_latches,
        reasons=reasons,
    )

    unique_reasons = _deduplicate(reasons)
    if unique_reasons:
        scope = (
            RiskExecutionScope.NONE
            if no_execution_reasons.intersection(unique_reasons)
            else RiskExecutionScope.RISK_REDUCING_ONLY
        )
        flatten_reasons = unique_reasons if scope is RiskExecutionScope.RISK_REDUCING_ONLY else ()
        return _decision(
            request=request,
            experiment=experiment,
            policy_digest=expected_policy_hash,
            input_hash=input_hash,
            original_proposal=original_proposal,
            proposed_targets=zero_targets,
            final_targets=zero_targets,
            before_exposure=zero_exposure,
            after_exposure=zero_exposure,
            controls=(),
            block_reasons=unique_reasons,
            flatten_reasons=flatten_reasons,
            source_timestamps=source_timestamps,
            active_latches=effective_latches,
            required_latch_events=required_latch_events,
            execution_scope=scope,
        )

    try:
        signals = _normalized_signals(request)
        initial = calculate_initial_targets(
            signals=signals,
            statistics=request.statistics,
            policy=experiment.risk_policy,
        )
        constrained = apply_signed_constraints(
            targets=initial.targets,
            statistics=request.statistics,
            policy=experiment.risk_policy,
            risk_groups=experiment.risk_groups,
        )
    except (DomainValidationError, SignalValidationError):
        return _blocked_flat_decision(
            request=request,
            experiment=experiment,
            policy_digest=expected_policy_hash,
            input_hash=input_hash,
            original_proposal=original_proposal,
            reason="signal_or_statistics_invalid",
            zero_targets=zero_targets,
            zero_exposure=zero_exposure,
            source_timestamps=source_timestamps,
            active_latches=effective_latches,
            required_latch_events=required_latch_events,
        )
    if not constrained.converged:
        return _blocked_flat_decision(
            request=request,
            experiment=experiment,
            policy_digest=expected_policy_hash,
            input_hash=input_hash,
            original_proposal=original_proposal,
            reason=constrained.reason_code or "risk_constraints_not_converged",
            zero_targets=zero_targets,
            zero_exposure=zero_exposure,
            source_timestamps=source_timestamps,
            active_latches=effective_latches,
            required_latch_events=required_latch_events,
            proposed_targets=initial.targets,
            before_exposure=constrained.before_exposure,
            controls=constrained.controls,
        )

    try:
        projected_quantities = _projected_quantities(request)
        final_targets, band_controls = apply_rebalance_band(
            targets=constrained.final_targets,
            current_quantities=projected_quantities,
            prices={price.symbol: price.price for price in request.prices},
            equity=request.account.equity,
            statistics=request.statistics,
            policy=experiment.risk_policy,
            risk_groups=experiment.risk_groups,
            force_reduction=False,
        )
    except (DecimalException, DomainValidationError):
        return _blocked_flat_decision(
            request=request,
            experiment=experiment,
            policy_digest=expected_policy_hash,
            input_hash=input_hash,
            original_proposal=original_proposal,
            reason="rebalance_inputs_invalid",
            zero_targets=zero_targets,
            zero_exposure=zero_exposure,
            source_timestamps=source_timestamps,
            active_latches=effective_latches,
            required_latch_events=required_latch_events,
            proposed_targets=initial.targets,
            before_exposure=constrained.before_exposure,
            controls=constrained.controls,
        )
    final_exposure = exposure_snapshot(
        weights=final_targets,
        risk_groups=experiment.risk_groups,
        correlation_components_=constrained.correlation_components,
    )
    ordered_controls = _append_controls(constrained.controls, band_controls)
    flatten_reasons = ("all_scores_zero",) if initial.reason_code == "all_scores_zero" else ()
    return _decision(
        request=request,
        experiment=experiment,
        policy_digest=expected_policy_hash,
        input_hash=input_hash,
        original_proposal=original_proposal,
        proposed_targets=initial.targets,
        final_targets=final_targets,
        before_exposure=constrained.before_exposure,
        after_exposure=final_exposure,
        controls=ordered_controls,
        block_reasons=(),
        flatten_reasons=flatten_reasons,
        source_timestamps=source_timestamps,
        active_latches=effective_latches,
        required_latch_events=required_latch_events,
        execution_scope=RiskExecutionScope.FULL,
    )


def _identity_gates(
    *,
    request: RiskEvaluationRequest,
    experiment: ExperimentDefinition,
    expected_policy_hash: str,
    reasons: list[str],
) -> None:
    signal = request.signal
    context = request.decision_context
    try:
        signal.validate_for(context)
    except SignalValidationError:
        reasons.append("signal_context_mismatch")
    if (
        signal.experiment_id != experiment.experiment_id
        or signal.experiment_version != experiment.experiment_version
        or signal.experiment_hash != experiment.content_hash
        or context.slot.experiment_hash != experiment.content_hash
    ):
        reasons.append("experiment_identity_mismatch")
    if signal.active_symbols != experiment.active_tradable:
        reasons.append("active_symbol_set_mismatch")
    if signal.policy_hash != expected_policy_hash or context.policy_hash != expected_policy_hash:
        reasons.append("policy_hash_mismatch")
    if request.statistics.symbols != signal.active_symbols:
        reasons.append("statistics_symbol_mismatch")
    if request.statistics.eigenvalue_floor != experiment.risk_policy.covariance_eigenvalue_floor:
        reasons.append("statistics_policy_mismatch")
    if request.latch_state.experiment_hash != experiment.content_hash:
        reasons.append("latch_experiment_mismatch")
    if request.evaluated_at >= signal.expires_at:
        reasons.append("signal_expired")
    if request.evaluated_at >= context.slot.deadline_at:
        reasons.append("slot_deadline_passed")


def _freshness_gates(
    *,
    request: RiskEvaluationRequest,
    reasons: list[str],
    no_execution_reasons: set[str],
) -> None:
    checks = (
        ("account_stale", request.account.observed_at, ACCOUNT_FRESHNESS),
        (
            "reconciliation_stale",
            request.reconciliation.observed_at,
            RECONCILIATION_FRESHNESS,
        ),
    )
    for reason, observed_at, maximum_age in checks:
        if not _fresh(observed_at=observed_at, now=request.evaluated_at, maximum_age=maximum_age):
            reasons.append(reason)
            no_execution_reasons.add(reason)
    if not _fresh(
        observed_at=request.open_orders.observed_at,
        now=request.evaluated_at,
        maximum_age=RECONCILIATION_FRESHNESS,
    ):
        reasons.append("open_order_state_stale")
        no_execution_reasons.add("open_order_state_stale")
    for price in request.prices:
        if not price.validated:
            reason = f"planning_price_unvalidated_{price.symbol.lower()}"
            reasons.append(reason)
            no_execution_reasons.add(reason)
        if not _fresh(
            observed_at=price.observed_at,
            now=request.evaluated_at,
            maximum_age=PLANNING_PRICE_FRESHNESS,
        ):
            reason = f"planning_price_stale_{price.symbol.lower()}"
            reasons.append(reason)
            no_execution_reasons.add(reason)
    for security in request.security_metadata:
        if not _fresh(
            observed_at=security.observed_at,
            now=request.evaluated_at,
            maximum_age=SECURITY_FRESHNESS,
        ):
            reason = f"security_metadata_stale_{security.symbol.lower()}"
            reasons.append(reason)
            no_execution_reasons.add(reason)


def _integrity_gates(*, request: RiskEvaluationRequest, reasons: list[str]) -> None:
    integrity = request.market_integrity
    if not integrity.active_basket_complete or not all(request.signal.availability_mask):
        reasons.append("active_basket_incomplete")
    if integrity.unresolved_gap:
        reasons.append("unresolved_data_gap")
    if integrity.correction_uncertainty:
        reasons.append("correction_uncertainty")
    if not integrity.supported_session:
        reasons.append("unsupported_session")


def _eligibility_gates(
    *,
    request: RiskEvaluationRequest,
    reasons: list[str],
    no_execution_reasons: set[str],
) -> None:
    if request.account.equity <= 0:
        reasons.append("account_equity_nonpositive")
        no_execution_reasons.add("account_equity_nonpositive")
    if request.open_orders.ambiguous_order_exists or request.reconciliation.ambiguous_order_exists:
        reasons.append("ambiguous_order_exists")
        no_execution_reasons.add("ambiguous_order_exists")
    if request.open_orders.conflicting_symbols:
        reasons.append("open_order_conflict")
        no_execution_reasons.add("open_order_conflict")
    if not request.reconciliation.reconciled:
        reasons.append("reconciliation_not_clean")
        no_execution_reasons.add("reconciliation_not_clean")
    if (
        any(action is not SignalAction.FLAT for action in request.signal.actions)
        and request.account.buying_power <= 0
    ):
        reasons.append("buying_power_unavailable")
    for security in request.security_metadata:
        action = request.signal.action_for(security.symbol)
        if action is SignalAction.FLAT:
            continue
        symbol = security.symbol.lower()
        if not security.asset_active:
            reasons.append(f"asset_inactive_{symbol}")
        if not security.tradable:
            reasons.append(f"asset_not_tradable_{symbol}")
        if not security.primary_listing_eligible:
            reasons.append(f"primary_listing_ineligible_{symbol}")
        if not security.broker_capability_known:
            reasons.append(f"broker_capability_unknown_{symbol}")
        if action is SignalAction.SHORT and not (security.shortable and security.easy_to_borrow):
            reasons.append(f"short_not_eligible_{symbol}")


def _required_latch_events(
    *,
    request: RiskEvaluationRequest,
    experiment: ExperimentDefinition,
    account_is_fresh: bool,
    reconciliation_is_fresh: bool,
) -> tuple[RiskLatchEvent, ...]:
    events: list[RiskLatchEvent] = []
    if request.latch_state.experiment_hash != experiment.content_hash:
        return ()
    if account_is_fresh:
        assessment = assess_financial_latches(
            account_equity=request.account.equity,
            session_start_equity=request.session_start_equity,
            deployment_high_water_equity=request.deployment_high_water_equity,
            latch_state=request.latch_state,
            session_loss_trigger=experiment.risk_policy.session_loss_trigger,
            deployment_drawdown_trigger=experiment.risk_policy.deployment_drawdown_trigger,
        )
        for latch_type in assessment.engage:
            events.append(
                create_latch_engagement(
                    latch_state=request.latch_state,
                    latch_type=latch_type,
                    reason_code=latch_type.value,
                    actor="aqa_execution",
                    occurred_at=request.evaluated_at,
                    correlation_id=request.signal.correlation_id,
                    idempotency_key=f"risk_{request.signal.content_hash[:32]}_{latch_type.value[:16]}",
                )
            )
    if (
        reconciliation_is_fresh
        and not request.reconciliation.reconciled
        and not request.latch_state.is_active(RiskLatchKind.RECONCILIATION)
    ):
        events.append(
            create_latch_engagement(
                latch_state=request.latch_state,
                latch_type=RiskLatchKind.RECONCILIATION,
                reason_code="reconciliation_not_clean",
                actor="aqa_execution",
                occurred_at=request.evaluated_at,
                correlation_id=request.signal.correlation_id,
                idempotency_key=f"risk_{request.signal.content_hash[:32]}_reconciliation",
            )
        )
    return tuple(sorted(events, key=lambda event: event.latch_type.value))


def _latch_gates(
    *,
    request: RiskEvaluationRequest,
    active_latches: tuple[RiskLatchKind, ...],
    reasons: list[str],
) -> None:
    for latch_type in active_latches:
        reasons.append(f"{latch_type.value}_latch_active")
    if request.operator_halt and RiskLatchKind.OPERATOR_HALT not in active_latches:
        reasons.append("operator_halt_control_active")
    if not request.operator_halt and RiskLatchKind.OPERATOR_HALT in active_latches:
        reasons.append("operator_halt_state_mismatch")


def _normalized_signals(request: RiskEvaluationRequest) -> tuple[SymbolSignal, ...]:
    values: list[SymbolSignal] = []
    for symbol in request.signal.active_symbols:
        action = request.signal.action_for(symbol)
        direction = SignalDirection(action.value)
        edge = request.signal.edge_for(symbol)
        if direction is SignalDirection.FLAT and edge is None:
            edge = Decimal(0)
        values.append(SymbolSignal(symbol=symbol, direction=direction, expected_edge_bps=edge))
    return tuple(values)


def _projected_quantities(request: RiskEvaluationRequest) -> dict[str, Decimal]:
    positions = {position.symbol: position.quantity for position in request.positions}
    prices = {price.symbol: price.price for price in request.prices}
    reserved = dict(request.open_orders.reserved_signed_notional)
    with localcontext(_ARITHMETIC):
        return {
            symbol: positions[symbol] + (reserved[symbol] / prices[symbol])
            for symbol in request.signal.active_symbols
        }


def _blocked_flat_decision(
    *,
    request: RiskEvaluationRequest,
    experiment: ExperimentDefinition,
    policy_digest: str,
    input_hash: str,
    original_proposal: tuple[tuple[str, str, Decimal | None, Decimal | None], ...],
    reason: str,
    zero_targets: tuple[tuple[str, Decimal], ...],
    zero_exposure: ExposureSnapshot,
    source_timestamps: tuple[tuple[str, datetime], ...],
    active_latches: tuple[RiskLatchKind, ...],
    required_latch_events: tuple[RiskLatchEvent, ...],
    proposed_targets: tuple[tuple[str, Decimal], ...] | None = None,
    before_exposure: ExposureSnapshot | None = None,
    controls: tuple[AppliedRiskControl, ...] = (),
) -> RiskDecision:
    return _decision(
        request=request,
        experiment=experiment,
        policy_digest=policy_digest,
        input_hash=input_hash,
        original_proposal=original_proposal,
        proposed_targets=proposed_targets or zero_targets,
        final_targets=zero_targets,
        before_exposure=before_exposure or zero_exposure,
        after_exposure=zero_exposure,
        controls=controls,
        block_reasons=(reason,),
        flatten_reasons=(reason,),
        source_timestamps=source_timestamps,
        active_latches=active_latches,
        required_latch_events=required_latch_events,
        execution_scope=RiskExecutionScope.RISK_REDUCING_ONLY,
    )


def _decision(
    *,
    request: RiskEvaluationRequest,
    experiment: ExperimentDefinition,
    policy_digest: str,
    input_hash: str,
    original_proposal: tuple[tuple[str, str, Decimal | None, Decimal | None], ...],
    proposed_targets: tuple[tuple[str, Decimal], ...],
    final_targets: tuple[tuple[str, Decimal], ...],
    before_exposure: ExposureSnapshot,
    after_exposure: ExposureSnapshot,
    controls: tuple[AppliedRiskControl, ...],
    block_reasons: tuple[str, ...],
    flatten_reasons: tuple[str, ...],
    source_timestamps: tuple[tuple[str, datetime], ...],
    active_latches: tuple[RiskLatchKind, ...],
    required_latch_events: tuple[RiskLatchEvent, ...],
    execution_scope: RiskExecutionScope,
) -> RiskDecision:
    return RiskDecision.create(
        slot_id=request.signal.slot_id,
        signal_id=request.signal.signal_id,
        signal_hash=request.signal.content_hash,
        experiment_hash=experiment.content_hash,
        policy_id=experiment.risk_policy.id,
        policy_version=experiment.risk_policy.version,
        policy_hash=policy_digest,
        correlation_id=request.signal.correlation_id,
        decided_at=request.evaluated_at,
        input_hash=input_hash,
        statistics_hash=request.statistics.output_hash,
        original_proposal=original_proposal,
        proposed_targets=proposed_targets,
        final_targets=final_targets,
        before_exposure=before_exposure,
        after_exposure=after_exposure,
        ordered_controls=controls,
        block_reasons=block_reasons,
        flatten_reasons=flatten_reasons,
        source_timestamps=source_timestamps,
        latch_state_hash=request.latch_state.content_hash,
        active_latches=active_latches,
        required_latch_events=required_latch_events,
        execution_scope=execution_scope,
    )


def _source_timestamps(request: RiskEvaluationRequest) -> tuple[tuple[str, datetime], ...]:
    values: list[tuple[str, datetime]] = [
        ("account", request.account.observed_at),
        ("open_orders", request.open_orders.observed_at),
        ("reconciliation", request.reconciliation.observed_at),
        ("signal_created", request.signal.created_at),
        ("signal_source_bar_end", request.signal.source_bar_end),
    ]
    values.extend((f"price.{price.symbol}", price.observed_at) for price in request.prices)
    values.extend(
        (f"security.{security.symbol}", security.observed_at)
        for security in request.security_metadata
    )
    return tuple(sorted(values, key=lambda item: item[0]))


def _fresh(*, observed_at: datetime, now: datetime, maximum_age: timedelta) -> bool:
    age = now - observed_at
    return timedelta(0) <= age <= maximum_age


def _safe_components(
    *,
    request: RiskEvaluationRequest,
    experiment: ExperimentDefinition,
) -> tuple[tuple[str, ...], ...]:
    if request.statistics.symbols != experiment.active_tradable:
        return tuple((symbol,) for symbol in experiment.active_tradable)
    try:
        return correlation_components(
            statistics=request.statistics,
            threshold=experiment.risk_policy.correlation_edge_threshold,
        )
    except SignedRiskPolicyError:
        return tuple((symbol,) for symbol in experiment.active_tradable)


def _deduplicate(reasons: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def _append_controls(
    hard_controls: tuple[AppliedRiskControl, ...],
    band_controls: tuple[AppliedRiskControl, ...],
) -> tuple[AppliedRiskControl, ...]:
    if not band_controls:
        return hard_controls
    return (
        *hard_controls,
        *tuple(
            AppliedRiskControl(
                pass_number=control.pass_number,
                ordinal=len(hard_controls) + index,
                control=control.control,
                scope=control.scope,
                factor=control.factor,
                before=control.before,
                after=control.after,
            )
            for index, control in enumerate(band_controls, start=1)
        ),
    )
