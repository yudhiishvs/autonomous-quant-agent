"""Read-only Streamlit dashboard for Adaptive Portfolio Agent operations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "observer.yaml"
PAPER_BANNER = "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
IEX_BANNER = "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET"
SIP_BANNER = "REAL-TIME SIP FEED"

# The dashboard has no write path. Keeping the table names in a fixed allowlist also
# prevents a user-controlled identifier from reaching a SQL statement.
READABLE_TABLES = frozenset(
    {
        "application_runs",
        "market_bars",
        "stream_events",
        "account_snapshots",
        "position_snapshots",
        "strategy_signals",
        "regime_states",
        "allocation_results",
        "risk_decisions",
        "risk_actions",
        "rebalance_decisions",
        "decision_receipts",
        "order_intents",
        "broker_orders",
        "order_events",
        "fill_events",
        "reconciliation_runs",
        "reconciliation_discrepancies",
        "halt_events",
        "system_incidents",
        "heartbeats",
        "daily_performance",
        "benchmark_performance",
        "generated_reports",
    }
)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load display-only settings without constructing network clients."""

    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(raw) if isinstance(raw, dict) else {}


def _resolve_path(raw: str | None, default: str) -> Path:
    value = Path(raw or default)
    return value if value.is_absolute() else PROJECT_ROOT / value


def _database_path(config: dict[str, Any]) -> Path:
    project = config.get("project", {})
    raw = project.get("database_path") if isinstance(project, dict) else None
    return _resolve_path(raw, "runtime/adaptive_portfolio_agent.db")


def _output_path(config: dict[str, Any]) -> Path:
    project = config.get("project", {})
    raw = project.get("output_directory") if isinstance(project, dict) else None
    return _resolve_path(raw, "outputs/primary_forward_paper")


def _feed(config: dict[str, Any]) -> str:
    market_data = config.get("market_data", {})
    if isinstance(market_data, dict):
        return str(market_data.get("feed", "IEX")).upper()
    return "IEX"


@st.cache_data(ttl=5, show_spinner=False)
def _read_table(database: str, table: str, limit: int = 500) -> pd.DataFrame:
    """Read one allowlisted table through SQLite's read-only URI mode."""

    if table not in READABLE_TABLES:
        raise ValueError(f"Table is not dashboard-readable: {table}")
    database_path = Path(database)
    if not database_path.is_file():
        return pd.DataFrame()
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if table not in names:
                return pd.DataFrame()
            return pd.read_sql_query(
                f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT ?',
                connection,
                params=(int(limit),),
            )
    except sqlite3.Error:
        return pd.DataFrame()


@st.cache_data(ttl=5, show_spinner=False)
def _read_unresolved_incidents(database: str, limit: int = 500) -> pd.DataFrame:
    """Filter durable incidents in SQL so an old unresolved row cannot be hidden."""

    database_path = Path(database)
    if not database_path.is_file():
        return pd.DataFrame()
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'system_incidents'"
            ).fetchone()
            if exists is None:
                return pd.DataFrame()
            return pd.read_sql_query(
                "SELECT * FROM system_incidents WHERE resolved_at IS NULL "
                "ORDER BY rowid DESC LIMIT ?",
                connection,
                params=(int(limit),),
            )
    except sqlite3.Error:
        return pd.DataFrame()


@st.cache_data(ttl=5, show_spinner=False)
def _read_current_run_entitlement(database: str) -> pd.DataFrame:
    """Read current-run entitlement evidence without truncating newer stream events."""

    database_path = Path(database)
    if not database_path.is_file():
        return pd.DataFrame()
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not {"application_runs", "stream_events"}.issubset(names):
                return pd.DataFrame()
            return pd.read_sql_query(
                "SELECT events.* FROM stream_events AS events "
                "WHERE events.run_id = ("
                "SELECT run_id FROM application_runs ORDER BY rowid DESC LIMIT 1"
                ") AND events.event_type = 'feed_entitlement_confirmed' "
                "ORDER BY events.rowid DESC",
                connection,
            )
    except sqlite3.Error:
        return pd.DataFrame()


