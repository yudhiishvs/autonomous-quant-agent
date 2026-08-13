"""Adaptive and static combination of independent strategy portfolios."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from adaptive_trader.models import RebalanceDecision, RegimeState, StrategyResult

if TYPE_CHECKING:
    from adaptive_trader.config import AppConfig, RegimeAllocation, RegimeConfig


def _allocation_dict(allocation: Mapping[str, float] | RegimeAllocation) -> dict[str, float]:
    required = ("momentum", "mean_reversion", "strategic_cash")
    missing = [key for key in required if key not in allocation]
    if missing:
        raise ValueError(f"Strategy allocation is missing fields: {missing}")
    values = {key: float(allocation[key]) for key in required}
    if any(value < 0.0 for value in values.values()):
        raise ValueError("Strategy allocation fractions cannot be negative")
    if abs(sum(values.values()) - 1.0) > 1e-9:
        raise ValueError("Strategy allocation fractions must sum to one")
    return values


def _strategy_weights(
    result: StrategyResult | Mapping[str, float], expected_name: str
) -> tuple[dict[str, float], dict[str, Any], Any]:
    if isinstance(result, StrategyResult):
        if result.name != expected_name:
            raise ValueError(
                f"Expected {expected_name!r} result; received strategy {result.name!r}"
            )
        return dict(result.weights), dict(result.metadata), result.as_of_date
    if isinstance(result, Mapping):
        weights = {str(ticker): float(weight) for ticker, weight in result.items()}
        if any(weight < 0.0 for weight in weights.values()):
            raise ValueError(f"{expected_name} weights cannot be negative")
        if sum(weights.values()) > 1.0 + 1e-9:
            raise ValueError(f"{expected_name} weights cannot exceed one")
        return weights, {}, None
    raise TypeError(f"{expected_name} must be a StrategyResult or weight mapping")


def _combine(
    regime: RegimeState | str,
    momentum_result: StrategyResult | Mapping[str, float],
    mean_reversion_result: StrategyResult | Mapping[str, float],
    allocation: Mapping[str, float] | RegimeAllocation,
    *,
    allocator_name: str,
) -> RebalanceDecision:
    regime_name = regime.name if isinstance(regime, RegimeState) else str(regime)
    if regime_name not in RegimeState.VALID_NAMES:
        raise ValueError(f"Unsupported regime: {regime_name!r}")
    fractions = _allocation_dict(allocation)
    momentum_weights, momentum_metadata, momentum_date = _strategy_weights(
        momentum_result, "momentum"
    )
    mean_reversion_weights, mean_reversion_metadata, mean_reversion_date = _strategy_weights(
        mean_reversion_result, "mean_reversion"
    )

    if isinstance(regime, RegimeState):
        as_of_date = regime.as_of_date
        regime_metadata = dict(regime.metadata)
    else:
        as_of_date = momentum_date or mean_reversion_date
        regime_metadata = {}
        if as_of_date is None:
            raise ValueError(
                "A RegimeState or dated StrategyResult is required to record the as-of date"
            )
    for result_date in (momentum_date, mean_reversion_date):
        if result_date is not None and result_date != as_of_date:
            raise ValueError("Regime and strategy results must share the same as-of date")

    tickers = sorted(set(momentum_weights) | set(mean_reversion_weights))
    combined: dict[str, float] = {}
    contributions: dict[str, dict[str, float]] = {}
    for ticker in tickers:
        momentum_contribution = fractions["momentum"] * momentum_weights.get(ticker, 0.0)
        mean_reversion_contribution = fractions["mean_reversion"] * mean_reversion_weights.get(
            ticker, 0.0
        )
        weight = momentum_contribution + mean_reversion_contribution
        if weight > 0.0:
            combined[ticker] = weight
        contributions[ticker] = {
            "momentum": momentum_contribution,
            "mean_reversion": mean_reversion_contribution,
        }

    gross = sum(combined.values())
    if gross > 1.0 + 1e-9:
        raise ValueError(f"Combined strategy exposure exceeds one: {gross}")
    cash = max(0.0, 1.0 - gross)
    momentum_unused_cash = fractions["momentum"] * (1.0 - sum(momentum_weights.values()))
    mean_reversion_unused_cash = fractions["mean_reversion"] * (
        1.0 - sum(mean_reversion_weights.values())
    )
    metadata = {
        "allocator": allocator_name,
        "regime_features": regime_metadata,
        "strategy_weights": {
            "momentum": momentum_weights,
            "mean_reversion": mean_reversion_weights,
        },
        "strategy_metadata": {
            "momentum": momentum_metadata,
            "mean_reversion": mean_reversion_metadata,
        },
        "asset_contributions": contributions,
        "cash_components": {
            "strategic_cash": fractions["strategic_cash"],
            "unused_momentum_capital": momentum_unused_cash,
            "unused_mean_reversion_capital": mean_reversion_unused_cash,
        },
        "pre_risk_gross_exposure": gross,
        "pre_risk_cash_weight": cash,
    }
    return RebalanceDecision(
        as_of_date=as_of_date,
        regime=regime_name,
        strategy_allocations=fractions,
        pre_risk_weights=combined,
        pre_risk_cash=cash,
        metadata=metadata,
    )


class AdaptiveAllocator:
    """Combine strategy portfolios using the allocation for the detected regime."""

    def __init__(
        self,
        config: RegimeConfig | AppConfig | Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize from a full config, regime config, or regime-allocation mapping."""

        if config is None:
            from adaptive_trader.config import RegimeConfig

            config = RegimeConfig()
        if hasattr(config, "regime"):
            self.allocations = config.regime.allocations
        elif hasattr(config, "allocations"):
            self.allocations = config.allocations
        elif isinstance(config, Mapping):
            self.allocations = config
        else:
            raise TypeError("AdaptiveAllocator requires regime allocation configuration")

    def allocate(
        self,
        regime: RegimeState | str,
        momentum_result: StrategyResult | Mapping[str, float],
        mean_reversion_result: StrategyResult | Mapping[str, float],
    ) -> RebalanceDecision:
        """Return the regime-weighted pre-risk portfolio and explicit cash balance."""

        regime_name = regime.name if isinstance(regime, RegimeState) else str(regime)
        try:
            allocation = self.allocations[regime_name]
        except KeyError as exc:
            raise ValueError(f"No allocation is configured for regime {regime_name!r}") from exc
        return _combine(
            regime,
            momentum_result,
            mean_reversion_result,
            allocation,
            allocator_name="adaptive",
        )

    def combine(
        self,
        momentum_result: StrategyResult | Mapping[str, float],
        mean_reversion_result: StrategyResult | Mapping[str, float],
        regime: RegimeState | str,
    ) -> RebalanceDecision:
        """Combine results using result-first ordering (compatibility alias)."""

        return self.allocate(regime, momentum_result, mean_reversion_result)


