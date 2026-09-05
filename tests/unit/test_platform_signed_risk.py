"""End-to-end fail-closed tests for signed-risk evaluation receipts."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from adaptive_trader.platform.config import (
    BrokerAdapter,
    ExecutionMode,
    ExperimentDefinition,
    load_experiment,
)
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    ANNUALIZATION_FACTOR,
    DEFAULT_EIGENVALUE_FLOOR,
    RETURNS_PER_SYMBOL,
    AccountSnapshot,
    MarketIntegritySnapshot,
    OpenOrderSnapshot,
    PlanningPrice,
    ReconciliationSnapshot,
    RiskDecision,
    RiskEvaluationRequest,
    RiskExecutionScope,
    RiskLatchAction,
    RiskLatchKind,
    RiskLatchState,
    RiskStatistics,
    SecurityMetadataSnapshot,
    SignedPosition,
    SignedRiskValidationError,
    create_latch_engagement,
    evaluate_signed_risk,
    policy_hash,
)
from adaptive_trader.platform.scheduling import build_session_schedule
from adaptive_trader.platform.signals import (
    AlwaysFlatSignalProvider,
    DecisionContext,
    FixtureSignalScenario,
    OfflineFixtureSignalProvider,
)

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"
_SESSION_DATE = date(2026, 7, 6)
_DATA_HASH = sha256_hex(("risk-test-data", 1))


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_CONFIG_ROOT,
    )


def _statistics(symbols: tuple[str, ...]) -> RiskStatistics:
    covariance = tuple(
        tuple(Decimal("0.04") if left == right else Decimal(0) for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    correlation = tuple(
        tuple(Decimal(1) if left == right else Decimal(0) for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    sigma = (Decimal("0.2"),) * len(symbols)
    input_hash = "3" * 64
    output_hash = sha256_hex(
        {
            "annualization_factor": ANNUALIZATION_FACTOR,
            "annualized_covariance": covariance,
            "annualized_sigma": sigma,
            "eigenvalue_floor": DEFAULT_EIGENVALUE_FLOOR,
            "input_hash": input_hash,
            "observation_count": RETURNS_PER_SYMBOL,
            "prior_correlation": correlation,
            "schema": "signed-risk-statistics-output-v1",
            "symbols": symbols,
        }
    )
    return RiskStatistics(
        symbols=symbols,
        observation_count=RETURNS_PER_SYMBOL,
        annualization_factor=ANNUALIZATION_FACTOR,
        eigenvalue_floor=DEFAULT_EIGENVALUE_FLOOR,
        annualized_covariance=covariance,
        prior_correlation=correlation,
        annualized_sigma=sigma,
        input_hash=input_hash,
        output_hash=output_hash,
    )


def _context(
    experiment: ExperimentDefinition,
    *,
    provider_id: str = "deterministic_fixture",
) -> DecisionContext:
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id=provider_id,
        signal_provider_version="1",
        session_date=_SESSION_DATE,
        calendar=XnasExchangeCalendar(),
    )
    slot = schedule.strategy_slots[0]
    return DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=_DATA_HASH,
        policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
        execution_mode=ExecutionMode.OFFLINE,
        broker_adapter=BrokerAdapter.FAKE,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )


def _request(
    experiment: ExperimentDefinition,
    *,
    flat: bool = False,
    account_equity: Decimal = Decimal("100000"),
    session_start_equity: Decimal = Decimal("100000"),
    high_water_equity: Decimal = Decimal("100000"),
) -> RiskEvaluationRequest:
    context = _context(experiment, provider_id="always_flat" if flat else "deterministic_fixture")
    now = context.slot.ready_at + timedelta(seconds=10)
    signal = (
        AlwaysFlatSignalProvider(clock=lambda: context.slot.ready_at).signal_for(context)
        if flat
        else OfflineFixtureSignalProvider(
            clock=lambda: context.slot.ready_at,
            scenario=FixtureSignalScenario(
                first_slot_long_symbol="NVDA",
                first_slot_short_symbol="AMD",
                expected_edge_bps=Decimal("25"),
            ),
        ).signal_for(context)
    )
    symbols = experiment.active_tradable
    return RiskEvaluationRequest(
        signal=signal,
        decision_context=context,
        positions=tuple(SignedPosition(symbol=symbol, quantity=Decimal(0)) for symbol in symbols),
        open_orders=OpenOrderSnapshot.create(
            reserved_signed_notional={symbol: Decimal(0) for symbol in symbols},
            conflicting_symbols=(),
            ambiguous_order_exists=False,
            observed_at=now,
        ),
        account=AccountSnapshot(
            equity=account_equity,
            cash=account_equity,
            buying_power=account_equity,
            observed_at=now,
        ),
        prices=tuple(
            PlanningPrice(symbol=symbol, price=Decimal(100), observed_at=now, validated=True)
            for symbol in symbols
        ),
        security_metadata=tuple(
            SecurityMetadataSnapshot(
                symbol=symbol,
                asset_active=True,
                tradable=True,
                shortable=True,
                easy_to_borrow=True,
                primary_listing_eligible=True,
                broker_capability_known=True,
                observed_at=now,
            )
            for symbol in symbols
        ),
        reconciliation=ReconciliationSnapshot.create(
            reconciled=True,
            ambiguous_order_exists=False,
            observed_at=now,
        ),
        market_integrity=MarketIntegritySnapshot.create(
            active_basket_complete=True,
            unresolved_gap=False,
            correction_uncertainty=False,
            supported_session=True,
        ),
        session_start_equity=session_start_equity,
        deployment_high_water_equity=high_water_equity,
        statistics=_statistics(symbols),
        latch_state=RiskLatchState.empty(experiment_hash=experiment.content_hash),
        operator_halt=False,
        evaluated_at=now,
    )


def test_valid_fixture_produces_deterministic_signed_constrained_receipt(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    first = evaluate_signed_risk(request=request, experiment=experiment)
    replay = evaluate_signed_risk(request=request, experiment=experiment)

    assert first == replay
    assert first.risk_decision_id == f"risk_{first.content_hash}"
    assert first.execution_scope is RiskExecutionScope.FULL
    assert first.block_reasons == ()
    assert dict(first.final_targets)["AMD"] == Decimal("-0.15")
    assert dict(first.final_targets)["NVDA"] == Decimal("0.15")
    assert all(
        weight == 0 for symbol, weight in first.final_targets if symbol not in {"AMD", "NVDA"}
    )
    assert first.after_exposure.gross == Decimal("0.30")
    assert first.after_exposure.net == 0
    assert first.signal_hash == request.signal.content_hash
    assert first.policy_hash == policy_hash(experiment.risk_policy, experiment.risk_groups)
    assert first.source_timestamps == tuple(sorted(first.source_timestamps))


def test_flat_signal_keeps_explicit_zero_targets(experiment: ExperimentDefinition) -> None:
    decision = evaluate_signed_risk(request=_request(experiment, flat=True), experiment=experiment)

    assert decision.execution_scope is RiskExecutionScope.FULL
    assert set(dict(decision.final_targets).values()) == {Decimal(0)}
    assert decision.flatten_reasons == ("all_scores_zero",)


@pytest.mark.parametrize(
    "integrity_changes",
    [
        {"active_basket_complete": False},
        {"unresolved_gap": True},
        {"correction_uncertainty": True},
        {"supported_session": False},
    ],
)
def test_market_integrity_failures_flatten_and_never_create_exposure(
    experiment: ExperimentDefinition,
    integrity_changes: dict[str, bool],
) -> None:
    request = _request(experiment)
    integrity = MarketIntegritySnapshot.create(
        active_basket_complete=integrity_changes.get("active_basket_complete", True),
        unresolved_gap=integrity_changes.get("unresolved_gap", False),
        correction_uncertainty=integrity_changes.get("correction_uncertainty", False),
        supported_session=integrity_changes.get("supported_session", True),
    )
    decision = evaluate_signed_risk(
        request=replace(request, market_integrity=integrity),
        experiment=experiment,
    )

    assert decision.execution_scope is RiskExecutionScope.RISK_REDUCING_ONLY
    assert decision.block_reasons
    assert set(dict(decision.final_targets).values()) == {Decimal(0)}


@pytest.mark.parametrize("snapshot", ["account", "price", "security", "reconciliation"])
def test_stale_required_state_grants_no_order_authority(
    experiment: ExperimentDefinition,
    snapshot: str,
) -> None:
    request = _request(experiment)
    if snapshot == "account":
        request = replace(
            request,
            account=replace(
                request.account,
                observed_at=request.evaluated_at - timedelta(seconds=31),
            ),
        )
    elif snapshot == "price":
        request = replace(
            request,
            prices=(
                replace(
                    request.prices[0],
                    observed_at=request.evaluated_at - timedelta(seconds=121),
                ),
                *request.prices[1:],
            ),
        )
    elif snapshot == "security":
        request = replace(
            request,
            security_metadata=(
                replace(
                    request.security_metadata[0],
                    observed_at=request.evaluated_at - timedelta(seconds=301),
                ),
                *request.security_metadata[1:],
            ),
        )
    else:
        request = replace(
            request,
            reconciliation=ReconciliationSnapshot.create(
                reconciled=True,
                ambiguous_order_exists=False,
                observed_at=request.evaluated_at - timedelta(seconds=61),
            ),
        )
    decision = evaluate_signed_risk(request=request, experiment=experiment)

    assert decision.execution_scope is RiskExecutionScope.NONE
    assert any("stale" in reason for reason in decision.block_reasons)
    assert decision.flatten_reasons == ()


def test_exact_freshness_limits_pass_but_future_timestamps_fail_closed(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    boundary = replace(
        request,
        account=replace(
            request.account,
            observed_at=request.evaluated_at - timedelta(seconds=30),
        ),
        reconciliation=ReconciliationSnapshot.create(
            reconciled=True,
            ambiguous_order_exists=False,
            observed_at=request.evaluated_at - timedelta(seconds=60),
        ),
        prices=tuple(
            replace(price, observed_at=request.evaluated_at - timedelta(seconds=120))
            for price in request.prices
        ),
        security_metadata=tuple(
            replace(security, observed_at=request.evaluated_at - timedelta(seconds=300))
            for security in request.security_metadata
        ),
    )
    future = replace(
        request,
        account=replace(
            request.account, observed_at=request.evaluated_at + timedelta(microseconds=1)
        ),
    )

    assert (
        evaluate_signed_risk(request=boundary, experiment=experiment).execution_scope
        is RiskExecutionScope.FULL
    )
    assert (
        evaluate_signed_risk(request=future, experiment=experiment).execution_scope
        is RiskExecutionScope.NONE
    )


def test_session_loss_engages_append_only_latch_and_survives_restart(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(
        experiment,
        account_equity=Decimal("98000"),
        session_start_equity=Decimal("100000"),
        high_water_equity=Decimal("100000"),
    )
    decision = evaluate_signed_risk(request=request, experiment=experiment)

    assert decision.execution_scope is RiskExecutionScope.RISK_REDUCING_ONLY
    assert len(decision.required_latch_events) == 1
    event = decision.required_latch_events[0]
    assert event.latch_type is RiskLatchKind.SESSION_LOSS
    assert event.action is RiskLatchAction.ENGAGED
    assert RiskLatchKind.SESSION_LOSS in decision.active_latches
    assert set(dict(decision.final_targets).values()) == {Decimal(0)}

    restarted = RiskLatchState.from_events(
        experiment_hash=experiment.content_hash,
        events=decision.required_latch_events,
    )
    replay = evaluate_signed_risk(
        request=replace(request, latch_state=restarted),
        experiment=experiment,
    )
    assert replay.required_latch_events == ()
    assert RiskLatchKind.SESSION_LOSS in replay.active_latches


def test_reconciliation_failure_engages_latch_and_ambiguity_blocks_all_orders(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    broken = replace(
        request,
        reconciliation=ReconciliationSnapshot.create(
            reconciled=False,
            ambiguous_order_exists=True,
            observed_at=request.evaluated_at,
        ),
    )
    decision = evaluate_signed_risk(request=broken, experiment=experiment)

    assert decision.execution_scope is RiskExecutionScope.NONE
    assert "ambiguous_order_exists" in decision.block_reasons
    assert "reconciliation_not_clean" in decision.block_reasons
    assert tuple(event.latch_type for event in decision.required_latch_events) == (
        RiskLatchKind.RECONCILIATION,
    )


def test_zero_equity_fails_closed_and_engages_financial_latches(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(
        experiment,
        account_equity=Decimal(0),
        session_start_equity=Decimal("100000"),
        high_water_equity=Decimal("100000"),
    )
    request = replace(
        request,
        account=replace(request.account, buying_power=Decimal(0)),
    )
    decision = evaluate_signed_risk(request=request, experiment=experiment)

    assert decision.execution_scope is RiskExecutionScope.NONE
    assert "account_equity_nonpositive" in decision.block_reasons
    assert tuple(event.latch_type for event in decision.required_latch_events) == (
        RiskLatchKind.DEPLOYMENT_DRAWDOWN,
        RiskLatchKind.SESSION_LOSS,
    )
    assert set(dict(decision.final_targets).values()) == {Decimal(0)}


def test_experiment_identity_substitution_returns_only_a_flat_receipt(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    substituted = experiment.model_copy(update={"experiment_id": "substituted_experiment"})
    decision = evaluate_signed_risk(request=request, experiment=substituted)

    assert "experiment_identity_mismatch" in decision.block_reasons
    assert "latch_experiment_mismatch" in decision.block_reasons
    assert decision.execution_scope is RiskExecutionScope.RISK_REDUCING_ONLY
    assert decision.required_latch_events == ()
    assert set(dict(decision.final_targets).values()) == {Decimal(0)}


def test_short_eligibility_and_operator_halt_are_non_bypassable(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    securities = tuple(
        replace(security, shortable=False, easy_to_borrow=False)
        if security.symbol == "AMD"
        else security
        for security in request.security_metadata
    )
    short_block = evaluate_signed_risk(
        request=replace(request, security_metadata=securities),
        experiment=experiment,
    )
    halt_block = evaluate_signed_risk(
        request=replace(request, operator_halt=True),
        experiment=experiment,
    )

    assert "short_not_eligible_amd" in short_block.block_reasons
    assert short_block.execution_scope is RiskExecutionScope.RISK_REDUCING_ONLY
    assert "operator_halt_control_active" in halt_block.block_reasons


def test_existing_latch_and_conflicting_orders_force_safe_receipts(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    event = create_latch_engagement(
        latch_state=request.latch_state,
        latch_type=RiskLatchKind.OPERATOR_HALT,
        reason_code="operator_halt",
        actor="aqa_execution",
        occurred_at=request.evaluated_at,
        correlation_id=request.signal.correlation_id,
        idempotency_key="operator_halt_test",
    )
    latch_state = RiskLatchState.from_events(
        experiment_hash=experiment.content_hash,
        events=(event,),
    )
    halted = evaluate_signed_risk(
        request=replace(request, latch_state=latch_state, operator_halt=True),
        experiment=experiment,
    )
    conflicts = evaluate_signed_risk(
        request=replace(
            request,
            open_orders=OpenOrderSnapshot.create(
                reserved_signed_notional={
                    symbol: Decimal(0) for symbol in experiment.active_tradable
                },
                conflicting_symbols=("AMD",),
                ambiguous_order_exists=False,
                observed_at=request.evaluated_at,
            ),
        ),
        experiment=experiment,
    )

    assert halted.execution_scope is RiskExecutionScope.RISK_REDUCING_ONLY
    assert "operator_halt_latch_active" in halted.block_reasons
    assert conflicts.execution_scope is RiskExecutionScope.NONE
    assert "open_order_conflict" in conflicts.block_reasons


def test_decision_rejects_tampering_and_incomplete_input_sets(
    experiment: ExperimentDefinition,
) -> None:
    request = _request(experiment)
    decision = evaluate_signed_risk(request=request, experiment=experiment)
    with pytest.raises(SignedRiskValidationError, match=r"exposure|identity or content hash"):
        replace(
            decision,
            final_targets=tuple((symbol, Decimal(0)) for symbol in experiment.active_tradable),
        )
    with pytest.raises(SignedRiskValidationError, match="exactly active symbols"):
        replace(request, prices=request.prices[:-1])

    reconstructed = RiskDecision.create(
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
    assert reconstructed == decision
