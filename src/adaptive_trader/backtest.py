"""Causal event-driven backtesting for adaptive and baseline portfolios."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from adaptive_trader.allocator import AdaptiveAllocator, StaticAllocator
from adaptive_trader.config import AppConfig
from adaptive_trader.data import (
    MarketData,
    required_history_from_config,
    validate_market_data,
)
from adaptive_trader.models import (
    BacktestResult,
    RebalanceDecision,
    RiskAction,
    StrategyResult,
)
from adaptive_trader.regimes import RegimeDetector
from adaptive_trader.risk import RiskEngine, calculate_turnover
from adaptive_trader.strategies.mean_reversion import MeanReversionStrategy
from adaptive_trader.strategies.momentum import MomentumStrategy

LOGGER = logging.getLogger(__name__)
PORTFOLIO_NAMES = (
    "adaptive",
    "static_blend",
    "momentum_only",
    "mean_reversion_only",
    "equal_weight_buy_hold",
    "benchmark_buy_hold",
)


class BacktestError(RuntimeError):
    """Raised when a simulation cannot proceed without invalid assumptions."""


@dataclass(slots=True)
class BacktestSuiteResult:
    """The six comparable portfolio simulations and their shared inputs."""

    runs: dict[str, BacktestResult]
    market_data: MarketData
    config: AppConfig

    def __post_init__(self) -> None:
        missing = set(PORTFOLIO_NAMES).difference(self.runs)
        if missing:
            raise ValueError(f"Backtest suite is missing portfolio runs: {sorted(missing)}")

    @property
    def start_date(self) -> pd.Timestamp:
        """Return the first common simulated execution date."""

        return min(run.daily.index.min() for run in self.runs.values())

    @property
    def end_date(self) -> pd.Timestamp:
        """Return the last common simulated execution date."""

        return max(run.daily.index.max() for run in self.runs.values())


def _iso_week(date: pd.Timestamp) -> tuple[int, int]:
    calendar = date.isocalendar()
    return int(calendar.year), int(calendar.week)


def _is_rebalance_date(
    date: pd.Timestamp,
    previous_date: pd.Timestamp | None,
    frequency: str = "weekly",
) -> bool:
    """Return whether the configured daily or weekly evaluation is due."""

    if frequency == "daily":
        return True
    if frequency == "weekly":
        return previous_date is None or _iso_week(date) != _iso_week(previous_date)
    raise BacktestError(f"Unsupported rebalance frequency: {frequency!r}")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if np.isfinite(result) else default


def _historical_decision_id(
    config: AppConfig,
    portfolio: str,
    execution_date: pd.Timestamp,
    audit_run_id: str,
) -> str:
    """Return a stable identifier namespaced by the canonical configuration."""

    namespace = audit_run_id[:12] if audit_run_id else config.configuration_hash[:12]
    return f"bt-{namespace}-{portfolio}-{execution_date:%Y%m%d}"


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy values into JSON-safe primitive structures."""

    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _single_strategy_allocation(
    regime_name: str,
    result: StrategyResult,
    as_of_date: pd.Timestamp,
    strategy_name: str,
) -> RebalanceDecision:
    allocations = {
        "momentum": 1.0 if strategy_name == "momentum" else 0.0,
        "mean_reversion": 1.0 if strategy_name == "mean_reversion" else 0.0,
        "strategic_cash": 0.0,
    }
    weights = dict(result.weights)
    return RebalanceDecision(
        as_of_date=as_of_date,
        regime=regime_name,
        strategy_allocations=allocations,
        pre_risk_weights=weights,
        pre_risk_cash=max(0.0, 1.0 - sum(weights.values())),
        metadata={"allocator": f"{strategy_name}_only"},
    )


def _historical_positions(weights: dict[str, float], equity: float) -> list[dict[str, Any]]:
    """Represent simulated holdings without inventing share quantities."""

    return [
        {
            "symbol": symbol,
            "portfolio_weight": float(weight),
            "simulated_market_value": float(equity * weight),
            "quantity": None,
            "quantity_reason": "weight-based historical simulation does not model shares",
        }
        for symbol, weight in sorted(weights.items())
        if abs(float(weight)) > 1e-12
    ]


def _historical_order_intents(
    *,
    decision_id: str,
    current_weights: dict[str, float],
    final_weights: dict[str, float],
    equity: float,
    execution_prices: pd.Series,
) -> list[dict[str, Any]]:
    """Describe simulated session-open weight changes without paper-order identifiers."""

    intents: list[dict[str, Any]] = []
    for symbol in sorted(set(current_weights) | set(final_weights)):
        current = float(current_weights.get(symbol, 0.0))
        target = float(final_weights.get(symbol, 0.0))
        delta = target - current
        if abs(delta) <= 1e-12:
            continue
        price = execution_prices.get(symbol)
        reference_price = None if pd.isna(price) else float(price)
        intents.append(
            {
                "intent_id": f"{decision_id}-{symbol}",
                "client_order_id": None,
                "broker_order_id": None,
                "identifier_reason": "historical simulation; no paper order was created",
                "symbol": symbol,
                "side": "buy" if delta > 0 else "sell",
                "current_weight": current,
                "target_weight": target,
                "weight_delta": delta,
                "simulated_notional": abs(delta) * equity,
                "reference_adjusted_open": reference_price,
                "status": "historically_simulated_at_session_open",
            }
        )
    return intents


