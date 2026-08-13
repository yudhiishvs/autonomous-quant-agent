"""Transparent four-state trend and realized-volatility regime detector."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import numpy as np
import pandas as pd

from adaptive_trader.features import (
    calculate_returns,
    rolling_annualized_volatility,
    slice_as_of,
    validate_price_frame,
)
from adaptive_trader.models import RegimeState

if TYPE_CHECKING:
    from adaptive_trader.config import AppConfig, RegimeConfig


class RegimeDetector:
    """Classify benchmark history into bull/bear and high/low-volatility states."""

    def __init__(
        self,
        config: RegimeConfig | AppConfig | None = None,
        *,
        annualization_factor: int | None = None,
    ) -> None:
        """Initialize the detector from a regime section or complete app config."""

        if config is None:
            from adaptive_trader.config import RegimeConfig

            config = RegimeConfig()
        if hasattr(config, "regime"):
            root_config = cast("AppConfig", config)
            self.config = root_config.regime
            configured_annualization = int(root_config.backtest.annualization_factor)
        else:
            self.config = config
            configured_annualization = 252
        self.annualization_factor = (
            configured_annualization if annualization_factor is None else annualization_factor
        )
        if self.annualization_factor <= 0:
            raise ValueError("annualization_factor must be positive")

    def detect(
        self,
        prices: pd.DataFrame,
        returns: pd.DataFrame | object | None = None,
        as_of_date: object | None = None,
    ) -> RegimeState:
        """Detect the regime using only observations on or before ``as_of_date``."""

        # Support detect(prices, as_of_date) while keeping the explicit three-input API.
        if as_of_date is None and returns is not None and not isinstance(returns, pd.DataFrame):
            as_of_date = returns
            returns = None
        validate_price_frame(prices)
        price_history = slice_as_of(prices, as_of_date)
        signal_date = price_history.index[-1] if as_of_date is None else pd.Timestamp(as_of_date)
        if self.config.benchmark not in price_history.columns:
            raise ValueError(
                f"Configured regime benchmark {self.config.benchmark!r} is absent from prices"
            )
        benchmark_prices = pd.to_numeric(price_history[self.config.benchmark], errors="coerce")
        slow_window = benchmark_prices.iloc[-self.config.slow_moving_average_days :]
        if len(slow_window) < self.config.slow_moving_average_days:
            raise ValueError(
                "Insufficient benchmark history for regime slow moving average: "
                f"need {self.config.slow_moving_average_days}, have {len(slow_window)}"
            )
        if slow_window.isna().any() or not np.isfinite(slow_window).all():
            raise ValueError("Benchmark moving-average window contains missing/invalid prices")
        fast_window = benchmark_prices.iloc[-self.config.fast_moving_average_days :]
        fast_average = float(fast_window.mean())
        slow_average = float(slow_window.mean())

        if returns is None:
            benchmark_returns = calculate_returns(price_history)[self.config.benchmark]
        else:
            if not isinstance(returns, pd.DataFrame):
                raise TypeError("returns must be a pandas DataFrame when supplied")
            return_history = slice_as_of(returns, signal_date)
            if self.config.benchmark not in return_history.columns:
                raise ValueError(
                    f"Configured benchmark {self.config.benchmark!r} is absent from returns"
                )
            benchmark_returns = pd.to_numeric(
                return_history[self.config.benchmark], errors="coerce"
            )
            invalid = benchmark_returns.notna() & ~np.isfinite(benchmark_returns)
            if invalid.any():
                raise ValueError("Benchmark returns contain nonfinite values")

        realized_series = rolling_annualized_volatility(
            benchmark_returns,
            self.config.volatility_lookback_days,
            self.annualization_factor,
        )
        if realized_series.empty or not np.isfinite(realized_series.iloc[-1]):
            raise ValueError(
                "Insufficient benchmark returns for realized-volatility regime feature"
            )
        threshold_window = realized_series.dropna().iloc[
            -self.config.volatility_threshold_lookback_days :
        ]
        if len(threshold_window) < self.config.volatility_threshold_lookback_days:
            raise ValueError(
                "Insufficient benchmark history for the configured trailing volatility threshold: "
                f"need {self.config.volatility_threshold_lookback_days}, "
                f"have {len(threshold_window)}"
            )
        current_volatility = float(realized_series.iloc[-1])
        volatility_threshold = float(threshold_window.median())
        if not np.isfinite(current_volatility) or not np.isfinite(volatility_threshold):
            raise ValueError("Regime volatility features must be finite")

        trend = "bull" if fast_average >= slow_average else "bear"
        volatility_class = "high_vol" if current_volatility > volatility_threshold else "low_vol"
        name = f"{trend}_{volatility_class}"
        metadata = {
            "as_of_date": signal_date.isoformat(),
            "benchmark": self.config.benchmark,
            "fast_moving_average": fast_average,
            "slow_moving_average": slow_average,
            "fast_moving_average_days": self.config.fast_moving_average_days,
            "slow_moving_average_days": self.config.slow_moving_average_days,
            "realized_volatility": current_volatility,
            "volatility_threshold": volatility_threshold,
            "volatility_lookback_days": self.config.volatility_lookback_days,
            "volatility_threshold_lookback_days": (self.config.volatility_threshold_lookback_days),
            "threshold_observations": len(threshold_window),
            "trend_classification": trend,
            "volatility_classification": volatility_class,
            "classification": name,
            "data_sufficiency_warnings": [],
        }
        return RegimeState(name=name, as_of_date=signal_date, metadata=metadata)

    def detect_regime(
        self,
        prices: pd.DataFrame,
        returns: pd.DataFrame | object | None = None,
        as_of_date: object | None = None,
    ) -> RegimeState:
        """Detect a regime (compatibility alias for :meth:`detect`)."""

        return self.detect(prices=prices, returns=returns, as_of_date=as_of_date)


MarketRegimeDetector = RegimeDetector
"""Descriptive compatibility alias for :class:`RegimeDetector`."""
