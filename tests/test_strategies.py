"""Offline causal-signal tests for momentum and mean reversion."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.config import MeanReversionConfig, MomentumConfig
from adaptive_trader.strategies import MeanReversionStrategy, MomentumStrategy


def _strategy_prices(periods: int = 180) -> pd.DataFrame:
    dates = pd.bdate_range("2021-01-04", periods=periods)
    day = np.arange(periods, dtype=float)
    leader_returns = 0.0015 + 0.003 * np.sin(day / 5.0)
    laggard_returns = 0.0002 + 0.004 * np.cos(day / 7.0)
    loser_returns = -0.0008 + 0.003 * np.sin(day / 6.0 + 1.0)
    prices = pd.DataFrame(
        {
            "LEADER": 100.0 * np.cumprod(1.0 + leader_returns),
            "LAGGARD": 100.0 * np.cumprod(1.0 + laggard_returns),
            "LOSER": 100.0 * np.cumprod(1.0 + loser_returns),
        },
        index=dates,
    )
    return prices


def _mean_reversion_prices(periods: int = 150) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", periods=periods)
    day = np.arange(periods, dtype=float)
    oversold = 100.0 + 0.32 * day + 0.35 * np.sin(day / 3.0)
    oversold[-1] -= 5.0  # Oversold, while still well above its 100-day trend.
    neutral = 90.0 + 0.20 * day + 0.50 * np.sin(day / 4.0)
    below_trend = 150.0 - 0.25 * day + 0.40 * np.sin(day / 4.5)
    below_trend[-1] -= 4.0
    return pd.DataFrame(
        {"OVERSOLD": oversold, "NEUTRAL": neutral, "BELOW": below_trend},
        index=dates,
    )


@pytest.mark.parametrize(
    "strategy",
    [
        MomentumStrategy(MomentumConfig()),
        MeanReversionStrategy(MeanReversionConfig()),
    ],
)
def test_strategy_weights_are_long_only_and_at_most_fully_invested(strategy) -> None:
    prices = (
        _strategy_prices() if isinstance(strategy, MomentumStrategy) else _mean_reversion_prices()
    )
    result = strategy.generate(
        prices=prices,
        returns=prices.pct_change(fill_method=None),
        as_of_date=prices.index[-1],
    )

    assert all(np.isfinite(weight) and weight >= 0.0 for weight in result.weights.values())
    assert sum(result.weights.values()) <= 1.0 + 1e-12
    assert result.cash_weight == pytest.approx(1.0 - sum(result.weights.values()))


def test_momentum_selects_leaders_and_excludes_nonpositive_returns() -> None:
    prices = _strategy_prices()
    config = replace(MomentumConfig(), top_n=2, require_positive_return=True)
    result = MomentumStrategy(config).generate(
        prices,
        prices.pct_change(fill_method=None),
        prices.index[-1],
    )

    assert result.metadata["selected_assets"][0] == "LEADER"
    assert "LOSER" not in result.weights
    assert result.metadata["exclusions"]["LOSER"] == "nonpositive_trailing_return"
    assert set(result.weights) == set(result.metadata["selected_assets"])


def test_mean_reversion_requires_oversold_price_above_long_term_trend() -> None:
    prices = _mean_reversion_prices()
    result = MeanReversionStrategy(MeanReversionConfig(top_n=3)).generate(
        prices=prices,
        returns=prices.pct_change(fill_method=None),
        as_of_date=prices.index[-1],
    )

    assert result.metadata["selected_assets"] == ["OVERSOLD"]
    assert result.metadata["zscores"]["OVERSOLD"] < -0.5
    assert (
        result.metadata["current_prices"]["OVERSOLD"]
        > result.metadata["long_term_moving_averages"]["OVERSOLD"]
    )
    assert "BELOW" not in result.weights


@pytest.mark.parametrize(
    ("strategy", "price_factory"),
    [
        (MomentumStrategy(MomentumConfig()), _strategy_prices),
        (MeanReversionStrategy(MeanReversionConfig()), _mean_reversion_prices),
    ],
)
def test_strategy_ignores_all_prices_and_returns_after_as_of_date(strategy, price_factory) -> None:
    prices = price_factory()
    cutoff = prices.index[-21]
    original_returns = prices.pct_change(fill_method=None)
    original = strategy.generate(prices, original_returns, cutoff)

    perturbed = prices.copy()
    perturbed.loc[perturbed.index > cutoff] *= np.array([50.0, 0.02, 10.0])
    perturbed_returns = perturbed.pct_change(fill_method=None)
    rerun = strategy.generate(perturbed, perturbed_returns, cutoff)

    assert rerun.weights == pytest.approx(original.weights)
    assert rerun.metadata == original.metadata
    assert rerun.as_of_date == original.as_of_date == cutoff


def test_invalid_or_zero_volatility_safely_produces_cash() -> None:
    dates = pd.bdate_range("2022-01-03", periods=80)
    prices = pd.DataFrame({"CONSTANT": 100.0}, index=dates)
    config = MomentumConfig(require_positive_return=False, top_n=1)

    result = MomentumStrategy(config).generate(prices, as_of_date=dates[-1])

    assert result.weights == {}
    assert result.cash_weight == 1.0
    assert result.metadata["exclusions"]["CONSTANT"] == "zero_or_invalid_volatility"


def test_mean_reversion_handles_no_qualifying_assets_as_cash() -> None:
    dates = pd.bdate_range("2022-01-03", periods=150)
    prices = pd.DataFrame(
        {
            "RISING_A": 100.0 + np.arange(len(dates), dtype=float),
            "RISING_B": 80.0 + 0.5 * np.arange(len(dates), dtype=float),
        },
        index=dates,
    )

    result = MeanReversionStrategy(MeanReversionConfig(top_n=2)).generate(
        prices,
        prices.pct_change(fill_method=None),
        dates[-1],
    )

    assert result.weights == {}
    assert result.cash_weight == 1.0
    assert result.metadata["selected_assets"] == []
