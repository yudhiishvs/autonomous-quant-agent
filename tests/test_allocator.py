"""Tests for adaptive and static strategy portfolio combination."""

from __future__ import annotations

import pandas as pd
import pytest

from adaptive_trader.allocator import AdaptiveAllocator, StaticAllocator
from adaptive_trader.config import RegimeConfig
from adaptive_trader.models import RegimeState, StrategyResult


def _inputs() -> tuple[RegimeState, StrategyResult, StrategyResult]:
    date = pd.Timestamp("2024-01-31")
    regime = RegimeState(
        name="bull_high_vol",
        as_of_date=date,
        metadata={"realized_volatility": 0.2, "volatility_threshold": 0.1},
    )
    momentum = StrategyResult(
        name="momentum",
        as_of_date=date,
        weights={"A": 0.60, "B": 0.40},
        metadata={"selected_assets": ["A", "B"]},
    )
    mean_reversion = StrategyResult(
        name="mean_reversion",
        as_of_date=date,
        weights={"B": 0.50},
        metadata={"selected_assets": ["B"]},
    )
    return regime, momentum, mean_reversion


def test_adaptive_allocator_combines_assets_and_preserves_all_cash_components() -> None:
    regime, momentum, mean_reversion = _inputs()

    decision = AdaptiveAllocator(RegimeConfig()).allocate(regime, momentum, mean_reversion)

    # bull_high_vol = 45% momentum, 35% mean reversion, 20% strategic cash.
    assert decision.pre_risk_weights == pytest.approx({"A": 0.27, "B": 0.355})
    assert decision.pre_risk_cash == pytest.approx(0.375)
    assert decision.strategy_allocations == pytest.approx(
        {"momentum": 0.45, "mean_reversion": 0.35, "strategic_cash": 0.20}
    )
    assert decision.metadata["cash_components"] == pytest.approx(
        {
            "strategic_cash": 0.20,
            "unused_momentum_capital": 0.0,
            "unused_mean_reversion_capital": 0.175,
        }
    )
    assert sum(decision.weights.values()) + decision.cash_weight == pytest.approx(1.0)


def test_static_allocator_always_uses_fifty_fifty_strategy_capital() -> None:
    regime, momentum, mean_reversion = _inputs()

    decision = StaticAllocator().allocate(regime, momentum, mean_reversion)

    assert decision.strategy_allocations == pytest.approx(
        {"momentum": 0.50, "mean_reversion": 0.50, "strategic_cash": 0.0}
    )
    assert decision.pre_risk_weights == pytest.approx({"A": 0.30, "B": 0.45})
    assert decision.pre_risk_cash == pytest.approx(0.25)
    assert decision.metadata["allocator"] == "static_blend"


def test_allocator_permits_an_all_cash_result() -> None:
    regime, _, _ = _inputs()
    empty_momentum = StrategyResult("momentum", regime.as_of_date, {}, {})
    empty_reversion = StrategyResult("mean_reversion", regime.as_of_date, {}, {})

    decision = AdaptiveAllocator().allocate(regime, empty_momentum, empty_reversion)

    assert decision.pre_risk_weights == {}
    assert decision.pre_risk_cash == 1.0


def test_allocator_rejects_mixed_as_of_dates() -> None:
    regime, momentum, mean_reversion = _inputs()
    stale = StrategyResult(
        name="momentum",
        as_of_date=regime.as_of_date - pd.Timedelta(days=1),
        weights=momentum.weights,
        metadata={},
    )

    with pytest.raises(ValueError, match="same as-of date"):
        AdaptiveAllocator().allocate(regime, stale, mean_reversion)