def _risk_limits(config: AppConfig) -> dict[str, Any]:
    """Capture every configured portfolio and execution control used by research."""

    risk = config.risk
    execution = config.execution
    return {
        "max_position_weight": risk.max_position_weight,
        "max_gross_exposure": risk.max_gross_exposure,
        "target_annual_volatility": risk.target_annual_volatility,
        "covariance_lookback_sessions": risk.covariance_lookback_days,
        "max_turnover_per_rebalance": risk.max_turnover_per_rebalance,
        "drawdown_soft_limit": risk.drawdown_soft_limit,
        "soft_limit_max_gross_exposure": risk.soft_limit_max_gross_exposure,
        "drawdown_hard_limit": risk.drawdown_hard_limit,
        "daily_loss_limit": risk.daily_loss_limit,
        "required_cash_buffer": risk.required_cash_buffer,
        "minimum_order_notional": execution.minimum_order_notional,
        "maximum_orders_per_rebalance": execution.maximum_orders_per_rebalance,
        "max_single_order_fraction_of_equity": (execution.max_single_order_fraction_of_equity),
    }


def _build_receipt(
    *,
    portfolio: str,
    signal_date: pd.Timestamp,
    execution_date: pd.Timestamp,
    regime_name: str,
    regime_metadata: dict[str, Any],
    momentum: StrategyResult,
    mean_reversion: StrategyResult,
    allocation: RebalanceDecision,
    config: AppConfig,
    transaction_cost_bps: float,
    transaction_cost_amount: float,
    slippage_bps: float,
    slippage_amount: float,
    open_prices_are_approximated: bool,
    current_weights: dict[str, float],
    current_cash: float,
    current_equity: float,
    execution_prices: pd.Series,
    market_data_source: str,
    market_data_feed: str,
    audit_run_id: str,
) -> dict[str, Any]:
    if allocation.risk_decision is None:
        raise BacktestError("A decision receipt requires an attached risk decision")
    risk = allocation.risk_decision
    cost_fraction = risk.final_turnover * transaction_cost_bps / 10_000.0
    evaluation_timestamp = pd.Timestamp(
        f"{execution_date.date()} {config.schedule.evaluation_time_et}",
        tz=config.project.timezone,
    ).isoformat()
    strategy_versions = {
        "momentum": momentum.version,
        "mean_reversion": mean_reversion.version,
        "allocator": str(allocation.metadata.get("allocator", portfolio)),
        "risk_engine": "independent-risk-engine-v1",
    }
    strategy_warnings = sorted(
        {
            str(warning)
            for result in (momentum, mean_reversion)
            for warning in result.metadata.get("warnings", [])
        }
    )
    receipt_warnings = list(strategy_warnings)
    if open_prices_are_approximated:
        receipt_warnings.append(
            "Daily opens were unavailable; prior completed closes were used as explicit "
            "session-open proxies."
        )
    execution_equity = max(
        0.0,
        current_equity - transaction_cost_amount - slippage_amount,
    )
    order_intents = _historical_order_intents(
        decision_id=str(risk.decision_id),
        current_weights=current_weights,
        final_weights=risk.final_weights,
        equity=current_equity,
        execution_prices=execution_prices,
    )
    normalized = _json_value(
        {
            "decision_id": risk.decision_id,
            "run_id": audit_run_id,
            "application_run_id": audit_run_id,
            "configuration_hash": config.configuration_hash,
            "strategy_version": strategy_versions,
            "portfolio": portfolio,
            "signal_as_of_date": signal_date,
            "historical_data_cutoff": signal_date,
            "scheduled_evaluation_timestamp": evaluation_timestamp,
            "actual_evaluation_timestamp": evaluation_timestamp,
            "market_session_date": execution_date.date().isoformat(),
            "execution_date": execution_date,
            "execution_timing": (
                "session_open_approximation_using_prior_close"
                if open_prices_are_approximated
                else "session_open_using_adjusted_daily_open"
            ),
            "market_data_provider": config.market_data.provider,
            "market_data_source": market_data_source,
            "market_data_feed": market_data_feed,
            "configured_live_feed": config.market_data.feed,
            "market_status": "historical_session",
            "data_freshness": "completed_t_minus_1_history",
            "mode": "backtest",
            "regime": regime_name,
            "regime_features": regime_metadata,
            "momentum": {
                "strategy_name": momentum.name,
                "strategy_version": momentum.version,
                "as_of_date": momentum.as_of_date,
                "selected_assets": momentum.metadata.get("selected_assets", []),
                "scores": momentum.metadata.get("scores", {}),
                "exclusions": momentum.metadata.get("exclusions", {}),
                "weights": momentum.weights,
                "cash_weight": momentum.cash_weight,
                "metadata": momentum.metadata,
            },
            "mean_reversion": {
                "strategy_name": mean_reversion.name,
                "strategy_version": mean_reversion.version,
                "as_of_date": mean_reversion.as_of_date,
                "selected_assets": mean_reversion.metadata.get("selected_assets", []),
                "scores": mean_reversion.metadata.get(
                    "scores", mean_reversion.metadata.get("z_scores", {})
                ),
                "exclusions": mean_reversion.metadata.get("exclusions", {}),
                "weights": mean_reversion.weights,
                "cash_weight": mean_reversion.cash_weight,
                "metadata": mean_reversion.metadata,
            },
            "strategy_allocations": allocation.strategy_allocations,
            "allocation_metadata": allocation.metadata,
            "simulated_portfolio_before_execution": {
                "equity": current_equity,
                "cash_weight": current_cash,
                "simulated_cash": current_equity * current_cash,
                "positions": _historical_positions(current_weights, current_equity),
            },
            "risk_inputs": {
                "current_weights": current_weights,
                "current_cash": current_cash,
                "current_drawdown": risk.current_drawdown,
                "current_daily_loss": risk.current_daily_loss,
                "hard_stop_latched": risk.hard_stop_latched,
                "daily_loss_latched": risk.daily_loss_latched,
                "halt_state": risk.halt_state,
                "data_freshness_state": risk.data_freshness_state,
                "market_state": risk.market_state,
                "historical_returns_cutoff": signal_date,
            },
            "risk_limits": _risk_limits(config),
            "proposed_asset_weights": risk.proposed_weights,
            "proposed_cash": risk.proposed_cash,
            "risk_adjusted_weights": risk.final_weights,
            "final_cash": risk.final_cash,
            "estimated_volatility": risk.estimated_volatility,
            "final_estimated_volatility": risk.final_estimated_volatility,
            "current_drawdown": risk.current_drawdown,
            "current_daily_loss": risk.current_daily_loss,
            "proposed_gross_exposure": risk.proposed_gross_exposure,
            "final_gross_exposure": risk.final_gross_exposure,
            "proposed_turnover": risk.proposed_turnover,
            "turnover": risk.final_turnover,
            "estimated_transaction_cost_fraction": cost_fraction,
            "estimated_transaction_cost_amount": transaction_cost_amount,
            "slippage_bps": slippage_bps,
            "estimated_slippage_fraction": risk.final_turnover * slippage_bps / 10_000.0,
            "estimated_slippage_amount": slippage_amount,
            "risk_interventions": [action.as_dict() for action in risk.actions],
            "risk_status": risk.status,
            "hard_stop_status": risk.hard_stop_latched,
            "daily_loss_status": risk.daily_loss_latched,
            "liquidation_authorized": risk.liquidation_authorized,
            "rejection_reasons": risk.rejection_reasons,
            "skip_reason": (
                "; ".join(risk.rejection_reasons) if risk.status == "rejected" else None
            ),
            "order_intents": order_intents,
            "broker_order_ids": [],
            "broker_order_ids_reason": "historical simulation; no Alpaca paper order existed",
            "final_known_execution_status": "historically_simulated_at_session_open",
            "paper_fills": [],
            "paper_fills_reason": "historical simulation; Alpaca paper fills are not modeled",
            "simulated_portfolio_after_execution": {
                "equity_after_estimated_costs": execution_equity,
                "cash_weight": risk.final_cash,
                "simulated_cash": execution_equity * risk.final_cash,
                "positions": _historical_positions(risk.final_weights, execution_equity),
            },
            "incidents": [],
            "warnings": receipt_warnings,
        }
    )
    if not isinstance(normalized, dict):
        raise BacktestError("Decision receipt normalization did not return an object")
    return normalized


