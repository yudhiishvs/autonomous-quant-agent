"""Canonical historical output and receipt checks."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pandas as pd

from adaptive_trader import cli
from adaptive_trader.persistence import AuditRepository, Database
from adaptive_trader.reporting import _historical_feed_disclosure, generate_outputs


def test_canonical_historical_artifacts_and_receipt_identity(fast_suite, tmp_path: Path) -> None:
    artifacts = generate_outputs(fast_suite, output_directory=tmp_path / "historical")
    required = {
        "metrics_post_2020.csv",
        "annual_returns.csv",
        "regime_metrics.csv",
        "rebalance_decisions.csv",
        "historical_equity_curves.png",
        "historical_drawdowns.png",
        "historical_regime_timeline.png",
        "historical_strategy_allocations.png",
        "historical_asset_weights.png",
        "historical_rolling_sharpe.png",
        "historical_risk_interventions.png",
    }

    assert required.issubset(artifacts)
    assert not pd.read_csv(artifacts["annual_returns.csv"]).empty
    assert not pd.read_csv(artifacts["regime_metrics.csv"]).empty
    assert not pd.read_csv(artifacts["rebalance_decisions.csv"]).empty

    first_receipt = json.loads(
        artifacts["decision_receipts.jsonl"].read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_receipt["decision_id"]
    assert first_receipt["run_id"] == fast_suite.config.project.run_name
    assert first_receipt["configuration_hash"] == fast_suite.config.configuration_hash
    assert first_receipt["mode"] == "backtest"
    assert first_receipt["market_session_date"] == first_receipt["execution_date"][:10]
    assert first_receipt["historical_data_cutoff"] < first_receipt["execution_date"]
    assert (
        "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET"
        in first_receipt["feed_disclosure"]
    )
    assert first_receipt["market_data_feed"] == "SYNTHETIC"
    assert "synthetic" in first_receipt["market_data_source"].lower()
    assert first_receipt["strategy_version"]
    assert first_receipt["simulated_portfolio_before_execution"]["equity"] > 0
    assert first_receipt["risk_inputs"]["current_weights"] is not None
    assert first_receipt["risk_limits"]["required_cash_buffer"] >= 0
    assert isinstance(first_receipt["order_intents"], list)
    assert first_receipt["broker_order_ids"] == []
    assert first_receipt["final_known_execution_status"]
    assert first_receipt["incidents"] == []

    report = artifacts["report.md"].read_text(encoding="utf-8")
    receipts_markdown = artifacts["decision_receipts.md"].read_text(encoding="utf-8")
    assert "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS" in report
    assert "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET" in report
    assert "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS" in receipts_markdown
    assert "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET" in receipts_markdown
    assert "not forward paper-trading performance" in report

    archives = list((tmp_path / "historical" / "runs").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "decision_receipts.jsonl").read_bytes() == artifacts[
        "decision_receipts.jsonl"
    ].read_bytes()

    context = cli.CommandContext(
        config_path=tmp_path / "backtest.yaml",
        project_root=tmp_path,
        config=fast_suite.config,
        database_path=tmp_path / "historical.db",
        output_directory=tmp_path / "historical",
    )
    first_run_receipts = [
        line
        for line in artifacts["decision_receipts.jsonl"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert first_run_receipts
    assert cli._persist_historical_receipts(
        context,
        fast_suite,
        artifacts["decision_receipts.jsonl"],
    ) == len(first_run_receipts)
    assert cli._persist_historical_receipts(
        context,
        fast_suite,
        artifacts["decision_receipts.jsonl"],
    ) == len(first_run_receipts)
    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        assert repository.count("application_runs") == 2
        assert repository.count("decision_receipts") == 2 * len(first_run_receipts)
        assert repository.count("rebalance_decisions") == 2 * len(first_run_receipts)
    finally:
        database.close()


def test_synthetic_history_never_claims_unverified_sip(fast_suite) -> None:
    config = deepcopy(fast_suite.config)
    config.market_data.feed = "SIP"

    disclosure = _historical_feed_disclosure(
        {"feed": "SYNTHETIC", "is_synthetic": True},
        config,
    )

    assert "REAL-TIME SIP FEED" not in disclosure
    assert "ENTITLEMENT UNCONFIRMED" in disclosure
    assert "NO SIP CLAIM OR FALLBACK" in disclosure


def test_frozen_research_windows_emit_separate_metric_tables(
    fast_suite,
    tmp_path: Path,
) -> None:
    suite = deepcopy(fast_suite)
    suite.config.backtest.development_period_start = "2018-01-01"
    suite.config.backtest.development_period_end = "2019-12-31"
    suite.config.backtest.validation_period_start = "2020-01-01"
    suite.config.backtest.validation_period_end = "2021-12-31"
    suite.config.backtest.holdout_period_start = "2022-01-01"
    suite.config.backtest.out_of_sample_start = "2022-01-01"
    suite.config.backtest.comparison_period_start = "2022-01-01"

    artifacts = generate_outputs(suite, output_directory=tmp_path / "frozen-windows")

    for filename in (
        "metrics_development.csv",
        "metrics_validation.csv",
        "metrics_holdout.csv",
    ):
        assert filename in artifacts
        metrics = pd.read_csv(artifacts[filename])
        assert set(metrics["portfolio"]) == set(suite.runs)
    report = artifacts["report.md"].read_text(encoding="utf-8")
    assert "Development description period — 2018-01-01 through 2019-12-31" in report
    assert "Validation description period — 2020-01-01 through 2021-12-31" in report
    assert "Locked-holdout metrics" in report
