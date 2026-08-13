"""Broker-independent forward portfolio target generation.

The callable in this module is deliberately limited to official/provider daily
bars plus already-observed account and position snapshots.  It does not import
or access a broker, order planner, order manager, or persistence repository.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
import pandas as pd

from adaptive_trader.allocator import AdaptiveAllocator
from adaptive_trader.clock import as_utc
from adaptive_trader.constants import NEW_YORK, UTC
from adaptive_trader.data import required_history_from_config
from adaptive_trader.features import calculate_returns
from adaptive_trader.live_models import MarketBar
from adaptive_trader.regimes import RegimeDetector
from adaptive_trader.risk import RiskContext, RiskEngine
from adaptive_trader.strategies.mean_reversion import MeanReversionStrategy
from adaptive_trader.strategies.momentum import MomentumStrategy

if TYPE_CHECKING:
    from adaptive_trader.config import AppConfig
    from adaptive_trader.live_models import AccountState, PositionState
    from adaptive_trader.market_data_live import MarketDataProvider
    from adaptive_trader.models import RebalanceDecision, RegimeState, RiskDecision, StrategyResult


class ForwardDecisionError(ValueError):
    """A forward target could not be produced from trustworthy completed data."""


def _freeze(value: Any) -> Any:
    """Recursively make metadata containers read-only and deterministic."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_freeze(item) for item in sorted(value, key=repr))
    if isinstance(value, np.generic):
        return value.item()
    return value


def _float_map(values: Mapping[str, float]) -> Mapping[str, float]:
    """Return a sorted, finite, read-only float mapping."""

    result = {str(symbol): float(weight) for symbol, weight in sorted(values.items())}
    if any(not isfinite(weight) for weight in result.values()):
        raise ForwardDecisionError("Portfolio weights must be finite")
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class ForwardDecisionMetadata(Mapping[str, Any]):
    """Immutable audit metadata retained for the most recent engine invocation."""

    status: str
    evaluated_at: datetime
    current_session: date
    cutoff: date | None
    provider_feed: str
    history_observations: int
    bars_received: int
    bars_excluded_as_incomplete: int
    current_weights: Mapping[str, float] | None
    strategy_outputs: Mapping[str, Any]
    regime: Mapping[str, Any] | None
    allocation: Mapping[str, Any] | None
    risk_actions: tuple[Mapping[str, Any], ...]
    risk_decision: Mapping[str, Any] | None
    final_target: Mapping[str, float] | None
    error: str | None = None

    _FIELDS: ClassVar[tuple[str, ...]] = (
        "status",
        "evaluated_at",
        "current_session",
        "cutoff",
        "provider_feed",
        "history_observations",
        "bars_received",
        "bars_excluded_as_incomplete",
        "current_weights",
        "strategy_outputs",
        "regime",
        "allocation",
        "risk_actions",
        "risk_decision",
        "final_target",
        "error",
    )

    def __getitem__(self, key: str) -> Any:
        if key not in self._FIELDS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._FIELDS)

    def __len__(self) -> int:
        return len(self._FIELDS)


@dataclass(frozen=True, slots=True)
class _CompletedHistory:
    prices: pd.DataFrame
    cutoff: date
    bars_received: int
    bars_excluded_as_incomplete: int