def _drift_weights(
    weights: dict[str, float],
    cash_weight: float,
    day_returns: pd.Series,
) -> tuple[dict[str, float], float, float]:
    """Apply one day of returns and return drifted weights and gross portfolio return."""

    usable_returns: dict[str, float] = {}
    for ticker, weight in weights.items():
        value = day_returns.get(ticker, np.nan)
        if weight > 1e-12 and (pd.isna(value) or not np.isfinite(float(value))):
            raise BacktestError(f"Missing/nonfinite return for held asset {ticker!r}")
        usable_returns[ticker] = 0.0 if pd.isna(value) else float(value)
    portfolio_return = float(sum(weights[ticker] * usable_returns[ticker] for ticker in weights))
    growth = 1.0 + portfolio_return
    if not np.isfinite(growth) or growth <= 0.0:
        raise BacktestError(f"Invalid portfolio growth factor: {growth}")
    drifted = {
        ticker: weight * (1.0 + usable_returns[ticker]) / growth
        for ticker, weight in weights.items()
    }
    drifted_cash = cash_weight / growth
    total = sum(drifted.values()) + drifted_cash
    if not np.isfinite(total) or total <= 0.0:
        raise BacktestError("Drifted portfolio weights are unusable")
    # Normalization removes only floating-point residue, not economic exposure.
    drifted = {ticker: max(0.0, weight / total) for ticker, weight in drifted.items()}
    drifted_cash = max(0.0, drifted_cash / total)
    return drifted, drifted_cash, portfolio_return


