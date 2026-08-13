"""Read-only dashboard and forward-report output checks."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import app as dashboard
from adaptive_trader.forward_reporting import generate_forward_outputs


def test_dashboard_contains_no_order_mutation_path(project_root: Path) -> None:
    source = (project_root / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not any(
        name.startswith(
            (
                "alpaca",
                "adaptive_trader.broker",
                "adaptive_trader.execution",
                "adaptive_trader.live",
            )
        )
        for name in imported
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {
            "submit_order",
            "cancel_order",
            "cancel_orders",
            "cancel_all_orders",
            "replace_order",
            "close_position",
            "close_all_positions",
            "liquidate",
        }
    )
    assert "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS" in source


def test_forward_report_generation_is_read_only_and_complete(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE account_snapshots "
            "(timestamp TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO account_snapshots VALUES ('2026-01-02T21:00:00+00:00', 100500, 25000)"
        )
        connection.execute(
            "CREATE TABLE daily_performance "
            "(session_date TEXT NOT NULL, end_equity REAL, cumulative_return REAL, "
            "drawdown REAL, gross_exposure REAL, cash_allocation REAL)"
        )
        connection.execute(
            "INSERT INTO daily_performance VALUES ('2026-01-02', 100500, .005, -.001, .75, .25)"
        )
        for table in (
            "position_snapshots",
            "broker_orders",
            "fill_events",
            "risk_actions",
            "decision_receipts",
            "rebalance_decisions",
            "benchmark_performance",
            "allocation_results",
            "reconciliation_discrepancies",
            "system_incidents",
        ):
            connection.execute(f'CREATE TABLE "{table}" (created_at TEXT)')
        connection.execute("INSERT INTO decision_receipts VALUES ('2026-01-02T15:05:00+00:00')")

    before = database.read_bytes()
    output = tmp_path / "reports"
    artifacts = generate_forward_outputs(database, output, feed="IEX")
    assert database.read_bytes() == before
    required = {
        "forward_paper_summary.csv",
        "forward_daily_performance.csv",
        "forward_positions.csv",
        "forward_orders.csv",
        "forward_fills.csv",
        "forward_risk_actions.csv",
        "forward_decision_receipts.jsonl",
        "forward_report.md",
        "forward_paper_equity.png",
        "forward_paper_drawdown.png",
        "forward_paper_vs_benchmark.png",
        "forward_exposure.png",
        "forward_strategy_allocations.png",
        "forward_risk_interventions.png",
    }
    assert required.issubset(artifacts)
    assert all(artifacts[name].is_file() for name in required)
    report = artifacts["forward_report.md"].read_text(encoding="utf-8")
    assert "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS" in report
    assert "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET" in report
    receipt = ast.literal_eval(
        artifacts["forward_decision_receipts.jsonl"].read_text(encoding="utf-8").strip()
    )
    assert receipt["paper_disclosure"] == ("PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS")
    assert receipt["feed_disclosure"] == (
        "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET"
    )
    summary = pd.read_csv(artifacts["forward_paper_summary.csv"])
    assert summary.loc[0, "paper_order_count"] == 0


def test_dashboard_derives_active_latches_from_append_only_events() -> None:
    events = pd.DataFrame(
        [
            {"created_at": "2026-01-02T15:00:00+00:00", "action": "halt", "latch_type": "operator"},
            {
                "created_at": "2026-01-02T15:01:00+00:00",
                "action": "resume",
                "latch_type": "operator",
            },
            {
                "created_at": "2026-01-02T15:02:00+00:00",
                "action": "hard_stop",
                "latch_type": "hard_stop",
            },
        ]
    )

    active = dashboard._active_halts(events.iloc[::-1])

    assert set(active) == {"hard_stop"}


def test_dashboard_deduplicates_restart_projections_by_series_and_session() -> None:
    frame = pd.DataFrame(
        [
            {
                "run_id": "new-run",
                "series_id": "paper-series",
                "session_date": "2026-01-02",
                "end_equity": 100250,
            },
            {
                "run_id": "old-run",
                "series_id": "paper-series",
                "session_date": "2026-01-02",
                "end_equity": 100100,
            },
        ]
    )

    result = dashboard._latest_series_sessions(frame)

    assert len(result) == 1
    assert result.loc[0, "end_equity"] == 100250


def test_dashboard_aligns_forward_paper_and_spy_returns() -> None:
    forward = pd.DataFrame(
        [
            {"session_date": "2026-01-02", "cumulative_return": 0.01},
            {"session_date": "2026-01-05", "cumulative_return": 0.02},
        ]
    )
    benchmark = pd.DataFrame(
        [
            {"session_date": "2026-01-02", "benchmark_cumulative_return": 0.005},
            {"session_date": "2026-01-06", "benchmark_cumulative_return": 0.007},
        ]
    )

    comparison = dashboard._forward_benchmark_comparison(forward, benchmark)

    assert list(comparison.columns) == ["session_date", "Forward paper", "SPY"]
    assert len(comparison) == 1
    assert comparison.iloc[0]["Forward paper"] == pytest.approx(0.01)
    assert comparison.iloc[0]["SPY"] == pytest.approx(0.005)


def test_dashboard_current_incidents_are_filtered_before_limit(tmp_path: Path) -> None:
    database = tmp_path / "incidents.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE system_incidents (incident_id TEXT, created_at TEXT, resolved_at TEXT)"
        )
        connection.execute(
            "INSERT INTO system_incidents VALUES "
            "('unresolved-old', '2026-01-01T00:00:00+00:00', NULL)"
        )
        connection.executemany(
            "INSERT INTO system_incidents VALUES (?, ?, ?)",
            [
                (
                    f"resolved-{index}",
                    f"2026-01-02T00:{index:02d}:00+00:00",
                    f"2026-01-02T01:{index:02d}:00+00:00",
                )
                for index in range(60)
            ],
        )
    dashboard._read_unresolved_incidents.clear()

    incidents = dashboard._read_unresolved_incidents(str(database), 50)

    assert incidents["incident_id"].tolist() == ["unresolved-old"]


def test_dashboard_requires_persisted_sip_evidence(tmp_path: Path) -> None:
    database = tmp_path / "sip.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE application_runs (run_id TEXT)")
        connection.execute("INSERT INTO application_runs VALUES ('current-run')")
        connection.execute(
            "CREATE TABLE stream_events (run_id TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO stream_events VALUES "
            "('old-run', 'feed_entitlement_confirmed', '{\"feed\":\"SIP\"}')"
        )
    dashboard._read_table.clear()
    dashboard._read_current_run_entitlement.clear()
    assert dashboard._sip_entitlement_confirmed(database) is False

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO stream_events VALUES "
            "('current-run', 'feed_entitlement_confirmed', '{\"feed\":\"SIP\"}')"
        )
    dashboard._read_table.clear()
    dashboard._read_current_run_entitlement.clear()
    assert dashboard._sip_entitlement_confirmed(database) is True


def test_dashboard_sip_entitlement_cannot_age_out_of_recent_event_window(
    tmp_path: Path,
) -> None:
    database = tmp_path / "long-running-sip.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE application_runs (run_id TEXT)")
        connection.execute("INSERT INTO application_runs VALUES ('current-run')")
        connection.execute(
            "CREATE TABLE stream_events (run_id TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO stream_events VALUES "
            "('current-run', 'feed_entitlement_confirmed', '{\"feed\":\"SIP\"}')"
        )
        connection.executemany(
            "INSERT INTO stream_events VALUES (?, ?, ?)",
            [("current-run", "open_order_monitor", '{"status":"healthy"}') for _ in range(600)],
        )
    dashboard._read_current_run_entitlement.clear()

    assert dashboard._sip_entitlement_confirmed(database) is True


def test_forward_report_refuses_unconfirmed_sip_feed(tmp_path: Path) -> None:
    database = tmp_path / "unconfirmed-sip.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE application_runs (run_id TEXT)")
        connection.executemany(
            "INSERT INTO application_runs VALUES (?)", [("old-run",), ("current-run",)]
        )
        connection.execute(
            "CREATE TABLE stream_events (run_id TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO stream_events VALUES "
            "('old-run', 'feed_entitlement_confirmed', '{\"feed\":\"SIP\"}')"
        )

    with pytest.raises(ValueError, match="SIP feed entitlement is unconfirmed"):
        generate_forward_outputs(database, tmp_path / "report", feed="SIP")


def test_forward_report_accepts_current_run_sip_entitlement_only(tmp_path: Path) -> None:
    database = tmp_path / "confirmed-sip.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE application_runs (run_id TEXT)")
        connection.execute("INSERT INTO application_runs VALUES ('current-run')")
        connection.execute(
            "CREATE TABLE stream_events (run_id TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO stream_events VALUES "
            "('current-run', 'feed_entitlement_confirmed', '{\"feed\":\"SIP\"}')"
        )

    artifacts = generate_forward_outputs(database, tmp_path / "sip-report", feed="SIP")

    report = artifacts["forward_report.md"].read_text(encoding="utf-8")
    assert "REAL-TIME SIP FEED" in report


def test_forward_report_unpacks_production_payload_schema(tmp_path: Path) -> None:
    database = tmp_path / "production-shape.db"
    payload = (
        '{"end_equity":100250,"cumulative_return":0.0025,"daily_return":0.0025,'
        '"drawdown":0.0,"gross_exposure":0.7,"cash_allocation":0.3,'
        '"daily_pnl":250,"continuity_status":"continuous"}'
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE daily_performance "
            "(performance_id TEXT, run_id TEXT, session_date TEXT, created_at TEXT, "
            "value REAL, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO daily_performance VALUES (?, ?, ?, ?, ?, ?)",
            ("p1", "r1", "2026-01-02", "2026-01-02T21:00:00+00:00", 0.0025, payload),
        )

    artifacts = generate_forward_outputs(database, tmp_path / "forward", feed="IEX")
    daily = pd.read_csv(artifacts["forward_daily_performance.csv"])

    assert daily.loc[0, "end_equity"] == 100250
    assert daily.loc[0, "cumulative_return"] == pytest.approx(0.0025)
    assert daily.loc[0, "continuity_status"] == "continuous"


def test_forward_summary_calculates_execution_outage_and_regime_metrics(tmp_path: Path) -> None:
    database = tmp_path / "paper-metrics.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE broker_orders (client_order_id TEXT, state TEXT)")
        connection.executemany(
            "INSERT INTO broker_orders VALUES (?, ?)",
            [("order-1", "filled"), ("order-2", "rejected")],
        )
        connection.execute(
            "CREATE TABLE order_events (client_order_id TEXT, to_state TEXT, event_type TEXT)"
        )
        connection.execute(
            "INSERT INTO order_events VALUES ('order-1', 'partially_filled', 'partial_fill')"
        )
        connection.execute(
            "CREATE TABLE order_intents (client_order_id TEXT, reference_price TEXT, side TEXT)"
        )
        connection.executemany(
            "INSERT INTO order_intents VALUES (?, ?, ?)",
            [("order-1", "100", "buy"), ("order-2", "50", "sell")],
        )
        connection.execute(
            "CREATE TABLE fill_events (client_order_id TEXT, price TEXT, quantity TEXT, side TEXT)"
        )
        connection.execute("INSERT INTO fill_events VALUES ('order-1', '101', '10', 'buy')")
        connection.execute("CREATE TABLE stream_events (event_type TEXT)")
        connection.executemany(
            "INSERT INTO stream_events VALUES (?)",
            [
                ("market_data_disconnected",),
                ("market_data_disconnected",),
                ("market_data_recovered",),
            ],
        )
        connection.execute("CREATE TABLE regime_states (payload TEXT)")
        connection.executemany(
            "INSERT INTO regime_states VALUES (?)",
            [
                ('{"regime":{"name":"bull_low_vol"}}',),
                ('{"regime":{"name":"bear_high_vol"}}',),
            ],
        )
        connection.execute("CREATE TABLE rebalance_decisions (decision_id TEXT)")
        connection.executemany("INSERT INTO rebalance_decisions VALUES (?)", [("d1",), ("d2",)])
        connection.execute("CREATE TABLE halt_events (action TEXT, latch_type TEXT)")
        connection.executemany(
            "INSERT INTO halt_events VALUES (?, ?)",
            [("hard_stop", "hard_stop"), ("resume", "hard_stop")],
        )

    artifacts = generate_forward_outputs(database, tmp_path / "metrics", feed="IEX")
    summary = pd.read_csv(artifacts["forward_paper_summary.csv"])

    assert summary.loc[0, "decision_count"] == 2
    assert summary.loc[0, "paper_order_count"] == 2
    assert summary.loc[0, "paper_fill_rate"] == pytest.approx(0.5)
    assert summary.loc[0, "paper_partial_fill_rate"] == pytest.approx(0.5)
    assert summary.loc[0, "paper_rejection_rate"] == pytest.approx(0.5)
    assert summary.loc[0, "average_paper_slippage_bps"] == pytest.approx(100.0)
    assert summary.loc[0, "data_outage_count"] == 1
    assert summary.loc[0, "hard_stop_count"] == 1
    assert summary.loc[0, "time_in_regime_bull_low_vol"] == pytest.approx(0.5)
    assert summary.loc[0, "time_in_regime_bear_high_vol"] == pytest.approx(0.5)


def test_forward_exports_deduplicate_restart_projection_by_series_and_session(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart-series.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE daily_performance "
            "(run_id TEXT, session_date TEXT, created_at TEXT, payload TEXT)"
        )
        connection.executemany(
            "INSERT INTO daily_performance VALUES (?, ?, ?, ?)",
            [
                (
                    "run-1",
                    "2026-01-02",
                    "2026-01-02T20:00:00+00:00",
                    '{"series_id":"paper-series","turnover":0.10,"end_equity":100100}',
                ),
                (
                    "run-2",
                    "2026-01-02",
                    "2026-01-02T21:00:00+00:00",
                    '{"series_id":"paper-series","turnover":0.25,"end_equity":100250}',
                ),
            ],
        )

    artifacts = generate_forward_outputs(database, tmp_path / "restart-output", feed="IEX")
    daily = pd.read_csv(artifacts["forward_daily_performance.csv"])
    summary = pd.read_csv(artifacts["forward_paper_summary.csv"])

    assert len(daily) == 1
    assert daily.loc[0, "end_equity"] == 100250
    assert summary.loc[0, "paper_account_turnover"] == pytest.approx(0.25)
