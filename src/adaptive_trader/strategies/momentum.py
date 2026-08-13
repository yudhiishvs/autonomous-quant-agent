"""Cross-sectional trailing-return momentum strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from adaptive_trader.features import (
    annualized_volatility,
    inverse_volatility_weights,
    trailing_returns,
)
from adaptive_trader.models import StrategyResult
from adaptive_trader.strategies.base import BaseStrategy, _causal_inputs, _config_section

if TYPE_CHECKING:
    from adaptive_trader.config import AppConfig, MomentumConfig


class MomentumStrategy(BaseStrategy):
    """Select positive trailing-return leaders and weight them by inverse volatility."""

    name = "momentum"

    def __init__(
        self,
        config: MomentumConfig | AppConfig | None = None,
        *,
        annualization_factor: int | None = None,
    ) -> None:
        """Initialize the strategy from a momentum section or complete app config."""

        if config is None:
            from adaptive_trader.config import MomentumConfig

            config = MomentumConfig()
        section, configured_annualization = _config_section(config, "momentum")
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
        """Generate a causal inverse-volatility momentum portfolio as of a date."""

        price_history, return_history, signal_date = _causal_inputs(prices, returns, as_of_date)
        raw_scores = trailing_returns(price_history, self.config.lookback_days)
        raw_volatilities = annualized_volatility(
            return_history,
            self.config.volatility_lookback_days,
            self.annualization_factor,
        )
        assert isinstance(raw_volatilities, pd.Series)

        scores: dict[str, float] = {}
        valid_volatilities: dict[str, float] = {}
        exclusions: dict[str, str] = {}
        eligible: list[str] = []

        for column in price_history.columns:
            ticker = str(column)
            score = float(raw_scores.get(ticker, np.nan))
            if not np.isfinite(score):
                exclusions[ticker] = "insufficient_or_invalid_return_history"
                continue
            scores[ticker] = score
            if self.config.require_positive_return and score <= 0.0:
                exclusions[ticker] = "nonpositive_trailing_return"
                continue
            volatility = float(raw_volatilities.get(ticker, np.nan))
            if not np.isfinite(volatility) or volatility <= 0.0:
                exclusions[ticker] = "zero_or_invalid_volatility"
                continue
            valid_volatilities[ticker] = volatility
            eligible.append(ticker)

        eligible.sort(key=lambda ticker: (-scores[ticker], ticker))
        selected = eligible[: self.config.top_n]
        for ticker in eligible[self.config.top_n :]:
            exclusions[ticker] = "rank_below_top_n"
        selected_volatilities = {ticker: valid_volatilities[ticker] for ticker in selected}
        weights = inverse_volatility_weights(selected_volatilities)
        momentum_window = price_history.index[-(self.config.lookback_days + 1) :]
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
            "lookback_days": self.config.lookback_days,
            "lookback_start": momentum_window[0].isoformat() if len(momentum_window) else None,
            "lookback_end": momentum_window[-1].isoformat() if len(momentum_window) else None,
            "volatility_lookback_days": self.config.volatility_lookback_days,
            "volatility_lookback_start": (
                volatility_window[0].isoformat() if len(volatility_window) else None
            ),
            "volatility_lookback_end": (
                volatility_window[-1].isoformat() if len(volatility_window) else None
            ),
            "require_positive_return": self.config.require_positive_return,
            "scores": scores,
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