def _run_dynamic_portfolio(
    name: str,
    prices: pd.DataFrame,
    opens: pd.DataFrame,
    returns: pd.DataFrame,
    execution_dates: pd.DatetimeIndex,
    config: AppConfig,
    *,
    open_prices_are_approximated: bool,
    market_data_source: str,
    market_data_feed: str,
    audit_run_id: str,
) -> BacktestResult:
    universe = list(config.data.tickers)
    momentum_strategy = MomentumStrategy(config)
    mean_reversion_strategy = MeanReversionStrategy(config)
    regime_detector = RegimeDetector(config)
    risk_engine = RiskEngine(config)
    adaptive_allocator = AdaptiveAllocator(config)
    static_allocator = StaticAllocator()

    equity = float(config.backtest.initial_capital)
    peak = equity
    current_drawdown = 0.0
    current_weights = {ticker: 0.0 for ticker in universe}
    current_cash = 1.0
    hard_stop_latched = False
    previous_execution_date: pd.Timestamp | None = None

    daily_rows: list[dict[str, Any]] = []
    weight_rows: list[dict[str, Any]] = []
    rebalance_rows: list[dict[str, Any]] = []
    allocation_rows: list[dict[str, Any]] = []
    regime_dates: list[pd.Timestamp] = []
    regime_values: list[str] = []
    risk_actions: list[RiskAction] = []
    receipts: list[dict[str, Any]] = []

    for execution_date in execution_dates:
        location = prices.index.get_loc(execution_date)
        if not isinstance(location, int) or location <= 0:
            raise BacktestError("Execution dates must have a previous trading observation")
        signal_date = prices.index[location - 1]
        turnover = 0.0
        transaction_cost_fraction = 0.0
        transaction_cost_amount = 0.0
        slippage_fraction = 0.0
        slippage_amount = 0.0
        equity_before_day = equity

        # Holdings carried from t-1 close experience the overnight move before
        # the session-t open rebalance. This prevents a newly selected portfolio
        # from receiving a return that occurred before its modeled execution.
        overnight_returns = (
            opens.loc[execution_date, universe] / prices.loc[signal_date, universe] - 1.0
        )
        current_weights, current_cash, overnight_return = _drift_weights(
            current_weights,
            current_cash,
            overnight_returns,
        )
        equity *= 1.0 + overnight_return
        open_drawdown = equity / peak - 1.0
        current_session_loss = min(0.0, equity / equity_before_day - 1.0)

        if _is_rebalance_date(
            execution_date,
            previous_execution_date,
            config.backtest.rebalance_frequency,
        ):
            # Every object receives a history explicitly truncated at t-1.
            historical_prices = prices.loc[:signal_date, universe]
            historical_returns = returns.loc[:signal_date, universe]
            momentum = momentum_strategy.generate(
                prices=historical_prices,
                returns=historical_returns,
                as_of_date=signal_date,
            )
            mean_reversion = mean_reversion_strategy.generate(
                prices=historical_prices,
                returns=historical_returns,
                as_of_date=signal_date,
            )
            regime = regime_detector.detect(
                prices=prices.loc[:signal_date],
                returns=returns.loc[:signal_date],
                as_of_date=signal_date,
            )
            if name == "adaptive":
                allocation = adaptive_allocator.allocate(regime, momentum, mean_reversion)
            elif name == "static_blend":
                allocation = static_allocator.allocate(regime, momentum, mean_reversion)
            elif name == "momentum_only":
                allocation = _single_strategy_allocation(
                    regime.name, momentum, signal_date, "momentum"
                )
            elif name == "mean_reversion_only":
                allocation = _single_strategy_allocation(
                    regime.name, mean_reversion, signal_date, "mean_reversion"
                )
            else:  # pragma: no cover - protected by the suite builder
                raise BacktestError(f"Unknown dynamic portfolio: {name}")

            decision_weights = dict(current_weights)
            decision_cash = current_cash
            decision_equity = equity
            risk = risk_engine.evaluate(
                proposed_weights=allocation.pre_risk_weights,
                current_weights=current_weights,
                historical_returns=historical_returns,
                current_drawdown=open_drawdown,
                hard_stop_latched=hard_stop_latched,
                current_daily_loss=current_session_loss,
                preserve_current_on_rejection=True,
                decision_id=_historical_decision_id(
                    config,
                    name,
                    execution_date,
                    audit_run_id,
                ),
            )
            allocation.execution_date = execution_date
            allocation.risk_decision = risk
            turnover = risk.final_turnover
            transaction_cost_fraction = (
                turnover * float(config.backtest.transaction_cost_bps) / 10_000.0
            )
            slippage_fraction = turnover * float(config.backtest.slippage_bps) / 10_000.0
            total_execution_cost_fraction = transaction_cost_fraction + slippage_fraction
            if total_execution_cost_fraction >= 1.0:
                raise BacktestError("Transaction costs and slippage consumed all portfolio equity")
            transaction_cost_amount = equity * transaction_cost_fraction
            slippage_amount = equity * slippage_fraction
            equity -= transaction_cost_amount + slippage_amount
            current_weights = {
                ticker: max(0.0, float(risk.final_weights.get(ticker, 0.0))) for ticker in universe
            }
            current_cash = max(0.0, float(risk.final_cash))
            hard_stop_latched = risk.hard_stop_latched

            for action in risk.actions:
                details = {
                    **action.details,
                    "portfolio": name,
                    "signal_as_of_date": signal_date.isoformat(),
                    "execution_date": execution_date.isoformat(),
                }
                risk_actions.append(
                    RiskAction(
                        control=action.control,
                        description=action.description,
                        details=_json_value(details),
                    )
                )
            receipt = _build_receipt(
                portfolio=name,
                signal_date=signal_date,
                execution_date=execution_date,
                regime_name=regime.name,
                regime_metadata=regime.metadata,
                momentum=momentum,
                mean_reversion=mean_reversion,
                allocation=allocation,
                config=config,
                transaction_cost_bps=float(config.backtest.transaction_cost_bps),
                transaction_cost_amount=transaction_cost_amount,
                slippage_bps=float(config.backtest.slippage_bps),
                slippage_amount=slippage_amount,
                open_prices_are_approximated=open_prices_are_approximated,
                current_weights=decision_weights,
                current_cash=decision_cash,
                current_equity=decision_equity,
                execution_prices=opens.loc[execution_date, universe],
                market_data_source=market_data_source,
                market_data_feed=market_data_feed,
                audit_run_id=audit_run_id,
            )
            receipts.append(receipt)
            rebalance_rows.append(
                {
                    "portfolio": name,
                    "signal_as_of_date": signal_date,
                    "execution_date": execution_date,
                    "regime": regime.name,
                    "risk_status": risk.status,
                    "proposed_turnover": risk.proposed_turnover,
                    "turnover": turnover,
                    "transaction_cost_fraction": transaction_cost_fraction,
                    "transaction_cost_amount": transaction_cost_amount,
                    "slippage_fraction": slippage_fraction,
                    "slippage_amount": slippage_amount,
                    "total_execution_cost_fraction": (
                        transaction_cost_fraction + slippage_fraction
                    ),
                    "estimated_volatility": risk.estimated_volatility,
                    "current_drawdown": risk.current_drawdown,
                    "hard_stop_latched": hard_stop_latched,
                }
            )
            allocation_rows.append(
                {
                    "date": execution_date,
                    **allocation.strategy_allocations,
                }
            )
            regime_dates.append(execution_date)
            regime_values.append(regime.name)

        intraday_returns = (
            prices.loc[execution_date, universe] / opens.loc[execution_date, universe] - 1.0
        )
        current_weights, current_cash, intraday_return = _drift_weights(
            current_weights, current_cash, intraday_returns
        )
        equity *= 1.0 + intraday_return
        if not np.isfinite(equity) or equity <= 0.0:
            raise BacktestError(f"Portfolio equity became invalid on {execution_date.date()}")
        peak = max(peak, equity)
        current_drawdown = equity / peak - 1.0
        total_daily_return = equity / equity_before_day - 1.0
        asset_return_before_cost = (1.0 + overnight_return) * (1.0 + intraday_return) - 1.0
        daily_rows.append(
            {
                "date": execution_date,
                "equity": equity,
                "daily_return": total_daily_return,
                "asset_return_before_cost": asset_return_before_cost,
                "overnight_return_before_cost": overnight_return,
                "intraday_return_before_cost": intraday_return,
                "drawdown": current_drawdown,
                "gross_exposure": sum(current_weights.values()),
                "cash_weight": current_cash,
                "turnover": turnover,
                "transaction_cost": transaction_cost_fraction,
                "transaction_cost_amount": transaction_cost_amount,
                "slippage": slippage_fraction,
                "slippage_amount": slippage_amount,
                "total_execution_cost": transaction_cost_fraction + slippage_fraction,
                "hard_stop_latched": hard_stop_latched,
            }
        )
        weight_rows.append(
            {
                "date": execution_date,
                **current_weights,
                "cash": current_cash,
            }
        )
        previous_execution_date = execution_date

    daily = pd.DataFrame(daily_rows).set_index("date")
    weights = pd.DataFrame(weight_rows).set_index("date")
    rebalances = rebalance_rows
    if allocation_rows:
        strategy_allocations = pd.DataFrame(allocation_rows).set_index("date")
    else:
        strategy_allocations = pd.DataFrame(
            columns=["momentum", "mean_reversion", "strategic_cash"]
        )
    regimes = pd.Series(regime_values, index=regime_dates, name="regime", dtype="object")
    return BacktestResult(
        name=name,
        daily=daily,
        weights=weights,
        rebalances=rebalances,
        strategy_allocations=strategy_allocations,
        regimes=regimes,
        risk_actions=risk_actions,
        decision_receipts=receipts,
        metadata={
            "uses_risk_engine": True,
            "rebalance_rule": (
                "every available market session"
                if config.backtest.rebalance_frequency == "daily"
                else "first available trading day of each ISO week"
            ),
            "execution_timing": (
                "prior-close proxy for session open"
                if open_prices_are_approximated
                else "corporate-action-consistent session open"
            ),
            "transaction_cost_bps": float(config.backtest.transaction_cost_bps),
            "slippage_bps": float(config.backtest.slippage_bps),
        },
    )


