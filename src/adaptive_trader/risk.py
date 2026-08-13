"""Independent, deterministic portfolio risk controls.

The engine deliberately does not know how a portfolio was generated.  It accepts
only risky-asset weights, infers cash as the residual to one, and applies the
configured controls in a fixed order.  A hard drawdown stop is a latch: once set,
it can only be cleared outside the backtest by constructing new state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, SupportsFloat, SupportsIndex

import numpy as np
import pandas as pd

from .config import RiskConfig
from .models import RiskAction, RiskDecision

_TOLERANCE = 1e-12


@dataclass(frozen=True)
class _CovarianceEstimate:
    """A covariance matrix and its deterministic column ordering."""

    assets: tuple[str, ...]
    matrix: np.ndarray


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Broker-independent operational inputs accompanying a portfolio proposal."""

    account_equity: float | None = None
    account_cash: float | None = None
    account_timestamp: Any | None = None
    position_weights: Mapping[str, float] | None = None
    positions: tuple[Mapping[str, Any], ...] = ()
    open_order_symbols: tuple[str, ...] = ()
    open_orders: tuple[Mapping[str, Any], ...] = ()
    current_prices: Mapping[str, float] | None = None
    current_price_timestamps: Mapping[str, Any] | None = None
    asset_eligibility: Mapping[str, bool] | None = None
    asset_metadata: Mapping[str, Mapping[str, Any]] | None = None
    market_timestamp: Any | None = None
    next_market_open: Any | None = None
    next_market_close: Any | None = None
    evaluation_cutoff: Any | None = None
    data_freshness_state: str = "not_evaluated"
    market_state: str = "not_evaluated"
    halt_state: str = "clear"

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_equity": self.account_equity,
            "account_cash": self.account_cash,
            "account_timestamp": self.account_timestamp,
            "position_weights": dict(self.position_weights or {}),
            "positions": [dict(position) for position in self.positions],
            "open_order_symbols": list(self.open_order_symbols),
            "open_orders": [dict(order) for order in self.open_orders],
            "current_prices": dict(self.current_prices or {}),
            "current_price_timestamps": dict(self.current_price_timestamps or {}),
            "asset_eligibility": dict(self.asset_eligibility or {}),
            "asset_metadata": {
                str(symbol): dict(metadata)
                for symbol, metadata in (self.asset_metadata or {}).items()
            },
            "market_timestamp": self.market_timestamp,
            "next_market_open": self.next_market_open,
            "next_market_close": self.next_market_close,
            "evaluation_cutoff": self.evaluation_cutoff,
            "data_freshness_state": self.data_freshness_state,
            "market_state": self.market_state,
            "halt_state": self.halt_state,
        }


