"""Perturbation test proving future prices cannot change earlier decisions."""

from __future__ import annotations

import numpy as np
import pandas as pd

from adaptive_trader.backtest import run_backtest_suite
from adaptive_trader.data import MarketData


def test_future_price_perturbation_does_not_change_prior_allocations(
    fast_config, fast_market_data
) -> None:
    cutoff = pd.Timestamp("2021-06-30")
    changed_prices = fast_market_data.prices.copy()
    future_mask = changed_prices.index > cutoff
    rng = np.random.default_rng(4402)
    future_returns = rng.normal(0.0002, 0.025, size=(future_mask.sum(), changed_prices.shape[1]))
    prior = changed_prices.loc[~future_mask].iloc[-1]
    changed_prices.loc[future_mask] = prior.to_numpy() * np.cumprod(1.0 + future_returns, axis=0)
    perturbed = MarketData(
        prices=changed_prices,
        volumes=fast_market_data.volumes,
        # Preserve run provenance so this test isolates only a future-price
        # perturbation; receipts intentionally include source/feed metadata.
        source=fast_market_data.source,
        feed=fast_market_data.feed,
        adjustment=fast_market_data.adjustment,
        open_prices_are_approximated=fast_market_data.open_prices_are_approximated,
    )

    original_result = run_backtest_suite(fast_config, fast_market_data)
    perturbed_result = run_backtest_suite(fast_config, perturbed)
    for name in original_result.runs:
        pd.testing.assert_frame_equal(
            original_result.runs[name].weights.loc[:cutoff],
            perturbed_result.runs[name].weights.loc[:cutoff],
        )
        original_receipts = [
            receipt
            for receipt in original_result.runs[name].decision_receipts
            if pd.Timestamp(receipt["execution_date"]) <= cutoff
        ]
        perturbed_receipts = [
            receipt
            for receipt in perturbed_result.runs[name].decision_receipts
            if pd.Timestamp(receipt["execution_date"]) <= cutoff
        ]
        assert original_receipts == perturbed_receipts
