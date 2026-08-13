"""Tests for causal daily simulation, costs, schedules, and determinism."""

from __future__ import annotations

from itertools import pairwise

import pandas as pd
import pytest

from adaptive_trader.backtest import PORTFOLIO_NAMES, _drift_weights, run_backtest_suite
from adaptive_trader.config import AppConfig


def test_suite_contains_all_baselines_and_finite_daily_state(fast_suite) -> None:
    assert tuple(fast_suite.runs) == PORTFOLIO_NAMES
    for run in fast_suite.runs.values():
        assert not run.daily.empty
        assert run.daily.index.is_monotonic_increasing
        assert (run.daily["equity"] > 0.0).all()
        assert (run.daily["gross_exposure"] >= -1e-12).all()
        assert (run.daily["gross_exposure"] <= 1.0 + 1e-9).all()
        assert (run.daily["cash_weight"] >= -1e-12).all()
        assert (run.weights.sum(axis=1) - 1.0).abs().max() < 1e-9


def test_weekly_rebalances_use_first_available_iso_week_date(fast_suite) -> None:
    run = fast_suite.runs["adaptive"]
    execution_dates = run.daily.index
    expected = [execution_dates[0]]
    for previous, current in pairwise(execution_dates):
        if (previous.isocalendar().year, previous.isocalendar().week) != (
            current.isocalendar().year,
            current.isocalendar().week,
        ):
            expected.append(current)
    actual = [pd.Timestamp(row["execution_date"]) for row in run.rebalances]
    assert actual == expected


def test_transaction_costs_reduce_buy_and_hold_wealth(fast_config, fast_market_data) -> None:
    without_cost_values = fast_config.to_dict()
    without_cost_values["backtest"]["transaction_cost_bps"] = 0.0
    with_cost_values = fast_config.to_dict()
    with_cost_values["backtest"]["transaction_cost_bps"] = 25.0
    without_cost = run_backtest_suite(AppConfig.from_dict(without_cost_values), fast_market_data)
    with_cost = run_backtest_suite(AppConfig.from_dict(with_cost_values), fast_market_data)
    for name in ("equal_weight_buy_hold", "benchmark_buy_hold"):
        wealth_without = without_cost.runs[name].daily["equity"].iloc[-1]
        wealth_with = with_cost.runs[name].daily["equity"].iloc[-1]
        assert wealth_with < wealth_without
        assert with_cost.runs[name].daily["transaction_cost"].sum() == 0.0025


def test_backtest_is_deterministic(fast_suite, fast_config, fast_market_data) -> None:
    repeated = run_backtest_suite(fast_config, fast_market_data)
    for name in PORTFOLIO_NAMES:
        pd.testing.assert_frame_equal(fast_suite.runs[name].daily, repeated.runs[name].daily)
        pd.testing.assert_frame_equal(fast_suite.runs[name].weights, repeated.runs[name].weights)
        assert fast_suite.runs[name].decision_receipts == repeated.runs[name].decision_receipts


def test_signal_dates_always_precede_execution_dates(fast_suite) -> None:
    for run in fast_suite.runs.values():
        for receipt in run.decision_receipts:
            assert pd.Timestamp(receipt["signal_as_of_date"]) < pd.Timestamp(
                receipt["execution_date"]
            )


def test_portfolio_weights_drift_with_held_asset_returns() -> None:
    drifted, cash, portfolio_return = _drift_weights(
        {"WINNER": 0.50, "LOSER": 0.25},
        0.25,
        pd.Series({"WINNER": 0.10, "LOSER": -0.10}),
    )

    assert portfolio_return == 0.025
    assert drifted["WINNER"] > 0.50
    assert drifted["LOSER"] < 0.25
    assert cash < 0.25
    assert sum(drifted.values()) + cash == pytest.approx(1.0)
