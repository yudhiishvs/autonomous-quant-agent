"""Read-only forward paper-trading exports and static plots."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

_CACHE_ROOT = Path(tempfile.gettempdir()) / "adaptive-portfolio-agent-cache"
_MPL_CONFIG = _CACHE_ROOT / "matplotlib"
_MPL_CONFIG.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("XDG_CACHE_HOME", str(_CACHE_ROOT))
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CONFIG))

import matplotlib  # noqa: E402

matplotlib.use("Agg", force=True)
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

PAPER_BANNER = "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
IEX_BANNER = "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET"
SIP_BANNER = "REAL-TIME SIP FEED"
_SENSITIVE_FIELD_PARTS = ("secret", "api_key", "authorization", "credential", "token")

TABLE_EXPORTS = {
    "daily_performance": "forward_daily_performance.csv",
    "position_snapshots": "forward_positions.csv",
    "broker_orders": "forward_orders.csv",
    "fill_events": "forward_fills.csv",
    "risk_actions": "forward_risk_actions.csv",
}

EXPORT_COLUMNS = {
    "daily_performance": (
        "session_date",
        "segment_id",
        "start_equity",
        "end_equity",
        "external_cash_flow",
        "daily_return",
        "cumulative_return",
        "daily_pnl",
        "drawdown",
        "gross_exposure",
        "cash_allocation",
        "turnover",
        "continuity_status",
        "undefined_reason",
    ),
    "position_snapshots": (
        "timestamp",
        "symbol",
        "quantity",
        "market_value",
        "average_entry_price",
        "current_price",
        "unrealized_pl",
    ),
    "broker_orders": (
        "client_order_id",
        "broker_order_id",
        "decision_id",
        "symbol",
        "side",
        "state",
        "filled_quantity",
        "average_fill_price",
        "last_update_at",
    ),
    "fill_events": (
        "fill_id",
        "client_order_id",
        "broker_order_id",
        "decision_id",
        "created_at",
        "symbol",
        "side",
        "quantity",
        "price",
    ),
    "risk_actions": (
        "risk_action_id",
        "decision_id",
        "created_at",
        "control",
        "description",
    ),
}


def _safe_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove any accidentally persisted credential-like field from an export."""

    allowed = [
        name for name in frame if not any(part in name.lower() for part in _SENSITIVE_FIELD_PARTS)
    ]
    return frame.loc[:, allowed]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return {str(key): item for key, item in parsed.items()} if isinstance(parsed, dict) else {}


def _expand_payload(frame: pd.DataFrame) -> pd.DataFrame:
    """Flatten scalar JSON payload fields from the production audit schema."""

    if frame.empty or "payload" not in frame:
        return frame
    expanded = pd.json_normalize([_json_object(value) for value in frame["payload"]])
    expanded.index = frame.index
    result = frame.drop(columns=["payload"])
    for column in expanded:
        if column not in result:
            result[column] = expanded[column]
    aliases = {
        "momentum_weight": (
            "allocation.strategy_allocations.momentum",
            "strategy_allocations.momentum",
        ),
        "mean_reversion_weight": (
            "allocation.strategy_allocations.mean_reversion",
            "strategy_allocations.mean_reversion",
        ),
        "strategic_cash_weight": (
            "allocation.strategy_allocations.strategic_cash",
            "strategy_allocations.strategic_cash",
        ),
        "continuity_status": ("continuity_flag",),
        "undefined_reason": ("return_unavailable_reason",),
    }
    for target, candidates in aliases.items():
        source = next((name for name in candidates if name in result), None)
        if target not in result and source is not None:
            result[target] = result[source]
    return result


def _canonical_export(frame: pd.DataFrame, table: str) -> pd.DataFrame:
    frame = _expand_payload(frame)
    if not frame.empty:
        return frame
    return pd.DataFrame(columns=EXPORT_COLUMNS.get(table, ()))


def _read_tables(database_path: Path, tables: Iterable[str]) -> dict[str, pd.DataFrame]:
    frames = {name: pd.DataFrame() for name in tables}
    if not database_path.is_file():
        return frames
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=3.0)) as connection:
            existing = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in frames:
                if table in existing:
                    frames[table] = _safe_columns(
                        pd.read_sql_query(f'SELECT * FROM "{table}" ORDER BY rowid', connection)
                    )
    except sqlite3.Error:
        return frames
    return frames