class ForwardDecisionEngine:
    """Create a causal, risk-controlled target for ``LiveService``.

    Instances are directly callable with the live ``TargetProvider`` contract:
    ``(now, account, positions) -> Mapping[str, float]``.  The injected market
    data provider is used only for adjusted daily history.  Current-session and
    future bars are discarded even if a provider incorrectly returns them.
    """

    def __init__(self, config: AppConfig, market_data: MarketDataProvider) -> None:
        self.config = config
        self.market_data = market_data
        self.universe = tuple(str(symbol).strip().upper() for symbol in config.data.tickers)
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ValueError("ForwardDecisionEngine requires a unique, nonempty universe")
        if config.regime.benchmark not in self.universe:
            raise ValueError("The configured regime benchmark must be in the universe")
        if not callable(getattr(market_data, "get_bars", None)):
            raise TypeError("market_data must implement MarketDataProvider.get_bars")

        self.minimum_history = required_history_from_config(config)
        self._momentum = MomentumStrategy(config)
        self._mean_reversion = MeanReversionStrategy(config)
        self._regime_detector = RegimeDetector(config)
        self._allocator = AdaptiveAllocator(config)
        self._risk = RiskEngine(config)
        self._last_metadata: ForwardDecisionMetadata | None = None
        self._live_risk_context: dict[str, Any] = {}

    @property
    def last_metadata(self) -> ForwardDecisionMetadata | None:
        """Return the immutable audit record from the most recent invocation."""

        return self._last_metadata

    def set_live_risk_context(self, **context: Any) -> None:
        """Accept broker-independent operational facts supplied by the orchestrator."""

        self._live_risk_context = {str(key): value for key, value in context.items()}

    def preflight_history(self, now: datetime) -> _CompletedHistory:
        """Validate sufficient completed daily history without producing a decision."""

        evaluated_at = as_utc(now, field="ForwardDecisionEngine.preflight_history.now")
        current_session = evaluated_at.astimezone(NEW_YORK).date()
        return self._completed_history(evaluated_at, current_session)

    def __call__(
        self,
        now: datetime,
        account: AccountState,
        positions: Sequence[PositionState],
    ) -> Mapping[str, float]:
        """Return a finite long-only target based strictly on completed sessions."""

        evaluated_at = as_utc(now, field="ForwardDecisionEngine.now")
        current_session = evaluated_at.astimezone(NEW_YORK).date()
        feed = str(getattr(self.market_data, "feed", "unknown")).strip().upper() or "UNKNOWN"
        current_weights: dict[str, float] | None = None
        history: _CompletedHistory | None = None

        try:
            current_weights, daily_loss = self._current_weights(account, positions)
            history = self._completed_history(evaluated_at, current_session)
            returns = calculate_returns(history.prices)
            signal_date = history.prices.index[-1]

            momentum = self._momentum.generate(
                history.prices,
                returns,
                as_of_date=signal_date,
            )
            mean_reversion = self._mean_reversion.generate(
                history.prices,
                returns,
                as_of_date=signal_date,
            )
            regime = self._regime_detector.detect(
                history.prices,
                returns,
                as_of_date=signal_date,
            )
            allocation = self._allocator.allocate(regime, momentum, mean_reversion)
            decision_id = (
                f"forward-{current_session.isoformat()}-{str(self.config.configuration_hash)[:12]}"
            )
            live_context = dict(self._live_risk_context)
            halt_latched = bool(live_context.get("halt_latched", False))
            raw_positions = live_context.get("positions", ())
            raw_open_orders = live_context.get("open_orders", ())
            raw_current_prices = live_context.get("current_prices")
            raw_price_timestamps = live_context.get("current_price_timestamps")
            raw_asset_eligibility = live_context.get("asset_eligibility")
            raw_asset_metadata = live_context.get("asset_metadata")
            risk_context = RiskContext(
                account_equity=float(account.equity),
                account_cash=float(account.cash),
                account_timestamp=live_context.get(
                    "account_timestamp", account.timestamp.isoformat()
                ),
                position_weights=current_weights,
                positions=tuple(
                    dict(position) for position in raw_positions if isinstance(position, Mapping)
                ),
                open_order_symbols=tuple(
                    str(symbol) for symbol in live_context.get("open_order_symbols", ())
                ),
                open_orders=tuple(
                    dict(order) for order in raw_open_orders if isinstance(order, Mapping)
                ),
                current_prices=(
                    raw_current_prices if isinstance(raw_current_prices, Mapping) else None
                ),
                current_price_timestamps=(
                    raw_price_timestamps if isinstance(raw_price_timestamps, Mapping) else None
                ),
                asset_eligibility=(
                    raw_asset_eligibility if isinstance(raw_asset_eligibility, Mapping) else None
                ),
                asset_metadata=(
                    raw_asset_metadata if isinstance(raw_asset_metadata, Mapping) else None
                ),
                market_timestamp=live_context.get("market_timestamp"),
                next_market_open=live_context.get("next_market_open"),
                next_market_close=live_context.get("next_market_close"),
                evaluation_cutoff=live_context.get("evaluation_cutoff"),
                data_freshness_state=str(
                    live_context.get("data_freshness_state", "completed_daily_history")
                ),
                market_state=str(live_context.get("market_state", "submission_gate_pending")),
                halt_state=(
                    str(live_context.get("halt_state", "latched")) if halt_latched else "clear"
                ),
            )
            risk = self._risk.evaluate(
                proposed_weights=allocation.pre_risk_weights,
                current_weights=current_weights,
                historical_returns=returns,
                current_drawdown=float(live_context.get("current_drawdown", 0.0)),
                hard_stop_latched=bool(live_context.get("hard_stop_latched", False)),
                current_daily_loss=float(live_context.get("current_daily_loss", daily_loss)),
                daily_loss_latched=bool(live_context.get("daily_loss_latched", False)),
                halt_latched=halt_latched,
                preserve_current_on_rejection=True,
                decision_id=decision_id,
                context=risk_context,
            )
            final_target = self._validated_target(risk.final_weights)
            self._last_metadata = self._success_metadata(
                evaluated_at=evaluated_at,
                current_session=current_session,
                feed=feed,
                history=history,
                current_weights=current_weights,
                momentum=momentum,
                mean_reversion=mean_reversion,
                regime=regime,
                allocation=allocation,
                risk=risk,
                final_target=final_target,
            )
            return dict(final_target)
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            self._last_metadata = self._rejection_metadata(
                evaluated_at=evaluated_at,
                current_session=current_session,
                feed=feed,
                current_weights=current_weights,
                history=history,
                reason=reason,
            )
            if isinstance(exc, ForwardDecisionError):
                raise
            raise ForwardDecisionError(f"Forward decision rejected: {reason}") from exc

    def _completed_history(
        self,
        evaluated_at: datetime,
        current_session: date,
    ) -> _CompletedHistory:
        session_start_et = datetime.combine(current_session, time.min, tzinfo=NEW_YORK)
        request_end = session_start_et.astimezone(UTC)
        request_start = request_end - timedelta(
            days=int(self.config.market_data.historical_calendar_days)
        )
        supplied = tuple(
            self.market_data.get_bars(
                self.universe,
                start=request_start,
                end=request_end,
                timeframe="day",
            )
        )
        rows: list[dict[str, Any]] = []
        excluded = 0
        seen: set[tuple[date, str]] = set()
        allowed = set(self.universe)
        for bar in supplied:
            if not isinstance(bar, MarketBar):
                raise ForwardDecisionError(
                    "MarketDataProvider.get_bars must return MarketBar records"
                )
            if bar.symbol not in allowed:
                continue
            session = bar.start.astimezone(NEW_YORK).date()
            if session >= current_session:
                excluded += 1
                continue
            key = (session, bar.symbol)
            if key in seen:
                raise ForwardDecisionError(
                    f"Duplicate daily bar for {bar.symbol} on {session.isoformat()}"
                )
            seen.add(key)
            close = float(bar.close)
            if not isfinite(close) or close <= 0.0:
                raise ForwardDecisionError(
                    f"Nonfinite or nonpositive daily close for {bar.symbol} on {session}"
                )
            rows.append({"session": pd.Timestamp(session), "symbol": bar.symbol, "close": close})

        if not rows:
            raise ForwardDecisionError("No completed daily bars are available before this session")
        prices = (
            pd.DataFrame.from_records(rows)
            .pivot(index="session", columns="symbol", values="close")
            .sort_index()
            .reindex(columns=list(self.universe))
        )
        if len(prices) < self.minimum_history:
            raise ForwardDecisionError(
                "Insufficient completed daily history: "
                f"need {self.minimum_history} sessions, have {len(prices)}"
            )
        prices = prices.tail(self.minimum_history).astype(float)
        missing = prices.isna()
        if missing.to_numpy().any():
            locations = [
                f"{prices.columns[column]}@{prices.index[row].date().isoformat()}"
                for row, column in zip(*np.where(missing.to_numpy()), strict=True)
            ]
            preview = ", ".join(locations[:5])
            raise ForwardDecisionError(f"Missing required completed daily bars: {preview}")
        matrix = prices.to_numpy(dtype=float)
        if not np.isfinite(matrix).all() or (matrix <= 0.0).any():
            raise ForwardDecisionError("Completed daily price history must be positive and finite")
        cutoff = prices.index[-1].date()
        if cutoff >= current_session:
            raise ForwardDecisionError(
                "Completed-history cutoff is not strictly before the session"
            )
        return _CompletedHistory(
            prices=prices,
            cutoff=cutoff,
            bars_received=len(supplied),
            bars_excluded_as_incomplete=excluded,
        )

    def _current_weights(
        self,
        account: AccountState,
        positions: Sequence[PositionState],
    ) -> tuple[dict[str, float], float]:
        try:
            equity = float(account.equity)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ForwardDecisionError("Account equity is unavailable or invalid") from exc
        if not isfinite(equity) or equity <= 0.0:
            raise ForwardDecisionError("Account equity must be positive and finite")

        weights = {symbol: 0.0 for symbol in self.universe}
        seen: set[str] = set()
        for position in positions:
            try:
                symbol = str(position.symbol).strip().upper()
                market_value = float(position.market_value)
                quantity = float(position.quantity)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ForwardDecisionError("Position values are unavailable or invalid") from exc
            if symbol in seen:
                raise ForwardDecisionError(f"Duplicate current position for {symbol}")
            seen.add(symbol)
            if not isfinite(market_value) or not isfinite(quantity):
                raise ForwardDecisionError(f"Current position for {symbol} is nonfinite")
            if market_value < 0.0 or quantity < 0.0:
                raise ForwardDecisionError(
                    f"Current position for {symbol} violates long-only safety"
                )
            if symbol not in weights:
                if market_value > 0.0 or quantity > 0.0:
                    raise ForwardDecisionError(
                        f"Current position {symbol} is outside the configured universe"
                    )
                continue
            weights[symbol] = market_value / equity

        gross = float(sum(weights.values()))
        if not isfinite(gross) or gross > 1.0 + 1e-8:
            raise ForwardDecisionError("Current position market values exceed account equity")

        last_equity_value = getattr(account, "last_equity", None)
        if last_equity_value is None:
            daily_loss = 0.0
        else:
            try:
                last_equity = float(last_equity_value)
            except (TypeError, ValueError) as exc:
                raise ForwardDecisionError("Account last_equity is invalid") from exc
            if not isfinite(last_equity) or last_equity <= 0.0:
                raise ForwardDecisionError("Account last_equity must be positive and finite")
            daily_loss = equity / last_equity - 1.0
            if not isfinite(daily_loss):
                raise ForwardDecisionError("Derived account daily loss is nonfinite")
        return weights, daily_loss

    @staticmethod
    def _validated_target(weights: Mapping[str, float]) -> Mapping[str, float]:
        target = _float_map(weights)
        if any(weight < -1e-12 for weight in target.values()):
            raise ForwardDecisionError("Risk-controlled target contains a negative weight")
        if sum(target.values()) > 1.0 + 1e-8:
            raise ForwardDecisionError("Risk-controlled target exceeds total account equity")
        return target

    def _success_metadata(
        self,
        *,
        evaluated_at: datetime,
        current_session: date,
        feed: str,
        history: _CompletedHistory,
        current_weights: Mapping[str, float],
        momentum: StrategyResult,
        mean_reversion: StrategyResult,
        regime: RegimeState,
        allocation: RebalanceDecision,
        risk: RiskDecision,
        final_target: Mapping[str, float],
    ) -> ForwardDecisionMetadata:
        strategy_outputs = _freeze(
            {
                "momentum": {
                    "name": momentum.name,
                    "version": momentum.version,
                    "as_of_date": momentum.as_of_date.isoformat(),
                    "weights": momentum.weights,
                    "cash_weight": momentum.cash_weight,
                    "metadata": momentum.metadata,
                },
                "mean_reversion": {
                    "name": mean_reversion.name,
                    "version": mean_reversion.version,
                    "as_of_date": mean_reversion.as_of_date.isoformat(),
                    "weights": mean_reversion.weights,
                    "cash_weight": mean_reversion.cash_weight,
                    "metadata": mean_reversion.metadata,
                },
            }
        )
        regime_payload = _freeze(
            {
                "name": regime.name,
                "as_of_date": regime.as_of_date.isoformat(),
                "metadata": regime.metadata,
            }
        )
        allocation_payload = _freeze(
            {
                "as_of_date": allocation.as_of_date.isoformat(),
                "regime": allocation.regime,
                "strategy_allocations": allocation.strategy_allocations,
                "pre_risk_weights": allocation.pre_risk_weights,
                "pre_risk_cash": allocation.pre_risk_cash,
                "metadata": allocation.metadata,
            }
        )
        action_payloads = tuple(_freeze(action.as_dict()) for action in risk.actions)
        risk_payload = _freeze(
            {
                "decision_id": risk.decision_id,
                "status": risk.status,
                "proposed_weights": risk.proposed_weights,
                "final_weights": risk.final_weights,
                "proposed_cash": risk.proposed_cash,
                "final_cash": risk.final_cash,
                "proposed_gross_exposure": risk.proposed_gross_exposure,
                "final_gross_exposure": risk.final_gross_exposure,
                "proposed_turnover": risk.proposed_turnover,
                "final_turnover": risk.final_turnover,
                "estimated_volatility": risk.estimated_volatility,
                "final_estimated_volatility": risk.final_estimated_volatility,
                "current_drawdown": risk.current_drawdown,
                "current_daily_loss": risk.current_daily_loss,
                "daily_loss_latched": risk.daily_loss_latched,
                "hard_stop_latched": risk.hard_stop_latched,
                "halt_state": risk.halt_state,
                "data_freshness_state": risk.data_freshness_state,
                "market_state": risk.market_state,
                "liquidation_authorized": risk.liquidation_authorized,
                "rejection_reasons": risk.rejection_reasons,
                "evaluation_context": risk.evaluation_context,
            }
        )
        return ForwardDecisionMetadata(
            status=risk.status,
            evaluated_at=evaluated_at,
            current_session=current_session,
            cutoff=history.cutoff,
            provider_feed=feed,
            history_observations=len(history.prices),
            bars_received=history.bars_received,
            bars_excluded_as_incomplete=history.bars_excluded_as_incomplete,
            current_weights=_float_map(current_weights),
            strategy_outputs=strategy_outputs,
            regime=regime_payload,
            allocation=allocation_payload,
            risk_actions=action_payloads,
            risk_decision=risk_payload,
            final_target=_float_map(final_target),
        )

    @staticmethod
    def _rejection_metadata(
        *,
        evaluated_at: datetime,
        current_session: date,
        feed: str,
        current_weights: Mapping[str, float] | None,
        history: _CompletedHistory | None,
        reason: str,
    ) -> ForwardDecisionMetadata:
        # A failed data decision has no executable target.  When current
        # holdings are trustworthy they are recorded as the explicit safe hold,
        # never as an empty mapping that could be interpreted as liquidation.
        safe_hold = None if current_weights is None else _float_map(current_weights)
        return ForwardDecisionMetadata(
            status="rejected",
            evaluated_at=evaluated_at,
            current_session=current_session,
            cutoff=None if history is None else history.cutoff,
            provider_feed=feed,
            history_observations=0 if history is None else len(history.prices),
            bars_received=0 if history is None else history.bars_received,
            bars_excluded_as_incomplete=(
                0 if history is None else history.bars_excluded_as_incomplete
            ),
            current_weights=safe_hold,
            strategy_outputs=MappingProxyType({}),
            regime=None,
            allocation=None,
            risk_actions=(),
            risk_decision=None,
            final_target=safe_hold,
            error=reason,
        )
