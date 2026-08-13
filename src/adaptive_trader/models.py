"""Shared validated domain models for signals, decisions, metrics, and results."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import isfinite, isnan
from numbers import Real
from typing import Any, ClassVar

import numpy as np
import pandas as pd


def validate_finite(value: Any, location: str = "value") -> None:
    """Raise ``ValueError`` when a nested recorded numeric value is NaN or infinite."""

    if value is None or isinstance(value, (str, bytes, bool, pd.Timestamp)):
        return
    if isinstance(value, Real):
        if not isfinite(float(value)):
            raise ValueError(f"{location} must be finite; received {value!r}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            validate_finite(item, f"{location}.{key}")
        return
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            validate_finite(item, f"{location}[{index}]")
        return
    if (
        isinstance(value, np.ndarray)
        and np.issubdtype(value.dtype, np.number)
        and not np.isfinite(value).all()
    ):
        raise ValueError(f"{location} must contain only finite values")


def _timestamp(value: Any, location: str) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location} must be a valid date; received {value!r}") from exc
    if pd.isna(result):
        raise ValueError(f"{location} must be a valid date; received {value!r}")
    return result


def _weight_map(
    values: Mapping[str, float],
    location: str,
    *,
    require_nonnegative: bool,
) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise ValueError(f"{location} must be a ticker-to-weight mapping")
    normalized: dict[str, float] = {}
    for ticker, raw_weight in values.items():
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError(f"{location} contains an invalid ticker: {ticker!r}")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, Real):
            raise ValueError(f"{location}.{ticker} must be a finite number")
        weight = float(raw_weight)
        if not isfinite(weight):
            raise ValueError(f"{location}.{ticker} must be finite")
        if require_nonnegative and weight < -1e-12:
            raise ValueError(f"{location}.{ticker} cannot be negative")
        normalized[ticker] = max(0.0, weight) if require_nonnegative else weight
    return normalized


@dataclass(slots=True)
class StrategyResult:
    """A strategy's as-of-date risky weights and explanatory signal metadata."""

    name: str
    as_of_date: pd.Timestamp
    weights: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("StrategyResult.name must be a nonempty string")
        self.as_of_date = _timestamp(self.as_of_date, "StrategyResult.as_of_date")
        self.weights = _weight_map(self.weights, "StrategyResult.weights", require_nonnegative=True)
        gross = sum(self.weights.values())
        if gross > 1.0 + 1e-9:
            raise ValueError(f"StrategyResult risky weights cannot exceed 1; received {gross}")
        if not isinstance(self.metadata, dict):
            self.metadata = dict(self.metadata)
        validate_finite(self.metadata, "StrategyResult.metadata")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("StrategyResult.version must be a nonempty string")

    @property
    def cash_weight(self) -> float:
        """Return the strategy capital not assigned to risky assets."""

        return max(0.0, 1.0 - sum(self.weights.values()))

    @property
    def strategy_name(self) -> str:
        """Return the strategy name (compatibility alias)."""

        return self.name

    @property
    def asset_weights(self) -> dict[str, float]:
        """Return a copy of risky weights (compatibility alias)."""

        return dict(self.weights)


@dataclass(slots=True)
class RegimeState:
    """One transparent market-regime classification and its input features."""

    VALID_NAMES: ClassVar[frozenset[str]] = frozenset(
        {"bull_low_vol", "bull_high_vol", "bear_low_vol", "bear_high_vol"}
    )

    name: str
    as_of_date: pd.Timestamp
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in self.VALID_NAMES:
            raise ValueError(
                f"RegimeState.name must be one of {sorted(self.VALID_NAMES)}; "
                f"received {self.name!r}"
            )
        self.as_of_date = _timestamp(self.as_of_date, "RegimeState.as_of_date")
        if not isinstance(self.metadata, dict):
            self.metadata = dict(self.metadata)
        validate_finite(self.metadata, "RegimeState.metadata")

    @property
    def regime(self) -> str:
        """Return the regime name (compatibility alias)."""

        return self.name