class RiskEngine:
    """Apply independent portfolio controls to proposed risky-asset weights.

    Parameters
    ----------
    config:
        A :class:`~adaptive_trader.config.RiskConfig`.  Passing the complete
        application configuration is also supported; in that case its ``risk``
        section is used and its backtest annualization factor is honored.
    annualization_factor:
        Number of return observations per year for covariance annualization.
    """

    def __init__(
        self,
        config: RiskConfig | Any,
        annualization_factor: int = 252,
    ) -> None:
        root_config = config
        self.config: RiskConfig = getattr(root_config, "risk", root_config)
        execution_config = getattr(root_config, "execution", None)
        # Bare RiskConfig construction is a legacy research API. Full
        # application configs enforce the canonical execution cash buffer.
        self.required_cash_buffer = (
            float(execution_config.required_cash_buffer)
            if execution_config is not None
            else float(self.config.required_cash_buffer)
        )
        backtest_config = getattr(root_config, "backtest", None)
        configured_factor = getattr(backtest_config, "annualization_factor", annualization_factor)
        self.annualization_factor = int(configured_factor)
        if self.annualization_factor <= 0:
            raise ValueError("annualization_factor must be positive")

    def evaluate(
        self,
        proposed_weights: Mapping[str, float],
        current_weights: Mapping[str, float],
        historical_returns: pd.DataFrame,
        current_drawdown: float,
        hard_stop_latched: bool = False,
        current_daily_loss: float = 0.0,
        daily_loss_latched: bool = False,
        halt_latched: bool = False,
        preserve_current_on_rejection: bool = False,
        decision_id: str | None = None,
        context: RiskContext | None = None,
    ) -> RiskDecision:
        """Evaluate and, when necessary, modify a proposed portfolio.

        Controls run in this documented order: finite-data validation, system
        halt, long-only, position cap, gross cap, cash buffer, volatility
        target, soft drawdown, hard drawdown, daily loss, and turnover.
        Historical returns must already be cut off before the intended execution
        date; the engine uses only their trailing covariance lookback.
        """

        context_values = {} if context is None else context.as_dict()
        data_freshness_state = (
            "not_evaluated" if context is None else str(context.data_freshness_state)
        )
        market_state = "not_evaluated" if context is None else str(context.market_state)
        context_halt_state = "clear" if context is None else str(context.halt_state)
        halt_latched = bool(halt_latched or context_halt_state != "clear")

        proposed, invalid_proposed = _coerce_weights(proposed_weights)
        current, invalid_current = _coerce_weights(current_weights)
        all_assets = tuple(sorted(set(proposed) | set(current)))
        proposed = {asset: proposed.get(asset, 0.0) for asset in all_assets}
        current = {asset: current.get(asset, 0.0) for asset in all_assets}

        drawdown_is_finite = _is_finite_number(current_drawdown)
        safe_drawdown = float(current_drawdown) if drawdown_is_finite else 0.0
        daily_loss_is_finite = _is_finite_number(current_daily_loss)
        safe_daily_loss = float(current_daily_loss) if daily_loss_is_finite else 0.0
        invalid_reasons = [
            *(f"proposed weight for {asset!r} is not finite" for asset in invalid_proposed),
            *(f"current weight for {asset!r} is not finite" for asset in invalid_current),
        ]
        if not drawdown_is_finite:
            invalid_reasons.append("current drawdown is not finite")
        if not daily_loss_is_finite:
            invalid_reasons.append("current daily loss is not finite")
        if context is not None:
            for field_name, raw_value in (
                ("account equity", context.account_equity),
                ("account cash", context.account_cash),
            ):
                if raw_value is not None and not _is_finite_number(raw_value):
                    invalid_reasons.append(f"{field_name} is not finite")
            context_positions, invalid_context_positions = _coerce_weights(
                context.position_weights or {}
            )
            if invalid_context_positions:
                invalid_reasons.extend(
                    f"context position weight for {asset!r} is not finite"
                    for asset in invalid_context_positions
                )
            elif context.position_weights is not None:
                for asset in sorted(set(context_positions) | set(current)):
                    if abs(context_positions.get(asset, 0.0) - current.get(asset, 0.0)) > 1e-8:
                        invalid_reasons.append(
                            f"current weight for {asset!r} disagrees with position context"
                        )
            ineligible = sorted(
                asset
                for asset, eligible in (context.asset_eligibility or {}).items()
                if not bool(eligible) and proposed.get(str(asset), 0.0) > _TOLERANCE
            )
            if ineligible:
                invalid_reasons.append(
                    "proposed exposure includes ineligible assets: " + ", ".join(ineligible)
                )
            conflicts = sorted(
                asset
                for asset in set(context.open_order_symbols)
                if proposed.get(asset, 0.0) > current.get(asset, 0.0) + _TOLERANCE
            )
            if conflicts:
                invalid_reasons.append(
                    "open conflicting orders block increased exposure: " + ", ".join(conflicts)
                )
            trusted_data_states = {
                "fresh",
                "completed_daily_history",
                "completed_t_minus_1_history",
            }
            if data_freshness_state not in trusted_data_states:
                invalid_reasons.append(
                    f"data freshness state is not trusted: {data_freshness_state}"
                )
            trusted_market_states = {
                "open",
                "historical_session",
                "submission_gate_pending",
            }
            if market_state not in trusted_market_states:
                invalid_reasons.append(f"market state does not permit evaluation: {market_state}")

        current_gross = _stable_sum(current.values())
        proposed_gross = _stable_sum(proposed.values())
        if not np.isfinite(proposed_gross):
            invalid_reasons.append("proposed gross exposure is not finite")
        if not np.isfinite(current_gross):
            invalid_reasons.append("current gross exposure is not finite")
        elif current_gross > 1.0 + _TOLERANCE:
            invalid_reasons.append("current risky weights imply negative cash")
        if any(weight < -_TOLERANCE for weight in current.values()):
            invalid_reasons.append("current portfolio contains a negative risky weight")

        proposed_cash = _cash_weight(proposed)
        if invalid_reasons:
            return self._safe_cash_decision(
                proposed=proposed,
                current=current,
                proposed_cash=proposed_cash,
                current_drawdown=safe_drawdown,
                hard_stop_latched=bool(hard_stop_latched),
                reasons=invalid_reasons,
                preserve_current=preserve_current_on_rejection,
                current_daily_loss=safe_daily_loss,
                daily_loss_latched=bool(daily_loss_latched),
                decision_id=decision_id,
                data_freshness_state=data_freshness_state,
                market_state=market_state,
                evaluation_context=context_values,
            )

        raw_proposed_turnover = calculate_turnover(proposed, current)

        if halt_latched and not hard_stop_latched:
            return self._hold_rejection_decision(
                proposed=proposed,
                current=current,
                proposed_cash=proposed_cash,
                current_drawdown=safe_drawdown,
                current_daily_loss=safe_daily_loss,
                daily_loss_latched=bool(daily_loss_latched),
                decision_id=decision_id,
                control="system_halt",
                reason="The persistent halt latch blocks all new strategy orders.",
                data_freshness_state=data_freshness_state,
                market_state=market_state,
                evaluation_context=context_values,
            )

        hard_stop_required = bool(hard_stop_latched) or (
            safe_drawdown <= -float(self.config.drawdown_hard_limit)
        )

        # Validate covariance inputs as part of the first control.  A portfolio
        # that will be forced to cash by a hard latch does not require covariance
        # data, nor does an already all-cash proposal.
        covariance: _CovarianceEstimate | None = None
        covariance_assets = tuple(
            sorted(asset for asset, weight in proposed.items() if weight > _TOLERANCE)
        )
        if covariance_assets and not hard_stop_required:
            try:
                covariance = self._trailing_covariance(historical_returns, covariance_assets)
            except (TypeError, ValueError) as exc:
                return self._safe_cash_decision(
                    proposed=proposed,
                    current=current,
                    proposed_cash=proposed_cash,
                    current_drawdown=safe_drawdown,
                    hard_stop_latched=bool(hard_stop_latched),
                    reasons=[str(exc)],
                    preserve_current=preserve_current_on_rejection,
                    current_daily_loss=safe_daily_loss,
                    daily_loss_latched=bool(daily_loss_latched),
                    decision_id=decision_id,
                    data_freshness_state=data_freshness_state,
                    market_state=market_state,
                    evaluation_context=context_values,
                )

        working = proposed.copy()
        actions: list[RiskAction] = []

        # 2. Long-only.  Negative proposals are rejected asset by asset rather
        # than redistributed, so their capital remains in cash.
        negative_assets = [asset for asset, weight in working.items() if weight < 0.0]
        if negative_assets:
            before = {asset: working[asset] for asset in negative_assets}
            for asset in negative_assets:
                working[asset] = 0.0
            actions.append(
                RiskAction(
                    control="long_only",
                    description="Negative risky-asset weights were set to zero.",
                    details={"assets": negative_assets, "weights_before": before},
                )
            )

        # 3. Maximum position.  Clipped exposure is deliberately not
        # redistributed.
        position_limit = float(self.config.max_position_weight)
        clipped_assets = [
            asset for asset, weight in working.items() if weight > position_limit + _TOLERANCE
        ]
        if clipped_assets:
            before = {asset: working[asset] for asset in clipped_assets}
            for asset in clipped_assets:
                working[asset] = position_limit
            actions.append(
                RiskAction(
                    control="max_position_weight",
                    description="One or more positions were clipped to the configured maximum.",
                    details={
                        "limit": position_limit,
                        "assets": clipped_assets,
                        "weights_before": before,
                    },
                )
            )

        # 4. Maximum gross exposure.  This control can only scale down.
        gross_before = _stable_sum(working.values())
        gross_limit = float(self.config.max_gross_exposure)
        if gross_before > gross_limit + _TOLERANCE:
            scale = gross_limit / gross_before if gross_before > 0.0 else 0.0
            working = _scale_weights(working, scale)
            actions.append(
                RiskAction(
                    control="max_gross_exposure",
                    description="Risky exposure was scaled down to the gross-exposure limit.",
                    details={
                        "gross_before": gross_before,
                        "gross_after": _stable_sum(working.values()),
                        "limit": gross_limit,
                        "scale": scale,
                    },
                )
            )

        # 5. Required cash. The application-wide execution buffer is enforced
        # before volatility scaling and is never reallocated to risky assets.
        cash_gross_limit = max(0.0, 1.0 - self.required_cash_buffer)
        gross_before = _stable_sum(working.values())
        if gross_before > cash_gross_limit + _TOLERANCE:
            scale = cash_gross_limit / gross_before if gross_before > 0.0 else 0.0
            working = _scale_weights(working, scale)
            actions.append(
                RiskAction(
                    control="required_cash_buffer",
                    description="Risky exposure was reduced to retain the required cash buffer.",
                    details={
                        "cash_buffer": self.required_cash_buffer,
                        "gross_before": gross_before,
                        "gross_after": _stable_sum(working.values()),
                        "scale": scale,
                    },
                )
            )

        # 6. Volatility target.  Never scale up a low-volatility portfolio.
        estimated_volatility = 0.0
        if covariance is not None and any(weight > _TOLERANCE for weight in working.values()):
            estimated_volatility = _portfolio_volatility(working, covariance)
            volatility_target = float(self.config.target_annual_volatility)
            if estimated_volatility > volatility_target + _TOLERANCE:
                scale = volatility_target / estimated_volatility
                working = _scale_weights(working, scale)
                actions.append(
                    RiskAction(
                        control="volatility_target",
                        description="Risky exposure was scaled down to the volatility target.",
                        details={
                            "estimated_volatility_before": estimated_volatility,
                            "estimated_volatility_after": estimated_volatility * scale,
                            "target": volatility_target,
                            "scale": scale,
                        },
                    )
                )

        # 7. Soft drawdown circuit breaker.
        if safe_drawdown <= -float(self.config.drawdown_soft_limit):
            gross_before = _stable_sum(working.values())
            soft_limit = float(self.config.soft_limit_max_gross_exposure)
            if gross_before > soft_limit + _TOLERANCE:
                scale = soft_limit / gross_before if gross_before > 0.0 else 0.0
                working = _scale_weights(working, scale)
                actions.append(
                    RiskAction(
                        control="soft_drawdown",
                        description="Risky exposure was reduced by the soft drawdown circuit breaker.",
                        details={
                            "drawdown": safe_drawdown,
                            "gross_before": gross_before,
                            "gross_after": _stable_sum(working.values()),
                            "limit": soft_limit,
                            "scale": scale,
                        },
                    )
                )

        # 8. Hard drawdown circuit breaker.  Liquidation is safety critical and
        # therefore bypasses the turnover throttle below.
        if hard_stop_required:
            newly_latched = not bool(hard_stop_latched)
            working = {asset: 0.0 for asset in all_assets}
            actions.append(
                RiskAction(
                    control="hard_drawdown",
                    description=(
                        "The hard drawdown stop was triggered; the portfolio is latched in cash."
                        if newly_latched
                        else "The existing hard drawdown latch kept the portfolio in cash."
                    ),
                    details={
                        "drawdown": safe_drawdown,
                        "limit": float(self.config.drawdown_hard_limit),
                        "newly_latched": newly_latched,
                    },
                )
            )
            final_turnover = calculate_turnover(working, current)
            return RiskDecision(
                proposed_weights=proposed,
                final_weights=working,
                proposed_cash=proposed_cash,
                final_cash=1.0,
                estimated_volatility=estimated_volatility,
                proposed_turnover=raw_proposed_turnover,
                final_turnover=final_turnover,
                current_drawdown=safe_drawdown,
                hard_stop_latched=True,
                actions=actions,
                status="stopped",
                decision_id=decision_id,
                final_estimated_volatility=0.0,
                current_daily_loss=safe_daily_loss,
                daily_loss_latched=bool(daily_loss_latched),
                halt_state="hard_stop",
                data_freshness_state=data_freshness_state,
                market_state=market_state,
                liquidation_authorized=True,
                evaluation_context=context_values,
            )

        # 9. A daily-loss latch is reduction-only: it blocks every increase and
        # every new symbol, while preserving already-proposed reductions.
        daily_loss_required = bool(daily_loss_latched) or (
            safe_daily_loss <= -float(self.config.daily_loss_limit)
        )
        if daily_loss_required:
            before = dict(working)
            working = {
                asset: min(max(0.0, working[asset]), max(0.0, current[asset]))
                for asset in all_assets
            }
            actions.append(
                RiskAction(
                    control="daily_loss",
                    description=(
                        "The daily-loss limit permits reductions only for the rest of the session."
                    ),
                    details={
                        "daily_loss": safe_daily_loss,
                        "limit": float(self.config.daily_loss_limit),
                        "already_latched": bool(daily_loss_latched),
                        "weights_before": before,
                        "weights_after": dict(working),
                    },
                )
            )

        # 10. Turnover limit, measured over risky assets and cash.  Linear
        # interpolation scales every component of the trade by the same amount.
        pre_turnover = calculate_turnover(working, current)
        turnover_limit = float(self.config.max_turnover_per_rebalance)
        if pre_turnover > turnover_limit + _TOLERANCE:
            scale = turnover_limit / pre_turnover if pre_turnover > 0.0 else 0.0
            working = {
                asset: current[asset] + scale * (working[asset] - current[asset])
                for asset in all_assets
            }
            working = {
                asset: 0.0 if abs(weight) <= _TOLERANCE else float(weight)
                for asset, weight in working.items()
            }
            actions.append(
                RiskAction(
                    control="turnover_limit",
                    description="The trade was interpolated to the turnover limit.",
                    details={
                        "turnover_before": pre_turnover,
                        "limit": turnover_limit,
                        "interpolation_fraction": scale,
                    },
                )
            )

        final_turnover = calculate_turnover(working, current)
        final_cash = _cash_weight(working)
        final_estimated_volatility = 0.0
        if covariance is not None and any(weight > _TOLERANCE for weight in working.values()):
            final_estimated_volatility = _portfolio_volatility(working, covariance)
        status = "modified" if actions else "approved"
        return RiskDecision(
            proposed_weights=proposed,
            final_weights=working,
            proposed_cash=proposed_cash,
            final_cash=final_cash,
            estimated_volatility=estimated_volatility,
            proposed_turnover=raw_proposed_turnover,
            final_turnover=final_turnover,
            current_drawdown=safe_drawdown,
            hard_stop_latched=False,
            actions=actions,
            status=status,
            decision_id=decision_id,
            final_estimated_volatility=final_estimated_volatility,
            current_daily_loss=safe_daily_loss,
            daily_loss_latched=daily_loss_required,
            halt_state="daily_loss" if daily_loss_required else "clear",
            data_freshness_state=data_freshness_state,
            market_state=market_state,
            evaluation_context=context_values,
        )

    def apply(
        self,
        proposed_weights: Mapping[str, float],
        current_weights: Mapping[str, float],
        historical_returns: pd.DataFrame,
        current_drawdown: float,
        hard_stop_latched: bool = False,
        current_daily_loss: float = 0.0,
        daily_loss_latched: bool = False,
        halt_latched: bool = False,
        preserve_current_on_rejection: bool = False,
        decision_id: str | None = None,
        context: RiskContext | None = None,
    ) -> RiskDecision:
        """Compatibility alias for :meth:`evaluate`."""

        return self.evaluate(
            proposed_weights=proposed_weights,
            current_weights=current_weights,
            historical_returns=historical_returns,
            current_drawdown=current_drawdown,
            hard_stop_latched=hard_stop_latched,
            current_daily_loss=current_daily_loss,
            daily_loss_latched=daily_loss_latched,
            halt_latched=halt_latched,
            preserve_current_on_rejection=preserve_current_on_rejection,
            decision_id=decision_id,
            context=context,
        )

    def _trailing_covariance(
        self,
        historical_returns: pd.DataFrame,
        assets: tuple[str, ...],
    ) -> _CovarianceEstimate:
        if not isinstance(historical_returns, pd.DataFrame):
            raise TypeError("historical_returns must be a pandas DataFrame")
        if historical_returns.columns.has_duplicates:
            raise ValueError("historical_returns contains duplicate asset columns")
        missing = [asset for asset in assets if asset not in historical_returns.columns]
        if missing:
            raise ValueError(f"historical_returns is missing assets: {', '.join(missing)}")

        lookback = int(self.config.covariance_lookback_days)
        observations = historical_returns.loc[:, list(assets)].tail(lookback).copy()
        observations = observations.apply(pd.to_numeric, errors="coerce")
        observations = observations.replace([np.inf, -np.inf], np.nan).dropna(how="any")
        if len(observations) < 2:
            raise ValueError("insufficient finite return history for covariance estimation")

        matrix = observations.cov().to_numpy(dtype=float) * self.annualization_factor
        if matrix.shape != (len(assets), len(assets)) or not np.isfinite(matrix).all():
            raise ValueError("the trailing covariance matrix is not finite")
        # Numerical symmetrization avoids tiny platform-dependent asymmetries.
        matrix = (matrix + matrix.T) / 2.0
        eigenvalues = np.linalg.eigvalsh(matrix)
        if not np.isfinite(eigenvalues).all() or eigenvalues.min() < -_TOLERANCE:
            raise ValueError("the trailing covariance matrix is not positive semidefinite")
        return _CovarianceEstimate(assets=assets, matrix=matrix)

    def _safe_cash_decision(
        self,
        *,
        proposed: dict[str, float],
        current: dict[str, float],
        proposed_cash: float,
        current_drawdown: float,
        hard_stop_latched: bool,
        reasons: list[str],
        preserve_current: bool,
        current_daily_loss: float,
        daily_loss_latched: bool,
        decision_id: str | None,
        data_freshness_state: str,
        market_state: str,
        evaluation_context: dict[str, Any],
    ) -> RiskDecision:
        current_is_preservable = (
            all(weight >= 0.0 for weight in current.values())
            and _stable_sum(current.values()) <= 1.0 + _TOLERANCE
        )
        should_preserve = preserve_current and current_is_preservable and not hard_stop_latched
        final_weights = (
            dict(current)
            if should_preserve
            else {asset: 0.0 for asset in sorted(set(proposed) | set(current))}
        )
        try:
            turnover = calculate_turnover(final_weights, current)
        except ValueError:
            # An invalid current state makes actual turnover unknowable.  The
            # safety decision itself must nevertheless remain finite.
            turnover = 0.0
        action = RiskAction(
            control="data_validation",
            description=(
                "Required risk inputs were unusable; new orders were rejected and holdings preserved."
                if should_preserve
                else "Required risk inputs were unusable; the legacy safety result moved to cash."
            ),
            details={"reasons": reasons, "holdings_preserved": should_preserve},
        )
        return RiskDecision(
            proposed_weights=proposed,
            final_weights=final_weights,
            proposed_cash=proposed_cash,
            final_cash=_cash_weight(final_weights),
            estimated_volatility=0.0,
            proposed_turnover=turnover,
            final_turnover=turnover,
            current_drawdown=current_drawdown,
            hard_stop_latched=hard_stop_latched,
            actions=[action],
            status="rejected" if should_preserve else "stopped",
            decision_id=decision_id,
            final_estimated_volatility=None,
            current_daily_loss=current_daily_loss,
            daily_loss_latched=daily_loss_latched,
            halt_state="data_rejection",
            data_freshness_state=data_freshness_state,
            market_state=market_state,
            rejection_reasons=list(reasons),
            evaluation_context=evaluation_context,
        )

    def _hold_rejection_decision(
        self,
        *,
        proposed: dict[str, float],
        current: dict[str, float],
        proposed_cash: float,
        current_drawdown: float,
        current_daily_loss: float,
        daily_loss_latched: bool,
        decision_id: str | None,
        control: str,
        reason: str,
        data_freshness_state: str,
        market_state: str,
        evaluation_context: dict[str, Any],
    ) -> RiskDecision:
        turnover = calculate_turnover(proposed, current)
        return RiskDecision(
            proposed_weights=proposed,
            final_weights=dict(current),
            proposed_cash=proposed_cash,
            final_cash=_cash_weight(current),
            estimated_volatility=None,
            final_estimated_volatility=None,
            proposed_turnover=turnover,
            final_turnover=0.0,
            current_drawdown=current_drawdown,
            current_daily_loss=current_daily_loss,
            hard_stop_latched=False,
            daily_loss_latched=daily_loss_latched,
            actions=[RiskAction(control=control, description=reason)],
            status="rejected",
            decision_id=decision_id,
            halt_state=control,
            data_freshness_state=data_freshness_state,
            market_state=market_state,
            rejection_reasons=[reason],
            evaluation_context=evaluation_context,
        )


