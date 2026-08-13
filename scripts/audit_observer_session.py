#!/usr/bin/env python3
"""Audit one genuine completed observer session from the official database."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from adaptive_trader.config import load_config
from adaptive_trader.observer_evidence import (
    audit_observer_session,
    write_evidence_json,
    write_evidence_markdown,
)


def _project_root(config_path: Path) -> Path:
    resolved = config_path.resolve()
    for candidate in (resolved.parent, *resolved.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/observer.yaml"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument(
        "--controlled-restart",
        action="store_true",
        help="Request restart evaluation; durable restart-drill evidence is still required.",
    )
    parser.add_argument(
        "--minimum-session-minutes",
        type=int,
        default=300,
        help="May raise, but never lower, the non-bypassable 300-minute floor.",
    )
    args = parser.parse_args()

    root = _project_root(args.config)
    config = load_config(args.config)
    selected_date = args.session_date or datetime.now(ZoneInfo(config.project.timezone)).date()
    database = args.database or root / config.project.database_path
    report = audit_observer_session(
        config,
        database_path=database,
        session_date=selected_date,
        controlled_restart=args.controlled_restart,
        minimum_session_minutes=max(300, args.minimum_session_minutes),
    )
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"observer_session_{selected_date.isoformat()}_{stamp}"
    json_path = root / "outputs" / "observer_evidence" / "sessions" / f"{stem}.json"
    markdown_path = root / "audit" / f"{stem}.md"
    write_evidence_json(report, json_path)
    write_evidence_markdown(report, markdown_path, title="Observer Session Audit")
    print(f"Observer session status: {report['status']}")
    print(f"JSON evidence: {json_path}")
    print(f"Markdown audit: {markdown_path}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