class StaticAllocator:
    """Combine the same strategies with a constant 50/50 capital allocation."""

    allocation: ClassVar[dict[str, float]] = {
        "momentum": 0.5,
        "mean_reversion": 0.5,
        "strategic_cash": 0.0,
    }

    def __init__(self, config: object | None = None) -> None:
        """Initialize the static allocator; ``config`` is accepted for API symmetry."""

        self.config = config

    def allocate(
        self,
        regime: RegimeState | str,
        momentum_result: StrategyResult | Mapping[str, float],
        mean_reversion_result: StrategyResult | Mapping[str, float],
    ) -> RebalanceDecision:
        """Return a constant 50/50 pre-risk blend with all unused capital in cash."""

        return _combine(
            regime,
            momentum_result,
            mean_reversion_result,
            self.allocation,
            allocator_name="static_blend",
        )

    def combine(
        self,
        momentum_result: StrategyResult | Mapping[str, float],
        mean_reversion_result: StrategyResult | Mapping[str, float],
        regime: RegimeState | str,
    ) -> RebalanceDecision:
        """Combine results using result-first ordering (compatibility alias)."""

        return self.allocate(regime, momentum_result, mean_reversion_result)


StaticBlendAllocator = StaticAllocator
"""Descriptive compatibility alias for :class:`StaticAllocator`."""
