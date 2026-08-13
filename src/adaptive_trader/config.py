"""Strict configuration models shared by research and forward paper operation.

The repository started as a compact historical-research prototype whose public
configuration exposed a ``data`` section.  The complete application uses the
more explicit ``universe`` and ``market_data`` sections.  This module accepts
both shapes, keeps the legacy ``config.data`` API available, and emits a stable
canonical representation for hashing and audit records.

No setting in this module can enable live-money trading.  ``paper_only`` is a
literal invariant and configuration containing known live-trading switches or
live Alpaca trading endpoints is rejected before model construction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from math import isclose, isfinite
from pathlib import Path
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

REGIME_NAMES = (
    "bull_low_vol",
    "bull_high_vol",
    "bear_low_vol",
    "bear_high_vol",
)
SUPPORTED_FEEDS = frozenset({"IEX", "SIP"})
SUPPORTED_ASSET_CLASS = "us_equity"
PAPER_ENABLEMENT_VALUE = "I_ACKNOWLEDGE_PAPER_ONLY"


def _finite_number(name: str, value: float, *, minimum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number; received {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}; received {value!r}")


def _positive_integer(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer; received {value!r}")


def _unit_interval(name: str, value: float) -> None:
    _finite_number(name, value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1; received {value!r}")


def _iso_date(name: str, value: str | None) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date string or null; received {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD format; received {value!r}") from exc


def _clock_time(name: str, value: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use HH:MM format")
    parts = value.split(":")
    if (
        len(parts) != 2
        or any(len(part) != 2 for part in parts)
        or not all(part.isdigit() for part in parts)
    ):
        raise ValueError(f"{name} must use HH:MM format")
    hour, minute = (int(part) for part in parts)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{name} must be a valid 24-hour time")
    return hour, minute


def _nonempty_path(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty path")


def _normalized_symbols(values: list[str] | tuple[str, ...], location: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{location} must be a list of ticker strings")
    normalized: list[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for ticker in values:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError(f"{location} may contain only nonempty strings")
        symbol = ticker.strip().upper()
        if symbol in seen:
            duplicates.add(symbol)
        else:
            normalized.append(symbol)
            seen.add(symbol)
    if duplicates:
        raise ValueError(f"{location} contains duplicate tickers: {sorted(duplicates)}")
    if not normalized:
        raise ValueError(f"{location} must contain at least one ticker")
    return normalized


@dataclass(slots=True)
class ProjectConfig:
    """Project identity and durable output locations."""

    name: str = "Adaptive Portfolio Agent"
    run_name: str = "primary_forward_paper"
    timezone: str = "America/New_York"
    database_path: str = "runtime/adaptive_portfolio_agent.db"
    output_directory: str = "outputs/primary_forward_paper"

    def __post_init__(self) -> None:
        for field_name in ("name", "run_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"project.{field_name} must be a nonempty string")
        for field_name in ("database_path", "output_directory"):
            _nonempty_path(f"project.{field_name}", getattr(self, field_name))
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("project.timezone must be a nonempty IANA timezone")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"project.timezone is not a known IANA timezone: {self.timezone!r}"
            ) from exc


@dataclass(slots=True)
class UniverseConfig:
    """Permitted long-only US equity and ETF universe."""

    tickers: list[str] = field(
        default_factory=lambda: ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY"]
    )
    benchmark: str = "SPY"
    asset_class: str = SUPPORTED_ASSET_CLASS
    require_fractionable: bool = True

    def __post_init__(self) -> None:
        self.tickers = _normalized_symbols(self.tickers, "universe.tickers")
        if not isinstance(self.benchmark, str) or not self.benchmark.strip():
            raise ValueError("universe.benchmark must be a nonempty ticker")
        self.benchmark = self.benchmark.strip().upper()
        if self.benchmark not in self.tickers:
            raise ValueError("universe.benchmark must also appear in universe.tickers")
        if self.asset_class != SUPPORTED_ASSET_CLASS:
            raise ValueError(
                "universe.asset_class must be 'us_equity'; options, crypto, futures, and other "
                "asset classes are unsupported"
            )
        if not isinstance(self.require_fractionable, bool):
            raise ValueError("universe.require_fractionable must be true or false")


@dataclass(slots=True)
class DataConfig:
    """Legacy-compatible historical-data view used by the research engine."""

    tickers: list[str] = field(
        default_factory=lambda: ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "SHY"]
    )
    benchmark: str = "SPY"
    start_date: str = "2016-01-01"
    end_date: str | None = None
    cache_file: str = "data/cache/market_data.csv"
    refresh_cache: bool = False
    asset_class: str = SUPPORTED_ASSET_CLASS
    require_fractionable: bool = True

    def __post_init__(self) -> None:
        self.tickers = _normalized_symbols(self.tickers, "data.tickers")
        if not isinstance(self.benchmark, str) or not self.benchmark.strip():
            raise ValueError("data.benchmark must be a nonempty ticker")
        self.benchmark = self.benchmark.strip().upper()
        if self.benchmark not in self.tickers:
            raise ValueError("data.benchmark must also appear in data.tickers")
        start = _iso_date("data.start_date", self.start_date)
        end = _iso_date("data.end_date", self.end_date)
        if start is not None and end is not None and end <= start:
            raise ValueError("data.end_date must be later than data.start_date")
        _nonempty_path("data.cache_file", self.cache_file)
        if not isinstance(self.refresh_cache, bool):
            raise ValueError("data.refresh_cache must be true or false")
        if self.asset_class != SUPPORTED_ASSET_CLASS:
            raise ValueError("data.asset_class must be 'us_equity'")
        if not isinstance(self.require_fractionable, bool):
            raise ValueError("data.require_fractionable must be true or false")

    def as_universe(self) -> UniverseConfig:
        """Return the canonical universe represented by this compatibility section."""

        return UniverseConfig(
            tickers=list(self.tickers),
            benchmark=self.benchmark,
            asset_class=self.asset_class,
            require_fractionable=self.require_fractionable,
        )


@dataclass(slots=True)
class MarketDataConfig:
    """Historical and streaming market-data policy."""

    provider: str = "alpaca"
    feed: str = "IEX"
    stream_type: str = "minute_bars"
    stale_after_seconds: int = 180
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    historical_calendar_days: int = 900
    minimum_completed_sessions: int = 450
    cache_directory: str = "data/cache"
    adjustment: str = "all"

    def __post_init__(self) -> None:
        if self.provider not in {"alpaca", "replay", "synthetic"}:
            raise ValueError("market_data.provider must be alpaca, replay, or synthetic")
        if not isinstance(self.feed, str):
            raise ValueError("market_data.feed must be IEX or SIP")
        self.feed = self.feed.upper()
        if self.feed not in SUPPORTED_FEEDS:
            raise ValueError(f"Unsupported market-data feed: {self.feed!r}; use IEX or SIP")
        if self.stream_type != "minute_bars":
            raise ValueError("market_data.stream_type must be 'minute_bars'")
        _positive_integer("market_data.stale_after_seconds", self.stale_after_seconds)
        _finite_number(
            "market_data.reconnect_initial_seconds", self.reconnect_initial_seconds, minimum=0.0
        )
        _finite_number("market_data.reconnect_max_seconds", self.reconnect_max_seconds, minimum=0.0)
        if self.reconnect_initial_seconds <= 0 or self.reconnect_max_seconds <= 0:
            raise ValueError("market-data reconnect delays must be greater than zero")
        if self.reconnect_initial_seconds > self.reconnect_max_seconds:
            raise ValueError(
                "market_data.reconnect_initial_seconds cannot exceed reconnect_max_seconds"
            )
        _positive_integer("market_data.historical_calendar_days", self.historical_calendar_days)
        _positive_integer("market_data.minimum_completed_sessions", self.minimum_completed_sessions)
        _nonempty_path("market_data.cache_directory", self.cache_directory)
        if self.adjustment.lower() not in {"all", "split", "raw"}:
            raise ValueError("market_data.adjustment must be all, split, or raw")
        self.adjustment = self.adjustment.lower()


@dataclass(slots=True)
class ScheduleConfig:
    """Session scheduling and monitoring intervals."""

    evaluation_time_et: str = "10:05"
    catch_up_cutoff_et: str = "14:30"
    heartbeat_interval_seconds: int = 30
    risk_monitor_interval_seconds: int = 60
    reconciliation_interval_seconds: int = 300
    order_fill_timeout_seconds: int = 120
    maximum_decisions_per_session: int = 1

    def __post_init__(self) -> None:
        evaluation_time = _clock_time("schedule.evaluation_time_et", self.evaluation_time_et)
        cutoff_time = _clock_time("schedule.catch_up_cutoff_et", self.catch_up_cutoff_et)
        if evaluation_time >= cutoff_time:
            raise ValueError("schedule.evaluation_time_et must precede catch_up_cutoff_et")
        for name in (
            "heartbeat_interval_seconds",
            "risk_monitor_interval_seconds",
            "reconciliation_interval_seconds",
            "order_fill_timeout_seconds",
            "maximum_decisions_per_session",
        ):
            _positive_integer(f"schedule.{name}", getattr(self, name))
        if self.maximum_decisions_per_session != 1:
            raise ValueError("schedule.maximum_decisions_per_session must remain 1")


@dataclass(slots=True)
class ExecutionConfig:
    """Structurally paper-only order policy."""

    paper_only: bool = True
    paper_order_submission_enabled: bool = False
    required_enablement_value: str = PAPER_ENABLEMENT_VALUE
    regular_hours_only: bool = True
    order_type: str = "market"
    time_in_force: str = "day"
    minimum_order_notional: float = 25.0
    required_cash_buffer: float = 0.02
    maximum_orders_per_rebalance: int = 20
    max_single_order_fraction_of_equity: float = 0.20
    cancel_conflicting_open_orders: bool = True

    def __post_init__(self) -> None:
        if self.paper_only is not True:
            raise ValueError("execution.paper_only is an invariant and must be true")
        if not isinstance(self.paper_order_submission_enabled, bool):
            raise ValueError("execution.paper_order_submission_enabled must be true or false")
        if self.required_enablement_value != PAPER_ENABLEMENT_VALUE:
            raise ValueError(
                "execution.required_enablement_value must remain I_ACKNOWLEDGE_PAPER_ONLY"
            )
        if self.regular_hours_only is not True:
            raise ValueError("execution.regular_hours_only must be true")
        if self.order_type.lower() != "market":
            raise ValueError("execution.order_type must be market")
        self.order_type = "market"
        if self.time_in_force.lower() != "day":
            raise ValueError("execution.time_in_force must be day")
        self.time_in_force = "day"
        _finite_number("execution.minimum_order_notional", self.minimum_order_notional, minimum=0.0)
        _unit_interval("execution.required_cash_buffer", self.required_cash_buffer)
        _positive_integer(
            "execution.maximum_orders_per_rebalance", self.maximum_orders_per_rebalance
        )
        _unit_interval(
            "execution.max_single_order_fraction_of_equity",
            self.max_single_order_fraction_of_equity,
        )
        if self.max_single_order_fraction_of_equity <= 0:
            raise ValueError("execution.max_single_order_fraction_of_equity must be positive")
        if not isinstance(self.cancel_conflicting_open_orders, bool):
            raise ValueError("execution.cancel_conflicting_open_orders must be true or false")


@dataclass(slots=True)
class BacktestConfig:
    """Historical execution, cost, and evaluation settings."""

    start_date: str | None = None
    end_date: str | None = None
    initial_capital: float = 100_000.0
    rebalance_frequency: str = "daily"
    transaction_cost_bps: float = 5.0
    slippage_bps: float = 5.0
    annualization_factor: int = 252
    comparison_period_start: str = "2020-01-01"
    out_of_sample_start: str | None = None
    development_period_start: str | None = None
    development_period_end: str | None = None
    validation_period_start: str | None = None
    validation_period_end: str | None = None
    holdout_period_start: str | None = None

    def __post_init__(self) -> None:
        start = _iso_date("backtest.start_date", self.start_date)
        end = _iso_date("backtest.end_date", self.end_date)
        if start is not None and end is not None and end <= start:
            raise ValueError("backtest.end_date must be later than backtest.start_date")
        _finite_number("backtest.initial_capital", self.initial_capital, minimum=0.0)
        if self.initial_capital <= 0:
            raise ValueError("backtest.initial_capital must be greater than zero")
        if self.rebalance_frequency not in {"daily", "weekly"}:
            raise ValueError("backtest.rebalance_frequency must be daily or weekly")
        _finite_number("backtest.transaction_cost_bps", self.transaction_cost_bps, minimum=0.0)
        _finite_number("backtest.slippage_bps", self.slippage_bps, minimum=0.0)
        _positive_integer("backtest.annualization_factor", self.annualization_factor)
        _iso_date("backtest.comparison_period_start", self.comparison_period_start)
        if self.out_of_sample_start is None:
            self.out_of_sample_start = self.comparison_period_start
        else:
            _iso_date("backtest.out_of_sample_start", self.out_of_sample_start)
            self.comparison_period_start = self.out_of_sample_start
        window_names = (
            "development_period_start",
            "development_period_end",
            "validation_period_start",
            "validation_period_end",
            "holdout_period_start",
        )
        window_dates = {
            name: _iso_date(f"backtest.{name}", getattr(self, name)) for name in window_names
        }
        configured_windows = [value is not None for value in window_dates.values()]
        if any(configured_windows) and not all(configured_windows):
            raise ValueError(
                "backtest development, validation, and holdout window fields must be "
                "configured together"
            )
        if all(configured_windows):
            development_start = cast(date, window_dates["development_period_start"])
            development_end = cast(date, window_dates["development_period_end"])
            validation_start = cast(date, window_dates["validation_period_start"])
            validation_end = cast(date, window_dates["validation_period_end"])
            holdout_start = cast(date, window_dates["holdout_period_start"])
            if not (
                development_start
                <= development_end
                < validation_start
                <= validation_end
                < holdout_start
            ):
                raise ValueError(
                    "backtest research windows must be ordered development, validation, holdout"
                )
            if self.out_of_sample_start != self.holdout_period_start:
                raise ValueError(
                    "backtest.out_of_sample_start must equal backtest.holdout_period_start"
                )


@dataclass(slots=True)
class MomentumConfig:
    """Cross-sectional momentum signal settings."""

    lookback_days: int = 63
    volatility_lookback_days: int = 20
    top_n: int = 3
    require_positive_return: bool = True

    def __post_init__(self) -> None:
        _positive_integer("momentum.lookback_sessions", self.lookback_days)
        _positive_integer("momentum.volatility_lookback_sessions", self.volatility_lookback_days)
        _positive_integer("momentum.top_n", self.top_n)
        if not isinstance(self.require_positive_return, bool):
            raise ValueError("momentum.require_positive_return must be true or false")

    @property
    def lookback_sessions(self) -> int:
        return self.lookback_days

    @lookback_sessions.setter
    def lookback_sessions(self, value: int) -> None:
        self.lookback_days = value

    @property
    def volatility_lookback_sessions(self) -> int:
        return self.volatility_lookback_days

    @volatility_lookback_sessions.setter
    def volatility_lookback_sessions(self, value: int) -> None:
        self.volatility_lookback_days = value


@dataclass(slots=True)
class MeanReversionConfig:
    """Trend-filtered mean-reversion signal settings."""

    zscore_lookback_days: int = 20
    entry_zscore: float = -0.5
    long_term_trend_days: int = 100
    volatility_lookback_days: int = 20
    top_n: int = 3

    def __post_init__(self) -> None:
        _positive_integer("mean_reversion.zscore_lookback_sessions", self.zscore_lookback_days)
        _finite_number("mean_reversion.entry_zscore", self.entry_zscore)
        if self.entry_zscore >= 0:
            raise ValueError("mean_reversion.entry_zscore must be negative")
        _positive_integer("mean_reversion.long_term_trend_sessions", self.long_term_trend_days)
        _positive_integer(
            "mean_reversion.volatility_lookback_sessions", self.volatility_lookback_days
        )
        _positive_integer("mean_reversion.top_n", self.top_n)

    @property
    def zscore_lookback_sessions(self) -> int:
        return self.zscore_lookback_days

    @zscore_lookback_sessions.setter
    def zscore_lookback_sessions(self, value: int) -> None:
        self.zscore_lookback_days = value

    @property
    def long_term_trend_sessions(self) -> int:
        return self.long_term_trend_days

    @long_term_trend_sessions.setter
    def long_term_trend_sessions(self, value: int) -> None:
        self.long_term_trend_days = value

    @property
    def volatility_lookback_sessions(self) -> int:
        return self.volatility_lookback_days

    @volatility_lookback_sessions.setter
    def volatility_lookback_sessions(self, value: int) -> None:
        self.volatility_lookback_days = value


@dataclass(slots=True)
class RegimeAllocation(Mapping[str, float]):
    """Capital fractions assigned to each strategy and strategic cash."""

    momentum: float
    mean_reversion: float
    strategic_cash: float

    def __post_init__(self) -> None:
        for name in ("momentum", "mean_reversion", "strategic_cash"):
            _unit_interval(f"regime allocation {name}", getattr(self, name))
        total = self.momentum + self.mean_reversion + self.strategic_cash
        if not isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(f"regime allocation fractions must sum to 1; received {total}")

    def __getitem__(self, key: str) -> float:
        if key not in {"momentum", "mean_reversion", "strategic_cash"}:
            raise KeyError(key)
        return float(getattr(self, key))

    def __iter__(self) -> Iterator[str]:
        return iter(("momentum", "mean_reversion", "strategic_cash"))

    def __len__(self) -> int:
        return 3

    def as_dict(self) -> dict[str, float]:
        return {name: self[name] for name in self}


def _default_regime_allocations() -> dict[str, RegimeAllocation]:
    return {
        "bull_low_vol": RegimeAllocation(0.70, 0.30, 0.00),
        "bull_high_vol": RegimeAllocation(0.45, 0.35, 0.20),
        "bear_low_vol": RegimeAllocation(0.25, 0.35, 0.40),
        "bear_high_vol": RegimeAllocation(0.10, 0.15, 0.75),
    }


@dataclass(slots=True)
class RegimeConfig:
    """Transparent moving-average and realized-volatility regime settings."""

    benchmark: str = "SPY"
    fast_moving_average_days: int = 50
    slow_moving_average_days: int = 200
    volatility_lookback_days: int = 20
    volatility_threshold_lookback_days: int = 252
    allocations: dict[str, RegimeAllocation] = field(default_factory=_default_regime_allocations)

    def __post_init__(self) -> None:
        if not isinstance(self.benchmark, str) or not self.benchmark.strip():
            raise ValueError("regime.benchmark must be a nonempty ticker")
        self.benchmark = self.benchmark.strip().upper()
        _positive_integer("regime.fast_moving_average_sessions", self.fast_moving_average_days)
        _positive_integer("regime.slow_moving_average_sessions", self.slow_moving_average_days)
        if self.fast_moving_average_days >= self.slow_moving_average_days:
            raise ValueError(
                "regime.fast_moving_average_sessions must be less than slow_moving_average_sessions"
            )
        _positive_integer("regime.volatility_lookback_sessions", self.volatility_lookback_days)
        _positive_integer(
            "regime.volatility_threshold_lookback_sessions",
            self.volatility_threshold_lookback_days,
        )
        if not isinstance(self.allocations, Mapping):
            raise ValueError("regime.allocations must be a mapping")
        converted: dict[str, RegimeAllocation] = {}
        for regime_name, allocation in self.allocations.items():
            if isinstance(allocation, RegimeAllocation):
                converted[str(regime_name)] = allocation
            elif isinstance(allocation, Mapping):
                converted[str(regime_name)] = _construct(
                    RegimeAllocation, allocation, f"regime.allocations.{regime_name}"
                )
            else:
                raise ValueError(f"regime.allocations.{regime_name} must be an allocation mapping")
        missing = set(REGIME_NAMES).difference(converted)
        extra = set(converted).difference(REGIME_NAMES)
        if missing or extra:
            raise ValueError(
                "regime.allocations must define exactly the four supported regimes; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        self.allocations = converted

    @property
    def fast_moving_average_sessions(self) -> int:
        return self.fast_moving_average_days

    @fast_moving_average_sessions.setter
    def fast_moving_average_sessions(self, value: int) -> None:
        self.fast_moving_average_days = value

    @property
    def slow_moving_average_sessions(self) -> int:
        return self.slow_moving_average_days

    @slow_moving_average_sessions.setter
    def slow_moving_average_sessions(self, value: int) -> None:
        self.slow_moving_average_days = value

    @property
    def volatility_lookback_sessions(self) -> int:
        return self.volatility_lookback_days

    @volatility_lookback_sessions.setter
    def volatility_lookback_sessions(self, value: int) -> None:
        self.volatility_lookback_days = value

    @property
    def volatility_threshold_lookback_sessions(self) -> int:
        return self.volatility_threshold_lookback_days

    @volatility_threshold_lookback_sessions.setter
    def volatility_threshold_lookback_sessions(self, value: int) -> None:
        self.volatility_threshold_lookback_days = value


@dataclass(slots=True)
class RiskConfig:
    """Independent portfolio-risk constraints."""

    max_position_weight: float = 0.25
    max_gross_exposure: float = 1.0
    target_annual_volatility: float = 0.12
    covariance_lookback_days: int = 60
    max_turnover_per_rebalance: float = 0.35
    drawdown_soft_limit: float = 0.10
    soft_limit_max_gross_exposure: float = 0.50
    drawdown_hard_limit: float = 0.15
    daily_loss_limit: float = 0.03
    required_cash_buffer: float = 0.02

    def __post_init__(self) -> None:
        _unit_interval("risk.max_position_weight", self.max_position_weight)
        if self.max_position_weight <= 0:
            raise ValueError("risk.max_position_weight must be greater than zero")
        _unit_interval("risk.max_gross_exposure", self.max_gross_exposure)
        _finite_number("risk.target_annual_volatility", self.target_annual_volatility, minimum=0.0)
        if self.target_annual_volatility <= 0:
            raise ValueError("risk.target_annual_volatility must be greater than zero")
        _positive_integer("risk.covariance_lookback_sessions", self.covariance_lookback_days)
        _unit_interval("risk.max_turnover_per_rebalance", self.max_turnover_per_rebalance)
        _unit_interval("risk.drawdown_soft_limit", self.drawdown_soft_limit)
        _unit_interval("risk.soft_limit_max_gross_exposure", self.soft_limit_max_gross_exposure)
        _unit_interval("risk.drawdown_hard_limit", self.drawdown_hard_limit)
        _unit_interval("risk.daily_loss_limit", self.daily_loss_limit)
        _unit_interval("risk.required_cash_buffer", self.required_cash_buffer)
        if self.drawdown_soft_limit >= self.drawdown_hard_limit:
            raise ValueError("risk.drawdown_soft_limit must be below drawdown_hard_limit")
        if self.soft_limit_max_gross_exposure > self.max_gross_exposure:
            raise ValueError("risk.soft_limit_max_gross_exposure cannot exceed max_gross_exposure")

    @property
    def covariance_lookback_sessions(self) -> int:
        return self.covariance_lookback_days

    @covariance_lookback_sessions.setter
    def covariance_lookback_sessions(self, value: int) -> None:
        self.covariance_lookback_days = value


@dataclass(slots=True)
class ReportingConfig:
    """Output persistence and rolling-statistic settings."""

    rolling_sharpe_window_days: int = 63
    risk_free_rate: float = 0.0
    save_csv: bool = True
    save_json: bool = True
    save_markdown: bool = True
    save_plots: bool = True

    def __post_init__(self) -> None:
        _positive_integer(
            "reporting.rolling_sharpe_window_sessions", self.rolling_sharpe_window_days
        )
        _finite_number("reporting.risk_free_rate", self.risk_free_rate)
        for name in ("save_csv", "save_json", "save_markdown", "save_plots"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"reporting.{name} must be true or false")

    @property
    def rolling_sharpe_window_sessions(self) -> int:
        return self.rolling_sharpe_window_days

    @rolling_sharpe_window_sessions.setter
    def rolling_sharpe_window_sessions(self, value: int) -> None:
        self.rolling_sharpe_window_days = value


@dataclass(slots=True)
class ReplayConfig:
    """Deterministic replay policy."""

    fixture_path: str = "data/replay/synthetic_session.jsonl"
    speed_multiplier: float = 100.0
    deterministic_seed: int = 20260808

    def __post_init__(self) -> None:
        _nonempty_path("replay.fixture_path", self.fixture_path)
        _finite_number("replay.speed_multiplier", self.speed_multiplier, minimum=0.0)
        if self.speed_multiplier <= 0:
            raise ValueError("replay.speed_multiplier must be greater than zero")
        if isinstance(self.deterministic_seed, bool) or not isinstance(
            self.deterministic_seed, int
        ):
            raise ValueError("replay.deterministic_seed must be an integer")


@dataclass(slots=True)
class AppConfig:
    """Complete validated configuration for an Adaptive Portfolio Agent run."""

    project: ProjectConfig = field(default_factory=ProjectConfig)
    data: DataConfig = field(default_factory=DataConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    momentum: MomentumConfig = field(default_factory=MomentumConfig)
    mean_reversion: MeanReversionConfig = field(default_factory=MeanReversionConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    replay: ReplayConfig = field(default_factory=ReplayConfig)

    def __post_init__(self) -> None:
        if self.data.benchmark not in self.data.tickers:
            raise ValueError("data.benchmark must also appear in data.tickers")
        if self.regime.benchmark not in self.data.tickers:
            raise ValueError("regime.benchmark must also appear in data.tickers")
        if self.momentum.top_n > len(self.data.tickers):
            raise ValueError("momentum.top_n cannot exceed the number of configured tickers")
        if self.mean_reversion.top_n > len(self.data.tickers):
            raise ValueError("mean_reversion.top_n cannot exceed the number of configured tickers")
        start_value = self.backtest.start_date or self.data.start_date
        end_value = (
            self.backtest.end_date if self.backtest.end_date is not None else self.data.end_date
        )
        start = _iso_date("backtest.start_date", start_value)
        end = _iso_date("backtest.end_date", end_value)
        comparison = _iso_date(
            "backtest.comparison_period_start", self.backtest.comparison_period_start
        )
        if start is not None and end is not None and end <= start:
            raise ValueError("backtest.end_date must be later than backtest.start_date")
        if start is not None and comparison is not None and comparison < start:
            raise ValueError("backtest.comparison_period_start cannot precede the research start")
        self.data.start_date = start_value
        self.data.end_date = end_value
        self.backtest.start_date = start_value
        self.backtest.end_date = end_value
        # The canonical execution cash buffer is also mirrored into the pure
        # research risk section so RiskEngine(RiskConfig) retains the invariant.
        if not isclose(
            self.risk.required_cash_buffer,
            self.execution.required_cash_buffer,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "risk.required_cash_buffer and execution.required_cash_buffer must match"
            )

    @property
    def universe(self) -> UniverseConfig:
        """Return the canonical universe while preserving the legacy data API."""

        return self.data.as_universe()

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> AppConfig:
        """Construct and validate a configuration from canonical or legacy input."""

        if not isinstance(values, Mapping):
            raise ValueError("Configuration root must be a mapping")
        _reject_forbidden_live_configuration(values)
        allowed = {
            "project",
            "universe",
            "data",
            "market_data",
            "schedule",
            "execution",
            "backtest",
            "momentum",
            "mean_reversion",
            "regime",
            "risk",
            "reporting",
            "replay",
        }
        extra = set(values).difference(allowed)
        if extra:
            raise ValueError(f"Unknown top-level configuration sections: {sorted(extra)}")

        raw_data = values.get("data")
        raw_universe = values.get("universe")
        raw_backtest = values.get("backtest", {})
        raw_market = values.get("market_data", {})
        if raw_data is not None:
            data_values = dict(_mapping(raw_data, "data"))
        elif raw_universe is not None:
            universe_values = dict(_mapping(raw_universe, "universe"))
            data_values = {
                "tickers": universe_values.get("tickers"),
                "benchmark": universe_values.get("benchmark", "SPY"),
                "asset_class": universe_values.get("asset_class", SUPPORTED_ASSET_CLASS),
                "require_fractionable": universe_values.get("require_fractionable", True),
            }
        else:
            data_values = {}

        backtest_values = _normalize_legacy_keys(
            dict(_mapping(raw_backtest, "backtest")),
            {
                "comparison_period_start": "comparison_period_start",
            },
        )
        if "start_date" in backtest_values:
            data_values.setdefault("start_date", backtest_values["start_date"])
        if "end_date" in backtest_values:
            data_values.setdefault("end_date", backtest_values["end_date"])
        market_values = dict(_mapping(raw_market, "market_data"))
        if "cache_directory" in market_values and "cache_file" not in data_values:
            data_values["cache_file"] = str(
                Path(str(market_values["cache_directory"])) / "market_data.csv"
            )
        # When both shapes are present, legacy ``data`` is authoritative for
        # compatibility but unsupported universe policy still cannot be hidden.
        if raw_universe is not None:
            universe = _construct(UniverseConfig, raw_universe, "universe")
            if raw_data is None:
                data_values.update(
                    {
                        "tickers": universe.tickers,
                        "benchmark": universe.benchmark,
                        "asset_class": universe.asset_class,
                        "require_fractionable": universe.require_fractionable,
                    }
                )
            elif universe.asset_class != SUPPORTED_ASSET_CLASS:
                raise ValueError("Unsupported universe asset class")

        momentum_values = _normalize_legacy_keys(
            dict(_mapping(values.get("momentum", {}), "momentum")),
            {
                "lookback_sessions": "lookback_days",
                "volatility_lookback_sessions": "volatility_lookback_days",
            },
        )
        mean_reversion_values = _normalize_legacy_keys(
            dict(_mapping(values.get("mean_reversion", {}), "mean_reversion")),
            {
                "zscore_lookback_sessions": "zscore_lookback_days",
                "long_term_trend_sessions": "long_term_trend_days",
                "volatility_lookback_sessions": "volatility_lookback_days",
            },
        )
        regime_values = _normalize_legacy_keys(
            dict(_mapping(values.get("regime", {}), "regime")),
            {
                "fast_moving_average_sessions": "fast_moving_average_days",
                "slow_moving_average_sessions": "slow_moving_average_days",
                "volatility_lookback_sessions": "volatility_lookback_days",
                "volatility_threshold_lookback_sessions": ("volatility_threshold_lookback_days"),
            },
        )
        risk_values = _normalize_legacy_keys(
            dict(_mapping(values.get("risk", {}), "risk")),
            {"covariance_lookback_sessions": "covariance_lookback_days"},
        )
        execution_values = dict(_mapping(values.get("execution", {}), "execution"))
        cash_buffer = execution_values.get(
            "required_cash_buffer", risk_values.get("required_cash_buffer", 0.02)
        )
        execution_values.setdefault("required_cash_buffer", cash_buffer)
        risk_values.setdefault("required_cash_buffer", cash_buffer)
        reporting_values = _normalize_legacy_keys(
            dict(_mapping(values.get("reporting", {}), "reporting")),
            {"rolling_sharpe_window_sessions": "rolling_sharpe_window_days"},
        )

        sections: dict[str, Any] = {
            "project": _construct(ProjectConfig, values.get("project", {}), "project"),
            "data": _construct(DataConfig, data_values, "data"),
            "market_data": _construct(MarketDataConfig, market_values, "market_data"),
            "schedule": _construct(ScheduleConfig, values.get("schedule", {}), "schedule"),
            "execution": _construct(ExecutionConfig, execution_values, "execution"),
            "backtest": _construct(BacktestConfig, backtest_values, "backtest"),
            "momentum": _construct(MomentumConfig, momentum_values, "momentum"),
            "mean_reversion": _construct(
                MeanReversionConfig, mean_reversion_values, "mean_reversion"
            ),
            "regime": _construct(RegimeConfig, regime_values, "regime"),
            "risk": _construct(RiskConfig, risk_values, "risk"),
            "reporting": _construct(ReportingConfig, reporting_values, "reporting"),
            "replay": _construct(ReplayConfig, values.get("replay", {}), "replay"),
        }
        return cls(**sections)

    def to_dict(self) -> dict[str, Any]:
        """Return a round-trippable mapping with canonical and compatibility data."""

        canonical = self.to_canonical_dict()
        canonical["data"] = asdict(self.data)
        return canonical

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the stable audit/configuration representation."""

        backtest = asdict(self.backtest)
        backtest["start_date"] = self.data.start_date
        backtest["end_date"] = self.data.end_date
        backtest.pop("out_of_sample_start", None)
        momentum = _rename_keys(
            asdict(self.momentum),
            {
                "lookback_days": "lookback_sessions",
                "volatility_lookback_days": "volatility_lookback_sessions",
            },
        )
        mean_reversion = _rename_keys(
            asdict(self.mean_reversion),
            {
                "zscore_lookback_days": "zscore_lookback_sessions",
                "long_term_trend_days": "long_term_trend_sessions",
                "volatility_lookback_days": "volatility_lookback_sessions",
            },
        )
        regime = _rename_keys(
            asdict(self.regime),
            {
                "fast_moving_average_days": "fast_moving_average_sessions",
                "slow_moving_average_days": "slow_moving_average_sessions",
                "volatility_lookback_days": "volatility_lookback_sessions",
                "volatility_threshold_lookback_days": ("volatility_threshold_lookback_sessions"),
            },
        )
        risk = _rename_keys(
            asdict(self.risk), {"covariance_lookback_days": "covariance_lookback_sessions"}
        )
        reporting = _rename_keys(
            asdict(self.reporting),
            {"rolling_sharpe_window_days": "rolling_sharpe_window_sessions"},
        )
        return {
            "project": asdict(self.project),
            "universe": asdict(self.universe),
            "market_data": asdict(self.market_data),
            "schedule": asdict(self.schedule),
            "execution": asdict(self.execution),
            "momentum": momentum,
            "mean_reversion": mean_reversion,
            "regime": regime,
            "risk": risk,
            "backtest": backtest,
            "reporting": reporting,
            "replay": asdict(self.replay),
        }

    @property
    def configuration_hash(self) -> str:
        """Return a deterministic SHA-256 hash of the final canonical config."""

        encoded = json.dumps(
            self.to_canonical_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def config_hash(self) -> str:
        """Compatibility alias for :attr:`configuration_hash`."""

        return self.configuration_hash


Config = AppConfig
"""Backward-compatible concise alias for :class:`AppConfig`."""

T = TypeVar("T")


def _mapping(values: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(values, Mapping):
        raise ValueError(f"{location} must be a mapping")
    return values


def _normalize_legacy_keys(values: dict[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    result = dict(values)
    for legacy, canonical in aliases.items():
        if legacy not in result:
            continue
        # A mapping produced by ``to_dict`` may be edited through the legacy
        # public key. In that compatibility case the explicit legacy-facing
        # value is authoritative. Canonical YAML contains only ``legacy`` here.
        if canonical in result:
            result.pop(legacy)
        else:
            result[canonical] = result.pop(legacy)
    return result


def _rename_keys(values: dict[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    """Return a copy with selected compatibility field names made canonical."""

    result = dict(values)
    for current, canonical in aliases.items():
        if current in result:
            result[canonical] = result.pop(current)
    return result


def _construct(model: type[T], values: Mapping[str, Any], location: str) -> T:
    if not isinstance(values, Mapping):
        raise ValueError(f"{location} must be a mapping")
    try:
        return model(**dict(values))
    except TypeError as exc:
        raise ValueError(f"Invalid fields in {location}: {exc}") from exc


def _walk_config(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            current = (*path, str(key))
            yield current, item
            yield from _walk_config(item, current)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_config(item, (*path, str(index)))


def _reject_forbidden_live_configuration(values: Mapping[str, Any]) -> None:
    forbidden_keys = {
        "live_trading_enabled",
        "live_money_trading",
        "enable_live_trading",
        "live_broker",
        "live_api_base_url",
        "alpaca_live_base_url",
    }
    for path, value in _walk_config(values):
        key = path[-1].lower()
        location = ".".join(path)
        if key in forbidden_keys:
            raise ValueError(f"Forbidden live-trading configuration field: {location}")
        if key == "paper_only" and value is not True:
            raise ValueError("execution.paper_only must be true")
        if isinstance(value, str):
            lowered = value.strip().lower()
            if "api.alpaca.markets" in lowered and "paper-api.alpaca.markets" not in lowered:
                raise ValueError(f"Live Alpaca trading endpoint is forbidden at {location}")


def load_config(path: str | Path) -> AppConfig:
    """Load and strictly validate an application configuration from YAML."""

    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file does not exist: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc
    if raw is None:
        raise ValueError(f"Configuration file is empty: {config_path}")
    try:
        return AppConfig.from_dict(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid configuration in {config_path}: {exc}") from exc