def calculate_turnover(
    target_weights: Mapping[str, float],
    current_weights: Mapping[str, float],
) -> float:
    """Return one-way turnover across risky assets and implicit cash.

    ``0.5`` prevents a sale and the corresponding purchase from being counted
    twice.  Cash is included explicitly, so moving from fully invested to fully
    cash has turnover one.
    """

    assets = sorted(set(target_weights) | set(current_weights))
    target = {asset: float(target_weights.get(asset, 0.0)) for asset in assets}
    current = {asset: float(current_weights.get(asset, 0.0)) for asset in assets}
    risky_change = _stable_sum(abs(target[asset] - current[asset]) for asset in assets)
    cash_change = abs(_cash_weight(target) - _cash_weight(current))
    turnover = 0.5 * (risky_change + cash_change)
    if not np.isfinite(turnover):
        raise ValueError("turnover is not finite")
    return float(turnover)


def apply_risk_controls(
    proposed_weights: Mapping[str, float],
    current_weights: Mapping[str, float],
    historical_returns: pd.DataFrame,
    current_drawdown: float,
    hard_stop_latched: bool,
    config: RiskConfig | Any,
    annualization_factor: int = 252,
) -> RiskDecision:
    """Functional wrapper around :class:`RiskEngine`."""

    return RiskEngine(config, annualization_factor=annualization_factor).evaluate(
        proposed_weights=proposed_weights,
        current_weights=current_weights,
        historical_returns=historical_returns,
        current_drawdown=current_drawdown,
        hard_stop_latched=hard_stop_latched,
    )


