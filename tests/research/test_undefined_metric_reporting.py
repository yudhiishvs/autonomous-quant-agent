"""Historical reports must distinguish undefined metrics from defined zeros."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from adaptive_trader.reporting import (
    _comparison_text,
    _csv_safe,
    _metrics_markdown,
    _metrics_table,
)


def _flat_run() -> SimpleNamespace:
    index = pd.date_range("2026-01-02", periods=3, freq="B")
    return SimpleNamespace(
        daily=pd.DataFrame(
            {
                "daily_return": [0.0, 0.0, 0.0],
                "gross_exposure": [0.0, 0.0, 0.0],
                "cash_weight": [1.0, 1.0, 1.0],
                "turnover": [0.0, 0.0, 0.0],
                "total_execution_cost": [0.0, 0.0, 0.0],
            },
            index=index,
        ),
        rebalances=[],
        risk_actions=[],
    )


def test_metric_csv_serializes_null_with_adjacent_reason(tmp_path) -> None:
    table = _metrics_table(
        {"adaptive": _flat_run(), "static_blend": _flat_run()},
        annualization_factor=252,
    )

    adaptive = table.set_index("portfolio").loc["adaptive"]
    assert adaptive["total_return"] == 0.0
    assert pd.isna(adaptive["sharpe_ratio"])
    assert "volatility is zero" in adaptive["sharpe_ratio_reason"]

    path = tmp_path / "metrics_full_period.csv"
    _csv_safe(table).to_csv(path, index=False)
    serialized = path.read_text(encoding="utf-8")
    restored = pd.read_csv(path).set_index("portfolio")

    assert "sharpe_ratio_reason" in restored
    assert pd.isna(restored.loc["adaptive", "sharpe_ratio"])
    assert restored.loc["adaptive", "total_return"] == 0.0
    assert "volatility is zero" in restored.loc["adaptive", "sharpe_ratio_reason"]
    assert ",nan," not in serialized.lower()
    assert ",inf," not in serialized.lower()


def test_markdown_explains_na_and_comparison_avoids_false_tie() -> None:
    table = _metrics_table(
        {"adaptive": _flat_run(), "static_blend": _flat_run()},
        annualization_factor=252,
    )

    markdown = _metrics_markdown(table)
    comparison = _comparison_text(table, "flat test period")

    assert "n/a" in markdown
    assert "Undefined metrics are shown as `n/a`; they are not zero" in markdown
    assert "volatility is zero" in markdown
    assert "conclusion for the flat test period is unavailable" in comparison
    assert "same realized Sharpe" not in comparison
