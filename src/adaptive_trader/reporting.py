"""Persistent research artifacts, plots, receipts, and Markdown reporting."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from numbers import Integral, Real
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from .metrics import calculate_metrics, rolling_sharpe_ratio

# Matplotlib/fontconfig try to populate user cache directories at import time. The
# research pipeline must also work in restricted and headless environments, so
# direct those caches to a private writable temporary location before importing
# pyplot and force the non-interactive backend.
_CACHE_ROOT = Path(tempfile.gettempdir()) / "adaptive-portfolio-agent-cache"
_MPL_CONFIG = _CACHE_ROOT / "matplotlib"
_MPL_CONFIG.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("XDG_CACHE_HOME", str(_CACHE_ROOT))
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CONFIG))

import matplotlib  # noqa: E402

matplotlib.use("Agg", force=True)

from matplotlib import dates as mdates  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402

_METRIC_COLUMNS = (
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
    "number_of_rebalances",
    "number_of_risk_interventions",
    "number_of_hard_stop_events",
)

_METRIC_REASON_COLUMNS = tuple(f"{name}_reason" for name in _METRIC_COLUMNS)

_METRIC_LABELS = {
    "total_return": "Total return",
    "cagr": "CAGR",
    "annualized_volatility": "Ann. volatility",
    "sharpe_ratio": "Sharpe",
    "sortino_ratio": "Sortino",
    "maximum_drawdown": "Max drawdown",
    "calmar_ratio": "Calmar",
    "var_95": "95% VaR",
    "cvar_95": "95% CVaR",
    "positive_day_percentage": "Positive days",
    "average_gross_exposure": "Avg. gross",
    "average_cash_allocation": "Avg. cash",
    "total_turnover": "Total turnover",
    "estimated_transaction_costs": "Est. costs",
    "number_of_rebalances": "Rebalances",
    "number_of_risk_interventions": "Risk actions",
    "number_of_hard_stop_events": "Hard stops",
}

_PERCENT_METRICS = {
    "total_return",
    "cagr",
    "annualized_volatility",
    "maximum_drawdown",
    "var_95",
    "cvar_95",
    "positive_day_percentage",
    "average_gross_exposure",
    "average_cash_allocation",
    "total_turnover",
    "estimated_transaction_costs",
}

_COUNT_METRICS = {
    "number_of_rebalances",
    "number_of_risk_interventions",
    "number_of_hard_stop_events",
}

_REGIME_ORDER = (
    "bear_high_vol",
    "bear_low_vol",
    "bull_high_vol",
    "bull_low_vol",
)


def _historical_feed_disclosure(data_summary: Mapping[str, Any], config: Any) -> str:
    """Describe the configured live feed without mislabeling historical inputs."""

    configured = str(getattr(getattr(config, "market_data", None), "feed", "IEX")).upper()
    actual = str(data_summary.get("feed", "unknown")).upper()
    synthetic = bool(data_summary.get("is_synthetic", False))
    if configured == "SIP" and (synthetic or actual != "SIP"):
        return (
            "SIP FEED CONFIGURED — ENTITLEMENT UNCONFIRMED FOR THIS HISTORICAL ARTIFACT; "
            f"ACTUAL HISTORICAL FEED: {actual}; NO SIP CLAIM OR FALLBACK"
        )
    if configured == "SIP":
        return "REAL-TIME SIP FEED — HISTORICAL ARTIFACT SOURCE FEED: SIP"
    return (
        "CONFIGURED LIVE-SERVICE DISCLOSURE: "
        "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET; "
        f"ACTUAL HISTORICAL FEED: {actual} (NOT REAL-TIME)"
    )


def generate_outputs(
    result: Any,
    output_directory: str | Path | None = None,
) -> dict[str, Path]:
    """Generate the complete required artifact set for a backtest suite.

    Parameters
    ----------
    result:
        A backtest suite with ``runs``, ``market_data``, and ``config`` attributes.
    output_directory:
        Optional destination override. When omitted, the configured project output
        directory is used.

    Returns
    -------
    dict[str, pathlib.Path]
        A mapping from exact artifact filename to its written path.
    """

    runs = getattr(result, "runs", None)
    if not isinstance(runs, Mapping) or not runs:
        raise ValueError("Backtest suite result must contain at least one named run")
    config = getattr(result, "config", None)
    if config is None:
        raise ValueError("Backtest suite result has no configuration")

    configured_directory = getattr(getattr(config, "project", None), "output_directory", None)
    destination = output_directory if output_directory is not None else configured_directory
    if destination is None or not str(destination).strip():
        raise ValueError("An output directory must be supplied or configured")
    output_path = Path(destination).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, Path] = {}
    oos_start = pd.Timestamp(config.backtest.out_of_sample_start)
    annualization = int(config.backtest.annualization_factor)

    risk_free_rate = float(getattr(config.reporting, "risk_free_rate", 0.0))
    full_metrics = _metrics_table(
        runs, annualization_factor=annualization, risk_free_rate=risk_free_rate
    )
    oos_metrics = _metrics_table(
        runs,
        annualization_factor=annualization,
        start_date=oos_start,
        risk_free_rate=risk_free_rate,
    )
    development_metrics: pd.DataFrame | None = None
    validation_metrics: pd.DataFrame | None = None
    holdout_metrics: pd.DataFrame | None = None
    if config.backtest.development_period_start is not None:
        development_metrics = _metrics_table(
            runs,
            annualization_factor=annualization,
            start_date=pd.Timestamp(config.backtest.development_period_start),
            end_date=pd.Timestamp(config.backtest.development_period_end),
            risk_free_rate=risk_free_rate,
        )
        validation_metrics = _metrics_table(
            runs,
            annualization_factor=annualization,
            start_date=pd.Timestamp(config.backtest.validation_period_start),
            end_date=pd.Timestamp(config.backtest.validation_period_end),
            risk_free_rate=risk_free_rate,
        )
        holdout_metrics = _metrics_table(
            runs,
            annualization_factor=annualization,
            start_date=pd.Timestamp(config.backtest.holdout_period_start),
            risk_free_rate=risk_free_rate,
        )
    risk_actions = _risk_actions_table(runs)

    tables = {
        "metrics_full_period.csv": full_metrics,
        "metrics_out_of_sample.csv": oos_metrics,
        "metrics_post_2020.csv": oos_metrics,
        "annual_returns.csv": _annual_returns_table(runs),
        "regime_metrics.csv": _regime_metrics_table(
            runs,
            annualization_factor=annualization,
            risk_free_rate=risk_free_rate,
        ),
        "daily_portfolio_values.csv": _daily_table(runs, "equity", "equity"),
        "daily_returns.csv": _daily_table(runs, "daily_return", "daily_return"),
        "daily_drawdowns.csv": _daily_table(runs, "drawdown", "drawdown"),
        "asset_weights.csv": _weights_table(runs),
        "strategy_allocations.csv": _strategy_allocations_table(runs),
        "regimes.csv": _regimes_table(runs),
        "trades_or_rebalances.csv": _rebalances_table(runs),
        "rebalance_decisions.csv": _rebalances_table(runs),
        "risk_actions.csv": risk_actions,
    }
    if (
        development_metrics is not None
        and validation_metrics is not None
        and holdout_metrics is not None
    ):
        tables.update(
            {
                "metrics_development.csv": development_metrics,
                "metrics_validation.csv": validation_metrics,
                "metrics_holdout.csv": holdout_metrics,
            }
        )
    for filename, frame in tables.items():
        path = output_path / filename
        _csv_safe(frame).to_csv(path, index=False)
        artifacts[filename] = path

    configuration_path = output_path / "run_configuration.yaml"
    configuration = (
        config.to_canonical_dict()
        if hasattr(config, "to_canonical_dict")
        else _object_mapping(config)
    )
    configuration_path.write_text(
        yaml.safe_dump(_to_jsonable(configuration), sort_keys=False),
        encoding="utf-8",
    )
    artifacts[configuration_path.name] = configuration_path

    data_summary = _build_data_summary(getattr(result, "market_data", None), config)
    data_summary_path = output_path / "data_summary.json"
    data_summary_path.write_text(
        json.dumps(data_summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    artifacts[data_summary_path.name] = data_summary_path

    configured_feed = str(getattr(getattr(config, "market_data", None), "feed", "IEX")).upper()
    actual_feed = str(data_summary.get("feed", "unknown")).upper()
    feed_disclosure = _historical_feed_disclosure(data_summary, config)
    receipts = _decision_receipts(runs)
    for receipt in receipts:
        receipt["paper_disclosure"] = "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
        receipt["feed_disclosure"] = feed_disclosure
        receipt["configured_live_feed"] = configured_feed
        receipt["market_data_feed"] = actual_feed
        receipt["market_data_source"] = str(data_summary.get("source", "unknown"))
        receipt["feed_disclosure_context"] = (
            "The feed disclosure describes the configured live-paper service. "
            "The receipt's market_data_feed and market_data_source identify the actual "
            "historical input and never imply a real-time connection."
        )
    jsonl_path = output_path / "decision_receipts.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for receipt in receipts:
            handle.write(json.dumps(receipt, sort_keys=True, allow_nan=False) + "\n")
    artifacts[jsonl_path.name] = jsonl_path

    receipts_markdown_path = output_path / "decision_receipts.md"
    receipts_markdown_path.write_text(
        _render_receipts_markdown(receipts, feed_disclosure),
        encoding="utf-8",
    )
    artifacts[receipts_markdown_path.name] = receipts_markdown_path

    plot_writers = {
        "equity_curves.png": lambda path: _plot_equity(
            runs, path, logarithmic=False, feed_disclosure=feed_disclosure
        ),
        "equity_curves_log_scale.png": lambda path: _plot_equity(
            runs, path, logarithmic=True, feed_disclosure=feed_disclosure
        ),
        "drawdowns.png": lambda path: _plot_drawdowns(runs, path, feed_disclosure=feed_disclosure),
        "regime_timeline.png": lambda path: _plot_regimes(
            runs, path, feed_disclosure=feed_disclosure
        ),
        "strategy_allocations.png": lambda path: _plot_strategy_allocations(
            runs, path, feed_disclosure=feed_disclosure
        ),
        "adaptive_asset_weights.png": lambda path: _plot_adaptive_weights(
            runs, path, feed_disclosure=feed_disclosure
        ),
        "rolling_sharpe.png": lambda path: _plot_rolling_sharpe(
            runs,
            path,
            window=int(config.reporting.rolling_sharpe_window_days),
            annualization_factor=annualization,
            feed_disclosure=feed_disclosure,
        ),
        "risk_interventions.png": lambda path: _plot_risk_interventions(
            risk_actions, path, feed_disclosure=feed_disclosure
        ),
    }
    for filename, writer in plot_writers.items():
        path = output_path / filename
        writer(path)
        artifacts[filename] = path

    canonical_plot_aliases = {
        "historical_equity_curves.png": "equity_curves.png",
        "historical_drawdowns.png": "drawdowns.png",
        "historical_regime_timeline.png": "regime_timeline.png",
        "historical_strategy_allocations.png": "strategy_allocations.png",
        "historical_asset_weights.png": "adaptive_asset_weights.png",
        "historical_rolling_sharpe.png": "rolling_sharpe.png",
        "historical_risk_interventions.png": "risk_interventions.png",
    }
    for canonical_name, legacy_name in canonical_plot_aliases.items():
        canonical_path = output_path / canonical_name
        shutil.copyfile(artifacts[legacy_name], canonical_path)
        artifacts[canonical_name] = canonical_path

    report_path = output_path / "report.md"
    report_path.write_text(
        _render_report(
            config=config,
            data_summary=data_summary,
            feed_disclosure=feed_disclosure,
            full_metrics=full_metrics,
            oos_metrics=oos_metrics,
            development_metrics=development_metrics,
            validation_metrics=validation_metrics,
            holdout_metrics=holdout_metrics,
            risk_actions=risk_actions,
        ),
        encoding="utf-8",
    )
    artifacts[report_path.name] = report_path

    # Preserve a content-addressed, immutable copy of every completed
    # historical evidence bundle while keeping the configured top-level files
    # as convenient latest-run projections. No existing archive file is ever
    # rewritten.
    bundle_hash = hashlib.sha256()
    for filename, artifact_path in sorted(artifacts.items()):
        bundle_hash.update(filename.encode("utf-8"))
        bundle_hash.update(artifact_path.read_bytes())
    config_hash = str(getattr(config, "configuration_hash", "unhashed"))
    archive_directory = (
        output_path / "runs" / (f"{config_hash[:12]}-{bundle_hash.hexdigest()[:16]}")
    )
    archive_directory.mkdir(parents=True, exist_ok=True)
    for filename, artifact_path in artifacts.items():
        archived_path = archive_directory / filename
        if archived_path.exists():
            if archived_path.read_bytes() != artifact_path.read_bytes():
                raise RuntimeError(f"Immutable historical archive conflict: {archived_path}")
            continue
        shutil.copyfile(artifact_path, archived_path)
    return artifacts


def _metrics_table(
    runs: Mapping[str, Any],
    *,
    annualization_factor: int,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
    risk_free_rate: float = 0.0,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        period = daily
        if start_date is not None:
            period = period.loc[period.index >= start_date]
        if end_date is not None:
            period = period.loc[period.index <= end_date]
        rebalances = _count_records(getattr(run, "rebalances", []), start_date, end_date)
        interventions = _count_records(getattr(run, "risk_actions", []), start_date, end_date)
        hard_stops = _hard_stop_events(daily, start_date, end_date)
        execution_costs = (
            _column(period, "total_execution_cost")
            if "total_execution_cost" in period
            else _column(period, "transaction_cost")
        )
        metrics = calculate_metrics(
            returns=_column(period, "daily_return"),
            gross_exposure=_column(period, "gross_exposure"),
            cash_allocation=_column(period, "cash_weight"),
            turnover=_column(period, "turnover"),
            transaction_costs=execution_costs,
            number_of_rebalances=rebalances,
            number_of_risk_interventions=interventions,
            number_of_hard_stop_events=hard_stops,
            annualization_factor=annualization_factor,
            risk_free_rate=risk_free_rate,
        )
        record: dict[str, Any] = {"portfolio": str(portfolio)}
        record.update(metrics.as_dict())
        record.update(_metric_reason_columns(metrics))
        records.append(record)
    columns = ["portfolio", *_METRIC_COLUMNS, *_METRIC_REASON_COLUMNS]
    return pd.DataFrame.from_records(records).reindex(columns=columns)


def _metric_reason_columns(metrics: Any) -> dict[str, str | None]:
    """Flatten metric explanations into additive, CSV-friendly columns."""

    reasons = getattr(metrics, "undefined_reasons", {})
    if not isinstance(reasons, Mapping):
        reasons = {}
    return {f"{name}_reason": reasons.get(name) for name in _METRIC_COLUMNS}


def _annual_returns_table(runs: Mapping[str, Any]) -> pd.DataFrame:
    """Return compounded calendar-year performance for every portfolio."""

    records: list[dict[str, Any]] = []
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        returns = _column(daily, "daily_return").dropna()
        for year, values in returns.groupby(returns.index.year):
            records.append(
                {
                    "year": int(year),
                    "portfolio": str(portfolio),
                    "annual_return": float((1.0 + values).prod() - 1.0),
                    "observations": len(values),
                }
            )
    return pd.DataFrame.from_records(records).reindex(
        columns=["year", "portfolio", "annual_return", "observations"]
    )


def _regime_metrics_table(
    runs: Mapping[str, Any],
    *,
    annualization_factor: int,
    risk_free_rate: float,
) -> pd.DataFrame:
    """Return comparable metrics conditioned on the recorded market regime."""

    adaptive = _named_run(runs, "adaptive")
    shared_regimes = getattr(adaptive, "regimes", pd.Series(dtype="object"))
    records: list[dict[str, Any]] = []
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        if daily.empty:
            continue
        own_regimes = getattr(run, "regimes", pd.Series(dtype="object"))
        source = (
            own_regimes
            if isinstance(own_regimes, pd.Series) and not own_regimes.empty
            else shared_regimes
        )
        if not isinstance(source, pd.Series) or source.empty:
            continue
        regime_by_day = source.copy()
        regime_by_day.index = pd.to_datetime(regime_by_day.index)
        regime_by_day = regime_by_day.sort_index().reindex(daily.index, method="ffill")
        for regime_name in _REGIME_ORDER:
            period = daily.loc[regime_by_day == regime_name]
            if period.empty:
                continue
            metrics = calculate_metrics(
                returns=_column(period, "daily_return"),
                gross_exposure=_column(period, "gross_exposure"),
                cash_allocation=_column(period, "cash_weight"),
                turnover=_column(period, "turnover"),
                transaction_costs=_column(period, "total_execution_cost"),
                annualization_factor=annualization_factor,
                risk_free_rate=risk_free_rate,
            )
            record = {
                "portfolio": str(portfolio),
                "regime": regime_name,
                "observations": len(period),
            }
            record.update(metrics.as_dict())
            record.update(_metric_reason_columns(metrics))
            records.append(record)
    return pd.DataFrame.from_records(records).reindex(
        columns=[
            "portfolio",
            "regime",
            "observations",
            *_METRIC_COLUMNS,
            *_METRIC_REASON_COLUMNS,
        ]
    )


def _daily_table(
    runs: Mapping[str, Any],
    source_column: str,
    value_column: str,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        if source_column not in daily:
            continue
        frame = daily[[source_column]].rename(columns={source_column: value_column})
        frame = frame.rename_axis("date").reset_index()
        frame.insert(1, "portfolio", str(portfolio))
        frames.append(frame)
    columns = ["date", "portfolio", value_column]
    if not frames:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(frames, ignore_index=True)
        .reindex(columns=columns)
        .sort_values(["date", "portfolio"], ignore_index=True)
    )


def _weights_table(runs: Mapping[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for portfolio, run in runs.items():
        weights = _dated_frame(getattr(run, "weights", pd.DataFrame()))
        if weights.empty:
            continue
        if "cash" not in weights.columns:
            daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
            weights["cash"] = _column(daily, "cash_weight").reindex(weights.index)
        frame = (
            weights.rename_axis("date")
            .reset_index()
            .melt(
                id_vars="date",
                var_name="asset",
                value_name="weight",
            )
        )
        frame.insert(1, "portfolio", str(portfolio))
        frames.append(frame)
    columns = ["date", "portfolio", "asset", "weight"]
    if not frames:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(frames, ignore_index=True)
        .reindex(columns=columns)
        .sort_values(["date", "portfolio", "asset"], ignore_index=True)
    )


def _strategy_allocations_table(runs: Mapping[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    expected = ["momentum", "mean_reversion", "strategic_cash"]
    for portfolio, run in runs.items():
        allocations = _dated_frame(getattr(run, "strategy_allocations", pd.DataFrame()))
        if allocations.empty:
            continue
        frame = allocations.rename_axis("date").reset_index()
        frame.insert(1, "portfolio", str(portfolio))
        for column in expected:
            if column not in frame:
                frame[column] = 0.0
        frames.append(frame)
    columns = ["date", "portfolio", *expected]
    if not frames:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(frames, ignore_index=True)
        .reindex(columns=columns)
        .sort_values(["date", "portfolio"], ignore_index=True)
    )


def _regimes_table(runs: Mapping[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for portfolio, run in runs.items():
        regimes = getattr(run, "regimes", pd.Series(dtype="object"))
        if isinstance(regimes, pd.DataFrame):
            if regimes.empty:
                continue
            series = regimes.iloc[:, 0]
        elif isinstance(regimes, pd.Series):
            series = regimes
        else:
            continue
        if series.empty:
            continue
        series = series.copy()
        series.index = pd.to_datetime(series.index)
        frame = series.sort_index().rename("regime").rename_axis("date").reset_index()
        frame.insert(1, "portfolio", str(portfolio))
        frames.append(frame)
    columns = ["date", "portfolio", "regime"]
    if not frames:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(frames, ignore_index=True)
        .reindex(columns=columns)
        .sort_values(["date", "portfolio"], ignore_index=True)
    )


def _rebalances_table(runs: Mapping[str, Any]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for portfolio, run in runs.items():
        for raw in _iter_records(getattr(run, "rebalances", [])):
            record = _object_mapping(raw)
            record.setdefault("portfolio", str(portfolio))
            records.append(record)
    if not records:
        return pd.DataFrame(columns=["portfolio", "signal_as_of_date", "execution_date"])
    frame = pd.json_normalize(records, sep=".")
    preferred = ["portfolio", "signal_as_of_date", "as_of_date", "execution_date"]
    return _preferred_columns(frame, preferred)


def _risk_actions_table(runs: Mapping[str, Any]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for portfolio, run in runs.items():
        for raw in _iter_records(getattr(run, "risk_actions", [])):
            action = _object_mapping(raw)
            details = action.pop("details", {})
            details = dict(details) if isinstance(details, Mapping) else {"value": details}
            record = {
                "portfolio": details.pop("portfolio", str(portfolio)),
                "signal_as_of_date": details.pop(
                    "signal_as_of_date", action.pop("signal_as_of_date", None)
                ),
                "execution_date": details.pop("execution_date", action.pop("execution_date", None)),
                "control": action.pop("control", "unknown"),
                "description": action.pop("description", ""),
                "details": details,
            }
            if action:
                record["additional_fields"] = action
            records.append(record)
    columns = [
        "portfolio",
        "signal_as_of_date",
        "execution_date",
        "control",
        "description",
        "details",
    ]
    if not records:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame.from_records(records)
    return _preferred_columns(frame, columns)


def _decision_receipts(runs: Mapping[str, Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for portfolio, run in runs.items():
        raw_receipts = getattr(run, "decision_receipts", [])
        source = raw_receipts if len(raw_receipts) else getattr(run, "rebalances", [])
        for raw in _iter_records(source):
            receipt = _object_mapping(raw)
            receipt.setdefault("portfolio", str(portfolio))
            receipts.append(_to_jsonable(receipt))
    return sorted(receipts, key=_receipt_sort_key)


def _build_data_summary(market_data: Any, config: Any) -> dict[str, Any]:
    prices = getattr(market_data, "prices", pd.DataFrame())
    volumes = getattr(market_data, "volumes", pd.DataFrame())
    if not isinstance(prices, pd.DataFrame):
        prices = pd.DataFrame()
    if not isinstance(volumes, pd.DataFrame):
        volumes = pd.DataFrame()
    source = str(getattr(market_data, "source", "unknown"))
    is_synthetic = "synthetic" in source.lower()
    index = pd.to_datetime(prices.index) if not prices.empty else pd.DatetimeIndex([])
    tickers = [str(column) for column in prices.columns]
    summary: dict[str, Any] = {
        "source": source,
        "feed": str(getattr(market_data, "feed", "unknown")),
        "adjustment": str(getattr(market_data, "adjustment", "unknown")),
        "open_prices_are_approximated": bool(
            getattr(market_data, "open_prices_are_approximated", True)
        ),
        "configuration_hash": str(getattr(config, "configuration_hash", "unavailable")),
        "is_synthetic": is_synthetic,
        "source_disclosure": (
            "Deterministic synthetic data; results are software demonstrations and are not "
            "real-market evidence."
            if is_synthetic
            else "Historical market data from the recorded source; vendor and backtest "
            "limitations still apply."
        ),
        "benchmark": str(config.data.benchmark),
        "configured_tickers": [str(ticker) for ticker in config.data.tickers],
        "available_tickers": tickers,
        "start_date": index.min().date().isoformat() if len(index) else None,
        "end_date": index.max().date().isoformat() if len(index) else None,
        "trading_days": len(index),
        "price_observations": {
            str(column): int(prices[column].notna().sum()) for column in prices.columns
        },
        "missing_price_observations": {
            str(column): int(prices[column].isna().sum()) for column in prices.columns
        },
        "volume_observations": {
            str(column): int(volumes[column].notna().sum()) for column in volumes.columns
        },
    }
    normalized = _to_jsonable(summary)
    if not isinstance(normalized, dict):
        raise TypeError("Data summary normalization did not return an object")
    return normalized


def _render_receipts_markdown(
    receipts: list[dict[str, Any]],
    feed_disclosure: str,
) -> str:
    lines = [
        "# Decision Receipts",
        "",
        "> **PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS**",
        "",
        f"**{feed_disclosure}**",
        "",
        "These receipts describe a historical simulation, not forward paper-account activity.",
        "",
        "This document and the JSONL artifact contain every scheduled rebalance receipt, "
        "including skipped and rejected evaluations.",
        "",
    ]
    if not receipts:
        lines.extend(["No rebalance receipts were generated.", ""])
        return "\n".join(lines)

    for sequence, receipt in enumerate(receipts, start=1):
        portfolio = receipt.get("portfolio", "unknown")
        execution_date = receipt.get("execution_date", receipt.get("date", "date unavailable"))
        lines.extend(
            [
                f"## Receipt {sequence}: {portfolio} — {execution_date}",
                "",
                "```json",
                json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def _render_report(
    *,
    config: Any,
    data_summary: Mapping[str, Any],
    feed_disclosure: str,
    full_metrics: pd.DataFrame,
    oos_metrics: pd.DataFrame,
    development_metrics: pd.DataFrame | None,
    validation_metrics: pd.DataFrame | None,
    holdout_metrics: pd.DataFrame | None,
    risk_actions: pd.DataFrame,
) -> str:
    synthetic = bool(data_summary.get("is_synthetic", False))
    source_notice = str(data_summary.get("source_disclosure", "Source unavailable."))
    date_range = (
        f"{data_summary.get('start_date') or 'unavailable'} to "
        f"{data_summary.get('end_date') or 'unavailable'}"
    )
    universe = ", ".join(map(str, data_summary.get("available_tickers", []))) or "unavailable"
    full_comparison = _comparison_text(full_metrics, "full period")
    oos_label = (
        f"locked holdout beginning {config.backtest.holdout_period_start}"
        if config.backtest.holdout_period_start is not None
        else f"period beginning {config.backtest.out_of_sample_start}"
    )
    oos_comparison = _comparison_text(oos_metrics, oos_label)
    intervention_table = _risk_summary_markdown(risk_actions)
    frozen_window_lines: list[str] = []
    has_frozen_windows = bool(
        development_metrics is not None
        and validation_metrics is not None
        and holdout_metrics is not None
    )
    if has_frozen_windows:
        development_label = (
            f"{config.backtest.development_period_start} through "
            f"{config.backtest.development_period_end}"
        )
        validation_label = (
            f"{config.backtest.validation_period_start} through "
            f"{config.backtest.validation_period_end}"
        )
        frozen_window_lines = [
            "## 11. Frozen development and validation windows",
            "",
            f"### Development description period — {development_label}",
            "",
            _metrics_markdown(development_metrics),
            "",
            _comparison_text(development_metrics, "development description period"),
            "",
            f"### Validation description period — {validation_label}",
            "",
            _metrics_markdown(validation_metrics),
            "",
            _comparison_text(validation_metrics, "validation description period"),
            "",
            "The locked-holdout table is the configured-analysis table above and is also "
            "exported as `metrics_holdout.csv`. These boundaries were frozen before retrieval.",
            "",
        ]

    lines = [
        "# Adaptive Portfolio Agent — Research Report",
        "",
        "> **PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS**",
        "",
        f"**{feed_disclosure}**",
        "",
        "> This section reports a historical backtest, not forward paper-trading performance.",
        "",
    ]
    if synthetic:
        lines.extend(
            [
                "> **Synthetic-data disclosure:** This run used deterministic synthetic data. "
                "Its performance is a software demonstration, not real-market evidence.",
                "",
            ]
        )
    lines.extend(
        [
            "## 1. Executive summary",
            "",
            "This report evaluates a transparent regime-aware blend of momentum and "
            "mean-reversion portfolios against static strategy and buy-and-hold baselines. "
            "All active portfolios are long-only and their proposals pass through an "
            "independent risk engine. Results are descriptive historical-simulation evidence, "
            "not a claim of profitability.",
            "",
            full_comparison,
            "",
            source_notice,
            "",
            "![Equity curves](equity_curves.png)",
            "",
            "## 2. Research question",
            "",
            "Can regime-aware allocation between momentum and mean-reversion strategies "
            "produce better risk-adjusted performance than static strategy allocation while "
            "complying with strict portfolio risk constraints? The experiment does not assume "
            "the answer is yes.",
            "",
            "## 3. Data and universe",
            "",
            f"- Source: `{data_summary.get('source', 'unknown')}`",
            f"- Available range: {date_range}",
            f"- Trading dates: {data_summary.get('trading_days', 0)}",
            f"- Universe: {universe}",
            f"- Benchmark: {config.data.benchmark}",
            "",
            "Adjusted daily prices are used for returns. The loader validates required symbols, "
            "ordering, positive finite prices, duplicate observations, gaps, and sufficient "
            "history. It does not silently fill long missing periods.",
            "",
            "## 4. Strategy definitions",
            "",
            f"**Momentum.** Rank trailing {config.momentum.lookback_days}-trading-day returns, "
            f"select up to {config.momentum.top_n} assets"
            + (
                " with positive returns"
                if config.momentum.require_positive_return
                else " regardless of return sign"
            )
            + f", and assign inverse-volatility weights using the prior "
            f"{config.momentum.volatility_lookback_days} returns.",
            "",
            f"**Mean reversion.** Require a {config.mean_reversion.zscore_lookback_days}-day "
            f"price z-score below {config.mean_reversion.entry_zscore:.2f} and price above its "
            f"{config.mean_reversion.long_term_trend_days}-day moving average. Select up to "
            f"{config.mean_reversion.top_n} most-negative qualifying signals and weight them "
            "by inverse volatility. Either strategy may return all cash.",
            "",
            "## 5. Regime definition",
            "",
            f"The {config.regime.benchmark} regime is bull when its trailing "
            f"{config.regime.fast_moving_average_days}-day moving average is at least its "
            f"{config.regime.slow_moving_average_days}-day moving average; otherwise it is "
            "bear. Current annualized realized volatility uses "
            f"{config.regime.volatility_lookback_days} days and is high when it exceeds the "
            f"trailing {config.regime.volatility_threshold_lookback_days}-observation median "
            "of that volatility series. These rules yield exactly four states.",
            "",
            "![Regime timeline](regime_timeline.png)",
            "",
            "![Strategy allocations](strategy_allocations.png)",
            "",
            "## 6. Risk controls",
            "",
            "The risk engine is separate from strategy and allocation code. In deterministic "
            "order it checks data and finite values, prohibits short positions, clips any "
            f"position above {config.risk.max_position_weight:.1%}, caps gross exposure at "
            f"{config.risk.max_gross_exposure:.1%}, scales down above the "
            f"{config.risk.target_annual_volatility:.1%} annual volatility target, applies "
            f"a {config.risk.soft_limit_max_gross_exposure:.1%} risky-exposure cap at a "
            f"-{config.risk.drawdown_soft_limit:.1%} drawdown, moves to cash and latches at "
            f"-{config.risk.drawdown_hard_limit:.1%}, and limits ordinary rebalance turnover "
            f"to {config.risk.max_turnover_per_rebalance:.1%}. A safety-critical hard stop is "
            "not throttled by the turnover limit. Clipped exposure remains cash.",
            "",
            "![Risk interventions](risk_interventions.png)",
            "",
            "## 7. Anti-look-ahead design",
            "",
            "For an execution on trading date `t`, strategies, regime features, covariance "
            "estimates, and risk decisions receive data only through the immediately preceding "
            "trading date `t-1`. Holdings carried from `t-1` receive the overnight return, the "
            "rebalance is modeled at the adjusted session-`t` open, and the new holdings receive "
            "only the open-to-close return. When a legacy data source has no open, its prior close "
            "is recorded as an explicit open-price proxy. Receipts disclose which path was used. "
            "This open-boundary model approximates, but does not reproduce, the live paper "
            "service's configured 10:05 ET evaluation or the intervening intraday price path.",
            "",
            "## 8. Transaction-cost assumptions",
            "",
            f"One-way turnover includes risky assets and cash. Each execution deducts `turnover "
            f"x ({config.backtest.transaction_cost_bps:.2f} transaction-cost bps + "
            f"{config.backtest.slippage_bps:.2f} slippage bps) / 10,000` from portfolio equity. "
            "The constant basis-point model omits spread variation, market impact, capacity, "
            "partial fills, taxes, financing, and cash interest.",
            "",
            "## 9. Full-period metrics",
            "",
            _metrics_markdown(full_metrics),
            "",
            "![Equity curves on a log scale](equity_curves_log_scale.png)",
            "",
            "![Drawdowns](drawdowns.png)",
            "",
            (
                "## 10. Locked-holdout metrics"
                if config.backtest.holdout_period_start is not None
                else "## 10. Metrics from the configured analysis start"
            ),
            "",
            (
                f"The locked holdout begins on `{config.backtest.holdout_period_start}` and "
                "continues through the last completed session in this run."
                if config.backtest.holdout_period_start is not None
                else f"The period begins on `{config.backtest.out_of_sample_start}`. It is a "
                "post-start analysis period, not a pure machine-learning holdout."
            ),
            "",
            _metrics_markdown(oos_metrics),
            "",
            "![Rolling Sharpe ratio](rolling_sharpe.png)",
            "",
            *frozen_window_lines,
            f"## {12 if has_frozen_windows else 11}. Did adaptive allocation help?",
            "",
            full_comparison,
            "",
            oos_comparison,
            "",
            "These comparisons are descriptive and do not establish statistical significance. "
            "The adaptive-versus-static comparison is the principal allocator test because "
            "both use the same component strategies and independent risk engine.",
            "",
            f"## {13 if has_frozen_windows else 12}. Risk-intervention summary",
            "",
            intervention_table,
            "",
            "![Adaptive asset and cash weights](adaptive_asset_weights.png)",
            "",
            f"## {14 if has_frozen_windows else 13}. Limitations",
            "",
            "- Historical or synthetic simulation cannot demonstrate future profitability.",
            "- A present-day ETF universe introduces selection and survivorship bias.",
            "- Daily data omit intraday execution paths and liquidity constraints.",
            "- Repeated parameter revision can overfit even transparent rules.",
            "- Volatility and covariance estimates are backward-looking and unstable around "
            "structural breaks.",
            "- The regime rule is lagging and does not predict that a state will persist.",
            "- Buy-and-hold references do not receive the active portfolios' periodic risk "
            "treatment, so they provide context rather than a controlled risk-policy ablation.",
            "- Cash earns zero and implementation costs are simplified.",
            "- Open-boundary historical execution approximates the 10:05 ET forward-paper "
            "decision time; it is not a 10:05 fill simulation.",
            "",
            f"## {15 if has_frozen_windows else 14}. Proposed semester-long extensions",
            "",
            "Priorities include walk-forward parameter selection, point-in-time universes, "
            "bootstrap uncertainty estimates, robust covariance estimators, cost and capacity "
            "models, factor attribution, sensitivity maps, and transparent alternative regime "
            "detectors evaluated against this baseline.",
            "",
            f"## {16 if has_frozen_windows else 15}. Educational-use statement",
            "",
            "**Adaptive Portfolio Agent is educational, paper-only research. It is not "
            "investment advice, offers no guarantee of performance, and structurally cannot "
            "place real-money orders. Paper-account capital and fills are simulated.**",
            "",
        ]
    )
    return "\n".join(lines)


def _comparison_text(metrics: pd.DataFrame, period_label: str) -> str:
    indexed = metrics.set_index("portfolio") if "portfolio" in metrics else pd.DataFrame()
    if "adaptive" not in indexed.index or "static_blend" not in indexed.index:
        return (
            f"A direct adaptive-versus-static conclusion for the {period_label} is unavailable "
            "because one or both comparison portfolios were not produced."
        )
    adaptive = indexed.loc["adaptive"]
    static = indexed.loc["static_blend"]
    required = ("sharpe_ratio", "total_return", "maximum_drawdown")
    undefined: list[str] = []
    for portfolio, row in (("adaptive", adaptive), ("static_blend", static)):
        for metric_name in required:
            if _finite_number(row.get(metric_name)) is None:
                reason = row.get(f"{metric_name}_reason")
                explanation = (
                    str(reason)
                    if reason is not None and not pd.isna(reason) and str(reason).strip()
                    else "no finite value or explanation was recorded"
                )
                undefined.append(
                    f"{portfolio} {_METRIC_LABELS[metric_name].lower()}: {explanation}"
                )
    if undefined:
        return (
            f"A direct adaptive-versus-static conclusion for the {period_label} is unavailable "
            "because one or more required metrics are undefined. " + " ".join(undefined)
        )

    adaptive_sharpe = float(adaptive["sharpe_ratio"])
    static_sharpe = float(static["sharpe_ratio"])
    difference = adaptive_sharpe - static_sharpe
    tolerance = 1e-12
    if difference > tolerance:
        conclusion = "Adaptive allocation improved the realized Sharpe ratio"
    elif difference < -tolerance:
        conclusion = "Adaptive allocation did not improve the realized Sharpe ratio"
    else:
        conclusion = "Adaptive and static allocation had the same realized Sharpe ratio"
    return (
        f"For the {period_label}, adaptive Sharpe was {adaptive_sharpe:.3f} versus "
        f"{static_sharpe:.3f} for the static blend (difference {difference:+.3f}). "
        f"{conclusion}. Adaptive total return was {float(adaptive['total_return']):.2%} "
        f"versus {float(static['total_return']):.2%}, and maximum drawdown was "
        f"{float(adaptive['maximum_drawdown']):.2%} versus "
        f"{float(static['maximum_drawdown']):.2%}."
    )


def _metrics_markdown(metrics: pd.DataFrame) -> str:
    if metrics.empty:
        return "No metrics were available for this period."
    selected = [
        "total_return",
        "cagr",
        "annualized_volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "maximum_drawdown",
        "calmar_ratio",
        "var_95",
        "cvar_95",
        "average_cash_allocation",
        "total_turnover",
        "number_of_risk_interventions",
        "number_of_hard_stop_events",
    ]
    headers = ["Portfolio", *(_METRIC_LABELS[column] for column in selected)]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---", *("---:" for _ in selected)]) + " |",
    ]
    for _, row in metrics.iterrows():
        values = [str(row["portfolio"])]
        values.extend(_format_metric(column, row[column]) for column in selected)
        lines.append("| " + " | ".join(values) + " |")
    explanations: list[str] = []
    for _, row in metrics.iterrows():
        portfolio = str(row["portfolio"])
        for metric_name in _METRIC_COLUMNS:
            if _finite_number(row.get(metric_name)) is not None:
                continue
            reason = row.get(f"{metric_name}_reason")
            if reason is None or pd.isna(reason) or not str(reason).strip():
                reason = "No explanation was recorded."
            explanations.append(
                f"- **{portfolio} — {_METRIC_LABELS[metric_name]}:** {str(reason).strip()}"
            )
    if explanations:
        lines.extend(
            [
                "",
                "Undefined metrics are shown as `n/a`; they are not zero:",
                "",
                *explanations,
            ]
        )
    return "\n".join(lines)


def _risk_summary_markdown(risk_actions: pd.DataFrame) -> str:
    if risk_actions.empty:
        return "No risk controls modified a portfolio proposal during this run."
    summary = (
        risk_actions.groupby(["portfolio", "control"], dropna=False)
        .size()
        .rename("actions")
        .reset_index()
    )
    lines = [
        "| Portfolio | Control | Recorded actions |",
        "| --- | --- | ---: |",
    ]
    for row in summary.itertuples(index=False):
        lines.append(f"| {row.portfolio} | {row.control} | {int(row.actions)} |")
    return "\n".join(lines)


def _plot_equity(
    runs: Mapping[str, Any],
    path: Path,
    *,
    logarithmic: bool,
    feed_disclosure: str,
) -> None:
    fig, axis = plt.subplots(figsize=(11, 6))
    plotted = False
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        values = _column(daily, "equity")
        if values.empty:
            continue
        if logarithmic:
            values = values.where(values > 0.0)
        axis.plot(values.index, values, label=str(portfolio), linewidth=1.5)
        plotted = True
    if logarithmic:
        axis.set_yscale("log")
    axis.set_title("Portfolio Equity Curves" + (" — Log Scale" if logarithmic else ""))
    axis.set_xlabel("Date")
    axis.set_ylabel("Portfolio equity (currency units)")
    _finish_date_axis(axis, plotted)
    _save_figure(fig, path, feed_disclosure)


def _plot_drawdowns(runs: Mapping[str, Any], path: Path, *, feed_disclosure: str) -> None:
    fig, axis = plt.subplots(figsize=(11, 6))
    plotted = False
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        values = _column(daily, "drawdown")
        if values.empty:
            continue
        axis.plot(values.index, values * 100.0, label=str(portfolio), linewidth=1.4)
        plotted = True
    axis.axhline(0.0, color="black", linewidth=0.7)
    axis.set_title("Portfolio Drawdowns")
    axis.set_xlabel("Date")
    axis.set_ylabel("Drawdown (%)")
    _finish_date_axis(axis, plotted)
    _save_figure(fig, path, feed_disclosure)


def _plot_regimes(runs: Mapping[str, Any], path: Path, *, feed_disclosure: str) -> None:
    fig, axis = plt.subplots(figsize=(11, 4.8))
    run = _named_run(runs, "adaptive")
    regimes = getattr(run, "regimes", pd.Series(dtype="object")) if run is not None else None
    plotted = isinstance(regimes, pd.Series) and not regimes.empty
    if isinstance(regimes, pd.Series) and not regimes.empty:
        series = regimes.copy()
        series.index = pd.to_datetime(series.index)
        series = series.sort_index().astype(str)
        codes = series.map({name: index for index, name in enumerate(_REGIME_ORDER)})
        valid = codes.notna()
        axis.step(codes.index[valid], codes[valid], where="post", color="#315a7d")
        axis.scatter(codes.index[valid], codes[valid], c=codes[valid], cmap="viridis", s=12)
        axis.set_yticks(range(len(_REGIME_ORDER)), labels=_REGIME_ORDER)
    axis.set_title("Adaptive Portfolio Regime Timeline")
    axis.set_xlabel("Execution date")
    axis.set_ylabel("Detected regime")
    _finish_date_axis(axis, bool(plotted), legend=False)
    _save_figure(fig, path, feed_disclosure)


def _plot_strategy_allocations(
    runs: Mapping[str, Any], path: Path, *, feed_disclosure: str
) -> None:
    fig, axis = plt.subplots(figsize=(11, 5.5))
    run = _named_run(runs, "adaptive")
    allocations = (
        _dated_frame(getattr(run, "strategy_allocations", pd.DataFrame()))
        if run is not None
        else pd.DataFrame()
    )
    columns = [
        column
        for column in ("momentum", "mean_reversion", "strategic_cash")
        if column in allocations
    ]
    plotted = bool(columns) and not allocations.empty
    if plotted:
        numeric = allocations[columns].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        axis.stackplot(numeric.index, *[numeric[column] for column in columns], labels=columns)
    axis.set_title("Adaptive Strategy Allocations")
    axis.set_xlabel("Execution date")
    axis.set_ylabel("Capital allocation")
    axis.set_ylim(0.0, 1.0)
    _finish_date_axis(axis, plotted)
    _save_figure(fig, path, feed_disclosure)


def _plot_adaptive_weights(runs: Mapping[str, Any], path: Path, *, feed_disclosure: str) -> None:
    fig, axis = plt.subplots(figsize=(11, 6))
    run = _named_run(runs, "adaptive")
    weights = (
        _dated_frame(getattr(run, "weights", pd.DataFrame())) if run is not None else pd.DataFrame()
    )
    plotted = not weights.empty
    if plotted:
        numeric = weights.apply(pd.to_numeric, errors="coerce").fillna(0.0).clip(lower=0.0)
        axis.stackplot(numeric.index, *[numeric[column] for column in numeric], labels=numeric)
    axis.set_title("Adaptive Portfolio Asset and Cash Weights")
    axis.set_xlabel("Date")
    axis.set_ylabel("Portfolio weight")
    axis.set_ylim(0.0, 1.0)
    _finish_date_axis(axis, plotted)
    _save_figure(fig, path, feed_disclosure)


def _plot_rolling_sharpe(
    runs: Mapping[str, Any],
    path: Path,
    *,
    window: int,
    annualization_factor: int,
    feed_disclosure: str,
) -> None:
    fig, axis = plt.subplots(figsize=(11, 6))
    plotted = False
    for portfolio, run in runs.items():
        daily = _dated_frame(getattr(run, "daily", pd.DataFrame()))
        returns = _column(daily, "daily_return")
        if returns.empty:
            continue
        rolling = rolling_sharpe_ratio(
            returns,
            window=window,
            annualization_factor=annualization_factor,
        )
        if rolling.notna().any():
            axis.plot(rolling.index, rolling, label=str(portfolio), linewidth=1.2)
            plotted = True
    axis.axhline(0.0, color="black", linewidth=0.7)
    axis.set_title(f"Rolling {window}-Day Sharpe Ratio")
    axis.set_xlabel("Date")
    axis.set_ylabel("Annualized Sharpe ratio")
    _finish_date_axis(axis, plotted)
    _save_figure(fig, path, feed_disclosure)


def _plot_risk_interventions(
    risk_actions: pd.DataFrame, path: Path, *, feed_disclosure: str
) -> None:
    fig, axis = plt.subplots(figsize=(11, 5.5))
    plotted = not risk_actions.empty
    if plotted:
        counts = risk_actions.pivot_table(
            index="control",
            columns="portfolio",
            values="description",
            aggfunc="count",
            fill_value=0,
        )
        counts.plot(kind="bar", ax=axis, width=0.8)
    axis.set_title("Recorded Risk Interventions by Control")
    axis.set_xlabel("Risk control")
    axis.set_ylabel("Number of recorded actions")
    if plotted:
        axis.legend(title="Portfolio", loc="best")
        axis.tick_params(axis="x", rotation=30)
    else:
        _empty_axis(axis)
    _save_figure(fig, path, feed_disclosure)


def _finish_date_axis(axis: Any, plotted: bool, *, legend: bool = True) -> None:
    if plotted:
        locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
        axis.xaxis.set_major_locator(locator)
        axis.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        if legend:
            axis.legend(loc="best", fontsize="small", ncols=2)
        axis.grid(alpha=0.2)
    else:
        _empty_axis(axis)


def _empty_axis(axis: Any) -> None:
    axis.text(
        0.5,
        0.5,
        "No observations available",
        ha="center",
        va="center",
        transform=axis.transAxes,
    )
    axis.grid(False)


def _save_figure(figure: Any, path: Path, feed_disclosure: str) -> None:
    figure.text(
        0.5,
        0.002,
        "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS\n"
        f"{feed_disclosure}\n"
        "HISTORICAL BACKTEST — simulated execution; not forward paper-trading performance",
        ha="center",
        va="bottom",
        fontsize=7,
        color="dimgray",
    )
    figure.tight_layout(rect=(0.0, 0.06, 1.0, 1.0))
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def _dated_frame(frame: Any) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        return pd.DataFrame()
    result = frame.copy()
    if result.empty:
        return result
    result.index = pd.to_datetime(result.index)
    return result.sort_index()


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


def _iter_records(records: Any) -> Iterable[Any]:
    if isinstance(records, pd.DataFrame):
        frame = records.copy()
        if frame.index.name and frame.index.name not in frame.columns:
            frame = frame.reset_index()
        values = frame.to_dict(orient="records")
        return values if isinstance(values, list) else []
    if records is None:
        return []
    if isinstance(records, (list, tuple)):
        return records
    return [records]


def _count_records(
    records: Any,
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp | None = None,
) -> int:
    items = list(_iter_records(records))
    if start_date is None and end_date is None:
        return len(items)
    count = 0
    for item in items:
        timestamp = _record_date(item)
        if timestamp is None:
            continue
        if start_date is not None and timestamp < start_date:
            continue
        if end_date is not None and timestamp > end_date:
            continue
        count += 1
    return count


def _record_date(record: Any) -> pd.Timestamp | None:
    mapping = _object_mapping(record)
    details = mapping.get("details", {})
    candidates = (
        mapping.get("execution_date"),
        mapping.get("date"),
        mapping.get("as_of_date"),
        details.get("execution_date") if isinstance(details, Mapping) else None,
        details.get("date") if isinstance(details, Mapping) else None,
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            timestamp = pd.Timestamp(candidate)
        except (TypeError, ValueError):
            continue
        if not pd.isna(timestamp):
            return timestamp
    return None


def _hard_stop_events(
    daily: pd.DataFrame,
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp | None = None,
) -> int:
    if "hard_stop_latched" not in daily:
        return 0
    latched = daily["hard_stop_latched"].fillna(False).astype(bool)
    events = latched & ~latched.shift(fill_value=False)
    if start_date is not None:
        events = events.loc[events.index >= start_date]
    if end_date is not None:
        events = events.loc[events.index <= end_date]
    return int(events.sum())


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "as_dict", None)
    if callable(method):
        converted = method()
        if isinstance(converted, Mapping):
            return dict(converted)
    method = getattr(value, "to_dict", None)
    if callable(method):
        converted = method()
        if isinstance(converted, Mapping):
            return dict(converted)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return dict(attributes)
    return {"value": value}


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, pd.Series):
        return [_to_jsonable(item) for item in value.tolist()]
    if isinstance(value, pd.DataFrame):
        return [_to_jsonable(item) for item in value.to_dict(orient="records")]
    if isinstance(value, np.ndarray):
        return [_to_jsonable(item) for item in value.tolist()]
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _csv_safe(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        dtype = result[column].dtype
        if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
            result[column] = result[column].map(_csv_value)
    return result


def _csv_value(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple, set, np.ndarray)) or (
        is_dataclass(value) and not isinstance(value, type)
    ):
        return json.dumps(_to_jsonable(value), sort_keys=True, allow_nan=False)
    return _to_jsonable(value)


def _preferred_columns(frame: pd.DataFrame, preferred: list[str]) -> pd.DataFrame:
    ordered = [column for column in preferred if column in frame]
    ordered.extend(column for column in frame if column not in ordered)
    return frame.reindex(columns=ordered)


def _receipt_sort_key(receipt: Mapping[str, Any]) -> tuple[str, str]:
    execution = receipt.get("execution_date", receipt.get("date", ""))
    return str(execution), str(receipt.get("portfolio", ""))


def _receipt_has_actions(receipt: Mapping[str, Any]) -> bool:
    candidates = [
        receipt.get("risk_actions"),
        receipt.get("risk_interventions"),
        receipt.get("actions"),
    ]
    risk_decision = receipt.get("risk_decision")
    if isinstance(risk_decision, Mapping):
        candidates.append(risk_decision.get("actions"))
    return any(bool(candidate) for candidate in candidates)


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _format_metric(name: str, value: Any) -> str:
    number = _finite_number(value)
    if number is None:
        return "n/a"
    if name in _COUNT_METRICS:
        return str(round(number))
    if name in _PERCENT_METRICS:
        return f"{number:.2%}"
    return f"{number:.3f}"


def _named_run(runs: Mapping[str, Any], desired_name: str) -> Any | None:
    if desired_name in runs:
        return runs[desired_name]
    for name, run in runs.items():
        if str(name).lower() == desired_name.lower():
            return run
    return None
