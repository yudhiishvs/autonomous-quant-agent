"""Pure canonical market-data contracts and transformations."""

from adaptive_trader.platform.data.aggregation import (
    AggregatedBar,
    AggregationError,
    EffectiveBar,
    SessionWindow,
    aggregate_one_minute_bars,
)
from adaptive_trader.platform.data.normalization import (
    CanonicalBar,
    MarketDataNormalizationError,
    NormalizationPolicy,
    normalize_alpaca_bar,
    normalize_fixture_bar,
)

__all__ = [
    "AggregatedBar",
    "AggregationError",
    "CanonicalBar",
    "EffectiveBar",
    "MarketDataNormalizationError",
    "NormalizationPolicy",
    "SessionWindow",
    "aggregate_one_minute_bars",
    "normalize_alpaca_bar",
    "normalize_fixture_bar",
]