def _first_column(frame: pd.DataFrame, names: Iterable[str]) -> str | None:
    return next((name for name in names if name in frame.columns), None)


def _number(frame: pd.DataFrame, names: Iterable[str]) -> float | None:
    column = _first_column(frame, names)
    if frame.empty or column is None:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else None


def _date_axis(frame: pd.DataFrame) -> tuple[pd.Series, str | None]:
    column = _first_column(
        frame,
        ("session_date", "date", "timestamp", "created_at", "recorded_at", "as_of"),
    )
    if column is None:
        return pd.Series(dtype="datetime64[ns, UTC]"), None
    return pd.to_datetime(frame[column], errors="coerce", utc=True), column


def _format_metric(value: float | None, *, percent: bool = False, money: bool = False) -> str:
    if value is None:
        return "Not available"
    if percent:
        return f"{value:.2%}"
    if money:
        return f"${value:,.2f}"
    return f"{value:,.4f}"


def _unique_ids(frame: pd.DataFrame, column: str) -> set[str]:
    if frame.empty or column not in frame:
        return set()
    return {
        str(value)
        for value in frame[column].dropna()
        if str(value).strip() and str(value).lower() != "nan"
    }


def _order_execution_metrics(
    orders: pd.DataFrame,
    events: pd.DataFrame,
    fills: pd.DataFrame,
    intents: pd.DataFrame,
) -> dict[str, Any]:
    """Calculate paper execution statistics without inventing zero denominators."""

    order_ids = _unique_ids(orders, "client_order_id")
    order_count = len(order_ids)
    state_column = _first_column(orders, ("state", "raw_status", "status"))
    filled_ids: set[str] = set()
    rejected_ids: set[str] = set()
    partial_ids: set[str] = set()
    if state_column is not None and "client_order_id" in orders:
        states = orders[state_column].fillna("").astype(str).str.lower()
        filled_ids |= _unique_ids(orders.loc[states.eq("filled")], "client_order_id")
        rejected_ids |= _unique_ids(orders.loc[states.eq("rejected")], "client_order_id")
        partial_ids |= _unique_ids(
            orders.loc[states.isin({"partial_fill", "partially_filled"})],
            "client_order_id",
        )
    event_state = _first_column(events, ("to_state", "event_type"))
    if event_state is not None and "client_order_id" in events:
        states = events[event_state].fillna("").astype(str).str.lower()
        filled_ids |= _unique_ids(events.loc[states.isin({"fill", "filled"})], "client_order_id")
        rejected_ids |= _unique_ids(events.loc[states.eq("rejected")], "client_order_id")
        partial_ids |= _unique_ids(
            events.loc[states.isin({"partial_fill", "partially_filled"})],
            "client_order_id",
        )

    fill_rate = len(filled_ids & order_ids) / order_count if order_count else None
    partial_fill_rate = len(partial_ids & order_ids) / order_count if order_count else None
    rejection_rate = len(rejected_ids & order_ids) / order_count if order_count else None

    slippage_bps: float | None = None
    usable_slippage_count = 0
    if (
        not fills.empty
        and not intents.empty
        and "client_order_id" in fills
        and "client_order_id" in intents
        and "price" in fills
        and "reference_price" in intents
    ):
        intent_columns = ["client_order_id", "reference_price"]
        if "side" in intents:
            intent_columns.append("side")
        joined = fills.merge(
            intents[intent_columns].drop_duplicates("client_order_id", keep="last"),
            on="client_order_id",
            how="inner",
            suffixes=("_fill", "_intent"),
        )
        joined["_fill_price"] = pd.to_numeric(joined["price"], errors="coerce")
        joined["_reference"] = pd.to_numeric(joined["reference_price"], errors="coerce")
        joined["_quantity"] = pd.to_numeric(
            joined.get("quantity", 1.0),
            errors="coerce",
        )
        side_column = (
            "side_fill"
            if "side_fill" in joined
            else ("side_intent" if "side_intent" in joined else "side")
        )
        sides = (
            joined[side_column].fillna("").astype(str).str.lower()
            if side_column in joined
            else pd.Series("", index=joined.index)
        )
        usable = (
            joined["_fill_price"].notna()
            & joined["_reference"].gt(0)
            & joined["_quantity"].gt(0)
            & sides.isin({"buy", "sell"})
        )
        if usable.any():
            sample = joined.loc[usable]
            sample_sides = sides.loc[usable]
            adverse_fraction = (sample["_fill_price"] - sample["_reference"]) / sample["_reference"]
            adverse_fraction = adverse_fraction.where(sample_sides.eq("buy"), -adverse_fraction)
            weights = sample["_quantity"] * sample["_reference"]
            slippage_bps = float((adverse_fraction * weights).sum() / weights.sum() * 10_000.0)
            usable_slippage_count = int(usable.sum())

    return {
        "paper_order_count": order_count,
        "paper_fully_filled_order_count": len(filled_ids & order_ids),
        "paper_partial_fill_order_count": len(partial_ids & order_ids),
        "paper_rejection_count": len(rejected_ids & order_ids),
        "paper_fill_count": len(fills),
        "paper_fill_rate": fill_rate,
        "paper_fill_rate_undefined_reason": (
            None if fill_rate is not None else "No submitted paper orders have been recorded."
        ),
        "paper_partial_fill_rate": partial_fill_rate,
        "paper_partial_fill_rate_undefined_reason": (
            None
            if partial_fill_rate is not None
            else "No submitted paper orders have been recorded."
        ),
        "paper_rejection_rate": rejection_rate,
        "paper_rejection_rate_undefined_reason": (
            None if rejection_rate is not None else "No submitted paper orders have been recorded."
        ),
        "average_paper_slippage_bps": slippage_bps,
        "average_paper_slippage_observation_count": usable_slippage_count,
        "average_paper_slippage_undefined_reason": (
            None
            if slippage_bps is not None
            else "No paper fill has a usable linked order-intent reference price."
        ),
    }


