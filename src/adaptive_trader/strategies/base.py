"""Common causal strategy interface and input normalization helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from adaptive_trader.features import calculate_returns, slice_as_of, validate_price_frame
from adaptive_trader.models import StrategyResult


class BaseStrategy(ABC):
    """Abstract interface implemented by every paper-only allocation strategy."""

    name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"

    @abstractmethod
    def generate(
        self,
        prices: pd.DataFrame,
        returns: pd.DataFrame | object | None = None,
        as_of_date: object | None = None,
    ) -> StrategyResult:
        """Generate target risky-asset weights using information available as of a date."""

    def generate_signals(
        self,
        prices: pd.DataFrame,
        returns: pd.DataFrame | object | None = None,
        as_of_date: object | None = None,
    ) -> StrategyResult:
        """Generate signals (compatibility alias for :meth:`generate`)."""

        return self.generate(prices=prices, returns=returns, as_of_date=as_of_date)


Strategy = BaseStrategy
"""Concise alias for the common strategy interface."""


def _causal_inputs(
    prices: pd.DataFrame,
    returns: pd.DataFrame | object | None,
    as_of_date: object | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Normalize flexible call forms and enforce the strategy anti-look-ahead boundary."""

    # Also support generate(prices, as_of_date) for callers that derive returns internally.
    if as_of_date is None and returns is not None and not isinstance(returns, pd.DataFrame):
        as_of_date = returns
        returns = None

    validate_price_frame(prices)
    price_history = slice_as_of(prices, as_of_date)
    # Record the actual last completed observation, not a requested weekend or
    # holiday that has no bar. This makes the signal cutoff auditable.
    signal_date = price_history.index[-1]
    if pd.isna(signal_date):
        raise ValueError(f"Invalid as-of date: {as_of_date!r}")

    if returns is None:
        return_history = calculate_returns(price_history)
    else:
        if not isinstance(returns, pd.DataFrame):
            raise TypeError("returns must be a pandas DataFrame when supplied")
        return_history = slice_as_of(returns, signal_date)
        if return_history.index.has_duplicates or not return_history.index.is_monotonic_increasing:
            raise ValueError("returns must have unique, increasing dates")
        missing_columns = set(price_history.columns).difference(return_history.columns)
        if missing_columns:
            raise ValueError(f"returns are missing price ticker columns: {sorted(missing_columns)}")
        return_history = return_history.reindex(columns=price_history.columns).apply(
            pd.to_numeric, errors="coerce"
        )
        if not return_history.index.equals(price_history.index):
            raise ValueError("returns dates must align exactly with completed price dates")
        valid_or_missing = np.isfinite(return_history) | return_history.isna()
        if not valid_or_missing.to_numpy().all():
            raise ValueError("returns must be finite when present")
    return price_history, return_history, signal_date


def _config_section(config: Any, section_name: str) -> tuple[Any, int]:
    """Extract a strategy section and annualization factor from full or section config."""

    if hasattr(config, section_name):
        section = getattr(config, section_name)
        annualization_factor = int(
            getattr(getattr(config, "backtest", None), "annualization_factor", 252)
        )
        return section, annualization_factor
    return config, 252