def _coerce_weights(weights: Mapping[str, float]) -> tuple[dict[str, float], list[str]]:
    if not isinstance(weights, Mapping):
        return {}, ["<weights mapping>"]
    clean: dict[str, float] = {}
    invalid: list[str] = []
    for raw_asset in sorted(weights, key=str):
        asset = str(raw_asset)
        raw_value = weights[raw_asset]
        try:
            if isinstance(raw_value, bool):
                raise TypeError
            value = float(raw_value)
        except (TypeError, ValueError):
            value = 0.0
            invalid.append(asset)
        if not np.isfinite(value):
            value = 0.0
            invalid.append(asset)
        clean[asset] = value
    return clean, sorted(set(invalid))


def _portfolio_volatility(
    weights: Mapping[str, float],
    covariance: _CovarianceEstimate,
) -> float:
    vector = np.array([float(weights.get(asset, 0.0)) for asset in covariance.assets])
    variance = float(vector @ covariance.matrix @ vector)
    if not np.isfinite(variance) or variance < -_TOLERANCE:
        raise ValueError("estimated portfolio variance is invalid")
    return float(np.sqrt(max(variance, 0.0)))


def _scale_weights(weights: Mapping[str, float], scale: float) -> dict[str, float]:
    return {asset: float(weight * scale) for asset, weight in weights.items()}


def _cash_weight(weights: Mapping[str, float]) -> float:
    # Proposed gross exposure can exceed one before the gross control.  Reports
    # show zero, rather than economically nonsensical negative, residual cash.
    return float(max(0.0, 1.0 - _stable_sum(weights.values())))


def _stable_sum(values: Any) -> float:
    return float(sum((float(value) for value in values), start=0.0))


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(
        value, (str, bytes, bytearray, SupportsFloat, SupportsIndex)
    ):
        return False
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False