def _run_buy_and_hold(
    name: str,
    target_weights: dict[str, float],
    prices: pd.DataFrame,
    opens: pd.DataFrame,
    returns: pd.DataFrame,
    execution_dates: pd.DatetimeIndex,
    config: AppConfig,
    *,
    open_prices_are_approximated: bool,
    market_data_source: str,
    market_data_feed: str,
    audit_run_id: str,
) -> BacktestResult:
    universe = list(config.data.tickers)
    intended_weights = {ticker: float(target_weights.get(ticker, 0.0)) for ticker in universe}
    intended_cash = max(0.0, 1.0 - sum(intended_weights.values()))
    current_weights = {ticker: 0.0 for ticker in universe}
    current_cash = 1.0
    starting_cash_weights = {ticker: 0.0 for ticker in universe}
    initial_turnover = calculate_turnover(intended_weights, starting_cash_weights)
    cost_fraction = initial_turnover * float(config.backtest.transaction_cost_bps) / 10_000.0
    slippage_fraction = initial_turnover * float(config.backtest.slippage_bps) / 10_000.0
    initial_equity = float(config.backtest.initial_capital)
    equity = initial_equity
    cost_amount = initial_equity * cost_fraction
    slippage_amount = initial_equity * slippage_fraction
    peak = initial_equity
    daily_rows: list[dict[str, Any]] = []
    weight_rows: list[dict[str, Any]] = []

    for offset, execution_date in enumerate(execution_dates):
        equity_before_day = equity
        turnover = initial_turnover if offset == 0 else 0.0
        transaction_cost = cost_fraction if offset == 0 else 0.0
        transaction_cost_amount = cost_amount if offset == 0 else 0.0
        day_slippage = slippage_fraction if offset == 0 else 0.0
        day_slippage_amount = slippage_amount if offset == 0 else 0.0

        if offset == 0:
            overnight_return = 0.0
            equity -= cost_amount + slippage_amount
            current_weights = dict(intended_weights)
            current_cash = intended_cash
        else:
            location = prices.index.get_loc(execution_date)
            if not isinstance(location, int) or location <= 0:
                raise BacktestError("Buy-and-hold execution date lacks a previous close")
            previous_close_date = prices.index[location - 1]
            overnight_returns = (
                opens.loc[execution_date, universe] / prices.loc[previous_close_date, universe]
                - 1.0
            )
            current_weights, current_cash, overnight_return = _drift_weights(
                current_weights, current_cash, overnight_returns
            )
            equity *= 1.0 + overnight_return

        intraday_returns = (
            prices.loc[execution_date, universe] / opens.loc[execution_date, universe] - 1.0
        )
        current_weights, current_cash, intraday_return = _drift_weights(
            current_weights,
            current_cash,
            intraday_returns,
        )
        equity *= 1.0 + intraday_return
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        asset_return = (1.0 + overnight_return) * (1.0 + intraday_return) - 1.0
        daily_rows.append(
            {
                "date": execution_date,
                "equity": equity,
                "daily_return": equity / equity_before_day - 1.0,
                "asset_return_before_cost": asset_return,
                "overnight_return_before_cost": overnight_return,
                "intraday_return_before_cost": intraday_return,
                "drawdown": drawdown,
                "gross_exposure": sum(current_weights.values()),
                "cash_weight": current_cash,
                "turnover": turnover,
                "transaction_cost": transaction_cost,
                "transaction_cost_amount": transaction_cost_amount,
                "slippage": day_slippage,
                "slippage_amount": day_slippage_amount,
                "total_execution_cost": transaction_cost + day_slippage,
                "hard_stop_latched": False,
            }
        )
        weight_rows.append({"date": execution_date, **current_weights, "cash": current_cash})

    first_date = execution_dates[0]
    signal_date = prices.index[prices.index.get_loc(first_date) - 1]
    evaluation_timestamp = pd.Timestamp(
        f"{first_date.date()} {config.schedule.evaluation_time_et}",
        tz=config.project.timezone,
    ).isoformat()
    decision_id = _historical_decision_id(config, name, first_date, audit_run_id)
    executed_equity = max(0.0, initial_equity - cost_amount - slippage_amount)
    receipt = _json_value(
        {
            "decision_id": decision_id,
            "run_id": audit_run_id,
            "application_run_id": audit_run_id,
            "configuration_hash": config.configuration_hash,
            "strategy_version": {
                "portfolio": "buy-and-hold-v1",
                "risk_engine": "not_applied_to_passive_baseline",
            },
            "portfolio": name,
            "signal_as_of_date": signal_date,
            "historical_data_cutoff": signal_date,
            "scheduled_evaluation_timestamp": evaluation_timestamp,
            "actual_evaluation_timestamp": evaluation_timestamp,
            "market_session_date": first_date.date().isoformat(),
            "execution_date": first_date,
            "execution_timing": (
                "session_open_approximation_using_prior_close"
                if open_prices_are_approximated
                else "session_open_using_adjusted_daily_open"
            ),
            "market_data_provider": config.market_data.provider,
            "market_data_source": market_data_source,
            "market_data_feed": market_data_feed,
            "configured_live_feed": config.market_data.feed,
            "market_status": "historical_session",
            "data_freshness": "completed_t_minus_1_history",
            "mode": "backtest",
            "regime": "not_applicable",
            "regime_features": {},
            "momentum": {
                "strategy_version": None,
                "selected_assets": [],
                "scores": {},
                "weights": {},
                "metadata": {},
                "not_applicable_reason": "passive buy-and-hold baseline",
            },
            "mean_reversion": {
                "strategy_version": None,
                "selected_assets": [],
                "scores": {},
                "weights": {},
                "metadata": {},
                "not_applicable_reason": "passive buy-and-hold baseline",
            },
            "strategy_allocations": {"buy_and_hold": 1.0},
            "allocation_metadata": {"rule": "initial purchase; holdings drift thereafter"},
            "simulated_portfolio_before_execution": {
                "equity": initial_equity,
                "cash_weight": 1.0,
                "simulated_cash": initial_equity,
                "positions": [],
            },
            "risk_inputs": {
                "current_weights": starting_cash_weights,
                "current_cash": 1.0,
                "current_drawdown": 0.0,
                "current_daily_loss": 0.0,
                "hard_stop_latched": False,
                "daily_loss_latched": False,
                "halt_state": "not_applicable",
                "data_freshness_state": "completed_t_minus_1_history",
                "market_state": "historical_session",
                "historical_returns_cutoff": signal_date,
                "not_applied_reason": "passive baseline intentionally bypasses adaptive risk",
            },
            "risk_limits": _risk_limits(config),
            "proposed_asset_weights": target_weights,
            "proposed_cash": 1.0 - sum(target_weights.values()),
            "risk_adjusted_weights": target_weights,
            "final_cash": 1.0 - sum(target_weights.values()),
            "estimated_volatility": None,
            "final_estimated_volatility": None,
            "current_drawdown": 0.0,
            "current_daily_loss": 0.0,
            "proposed_gross_exposure": sum(target_weights.values()),
            "final_gross_exposure": sum(target_weights.values()),
            "proposed_turnover": initial_turnover,
            "turnover": initial_turnover,
            "estimated_transaction_cost_fraction": cost_fraction,
            "estimated_transaction_cost_amount": cost_amount,
            "slippage_bps": float(config.backtest.slippage_bps),
            "estimated_slippage_fraction": slippage_fraction,
            "estimated_slippage_amount": slippage_amount,
            "risk_interventions": [],
            "risk_status": "not_applicable",
            "hard_stop_status": False,
            "daily_loss_status": False,
            "liquidation_authorized": False,
            "rejection_reasons": [],
            "skip_reason": None,
            "order_intents": _historical_order_intents(
                decision_id=decision_id,
                current_weights=starting_cash_weights,
                final_weights=intended_weights,
                equity=initial_equity,
                execution_prices=opens.loc[first_date, universe],
            ),
            "broker_order_ids": [],
            "broker_order_ids_reason": "historical simulation; no Alpaca paper order existed",
            "final_known_execution_status": "historically_simulated_at_session_open",
            "paper_fills": [],
            "paper_fills_reason": "historical simulation; Alpaca paper fills are not modeled",
            "simulated_portfolio_after_execution": {
                "equity_after_estimated_costs": executed_equity,
                "cash_weight": intended_cash,
                "simulated_cash": executed_equity * intended_cash,
                "positions": _historical_positions(intended_weights, executed_equity),
            },
            "incidents": [],
            "warnings": (
                [
                    "Daily opens were unavailable; prior completed closes were used as explicit "
                    "session-open proxies."
                ]
                if open_prices_are_approximated
                else []
            ),
        }
    )
    rebalance = {
        "portfolio": name,
        "signal_as_of_date": signal_date,
        "execution_date": first_date,
        "regime": "not_applicable",
        "risk_status": "not_applicable",
        "proposed_turnover": initial_turnover,
        "turnover": initial_turnover,
        "transaction_cost_fraction": cost_fraction,
        "transaction_cost_amount": cost_amount,
        "slippage_fraction": slippage_fraction,
        "slippage_amount": slippage_amount,
        "total_execution_cost_fraction": cost_fraction + slippage_fraction,
        "estimated_volatility": np.nan,
        "current_drawdown": 0.0,
        "hard_stop_latched": False,
    }
    return BacktestResult(
        name=name,
        daily=pd.DataFrame(daily_rows).set_index("date"),
        weights=pd.DataFrame(weight_rows).set_index("date"),
        rebalances=[rebalance],
        strategy_allocations=pd.DataFrame(),
        regimes=pd.Series(dtype="object", name="regime"),
        risk_actions=[],
        decision_receipts=[receipt],
        metadata={
            "uses_risk_engine": False,
            "rebalance_rule": "initial purchase only; holdings drift thereafter",
            "execution_timing": (
                "prior-close proxy for session open"
                if open_prices_are_approximated
                else "corporate-action-consistent session open"
            ),
            "transaction_cost_bps": float(config.backtest.transaction_cost_bps),
            "slippage_bps": float(config.backtest.slippage_bps),
        },
    )


