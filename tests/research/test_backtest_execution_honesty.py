"""Focused counterfactuals for backtest cost and execution-time honesty."""

from __future__ import annotations

import pandas as pd
import pytest

from adaptive_trader.backtest import run_backtest_suite
from adaptive_trader.config import AppConfig
from adaptive_trader.data import MarketData, required_history_from_config


def _short_history(
    config: AppConfig,
    market_data: MarketData,
    extra_sessions: int = 5,
    *,
    explicit_opens: bool = False,
) -> MarketData:
    observations = required_history_from_config(config) + extra_sessions
    assert market_data.opens is not None
    return MarketData(
        prices=market_data.prices.iloc[:observations],
        volumes=market_data.volumes.iloc[:observations],
        opens=market_data.opens.iloc[:observations],
        source=market_data.source,
        feed=market_data.feed,
        adjustment=market_data.adjustment,
        open_prices_are_approximated=(
            False if explicit_opens else market_data.open_prices_are_approximated
        ),
    )


def _replace_market_data(
    source: MarketData,
    *,
    prices: pd.DataFrame | None = None,
    opens: pd.DataFrame | None = None,
) -> MarketData:
    assert source.opens is not None
    return MarketData(
        prices=source.prices if prices is None else prices,
        volumes=source.volumes,
        opens=source.opens if opens is None else opens,
        source=source.source,
        feed=source.feed,
        adjustment=source.adjustment,
        open_prices_are_approximated=source.open_prices_are_approximated,
    )


def test_nonzero_slippage_reduces_wealth_with_transaction_costs_fixed(
    fast_config: AppConfig,
    fast_market_data: MarketData,
) -> None:
    no_slippage_values = fast_config.to_dict()
    no_slippage_values["backtest"].update({"transaction_cost_bps": 7.0, "slippage_bps": 0.0})
    with_slippage_values = fast_config.to_dict()
    with_slippage_values["backtest"].update({"transaction_cost_bps": 7.0, "slippage_bps": 35.0})
    no_slippage_config = AppConfig.from_dict(no_slippage_values)
    with_slippage_config = AppConfig.from_dict(with_slippage_values)
    market_data = _short_history(fast_config, fast_market_data, explicit_opens=True)

    no_slippage = run_backtest_suite(no_slippage_config, market_data)
    with_slippage = run_backtest_suite(with_slippage_config, market_data)
    baseline = no_slippage.runs["benchmark_buy_hold"].daily
    penalized = with_slippage.runs["benchmark_buy_hold"].daily

    assert baseline["transaction_cost"].sum() == pytest.approx(penalized["transaction_cost"].sum())
    assert baseline["slippage"].sum() == 0.0
    assert penalized["slippage"].sum() > 0.0
    assert penalized["equity"].iloc[-1] < baseline["equity"].iloc[-1]


def test_session_t_signal_excludes_t_close_and_execution_uses_t_open(
    fast_config: AppConfig,
    fast_market_data: MarketData,
) -> None:
    market_data = _short_history(fast_config, fast_market_data, explicit_opens=True)
    baseline = run_backtest_suite(fast_config, market_data)
    baseline_run = baseline.runs["adaptive"]
    baseline_receipt = baseline_run.decision_receipts[0]
    execution_date = pd.Timestamp(baseline_receipt["execution_date"])
    execution_location = market_data.prices.index.get_loc(execution_date)
    assert isinstance(execution_location, int) and execution_location > 0
    signal_date = market_data.prices.index[execution_location - 1]

    shocked_closes = market_data.prices.copy()
    shocked_closes.loc[execution_date] *= 1.07
    close_shock = run_backtest_suite(
        fast_config,
        _replace_market_data(market_data, prices=shocked_closes),
    )

    # The session-t close is realized only after the decision, so changing it
    # cannot change the receipt generated from history through t-1.
    assert close_shock.runs["adaptive"].decision_receipts[0] == baseline_receipt

    assert market_data.opens is not None
    changed_opens = market_data.opens.copy()
    changed_opens.loc[execution_date] *= 0.97
    open_shock = run_backtest_suite(
        fast_config,
        _replace_market_data(market_data, opens=changed_opens),
    )
    open_run = open_shock.runs["adaptive"]
    open_receipt = open_run.decision_receipts[0]
    weights = {
        symbol: float(weight) for symbol, weight in open_receipt["risk_adjusted_weights"].items()
    }
    expected_intraday_return = sum(
        weights.get(symbol, 0.0)
        * (
            market_data.prices.loc[execution_date, symbol]
            / changed_opens.loc[execution_date, symbol]
            - 1.0
        )
        for symbol in fast_config.data.tickers
    )
    actual_intraday_return = float(
        open_run.daily.loc[execution_date, "intraday_return_before_cost"]
    )

    # Changing the modeled execution open must not alter the completed-history
    # signal, regime, allocation, or independent risk target.  It should alter
    # only execution-time evidence such as intent reference prices and realized
    # intraday return.
    for field in (
        "signal_as_of_date",
        "historical_data_cutoff",
        "regime",
        "regime_features",
        "momentum",
        "mean_reversion",
        "strategy_allocations",
        "allocation_metadata",
        "proposed_asset_weights",
        "risk_adjusted_weights",
        "risk_interventions",
    ):
        assert open_receipt[field] == baseline_receipt[field]
    baseline_intents = {
        str(intent["symbol"]): intent for intent in baseline_receipt["order_intents"]
    }
    for intent in open_receipt["order_intents"]:
        symbol = str(intent["symbol"])
        assert float(intent["reference_adjusted_open"]) == pytest.approx(
            0.97 * float(baseline_intents[symbol]["reference_adjusted_open"])
        )
    assert pd.Timestamp(open_receipt["historical_data_cutoff"]) == signal_date
    assert pd.Timestamp(open_receipt["signal_as_of_date"]) == signal_date
    assert open_receipt["execution_timing"] == "session_open_using_adjusted_daily_open"
    assert actual_intraday_return == pytest.approx(expected_intraday_return)
    assert actual_intraday_return != pytest.approx(
        float(baseline_run.daily.loc[execution_date, "intraday_return_before_cost"])
    )
