"""Offline benchmark harness contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_pipeline_benchmark_reports_each_required_stage_without_thresholds() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "benchmark_pipeline.py"),
            "--warmups",
            "0",
            "--repeats",
            "1",
            "--iterations",
            "1",
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    report = json.loads(completed.stdout)
    assert report["schema"] == "offline-pipeline-benchmark-v1"
    assert report["input"] == {
        "fixture": "deterministic_synthetic",
        "iterations": 1,
        "repeats": 1,
        "warmups": 0,
    }
    assert set(report["measurements"]) == {
        "canonical_normalization",
        "decision_slot_claim",
        "fake_order_reconciliation",
        "fifteen_minute_aggregation",
        "one_minute_ingestion_persistence",
        "risk_decision",
    }
    for measurement in report["measurements"].values():
        assert measurement["operation_count_per_repeat"] == 1
        assert measurement["repeat_count"] == 1
        assert measurement["elapsed_seconds"]["minimum"] > 0
        assert measurement["latency_ms_per_operation"]["median"] > 0
        assert measurement["throughput_operations_per_second"]["maximum"] > 0
    assert all("threshold" not in name.lower() for name in report["measurements"])
    assert completed.stderr == ""


def test_pipeline_benchmark_rejects_unbounded_work() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "benchmark_pipeline.py"),
            "--iterations",
            "101",
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert "iterations must be between 1 and 100" in completed.stderr
