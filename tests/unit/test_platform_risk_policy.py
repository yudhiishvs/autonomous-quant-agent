"""Formula, constraint-order, exposure, and rebalance tests for signed risk."""

from __future__ import annotations

import random
from decimal import Decimal, getcontext, setcontext
from pathlib import Path

import pytest

from adaptive_trader.platform.config import (
    ExperimentDefinition,
    RiskGroupSpec,
    RiskPolicySpec,
    load_experiment,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    ANNUALIZATION_FACTOR,
    DEFAULT_EIGENVALUE_FLOOR,
    RETURNS_PER_SYMBOL,
    RiskControl,
    RiskStatistics,
    SignalDirection,
    SignedRiskPolicyError,
    SymbolSignal,
    apply_rebalance_band,
    apply_signed_constraints,
    calculate_initial_targets,
    constraints_satisfied,
    correlation_components,
    policy_hash,
)

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_CONFIG_ROOT,
    )


def _statistics(
    symbols: tuple[str, ...],
    *,
    correlation: Decimal = Decimal(0),
) -> RiskStatistics:
    covariance = tuple(
        tuple(
            Decimal("0.04") if left == right else Decimal("0.04") * correlation
            for right in range(len(symbols))
        )
        for left in range(len(symbols))
    )
    correlations = tuple(
        tuple(Decimal(1) if left == right else correlation for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    sigma = (Decimal("0.2"),) * len(symbols)
    input_hash = "1" * 64
    output_hash = sha256_hex(
        {
            "annualization_factor": ANNUALIZATION_FACTOR,
            "annualized_covariance": covariance,
            "annualized_sigma": sigma,
            "eigenvalue_floor": DEFAULT_EIGENVALUE_FLOOR,
            "input_hash": input_hash,
            "observation_count": RETURNS_PER_SYMBOL,
            "prior_correlation": correlations,
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
        prior_correlation=correlations,
        annualized_sigma=sigma,
        input_hash=input_hash,
        output_hash=output_hash,
    )


def _policy_with(policy: RiskPolicySpec, **changes: Decimal) -> RiskPolicySpec:
    values = policy.model_dump()
    values.update(changes)
    return RiskPolicySpec.model_validate(values, strict=True)


def _groups_with_limit(
    groups: tuple[RiskGroupSpec, ...], limit: Decimal
) -> tuple[RiskGroupSpec, ...]:
    return tuple(
        RiskGroupSpec.model_validate(
            {**group.model_dump(), "max_gross_weight": limit},
            strict=True,
        )
        for group in groups
    )


def test_raw_score_and_volatility_scaling_match_exact_formula(
    experiment: ExperimentDefinition,
) -> None:
    statistics = _statistics(experiment.active_tradable)
    signals = tuple(
        SymbolSignal(
            symbol=symbol,
            direction=(
                SignalDirection.SHORT
                if symbol == "AMD"
                else SignalDirection.LONG
                if symbol == "NVDA"
                else SignalDirection.FLAT
            ),
            expected_edge_bps=(
                Decimal("-25")
                if symbol == "AMD"
                else Decimal("25")
                if symbol == "NVDA"
                else Decimal(0)
            ),
        )
        for symbol in experiment.active_tradable
    )

    original_context = getcontext().copy()
    try:
        getcontext().prec = 6
        low_precision = calculate_initial_targets(
            signals=signals,
            statistics=statistics,
            policy=experiment.risk_policy,
        )
        getcontext().prec = 28
        normal_precision = calculate_initial_targets(
            signals=signals,
            statistics=statistics,
            policy=experiment.risk_policy,
        )
    finally:
        setcontext(original_context)

    assert low_precision == normal_precision
    scores = dict(normal_precision.raw_scores)
    assert scores["AMD"] == Decimal("-5")
    assert scores["NVDA"] == Decimal("5")
    assert float(normal_precision.forecast_volatility or 0) == pytest.approx(2**0.5)
    assert float(normal_precision.multiplier or 0) == pytest.approx(0.10 / (2**0.5))
    assert (
        dict(normal_precision.targets)["AMD"]
        == dict(normal_precision.targets)["NVDA"].copy_negate()
    )


def test_all_flat_skips_forecast_volatility(experiment: ExperimentDefinition) -> None:
    result = calculate_initial_targets(
        signals=tuple(
            SymbolSignal(symbol=symbol, direction=SignalDirection.FLAT, expected_edge_bps=None)
            for symbol in experiment.active_tradable
        ),
        statistics=_statistics(experiment.active_tradable),
        policy=experiment.risk_policy,
    )

    assert result.reason_code == "all_scores_zero"
    assert result.forecast_volatility is result.multiplier is None
    assert set(dict(result.targets).values()) == {Decimal(0)}


def test_constraint_controls_follow_exact_order_and_never_redistribute(
    experiment: ExperimentDefinition,
) -> None:
    symbols = experiment.active_tradable
    targets = tuple(
        (symbol, Decimal("0.5") if index % 2 == 0 else Decimal("-0.5"))
        for index, symbol in enumerate(symbols)
    )
    result = apply_signed_constraints(
        targets=targets,
        statistics=_statistics(symbols, correlation=Decimal("0.9")),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
    )

    controls = tuple(control.control for control in result.controls)
    assert controls[:8] == (RiskControl.SYMBOL_CLIP,) * 8
    assert controls[8] is RiskControl.GROSS
    assert controls[9] is RiskControl.CLUSTER_GROSS
    assert result.converged
    assert result.after_exposure.gross == Decimal("0.40")
    assert all(
        abs(final) <= abs(original)
        for (_, original), (_, final) in zip(
            result.original_targets, result.final_targets, strict=True
        )
    )
    assert constraints_satisfied(
        weights=result.final_targets,
        statistics=_statistics(symbols, correlation=Decimal("0.9")),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
    )


@pytest.mark.parametrize(
    ("sign", "expected_control", "expected_net"),
    [
        (Decimal(1), RiskControl.NET_LONG, Decimal("0.30")),
        (Decimal(-1), RiskControl.NET_SHORT, Decimal("-0.30")),
    ],
)
def test_positive_and_negative_net_constraints(
    experiment: ExperimentDefinition,
    sign: Decimal,
    expected_control: RiskControl,
    expected_net: Decimal,
) -> None:
    targets = tuple((symbol, sign * Decimal("0.10")) for symbol in experiment.active_tradable)
    result = apply_signed_constraints(
        targets=targets,
        statistics=_statistics(experiment.active_tradable),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
    )

    assert expected_control in tuple(control.control for control in result.controls)
    assert result.after_exposure.net == expected_net


def test_group_constraint_uses_lexicographic_group_order(
    experiment: ExperimentDefinition,
) -> None:
    groups = _groups_with_limit(experiment.risk_groups, Decimal("0.20"))
    targets = tuple((symbol, Decimal("0.10")) for symbol in experiment.active_tradable)
    balanced = tuple(
        (symbol, value if index % 2 == 0 else -value)
        for index, (symbol, value) in enumerate(targets)
    )
    result = apply_signed_constraints(
        targets=balanced,
        statistics=_statistics(experiment.active_tradable),
        policy=experiment.risk_policy,
        risk_groups=groups,
    )

    group_controls = tuple(
        control for control in result.controls if control.control is RiskControl.RISK_GROUP_GROSS
    )
    assert tuple(control.scope for control in group_controls) == (
        "group:compute_storage_materials",
        "group:connectivity",
    )
    assert dict(result.after_exposure.group_gross) == {
        "compute_storage_materials": Decimal("0.20"),
        "connectivity": Decimal("0.20"),
    }


def test_correlation_edges_are_strict_and_components_deterministic(
    experiment: ExperimentDefinition,
) -> None:
    symbols = experiment.active_tradable
    at_threshold = correlation_components(
        statistics=_statistics(symbols, correlation=Decimal("0.80")),
        threshold=Decimal("0.80"),
    )
    above_threshold = correlation_components(
        statistics=_statistics(symbols, correlation=Decimal("0.8001")),
        threshold=Decimal("0.80"),
    )

    assert at_threshold == tuple((symbol,) for symbol in symbols)
    assert above_threshold == (symbols,)


def test_rebalance_band_retains_only_safe_nonzero_weights_and_zero_bypasses(
    experiment: ExperimentDefinition,
) -> None:
    symbols = experiment.active_tradable
    targets = tuple(
        (symbol, Decimal("0.10") if symbol == "AMD" else Decimal(0)) for symbol in symbols
    )
    quantities = {symbol: Decimal(0) for symbol in symbols}
    quantities["AMD"] = Decimal("101")
    prices = {symbol: Decimal(100) for symbol in symbols}
    retained, controls = apply_rebalance_band(
        targets=targets,
        current_quantities=quantities,
        prices=prices,
        equity=Decimal("100000"),
        statistics=_statistics(symbols),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
        force_reduction=False,
    )
    forced, forced_controls = apply_rebalance_band(
        targets=targets,
        current_quantities=quantities,
        prices=prices,
        equity=Decimal("100000"),
        statistics=_statistics(symbols),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
        force_reduction=True,
    )

    assert dict(retained)["AMD"] == Decimal("0.101")
    assert all(dict(retained)[symbol] == 0 for symbol in symbols if symbol != "AMD")
    assert tuple(control.control for control in controls) == (RiskControl.REBALANCE_BAND,)
    assert forced == targets
    assert forced_controls == ()


def test_rebalance_threshold_is_strictly_below_and_cap_violations_are_not_retained(
    experiment: ExperimentDefinition,
) -> None:
    symbols = experiment.active_tradable
    target = tuple(
        (symbol, Decimal("0.10") if symbol == "AMD" else Decimal(0)) for symbol in symbols
    )
    prices = {symbol: Decimal(100) for symbol in symbols}
    at_threshold = {symbol: Decimal(0) for symbol in symbols}
    at_threshold["AMD"] = Decimal("102.5")
    unsafe = dict(at_threshold)
    unsafe["AMD"] = Decimal("151")

    unchanged, _ = apply_rebalance_band(
        targets=target,
        current_quantities=at_threshold,
        prices=prices,
        equity=Decimal("100000"),
        statistics=_statistics(symbols),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
        force_reduction=False,
    )
    cap_safe, _ = apply_rebalance_band(
        targets=tuple(
            (symbol, Decimal("0.149") if symbol == "AMD" else Decimal(0)) for symbol in symbols
        ),
        current_quantities=unsafe,
        prices=prices,
        equity=Decimal("100000"),
        statistics=_statistics(symbols),
        policy=experiment.risk_policy,
        risk_groups=experiment.risk_groups,
        force_reduction=False,
    )

    assert unchanged == target
    assert dict(cap_safe)["AMD"] == Decimal("0.149")


def test_seeded_constraint_property_matrix_is_shrink_only_and_within_every_cap(
    experiment: ExperimentDefinition,
) -> None:
    generator = random.Random(20260905)
    statistics = _statistics(experiment.active_tradable, correlation=Decimal("0.3"))
    for _ in range(50):
        targets = tuple(
            (symbol, Decimal(str(generator.uniform(-2, 2))))
            for symbol in experiment.active_tradable
        )
        result = apply_signed_constraints(
            targets=targets,
            statistics=statistics,
            policy=experiment.risk_policy,
            risk_groups=experiment.risk_groups,
        )
        assert result.converged
        assert constraints_satisfied(
            weights=result.final_targets,
            statistics=statistics,
            policy=experiment.risk_policy,
            risk_groups=experiment.risk_groups,
        )
        assert all(
            abs(final) <= abs(original)
            for (_, original), (_, final) in zip(
                result.original_targets, result.final_targets, strict=True
            )
        )


def test_signal_signs_and_policy_hash_are_strict(experiment: ExperimentDefinition) -> None:
    with pytest.raises(SignedRiskPolicyError, match="long expected edge"):
        SymbolSignal(
            symbol="AMD",
            direction=SignalDirection.LONG,
            expected_edge_bps=Decimal("-1"),
        )
    first = policy_hash(experiment.risk_policy, experiment.risk_groups)
    changed = policy_hash(
        _policy_with(experiment.risk_policy, sigma_floor=Decimal("0.21")),
        experiment.risk_groups,
    )
    assert first != changed