@dataclass(slots=True)
class RiskAction:
    """An auditable record of one risk control that changed a proposal."""

    control: str
    description: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.control, str) or not self.control.strip():
            raise ValueError("RiskAction.control must be a nonempty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("RiskAction.description must be a nonempty string")
        if not isinstance(self.details, dict):
            self.details = dict(self.details)
        validate_finite(self.details, "RiskAction.details")

    def as_dict(self) -> dict[str, Any]:
        """Return a serialization-ready action record."""

        return asdict(self)


@dataclass(slots=True)
class RiskDecision:
    """Complete before-and-after record produced by the independent risk engine."""

    proposed_weights: dict[str, float]
    final_weights: dict[str, float]
    proposed_cash: float
    final_cash: float
    estimated_volatility: float | None
    proposed_turnover: float
    final_turnover: float
    current_drawdown: float
    hard_stop_latched: bool
    actions: list[RiskAction] = field(default_factory=list)
    status: str = "approved"
    decision_id: str | None = None
    proposed_gross_exposure: float | None = None
    final_gross_exposure: float | None = None
    final_estimated_volatility: float | None = None
    current_daily_loss: float = 0.0
    daily_loss_latched: bool = False
    halt_state: str = "clear"
    data_freshness_state: str = "not_evaluated"
    market_state: str = "not_evaluated"
    liquidation_authorized: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    evaluation_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.proposed_weights = _weight_map(
            self.proposed_weights,
            "RiskDecision.proposed_weights",
            require_nonnegative=False,
        )
        self.final_weights = _weight_map(
            self.final_weights, "RiskDecision.final_weights", require_nonnegative=True
        )
        for name in (
            "proposed_cash",
            "final_cash",
            "proposed_turnover",
            "final_turnover",
            "current_drawdown",
        ):
            validate_finite(getattr(self, name), f"RiskDecision.{name}")
        if self.estimated_volatility is not None:
            validate_finite(self.estimated_volatility, "RiskDecision.estimated_volatility")
            if self.estimated_volatility < 0:
                raise ValueError("RiskDecision.estimated_volatility cannot be negative")
        if self.final_estimated_volatility is not None:
            validate_finite(
                self.final_estimated_volatility,
                "RiskDecision.final_estimated_volatility",
            )
            if self.final_estimated_volatility < 0:
                raise ValueError("RiskDecision.final_estimated_volatility cannot be negative")
        if self.proposed_turnover < 0 or self.final_turnover < 0:
            raise ValueError("RiskDecision turnover values cannot be negative")
        if self.final_cash < -1e-9 or self.final_cash > 1.0 + 1e-9:
            raise ValueError("RiskDecision.final_cash must be between 0 and 1")
        if not isinstance(self.hard_stop_latched, bool):
            raise ValueError("RiskDecision.hard_stop_latched must be boolean")
        if self.status not in {"approved", "modified", "rejected", "stopped"}:
            raise ValueError("RiskDecision.status must be approved, modified, rejected, or stopped")
        if not all(isinstance(action, RiskAction) for action in self.actions):
            raise ValueError("RiskDecision.actions must contain RiskAction records")
        if self.decision_id is not None and (
            not isinstance(self.decision_id, str) or not self.decision_id.strip()
        ):
            raise ValueError("RiskDecision.decision_id must be a nonempty string when supplied")
        proposed_gross = float(sum(self.proposed_weights.values()))
        final_gross = float(sum(self.final_weights.values()))
        if self.proposed_gross_exposure is None:
            self.proposed_gross_exposure = proposed_gross
        if self.final_gross_exposure is None:
            self.final_gross_exposure = final_gross
        validate_finite(self.proposed_gross_exposure, "RiskDecision.proposed_gross_exposure")
        validate_finite(self.final_gross_exposure, "RiskDecision.final_gross_exposure")
        if abs(self.proposed_gross_exposure - proposed_gross) > 1e-8:
            raise ValueError("RiskDecision.proposed_gross_exposure does not match weights")
        if abs(self.final_gross_exposure - final_gross) > 1e-8:
            raise ValueError("RiskDecision.final_gross_exposure does not match weights")
        if abs(final_gross + self.final_cash - 1.0) > 1e-8:
            raise ValueError("RiskDecision final risky weights and cash must sum to one")
        validate_finite(self.current_daily_loss, "RiskDecision.current_daily_loss")
        if not isinstance(self.daily_loss_latched, bool):
            raise ValueError("RiskDecision.daily_loss_latched must be boolean")
        if not isinstance(self.liquidation_authorized, bool):
            raise ValueError("RiskDecision.liquidation_authorized must be boolean")
        if self.liquidation_authorized and not self.hard_stop_latched:
            raise ValueError("Only a hard-stop decision may authorize liquidation")
        if not all(isinstance(reason, str) and reason for reason in self.rejection_reasons):
            raise ValueError("RiskDecision.rejection_reasons must contain nonempty strings")
        if not isinstance(self.evaluation_context, dict):
            self.evaluation_context = dict(self.evaluation_context)
        validate_finite(self.evaluation_context, "RiskDecision.evaluation_context")

    @property
    def approved(self) -> bool:
        """Whether the risk engine approved the proposal without modification."""

        return self.status == "approved"


@dataclass(slots=True)
class RebalanceDecision:
    """Pre-risk allocation record for one signal as-of date."""

    as_of_date: pd.Timestamp
    regime: str
    strategy_allocations: dict[str, float]
    pre_risk_weights: dict[str, float]
    pre_risk_cash: float
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_date: pd.Timestamp | None = None
    risk_decision: RiskDecision | None = None

    def __post_init__(self) -> None:
        self.as_of_date = _timestamp(self.as_of_date, "RebalanceDecision.as_of_date")
        if self.regime not in RegimeState.VALID_NAMES:
            raise ValueError(f"Unsupported rebalance regime: {self.regime!r}")
        self.strategy_allocations = _weight_map(
            self.strategy_allocations,
            "RebalanceDecision.strategy_allocations",
            require_nonnegative=True,
        )
        if sum(self.strategy_allocations.values()) > 1.0 + 1e-9:
            raise ValueError("RebalanceDecision.strategy_allocations cannot exceed 1")
        self.pre_risk_weights = _weight_map(
            self.pre_risk_weights,
            "RebalanceDecision.pre_risk_weights",
            require_nonnegative=True,
        )
        validate_finite(self.pre_risk_cash, "RebalanceDecision.pre_risk_cash")
        if not -1e-9 <= self.pre_risk_cash <= 1.0 + 1e-9:
            raise ValueError("RebalanceDecision.pre_risk_cash must be between 0 and 1")
        total = sum(self.pre_risk_weights.values()) + self.pre_risk_cash
        if abs(total - 1.0) > 1e-8:
            raise ValueError(
                f"RebalanceDecision risky weights and cash must sum to 1; received {total}"
            )
        if self.execution_date is not None:
            self.execution_date = _timestamp(
                self.execution_date, "RebalanceDecision.execution_date"
            )
            if self.execution_date <= self.as_of_date:
                raise ValueError("RebalanceDecision.execution_date must follow as_of_date")
        if not isinstance(self.metadata, dict):
            self.metadata = dict(self.metadata)
        validate_finite(self.metadata, "RebalanceDecision.metadata")

    @property
    def weights(self) -> dict[str, float]:
        """Return a copy of proposed risky weights."""

        return dict(self.pre_risk_weights)

    @property
    def cash_weight(self) -> float:
        """Return the pre-risk cash weight."""

        return self.pre_risk_cash

    @property
    def asset_weights(self) -> dict[str, float]:
        """Return a copy of proposed risky weights (compatibility alias)."""

        return dict(self.pre_risk_weights)


@dataclass(slots=True)
class BacktestResult:
    """Daily paths and rebalance records for one simulated portfolio."""

    name: str
    daily: pd.DataFrame
    weights: pd.DataFrame
    rebalances: pd.DataFrame | list[RebalanceDecision] | list[dict[str, Any]]
    strategy_allocations: pd.DataFrame = field(default_factory=pd.DataFrame)
    regimes: pd.Series = field(default_factory=lambda: pd.Series(dtype="object"))
    risk_actions: list[RiskAction] = field(default_factory=list)
    decision_receipts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("BacktestResult.name must be a nonempty string")
        if not isinstance(self.daily, pd.DataFrame):
            raise ValueError("BacktestResult.daily must be a pandas DataFrame")
        if not isinstance(self.weights, pd.DataFrame):
            raise ValueError("BacktestResult.weights must be a pandas DataFrame")
        if not isinstance(self.rebalances, (pd.DataFrame, list)):
            raise ValueError("BacktestResult.rebalances must be a DataFrame or list")
        if not isinstance(self.strategy_allocations, pd.DataFrame):
            raise ValueError("BacktestResult.strategy_allocations must be a DataFrame")
        if not isinstance(self.regimes, pd.Series):
            raise ValueError("BacktestResult.regimes must be a pandas Series")
        if not all(isinstance(action, RiskAction) for action in self.risk_actions):
            raise ValueError("BacktestResult.risk_actions must contain RiskAction records")
        validate_finite(self.metadata, "BacktestResult.metadata")

    @property
    def daily_values(self) -> pd.DataFrame:
        """Return the daily result frame (compatibility alias)."""

        return self.daily


@dataclass(slots=True)
class PerformanceMetrics(Mapping[str, float | int]):
    """Performance metrics with explicit reasons for every undefined value.

    Undefined floating-point metrics use ``NaN`` internally.  This preserves a
    numeric interface for callers while allowing CSV writers to serialize the
    value as null and JSON writers to convert it to ``null``.  A mathematically
    defined zero remains ``0.0`` and never receives an undefined reason.
    """

    total_return: float = float("nan")
    cagr: float = float("nan")
    annualized_volatility: float = float("nan")
    sharpe_ratio: float = float("nan")
    sortino_ratio: float = float("nan")
    maximum_drawdown: float = float("nan")
    calmar_ratio: float = float("nan")
    var_95: float = float("nan")
    cvar_95: float = float("nan")
    positive_day_percentage: float = float("nan")
    average_gross_exposure: float = float("nan")
    average_cash_allocation: float = float("nan")
    total_turnover: float = float("nan")
    estimated_transaction_costs: float = float("nan")
    number_of_rebalances: int = 0
    number_of_risk_interventions: int = 0
    number_of_hard_stop_events: int = 0
    undefined_reasons: dict[str, str] = field(default_factory=dict)

    _FLOAT_METRIC_NAMES: ClassVar[tuple[str, ...]] = (
        "total_return",
        "cagr",
        "annualized_volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "maximum_drawdown",
        "calmar_ratio",
        "var_95",
        "cvar_95",
        "positive_day_percentage",
        "average_gross_exposure",
        "average_cash_allocation",
        "total_turnover",
        "estimated_transaction_costs",
    )
    _COUNT_METRIC_NAMES: ClassVar[tuple[str, ...]] = (
        "number_of_rebalances",
        "number_of_risk_interventions",
        "number_of_hard_stop_events",
    )
    _METRIC_NAMES: ClassVar[tuple[str, ...]] = _FLOAT_METRIC_NAMES + _COUNT_METRIC_NAMES

    def __post_init__(self) -> None:
        if not isinstance(self.undefined_reasons, Mapping):
            raise ValueError("PerformanceMetrics.undefined_reasons must be a mapping")
        reasons: dict[str, str] = {}
        for raw_name, raw_reason in self.undefined_reasons.items():
            name = str(raw_name)
            reason = str(raw_reason).strip()
            if name not in self._FLOAT_METRIC_NAMES:
                raise ValueError(f"Unknown undefined performance metric: {name!r}")
            if not reason:
                raise ValueError(f"Undefined reason for {name!r} must be nonempty")
            reasons[name] = reason

        for name in self._FLOAT_METRIC_NAMES:
            raw_value = getattr(self, name)
            if isinstance(raw_value, bool) or not isinstance(raw_value, Real):
                raise ValueError(f"PerformanceMetrics.{name} must be numeric")
            value = float(raw_value)
            if not isfinite(value) and not isnan(value):
                raise ValueError(f"PerformanceMetrics.{name} cannot be infinite")
            setattr(self, name, value)
            if isnan(value):
                reasons.setdefault(name, "Metric was not calculated from sufficient valid data.")
            elif name in reasons:
                raise ValueError(
                    f"PerformanceMetrics.{name} is defined and cannot have an undefined reason"
                )

        for name in self._COUNT_METRIC_NAMES:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"PerformanceMetrics.{name} must be a nonnegative integer")
        self.undefined_reasons = reasons

    def as_dict(self) -> dict[str, float | int]:
        """Return numeric metrics, excluding the separate explanation mapping."""

        return {name: getattr(self, name) for name in self._METRIC_NAMES}

    def reason_for(self, metric_name: str) -> str | None:
        """Return why ``metric_name`` is undefined, or ``None`` when it is defined."""

        if metric_name not in self._METRIC_NAMES:
            raise KeyError(metric_name)
        return self.undefined_reasons.get(metric_name)

    def as_serializable_dict(self) -> dict[str, Any]:
        """Return standard-JSON-safe values plus the explanation mapping."""

        values: dict[str, Any] = {
            name: (None if isinstance(value, float) and isnan(value) else value)
            for name, value in self.as_dict().items()
        }
        values["undefined_reasons"] = dict(self.undefined_reasons)
        return values

    def __getitem__(self, key: str) -> float | int:
        try:
            return self.as_dict()[key]
        except KeyError as exc:
            raise KeyError(key) from exc

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())

    @property
    def max_drawdown(self) -> float:
        """Return maximum drawdown (compatibility alias)."""

        return self.maximum_drawdown

    @property
    def historical_cvar_95(self) -> float:
        """Return the 95% historical CVaR (compatibility alias)."""

        return self.cvar_95

    @property
    def historical_var_95(self) -> float:
        """Return the 95% historical VaR using the return sign convention."""

        return self.var_95


