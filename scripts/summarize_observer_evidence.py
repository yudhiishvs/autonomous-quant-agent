#!/usr/bin/env python3
"""Summarize distinct observer/dry-run evidence without fabricating sessions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from adaptive_trader.config import load_config
from adaptive_trader.observer_evidence import summarize_observer_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/observer.yaml"))
    parser.add_argument(
        "--evidence-directory",
        type=Path,
        default=Path("outputs/observer_evidence"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    summary = summarize_observer_evidence(
        evidence_directory=args.evidence_directory,
        configuration_hash=config.configuration_hash,
        configuration=config,
        database_path=config.project.database_path,
    )
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Observer evidence summary: {args.output}")
    else:
        print(rendered, end="")
    complete = bool(
        summary["distinct_pass_observer_sessions"] >= 5
        and summary["controlled_restart_pass"]
        and summary["distinct_pass_real_data_dry_runs"] >= 3
        and summary["broker_submissions"] == 0
        and summary["broker_cancellations"] == 0
        and summary["broker_fills"] == 0
        and summary["duplicate_decisions"] == 0
        and summary["unresolved_blocking_incidents"] == 0
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