def run_backtest_suite(
    config: AppConfig,
    market_data: MarketData,
    *,
    audit_run_id: str | None = None,
) -> BacktestSuiteResult:
    """Run adaptive, static, single-strategy, and buy-and-hold comparisons.

    The first simulated day follows a warm-up period large enough for every configured
    lookback. For every execution on date ``t``, all signals and risk estimates are cut off
    at the immediately preceding trading date ``t-1``.
    """

    universe = list(config.data.tickers)
    receipt_run_id = audit_run_id or config.project.run_name
    validate_market_data(
        market_data,
        list(dict.fromkeys([*universe, config.data.benchmark, config.regime.benchmark])),
        minimum_history=required_history_from_config(config) + 1,
    )
    prices = market_data.prices.sort_index().copy()
    if market_data.opens is None:
        raise BacktestError("Historical market data has no usable daily-open series")
    opens = market_data.opens.sort_index().copy()
    start = pd.Timestamp(config.data.start_date)
    end = pd.Timestamp(config.data.end_date) if config.data.end_date else prices.index.max()
    prices = prices.loc[prices.index <= end]
    opens = opens.reindex(index=prices.index, columns=prices.columns)
    if len(prices) <= required_history_from_config(config):
        raise BacktestError(
            "Not enough observations remain through the configured end date after applying "
            f"lookbacks; received {len(prices)}"
        )
    returns = prices.pct_change(fill_method=None)
    warmup = required_history_from_config(config)
    warmup_dates = prices.index[warmup:]
    execution_dates = warmup_dates[warmup_dates >= start]
    if execution_dates.empty:
        raise BacktestError("No execution dates remain after the warm-up period")
    LOGGER.info(
        "Running six portfolios from %s through %s after %d warm-up observations",
        execution_dates[0].date(),
        execution_dates[-1].date(),
        warmup,
    )

    runs: dict[str, BacktestResult] = {}
    for name in ("adaptive", "static_blend", "momentum_only", "mean_reversion_only"):
        runs[name] = _run_dynamic_portfolio(
            name=name,
            prices=prices,
            opens=opens,
            returns=returns,
            execution_dates=execution_dates,
            config=config,
            open_prices_are_approximated=market_data.open_prices_are_approximated,
            market_data_source=market_data.source,
            market_data_feed=market_data.feed,
            audit_run_id=receipt_run_id,
        )

    equal_weight = 1.0 / len(universe)
    runs["equal_weight_buy_hold"] = _run_buy_and_hold(
        "equal_weight_buy_hold",
        {ticker: equal_weight for ticker in universe},
        prices,
        opens,
        returns,
        execution_dates,
        config,
        open_prices_are_approximated=market_data.open_prices_are_approximated,
        market_data_source=market_data.source,
        market_data_feed=market_data.feed,
        audit_run_id=receipt_run_id,
    )
    runs["benchmark_buy_hold"] = _run_buy_and_hold(
        "benchmark_buy_hold",
        {config.data.benchmark: 1.0},
        prices,
        opens,
        returns,
        execution_dates,
        config,
        open_prices_are_approximated=market_data.open_prices_are_approximated,
        market_data_source=market_data.source,
        market_data_feed=market_data.feed,
        audit_run_id=receipt_run_id,
    )
    return BacktestSuiteResult(runs=runs, market_data=market_data, config=config)


def run_pipeline_from_config(
    config: AppConfig,
    market_data: MarketData,
    output_directory: str | Path | None = None,
) -> tuple[BacktestSuiteResult, dict[str, Path]]:
    """Run the backtest suite and generate all configured output artifacts."""

    from adaptive_trader.reporting import generate_outputs

    suite = run_backtest_suite(config, market_data)
    artifacts = generate_outputs(suite, output_directory=output_directory)
    return suite, artifacts