@dataclass(frozen=True, slots=True)
class StrategySignal:
    """Immutable identity and cutoff for a recorded research signal."""

    signal_id: str
    strategy_name: str
    strategy_version: str
    as_of_timestamp: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("signal_id", "strategy_name", "strategy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"StrategySignal.{name} must be a nonempty string")
        timestamp = self.as_of_timestamp
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("StrategySignal.as_of_timestamp must be timezone aware")
        object.__setattr__(self, "as_of_timestamp", timestamp.astimezone(UTC))
        validate_finite(self.metadata, "StrategySignal.metadata")


@dataclass(frozen=True, slots=True)
class PortfolioTarget:
    """Immutable explicit risky-asset and cash target."""

    target_id: str
    as_of_timestamp: datetime
    asset_weights: Mapping[str, float]
    cash_weight: float

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise ValueError("PortfolioTarget.target_id must be a nonempty string")
        if not isinstance(self.as_of_timestamp, datetime) or self.as_of_timestamp.tzinfo is None:
            raise ValueError("PortfolioTarget.as_of_timestamp must be timezone aware")
        object.__setattr__(self, "as_of_timestamp", self.as_of_timestamp.astimezone(UTC))
        weights = _weight_map(
            self.asset_weights, "PortfolioTarget.asset_weights", require_nonnegative=True
        )
        object.__setattr__(self, "asset_weights", weights)
        validate_finite(self.cash_weight, "PortfolioTarget.cash_weight")
        if not 0.0 <= self.cash_weight <= 1.0:
            raise ValueError("PortfolioTarget.cash_weight must be between zero and one")
        if abs(sum(weights.values()) + self.cash_weight - 1.0) > 1e-8:
            raise ValueError("PortfolioTarget weights and cash must sum to one")
