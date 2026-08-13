#!/usr/bin/env python3
"""Create and verify a consistent SQLite backup without exposing credentials."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from adaptive_trader.config import load_config
from adaptive_trader.observer_evidence import (
    summarize_observer_evidence,
    write_evidence_json,
)


def _default_destination(source: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return source.parent / "backups" / f"{source.stem}-{timestamp}{source.suffix}"


def _integrity_check(connection: sqlite3.Connection, label: str) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise RuntimeError(f"{label} database failed integrity_check")


def create_backup(source: Path, destination: Path | None = None) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    destination = (destination or _default_destination(source)).expanduser().resolve()
    if destination == source:
        raise ValueError("Backup destination must differ from the source database")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite an existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            _integrity_check(source_connection, "Source")
            with sqlite3.connect(destination) as destination_connection:
                source_connection.backup(destination_connection)
                _integrity_check(destination_connection, "Backup")
        os.chmod(destination, 0o600)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_backup_manifest(
    *,
    source: Path,
    backup: Path,
    config_path: Path,
    evidence_directory: Path,
    manifest_path: Path,
) -> Path:
    config_path = config_path.expanduser().resolve()
    root = config_path.parent.parent
    config = load_config(config_path)
    summary = summarize_observer_evidence(
        evidence_directory=(root / evidence_directory).resolve(),
        configuration_hash=config.configuration_hash,
        configuration=config,
        database_path=source,
    )
    record = {
        "schema_version": 1,
        "kind": "observer_database_backup",
        "created_at": datetime.now(UTC).isoformat(),
        "configuration_hash": config.configuration_hash,
        "primary_database_path": str(source.resolve()),
        "primary_database_sha256": _sha256(source),
        "backup_path": str(backup.resolve()),
        "backup_sha256": _sha256(backup),
        "accepted_session_run_ids": list(summary["accepted_session_run_ids"]),
        "integrity_check": "ok",
    }
    destination = (root / manifest_path).resolve()
    write_evidence_json(record, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("runtime/adaptive_portfolio_agent.db"),
    )
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--evidence-directory",
        type=Path,
        default=Path("outputs/observer_evidence"),
    )
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    backup = create_backup(args.source, args.destination)
    print(f"Verified database backup: {backup}")
    if args.manifest is not None:
        if args.config is None:
            parser.error("--manifest requires --config")
        manifest = write_backup_manifest(
            source=args.source.expanduser().resolve(),
            backup=backup,
            config_path=args.config,
            evidence_directory=args.evidence_directory,
            manifest_path=args.manifest,
        )
        print(f"Hash-bound backup manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
