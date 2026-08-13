"""Edge-safe portfolio metrics that preserve honest undefined values."""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt

import numpy as np
import pandas as pd

from .models import PerformanceMetrics

_TOLERANCE = 1e-15


def calculate_metrics(
    returns: pd.Series,
    equity_curve: pd.Series | None = None,
    gross_exposure: pd.Series | None = None,
    cash_allocation: pd.Series | None = None,
    turnover: pd.Series | None = None,
    transaction_costs: pd.Series | None = None,
    number_of_rebalances: int = 0,
    number_of_risk_interventions: int = 0,
    number_of_hard_stop_events: int = 0,
    annualization_factor: int = 252,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    """Calculate documented performance and implementation metrics.

    Non-finite observations are omitted from summary statistics.  A metric that
    cannot be computed from the remaining observations is ``NaN`` and has a
    corresponding entry in :attr:`PerformanceMetrics.undefined_reasons`.
    Mathematically defined zeros remain zero; for example, two zero returns have
    zero total return and zero volatility, but Sharpe and Calmar remain undefined
    because their denominators are zero.

    ``maximum_drawdown`` and ``cvar_95`` use the return sign convention and are
    normally non-positive.  ``positive_day_percentage`` is stored as a fraction
    (``0.5`` means half of observed days were positive), matching the other
    percentage-like return and exposure fields.
    """

    if (
        isinstance(annualization_factor, bool)
        or not isinstance(annualization_factor, (int, np.integer))
        or annualization_factor <= 0
    ):
        raise ValueError("annualization_factor must be a positive integer")
    annualization = int(annualization_factor)
    if not np.isfinite(float(risk_free_rate)) or float(risk_free_rate) <= -1.0:
        raise ValueError("risk_free_rate must be finite and greater than -1")

    clean_returns = _finite_series(returns, "returns")
    clean_equity = _finite_series(equity_curve, "equity_curve")

    undefined_reasons: dict[str, str] = {}

    total_return, reason = _total_return(clean_returns, clean_equity)
    _record_reason(undefined_reasons, "total_return", reason)
    periods = len(clean_returns)
    if periods == 0 and len(clean_equity) > 1:
        periods = len(clean_equity) - 1
    cagr, reason = _cagr(total_return, periods, annualization)
    _record_reason(undefined_reasons, "cagr", reason)

    annualized_volatility, reason = _annualized_volatility(clean_returns, annualization)
    _record_reason(undefined_reasons, "annualized_volatility", reason)
    daily_std = _sample_standard_deviation(clean_returns)
    daily_risk_free_rate = (1.0 + float(risk_free_rate)) ** (1.0 / annualization) - 1.0
    excess_returns = clean_returns - daily_risk_free_rate
    if len(excess_returns) < 2:
        sharpe_ratio = float("nan")
        reason = "Sharpe ratio requires at least two finite return observations."
    elif not np.isfinite(daily_std) or abs(daily_std) <= _TOLERANCE:
        sharpe_ratio = float("nan")
        reason = "Sharpe ratio is undefined because return volatility is zero."
    else:
        sharpe_ratio, reason = _ratio_metric(
            float(excess_returns.mean()) * sqrt(annualization),
            daily_std,
            metric_name="Sharpe ratio",
        )
    _record_reason(undefined_reasons, "sharpe_ratio", reason)

    if len(excess_returns) < 2:
        sortino_ratio = float("nan")
        reason = "Sortino ratio requires at least two finite return observations."
    else:
        downside = np.minimum(excess_returns.to_numpy(dtype=float), 0.0)
        downside_deviation = float(np.sqrt(np.mean(np.square(downside))))
        if abs(downside_deviation) <= _TOLERANCE:
            sortino_ratio = float("nan")
            reason = "Sortino ratio is undefined because downside deviation is zero."
        else:
            sortino_ratio, reason = _ratio_metric(
                float(excess_returns.mean()) * sqrt(annualization),
                downside_deviation,
                metric_name="Sortino ratio",
            )
    _record_reason(undefined_reasons, "sortino_ratio", reason)

    maximum_drawdown, reason = _maximum_drawdown(clean_returns, clean_equity)
    _record_reason(undefined_reasons, "maximum_drawdown", reason)
    if not np.isfinite(cagr):
        calmar_ratio = float("nan")
        reason = "Calmar ratio is unavailable because CAGR is undefined."
    elif not np.isfinite(maximum_drawdown):
        calmar_ratio = float("nan")
        reason = "Calmar ratio is unavailable because maximum drawdown is undefined."
    elif abs(maximum_drawdown) <= _TOLERANCE:
        calmar_ratio = float("nan")
        reason = "Calmar ratio is undefined because maximum drawdown is zero."
    else:
        calmar_ratio, reason = _ratio_metric(
            cagr,
            abs(maximum_drawdown),
            metric_name="Calmar ratio",
        )
    _record_reason(undefined_reasons, "calmar_ratio", reason)

    var_95, reason = _historical_var(clean_returns, confidence=0.95)
    _record_reason(undefined_reasons, "var_95", reason)
    cvar_95, reason = _historical_cvar(clean_returns, confidence=0.95)
    _record_reason(undefined_reasons, "cvar_95", reason)
    if clean_returns.empty:
        positive_day_percentage = float("nan")
        reason = "Positive-day percentage requires at least one finite return observation."
    else:
        positive_day_percentage = float((clean_returns > 0.0).mean())
        reason = None
    _record_reason(undefined_reasons, "positive_day_percentage", reason)

    average_gross_exposure, reason = _mean_metric(
        _finite_series(gross_exposure, "gross_exposure"),
        "Average gross exposure requires at least one finite exposure observation.",
    )
    _record_reason(undefined_reasons, "average_gross_exposure", reason)
    average_cash_allocation, reason = _mean_metric(
        _finite_series(cash_allocation, "cash_allocation"),
        "Average cash allocation requires at least one finite cash observation.",
    )
    _record_reason(undefined_reasons, "average_cash_allocation", reason)
    total_turnover, reason = _nonnegative_sum_metric(
        _finite_series(turnover, "turnover"),
        "Total turnover requires at least one finite turnover observation.",
        "Total turnover is undefined because a turnover observation is negative.",
    )
    _record_reason(undefined_reasons, "total_turnover", reason)
    estimated_transaction_costs, reason = _nonnegative_sum_metric(
        _finite_series(transaction_costs, "transaction_costs"),
        "Estimated transaction costs require at least one finite cost observation.",
        "Estimated transaction costs are undefined because a cost observation is negative.",
    )
    _record_reason(undefined_reasons, "estimated_transaction_costs", reason)

    metrics = PerformanceMetrics(
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        maximum_drawdown=maximum_drawdown,
        calmar_ratio=calmar_ratio,
        var_95=var_95,
        cvar_95=cvar_95,
        positive_day_percentage=positive_day_percentage,
        average_gross_exposure=average_gross_exposure,
        average_cash_allocation=average_cash_allocation,
        total_turnover=total_turnover,
        estimated_transaction_costs=estimated_transaction_costs,
        number_of_rebalances=_nonnegative_count(number_of_rebalances, "number_of_rebalances"),
        number_of_risk_interventions=_nonnegative_count(
            number_of_risk_interventions, "number_of_risk_interventions"
        ),
        number_of_hard_stop_events=_nonnegative_count(
            number_of_hard_stop_events, "number_of_hard_stop_events"
        ),
        undefined_reasons=undefined_reasons,
    )
    return metrics


def rolling_sharpe_ratio(
    returns: pd.Series,
    window: int = 63,
    annualization_factor: int = 252,
) -> pd.Series:
    """Return a finite rolling zero-risk-free-rate Sharpe series.

    Windows with fewer than ``window`` observations or zero sample volatility
    are left as ``NaN`` for plotting rather than represented as infinities.
    """

    if isinstance(window, bool) or not isinstance(window, (int, np.integer)) or window <= 1:
        raise ValueError("window must be an integer greater than one")
    if (
        isinstance(annualization_factor, bool)
        or not isinstance(annualization_factor, (int, np.integer))
        or annualization_factor <= 0
    ):
        raise ValueError("annualization_factor must be a positive integer")
    if not isinstance(returns, pd.Series):
        raise TypeError("returns must be a pandas Series")

    numeric = pd.to_numeric(returns, errors="coerce").replace([np.inf, -np.inf], np.nan)
    rolling_mean = numeric.rolling(int(window), min_periods=int(window)).mean()
    rolling_std = numeric.rolling(int(window), min_periods=int(window)).std(ddof=1)
    result = rolling_mean / rolling_std * sqrt(int(annualization_factor))
    return result.where(rolling_std.abs() > _TOLERANCE)


def compute_metrics(
    returns: pd.Series,
    equity_curve: pd.Series | None = None,
    gross_exposure: pd.Series | None = None,
    cash_allocation: pd.Series | None = None,
    turnover: pd.Series | None = None,
    transaction_costs: pd.Series | None = None,
    number_of_rebalances: int = 0,
    number_of_risk_interventions: int = 0,
    number_of_hard_stop_events: int = 0,
    annualization_factor: int = 252,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    """Compatibility alias for :func:`calculate_metrics`."""

    return calculate_metrics(
        returns=returns,
        equity_curve=equity_curve,
        gross_exposure=gross_exposure,
        cash_allocation=cash_allocation,
        turnover=turnover,
        transaction_costs=transaction_costs,
        number_of_rebalances=number_of_rebalances,
        number_of_risk_interventions=number_of_risk_interventions,
        number_of_hard_stop_events=number_of_hard_stop_events,
        annualization_factor=annualization_factor,
        risk_free_rate=risk_free_rate,
    )


# A descriptive alias used by some research notebooks.
calculate_performance_metrics = calculate_metrics


def _finite_series(
    values: pd.Series | Sequence[float] | None,
    name: str,
) -> pd.Series:
    if values is None:
        return pd.Series(dtype=float)
    if isinstance(values, pd.Series):
        series = values.copy()
    elif isinstance(values, Sequence) and not isinstance(values, str | bytes):
        series = pd.Series(values, dtype="object")
    else:
        raise TypeError(f"{name} must be a pandas Series or one-dimensional sequence")
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.replace([np.inf, -np.inf], np.nan).dropna().astype(float)


def _record_reason(reasons: dict[str, str], metric_name: str, reason: str | None) -> None:
    if reason is not None:
        reasons[metric_name] = reason


def _total_return(returns: pd.Series, equity: pd.Series) -> tuple[float, str | None]:
    # Daily returns are authoritative when present because a recorded equity
    # series commonly begins *after* the first simulated day's cost and return.
    if not returns.empty:
        if (returns < -1.0).any():
            return (
                float("nan"),
                "Total return is undefined because a return below -100% implies negative wealth.",
            )
        with np.errstate(over="ignore", invalid="ignore"):
            compounded = float(np.prod(1.0 + returns.to_numpy(dtype=float)) - 1.0)
        if np.isfinite(compounded):
            return compounded, None
        return float("nan"), "Total return overflowed while compounding finite observations."
    if len(equity) >= 2:
        if equity.iloc[0] <= 0.0 or (equity < 0.0).any():
            return (
                float("nan"),
                "Total return requires a positive starting equity and nonnegative equity values.",
            )
        result = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
        if np.isfinite(result):
            return result, None
        return float("nan"), "Total return from the equity curve is nonfinite."
    return (
        float("nan"),
        "Total return requires at least one finite return or two finite equity observations.",
    )


def _cagr(
    total_return: float,
    periods: int,
    annualization_factor: int,
) -> tuple[float, str | None]:
    if not np.isfinite(total_return):
        return float("nan"), "CAGR is unavailable because total return is undefined."
    if periods <= 0:
        return float("nan"), "CAGR requires at least one observed return period."
    ending_multiple = 1.0 + total_return
    if abs(ending_multiple) <= _TOLERANCE:
        return -1.0, None
    if ending_multiple < 0.0:
        return float("nan"), "CAGR is undefined when compounded ending wealth is negative."
    with np.errstate(over="ignore", invalid="ignore"):
        result = float(ending_multiple ** (annualization_factor / periods) - 1.0)
    if np.isfinite(result):
        return result, None
    return float("nan"), "CAGR overflowed during annualization."


def _annualized_volatility(
    returns: pd.Series,
    annualization_factor: int,
) -> tuple[float, str | None]:
    if len(returns) < 2:
        return (
            float("nan"),
            "Annualized volatility requires at least two finite return observations.",
        )
    result = _sample_standard_deviation(returns) * sqrt(annualization_factor)
    if np.isfinite(result):
        return result, None
    return float("nan"), "Annualized volatility could not be calculated as a finite value."


def _sample_standard_deviation(values: pd.Series) -> float:
    if len(values) < 2:
        return float("nan")
    result = float(values.std(ddof=1))
    return 0.0 if abs(result) <= _TOLERANCE else result


def _maximum_drawdown(returns: pd.Series, equity: pd.Series) -> tuple[float, str | None]:
    # Prepending one captures a loss on the first recorded simulation day even
    # when the equity frame does not contain an explicit initial-capital row.
    if not returns.empty:
        if (returns < -1.0).any():
            return (
                float("nan"),
                "Maximum drawdown is undefined because a return below -100% implies negative wealth.",
            )
        with np.errstate(over="ignore", invalid="ignore"):
            cumulative = np.cumprod(1.0 + returns.to_numpy(dtype=float))
        wealth = np.concatenate(([1.0], cumulative))
    elif len(equity) >= 2 and equity.iloc[0] > 0.0 and (equity >= 0.0).all():
        wealth = equity.to_numpy(dtype=float)
    else:
        return (
            float("nan"),
            "Maximum drawdown requires returns or at least two valid equity observations.",
        )

    if wealth.size == 0 or not np.isfinite(wealth).all():
        return float("nan"), "Maximum drawdown wealth values are nonfinite."
    peaks = np.maximum.accumulate(wealth)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = wealth / peaks - 1.0
    finite_drawdowns = drawdowns[np.isfinite(drawdowns)]
    if finite_drawdowns.size == 0:
        return float("nan"), "Maximum drawdown has no finite wealth-relative observations."
    return float(min(0.0, float(finite_drawdowns.min()))), None


def _historical_cvar(
    returns: pd.Series,
    confidence: float,
) -> tuple[float, str | None]:
    if returns.empty:
        return float("nan"), "Historical CVaR requires at least one finite return observation."
    cutoff = float(returns.quantile(1.0 - confidence))
    tail = returns[returns <= cutoff]
    if tail.empty:
        return float("nan"), "Historical CVaR has no observations in its empirical tail."
    result = float(tail.mean())
    if np.isfinite(result):
        return result, None
    return float("nan"), "Historical CVaR could not be calculated as a finite value."


def _historical_var(
    returns: pd.Series,
    confidence: float,
) -> tuple[float, str | None]:
    if returns.empty:
        return float("nan"), "Historical VaR requires at least one finite return observation."
    result = float(returns.quantile(1.0 - confidence))
    if np.isfinite(result):
        return result, None
    return float("nan"), "Historical VaR could not be calculated as a finite value."


def _mean_metric(values: pd.Series, empty_reason: str) -> tuple[float, str | None]:
    if values.empty:
        return float("nan"), empty_reason
    result = float(values.mean())
    if np.isfinite(result):
        return result, None
    return float("nan"), "The arithmetic mean could not be calculated as a finite value."


def _nonnegative_sum_metric(
    values: pd.Series,
    empty_reason: str,
    negative_reason: str,
) -> tuple[float, str | None]:
    if values.empty:
        return float("nan"), empty_reason
    if (values < -_TOLERANCE).any():
        return float("nan"), negative_reason
    result = float(values.sum())
    if np.isfinite(result):
        return max(0.0, result), None
    return float("nan"), "The aggregate could not be calculated as a finite value."


def _ratio_metric(
    numerator: float,
    denominator: float,
    *,
    metric_name: str,
) -> tuple[float, str | None]:
    if not np.isfinite(numerator) or not np.isfinite(denominator):
        return float("nan"), f"{metric_name} has a nonfinite numerator or denominator."
    if abs(denominator) <= _TOLERANCE:
        return float("nan"), f"{metric_name} is undefined because its denominator is zero."
    result = float(numerator / denominator)
    if np.isfinite(result):
        return result, None
    return float("nan"), f"{metric_name} could not be calculated as a finite value."


def _nonnegative_count(value: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a nonnegative integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a nonnegative integer") from exc
    if result != value or result < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return result
