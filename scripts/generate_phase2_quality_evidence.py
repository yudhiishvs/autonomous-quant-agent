#!/usr/bin/env python3
"""Run local quality gates and write current-source-bound Phase 2 evidence."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adaptive_trader.config import load_config
from adaptive_trader.observer_evidence import (
    _current_quality_source_hash,
    _file_sha256,
    _read_junit,
    write_evidence_json,
)


def _run(command: list[str], root: Path) -> bool:
    completed = subprocess.run(
        command,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=900,
    )
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/observer.yaml"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/observer_evidence/quality/phase2_final_quality.json"),
    )
    args = parser.parse_args()
    root = args.config.resolve().parent.parent
    config = load_config(args.config)
    # Do not dereference a virtual-environment Python symlink here. On macOS a
    # venv executable may resolve into Homebrew's system framework, while the
    # project-owned Ruff, Mypy, and Pytest entry points remain in `.venv/bin`.
    python_bin = Path(sys.executable)
    bin_directory = python_bin.parent
    ruff = bin_directory / "ruff"
    mypy = bin_directory / "mypy"
    pytest = bin_directory / "pytest"
    junit = root / "outputs" / "observer_evidence" / "quality" / "phase2_final_tests.xml"
    junit_ok, junit_totals = _read_junit(junit)

    statuses: dict[str, str] = {}
    statuses["ruff"] = (
        "PASS"
        if ruff.is_file()
        and _run([str(ruff), "check", "."], root)
        and _run([str(ruff), "format", "--check", "."], root)
        else "FAIL"
    )
    statuses["mypy"] = "PASS" if mypy.is_file() and _run([str(mypy), "src"], root) else "FAIL"
    static_ok = bool(
        pytest.is_file()
        and _run(
            [
                str(pytest),
                "-q",
                "tests/safety/test_static_repository_safety.py",
            ],
            root,
        )
    )
    statuses["safety_static_checks"] = "PASS" if static_ok else "FAIL"
    try:
        observer = load_config(root / "configs" / "observer.yaml")
        backtest = load_config(root / "configs" / "backtest.yaml")
        config_ok = bool(
            observer.execution.paper_only
            and not observer.execution.paper_order_submission_enabled
            and backtest.execution.paper_only
            and not backtest.execution.paper_order_submission_enabled
        )
    except Exception:
        config_ok = False
    statuses["config_validation"] = "PASS" if config_ok else "FAIL"
    passed = bool(junit_ok and all(value == "PASS" for value in statuses.values()))
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase2_final_quality",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if passed else "FAIL",
        "configuration_hash": config.configuration_hash,
        "source_hash": _current_quality_source_hash(root),
        "junit_sha256": _file_sha256(junit) if junit.is_file() else None,
        "junit_totals": junit_totals,
        "checks": statuses,
    }
    output = (root / args.output).resolve()
    write_evidence_json(report, output)
    print(f"Phase 2 final quality status: {report['status']}")
    print(f"Quality evidence: {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