def _latest(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    return {str(key): value for key, value in frame.iloc[0].to_dict().items()}


def _first(record: dict[str, Any], *names: str, default: Any = "Not recorded") -> Any:
    for name in names:
        value = record.get(name)
        if value is not None and not (isinstance(value, float) and pd.isna(value)):
            return value
    return default


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "Not recorded"


def _percent(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "Not recorded"


def _float_value(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


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
    """Expose scalar JSON payload fields while retaining normalized audit columns."""

    if frame.empty or "payload" not in frame:
        return frame
    payloads = [_json_object(value) for value in frame["payload"]]
    expanded = pd.json_normalize(payloads)
    expanded.index = frame.index
    base = frame.drop(columns=["payload"])
    for column in expanded:
        if column not in base:
            base[column] = expanded[column]
    return base


def _latest_series_sessions(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep one latest dashboard projection per logical forward series/session."""

    if frame.empty or "session_date" not in frame:
        return frame
    keys = ["session_date"]
    if "series_id" in frame:
        keys.insert(0, "series_id")
    # Dashboard reads are newest-first, so keep the first projection.
    return frame.drop_duplicates(keys, keep="first").reset_index(drop=True)


def _forward_benchmark_comparison(forward: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """Align forward-paper and SPY cumulative returns on completed sessions."""

    if forward.empty or benchmark.empty or "session_date" not in forward:
        return pd.DataFrame()
    benchmark_column = next(
        (
            name
            for name in ("benchmark_cumulative_return", "cumulative_return", "value")
            if name in benchmark
        ),
        None,
    )
    if (
        "cumulative_return" not in forward
        or "session_date" not in benchmark
        or benchmark_column is None
    ):
        return pd.DataFrame()
    paper = forward[["session_date", "cumulative_return"]].rename(
        columns={"cumulative_return": "Forward paper"}
    )
    spy = benchmark[["session_date", benchmark_column]].rename(columns={benchmark_column: "SPY"})
    aligned = paper.merge(spy, on="session_date", how="inner")
    aligned["session_date"] = pd.to_datetime(aligned["session_date"], errors="coerce")
    for column in ("Forward paper", "SPY"):
        aligned[column] = pd.to_numeric(aligned[column], errors="coerce")
    return aligned.dropna(subset=["session_date"]).sort_values("session_date")


def _frame(database: Path, table: str, limit: int = 500) -> pd.DataFrame:
    return _read_table(str(database), table, limit)


def _active_halts(events: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Derive current latch state from the append-only halt event stream."""

    active: dict[str, dict[str, Any]] = {}
    if events.empty or "latch_type" not in events or "action" not in events:
        return active
    ordered = events.iloc[::-1]
    if "created_at" in ordered:
        ordered = ordered.sort_values("created_at", kind="stable")
    for record in ordered.to_dict(orient="records"):
        latch_type = str(record.get("latch_type") or "unknown")
        action = str(record.get("action") or "").lower()
        if action in {"halt", "hard_stop", "daily_loss"}:
            active[latch_type] = {str(key): value for key, value in record.items()}
        elif action in {"resume", "expired"}:
            active.pop(latch_type, None)
    return active


def _sip_entitlement_confirmed(database: Path) -> bool:
    """Require a SIP entitlement confirmation tied to the latest application run."""

    confirmed = _read_current_run_entitlement(str(database))
    for payload in confirmed.get("payload", pd.Series(dtype=object)):
        if str(_json_object(payload).get("feed", "")).upper() == "SIP":
            return True
    return False


def _show_health(database: Path, feed: str, config: dict[str, Any]) -> None:
    st.header("System health")
    run = _latest(_frame(database, "application_runs", 1))
    heartbeat = _latest(_frame(database, "heartbeats", 1))
    components = _json_object(heartbeat.get("components"))
    stream = _latest(_frame(database, "stream_events", 1))
    reconciliation = _latest(_frame(database, "reconciliation_runs", 1))
    halt_events = _frame(database, "halt_events", 500)
    active_halts = _active_halts(halt_events)
    bar = _latest(_frame(database, "market_bars", 1))
    heartbeat_raw = _first(heartbeat, "timestamp", "created_at", "recorded_at")
    heartbeat_at = pd.to_datetime(heartbeat_raw, utc=True, errors="coerce")
    schedule = config.get("schedule", {})
    configured_interval = (
        schedule.get("heartbeat_interval_seconds", 30) if isinstance(schedule, dict) else 30
    )
    try:
        maximum_age_seconds = max(90, int(configured_interval) * 3)
    except (TypeError, ValueError):
        maximum_age_seconds = 90
    heartbeat_age_seconds = (
        None
        if pd.isna(heartbeat_at)
        else max(0.0, (pd.Timestamp.now(tz="UTC") - heartbeat_at).total_seconds())
    )
    heartbeat_current = bool(_first(heartbeat, "healthy", default=False)) and (
        heartbeat_age_seconds is not None and heartbeat_age_seconds <= maximum_age_seconds
    )

    columns = st.columns(5)
    columns[0].metric("Mode", str(_first(run, "mode", default="Not running")))
    columns[1].metric(
        "Running",
        "Healthy" if heartbeat_current else "Not running / unhealthy",
    )
    columns[2].metric("Feed", feed)
    columns[3].metric(
        "Halt status",
        ", ".join(sorted(active_halts)) if active_halts else "Clear",
    )
    columns[4].metric(
        "Stream", str(_first(stream, "status", "event_type", default="Not connected"))
    )
    details = pd.DataFrame(
        {
            "State": [
                "Last heartbeat",
                "Last market event",
                "Last reconciliation",
                "Market state",
                "Next market open",
                "Next market close",
                "Market-data stream",
                "Paper trade-update stream",
                "Data freshness",
                "Hard-stop state",
            ],
            "Value": [
                (
                    f"{heartbeat_raw} ({heartbeat_age_seconds:.1f}s ago)"
                    if heartbeat_age_seconds is not None
                    else heartbeat_raw
                ),
                _first(bar, "received_at", "start_timestamp", "timestamp"),
                _first(reconciliation, "completed_at", "started_at", "created_at"),
                _first(components, "market_open", default="Not recorded"),
                _first(components, "next_open", default="Not recorded"),
                _first(components, "next_close", default="Not recorded"),
                _first(components, "stream_connected", default="Not recorded"),
                _first(
                    components,
                    "trade_updates_status",
                    "trade_updates_healthy",
                    default="Not recorded",
                ),
                _first(components, "fresh", "data_fresh", default="Not recorded"),
                "Latched — manual review required"
                if "hard_stop" in active_halts
                else "Not latched",
            ],
        }
    )
    st.dataframe(details, hide_index=True, use_container_width=True)
    incidents = _frame(database, "system_incidents", 50)
    unresolved = _read_unresolved_incidents(str(database), 500)
    st.subheader("Current incidents")
    if unresolved.empty:
        st.info("No unresolved incident is recorded.")
    else:
        st.dataframe(unresolved, hide_index=True, use_container_width=True)
    if not incidents.empty:
        with st.expander("Recent incident history"):
            st.dataframe(incidents, hide_index=True, use_container_width=True)


def _show_account(database: Path) -> None:
    st.header("Simulated paper account")
    account = _latest(_frame(database, "account_snapshots", 1))
    performance = _latest(_expand_payload(_frame(database, "daily_performance", 1)))
    columns = st.columns(4)
    columns[0].metric("Simulated equity", _money(_first(account, "equity", default=None)))
    columns[1].metric("Simulated cash", _money(_first(account, "cash", default=None)))
    columns[2].metric(
        "Buying power (display only)", _money(_first(account, "buying_power", default=None))
    )
    columns[3].metric(
        "Today's paper P&L", _money(_first(performance, "daily_pnl", "pnl", default=None))
    )
    columns = st.columns(4)
    columns[0].metric(
        "Forward paper return", _percent(_first(performance, "cumulative_return", default=None))
    )
    columns[1].metric("Drawdown", _percent(_first(performance, "drawdown", default=None)))
    columns[2].metric(
        "Gross exposure", _percent(_first(performance, "gross_exposure", default=None))
    )
    columns[3].metric(
        "Cash allocation", _percent(_first(performance, "cash_allocation", default=None))
    )

    st.subheader("Current positions")
    positions = _frame(database, "position_snapshots", 100)
    if positions.empty:
        st.info("No simulated paper positions have been recorded.")
    else:
        positions = positions.copy()
        snapshot_id = _first(account, "snapshot_id", default=None)
        if snapshot_id is not None and "account_snapshot_id" in positions:
            positions = positions[
                positions["account_snapshot_id"].astype(str).eq(str(snapshot_id))
            ].copy()
        try:
            equity = float(_first(account, "equity", default=0.0))
        except (TypeError, ValueError):
            equity = 0.0
        if equity > 0 and "market_value" in positions:
            positions["weight"] = pd.to_numeric(positions["market_value"], errors="coerce") / equity
        bars = _frame(database, "market_bars", 5_000)
        if not bars.empty and {"symbol", "start_at"}.issubset(bars):
            latest_bars = (
                bars.drop_duplicates("symbol", keep="first")[["symbol", "start_at"]]
                .rename(columns={"start_at": "last_price_timestamp"})
                .copy()
            )
            positions = positions.merge(latest_bars, on="symbol", how="left")
        st.dataframe(positions, hide_index=True, use_container_width=True)


def _show_strategy(database: Path) -> None:
    st.header("Current strategy state")
    regime = _frame(database, "regime_states", 25)
    signals = _frame(database, "strategy_signals", 50)
    allocations = _frame(database, "allocation_results", 25)
    targets = _frame(database, "rebalance_decisions", 25)
    tabs = st.tabs(["Regime", "Signals", "Strategy allocation", "Final targets"])
    for tab, frame in zip(tabs, (regime, signals, allocations, targets), strict=True):
        with tab:
            if frame.empty:
                st.info("No state has been recorded yet.")
            else:
                st.dataframe(frame, hide_index=True, use_container_width=True)


def _show_orders(database: Path) -> None:
    st.header("Simulated paper orders and fills")
    orders = _frame(database, "broker_orders", 100)
    fills = _frame(database, "fill_events", 100)
    order_events = _frame(database, "order_events", 100)
    tabs = st.tabs(["Orders", "Fills", "Append-only order events"])
    for tab, frame in zip(tabs, (orders, fills, order_events), strict=True):
        with tab:
            if frame.empty:
                st.info("No records yet.")
            else:
                st.dataframe(frame, hide_index=True, use_container_width=True)


def _show_risk(database: Path, config: dict[str, Any]) -> None:
    st.header("Independent risk controls")
    risk_config = config.get("risk", {})
    execution_config = config.get("execution", {})
    if isinstance(risk_config, dict) and isinstance(execution_config, dict):
        limits = {**risk_config, **{f"execution.{k}": v for k, v in execution_config.items()}}
        safe_limits = {
            key: value
            for key, value in limits.items()
            if "key" not in key.lower() and "secret" not in key.lower()
        }
        st.json(safe_limits)
    decisions = _expand_payload(_frame(database, "risk_decisions", 50))
    actions = _frame(database, "risk_actions", 100)
    discrepancies = _frame(database, "reconciliation_discrepancies", 100)

    heartbeat = _latest(_frame(database, "heartbeats", 1))
    components = _json_object(heartbeat.get("components"))
    performance = _latest(_expand_payload(_frame(database, "daily_performance", 1)))
    reconciliation = _latest(_frame(database, "reconciliation_runs", 1))
    active_halts = _active_halts(_frame(database, "halt_events", 500))
    latest_risk = _latest(decisions)
    account = _latest(_frame(database, "account_snapshots", 1))
    positions = _frame(database, "position_snapshots", 500)
    account_snapshot_id = account.get("snapshot_id")
    if account_snapshot_id is not None and "account_snapshot_id" in positions:
        positions = positions[
            positions["account_snapshot_id"].astype(str).eq(str(account_snapshot_id))
        ]
    equity = _float_value(account.get("equity"))
    market_values = (
        pd.to_numeric(positions["market_value"], errors="coerce").abs()
        if "market_value" in positions
        else pd.Series(dtype=float)
    )
    largest_weight = (
        None
        if equity is None or equity <= 0 or market_values.dropna().empty
        else float(market_values.max() / equity)
    )
    gross = _float_value(_first(performance, "gross_exposure", default=None))
    cash = _float_value(_first(performance, "cash_allocation", default=None))
    drawdown = _float_value(
        _first(
            components,
            "current_drawdown",
            default=_first(performance, "drawdown", default=None),
        )
    )
    daily_loss = _float_value(_first(components, "current_daily_loss", default=None))
    turnover = _float_value(
        _first(
            latest_risk,
            "risk_decision.final_turnover",
            "final_turnover",
            default=None,
        )
    )
    max_position = _float_value(
        risk_config.get("max_position_weight") if isinstance(risk_config, dict) else None
    )
    max_gross = _float_value(
        risk_config.get("max_gross_exposure") if isinstance(risk_config, dict) else None
    )
    cash_buffer = _float_value(
        risk_config.get("required_cash_buffer") if isinstance(risk_config, dict) else None
    )
    soft_limit = _float_value(
        risk_config.get("drawdown_soft_limit") if isinstance(risk_config, dict) else None
    )
    hard_limit = _float_value(
        risk_config.get("drawdown_hard_limit") if isinstance(risk_config, dict) else None
    )
    daily_limit = _float_value(
        risk_config.get("daily_loss_limit") if isinstance(risk_config, dict) else None
    )
    turnover_limit = _float_value(
        risk_config.get("max_turnover_per_rebalance") if isinstance(risk_config, dict) else None
    )

    def utilization(value: float | None, limit: float | None) -> str:
        if value is None or limit is None or limit <= 0:
            return "Not recorded"
        return _percent(value / limit)

    fresh = components.get("fresh")
    blocking = reconciliation.get("blocking")
    fresh_state = None if fresh is None else bool(fresh)
    blocking_state = None if blocking is None else bool(blocking)
    status_rows = pd.DataFrame(
        [
            {
                "Control": "Maximum position",
                "Current": _percent(largest_weight),
                "Limit": _percent(max_position),
                "Utilization": utilization(largest_weight, max_position),
                "State": "Within limit"
                if largest_weight is not None
                and max_position is not None
                and largest_weight <= max_position
                else "Not recorded / review",
            },
            {
                "Control": "Gross exposure",
                "Current": _percent(gross),
                "Limit": _percent(max_gross),
                "Utilization": utilization(gross, max_gross),
                "State": "Within limit"
                if gross is not None and max_gross is not None and gross <= max_gross
                else "Not recorded / review",
            },
            {
                "Control": "Required cash buffer",
                "Current": _percent(cash),
                "Limit": f">= {_percent(cash_buffer)}",
                "Utilization": "Not applicable",
                "State": "Satisfied"
                if cash is not None and cash_buffer is not None and cash >= cash_buffer
                else "Not recorded / blocked",
            },
            {
                "Control": "Turnover",
                "Current": _percent(turnover),
                "Limit": _percent(turnover_limit),
                "Utilization": utilization(turnover, turnover_limit),
                "State": "Latest decision",
            },
            {
                "Control": "Soft drawdown",
                "Current": _percent(drawdown),
                "Limit": _percent(soft_limit),
                "Utilization": utilization(drawdown, soft_limit),
                "State": "Active"
                if drawdown is not None and soft_limit is not None and drawdown >= soft_limit
                else "Clear / not recorded",
            },
            {
                "Control": "Hard drawdown",
                "Current": _percent(drawdown),
                "Limit": _percent(hard_limit),
                "Utilization": utilization(drawdown, hard_limit),
                "State": "Latched" if "hard_stop" in active_halts else "Clear",
            },
            {
                "Control": "Daily loss",
                "Current": _percent(daily_loss),
                "Limit": _percent(daily_limit),
                "Utilization": utilization(daily_loss, daily_limit),
                "State": "Latched" if "daily_loss" in active_halts else "Clear / not recorded",
            },
            {
                "Control": "Market-data freshness",
                "Current": str(fresh) if fresh is not None else "Not recorded",
                "Limit": "Fresh and healthy",
                "Utilization": "Not applicable",
                "State": "Clear" if fresh_state is True else "Blocks submission",
            },
            {
                "Control": "Reconciliation",
                "Current": str(blocking) if blocking is not None else "Not recorded",
                "Limit": "Not blocking",
                "Utilization": "Not applicable",
                "State": "Blocks submission" if blocking_state is not False else "Clear",
            },
        ]
    )
    st.subheader("Current utilization and blocking state")
    st.dataframe(status_rows, hide_index=True, use_container_width=True)
    tabs = st.tabs(["Decisions", "Interventions", "Reconciliation blocks"])
    for tab, frame in zip(tabs, (decisions, actions, discrepancies), strict=True):
        with tab:
            if frame.empty:
                st.info("No records yet.")
            else:
                st.dataframe(frame, hide_index=True, use_container_width=True)


def _show_performance(database: Path, output: Path) -> None:
    st.header("Performance")
    (
        historical_tab,
        forward_tab,
        benchmark_tab,
        drawdown_tab,
        exposure_tab,
        turnover_tab,
        regime_risk_tab,
    ) = st.tabs(
        [
            "Historical backtest",
            "Forward paper performance",
            "SPY comparison",
            "Drawdown and daily P&L",
            "Exposure and cash",
            "Turnover",
            "Regimes and risk",
        ]
    )
    with historical_tab:
        candidates = (
            PROJECT_ROOT / "outputs" / "historical_backtest" / "metrics_full_period.csv",
            output / "metrics_full_period.csv",
        )
        metrics_path = next((path for path in candidates if path.is_file()), candidates[0])
        if metrics_path.is_file():
            st.dataframe(pd.read_csv(metrics_path), hide_index=True, use_container_width=True)
        else:
            st.info("Run the historical backtest to generate separate historical results.")
    forward = _latest_series_sessions(_expand_payload(_frame(database, "daily_performance", 5_000)))
    with forward_tab:
        if forward.empty:
            st.info("No forward paper-performance observations have been recorded.")
        else:
            date_column = next(
                (name for name in ("session_date", "date", "timestamp") if name in forward), None
            )
            equity_column = next(
                (name for name in ("end_equity", "equity") if name in forward), None
            )
            if date_column and equity_column:
                chart = forward[[date_column, equity_column]].copy()
                chart[date_column] = pd.to_datetime(chart[date_column], errors="coerce")
                st.line_chart(chart.dropna().set_index(date_column))
            st.dataframe(forward, hide_index=True, use_container_width=True)
    benchmark = _latest_series_sessions(
        _expand_payload(_frame(database, "benchmark_performance", 5_000))
    )
    with benchmark_tab:
        comparison = _forward_benchmark_comparison(forward, benchmark)
        if forward.empty or benchmark.empty:
            st.info("No aligned SPY benchmark observations have been recorded.")
        elif comparison.empty:
            st.info("Forward-paper and SPY observations do not yet share a completed session.")
        else:
            st.line_chart(comparison.set_index("session_date"))
            st.dataframe(comparison, hide_index=True, use_container_width=True)
            with st.expander("Stored SPY benchmark details"):
                st.dataframe(benchmark, hide_index=True, use_container_width=True)
    with drawdown_tab:
        columns = [name for name in ("drawdown", "daily_pnl") if name in forward]
        if forward.empty or not columns or "session_date" not in forward:
            st.info("No aligned forward paper drawdown or daily P&L is available.")
        else:
            chart = forward[["session_date", *columns]].copy()
            chart["session_date"] = pd.to_datetime(chart["session_date"], errors="coerce")
            st.line_chart(chart.dropna(subset=["session_date"]).set_index("session_date"))
    with exposure_tab:
        columns = [name for name in ("gross_exposure", "cash_allocation") if name in forward]
        if forward.empty or not columns or "session_date" not in forward:
            st.info("No forward paper exposure observations are available.")
        else:
            chart = forward[["session_date", *columns]].copy()
            chart["session_date"] = pd.to_datetime(chart["session_date"], errors="coerce")
            st.line_chart(chart.dropna(subset=["session_date"]).set_index("session_date"))
    with turnover_tab:
        if forward.empty or "turnover" not in forward or "session_date" not in forward:
            st.info("No forward paper turnover observations are available.")
        else:
            chart = forward[["session_date", "turnover"]].copy()
            chart["session_date"] = pd.to_datetime(chart["session_date"], errors="coerce")
            st.bar_chart(chart.dropna(subset=["session_date"]).set_index("session_date"))
    with regime_risk_tab:
        regimes = _frame(database, "regime_states", 500)
        actions = _frame(database, "risk_actions", 500)
        if regimes.empty and actions.empty:
            st.info("No forward paper regime or risk-intervention records are available.")
        if not regimes.empty:
            st.caption("Regime history")
            st.dataframe(regimes, hide_index=True, use_container_width=True)
        if not actions.empty:
            st.caption("Risk interventions")
            st.dataframe(actions, hide_index=True, use_container_width=True)


def _show_receipts(database: Path) -> None:
    st.header("Immutable decision receipts")
    receipts = _frame(database, "decision_receipts", 500)
    if receipts.empty:
        receipts = _frame(database, "rebalance_decisions", 500)
    if receipts.empty:
        st.info("No scheduled decision receipts have been recorded.")
        return
    st.dataframe(receipts, hide_index=True, use_container_width=True)
    json_columns = [name for name in receipts if "json" in name or "payload" in name]
    if json_columns:
        selected = st.selectbox("Inspect receipt payload", list(receipts.index))
        raw = receipts.loc[selected, json_columns[0]]
        try:
            st.json(json.loads(str(raw)))
        except (json.JSONDecodeError, TypeError):
            st.code(str(raw), language="json")


def main() -> None:
    """Render the dashboard without any brokerage mutation capability."""

    st.set_page_config(page_title="Adaptive Portfolio Agent", layout="wide")
    config_path = DEFAULT_CONFIG
    config = _load_yaml(config_path)
    database = _database_path(config)
    output = _output_path(config)
    feed = _feed(config)

    st.error(PAPER_BANNER)
    if feed == "IEX":
        st.warning(IEX_BANNER)
    elif feed == "SIP":
        if not _sip_entitlement_confirmed(database):
            st.error(
                "SIP FEED ENTITLEMENT UNCONFIRMED — no successful entitlement event is "
                "tied to the latest application run. Run the read-only doctor check and "
                "establish the configured SIP stream; the dashboard will not silently "
                "fall back to IEX."
            )
            st.stop()
        st.success(SIP_BANNER)
    else:
        st.error(f"Unsupported or unconfirmed market-data feed: {feed}")
        st.stop()

    st.title("Adaptive Portfolio Agent")
    st.caption(
        "Live market information, simulated paper-account capital, and simulated execution. "
        "This dashboard is read-only."
    )
    st.sidebar.caption(f"Configuration: {config_path}")
    st.sidebar.caption(f"Database (read-only): {database}")
    if not database.is_file():
        st.info(
            "The forward paper database does not exist yet. Start observer mode to create "
            "operational records; the dashboard will never create or modify it."
        )

    _show_health(database, feed, config)
    _show_account(database)
    _show_strategy(database)
    _show_orders(database)
    _show_risk(database, config)
    _show_performance(database, output)
    _show_receipts(database)

    st.header("Limitations")
    st.markdown(
        """
- Paper capital and paper fills are simulated; paper trading differs from live trading.
- Market impact and several real execution effects are not represented.
- Free IEX data is not the full consolidated US market.
- Forward paper-trading performance is not live-money performance.
- Historical performance is not a guarantee of future results.
- This project is educational research, not investment advice.
"""
    )
    st.error(PAPER_BANNER)


if __name__ == "__main__":
    main()
