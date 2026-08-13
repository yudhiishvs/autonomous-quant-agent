"""Causal price and return feature calculations shared by strategies."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite, sqrt
from numbers import Real
from typing import TypeVar

import numpy as np
import pandas as pd

FrameOrSeries = TypeVar("FrameOrSeries", pd.DataFrame, pd.Series)


def validate_price_frame(prices: pd.DataFrame) -> None:
    """Validate a wide price matrix without filling or rearranging observations."""

    if not isinstance(prices, pd.DataFrame):
        raise TypeError("prices must be a pandas DataFrame")
    if prices.empty:
        raise ValueError("prices must contain at least one observation")
    if not isinstance(prices.index, pd.DatetimeIndex):
        raise ValueError("prices must use a DatetimeIndex")
    if prices.index.has_duplicates:
        raise ValueError("prices must not contain duplicate dates")
    if not prices.index.is_monotonic_increasing:
        raise ValueError("prices must be ordered by increasing date")
    if prices.columns.has_duplicates:
        raise ValueError("prices must not contain duplicate ticker columns")
    if len(prices.columns) == 0:
        raise ValueError("prices must contain at least one ticker")
    numeric = prices.apply(pd.to_numeric, errors="coerce")
    supplied = prices.notna()
    invalid = supplied & ((numeric <= 0) | ~np.isfinite(numeric))
    if invalid.to_numpy().any():
        raise ValueError("nonmissing prices must be positive and finite")


def slice_as_of(data: FrameOrSeries, as_of_date: object | None) -> FrameOrSeries:
    """Return a copy containing observations no later than ``as_of_date``.

    The function is the central anti-look-ahead boundary used by strategies and the
    regime detector. An omitted date means the latest already-supplied observation.
    """

    if not isinstance(data, (pd.DataFrame, pd.Series)):
        raise TypeError("data must be a pandas DataFrame or Series")
    if data.empty:
        raise ValueError("data must contain at least one observation")
    if not isinstance(data.index, pd.DatetimeIndex):
        raise ValueError("data must use a DatetimeIndex")
    if data.index.has_duplicates:
        raise ValueError("data must not contain duplicate dates")
    if not data.index.is_monotonic_increasing:
        raise ValueError("data must be ordered by increasing date")
    if as_of_date is None:
        timestamp = data.index[-1]
    else:
        try:
            timestamp = pd.Timestamp(as_of_date)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid as-of date: {as_of_date!r}") from exc
        if pd.isna(timestamp):
            raise ValueError(f"Invalid as-of date: {as_of_date!r}")
    result = data.loc[data.index <= timestamp].copy()
    if result.empty:
        raise ValueError(f"No observations are available on or before {timestamp}")
    return result


def calculate_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Calculate simple close-to-close returns without implicit forward filling."""

    validate_price_frame(prices)
    numeric = prices.apply(pd.to_numeric, errors="coerce").astype(float)
    returns = numeric.pct_change(fill_method=None)
    finite_or_missing = np.isfinite(returns) | returns.isna()
    if not finite_or_missing.to_numpy().all():
        raise ValueError("price changes produced nonfinite returns")
    return returns


def trailing_returns(prices: pd.DataFrame, lookback_days: int) -> pd.Series:
    """Return exact ``lookback_days`` price changes for each sufficiently complete asset."""

    validate_price_frame(prices)
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days <= 0:
        raise ValueError("lookback_days must be a positive integer")
    scores: dict[str, float] = {}
    for ticker in prices.columns:
        series = pd.to_numeric(prices[ticker], errors="coerce")
        if len(series) < lookback_days + 1:
            scores[str(ticker)] = np.nan
            continue
        window = series.iloc[-(lookback_days + 1) :]
        if window.isna().any() or not np.isfinite(window).all() or (window <= 0).any():
            scores[str(ticker)] = np.nan
            continue
        score = float(window.iloc[-1] / window.iloc[0] - 1.0)
        scores[str(ticker)] = score if isfinite(score) else np.nan
    return pd.Series(scores, dtype=float, name="trailing_return")


def trailing_return(prices: pd.DataFrame, lookback_days: int) -> pd.Series:
    """Return exact trailing returns (singular-name compatibility alias)."""

    return trailing_returns(prices, lookback_days)


