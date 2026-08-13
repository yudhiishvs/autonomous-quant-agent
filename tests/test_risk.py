"""Tests for the independent portfolio risk engine."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.config import RiskConfig
from adaptive_trader.risk import RiskEngine, calculate_turnover


def _returns(columns: tuple[str, ...] = ("A", "B"), rows: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(20260808)
    values = rng.normal(0.0002, 0.01, size=(rows, len(columns)))
    return pd.DataFrame(values, columns=list(columns))


def _config(**overrides: float | int) -> RiskConfig:
    base = RiskConfig(
        max_position_weight=1.0,
        max_gross_exposure=1.0,
        target_annual_volatility=10.0,
        covariance_lookback_days=60,
        max_turnover_per_rebalance=1.0,
        drawdown_soft_limit=0.10,
        soft_limit_max_gross_exposure=0.50,
        drawdown_hard_limit=0.15,
    )
    return replace(base, **overrides)


def test_position_limit_is_enforced_without_redistribution() -> None:
    decision = RiskEngine(_config(max_position_weight=0.25)).evaluate(
        proposed_weights={"A": 0.80, "B": 0.10},
        current_weights={},
        historical_returns=_returns(),
        current_drawdown=0.0,
    )

    assert decision.final_weights == pytest.approx({"A": 0.25, "B": 0.10})
    assert decision.final_cash == pytest.approx(0.65)
    assert [action.control for action in decision.actions] == ["max_position_weight"]


def test_gross_exposure_is_scaled_down_and_never_up() -> None:
    decision = RiskEngine(_config(max_gross_exposure=0.60)).evaluate(
        proposed_weights={"A": 0.50, "B": 0.50},
        current_weights={},
        historical_returns=_returns(),
        current_drawdown=0.0,
    )

    assert sum(decision.final_weights.values()) == pytest.approx(0.60)
    assert decision.final_weights == pytest.approx({"A": 0.30, "B": 0.30})
    assert all(decision.final_weights[key] <= 0.50 for key in ("A", "B"))


def test_volatility_target_only_reduces_exposure() -> None:
    alternating = np.tile([0.04, -0.04], 40)
    returns = pd.DataFrame({"A": alternating})
    decision = RiskEngine(_config(target_annual_volatility=0.10)).evaluate(
        proposed_weights={"A": 0.80},
        current_weights={},
        historical_returns=returns,
        current_drawdown=0.0,
    )

    assert decision.estimated_volatility > 0.10
    assert 0.0 < decision.final_weights["A"] < 0.80
    assert "volatility_target" in [action.control for action in decision.actions]

    low_volatility = pd.DataFrame({"A": np.tile([0.0001, -0.0001], 40)})
    unchanged = RiskEngine(_config(target_annual_volatility=0.10)).evaluate(
        proposed_weights={"A": 0.80},
        current_weights={},
        historical_returns=low_volatility,
        current_drawdown=0.0,
    )
    assert unchanged.final_weights["A"] == pytest.approx(0.80)


def test_soft_drawdown_caps_total_risky_exposure() -> None:
    decision = RiskEngine(_config(soft_limit_max_gross_exposure=0.40)).evaluate(
        proposed_weights={"A": 0.50, "B": 0.30},
        current_weights={},
        historical_returns=_returns(),
        current_drawdown=-0.10,
    )

    assert sum(decision.final_weights.values()) == pytest.approx(0.40)
    assert decision.final_cash == pytest.approx(0.60)
    assert "soft_drawdown" in [action.control for action in decision.actions]


def test_hard_drawdown_moves_to_cash_latches_and_bypasses_turnover() -> None:
    decision = RiskEngine(_config(max_turnover_per_rebalance=0.05)).evaluate(
        proposed_weights={"A": 0.80},
        current_weights={"A": 0.80},
        historical_returns=_returns(("A",)),
        current_drawdown=-0.15,
        hard_stop_latched=False,
    )

    assert decision.final_weights == {"A": 0.0}
    assert decision.final_cash == 1.0
    assert decision.final_turnover == pytest.approx(0.80)
    assert decision.final_turnover > 0.05
    assert decision.hard_stop_latched is True
    assert decision.status == "stopped"
    assert [action.control for action in decision.actions][-1] == "hard_drawdown"


def test_existing_hard_stop_latch_cannot_reenter_after_drawdown_recovers() -> None:
    decision = RiskEngine(_config()).evaluate(
        proposed_weights={"A": 0.60},
        current_weights={},
        historical_returns=pd.DataFrame(),
        current_drawdown=0.0,
        hard_stop_latched=True,
    )

    assert decision.final_weights == {"A": 0.0}
    assert decision.final_cash == 1.0
    assert decision.hard_stop_latched is True


def test_turnover_includes_cash_and_interpolates_to_limit() -> None:
    decision = RiskEngine(_config(max_turnover_per_rebalance=0.10)).evaluate(
        proposed_weights={"A": 0.80},
        current_weights={"A": 0.50},
        historical_returns=_returns(("A",)),
        current_drawdown=0.0,
    )

    assert calculate_turnover({"A": 0.80}, {"A": 0.50}) == pytest.approx(0.30)
    assert decision.proposed_turnover == pytest.approx(0.30)
    assert decision.final_turnover == pytest.approx(0.10)
    assert decision.final_weights["A"] == pytest.approx(0.60)
    assert decision.final_cash == pytest.approx(0.40)


def test_turnover_for_complete_asset_rotation_is_one() -> None:
    assert calculate_turnover({"B": 1.0}, {"A": 1.0}) == pytest.approx(1.0)

    decision = RiskEngine(
        _config(max_turnover_per_rebalance=0.25, required_cash_buffer=0.0)
    ).evaluate(
        proposed_weights={"B": 1.0},
        current_weights={"A": 1.0},
        historical_returns=_returns(("B",)),
        current_drawdown=0.0,
    )
    assert decision.final_weights == pytest.approx({"A": 0.75, "B": 0.25})
    assert decision.final_turnover == pytest.approx(0.25)


def test_nonfinite_input_fails_safely_to_cash() -> None:
    decision = RiskEngine(_config()).evaluate(
        proposed_weights={"A": np.nan},
        current_weights={"A": 0.20},
        historical_returns=_returns(("A",)),
        current_drawdown=0.0,
    )

    assert decision.final_weights == {"A": 0.0}
    assert decision.final_cash == 1.0
    assert decision.status == "stopped"
    assert [action.control for action in decision.actions] == ["data_validation"]


def test_unusable_covariance_history_fails_safely_to_cash() -> None:
    decision = RiskEngine(_config()).evaluate(
        proposed_weights={"A": 0.50},
        current_weights={},
        historical_returns=pd.DataFrame({"A": [np.nan]}),
        current_drawdown=0.0,
    )

    assert decision.final_weights == {"A": 0.0}
    assert decision.final_cash == 1.0
    assert decision.status == "stopped"


def test_long_only_and_control_order_are_deterministic() -> None:
    decision = RiskEngine(
        _config(
            max_position_weight=0.50,
            max_gross_exposure=0.60,
            soft_limit_max_gross_exposure=0.40,
            max_turnover_per_rebalance=0.10,
        )
    ).evaluate(
        proposed_weights={"C": 0.80, "A": -0.20, "B": 0.80},
        current_weights={},
        historical_returns=_returns(("B", "C")),
        current_drawdown=-0.10,
    )

    assert [action.control for action in decision.actions] == [
        "long_only",
        "max_position_weight",
        "max_gross_exposure",
        "soft_drawdown",
        "turnover_limit",
    ]
    assert decision.final_turnover == pytest.approx(0.10)
    assert all(np.isfinite(value) for value in decision.final_weights.values())
