"""End-to-end verification of persistent artifacts and the dashboard import boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pandas as pd
import yaml

from adaptive_trader.config import AppConfig
from adaptive_trader.reporting import generate_outputs

CSV_ARTIFACTS = {
    "metrics_full_period.csv",
    "metrics_out_of_sample.csv",
    "daily_portfolio_values.csv",
    "daily_returns.csv",
    "daily_drawdowns.csv",
    "asset_weights.csv",
    "strategy_allocations.csv",
    "regimes.csv",
    "trades_or_rebalances.csv",
    "risk_actions.csv",
}

SERIALIZED_ARTIFACTS = {
    "decision_receipts.jsonl",
    "decision_receipts.md",
    "run_configuration.yaml",
    "data_summary.json",
    "report.md",
}

PLOT_ARTIFACTS = {
    "equity_curves.png",
    "equity_curves_log_scale.png",
    "drawdowns.png",
    "regime_timeline.png",
    "strategy_allocations.png",
    "adaptive_asset_weights.png",
    "rolling_sharpe.png",
    "risk_interventions.png",
}

REPORT_SECTIONS = (
    "## 1. Executive summary",
    "## 2. Research question",
    "## 3. Data and universe",
    "## 4. Strategy definitions",
    "## 5. Regime definition",
    "## 6. Risk controls",
    "## 7. Anti-look-ahead design",
    "## 8. Transaction-cost assumptions",
    "## 9. Full-period metrics",
    "## 10. Metrics from the configured analysis start",
    "## 11. Did adaptive allocation help?",
    "## 12. Risk-intervention summary",
    "## 13. Limitations",
    "## 14. Proposed semester-long extensions",
    "## 15. Educational-use statement",
)


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"Nonstandard JSON constant encountered: {value}")


def _import_dashboard(project_root: Path) -> ModuleType:
    app_path = project_root / "app.py"
    specification = importlib.util.spec_from_file_location(
        "adaptive_portfolio_dashboard_import_check", app_path
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_full_pipeline_writes_valid_complete_artifact_set(fast_suite, tmp_path: Path) -> None:
    output_directory = tmp_path / "integration_outputs"

    artifacts = generate_outputs(fast_suite, output_directory=output_directory)

    required = CSV_ARTIFACTS | SERIALIZED_ARTIFACTS | PLOT_ARTIFACTS
    assert required.issubset(artifacts)
    for filename in required:
        path = artifacts[filename]
        assert path == output_directory / filename
        assert path.is_file(), f"Missing required artifact: {filename}"
        assert path.stat().st_size > 0, f"Required artifact is empty: {filename}"

    for filename in CSV_ARTIFACTS:
        frame = pd.read_csv(artifacts[filename])
        assert len(frame.columns) > 0, f"CSV has no schema: {filename}"

    receipt_lines = [
        line
        for line in artifacts["decision_receipts.jsonl"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    receipts = [
        json.loads(line, parse_constant=_reject_nonstandard_json_constant) for line in receipt_lines
    ]
    expected_receipts = sum(len(run.rebalances) for run in fast_suite.runs.values())
    assert len(receipts) == expected_receipts
    assert all(
        {
            "portfolio",
            "signal_as_of_date",
            "execution_date",
            "proposed_asset_weights",
            "risk_adjusted_weights",
            "turnover",
            "hard_stop_status",
        }.issubset(receipt)
        for receipt in receipts
    )

    configuration_values = yaml.safe_load(
        artifacts["run_configuration.yaml"].read_text(encoding="utf-8")
    )
    restored_config = AppConfig.from_dict(configuration_values)
    assert restored_config.to_dict() == fast_suite.config.to_dict()

    data_summary = json.loads(
        artifacts["data_summary.json"].read_text(encoding="utf-8"),
        parse_constant=_reject_nonstandard_json_constant,
    )
    prices = fast_suite.market_data.prices
    assert data_summary["is_synthetic"] is True
    assert "synthetic" in data_summary["source"].lower()
    assert "not real-market evidence" in data_summary["source_disclosure"].lower()
    assert data_summary["benchmark"] == fast_suite.config.data.benchmark
    assert data_summary["configured_tickers"] == fast_suite.config.data.tickers
    assert data_summary["available_tickers"] == list(prices.columns)
    assert data_summary["start_date"] == prices.index.min().date().isoformat()
    assert data_summary["end_date"] == prices.index.max().date().isoformat()
    assert data_summary["trading_days"] == len(prices)
    assert data_summary["price_observations"] == {
        ticker: int(prices[ticker].notna().sum()) for ticker in prices.columns
    }

    report = artifacts["report.md"].read_text(encoding="utf-8")
    heading_offsets = [report.index(section) for section in REPORT_SECTIONS]
    assert heading_offsets == sorted(heading_offsets)
    assert "synthetic-data disclosure" in report.lower()
    assert "not real-market evidence" in report.lower()
    assert "not investment advice" in report.lower()

    for filename in PLOT_ARTIFACTS:
        assert artifacts[filename].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    dashboard = _import_dashboard(Path(__file__).resolve().parents[1])
    assert callable(dashboard.main)
    assert dashboard.DEFAULT_CONFIG.is_file()
