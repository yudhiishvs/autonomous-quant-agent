"""Trend-filtered cross-sectional mean-reversion strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from adaptive_trader.features import (
    annualized_volatility,
    inverse_volatility_weights,
    latest_zscores,
)
from adaptive_trader.models import StrategyResult
from adaptive_trader.strategies.base import BaseStrategy, _causal_inputs, _config_section

if TYPE_CHECKING:
    from adaptive_trader.config import AppConfig, MeanReversionConfig


class MeanReversionStrategy(BaseStrategy):
    """Buy oversold assets only while they remain above their long-term trend."""

    name = "mean_reversion"

    def __init__(
        self,
        config: MeanReversionConfig | AppConfig | None = None,
        *,
        annualization_factor: int | None = None,
    ) -> None:
        """Initialize the strategy from a mean-reversion section or app config."""

        if config is None:
            from adaptive_trader.config import MeanReversionConfig

            config = MeanReversionConfig()
        section, configured_annualization = _config_section(config, "mean_reversion")
        self.config = section
        self.annualization_factor = (
            configured_annualization if annualization_factor is None else annualization_factor
        )
        if self.annualization_factor <= 0:
            raise ValueError("annualization_factor must be positive")

    def generate(
        self,
        prices: pd.DataFrame,
        returns: pd.DataFrame | object | None = None,
        as_of_date: object | None = None,
    ) -> StrategyResult:
        """Generate a causal inverse-volatility mean-reversion portfolio as of a date."""

        price_history, return_history, signal_date = _causal_inputs(prices, returns, as_of_date)
        raw_zscores = latest_zscores(price_history, self.config.zscore_lookback_days)
        raw_volatilities = annualized_volatility(
            return_history,
            self.config.volatility_lookback_days,
            self.annualization_factor,
        )
        assert isinstance(raw_volatilities, pd.Series)

        zscores: dict[str, float] = {}
        trend_averages: dict[str, float] = {}
        current_prices: dict[str, float] = {}
        valid_volatilities: dict[str, float] = {}
        exclusions: dict[str, str] = {}
        eligible: list[str] = []

        for column in price_history.columns:
            ticker = str(column)
            zscore = float(raw_zscores.get(ticker, np.nan))
            if not np.isfinite(zscore):
                exclusions[ticker] = "insufficient_or_invalid_zscore_history"
                continue
            zscores[ticker] = zscore
            if zscore >= self.config.entry_zscore:
                exclusions[ticker] = "zscore_above_entry_threshold"
                continue

            series = pd.to_numeric(price_history[column], errors="coerce")
            if len(series) < self.config.long_term_trend_days:
                exclusions[ticker] = "insufficient_long_term_trend_history"
                continue
            trend_window = series.iloc[-self.config.long_term_trend_days :]
            if trend_window.isna().any() or not np.isfinite(trend_window).all():
                exclusions[ticker] = "invalid_long_term_trend_history"
                continue
            current_price = float(trend_window.iloc[-1])
            trend_average = float(trend_window.mean())
            current_prices[ticker] = current_price
            trend_averages[ticker] = trend_average
            if current_price <= trend_average:
                exclusions[ticker] = "below_or_equal_long_term_trend"
                continue

            volatility = float(raw_volatilities.get(ticker, np.nan))
            if not np.isfinite(volatility) or volatility <= 0.0:
                exclusions[ticker] = "zero_or_invalid_volatility"
                continue
            valid_volatilities[ticker] = volatility
            eligible.append(ticker)

        eligible.sort(key=lambda ticker: (zscores[ticker], ticker))
        selected = eligible[: self.config.top_n]
        for ticker in eligible[self.config.top_n :]:
            exclusions[ticker] = "rank_below_top_n"
        selected_volatilities = {ticker: valid_volatilities[ticker] for ticker in selected}
        weights = inverse_volatility_weights(selected_volatilities)
        zscore_window = price_history.index[-self.config.zscore_lookback_days :]
        trend_window_dates = price_history.index[-self.config.long_term_trend_days :]
        volatility_window = return_history.index[-self.config.volatility_lookback_days :]
        warnings = sorted(
            {
                reason
                for reason in exclusions.values()
                if reason.startswith("insufficient") or "invalid" in reason or "zero" in reason
            }
        )
        metadata: dict[str, Any] = {
            "as_of_date": signal_date.isoformat(),
            "zscore_lookback_days": self.config.zscore_lookback_days,
            "zscore_lookback_start": zscore_window[0].isoformat() if len(zscore_window) else None,
            "zscore_lookback_end": zscore_window[-1].isoformat() if len(zscore_window) else None,
            "entry_zscore": self.config.entry_zscore,
            "long_term_trend_days": self.config.long_term_trend_days,
            "long_term_trend_start": (
                trend_window_dates[0].isoformat() if len(trend_window_dates) else None
            ),
            "long_term_trend_end": (
                trend_window_dates[-1].isoformat() if len(trend_window_dates) else None
            ),
            "volatility_lookback_days": self.config.volatility_lookback_days,
            "volatility_lookback_start": (
                volatility_window[0].isoformat() if len(volatility_window) else None
            ),
            "volatility_lookback_end": (
                volatility_window[-1].isoformat() if len(volatility_window) else None
            ),
            "scores": zscores,
            "zscores": zscores,
            "current_prices": current_prices,
            "long_term_moving_averages": trend_averages,
            "selected_assets": selected,
            "exclusions": exclusions,
            "annualized_volatility": selected_volatilities,
            "raw_weights": dict(weights),
            "final_weights": dict(weights),
            "cash_weight": max(0.0, 1.0 - sum(weights.values())),
            "warnings": warnings,
        }
        return StrategyResult(
            name=self.name,
            as_of_date=signal_date,
            weights=weights,
            metadata=metadata,
            version=self.version,
        )