def _regime_time_metrics(regimes: pd.DataFrame) -> dict[str, Any]:
    names = ("bull_low_vol", "bull_high_vol", "bear_low_vol", "bear_high_vol")
    column = _first_column(regimes, ("regime.name", "name", "classification", "regime"))
    if regimes.empty or column is None:
        return {
            **{f"time_in_regime_{name}": None for name in names},
            "time_in_regime_undefined_reason": "No forward regime observations have been recorded.",
        }
    observed = regimes[column].dropna().astype(str).str.lower()
    observed = observed[observed.isin(names)]
    if observed.empty:
        return {
            **{f"time_in_regime_{name}": None for name in names},
            "time_in_regime_undefined_reason": "Forward regime records contain no recognized classification.",
        }
    return {
        **{f"time_in_regime_{name}": float(observed.eq(name).mean()) for name in names},
        "time_in_regime_undefined_reason": None,
    }


def _outage_count(events: pd.DataFrame) -> int:
    column = _first_column(events, ("event_type", "type"))
    if events.empty or column is None:
        return 0
    active: dict[str, bool] = {}
    outages = 0
    for _, row in events.iterrows():
        stream = str(row.get("stream") or "unknown").lower()
        value = str(row.get(column) or "").lower()
        is_outage = any(
            marker in value
            for marker in (
                "disconnect",
                "outage",
                "unhealthy",
                "reconnect_exhausted",
                "recovery_incomplete",
                "stale",
            )
        )
        is_recovery = any(
            token in value.split("_") for token in ("recovered", "resolved", "healthy", "connected")
        )
        if is_outage and not is_recovery:
            if not active.get(stream, False):
                outages += 1
            active[stream] = True
        elif is_recovery:
            active[stream] = False
    return outages


