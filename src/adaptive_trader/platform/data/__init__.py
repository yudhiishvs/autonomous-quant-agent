"""Pure canonical market-data contracts and transformations."""

from adaptive_trader.platform.data.aggregation import (
    AggregatedBar,
    AggregationError,
    EffectiveBar,
    SessionWindow,
    aggregate_one_minute_bars,
)
from adaptive_trader.platform.data.materialization import (
    AggregateStore,
    FifteenMinuteMaterializer,
    MaterializationReceipt,
)
from adaptive_trader.platform.data.normalization import (
    CanonicalBar,
    MarketDataNormalizationError,
    NormalizationPolicy,
    normalize_alpaca_bar,
    normalize_fixture_bar,
)

__all__ = [
    "AggregateStore",
    "AggregatedBar",
    "AggregationError",
    "CanonicalBar",
    "EffectiveBar",
    "FifteenMinuteMaterializer",
    "MarketDataNormalizationError",
    "MaterializationReceipt",
    "NormalizationPolicy",
    "SessionWindow",
    "aggregate_one_minute_bars",
    "normalize_alpaca_bar",
    "normalize_fixture_bar",
]
