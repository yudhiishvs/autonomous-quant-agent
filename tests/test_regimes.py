"""Controlled tests for the transparent four-state regime detector."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.config import RegimeConfig
from adaptive_trader.regimes import RegimeDetector


def _detector() -> RegimeDetector:
    return RegimeDetector(
        RegimeConfig(
            fast_moving_average_days=5,
            slow_moving_average_days=20,
            volatility_lookback_days=5,
            volatility_threshold_lookback_days=20,
        )
    )


def _controlled_inputs(*, bull: bool, high_volatility: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2022-01-03", periods=60)
    direction = 1.002 if bull else 0.998
    prices = pd.DataFrame({"SPY": 100.0 * direction ** np.arange(len(dates))}, index=dates)
    returns = np.tile([0.001, -0.001], len(dates) // 2).astype(float)
    if high_volatility:
        returns[-6:] = [0.04, -0.04, 0.04, -0.04, 0.04, -0.04]
    return prices, pd.DataFrame({"SPY": returns}, index=dates)


@pytest.mark.parametrize(
    ("bull", "high_volatility", "expected"),
    [
        (True, False, "bull_low_vol"),
        (True, True, "bull_high_vol"),
        (False, False, "bear_low_vol"),
        (False, True, "bear_high_vol"),
    ],
)
def test_regime_detector_classifies_all_four_states(
    bull: bool, high_volatility: bool, expected: str
) -> None:
    prices, returns = _controlled_inputs(bull=bull, high_volatility=high_volatility)

    state = _detector().detect(prices, returns, prices.index[-1])

    assert state.name == expected
    assert state.metadata["classification"] == expected
    trend_is_bull = state.metadata["fast_moving_average"] >= state.metadata["slow_moving_average"]
    assert trend_is_bull is bull
    assert (
        state.metadata["realized_volatility"] > state.metadata["volatility_threshold"]
    ) is high_volatility


def test_regime_detector_is_causal_at_as_of_date() -> None:
    prices, returns = _controlled_inputs(bull=True, high_volatility=False)
    cutoff = prices.index[-11]
    original = _detector().detect(prices, returns, cutoff)

    changed_prices = prices.copy()
    changed_returns = returns.copy()
    changed_prices.loc[changed_prices.index > cutoff, "SPY"] *= 0.01
    changed_returns.loc[changed_returns.index > cutoff, "SPY"] = 0.50
    rerun = _detector().detect(changed_prices, changed_returns, cutoff)

    assert rerun.name == original.name
    assert rerun.metadata == original.metadata
    assert rerun.as_of_date == original.as_of_date == cutoff


def test_regime_detector_requires_sufficient_slow_average_history() -> None:
    prices, returns = _controlled_inputs(bull=True, high_volatility=False)

    with pytest.raises(ValueError, match="Insufficient benchmark history"):
        _detector().detect(prices.iloc[:10], returns.iloc[:10], prices.index[9])


def test_regime_detector_requires_configured_benchmark() -> None:
    prices, returns = _controlled_inputs(bull=True, high_volatility=False)
    renamed_prices = prices.rename(columns={"SPY": "OTHER"})

    with pytest.raises(ValueError, match="benchmark"):
        _detector().detect(renamed_prices, returns.rename(columns={"SPY": "OTHER"}))