def _latest_series_sessions(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse restart projections to one latest row per logical series/session."""

    if frame.empty or "session_date" not in frame:
        return frame
    keys = ["session_date"]
    if "series_id" in frame:
        keys.insert(0, "series_id")
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def _save_placeholder_or_series(
    path: Path,
    frame: pd.DataFrame,
    columns: Iterable[str],
    *,
    title: str,
    ylabel: str,
    feed_banner: str,
) -> None:
    figure, axis = plt.subplots(figsize=(10, 5.5))
    dates, _ = _date_axis(frame)
    plotted = False
    for column in columns:
        if column not in frame:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        usable = dates.notna() & values.notna()
        if usable.any():
            axis.plot(dates[usable], values[usable], label=column.replace("_", " ").title())
            plotted = True
    if plotted:
        axis.legend(loc="best")
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        figure.autofmt_xdate()
    else:
        axis.text(
            0.5,
            0.5,
            "No forward paper observations recorded",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
    axis.set_title(title)
    axis.set_xlabel("Date (UTC)")
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.25)
    figure.text(0.5, 0.015, f"{PAPER_BANNER} · {feed_banner}", ha="center", fontsize=8)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _save_intervention_plot(path: Path, actions: pd.DataFrame, feed_banner: str) -> None:
    figure, axis = plt.subplots(figsize=(10, 5.5))
    control = _first_column(actions, ("control", "action_type", "event_type", "reason"))
    if not actions.empty and control:
        counts = actions[control].fillna("unknown").astype(str).value_counts().sort_values()
        counts.plot(kind="barh", ax=axis, color="#d97706")
    else:
        axis.text(
            0.5,
            0.5,
            "No forward paper risk interventions recorded",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
    axis.set_title("Forward Paper Risk Interventions")
    axis.set_xlabel("Count")
    axis.set_ylabel("Risk control")
    axis.grid(axis="x", alpha=0.25)
    figure.text(0.5, 0.015, f"{PAPER_BANNER} · {feed_banner}", ha="center", fontsize=8)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _json_value(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_FIELD_PARTS)
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _write_receipts(
    path: Path,
    receipts: pd.DataFrame,
    *,
    paper_disclosure: str,
    feed_disclosure: str,
) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in receipts.to_dict(orient="records"):
            payload_column = next(
                (name for name in ("payload_json", "receipt_json", "payload") if name in row),
                None,
            )
            if payload_column and isinstance(row[payload_column], str):
                try:
                    payload = json.loads(row[payload_column])
                except json.JSONDecodeError:
                    payload = {key: _json_value(value) for key, value in row.items()}
            else:
                payload = {key: _json_value(value) for key, value in row.items()}
            if not isinstance(payload, dict):
                payload = {"receipt": _json_value(payload)}
            payload["paper_disclosure"] = paper_disclosure
            payload["feed_disclosure"] = feed_disclosure
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def generate_forward_outputs(
    database_path: str | Path,
    output_directory: str | Path,
    *,
    feed: str,
) -> dict[str, Path]:
    """Generate read-only forward-paper exports from the audit database.

    Empty, clearly labeled artifacts are still produced before the first observer run so
    operators can verify the reporting pipeline without inventing account performance.
    """

    normalized_feed = feed.upper()
    if normalized_feed not in {"IEX", "SIP"}:
        raise ValueError(f"Unsupported or unconfirmed market-data feed: {feed!r}")
    feed_banner = IEX_BANNER if normalized_feed == "IEX" else SIP_BANNER
    database = Path(database_path)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    requested = {
        *TABLE_EXPORTS,
        "decision_receipts",
        "rebalance_decisions",
        "account_snapshots",
        "benchmark_performance",
        "allocation_results",
        "market_bars",
        "reconciliation_discrepancies",
        "system_incidents",
        "order_intents",
        "order_events",
        "stream_events",
        "regime_states",
        "halt_events",
        "application_runs",
    }
    frames = _read_tables(database, requested)
    for table in tuple(frames):
        frames[table] = _safe_columns(_expand_payload(frames[table]))
    frames["daily_performance"] = _latest_series_sessions(frames["daily_performance"])
    frames["benchmark_performance"] = _latest_series_sessions(frames["benchmark_performance"])
    if normalized_feed == "SIP":
        runs = frames["application_runs"]
        latest_run_id = None if runs.empty or "run_id" not in runs else runs.iloc[-1]["run_id"]
        events = frames["stream_events"]
        entitlement_confirmed = False
        if (
            latest_run_id is not None
            and not events.empty
            and {"run_id", "event_type", "feed"}.issubset(events)
        ):
            entitlement_confirmed = bool(
                (
                    events["run_id"].astype(str).eq(str(latest_run_id))
                    & events["event_type"].astype(str).eq("feed_entitlement_confirmed")
                    & events["feed"].astype(str).str.upper().eq("SIP")
                ).any()
            )
        if not entitlement_confirmed:
            raise ValueError(
                "SIP feed entitlement is unconfirmed for the latest application run; "
                "the report will not claim SIP or fall back to IEX"
            )
    artifacts: dict[str, Path] = {}

    for table, filename in TABLE_EXPORTS.items():
        path = output / filename
        _canonical_export(frames[table], table).to_csv(path, index=False)
        artifacts[filename] = path

    daily = frames["daily_performance"]
    accounts = frames["account_snapshots"]
    orders = frames["broker_orders"]
    fills = frames["fill_events"]
    actions = frames["risk_actions"]
    discrepancies = frames["reconciliation_discrepancies"]
    benchmark = frames["benchmark_performance"]
    decisions = frames["rebalance_decisions"]
    order_metrics = _order_execution_metrics(
        orders,
        frames["order_events"],
        fills,
        frames["order_intents"],
    )
    regime_metrics = _regime_time_metrics(frames["regime_states"])
    halt_events_frame = frames["halt_events"]
    if "action" in halt_events_frame:
        hard_stop_count = int(
            halt_events_frame["action"].astype(str).str.lower().eq("hard_stop").sum()
        )
    elif "latch_type" in halt_events_frame:
        hard_stop_count = int(
            halt_events_frame["latch_type"].astype(str).str.lower().eq("hard_stop").sum()
        )
    else:
        hard_stop_count = 0
    paper_turnover = None
    if "turnover" in daily:
        turnover_values = pd.to_numeric(daily["turnover"], errors="coerce").dropna()
        if not turnover_values.empty:
            paper_turnover = float(turnover_values.sum())
    chart_daily = daily.copy()
    if (
        not chart_daily.empty
        and not benchmark.empty
        and "session_date" in chart_daily
        and "session_date" in benchmark
    ):
        benchmark_column = _first_column(
            benchmark,
            ("cumulative_return", "benchmark_cumulative_return", "value"),
        )
        if benchmark_column is not None:
            aligned = (
                benchmark[["session_date", benchmark_column]]
                .drop_duplicates("session_date", keep="last")
                .rename(columns={benchmark_column: "benchmark_cumulative_return"})
            )
            chart_daily = chart_daily.merge(aligned, on="session_date", how="left")
    summary = pd.DataFrame(
        [
            {
                "disclaimer": PAPER_BANNER,
                "feed": feed_banner,
                "database_exists": database.is_file(),
                "paper_equity": _number(accounts, ("equity",)),
                "paper_cash": _number(accounts, ("cash",)),
                "forward_paper_return": _number(daily, ("cumulative_return",)),
                "current_drawdown": _number(daily, ("drawdown",)),
                "benchmark_return": _number(
                    benchmark,
                    ("cumulative_return", "benchmark_cumulative_return", "value"),
                ),
                "paper_account_turnover": paper_turnover,
                "paper_account_turnover_undefined_reason": (
                    None
                    if paper_turnover is not None
                    else "No forward paper turnover observation has been recorded."
                ),
                "decision_count": len(decisions),
                "risk_intervention_count": len(actions),
                "hard_stop_count": hard_stop_count,
                "data_outage_count": _outage_count(frames["stream_events"]),
                "reconciliation_discrepancy_count": len(discrepancies),
                **order_metrics,
                **regime_metrics,
            }
        ]
    )
    summary_path = output / "forward_paper_summary.csv"
    summary.to_csv(summary_path, index=False)
    artifacts[summary_path.name] = summary_path

    receipts = frames["decision_receipts"]
    if receipts.empty:
        receipts = frames["rebalance_decisions"]
    receipt_path = output / "forward_decision_receipts.jsonl"
    _write_receipts(
        receipt_path,
        receipts,
        paper_disclosure=PAPER_BANNER,
        feed_disclosure=feed_banner,
    )
    artifacts[receipt_path.name] = receipt_path

    plot_specs = {
        "forward_paper_equity.png": (
            daily,
            ("end_equity", "equity"),
            "Forward Paper Account Equity",
            "Simulated paper equity ($)",
        ),
        "forward_paper_drawdown.png": (
            daily,
            ("drawdown",),
            "Forward Paper Account Drawdown",
            "Drawdown",
        ),
        "forward_paper_vs_benchmark.png": (
            chart_daily,
            ("cumulative_return", "benchmark_cumulative_return"),
            "Forward Paper Performance vs Benchmark",
            "Cumulative return",
        ),
        "forward_exposure.png": (
            daily,
            ("gross_exposure", "cash_allocation"),
            "Forward Paper Exposure and Cash",
            "Portfolio fraction",
        ),
        "forward_strategy_allocations.png": (
            frames["allocation_results"],
            ("momentum_weight", "mean_reversion_weight", "strategic_cash_weight"),
            "Forward Paper Strategy Allocations",
            "Allocation",
        ),
    }
    for filename, (frame, columns, title, ylabel) in plot_specs.items():
        path = output / filename
        _save_placeholder_or_series(
            path,
            frame,
            columns,
            title=title,
            ylabel=ylabel,
            feed_banner=feed_banner,
        )
        artifacts[filename] = path
    intervention_path = output / "forward_risk_interventions.png"
    _save_intervention_plot(intervention_path, actions, feed_banner)
    artifacts[intervention_path.name] = intervention_path

    receipt_lines: list[str] = ["## Immutable decision receipts", ""]
    if receipts.empty:
        receipt_lines.extend(["No scheduled decision receipt has been recorded.", ""])
    else:
        for index, row in enumerate(receipts.to_dict(orient="records"), start=1):
            safe_row = {str(key): _json_value(value) for key, value in row.items()}
            decision_id = safe_row.get("decision_id", f"receipt-{index}")
            status = safe_row.get("status", "recorded")
            receipt_lines.extend(
                [
                    f"### {decision_id}",
                    "",
                    f"- Status: {status}",
                    f"- Created: {safe_row.get('created_at', 'Not recorded')}",
                    f"- Skip reason: {safe_row.get('skip_reason', 'None') or 'None'}",
                    "",
                    "```json",
                    json.dumps(safe_row, sort_keys=True, indent=2, default=str),
                    "```",
                    "",
                ]
            )

    report_path = output / "forward_report.md"
    report_lines = [
        "# Forward Paper-Trading Report",
        "",
        f"> **{PAPER_BANNER}**",
        "",
        f"**{feed_banner}**",
        "",
        "## Current summary",
        "",
        f"- Simulated paper equity: {_format_metric(_number(accounts, ('equity',)), money=True)}",
        f"- Simulated paper cash: {_format_metric(_number(accounts, ('cash',)), money=True)}",
        f"- Forward paper return: {_format_metric(_number(daily, ('cumulative_return',)), percent=True)}",
        f"- Current drawdown: {_format_metric(_number(daily, ('drawdown',)), percent=True)}",
        f"- Benchmark return: {_format_metric(_number(benchmark, ('cumulative_return', 'benchmark_cumulative_return', 'value')), percent=True)}",
        f"- Paper-account turnover: {_format_metric(paper_turnover)}",
        f"- Decisions: {len(decisions)}",
        f"- Recorded paper orders: {order_metrics['paper_order_count']}",
        f"- Recorded paper fills: {order_metrics['paper_fill_count']}",
        f"- Fill rate: {_format_metric(order_metrics['paper_fill_rate'], percent=True)}",
        f"- Partial-fill rate: {_format_metric(order_metrics['paper_partial_fill_rate'], percent=True)}",
        f"- Rejection rate: {_format_metric(order_metrics['paper_rejection_rate'], percent=True)}",
        f"- Average adverse paper slippage: {_format_metric(order_metrics['average_paper_slippage_bps'])} bps",
        f"- Risk interventions: {len(actions)}",
        f"- Hard stops: {hard_stop_count}",
        f"- Data outages: {_outage_count(frames['stream_events'])}",
        f"- Reconciliation discrepancies: {len(discrepancies)}",
        "",
        "No observation is fabricated when the database is empty. External paper-account "
        "cash-flow discontinuities must be segmented and are not investment return.",
        "",
        *receipt_lines,
        "## Limitations",
        "",
        "Paper capital and fills are simulated. Paper trading differs from live trading; "
        "market impact and several execution effects are absent. IEX is not the full "
        "consolidated US market. Historical and forward paper results are separate, are "
        "not a guarantee, and are not investment advice.",
        "",
    ]
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    artifacts[report_path.name] = report_path
    return artifacts
