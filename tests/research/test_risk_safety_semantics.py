"""Focused tests for risk rejection, cash, and loss-latch semantics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.config import AppConfig, RiskConfig
from adaptive_trader.risk import RiskContext, RiskEngine


def _returns(columns: tuple[str, ...] = ("A", "B")) -> pd.DataFrame:
    rng = np.random.default_rng(9817)
    return pd.DataFrame(rng.normal(0.0, 0.01, size=(80, len(columns))), columns=columns)


def test_full_config_enforces_required_cash_buffer_without_scaling_up() -> None:
    values = AppConfig.from_dict({}).to_dict()
    values["data"].update({"tickers": ["SPY", "A", "B"], "benchmark": "SPY"})
    values["risk"].update(
        {
            "max_position_weight": 1.0,
            "target_annual_volatility": 10.0,
            "max_turnover_per_rebalance": 1.0,
        }
    )
    config = AppConfig.from_dict(values)

    decision = RiskEngine(config).evaluate(
        {"A": 0.60, "B": 0.40}, {}, _returns(), current_drawdown=0.0
    )

    assert sum(decision.final_weights.values()) == pytest.approx(0.98)
    assert decision.final_cash == pytest.approx(0.02)
    assert "required_cash_buffer" in [action.control for action in decision.actions]


def test_bare_risk_config_enforces_its_required_cash_buffer() -> None:
    config = RiskConfig(
        max_position_weight=1.0,
        target_annual_volatility=10.0,
        max_turnover_per_rebalance=1.0,
        required_cash_buffer=0.10,
    )

    decision = RiskEngine(config).evaluate(
        {"A": 0.60, "B": 0.40}, {}, _returns(), current_drawdown=0.0
    )

    assert sum(decision.final_weights.values()) == pytest.approx(0.90)
    assert decision.final_cash == pytest.approx(0.10)
    assert "required_cash_buffer" in [action.control for action in decision.actions]


def test_daily_loss_latch_blocks_increases_but_permits_reductions() -> None:
    config = RiskConfig(
        max_position_weight=1.0,
        target_annual_volatility=10.0,
        max_turnover_per_rebalance=1.0,
    )
    decision = RiskEngine(config).evaluate(
        proposed_weights={"A": 0.20, "B": 0.50},
        current_weights={"A": 0.50},
        historical_returns=_returns(),
        current_drawdown=0.0,
        current_daily_loss=-0.03,
    )

    assert decision.final_weights == pytest.approx({"A": 0.20, "B": 0.0})
    assert decision.daily_loss_latched is True
    assert decision.halt_state == "daily_loss"
    assert "daily_loss" in [action.control for action in decision.actions]


def test_invalid_data_can_reject_and_preserve_holdings_without_liquidation() -> None:
    decision = RiskEngine(RiskConfig()).evaluate(
        proposed_weights={"A": np.nan},
        current_weights={"A": 0.20},
        historical_returns=_returns(("A",)),
        current_drawdown=0.0,
        preserve_current_on_rejection=True,
    )

    assert decision.status == "rejected"
    assert decision.final_weights == {"A": 0.20}
    assert decision.final_turnover == 0.0
    assert decision.liquidation_authorized is False
    assert decision.rejection_reasons


def test_hard_stop_is_the_only_path_that_authorizes_liquidation() -> None:
    decision = RiskEngine(RiskConfig(max_turnover_per_rebalance=0.01)).evaluate(
        proposed_weights={"A": 0.50},
        current_weights={"A": 0.50},
        historical_returns=_returns(("A",)),
        current_drawdown=-0.15,
    )

    assert decision.status == "stopped"
    assert decision.final_weights == {"A": 0.0}
    assert decision.liquidation_authorized is True
    assert decision.hard_stop_latched is True


def test_persistent_halt_rejects_and_holds_current_portfolio() -> None:
    decision = RiskEngine(RiskConfig()).evaluate(
        proposed_weights={"B": 0.40},
        current_weights={"A": 0.30},
        historical_returns=_returns(),
        current_drawdown=0.0,
        halt_latched=True,
    )

    assert decision.status == "rejected"
    assert decision.final_weights == pytest.approx({"A": 0.30, "B": 0.0})
    assert decision.final_turnover == 0.0
    assert decision.liquidation_authorized is False


def test_proposed_turnover_reflects_raw_proposal_before_position_clipping() -> None:
    config = RiskConfig(
        max_position_weight=0.25,
        target_annual_volatility=10.0,
        max_turnover_per_rebalance=1.0,
    )
    decision = RiskEngine(config).evaluate({"A": 0.80}, {}, _returns(("A",)), current_drawdown=0.0)

    assert decision.proposed_turnover == pytest.approx(0.80)
    assert decision.final_turnover == pytest.approx(0.25)


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            RiskContext(
                account_equity=100_000,
                account_cash=80_000,
                position_weights={"A": 0.20},
                data_freshness_state="stale",
                market_state="open",
            ),
            "data freshness state",
        ),
        (
            RiskContext(
                account_equity=100_000,
                account_cash=80_000,
                position_weights={"A": 0.20},
                data_freshness_state="fresh",
                market_state="closed",
            ),
            "market state",
        ),
        (
            RiskContext(
                account_equity=100_000,
                account_cash=80_000,
                position_weights={"A": 0.20},
                open_order_symbols=("B",),
                data_freshness_state="fresh",
                market_state="open",
            ),
            "open conflicting orders",
        ),
    ],
)
def test_operational_risk_context_rejects_and_preserves_holdings(
    context: RiskContext,
    reason: str,
) -> None:
    decision = RiskEngine(RiskConfig()).evaluate(
        proposed_weights={"A": 0.20, "B": 0.20},
        current_weights={"A": 0.20},
        historical_returns=_returns(),
        current_drawdown=0.0,
        preserve_current_on_rejection=True,
        context=context,
    )

    assert decision.status == "rejected"
    assert decision.final_weights == {"A": 0.20, "B": 0.0}
    assert any(reason in item for item in decision.rejection_reasons)
    assert decision.data_freshness_state == context.data_freshness_state
    assert decision.market_state == context.market_state
    assert decision.evaluation_context["account_equity"] == 100_000
