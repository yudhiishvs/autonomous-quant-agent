"""Tests for edge-safe performance metrics and honest undefined values."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.metrics import calculate_metrics, rolling_sharpe_ratio
from adaptive_trader.models import PerformanceMetrics


def test_empty_metrics_are_undefined_with_reasons_while_counts_are_zero() -> None:
    metrics = calculate_metrics(pd.Series(dtype=float))

    for name, value in metrics.as_dict().items():
        if name.startswith("number_of_"):
            assert value == 0
        else:
            assert np.isnan(float(value)), name
            assert metrics.reason_for(name)
    assert "at least one finite return" in metrics.undefined_reasons["total_return"]


def test_constant_returns_do_not_produce_infinite_ratios() -> None:
    returns = pd.Series([0.001] * 20)
    metrics = calculate_metrics(returns)

    assert metrics.total_return == pytest.approx((1.001**20) - 1.0)
    assert metrics.annualized_volatility == pytest.approx(0.0)
    assert np.isnan(metrics.sharpe_ratio)
    assert "volatility is zero" in metrics.undefined_reasons["sharpe_ratio"]
    assert np.isnan(metrics.sortino_ratio)
    assert "downside deviation is zero" in metrics.undefined_reasons["sortino_ratio"]
    assert metrics.maximum_drawdown == 0.0
    assert np.isnan(metrics.calmar_ratio)
    assert "maximum drawdown is zero" in metrics.undefined_reasons["calmar_ratio"]
    assert metrics.positive_day_percentage == 1.0


def test_known_path_metrics_follow_documented_sign_conventions() -> None:
    returns = pd.Series([-0.10, 0.05, -0.20, 0.10])
    equity = pd.Series([1.0, 0.90, 0.945, 0.756, 0.8316])
    metrics = calculate_metrics(
        returns,
        equity_curve=equity,
        annualization_factor=4,
    )

    expected_volatility = returns.std(ddof=1) * np.sqrt(4)
    expected_sharpe = returns.mean() / returns.std(ddof=1) * np.sqrt(4)
    expected_downside = np.sqrt(np.mean(np.minimum(returns.to_numpy(), 0.0) ** 2))
    expected_sortino = returns.mean() * np.sqrt(4) / expected_downside

    assert metrics.total_return == pytest.approx(-0.1684)
    assert metrics.cagr == pytest.approx(-0.1684)
    assert metrics.annualized_volatility == pytest.approx(expected_volatility)
    assert metrics.sharpe_ratio == pytest.approx(expected_sharpe)
    assert metrics.sortino_ratio == pytest.approx(expected_sortino)
    assert metrics.maximum_drawdown == pytest.approx(-0.244)
    assert metrics.calmar_ratio == pytest.approx(-0.1684 / 0.244)
    assert metrics.cvar_95 == pytest.approx(-0.20)
    assert metrics.positive_day_percentage == pytest.approx(0.50)


def test_implementation_and_exposure_metrics_are_aggregated() -> None:
    metrics = calculate_metrics(
        pd.Series([0.01, -0.01, 0.0]),
        gross_exposure=pd.Series([0.80, 0.60, 0.40]),
        cash_allocation=pd.Series([0.20, 0.40, 0.60]),
        turnover=pd.Series([0.25, 0.10, 0.0]),
        transaction_costs=pd.Series([0.000125, 0.00005, 0.0]),
        number_of_rebalances=3,
        number_of_risk_interventions=2,
        number_of_hard_stop_events=1,
    )

    assert metrics.average_gross_exposure == pytest.approx(0.60)
    assert metrics.average_cash_allocation == pytest.approx(0.40)
    assert metrics.total_turnover == pytest.approx(0.35)
    assert metrics.estimated_transaction_costs == pytest.approx(0.000175)
    assert metrics.number_of_rebalances == 3
    assert metrics.number_of_risk_interventions == 2
    assert metrics.number_of_hard_stop_events == 1


def test_nonfinite_observations_are_omitted_without_hiding_missing_inputs() -> None:
    metrics = calculate_metrics(
        pd.Series([0.10, np.nan, np.inf, -0.05]),
        gross_exposure=pd.Series([0.5, np.nan, np.inf]),
    )

    assert metrics.total_return == pytest.approx(1.10 * 0.95 - 1.0)
    assert metrics.positive_day_percentage == 0.50
    assert metrics.average_gross_exposure == 0.5
    assert np.isfinite(metrics.sharpe_ratio)
    assert np.isnan(metrics.average_cash_allocation)
    assert "at least one finite cash" in metrics.undefined_reasons["average_cash_allocation"]


def test_single_observation_and_total_loss_are_safe() -> None:
    one_day = calculate_metrics(pd.Series([0.10]))
    total_loss = calculate_metrics(pd.Series([-1.0]))

    assert np.isnan(one_day.annualized_volatility)
    assert np.isnan(one_day.sharpe_ratio)
    assert np.isnan(one_day.sortino_ratio)
    assert "at least two" in one_day.undefined_reasons["annualized_volatility"]
    assert np.isfinite(one_day.cagr)
    assert total_loss.total_return == -1.0
    assert total_loss.cagr == -1.0
    assert total_loss.maximum_drawdown == -1.0
    assert total_loss.calmar_ratio == -1.0


def test_observed_zero_returns_preserve_defined_zeros() -> None:
    metrics = calculate_metrics(
        pd.Series([0.0, 0.0, 0.0]),
        gross_exposure=pd.Series([0.0, 0.0, 0.0]),
        cash_allocation=pd.Series([1.0, 1.0, 1.0]),
        turnover=pd.Series([0.0, 0.0, 0.0]),
        transaction_costs=pd.Series([0.0, 0.0, 0.0]),
    )

    assert metrics.total_return == 0.0
    assert metrics.cagr == 0.0
    assert metrics.annualized_volatility == 0.0
    assert metrics.maximum_drawdown == 0.0
    assert metrics.var_95 == 0.0
    assert metrics.cvar_95 == 0.0
    assert metrics.positive_day_percentage == 0.0
    assert metrics.average_gross_exposure == 0.0
    assert metrics.total_turnover == 0.0
    assert np.isnan(metrics.sharpe_ratio)
    assert np.isnan(metrics.sortino_ratio)
    assert np.isnan(metrics.calmar_ratio)
    for defined_zero in (
        "total_return",
        "cagr",
        "annualized_volatility",
        "maximum_drawdown",
        "var_95",
        "cvar_95",
        "positive_day_percentage",
        "average_gross_exposure",
        "total_turnover",
    ):
        assert defined_zero not in metrics.undefined_reasons


def test_all_nonfinite_observations_are_not_recast_as_zero() -> None:
    metrics = calculate_metrics(pd.Series([np.nan, np.inf, -np.inf]))

    assert np.isnan(metrics.total_return)
    assert np.isnan(metrics.maximum_drawdown)
    assert np.isnan(metrics.var_95)
    assert np.isnan(metrics.cvar_95)
    assert metrics.undefined_reasons["total_return"]


def test_performance_metric_model_allows_explained_nan_but_rejects_infinity() -> None:
    metrics = PerformanceMetrics(
        total_return=0.0,
        undefined_reasons={"sharpe_ratio": "Sharpe ratio is undefined because volatility is zero."},
    )

    assert metrics.as_dict()["total_return"] == 0.0
    assert "undefined_reasons" not in metrics.as_dict()
    assert np.isnan(metrics.sharpe_ratio)
    assert metrics.reason_for("sharpe_ratio") is not None
    serializable = metrics.as_serializable_dict()
    assert serializable["sharpe_ratio"] is None
    assert isinstance(serializable["undefined_reasons"], dict)
    json.dumps(serializable, allow_nan=False)
    with pytest.raises(ValueError, match="cannot be infinite"):
        PerformanceMetrics(total_return=float("inf"))


def test_rolling_sharpe_keeps_undefined_windows_as_nan() -> None:
    returns = pd.Series([0.01, -0.01, 0.02, -0.01, 0.01])
    result = rolling_sharpe_ratio(returns, window=3, annualization_factor=252)

    assert result.iloc[:2].isna().all()
    assert np.isfinite(result.iloc[2:]).all()

    constant = rolling_sharpe_ratio(pd.Series([0.01] * 5), window=3)
    assert constant.isna().all()


def test_invalid_counts_and_annualization_raise_clear_errors() -> None:
    with pytest.raises(ValueError, match="number_of_rebalances"):
        calculate_metrics(pd.Series(dtype=float), number_of_rebalances=-1)
    with pytest.raises(ValueError, match="annualization_factor"):
        calculate_metrics(pd.Series(dtype=float), annualization_factor=0)
