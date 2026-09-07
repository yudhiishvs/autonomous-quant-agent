"""Risk receipts reject tampering and malformed inputs before authorizing exposure."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from adaptive_trader.platform.risk import (
    RiskLatchError,
    RiskLatchKind,
    RiskLatchState,
    SignalDirection,
    SignedRiskPolicyError,
    SymbolSignal,
    apply_rebalance_band,
    apply_signed_constraints,
    assess_financial_latches,
    calculate_initial_targets,
)
from adaptive_trader.platform.risk.models import RiskExecutionScope, SignedRiskValidationError
from tests.unit.test_platform_risk_latches import _empty, _engage
from tests.unit.test_platform_risk_policy import _statistics, experiment
from tests.unit.test_platform_risk_repository import _decision

__all__ = ["experiment"]


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "amd"},
        {"symbol": "A/B"},
        {"symbol": "\uff21\uff2d\uff24"},
        {"direction": "LONG"},
        {"expected_edge_bps": None},
        {"expected_edge_bps": Decimal(0)},
        {"expected_edge_bps": Decimal(-1)},
        {"expected_edge_bps": Decimal("1000001")},
        {"expected_edge_bps": 1.0},
        {"expected_edge_bps": Decimal("NaN")},
        {"direction": SignalDirection.SHORT, "expected_edge_bps": Decimal(1)},
        {"direction": SignalDirection.FLAT, "expected_edge_bps": Decimal(1)},
    ],
)
def test_signal_direction_and_edge_cannot_authorize_contradictory_exposure(change):
    good = SymbolSignal("AMD", SignalDirection.LONG, Decimal(10))
    with pytest.raises(SignedRiskPolicyError):
        replace(good, **change)


@pytest.mark.parametrize(
    "attack",
    [
        "empty",
        "mapping",
        "unvalidated",
        "duplicate",
        "reversed",
        "foreign_statistics",
        "unvalidated_statistics",
        "unvalidated_policy",
    ],
)
def test_sizing_refuses_ambiguous_symbol_identity(experiment, attack):
    signals = tuple(
        SymbolSignal(s, SignalDirection.LONG, Decimal(10)) for s in experiment.active_tradable
    )
    arguments = dict(
        signals=signals,
        statistics=_statistics(experiment.active_tradable),
        policy=experiment.risk_policy,
    )
    if attack == "empty":
        arguments["signals"] = ()
    elif attack == "mapping":
        arguments["signals"] = {s.symbol: s for s in signals}
    elif attack == "unvalidated":
        arguments["signals"] = (object(),)
    elif attack == "duplicate":
        arguments["signals"] = (signals[0], signals[0])
    elif attack == "reversed":
        arguments["signals"] = signals[::-1]
    elif attack == "foreign_statistics":
        arguments["statistics"] = _statistics(("AMD",))
    elif attack == "unvalidated_statistics":
        arguments["statistics"] = object()
    else:
        arguments["policy"] = object()
    with pytest.raises(SignedRiskPolicyError):
        calculate_initial_targets(**arguments)


@pytest.fixture
def receipt(experiment):
    return apply_signed_constraints(
        targets=tuple((s, Decimal("0.5")) for s in experiment.active_tradable),
        statistics=_statistics(experiment.active_tradable),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"final_targets": ()},
        {"final_targets": (("AMD", Decimal(0)),)},
        {"before_exposure": None},
        {"after_exposure": None},
        {"controls": []},
        {"controls": (object(),)},
        {"converged": 1},
        {"reason_code": ""},
        {"reason_code": "tampered"},
        {"content_hash": "0" * 64},
        {"correlation_components": "AMD"},
        {"correlation_components": ((),)},
        {"correlation_components": (("AMD", "AMD"),)},
        {"correlation_components": (("NVDA", "AMD"),)},
        {"correlation_components": (("AMD",),)},
    ],
)
def test_constraint_receipt_cannot_be_reused_after_tampering(receipt, change):
    with pytest.raises(SignedRiskPolicyError):
        replace(receipt, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"pass_number": True},
        {"pass_number": 0},
        {"ordinal": 0},
        {"control": "gross"},
        {"scope": ""},
        {"scope": "x" * 129},
        {"factor": Decimal("1.01")},
        {"factor": Decimal("-0.01")},
        {"before": []},
        {"before": ()},
        {"before": (("AMD",),)},
        {"before": (("AMD", Decimal(0)), ("AMD", Decimal(0)))},
    ],
)
def test_control_evidence_has_valid_order_scope_and_bounded_factor(receipt, change):
    with pytest.raises(SignedRiskPolicyError):
        replace(receipt.controls[0], **change)


@pytest.mark.parametrize(
    "change",
    [
        {"gross": Decimal(-1)},
        {"net": Decimal(20)},
        {"group_gross": []},
        {"group_gross": (("g",),)},
        {"group_gross": (("", Decimal(1)),)},
        {"group_gross": (("g", Decimal(-1)),)},
        {"group_gross": (("g", Decimal(1)), ("g", Decimal(1)))},
    ],
)
def test_exposure_receipt_cannot_hide_negative_or_duplicate_group_exposure(receipt, change):
    with pytest.raises(SignedRiskPolicyError):
        replace(receipt.before_exposure, **change)


@pytest.mark.parametrize(
    "attack",
    [
        "missing_quantity",
        "extra_price",
        "zero_price",
        "float_quantity",
        "zero_equity",
        "non_boolean_force",
        "empty_groups",
        "unvalidated_group",
        "duplicate_groups",
        "reversed_groups",
        "missing_group",
    ],
)
def test_rebalance_requires_complete_exact_account_and_partition(experiment, attack):
    symbols = experiment.active_tradable
    arguments = dict(
        targets=tuple((s, Decimal("0.01")) for s in symbols),
        current_quantities=dict.fromkeys(symbols, Decimal(0)),
        prices=dict.fromkeys(symbols, Decimal(100)),
        equity=Decimal(10000),
        statistics=_statistics(symbols),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
        force_reduction=False,
    )
    if attack == "missing_quantity":
        arguments["current_quantities"].pop(symbols[0])
    elif attack == "extra_price":
        arguments["prices"]["QQQ"] = Decimal(1)
    elif attack == "zero_price":
        arguments["prices"][symbols[0]] = Decimal(0)
    elif attack == "float_quantity":
        arguments["current_quantities"][symbols[0]] = 1.0
    elif attack == "zero_equity":
        arguments["equity"] = Decimal(0)
    elif attack == "non_boolean_force":
        arguments["force_reduction"] = 1
    elif attack == "empty_groups":
        arguments["risk_groups"] = ()
    elif attack == "unvalidated_group":
        arguments["risk_groups"] = (object(),)
    elif attack == "duplicate_groups":
        arguments["risk_groups"] = (experiment.risk_groups[0],) * 2
    elif attack == "reversed_groups":
        arguments["risk_groups"] = experiment.risk_groups[::-1]
    else:
        arguments["risk_groups"] = experiment.risk_groups[:-1]
    with pytest.raises(SignedRiskPolicyError):
        apply_rebalance_band(**arguments)


@pytest.mark.parametrize(
    "change",
    [
        {"latch_type": "session_loss"},
        {"sequence": True},
        {"sequence": 0},
        {"action": "ENGAGED"},
        {"actor": "Operator"},
        {"reason_code": "../clear"},
        {"occurred_at": datetime(2026, 7, 6)},
        {"correlation_id": "foreign"},
        {"idempotency_key": ""},
        {"experiment_hash": "A" * 64},
        {"latch_event_id": "latch_" + "0" * 64},
        {"content_hash": "0" * 64},
    ],
)
def test_restarted_latch_receipt_rejects_changed_identity_or_transition(change):
    with pytest.raises(RiskLatchError):
        replace(_engage(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"active": []},
        {"active": ("session_loss",)},
        {"last_sequences": []},
        {"last_sequences": (("session_loss", 1),)},
        {"last_sequences": ((RiskLatchKind.SESSION_LOSS, 1),) * 2},
        {"event_count": True},
        {"event_count": 2},
        {"source_hash": "invalid"},
        {"content_hash": "0" * 64},
    ],
)
def test_restarted_latch_projection_rejects_corrupt_stream_counts(change):
    good = RiskLatchState.from_events(experiment_hash=_empty().experiment_hash, events=(_engage(),))
    with pytest.raises(RiskLatchError):
        replace(good, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"account_equity": Decimal(-1)},
        {"account_equity": Decimal("NaN")},
        {"session_start_equity": Decimal(0)},
        {"deployment_high_water_equity": Decimal(0)},
        {"session_loss_trigger": Decimal(0)},
        {"deployment_drawdown_trigger": Decimal(-2)},
        {"latch_state": None},
    ],
)
def test_financial_latch_checks_refuse_unusable_account_state(change):
    arguments = dict(
        account_equity=Decimal(100),
        session_start_equity=Decimal(100),
        deployment_high_water_equity=Decimal(100),
        latch_state=_empty(),
    )
    arguments.update(change)
    with pytest.raises(RiskLatchError):
        assess_financial_latches(**arguments)


@pytest.mark.parametrize(
    "change",
    [
        {"risk_decision_id": "foreign"},
        {"slot_id": "foreign"},
        {"signal_id": "foreign"},
        {"correlation_id": "foreign"},
        {"policy_id": ""},
        {"policy_version": True},
        {"policy_version": 0},
        {"statistics_hash": "invalid"},
        {"decided_at": datetime(2026, 7, 6)},
        {"original_proposal": []},
        {"original_proposal": (("AMD",),)},
        {"original_proposal": (("AMD", "BUY", None, None),)},
        {"original_proposal": (("AMD", "FLAT", None, None),) * 2},
        {"proposed_targets": []},
        {"proposed_targets": (("AMD",),)},
        {"proposed_targets": (("amd", Decimal(0)),)},
        {"proposed_targets": (("AMD", Decimal(0)),) * 2},
        {"final_targets": (("NVDA", Decimal(0)),)},
        {"before_exposure": None},
        {"after_exposure": None},
        {"ordered_controls": []},
        {"ordered_controls": (object(),)},
        {"block_reasons": ["session_loss"]},
        {"block_reasons": ("BAD",)},
        {"block_reasons": ("session_loss", "session_loss")},
        {"source_timestamps": []},
        {"source_timestamps": (("account",),)},
        {"source_timestamps": (("", datetime(2026, 7, 6)),)},
        {"active_latches": []},
        {"active_latches": ("session_loss",)},
        {"required_latch_events": []},
        {"required_latch_events": (object(),)},
        {"execution_scope": "FULL"},
        {"account_snapshot": None},
        {"planning_positions": []},
        {"planning_prices": ()},
        {"security_metadata": ()},
        {"execution_scope": RiskExecutionScope.RISK_REDUCING_ONLY},
        {"content_hash": "0" * 64},
    ],
)
def test_signed_authority_receipt_rejects_corruption_before_reuse(change):
    with pytest.raises(SignedRiskValidationError):
        replace(_decision(), **change)