def annualized_volatility(
    returns: pd.DataFrame | pd.Series,
    window: int,
    annualization_factor: int = 252,
) -> pd.Series | float:
    """Estimate sample volatility from the final complete trailing-return window."""

    if not isinstance(returns, (pd.DataFrame, pd.Series)):
        raise TypeError("returns must be a pandas DataFrame or Series")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 1:
        raise ValueError("window must be an integer greater than one")
    if (
        isinstance(annualization_factor, bool)
        or not isinstance(annualization_factor, int)
        or annualization_factor <= 0
    ):
        raise ValueError("annualization_factor must be a positive integer")

    def estimate(series: pd.Series) -> float:
        numeric = pd.to_numeric(series, errors="coerce")
        if len(numeric) < window:
            return float("nan")
        sample = numeric.iloc[-window:]
        if sample.isna().any() or not np.isfinite(sample).all():
            return float("nan")
        value = float(sample.std(ddof=1) * sqrt(annualization_factor))
        return value if isfinite(value) and value > 0.0 else float("nan")

    if isinstance(returns, pd.Series):
        return estimate(returns)
    return pd.Series(
        {str(column): estimate(returns[column]) for column in returns.columns},
        dtype=float,
        name="annualized_volatility",
    )


def rolling_annualized_volatility(
    returns: pd.Series,
    window: int,
    annualization_factor: int = 252,
) -> pd.Series:
    """Calculate a backward-looking rolling annualized realized-volatility series."""

    if not isinstance(returns, pd.Series):
        raise TypeError("returns must be a pandas Series")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 1:
        raise ValueError("window must be an integer greater than one")
    if (
        isinstance(annualization_factor, bool)
        or not isinstance(annualization_factor, int)
        or annualization_factor <= 0
    ):
        raise ValueError("annualization_factor must be a positive integer")
    numeric = pd.to_numeric(returns, errors="coerce")
    result = numeric.rolling(window=window, min_periods=window).std(ddof=1)
    return result * sqrt(annualization_factor)


def latest_zscores(prices: pd.DataFrame, window: int) -> pd.Series:
    """Calculate each asset's latest z-score from an uncentered trailing price window."""

    validate_price_frame(prices)
    if isinstance(window, bool) or not isinstance(window, int) or window <= 1:
        raise ValueError("window must be an integer greater than one")
    values: dict[str, float] = {}
    for ticker in prices.columns:
        numeric = pd.to_numeric(prices[ticker], errors="coerce")
        if len(numeric) < window:
            values[str(ticker)] = np.nan
            continue
        sample = numeric.iloc[-window:]
        if sample.isna().any() or not np.isfinite(sample).all():
            values[str(ticker)] = np.nan
            continue
        standard_deviation = float(sample.std(ddof=1))
        if not isfinite(standard_deviation) or standard_deviation <= 0:
            values[str(ticker)] = np.nan
            continue
        zscore = float((sample.iloc[-1] - sample.mean()) / standard_deviation)
        values[str(ticker)] = zscore if isfinite(zscore) else np.nan
    return pd.Series(values, dtype=float, name="zscore")


def latest_zscore(prices: pd.DataFrame, window: int) -> pd.Series:
    """Calculate latest z-scores (singular-name compatibility alias)."""

    return latest_zscores(prices, window)


def inverse_volatility_weights(
    volatilities: Mapping[str, float] | pd.Series,
    *,
    maximum_exposure: float = 1.0,
) -> dict[str, float]:
    """Normalize positive finite inverse volatilities to the requested exposure cap."""

    if not isinstance(maximum_exposure, Real) or not isfinite(float(maximum_exposure)):
        raise ValueError("maximum_exposure must be finite")
    exposure = float(maximum_exposure)
    if not 0.0 <= exposure <= 1.0:
        raise ValueError("maximum_exposure must be between 0 and 1")
    inverse: dict[str, float] = {}
    for ticker, raw_volatility in volatilities.items():
        if not isinstance(raw_volatility, Real):
            continue
        volatility = float(raw_volatility)
        if isfinite(volatility) and volatility > 0.0:
            inverse[str(ticker)] = 1.0 / volatility
    denominator = sum(inverse.values())
    if not inverse or not isfinite(denominator) or denominator <= 0.0:
        return {}
    return {
        ticker: exposure * inverse_value / denominator for ticker, inverse_value in inverse.items()
    }


def estimate_annualized_volatility(
    returns: pd.DataFrame | pd.Series,
    window: int,
    annualization_factor: int = 252,
) -> pd.Series | float:
    """Return annualized volatility (descriptive-name compatibility alias)."""

    return annualized_volatility(returns, window, annualization_factor)
