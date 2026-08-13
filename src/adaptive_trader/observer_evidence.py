"""Read-only observer evidence inspection and readiness reporting.

This module never constructs a broker, never opens the primary database in
writable mode, and never synthesizes evidence.  Session and dry-run evidence is
accepted only from explicit, versioned JSON records with real-Alpaca
provenance.  Missing elapsed sessions are ``INCOMPLETE`` rather than failures;
unsafe configuration, corrupt evidence, or any recorded broker mutation is a
``FAIL``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from adaptive_trader.constants import (
    PAPER_API_KEY_ENV,
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    PAPER_SECRET_KEY_ENV,
    PAPER_TRADING_BANNER,
)

NEW_YORK = ZoneInfo("America/New_York")
REAL_STREAM_SOURCES = frozenset({"alpaca_stream", "alpaca_stream_update"})
DRY_RUN_REAL_SOURCES = frozenset({*REAL_STREAM_SOURCES, "alpaca_historical"})
OBSERVER_EVIDENCE_SCHEMA_VERSION = 1
QUALITY_EVIDENCE_SUBDIRECTORY = "quality"
RESEARCH_BUNDLE_ROLES = {
    "configs/backtest.yaml": "frozen_research_configuration",
    "docs/methodology.md": "frozen_research_protocol",
    "src/adaptive_trader/config.py": "configuration_validation_and_hashing",
    "src/adaptive_trader/strategies/__init__.py": "strategy_package_surface",
    "src/adaptive_trader/strategies/base.py": "strategy_contract",
    "src/adaptive_trader/strategies/mean_reversion.py": "mean_reversion_strategy",
    "src/adaptive_trader/strategies/momentum.py": "momentum_strategy",
    "src/adaptive_trader/features.py": "causal_feature_calculation",
    "src/adaptive_trader/allocator.py": "adaptive_allocation",
    "src/adaptive_trader/regimes.py": "regime_classification",
    "src/adaptive_trader/risk.py": "independent_risk_engine",
    "src/adaptive_trader/backtest.py": "causal_historical_execution",
    "src/adaptive_trader/data.py": "historical_data_retrieval_provenance_and_validation",
    "src/adaptive_trader/metrics.py": "evaluation_metrics",
    "src/adaptive_trader/reporting.py": "research_artifact_generation",
}


def evidence_content_hash(record: Mapping[str, Any]) -> str:
    """Hash a record while excluding its transport path and self-hash field."""

    payload = {
        str(key): _json_safe(value)
        for key, value in record.items()
        if key not in {"_evidence_path", "evidence_sha256"}
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_hash_valid(record: Mapping[str, Any]) -> bool:
    expected = record.get("evidence_sha256")
    try:
        return isinstance(expected, str) and expected == evidence_content_hash(record)
    except (TypeError, ValueError):
        return False


def _zero_integer_fields(record: Mapping[str, Any], fields: Iterable[str]) -> bool:
    try:
        return all(int(record.get(field, -1)) == 0 for field in fields)
    except (TypeError, ValueError, OverflowError):
        return False


def derive_real_data_dry_run_config(
    configuration: Any,
    *,
    database_path: str | Path,
    output_directory: str | Path,
) -> Any:
    """Create a strictly validated dry-run config with truthful destinations."""

    from adaptive_trader.config import AppConfig

    values = configuration.to_canonical_dict()
    values["project"] = {
        **values["project"],
        "run_name": f"{configuration.project.run_name}_real_data_dry_run",
        "database_path": str(Path(database_path).expanduser().resolve()),
        "output_directory": str(Path(output_directory).expanduser().resolve()),
    }
    values["execution"] = {
        **values["execution"],
        "paper_order_submission_enabled": False,
    }
    return AppConfig.from_dict(values)


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if value in {None, ""}:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


@contextmanager
def _read_only_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database without creating files or journals."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Observer database does not exist: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _placeholders(values: Sequence[str]) -> str:
    return ",".join("?" for _ in values)


def _query_count(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> int:
    row = connection.execute(sql, tuple(parameters)).fetchone()
    return 0 if row is None else int(row[0])


def _freshness_is_fresh(payload: Mapping[str, Any]) -> bool:
    freshness = payload.get("freshness")
    if not isinstance(freshness, Mapping):
        return False
    return bool(freshness.get("stream_healthy")) and not any(
        (
            freshness.get("missing_symbols"),
            freshness.get("stale_symbols"),
            freshness.get("unresolved_gap"),
        )
    )


def _asset_evidence_valid(payload: Mapping[str, Any], symbols: Sequence[str]) -> bool:
    values = payload.get("symbols")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return False
    return {str(value).upper() for value in values} == {str(symbol).upper() for symbol in symbols}


def _history_evidence_valid(
    payload: Mapping[str, Any],
    *,
    configuration: Any,
    session_date: date,
) -> bool:
    raw_observations = payload.get("observations")
    raw_minimum = payload.get("minimum_required")
    if not isinstance(raw_observations, (str, int, float)) or not isinstance(
        raw_minimum, (str, int, float)
    ):
        return False
    try:
        observations = int(raw_observations)
        minimum_required = int(raw_minimum)
        configured_minimum = int(configuration.market_data.minimum_completed_sessions)
    except (TypeError, ValueError, AttributeError):
        return False
    cutoff = _timestamp(payload.get("cutoff"))
    return bool(
        observations >= minimum_required >= configured_minimum > 0
        and cutoff is not None
        and cutoff.astimezone(NEW_YORK).date() < session_date
    )


def _add_session_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
    **evidence: Any,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "detail": detail,
            "evidence": _json_safe(evidence),
        }
    )


def audit_observer_session(
    configuration: Any,
    *,
    database_path: str | Path,
    session_date: date,
    controlled_restart: bool = False,
    minimum_session_minutes: int = 300,
) -> dict[str, Any]:
    """Evaluate one completed, real-market observer session from durable facts."""

    minimum_session_minutes = max(300, int(minimum_session_minutes))
    path = Path(database_path).expanduser().resolve()
    symbols = tuple(str(value).upper() for value in configuration.data.tickers)
    config_hash = str(configuration.configuration_hash)
    feed = str(configuration.market_data.feed).upper()
    provider = str(configuration.market_data.provider).lower()
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema_version": OBSERVER_EVIDENCE_SCHEMA_VERSION,
        "kind": "observer_session",
        "generated_at": datetime.now(UTC).isoformat(),
        "session_date": session_date.isoformat(),
        "configuration_hash": config_hash,
        "database_path": str(path),
        "provider": provider,
        "feed": feed,
        "real_market_data": False,
        "controlled_restart_requested": bool(controlled_restart),
        "minimum_session_minutes": int(minimum_session_minutes),
        "checks": checks,
    }
    _add_session_check(
        checks,
        "submission_disabled",
        configuration.execution.paper_order_submission_enabled is False,
        "observer evidence requires paper-order submission to remain disabled",
    )
    _add_session_check(
        checks,
        "paper_only",
        configuration.execution.paper_only is True,
        "the observer configuration must remain structurally paper-only",
    )
    _add_session_check(
        checks,
        "alpaca_provider",
        provider == "alpaca" and feed in {"IEX", "SIP"},
        "only explicit Alpaca IEX/SIP observations count as real-market evidence",
        provider=provider,
        feed=feed,
    )
    if not path.is_file():
        _add_session_check(
            checks,
            "database_exists",
            False,
            "the official observer database does not exist",
            path=path,
        )
        report.update(_empty_session_counts(symbols))
        report["status"] = "FAIL"
        return report

    required_tables = {
        "application_runs",
        "market_bars",
        "stream_events",
        "account_snapshots",
        "heartbeats",
        "rebalance_decisions",
        "decision_receipts",
        "order_intents",
        "broker_orders",
        "order_events",
        "fill_events",
        "reconciliation_runs",
        "reconciliation_discrepancies",
        "market_data_gaps",
        "system_incidents",
        "halt_events",
        "generated_reports",
    }
    try:
        with _read_only_database(path) as connection:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = None if integrity_row is None else str(integrity_row[0])
            _add_session_check(
                checks,
                "database_integrity",
                integrity == "ok",
                "SQLite PRAGMA integrity_check must return ok",
                result=integrity,
            )
            missing_tables = sorted(required_tables - _table_names(connection))
            _add_session_check(
                checks,
                "observer_schema",
                not missing_tables,
                "all observer evidence tables must exist",
                missing_tables=missing_tables,
            )
            if missing_tables:
                report.update(_empty_session_counts(symbols))
                report["status"] = "FAIL"
                return report

            matching_runs = connection.execute(
                """
                SELECT run_id, started_at, ended_at, market_data_feed, shutdown_reason
                FROM application_runs
                WHERE mode = 'observe' AND configuration_hash = ?
                ORDER BY started_at
                """,
                (config_hash,),
            ).fetchall()
            relevant: list[sqlite3.Row] = []
            for row in matching_runs:
                started = _timestamp(row["started_at"])
                ended = _timestamp(row["ended_at"])
                if started is None:
                    continue
                end_for_range = ended or datetime.now(UTC)
                if (
                    started.astimezone(NEW_YORK).date()
                    <= session_date
                    <= end_for_range.astimezone(NEW_YORK).date()
                ):
                    relevant.append(row)
            run_ids = tuple(str(row["run_id"]) for row in relevant)
            report["run_ids"] = list(run_ids)
            _add_session_check(
                checks,
                "observer_runs_present",
                bool(run_ids),
                "at least one matching observe-mode run must cover the requested session",
                matching_run_ids=run_ids,
            )
            if not run_ids:
                report.update(_empty_session_counts(symbols))
                report["status"] = "FAIL"
                return report

            marker_type = f"session_close_finalization:{session_date.isoformat()}"
            report_type = f"daily_forward_session:{session_date.isoformat()}"
            run_clause = _placeholders(run_ids)
            marker_rows = connection.execute(
                f"SELECT metadata FROM generated_reports WHERE run_id IN ({run_clause}) "
                "AND report_type = ?",
                (*run_ids, marker_type),
            ).fetchall()
            marker_metadata = next(
                (
                    metadata
                    for metadata in (_json_object(row["metadata"]) for row in marker_rows)
                    if metadata.get("finalized") is True
                    and _timestamp(metadata.get("session_open_at")) is not None
                    and _timestamp(metadata.get("session_close_at")) is not None
                ),
                None,
            )
            calendar_open = (
                None
                if marker_metadata is None
                else _timestamp(marker_metadata.get("session_open_at"))
            )
            calendar_close = (
                None
                if marker_metadata is None
                else _timestamp(marker_metadata.get("session_close_at"))
            )
            calendar_completed = bool(
                calendar_open is not None
                and calendar_close is not None
                and calendar_close > calendar_open
                and calendar_open.astimezone(NEW_YORK).date() == session_date
                and calendar_close.astimezone(NEW_YORK).date() == session_date
            )
            run_starts = [
                value
                for value in (_timestamp(row["started_at"]) for row in relevant)
                if value is not None
            ]
            run_ends = [
                value
                for value in (_timestamp(row["ended_at"]) for row in relevant)
                if value is not None
            ]
            runtime_covers_session = bool(
                calendar_open is not None
                and calendar_close is not None
                and run_starts
                and run_ends
                and min(run_starts) <= calendar_open
                and max(run_ends) >= calendar_close
            )
            daily_rows = connection.execute(
                f"SELECT metadata FROM generated_reports WHERE run_id IN ({run_clause}) "
                "AND report_type = ?",
                (*run_ids, report_type),
            ).fetchall()
            daily_report_present = any(
                bool(_json_object(row["metadata"]).get("generated")) for row in daily_rows
            )
            _add_session_check(
                checks,
                "completed_regular_market_session",
                calendar_completed,
                "a broker-calendar-backed market-close finalization is required",
                marker=marker_type,
            )
            _add_session_check(
                checks,
                "full_calendar_runtime",
                runtime_covers_session,
                "observer runs must collectively cover the broker-calendar open through close",
                earliest_run_start=min(run_starts).isoformat() if run_starts else None,
                latest_run_end=max(run_ends).isoformat() if run_ends else None,
            )

            bar_event_rows = connection.execute(
                f"SELECT symbol, payload FROM stream_events WHERE run_id IN ({run_clause}) "
                "AND event_type IN ('bar_inserted', 'bar_duplicate', 'bar_corrected')",
                run_ids,
            ).fetchall()
            run_bar_keys: set[tuple[str, str, str]] = set()
            for event in bar_event_rows:
                payload = _json_object(event["payload"])
                run_bar_keys.add(
                    (
                        str(event["symbol"] or "").upper(),
                        str(payload.get("start") or ""),
                        str(payload.get("feed") or "").upper(),
                    )
                )
            bar_rows = connection.execute(
                "SELECT symbol, start_at, received_at, feed, source FROM market_bars"
            ).fetchall()
            by_symbol: dict[str, list[datetime]] = {symbol: [] for symbol in symbols}
            sources_by_symbol: dict[str, set[str]] = {symbol: set() for symbol in symbols}
            rejected_sources: set[str] = set()
            last_bar: datetime | None = None
            for row in bar_rows:
                started = _timestamp(row["start_at"])
                received = _timestamp(row["received_at"])
                if started is None or received is None:
                    continue
                if started.astimezone(NEW_YORK).date() != session_date:
                    continue
                key = (str(row["symbol"]).upper(), started.isoformat(), str(row["feed"]).upper())
                if key not in run_bar_keys:
                    continue
                source = str(row["source"])
                if (
                    calendar_open is None
                    or calendar_close is None
                    or not calendar_open <= started < calendar_close
                ):
                    continue
                if source not in DRY_RUN_REAL_SOURCES or str(row["feed"]).upper() != feed:
                    rejected_sources.add(f"{source}:{row['feed']}")
                    continue
                symbol = str(row["symbol"]).upper()
                if symbol in by_symbol:
                    by_symbol[symbol].append(started)
                    sources_by_symbol[symbol].add(source)
                    last_bar = started if last_bar is None else max(last_bar, started)
            unique_by_symbol = {symbol: set(values) for symbol, values in by_symbol.items()}
            bar_counts = {symbol: len(values) for symbol, values in unique_by_symbol.items()}
            spans = {
                symbol: (
                    (max(values) - min(values)).total_seconds() / 60.0 if len(values) >= 2 else 0.0
                )
                for symbol, values in unique_by_symbol.items()
            }
            largest_gap = 0.0
            for values in unique_by_symbol.values():
                ordered = sorted(values)
                largest_gap = max(
                    largest_gap,
                    max(
                        (
                            max(0.0, (right - left).total_seconds() / 60.0 - 1.0)
                            for left, right in pairwise(ordered)
                        ),
                        default=0.0,
                    ),
                )
            expected_regular_minutes = (
                0
                if calendar_open is None or calendar_close is None
                else int((calendar_close - calendar_open).total_seconds() // 60)
            )
            expected_starts = (
                set()
                if calendar_open is None
                else {
                    calendar_open + timedelta(minutes=offset)
                    for offset in range(expected_regular_minutes)
                }
            )
            missing_expected_minutes = {
                symbol: len(expected_starts - values) for symbol, values in unique_by_symbol.items()
            }
            full_symbols = {
                symbol
                for symbol, values in unique_by_symbol.items()
                if expected_starts and not (expected_starts - values)
            }
            minimum_bar_count = expected_regular_minutes
            required_data = bool(
                full_symbols == set(symbols)
                and expected_regular_minutes > 0
                and all(not count for count in missing_expected_minutes.values())
                and largest_gap <= 5.0
                and not rejected_sources
            )
            report.update(
                {
                    "bars_received_by_symbol": bar_counts,
                    "bar_sources_by_symbol": {
                        symbol: sorted(values) for symbol, values in sources_by_symbol.items()
                    },
                    "bar_span_minutes_by_symbol": spans,
                    "largest_data_gap_minutes": largest_gap,
                    "last_bar_time": None if last_bar is None else last_bar.isoformat(),
                    "session_open_at": (
                        None if calendar_open is None else calendar_open.isoformat()
                    ),
                    "session_close_at": (
                        None if calendar_close is None else calendar_close.isoformat()
                    ),
                    "expected_regular_session_minutes": expected_regular_minutes,
                }
            )
            _add_session_check(
                checks,
                "required_real_stream_data",
                required_data,
                "every symbol needs complete exact-feed Alpaca stream/recovery minute coverage",
                missing_or_short_symbols=sorted(set(symbols) - full_symbols),
                missing_expected_minutes=missing_expected_minutes,
                rejected_sources=sorted(rejected_sources),
                minimum_bar_count=minimum_bar_count,
                largest_gap_minutes=largest_gap,
                maximum_gap_minutes=5.0,
            )

            decisions = connection.execute(
                f"SELECT decision_id, idempotency_key FROM rebalance_decisions "
                f"WHERE run_id IN ({run_clause}) AND session_date = ?",
                (*run_ids, session_date.isoformat()),
            ).fetchall()
            decision_ids = tuple(str(row["decision_id"]) for row in decisions)
            duplicate_decisions = max(
                0,
                len(decisions) - len({str(row["idempotency_key"]) for row in decisions}),
            )
            receipts: list[dict[str, Any]] = []
            if decision_ids:
                decision_clause = _placeholders(decision_ids)
                receipts = [
                    _json_object(row["payload"])
                    for row in connection.execute(
                        f"SELECT payload FROM decision_receipts WHERE decision_id IN "
                        f"({decision_clause})",
                        decision_ids,
                    ).fetchall()
                ]
            freshness_at_decision = bool(receipts) and all(
                _freshness_is_fresh(payload) for payload in receipts
            )
            hypothetical_intents = (
                0
                if not decision_ids
                else _query_count(
                    connection,
                    f"SELECT COUNT(*) FROM order_intents WHERE decision_id IN "
                    f"({_placeholders(decision_ids)}) AND reason LIKE 'hypothetical_%'",
                    decision_ids,
                )
            )
            # The official observer database is a zero-mutation ledger.  Any
            # broker mutation anywhere in it disqualifies every session,
            # including stale-config and orphan facts.
            submissions = _query_count(connection, "SELECT COUNT(*) FROM broker_orders")
            cancellations = _query_count(
                connection,
                "SELECT COUNT(*) FROM stream_events WHERE lower(event_type) LIKE '%cancel%'",
            )
            fill_rows = connection.execute(
                "SELECT f.client_order_id, f.created_at, o.run_id AS order_run_id "
                "FROM fill_events f LEFT JOIN broker_orders o "
                "ON o.client_order_id = f.client_order_id"
            ).fetchall()
            fills = len(fill_rows)
            orphan_fills = sum(row["order_run_id"] is None for row in fill_rows)
            reconciliation_discrepancies = _query_count(
                connection,
                "SELECT COUNT(*) FROM reconciliation_discrepancies d "
                "JOIN reconciliation_runs r ON r.reconciliation_id = d.reconciliation_id "
                f"WHERE r.run_id IN ({run_clause})",
                run_ids,
            )
            unresolved_reconciliation = _query_count(
                connection,
                "SELECT COUNT(*) FROM reconciliation_discrepancies d "
                "JOIN reconciliation_runs r ON r.reconciliation_id = d.reconciliation_id "
                f"WHERE r.run_id IN ({run_clause}) AND d.resolved_at IS NULL "
                "AND lower(d.severity) IN ('blocking', 'critical')",
                run_ids,
            )
            unresolved_incidents = _query_count(
                connection,
                f"SELECT COUNT(*) FROM system_incidents WHERE run_id IN ({run_clause}) "
                "AND resolved_at IS NULL",
                run_ids,
            )
            hard_stops = _query_count(
                connection,
                f"SELECT COUNT(*) FROM halt_events WHERE run_id IN ({run_clause}) "
                "AND latch_type = 'hard_stop' AND action IN ('halt', 'hard_stop')",
                run_ids,
            )
            halt_rows = connection.execute(
                "SELECT action, latch_type, created_at FROM halt_events ORDER BY created_at"
            ).fetchall()
            active_latches: set[str] = set()
            for halt in halt_rows:
                created = _timestamp(halt["created_at"])
                if calendar_close is not None and created is not None and created > calendar_close:
                    continue
                latch_type = str(halt["latch_type"])
                if str(halt["action"]) in {"halt", "hard_stop", "daily_loss"}:
                    active_latches.add(latch_type)
                elif str(halt["action"]) in {"resume", "expired"}:
                    active_latches.discard(latch_type)
            unresolved_gaps = _query_count(
                connection,
                f"SELECT COUNT(*) FROM market_data_gaps WHERE run_id IN ({run_clause}) "
                "AND resolved_at IS NULL",
                run_ids,
            )
            startup_rows = connection.execute(
                f"SELECT run_id, stream, event_type, payload FROM stream_events "
                f"WHERE run_id IN ({run_clause})",
                run_ids,
            ).fetchall()
            snapshot_rows = connection.execute(
                f"SELECT snapshot_id, run_id, account_id_hash, status, trading_blocked "
                f"FROM account_snapshots WHERE run_id IN ({run_clause})",
                run_ids,
            ).fetchall()
            snapshots = {str(row["snapshot_id"]): row for row in snapshot_rows}
            startup_by_run: dict[str, bool] = {}
            for current_run_id in run_ids:
                current_events = [
                    row for row in startup_rows if str(row["run_id"]) == current_run_id
                ]
                stream_types = {
                    (str(row["stream"]), str(row["event_type"])) for row in current_events
                }
                feed_ok = any(
                    str(payload.get("feed") or "").upper() == feed
                    and payload.get("fallback_used") is False
                    for payload in (
                        _json_object(row["payload"])
                        for row in current_events
                        if row["event_type"] == "feed_entitlement_confirmed"
                    )
                )
                assets_ok = any(
                    _asset_evidence_valid(payload, symbols)
                    for payload in (
                        _json_object(row["payload"])
                        for row in current_events
                        if row["event_type"] == "asset_validation_confirmed"
                    )
                )
                history_ok = any(
                    _history_evidence_valid(
                        payload,
                        configuration=configuration,
                        session_date=session_date,
                    )
                    for payload in (
                        _json_object(row["payload"])
                        for row in current_events
                        if row["event_type"] == "history_preflight_confirmed"
                    )
                )
                account_ok = False
                for row in current_events:
                    if row["event_type"] != "paper_account_verified":
                        continue
                    payload = _json_object(row["payload"])
                    snapshot = snapshots.get(str(payload.get("account_snapshot_id") or ""))
                    if (
                        payload.get("paper_only") is True
                        and payload.get("adapter") == "AlpacaPaperBroker"
                        and str(payload.get("account_status") or "").upper() == "ACTIVE"
                        and payload.get("trading_blocked") is False
                        and snapshot is not None
                        and str(snapshot["run_id"]) == current_run_id
                        and str(snapshot["status"]).upper() == "ACTIVE"
                        and not bool(snapshot["trading_blocked"])
                    ):
                        account_ok = True
                        break
                startup_by_run[current_run_id] = bool(
                    feed_ok
                    and assets_ok
                    and history_ok
                    and account_ok
                    and ("market_data", "connected") in stream_types
                    and ("trade_updates", "connected") in stream_types
                )
            startup_complete = bool(startup_by_run and all(startup_by_run.values()))

            heartbeat_continuity = True
            heartbeat_counts: dict[str, int] = {}
            max_heartbeat_gap = 0.0
            maximum_gap = max(90.0, float(configuration.schedule.heartbeat_interval_seconds) * 3)
            clean_shutdown = True
            all_session_heartbeats: list[tuple[datetime, bool]] = []
            for run in relevant:
                run_id = str(run["run_id"])
                raw_heartbeats = connection.execute(
                    "SELECT created_at, healthy FROM heartbeats WHERE run_id = ? "
                    "ORDER BY created_at",
                    (run_id,),
                ).fetchall()
                heartbeat_times = [
                    value
                    for value in (_timestamp(row["created_at"]) for row in raw_heartbeats)
                    if value is not None
                ]
                heartbeat_counts[run_id] = len(heartbeat_times)
                all_session_heartbeats.extend(
                    (created, bool(row["healthy"]))
                    for row in raw_heartbeats
                    if (created := _timestamp(row["created_at"])) is not None
                    and calendar_open is not None
                    and calendar_close is not None
                    and calendar_open <= created <= calendar_close
                )
                clean_shutdown = clean_shutdown and run["ended_at"] is not None
                clean_shutdown = clean_shutdown and bool(str(run["shutdown_reason"] or "").strip())

            ordered_heartbeats = sorted(all_session_heartbeats, key=lambda item: item[0])
            merged_times = [item[0] for item in ordered_heartbeats]
            heartbeat_gaps = [
                (right - left).total_seconds() for left, right in pairwise(merged_times)
            ]
            if calendar_open is not None and calendar_close is not None and merged_times:
                heartbeat_gaps.extend(
                    (
                        (merged_times[0] - calendar_open).total_seconds(),
                        (calendar_close - merged_times[-1]).total_seconds(),
                    )
                )
            max_heartbeat_gap = max(heartbeat_gaps, default=0.0)
            heartbeat_continuity = bool(
                merged_times
                and max_heartbeat_gap <= maximum_gap
                and all(healthy for _, healthy in ordered_heartbeats)
            )

            configured_feed_recorded = all(
                str(row["market_data_feed"] or "").upper() == feed for row in relevant
            )
            restart_pass = bool(controlled_restart and len(run_ids) >= 2 and clean_shutdown)
            report.update(
                {
                    "market_calendar_status": (
                        "COMPLETED_REGULAR_SESSION" if calendar_completed else "UNVERIFIED"
                    ),
                    "freshness_at_decision": freshness_at_decision,
                    "strategy_decisions": len(decisions),
                    "hypothetical_order_intents": hypothetical_intents,
                    "broker_submissions": submissions,
                    "broker_cancellations": cancellations,
                    "broker_fills": fills,
                    "orphan_fills": orphan_fills,
                    "reconciliation_discrepancies": reconciliation_discrepancies,
                    "unresolved_blocking_reconciliation_discrepancies": (unresolved_reconciliation),
                    "unresolved_incidents": unresolved_incidents,
                    "hard_stops": hard_stops,
                    "active_latches_at_close": sorted(active_latches),
                    "duplicate_decisions": duplicate_decisions,
                    "unresolved_data_gaps": unresolved_gaps,
                    "heartbeat_continuity": heartbeat_continuity,
                    "heartbeat_counts_by_run": heartbeat_counts,
                    "largest_heartbeat_gap_seconds": max_heartbeat_gap,
                    "clean_shutdown": clean_shutdown,
                    "daily_report_present": daily_report_present,
                    "controlled_restart_candidate": restart_pass,
                    "controlled_restart_test": False,
                    "startup_preflight_complete": startup_complete,
                    "startup_preflight_by_run": startup_by_run,
                }
            )
            for name, passed, detail in (
                (
                    "feed_provenance",
                    configured_feed_recorded,
                    "every matching run must record the configured feed",
                ),
                (
                    "startup_preflight",
                    startup_complete,
                    "feed, assets, history, and authenticated stream events are required",
                ),
                (
                    "one_fresh_decision",
                    len(decisions) == 1 and freshness_at_decision,
                    "exactly one fresh scheduled observer decision is required",
                ),
                (
                    "zero_broker_mutations",
                    submissions == cancellations == fills == orphan_fills == 0,
                    "submissions, cancellations, and fills must all equal zero",
                ),
                (
                    "no_duplicate_decision",
                    duplicate_decisions == 0 and len(decisions) <= 1,
                    "at most one logical strategy decision may exist for the session",
                ),
                (
                    "no_unresolved_blocking_state",
                    unresolved_reconciliation
                    == unresolved_incidents
                    == unresolved_gaps
                    == hard_stops
                    == 0
                    and not active_latches,
                    "no unresolved state, hard stop, or active latch may remain",
                ),
                (
                    "heartbeat_continuity",
                    heartbeat_continuity,
                    "heartbeats must exist and remain within the configured continuity bound",
                ),
                (
                    "clean_shutdown",
                    clean_shutdown,
                    "every run covering a stopped session must have a durable shutdown reason",
                ),
                (
                    "daily_report",
                    daily_report_present,
                    "the post-close daily report must be generated",
                ),
            ):
                _add_session_check(checks, name, passed, detail)
            report["real_market_data"] = bool(
                provider == "alpaca" and required_data and startup_complete
            )
    except (OSError, sqlite3.DatabaseError) as exc:
        _add_session_check(
            checks,
            "read_only_database_inspection",
            False,
            "the database could not be inspected read-only",
            error_type=type(exc).__name__,
        )
        report.update(_empty_session_counts(symbols))

    report["status"] = (
        "PASS"
        if report.get("real_market_data")
        and checks
        and all(item["status"] == "PASS" for item in checks)
        else "FAIL"
    )
    safe_report = _json_safe(report)
    if not isinstance(safe_report, dict):  # pragma: no cover - mapping invariant
        raise TypeError("Observer session report did not serialize to an object")
    return safe_report


def _empty_session_counts(symbols: Iterable[str]) -> dict[str, Any]:
    return {
        "run_ids": [],
        "market_calendar_status": "UNVERIFIED",
        "bars_received_by_symbol": {str(symbol): 0 for symbol in symbols},
        "bar_span_minutes_by_symbol": {str(symbol): 0.0 for symbol in symbols},
        "largest_data_gap_minutes": None,
        "last_bar_time": None,
        "freshness_at_decision": False,
        "strategy_decisions": 0,
        "hypothetical_order_intents": 0,
        "broker_submissions": 0,
        "broker_cancellations": 0,
        "broker_fills": 0,
        "orphan_fills": 0,
        "reconciliation_discrepancies": 0,
        "unresolved_blocking_reconciliation_discrepancies": 0,
        "unresolved_incidents": 0,
        "hard_stops": 0,
        "active_latches_at_close": [],
        "duplicate_decisions": 0,
        "unresolved_data_gaps": 0,
        "heartbeat_continuity": False,
        "clean_shutdown": False,
        "daily_report_present": False,
        "controlled_restart_test": False,
        "controlled_restart_candidate": False,
        "startup_preflight_complete": False,
    }


def write_evidence_json(report: Mapping[str, Any], path: str | Path) -> Path:
    """Write a deterministic JSON evidence record outside the primary database."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = dict(report)
    record["evidence_sha256"] = evidence_content_hash(record)
    destination.write_text(
        json.dumps(_json_safe(record), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def evidence_markdown(report: Mapping[str, Any], *, title: str) -> str:
    """Render a compact, non-promotional evidence report."""

    lines = [
        f"# {title}",
        "",
        f"> **{PAPER_TRADING_BANNER}**",
        "",
        f"Status: **{report.get('status', 'INCOMPLETE')}**  ",
        f"Generated: `{report.get('generated_at', report.get('checked_at', 'unknown'))}`",
        "",
    ]
    for field in (
        "session_date",
        "provider",
        "feed",
        "market_state",
        "market_calendar_status",
        "runtime_duration_seconds",
        "bars_expected",
        "bars_received_by_symbol",
        "bar_sources_by_symbol",
        "largest_data_gap_minutes",
        "last_bar_time",
        "stream_health",
        "freshness_at_decision",
        "strategy_decisions",
        "hypothetical_order_intents",
        "signal_cutoff",
        "execution_timestamp",
        "strategy_outputs",
        "regime",
        "proposed_portfolio",
        "risk_interventions",
        "final_target",
        "broker_submissions",
        "broker_cancellations",
        "broker_fills",
        "orphan_fills",
        "position_mutations",
        "account_mutations",
        "order_mutations",
        "before_account_snapshot",
        "after_account_snapshot",
        "database_integrity",
        "reconciliation_result",
        "reconciliation_discrepancies",
        "unresolved_blocking_reconciliation_discrepancies",
        "unresolved_incidents",
        "hard_stops",
        "active_latches_at_close",
        "duplicate_decisions",
        "unresolved_data_gaps",
        "heartbeat_continuity",
        "largest_heartbeat_gap_seconds",
        "clean_shutdown",
        "daily_report_present",
        "incidents",
        "qualifies_as_completed_observer_session",
        "controlled_restart_test",
    ):
        if field in report:
            value = report[field]
            if isinstance(value, (Mapping, list, tuple)):
                rendered = json.dumps(_json_safe(value), indent=2, sort_keys=True)
                lines.extend(
                    (
                        "",
                        f"### {field.replace('_', ' ').title()}",
                        "",
                        "```json",
                        rendered,
                        "```",
                    )
                )
            else:
                lines.append(f"- {field.replace('_', ' ').title()}: `{value}`")
    checks = report.get("checks")
    if isinstance(checks, list):
        lines.extend(("", "## Checks", ""))
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            lines.append(
                f"- **{check.get('status', 'INCOMPLETE')}** "
                f"`{check.get('group', 'session')}.{check.get('name', 'unknown')}` — "
                f"{check.get('detail', '')}"
            )
    lines.extend(
        (
            "",
            "This is operational observer evidence, not investment-performance evidence.",
            "",
        )
    )
    return "\n".join(lines)


def write_evidence_markdown(report: Mapping[str, Any], path: str | Path, *, title: str) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(evidence_markdown(report, title=title), encoding="utf-8")
    return destination


def _load_evidence_records_with_errors(
    directory: str | Path,
    *,
    kind: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        return [], []
    records: list[dict[str, Any]] = []
    corrupt: list[str] = []
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            corrupt.append(str(path))
            continue
        if not isinstance(value, Mapping) or value.get("kind") != kind:
            corrupt.append(str(path))
            continue
        record = dict(value)
        record["_evidence_path"] = str(path)
        records.append(record)
    return records, corrupt


def load_evidence_records(directory: str | Path, *, kind: str) -> list[dict[str, Any]]:
    records, _ = _load_evidence_records_with_errors(directory, kind=kind)
    return records


def _strict_pass_checks(record: Mapping[str, Any], required: set[str]) -> bool:
    checks = record.get("checks")
    if not isinstance(checks, list):
        return False
    by_name = {
        str(check.get("name")): str(check.get("status"))
        for check in checks
        if isinstance(check, Mapping)
    }
    return required <= set(by_name) and all(by_name[name] == "PASS" for name in required)


def _valid_session_evidence(
    record: Mapping[str, Any],
    *,
    configuration: Any,
    database_path: Path,
) -> bool:
    required_checks = {
        "submission_disabled",
        "paper_only",
        "alpaca_provider",
        "database_integrity",
        "observer_schema",
        "observer_runs_present",
        "completed_regular_market_session",
        "full_calendar_runtime",
        "required_real_stream_data",
        "feed_provenance",
        "startup_preflight",
        "one_fresh_decision",
        "zero_broker_mutations",
        "no_duplicate_decision",
        "no_unresolved_blocking_state",
        "heartbeat_continuity",
        "clean_shutdown",
        "daily_report",
    }
    try:
        session_date = date.fromisoformat(str(record.get("session_date")))
        recorded_database = Path(str(record.get("database_path"))).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return False
    if not (
        record.get("schema_version") == OBSERVER_EVIDENCE_SCHEMA_VERSION
        and record.get("status") == "PASS"
        and record.get("real_market_data") is True
        and str(record.get("configuration_hash")) == configuration.configuration_hash
        and recorded_database == database_path.resolve()
        and _evidence_hash_valid(record)
        and _strict_pass_checks(record, required_checks)
        and _zero_integer_fields(
            record,
            (
                "broker_submissions",
                "broker_cancellations",
                "broker_fills",
                "orphan_fills",
                "duplicate_decisions",
                "unresolved_incidents",
                "unresolved_blocking_reconciliation_discrepancies",
            ),
        )
    ):
        return False
    try:
        minimum_session_minutes = max(300, int(record.get("minimum_session_minutes", 300)))
    except (TypeError, ValueError, OverflowError):
        return False
    regenerated = audit_observer_session(
        configuration,
        database_path=database_path,
        session_date=session_date,
        controlled_restart=record.get("controlled_restart_requested") is True,
        minimum_session_minutes=minimum_session_minutes,
    )
    if regenerated.get("status") != "PASS":
        return False

    # A self-hash is only transport-integrity evidence: a forged record can be
    # re-hashed.  Accept a session only when every substantive claim is the
    # canonical value regenerated read-only from the official database.
    excluded = {"_evidence_path", "evidence_sha256", "generated_at"}
    recorded_payload = {
        str(key): _json_safe(value) for key, value in record.items() if key not in excluded
    }
    regenerated_payload = {
        str(key): _json_safe(value) for key, value in regenerated.items() if key not in excluded
    }
    return recorded_payload == regenerated_payload


def _valid_dry_run_evidence(
    record: Mapping[str, Any],
    *,
    observer_configuration: Any,
    observer_database_path: Path,
) -> bool:
    required_checks = {
        "cli_exit",
        "primary_database_unchanged",
        "zero_broker_mutations",
        "decision_receipt_complete",
        "risk_decision_complete",
        "database_integrity",
        "startup_preflight",
        "new_run_identity",
    }
    try:
        recorded_date = date.fromisoformat(str(record.get("session_date")))
        path = Path(str(record.get("database_path"))).expanduser().resolve()
        primary = Path(str(record.get("primary_database_path"))).expanduser().resolve()
        output = Path(str(record.get("output_directory"))).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return False
    expected_config = derive_real_data_dry_run_config(
        observer_configuration,
        database_path=path,
        output_directory=output,
    )
    derived_hash = str(record.get("dry_run_configuration_hash") or "")
    run_id = str(record.get("run_id") or "")
    if not (
        record.get("schema_version") == OBSERVER_EVIDENCE_SCHEMA_VERSION
        and record.get("status") == "PASS"
        and record.get("real_market_data") is True
        and str(record.get("configuration_hash")) == observer_configuration.configuration_hash
        and derived_hash == expected_config.configuration_hash
        and primary == observer_database_path.resolve()
        and path != primary
        and str(record.get("provider")).lower() == "alpaca"
        and str(record.get("feed")).upper() == str(observer_configuration.market_data.feed).upper()
        and expected_config.execution.paper_only is True
        and expected_config.execution.paper_order_submission_enabled is False
        and run_id
        and path.is_file()
        and _evidence_hash_valid(record)
        and _strict_pass_checks(record, required_checks)
        and record.get("decision_receipt_complete") is True
        and record.get("risk_decision_complete") is True
        and _zero_integer_fields(
            record,
            (
                "broker_submissions",
                "broker_cancellations",
                "broker_fills",
                "position_mutations",
                "account_mutations",
                "order_mutations",
                "orphan_fills",
            ),
        )
    ):
        return False
    if _contains_raw_account_identifier(record):
        return False
    before = record.get("before_account_snapshot")
    after = record.get("after_account_snapshot")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False

    def account_projection(snapshot: Mapping[str, Any]) -> tuple[str, str, bool, str]:
        return (
            str(snapshot.get("account_id_hash") or ""),
            str(snapshot.get("account_status") or ""),
            bool(snapshot.get("trading_blocked")),
            str(snapshot.get("cash") or ""),
        )

    def position_projection(snapshot: Mapping[str, Any]) -> dict[str, str]:
        positions = snapshot.get("positions")
        if not isinstance(positions, Mapping):
            return {}
        return {
            str(symbol): str(values.get("quantity"))
            for symbol, values in positions.items()
            if isinstance(values, Mapping)
        }

    def order_projection(snapshot: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
        orders = snapshot.get("orders")
        if not isinstance(orders, Mapping):
            return {}
        return {
            str(client_id): (
                str(values.get("status")),
                str(values.get("filled_quantity")),
            )
            for client_id, values in orders.items()
            if isinstance(values, Mapping)
        }

    expected_account_mutations = int(account_projection(before) != account_projection(after))
    expected_position_mutations = int(position_projection(before) != position_projection(after))
    expected_order_mutations = int(order_projection(before) != order_projection(after))
    primary_before = str(record.get("primary_database_sha256_before") or "")
    primary_after = str(record.get("primary_database_sha256_after") or "")
    if not (
        len(primary_before) == len(primary_after) == 64
        and primary_before == primary_after
        and _zero_integer_fields(
            record,
            ("account_mutations", "position_mutations", "order_mutations"),
        )
        and expected_account_mutations
        == expected_position_mutations
        == expected_order_mutations
        == 0
    ):
        return False
    try:
        with _read_only_database(path) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            run = connection.execute(
                "SELECT started_at, ended_at, market_data_feed FROM application_runs "
                "WHERE run_id = ? "
                "AND mode = 'paper_once' AND configuration_hash = ?",
                (run_id, derived_hash),
            ).fetchone()
            if integrity is None or str(integrity[0]) != "ok" or run is None:
                return False
            started = _timestamp(run["started_at"])
            ended = _timestamp(run["ended_at"])
            recorded_started = _timestamp(record.get("application_run_started_at"))
            recorded_ended = _timestamp(record.get("application_run_ended_at"))
            if not (
                started is not None
                and ended is not None
                and ended >= started
                and started.astimezone(NEW_YORK).date() == recorded_date
                and recorded_started == started
                and recorded_ended == ended
                and str(run["market_data_feed"] or "").upper()
                == str(observer_configuration.market_data.feed).upper()
            ):
                return False
            snapshot_row = connection.execute(
                "SELECT configuration FROM configuration_snapshots WHERE run_id = ? "
                "AND configuration_hash = ?",
                (run_id, derived_hash),
            ).fetchone()
            config_snapshot = _json_object(
                None if snapshot_row is None else snapshot_row["configuration"]
            )
            project_snapshot = config_snapshot.get("project")
            execution_snapshot = config_snapshot.get("execution")
            market_snapshot = config_snapshot.get("market_data")
            if not (
                isinstance(project_snapshot, Mapping)
                and Path(str(project_snapshot.get("database_path"))).expanduser().resolve() == path
                and Path(str(project_snapshot.get("output_directory"))).expanduser().resolve()
                == output
                and isinstance(execution_snapshot, Mapping)
                and execution_snapshot.get("paper_only") is True
                and execution_snapshot.get("paper_order_submission_enabled") is False
                and isinstance(market_snapshot, Mapping)
                and str(market_snapshot.get("provider")).lower() == "alpaca"
                and str(market_snapshot.get("feed")).upper()
                == str(observer_configuration.market_data.feed).upper()
            ):
                return False
            decisions = connection.execute(
                "SELECT decision_id, session_date FROM rebalance_decisions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            if (
                len(decisions) != 1
                or str(decisions[0]["session_date"]) != recorded_date.isoformat()
            ):
                return False
            decision_id = str(decisions[0]["decision_id"])
            fact_payloads: dict[str, dict[str, Any]] = {}
            for table in (
                "strategy_signals",
                "regime_states",
                "allocation_results",
                "risk_decisions",
            ):
                rows = connection.execute(
                    f"SELECT payload FROM {table} WHERE run_id = ? AND decision_id = ?",
                    (run_id, decision_id),
                ).fetchall()
                if len(rows) != 1:
                    return False
                fact_payloads[table] = _json_object(rows[0]["payload"])
            receipt_rows = connection.execute(
                "SELECT payload, receipt_hash, created_at FROM decision_receipts "
                "WHERE run_id = ? AND decision_id = ?",
                (run_id, decision_id),
            ).fetchall()
            if len(receipt_rows) != 1:
                return False
            receipt = _json_object(receipt_rows[0]["payload"])
            receipt_at = _timestamp(receipt_rows[0]["created_at"])
            actual_at = _timestamp(receipt.get("actual_at"))
            from adaptive_trader.persistence import canonical_json

            receipt_hash_valid = (
                str(receipt_rows[0]["receipt_hash"])
                == hashlib.sha256(canonical_json(receipt).encode()).hexdigest()
            )
            strategy_payload = fact_payloads["strategy_signals"]
            regime_payload = fact_payloads["regime_states"]
            allocation_payload = fact_payloads["allocation_results"]
            risk_payload = fact_payloads["risk_decisions"]
            strategy_outputs = strategy_payload.get("strategy_outputs")
            allocation = allocation_payload.get("allocation")
            raw_receipt_metadata = receipt.get("decision_metadata")
            receipt_metadata: Mapping[str, Any] = (
                raw_receipt_metadata if isinstance(raw_receipt_metadata, Mapping) else {}
            )
            receipt_complete = bool(
                receipt_hash_valid
                and receipt.get("decision_id") == decision_id
                and receipt.get("run_id") == run_id
                and receipt.get("configuration_hash") == derived_hash
                and receipt.get("execution_phase") == "hypothetical_not_submitted"
                and str(receipt.get("feed") or "").upper()
                == str(observer_configuration.market_data.feed).upper()
                and receipt.get("signal_cutoff") is not None
                and isinstance(raw_receipt_metadata, Mapping)
                and isinstance(receipt_metadata.get("strategy_outputs"), Mapping)
                and bool(receipt_metadata.get("strategy_outputs"))
                and receipt.get("regime") is not None
                and isinstance(receipt.get("allocation"), Mapping)
                and isinstance(receipt.get("final_target"), Mapping)
                and receipt_at is not None
                and actual_at is not None
                and started <= receipt_at <= ended
                and started <= actual_at <= ended
            )
            risk_complete = bool(
                isinstance(strategy_outputs, Mapping)
                and bool(strategy_outputs)
                and regime_payload.get("regime") is not None
                and isinstance(allocation, Mapping)
                and isinstance(allocation_payload.get("final_target"), Mapping)
                and isinstance(risk_payload.get("risk_decision"), Mapping)
                and isinstance(risk_payload.get("operational_risk_state"), Mapping)
                and isinstance(risk_payload.get("final_target"), Mapping)
            )
            orders = _query_count(
                connection,
                "SELECT COUNT(*) FROM broker_orders WHERE run_id = ?",
                (run_id,),
            )
            hypothetical_intents = _query_count(
                connection,
                "SELECT COUNT(*) FROM order_intents WHERE decision_id = ? "
                "AND reason LIKE 'hypothetical_%'",
                (decision_id,),
            )
            cancellations = _query_count(
                connection,
                "SELECT COUNT(*) FROM stream_events WHERE run_id = ? "
                "AND lower(event_type) LIKE '%cancel%'",
                (run_id,),
            )
            event_rows = connection.execute(
                "SELECT event_type, symbol, payload FROM stream_events WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            event_types = {str(row["event_type"]) for row in event_rows}
            required_events = {
                "paper_account_verified",
                "feed_entitlement_confirmed",
                "asset_validation_confirmed",
                "history_preflight_confirmed",
                "connected",
            }
            feed_events = [
                _json_object(row["payload"])
                for row in event_rows
                if row["event_type"] == "feed_entitlement_confirmed"
            ]
            explicit_no_fallback = any(
                str(payload.get("feed") or "").upper()
                == str(observer_configuration.market_data.feed).upper()
                and payload.get("fallback_used") is False
                for payload in feed_events
            )
            order_events = _query_count(
                connection,
                "SELECT COUNT(*) FROM order_events WHERE decision_id = ? "
                "OR (created_at >= ? AND created_at <= ?)",
                (decision_id, started.isoformat(), ended.isoformat()),
            )
            fills = _query_count(
                connection,
                "SELECT COUNT(*) FROM fill_events WHERE decision_id = ? "
                "OR (created_at >= ? AND created_at <= ?)",
                (decision_id, started.isoformat(), ended.isoformat()),
            )
            orphan_fills = _query_count(
                connection,
                "SELECT COUNT(*) FROM fill_events f LEFT JOIN broker_orders o "
                "ON o.client_order_id = f.client_order_id WHERE o.client_order_id IS NULL "
                "AND (f.decision_id = ? OR (f.created_at >= ? AND f.created_at <= ?))",
                (decision_id, started.isoformat(), ended.isoformat()),
            )
            account_events = [
                _json_object(row["payload"])
                for row in event_rows
                if row["event_type"] == "paper_account_verified"
            ]
            account_snapshot_rows = connection.execute(
                "SELECT snapshot_id, account_id_hash, status, trading_blocked "
                "FROM account_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            account_snapshots = {str(row["snapshot_id"]): row for row in account_snapshot_rows}
            paper_account_verified = False
            for payload in account_events:
                account_snapshot = account_snapshots.get(
                    str(payload.get("account_snapshot_id") or "")
                )
                if (
                    payload.get("adapter") == "AlpacaPaperBroker"
                    and payload.get("paper_only") is True
                    and str(payload.get("account_status") or "").upper() == "ACTIVE"
                    and payload.get("trading_blocked") is False
                    and account_snapshot is not None
                    and str(account_snapshot["status"]).upper() == "ACTIVE"
                    and not bool(account_snapshot["trading_blocked"])
                    and str(account_snapshot["account_id_hash"])
                    == str(before.get("account_id_hash") or "")
                ):
                    paper_account_verified = True
                    break
            bar_keys = {
                (
                    str(row["symbol"] or "").upper(),
                    str(_json_object(row["payload"]).get("start") or ""),
                    str(_json_object(row["payload"]).get("feed") or "").upper(),
                )
                for row in event_rows
                if str(row["event_type"]).startswith("bar_")
            }
            real_symbols: set[str] = set()
            bad_bar_provenance = False
            for row in connection.execute(
                "SELECT symbol, start_at, feed, source FROM market_bars"
            ).fetchall():
                bar_started = _timestamp(row["start_at"])
                key = (
                    str(row["symbol"]).upper(),
                    "" if bar_started is None else bar_started.isoformat(),
                    str(row["feed"]).upper(),
                )
                if key not in bar_keys:
                    continue
                if (
                    str(row["source"]) not in DRY_RUN_REAL_SOURCES
                    or str(row["feed"]).upper()
                    != str(observer_configuration.market_data.feed).upper()
                ):
                    bad_bar_provenance = True
                else:
                    real_symbols.add(str(row["symbol"]).upper())
            expected_claims = {
                "signal_cutoff": receipt.get("signal_cutoff"),
                "execution_timestamp": receipt.get("actual_at"),
                "strategy_outputs": receipt_metadata.get("strategy_outputs"),
                "regime": receipt_metadata.get("regime"),
                "proposed_portfolio": receipt_metadata.get("allocation"),
                "risk_interventions": receipt_metadata.get("risk_actions"),
                "final_target": receipt.get("final_target"),
                "hypothetical_order_intents": hypothetical_intents,
            }
            displayed_claims_match = all(
                _json_safe(record.get(field)) == _json_safe(expected)
                for field, expected in expected_claims.items()
            )
            fact_payloads_match_receipt = bool(
                _json_safe(strategy_outputs) == _json_safe(receipt_metadata.get("strategy_outputs"))
                and _json_safe(regime_payload.get("regime"))
                == _json_safe(receipt_metadata.get("regime"))
                and _json_safe(allocation) == _json_safe(receipt_metadata.get("allocation"))
                and _json_safe(allocation_payload.get("final_target"))
                == _json_safe(receipt.get("final_target"))
                and _json_safe(risk_payload.get("final_target"))
                == _json_safe(receipt.get("final_target"))
            )
            return bool(
                receipt_complete
                and risk_complete
                and displayed_claims_match
                and fact_payloads_match_receipt
                and record.get("database_integrity") == "ok"
                and record.get("startup_preflight_complete") is True
                and record.get("new_run_identity") is True
                and sorted(str(value).upper() for value in record.get("real_stream_symbols", ()))
                == sorted(real_symbols)
                and orders == cancellations == order_events == fills == orphan_fills == 0
                and required_events <= event_types
                and explicit_no_fallback
                and paper_account_verified
                and set(str(value).upper() for value in observer_configuration.data.tickers)
                <= real_symbols
                and not bad_bar_provenance
            )
    except (OSError, sqlite3.DatabaseError):
        return False


def _valid_restart_evidence(
    record: Mapping[str, Any],
    *,
    configuration_hash: str,
    database_path: Path,
    valid_session_dates: set[str],
) -> bool:
    try:
        recorded_database = Path(str(record.get("database_path"))).expanduser().resolve()
        session_date = str(record.get("session_date"))
        date.fromisoformat(session_date)
    except (TypeError, ValueError, OSError):
        return False
    run_ids = tuple(str(value) for value in record.get("run_ids", ()))
    drill_id = str(record.get("drill_id") or "")
    if not (
        record.get("schema_version") == OBSERVER_EVIDENCE_SCHEMA_VERSION
        and record.get("kind") == "observer_restart_drill"
        and record.get("status") == "PASS"
        and str(record.get("configuration_hash")) == configuration_hash
        and recorded_database == database_path.resolve()
        and session_date in valid_session_dates
        and len(set(run_ids)) >= 2
        and drill_id
        and _evidence_hash_valid(record)
    ):
        return False
    try:
        with _read_only_database(database_path) as connection:
            clause = _placeholders(run_ids)
            rows = connection.execute(
                f"SELECT run_id, event_type, payload FROM stream_events "
                f"WHERE run_id IN ({clause}) AND event_type IN "
                "('controlled_restart_drill_started', 'controlled_restart_drill_completed')",
                run_ids,
            ).fetchall()
            observed = {
                (str(row["run_id"]), str(row["event_type"]))
                for row in rows
                if str(_json_object(row["payload"]).get("drill_id")) == drill_id
            }
            return bool(
                any(event == "controlled_restart_drill_started" for _, event in observed)
                and any(event == "controlled_restart_drill_completed" for _, event in observed)
            )
    except (OSError, sqlite3.DatabaseError):
        return False


def _dry_run_evidence_records_mutation(
    record: Mapping[str, Any],
    *,
    observer_configuration_hash: str,
    observer_database_path: Path,
) -> bool:
    """Return true when a current-config dry record or linked run proves mutation."""

    if str(record.get("configuration_hash")) != observer_configuration_hash:
        return False
    try:
        primary = Path(str(record.get("primary_database_path"))).expanduser().resolve()
    except (TypeError, OSError):
        return False
    if primary != observer_database_path.resolve():
        return False
    for field in (
        "broker_submissions",
        "broker_cancellations",
        "broker_fills",
        "orphan_fills",
        "position_mutations",
        "account_mutations",
        "order_mutations",
    ):
        try:
            if int(record.get(field, 0)) > 0:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    before_hash = str(record.get("primary_database_sha256_before") or "")
    after_hash = str(record.get("primary_database_sha256_after") or "")
    if before_hash and after_hash and before_hash != after_hash:
        return True
    before = record.get("before_account_snapshot")
    after = record.get("after_account_snapshot")
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        stable_account_fields = (
            "account_id_hash",
            "account_status",
            "trading_blocked",
            "cash",
        )
        if any(before.get(field) != after.get(field) for field in stable_account_fields):
            return True
        for collection, compared_fields in (
            ("positions", ("quantity",)),
            ("orders", ("status", "filled_quantity")),
        ):
            left = before.get(collection)
            right = after.get(collection)
            if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                continue
            left_projection = {
                str(key): tuple(value.get(field) for field in compared_fields)
                for key, value in left.items()
                if isinstance(value, Mapping)
            }
            right_projection = {
                str(key): tuple(value.get(field) for field in compared_fields)
                for key, value in right.items()
                if isinstance(value, Mapping)
            }
            if left_projection != right_projection:
                return True
    run_id = str(record.get("run_id") or "")
    try:
        database = Path(str(record.get("database_path"))).expanduser().resolve()
    except (TypeError, OSError):
        return False
    if not run_id or not database.is_file():
        return False
    try:
        with _read_only_database(database) as connection:
            decisions = tuple(
                str(row["decision_id"])
                for row in connection.execute(
                    "SELECT decision_id FROM rebalance_decisions WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            )
            orders = _query_count(
                connection,
                "SELECT COUNT(*) FROM broker_orders WHERE run_id = ?",
                (run_id,),
            )
            cancellations = _query_count(
                connection,
                "SELECT COUNT(*) FROM stream_events WHERE run_id = ? "
                "AND lower(event_type) LIKE '%cancel%'",
                (run_id,),
            )
            fills = (
                0
                if not decisions
                else _query_count(
                    connection,
                    f"SELECT COUNT(*) FROM fill_events WHERE decision_id IN "
                    f"({_placeholders(decisions)})",
                    decisions,
                )
            )
            return bool(orders or cancellations or fills)
    except (OSError, sqlite3.DatabaseError):
        return False


def summarize_observer_evidence(
    *,
    evidence_directory: str | Path,
    configuration_hash: str | None = None,
    configuration: Any | None = None,
    database_path: str | Path | None = None,
) -> dict[str, Any]:
    """Summarize only strictly validated, database-linked evidence."""

    root = Path(evidence_directory).expanduser().resolve()
    session_records, corrupt_sessions = _load_evidence_records_with_errors(
        root / "sessions", kind="observer_session"
    )
    dry_records, corrupt_dry_runs = _load_evidence_records_with_errors(
        root / "dry_runs", kind="real_data_dry_run"
    )
    restart_records, corrupt_restarts = _load_evidence_records_with_errors(
        root / "restart_drills", kind="observer_restart_drill"
    )
    resolved_hash = (
        str(configuration.configuration_hash) if configuration is not None else configuration_hash
    )
    database = None if database_path is None else Path(database_path).expanduser().resolve()
    valid_session_candidates = (
        []
        if configuration is None or database is None
        else [
            record
            for record in session_records
            if _valid_session_evidence(
                record,
                configuration=configuration,
                database_path=database,
            )
        ]
    )
    valid_dry_candidates = (
        []
        if configuration is None or database is None
        else [
            record
            for record in dry_records
            if _valid_dry_run_evidence(
                record,
                observer_configuration=configuration,
                observer_database_path=database,
            )
        ]
    )
    session_by_date = {
        str(record.get("session_date")): record for record in valid_session_candidates
    }
    dry_by_run = {str(record.get("run_id")): record for record in valid_dry_candidates}
    dry_by_date: dict[str, dict[str, Any]] = {}
    for record in dry_by_run.values():
        dry_by_date.setdefault(str(record.get("session_date")), record)
    valid_sessions = list(session_by_date.values())
    valid_dry_runs = list(dry_by_date.values())
    session_dates = sorted(session_by_date)
    dry_dates = sorted(dry_by_date)
    accepted_session_run_ids = sorted(
        {str(run_id) for record in valid_sessions for run_id in record.get("run_ids", ())}
    )
    accepted_dry_run_ids = sorted(str(record.get("run_id")) for record in valid_dry_runs)
    return {
        "schema_version": OBSERVER_EVIDENCE_SCHEMA_VERSION,
        "kind": "observer_evidence_summary",
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration_hash": configuration_hash,
        "distinct_pass_observer_sessions": len(session_dates),
        "pass_observer_session_dates": session_dates,
        "accepted_session_run_ids": accepted_session_run_ids,
        "controlled_restart_pass": bool(
            resolved_hash is not None
            and database is not None
            and any(
                _valid_restart_evidence(
                    record,
                    configuration_hash=resolved_hash,
                    database_path=database,
                    valid_session_dates=set(session_dates),
                )
                for record in restart_records
            )
        ),
        "distinct_pass_real_data_dry_runs": len(dry_dates),
        "pass_real_data_dry_run_dates": dry_dates,
        "accepted_real_data_dry_run_ids": accepted_dry_run_ids,
        "broker_submissions": sum(
            int(record.get("broker_submissions", 0)) for record in valid_sessions
        )
        + sum(int(record.get("broker_submissions", 0)) for record in valid_dry_runs),
        "broker_cancellations": sum(
            int(record.get("broker_cancellations", 0)) for record in valid_sessions
        )
        + sum(int(record.get("broker_cancellations", 0)) for record in valid_dry_runs),
        "broker_fills": sum(int(record.get("broker_fills", 0)) for record in valid_sessions)
        + sum(int(record.get("broker_fills", 0)) for record in valid_dry_runs),
        "duplicate_decisions": sum(
            int(record.get("duplicate_decisions", 0)) for record in valid_sessions
        ),
        "unresolved_blocking_incidents": sum(
            int(record.get("unresolved_incidents", 0))
            + int(record.get("unresolved_blocking_reconciliation_discrepancies", 0))
            for record in valid_sessions
        ),
        "session_record_files_seen": len(session_records),
        "dry_run_record_files_seen": len(dry_records),
        "restart_record_files_seen": len(restart_records),
        "corrupt_session_record_files": corrupt_sessions,
        "corrupt_dry_run_record_files": corrupt_dry_runs,
        "corrupt_restart_record_files": corrupt_restarts,
        "invalid_pass_session_records": sum(
            record.get("status") == "PASS" and record not in valid_session_candidates
            for record in session_records
        ),
        "invalid_pass_dry_run_records": sum(
            record.get("status") == "PASS" and record not in valid_dry_candidates
            for record in dry_records
        ),
        "invalid_pass_restart_records": sum(
            record.get("status") == "PASS"
            and not (
                resolved_hash is not None
                and database is not None
                and _valid_restart_evidence(
                    record,
                    configuration_hash=resolved_hash,
                    database_path=database,
                    valid_session_dates=set(session_dates),
                )
            )
            for record in restart_records
        ),
    }


def _read_junit(path: Path) -> tuple[bool, dict[str, int]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False, {}
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    try:
        totals = {
            key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
    except (TypeError, ValueError, OverflowError):
        return False, {}
    if any(value < 0 for value in totals.values()):
        return False, {}
    return bool(totals["tests"] and not totals["failures"] and not totals["errors"]), totals


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_quality_source_hash(root: Path) -> str:
    digest = hashlib.sha256()
    candidates: set[Path] = set()
    for directory, suffixes in (
        (root / "src", {".py"}),
        (root / "tests", {".py"}),
        (root / "scripts", {".py", ".sh"}),
    ):
        if directory.is_dir():
            candidates.update(path for path in directory.rglob("*") if path.suffix in suffixes)
    candidates.update(
        {
            root / "app.py",
            root / "pyproject.toml",
            root / "uv.lock",
            root / "Makefile",
            root / "Dockerfile",
            root / "docker-compose.yml",
            root / ".env.example",
            root / ".gitignore",
            *(
                root / "configs" / name
                for name in ("backtest.yaml", "paper.yaml", "observer.yaml", "replay.yaml")
            ),
            *(
                root / "docs" / name
                for name in (
                    "architecture.md",
                    "methodology.md",
                    "data_dictionary.md",
                    "live_paper_runbook.md",
                    "incident_response.md",
                )
            ),
        }
    )
    for path in sorted(candidates):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _quality_evidence(
    root: Path,
    *,
    configuration_hash: str,
    evidence_directory: str | Path | None = None,
) -> tuple[str, dict[str, Any]]:
    evidence_root = (
        root / "outputs" / "observer_evidence"
        if evidence_directory is None
        else Path(evidence_directory).expanduser().resolve()
    )
    quality_root = evidence_root / QUALITY_EVIDENCE_SUBDIRECTORY
    path = quality_root / "phase2_final_quality.json"
    junit = quality_root / "phase2_final_tests.xml"
    details: dict[str, Any] = {"path": path}
    if not path.is_file():
        return "INCOMPLETE", details
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        details["reason"] = "unparseable_quality_evidence"
        return "FAIL", details
    if not isinstance(raw, Mapping):
        details["reason"] = "quality_evidence_not_an_object"
        return "FAIL", details
    checks = raw.get("checks")
    if isinstance(checks, Mapping):
        check_statuses = {str(key): str(value) for key, value in checks.items()}
    elif isinstance(checks, list):
        check_statuses = {
            str(item.get("name")): str(item.get("status"))
            for item in checks
            if isinstance(item, Mapping)
        }
    else:
        check_statuses = {}
    required = {"ruff", "mypy", "safety_static_checks", "config_validation"}
    junit_ok, totals = _read_junit(junit)
    expected_source_hash = _current_quality_source_hash(root)
    junit_hash = _file_sha256(junit) if junit.is_file() else None
    passed = bool(
        raw.get("schema_version") == 1
        and raw.get("kind") == "phase2_final_quality"
        and raw.get("status") == "PASS"
        and _evidence_hash_valid(raw)
        and str(raw.get("configuration_hash")) == configuration_hash
        and str(raw.get("source_hash")) == expected_source_hash
        and required <= set(check_statuses)
        and all(check_statuses[name] == "PASS" for name in required)
        and junit_ok
        and str(raw.get("junit_sha256")) == junit_hash
    )
    details.update(
        {
            "source_hash_matches": str(raw.get("source_hash")) == expected_source_hash,
            "junit_hash_matches": str(raw.get("junit_sha256")) == junit_hash,
            "junit_totals": totals,
            "check_statuses": check_statuses,
        }
    )
    return ("PASS" if passed else "FAIL"), details


def _hash_manifest_evidence(root: Path, path: Path) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {"path": path, "mismatches": []}
    if not path.is_file():
        return "INCOMPLETE", details
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        details["reason"] = "unparseable_manifest"
        return "FAIL", details
    if not isinstance(raw, Mapping) or not isinstance(raw.get("files"), list):
        details["reason"] = "invalid_manifest_schema"
        return "FAIL", details
    files = raw["files"]
    required_bundle = RESEARCH_BUNDLE_ROLES
    seen_paths: set[str] = set()
    seen_bundle: dict[str, str] = {}
    mismatches: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            mismatches.append("non_object_entry")
            continue
        relative = str(entry.get("path") or "")
        role = str(entry.get("role") or "")
        expected = str(entry.get("sha256") or "")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            mismatches.append(f"outside_root:{relative}")
            continue
        seen_paths.add(relative)
        seen_bundle[relative] = role
        try:
            actual = _file_sha256(candidate) if candidate.is_file() else None
        except OSError:
            actual = None
        if len(expected) != 64 or actual != expected:
            mismatches.append(relative or "missing_path")
    from adaptive_trader.config import load_config

    try:
        semantic_hash = load_config(root / "configs" / "backtest.yaml").configuration_hash
        file_count = int(raw.get("file_count", -1))
    except (OSError, TypeError, ValueError, OverflowError):
        details["reason"] = "invalid_manifest_metadata_or_configuration"
        return "FAIL", details
    recorded_semantic_hash = str(
        raw.get("real_market_config_semantic_sha256")
        or raw.get("configuration_semantic_hash")
        or ""
    )
    frozen_at = _timestamp(raw.get("frozen_at_utc"))
    generated_at = _timestamp(raw.get("manifest_generated_at_utc"))
    bundle_matches = seen_bundle == required_bundle
    passed = bool(
        raw.get("schema_version") == 1
        and raw.get("protocol_version") == "phase2-research-bundle-v1"
        and frozen_at is not None
        and generated_at is not None
        and generated_at >= frozen_at
        and str(raw.get("hash_algorithm")).upper() == "SHA-256"
        and file_count == len(files) == len(required_bundle)
        and len(seen_paths) == len(files)
        and bundle_matches
        and recorded_semantic_hash == semantic_hash
        and not mismatches
    )
    details.update(
        {
            "file_count": len(files),
            "missing_required_paths": sorted(set(required_bundle) - seen_paths),
            "unexpected_paths": sorted(seen_paths - set(required_bundle)),
            "bundle_roles_match": bundle_matches,
            "mismatches": mismatches,
            "semantic_hash_matches": recorded_semantic_hash == semantic_hash,
        }
    )
    return ("PASS" if passed else "FAIL"), details


def _dedicated_account_attestation(
    path: Path,
    *,
    configuration_hash: str,
    verified_account_hashes: set[str],
) -> tuple[bool, dict[str, Any]]:
    details: dict[str, Any] = {"path": path}
    if not path.is_file():
        return False, details
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        details["reason"] = "unparseable_attestation"
        return False, details
    if not isinstance(raw, Mapping):
        return False, details
    reviewed_at = _timestamp(raw.get("reviewed_at"))
    account_hash = str(raw.get("account_id_hash") or "")
    raw_identifier_present = _contains_raw_account_identifier(raw)
    passed = bool(
        raw.get("schema_version") == 1
        and raw.get("kind") == "dedicated_paper_account_attestation"
        and raw.get("dedicated_paper_account") is True
        and str(raw.get("configuration_hash")) == configuration_hash
        and len(verified_account_hashes) == 1
        and account_hash in verified_account_hashes
        and reviewed_at is not None
        and str(raw.get("reviewed_by") or "").strip()
        and not raw_identifier_present
        and _evidence_hash_valid(raw)
    )
    details.update(
        {
            "configuration_hash_matches": (
                str(raw.get("configuration_hash")) == configuration_hash
            ),
            "account_hash_matches_verified_snapshot": (account_hash in verified_account_hashes),
            "reviewed_at_valid": reviewed_at is not None,
            "raw_account_identifier_present": raw_identifier_present,
        }
    )
    return passed, details


def _contains_raw_account_identifier(value: Any) -> bool:
    """Detect forbidden complete account identifiers in generated evidence."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"account_id", "account_number", "paper_account_id"}:
                return True
            if _contains_raw_account_identifier(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_raw_account_identifier(item) for item in value)
    return False


def _backup_evidence(
    root: Path,
    *,
    database_path: Path,
    configuration_hash: str,
    accepted_run_ids: set[str],
    evidence_directory: str | Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    evidence_root = (
        root / "outputs" / "observer_evidence"
        if evidence_directory is None
        else Path(evidence_directory).expanduser().resolve()
    )
    manifest_path = evidence_root / "observer_database_backup.json"
    details: dict[str, Any] = {"manifest_path": manifest_path}
    if not manifest_path.is_file() or not accepted_run_ids:
        return False, details
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        details["reason"] = "unparseable_backup_manifest"
        return False, details
    if not isinstance(raw, Mapping):
        return False, details
    backup = Path(str(raw.get("backup_path") or "")).expanduser().resolve()
    claimed_runs = {str(value) for value in raw.get("accepted_session_run_ids", ())}
    try:
        with _read_only_database(backup) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            clause = _placeholders(tuple(accepted_run_ids))
            backed_up = {
                str(row["run_id"])
                for row in connection.execute(
                    f"SELECT run_id FROM application_runs WHERE run_id IN ({clause})",
                    tuple(accepted_run_ids),
                ).fetchall()
            }
    except (OSError, sqlite3.DatabaseError):
        return False, details
    actual_hash = _file_sha256(backup) if backup.is_file() else None
    primary_hash = _file_sha256(database_path) if database_path.is_file() else None
    passed = bool(
        raw.get("schema_version") == 1
        and raw.get("kind") == "observer_database_backup"
        and str(raw.get("configuration_hash")) == configuration_hash
        and Path(str(raw.get("primary_database_path") or "")).expanduser().resolve()
        == database_path.resolve()
        and backup != database_path.resolve()
        and integrity is not None
        and str(integrity[0]) == "ok"
        and accepted_run_ids <= backed_up
        and accepted_run_ids <= claimed_runs
        and str(raw.get("backup_sha256")) == actual_hash
        and str(raw.get("primary_database_sha256")) == primary_hash
        and _evidence_hash_valid(raw)
    )
    details.update(
        {
            "backup_path": backup,
            "backup_sha256": actual_hash,
            "primary_database_sha256": primary_hash,
            "accepted_run_ids": sorted(accepted_run_ids),
            "backed_up_run_ids": sorted(backed_up),
        }
    )
    return passed, details


def _markdown_all_checked(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if "- [ ]" in text or "- [x]" not in text.lower():
        return False

    def field(label: str) -> str:
        match = re.search(rf"^{re.escape(label)}:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
        return "" if match is None else match.group(1).strip().strip("*_`")

    review_status = field("Review status").upper()
    reviewer = field("- Reviewer name or club role")
    reviewed_at = field("- UTC review date")
    source_commit = field("- Source commit reviewed")
    manifest_review = field("- Configuration/protocol manifest reviewed")
    final_result = field("- Final manual review result").upper()
    placeholders = {"", "NOT ASSIGNED", "NOT COMPLETED", "INCOMPLETE", "TBD", "TODO"}
    reviewed_date_valid = bool(
        _timestamp(reviewed_at) is not None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", reviewed_at)
    )
    return bool(
        review_status in {"COMPLETE", "PASS"}
        and final_result in {"COMPLETE", "PASS"}
        and reviewer.upper() not in placeholders
        and reviewed_at.upper() not in placeholders
        and reviewed_date_valid
        and re.fullmatch(r"[0-9a-fA-F]{7,64}", source_commit) is not None
        and manifest_review.upper() not in placeholders
    )


def evaluate_observer_readiness(
    configuration: Any,
    *,
    database_path: str | Path,
    evidence_directory: str | Path = "outputs/observer_evidence",
    project_root: str | Path = ".",
) -> dict[str, Any]:
    """Evaluate every formal observer-readiness group without network or writes."""

    root = Path(project_root).expanduser().resolve()
    database = Path(database_path).expanduser().resolve()
    evidence_root = Path(evidence_directory).expanduser().resolve()
    config_hash = str(configuration.configuration_hash)
    checks: list[dict[str, Any]] = []

    def add(
        group: str,
        name: str,
        status: str,
        detail: str,
        **evidence: Any,
    ) -> None:
        if status not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise ValueError(f"Invalid readiness status: {status}")
        checks.append(
            {
                "group": group,
                "name": name,
                "status": status,
                "detail": detail,
                "evidence": _json_safe(evidence),
            }
        )

    unsafe_configuration = not (
        configuration.execution.paper_only is True
        and configuration.execution.paper_order_submission_enabled is False
    )
    add(
        "offline_engineering",
        "paper_only_configuration",
        "FAIL" if unsafe_configuration else "PASS",
        "paper_only must be true and paper-order submission must be false",
    )
    provider_ok = str(configuration.market_data.provider).lower() == "alpaca" and str(
        configuration.market_data.feed
    ).upper() in {"IEX", "SIP"}
    add(
        "offline_engineering",
        "observer_configuration_valid",
        "PASS" if provider_ok else "FAIL",
        "the strict observer configuration loaded and selects explicit Alpaca IEX/SIP",
    )
    quality_root = evidence_root / QUALITY_EVIDENCE_SUBDIRECTORY
    final_junit = quality_root / "phase2_final_tests.xml"
    quality_status, quality_details = _quality_evidence(
        root,
        configuration_hash=config_hash,
        evidence_directory=evidence_root,
    )
    junit_ok, junit_totals = _read_junit(final_junit)
    add(
        "offline_engineering",
        "full_test_suite",
        (
            "PASS"
            if quality_status == "PASS" and junit_ok
            else ("FAIL" if quality_status == "FAIL" or final_junit.is_file() else "INCOMPLETE")
        ),
        "a passing, current-source-hash-bound final JUnit report is required",
        path=final_junit,
        totals=junit_totals,
    )
    quality_statuses = quality_details.get("check_statuses", {})
    if not isinstance(quality_statuses, Mapping):
        quality_statuses = {}
    for name in ("ruff", "mypy", "safety_static_checks", "config_validation"):
        passed = quality_status == "PASS" and quality_statuses.get(name) == "PASS"
        explicitly_failed = quality_status == "FAIL" and quality_statuses.get(name) == "FAIL"
        add(
            "offline_engineering",
            name,
            "PASS" if passed else ("FAIL" if explicitly_failed else quality_status),
            "the final quality record must bind an explicit PASS to current sources",
            **quality_details,
        )
    add(
        "offline_engineering",
        "current_quality_evidence",
        quality_status,
        "quality evidence must match current source, JUnit, configuration, and check results",
        **quality_details,
    )

    credentials_present = bool(os.environ.get(PAPER_API_KEY_ENV)) and bool(
        os.environ.get(PAPER_SECRET_KEY_ENV)
    )
    token_inactive = os.environ.get(PAPER_ORDER_ENABLEMENT_ENV, "") != PAPER_ORDER_ACKNOWLEDGEMENT
    add(
        "account_and_data",
        "paper_credentials_available",
        "PASS" if credentials_present else "INCOMPLETE",
        "both dedicated paper credential variables must be present; values are never reported",
    )
    add(
        "account_and_data",
        "enablement_token_inactive",
        "PASS" if token_inactive else "FAIL",
        "the paper-order acknowledgement must remain inactive during observer validation",
    )

    database_integrity: str | None = None
    database_error: str | None = None
    account_snapshots = 0
    verified_account_hashes: set[str] = set()
    startup_events: set[str] = set()
    account_evidence_valid = False
    feed_evidence_valid = False
    asset_evidence_valid = False
    history_evidence_valid = False
    handshake_evidence_valid = False
    broker_orders = fills = cancellation_events = 0
    unresolved_current_state = 0
    active_latches: set[str] = set()
    observer_run_count = 0
    fallback_sources: set[str] = set()
    if database.is_file():
        try:
            with _read_only_database(database) as connection:
                row = connection.execute("PRAGMA integrity_check").fetchone()
                database_integrity = None if row is None else str(row[0])
                tables = _table_names(connection)
                if {"application_runs", "stream_events", "account_snapshots"} <= tables:
                    run_rows = connection.execute(
                        "SELECT run_id, started_at, ended_at FROM application_runs "
                        "WHERE mode = 'observe' "
                        "AND configuration_hash = ?",
                        (config_hash,),
                    ).fetchall()
                    run_ids = tuple(str(row["run_id"]) for row in run_rows)
                    observer_run_count = len(run_ids)
                    if run_ids:
                        clause = _placeholders(run_ids)
                        startup_rows = connection.execute(
                            f"SELECT run_id, stream, event_type, symbol, payload "
                            f"FROM stream_events "
                            f"WHERE run_id IN ({clause})",
                            run_ids,
                        ).fetchall()
                        startup_events = {str(row["event_type"]) for row in startup_rows}
                        snapshot_rows = connection.execute(
                            f"SELECT snapshot_id, run_id, account_id_hash, status, "
                            f"trading_blocked FROM account_snapshots "
                            f"WHERE run_id IN ({clause})",
                            run_ids,
                        ).fetchall()
                        snapshots = {str(row["snapshot_id"]): row for row in snapshot_rows}
                        startup_validity: dict[str, dict[str, bool]] = {}
                        for run in run_rows:
                            current_run_id = str(run["run_id"])
                            events = [
                                row for row in startup_rows if str(row["run_id"]) == current_run_id
                            ]
                            stream_types = {
                                (str(row["stream"]), str(row["event_type"])) for row in events
                            }
                            feed_ok = any(
                                str(payload.get("feed") or "").upper()
                                == str(configuration.market_data.feed).upper()
                                and payload.get("fallback_used") is False
                                for payload in (
                                    _json_object(row["payload"])
                                    for row in events
                                    if row["event_type"] == "feed_entitlement_confirmed"
                                )
                            )
                            assets_ok = any(
                                _asset_evidence_valid(payload, configuration.data.tickers)
                                for payload in (
                                    _json_object(row["payload"])
                                    for row in events
                                    if row["event_type"] == "asset_validation_confirmed"
                                )
                            )
                            run_started = _timestamp(run["started_at"])
                            history_ok = bool(
                                run_started is not None
                                and any(
                                    _history_evidence_valid(
                                        payload,
                                        configuration=configuration,
                                        session_date=run_started.astimezone(NEW_YORK).date(),
                                    )
                                    for payload in (
                                        _json_object(row["payload"])
                                        for row in events
                                        if row["event_type"] == "history_preflight_confirmed"
                                    )
                                )
                            )
                            account_ok = False
                            for row in events:
                                if row["event_type"] != "paper_account_verified":
                                    continue
                                payload = _json_object(row["payload"])
                                snapshot = snapshots.get(
                                    str(payload.get("account_snapshot_id") or "")
                                )
                                if (
                                    payload.get("adapter") == "AlpacaPaperBroker"
                                    and payload.get("paper_only") is True
                                    and str(payload.get("account_status") or "").upper() == "ACTIVE"
                                    and payload.get("trading_blocked") is False
                                    and snapshot is not None
                                    and str(snapshot["run_id"]) == current_run_id
                                    and str(snapshot["status"]).upper() == "ACTIVE"
                                    and not bool(snapshot["trading_blocked"])
                                ):
                                    account_ok = True
                                    break
                            startup_validity[current_run_id] = {
                                "account": account_ok,
                                "feed": feed_ok,
                                "assets": assets_ok,
                                "history": history_ok,
                                "market_stream": ("market_data", "connected") in stream_types,
                                "trade_stream": ("trade_updates", "connected") in stream_types,
                            }
                        account_evidence_valid = bool(
                            startup_validity
                            and all(row["account"] for row in startup_validity.values())
                        )
                        feed_evidence_valid = bool(
                            startup_validity
                            and all(row["feed"] for row in startup_validity.values())
                        )
                        asset_evidence_valid = bool(
                            startup_validity
                            and all(row["assets"] for row in startup_validity.values())
                        )
                        history_evidence_valid = bool(
                            startup_validity
                            and all(row["history"] for row in startup_validity.values())
                        )
                        handshake_evidence_valid = bool(
                            startup_validity
                            and all(
                                row["market_stream"] and row["trade_stream"]
                                for row in startup_validity.values()
                            )
                        )
                        bar_keys = {
                            (
                                str(row["symbol"] or "").upper(),
                                str(_json_object(row["payload"]).get("start") or ""),
                                str(_json_object(row["payload"]).get("feed") or "").upper(),
                            )
                            for row in startup_rows
                            if str(row["event_type"]).startswith("bar_")
                        }
                        account_snapshots = _query_count(
                            connection,
                            f"SELECT COUNT(*) FROM account_snapshots WHERE run_id IN ({clause})",
                            run_ids,
                        )
                        verified_account_hashes = {
                            str(row["account_id_hash"])
                            for row in snapshot_rows
                            if str(row["account_id_hash"] or "")
                        }
                        broker_orders = _query_count(
                            connection, "SELECT COUNT(*) FROM broker_orders"
                        )
                        cancellation_events = _query_count(
                            connection,
                            "SELECT COUNT(*) FROM stream_events "
                            "WHERE lower(event_type) LIKE '%cancel%'",
                        )
                        fills = _query_count(connection, "SELECT COUNT(*) FROM fill_events")
                        unresolved_current_state = sum(
                            (
                                _query_count(
                                    connection,
                                    f"SELECT COUNT(*) FROM system_incidents "
                                    f"WHERE run_id IN ({clause}) AND resolved_at IS NULL",
                                    run_ids,
                                ),
                                _query_count(
                                    connection,
                                    f"SELECT COUNT(*) FROM market_data_gaps "
                                    f"WHERE run_id IN ({clause}) AND resolved_at IS NULL",
                                    run_ids,
                                ),
                                _query_count(
                                    connection,
                                    "SELECT COUNT(*) FROM reconciliation_discrepancies d "
                                    "JOIN reconciliation_runs r ON r.reconciliation_id = "
                                    "d.reconciliation_id "
                                    f"WHERE r.run_id IN ({clause}) AND d.resolved_at IS NULL "
                                    "AND lower(d.severity) IN ('blocking', 'critical')",
                                    run_ids,
                                ),
                            )
                        )
                        for halt in connection.execute(
                            "SELECT action, latch_type FROM halt_events ORDER BY created_at"
                        ).fetchall():
                            latch = str(halt["latch_type"])
                            if str(halt["action"]) in {"halt", "hard_stop", "daily_loss"}:
                                active_latches.add(latch)
                            elif str(halt["action"]) in {"resume", "expired"}:
                                active_latches.discard(latch)
                        if "market_bars" in tables:
                            for row in connection.execute(
                                "SELECT symbol, start_at, feed, source FROM market_bars"
                            ).fetchall():
                                started = _timestamp(row["start_at"])
                                key = (
                                    str(row["symbol"]).upper(),
                                    "" if started is None else started.isoformat(),
                                    str(row["feed"]).upper(),
                                )
                                if key in bar_keys and (
                                    str(row["source"]) not in DRY_RUN_REAL_SOURCES
                                    or str(row["feed"]).upper()
                                    != str(configuration.market_data.feed).upper()
                                ):
                                    fallback_sources.add(
                                        f"{row['source']}:{str(row['feed']).upper()}"
                                    )
        except (OSError, sqlite3.DatabaseError) as exc:
            database_error = type(exc).__name__
    add(
        "account_and_data",
        "observer_database_integrity",
        (
            "INCOMPLETE"
            if not database.is_file()
            else ("PASS" if database_integrity == "ok" else "FAIL")
        ),
        "the primary observer database must exist and pass read-only integrity_check",
        path=database,
        result=database_integrity,
        error_type=database_error,
    )
    account_checks = {
        "paper_account_verified": (
            account_snapshots > 0 and account_evidence_valid and len(verified_account_hashes) == 1
        ),
        "feed_verified": feed_evidence_valid,
        "required_assets_valid": asset_evidence_valid,
        "historical_data_available": history_evidence_valid,
        "authenticated_stream_handshakes": handshake_evidence_valid,
        "no_silent_fallback": feed_evidence_valid and not fallback_sources,
    }
    for name, passed in account_checks.items():
        add(
            "account_and_data",
            name,
            "PASS" if passed else "INCOMPLETE",
            "current-configuration durable Alpaca observer evidence is required",
            startup_events=sorted(startup_events),
            fallback_sources=sorted(fallback_sources),
        )

    summary = summarize_observer_evidence(
        evidence_directory=evidence_root,
        configuration_hash=config_hash,
        configuration=configuration,
        database_path=database,
    )
    session_count = int(summary["distinct_pass_observer_sessions"])
    dry_count = int(summary["distinct_pass_real_data_dry_runs"])
    corrupt_record_files = sorted(
        str(path)
        for field in (
            "corrupt_session_record_files",
            "corrupt_dry_run_record_files",
            "corrupt_restart_record_files",
        )
        for path in summary.get(field, ())
    )
    invalid_pass_records = sum(
        int(summary.get(field, 0))
        for field in (
            "invalid_pass_session_records",
            "invalid_pass_dry_run_records",
            "invalid_pass_restart_records",
        )
    )
    add(
        "observer_evidence",
        "evidence_record_integrity",
        "FAIL" if corrupt_record_files or invalid_pass_records else "PASS",
        "corrupt files and forged or database-unlinked PASS records fail closed",
        corrupt_record_files=corrupt_record_files,
        invalid_pass_records=invalid_pass_records,
    )
    add(
        "observer_evidence",
        "five_distinct_pass_sessions",
        "PASS" if session_count >= 5 else "INCOMPLETE",
        "at least five distinct completed PASS observer sessions are required",
        count=session_count,
        dates=summary["pass_observer_session_dates"],
    )
    add(
        "observer_evidence",
        "controlled_restart",
        "PASS" if summary["controlled_restart_pass"] else "INCOMPLETE",
        "at least one explicitly controlled process restart audit must PASS",
    )
    for name in (
        "broker_submissions",
        "broker_cancellations",
        "broker_fills",
        "duplicate_decisions",
        "unresolved_blocking_incidents",
    ):
        count = int(summary[name])
        add(
            "observer_evidence",
            f"zero_{name}",
            "FAIL" if count else ("PASS" if session_count else "INCOMPLETE"),
            f"validated observer evidence requires {name} to equal zero",
            count=count,
        )
    recorded_mutations = broker_orders + cancellation_events + fills
    add(
        "observer_evidence",
        "primary_database_zero_mutations",
        ("FAIL" if recorded_mutations else ("PASS" if database.is_file() else "INCOMPLETE")),
        "the observer database must contain no broker order, cancel, or fill evidence",
        broker_orders=broker_orders,
        cancellations=cancellation_events,
        fills=fills,
    )
    add(
        "observer_evidence",
        "current_database_no_unresolved_state",
        (
            "FAIL"
            if unresolved_current_state or active_latches
            else ("PASS" if database.is_file() and observer_run_count else "INCOMPLETE")
        ),
        "no current-config unresolved incident/gap/blocking discrepancy or active latch may remain",
        unresolved_state_count=unresolved_current_state,
        active_latches=sorted(active_latches),
    )

    add(
        "dry_run_evidence",
        "three_distinct_real_data_dry_runs",
        "PASS" if dry_count >= 3 else "INCOMPLETE",
        "at least three distinct PASS real-data dry-run sessions are required",
        count=dry_count,
        dates=summary["pass_real_data_dry_run_dates"],
    )
    dry_records = load_evidence_records(evidence_root / "dry_runs", kind="real_data_dry_run")
    valid_dry = [
        record
        for record in dry_records
        if _valid_dry_run_evidence(
            record,
            observer_configuration=configuration,
            observer_database_path=database,
        )
    ]
    unsafe_dry_records = [
        record
        for record in dry_records
        if _dry_run_evidence_records_mutation(
            record,
            observer_configuration_hash=config_hash,
            observer_database_path=database,
        )
    ]
    for name, predicate, detail in (
        (
            "zero_dry_run_mutations",
            lambda record: all(
                int(record.get(field, 0)) == 0
                for field in (
                    "broker_submissions",
                    "broker_cancellations",
                    "broker_fills",
                    "position_mutations",
                    "account_mutations",
                    "order_mutations",
                    "orphan_fills",
                )
            ),
            "every accepted dry run must record zero broker and position mutations",
        ),
        (
            "decision_receipt_completeness",
            lambda record: record.get("decision_receipt_complete") is True,
            "every accepted dry run must contain a complete decision receipt",
        ),
        (
            "risk_decision_completeness",
            lambda record: record.get("risk_decision_complete") is True,
            "every accepted dry run must contain a complete independent risk decision",
        ),
    ):
        passed = bool(valid_dry) and all(predicate(record) for record in valid_dry)
        unsafe = bool(unsafe_dry_records) or any(not predicate(record) for record in valid_dry)
        add(
            "dry_run_evidence",
            name,
            "FAIL" if unsafe else ("PASS" if passed else "INCOMPLETE"),
            detail,
            accepted_records=len(valid_dry),
            unsafe_records=len(unsafe_dry_records),
        )

    dry_account_hashes = {
        str(snapshot.get("account_id_hash"))
        for record in valid_dry
        if isinstance((snapshot := record.get("before_account_snapshot")), Mapping)
        and str(snapshot.get("account_id_hash") or "")
    }
    all_verified_account_hashes = verified_account_hashes | dry_account_hashes
    add(
        "account_and_data",
        "single_dedicated_account_identity",
        (
            "FAIL"
            if len(all_verified_account_hashes) > 1
            else ("PASS" if len(all_verified_account_hashes) == 1 else "INCOMPLETE")
        ),
        "all accepted observer and dry-run evidence must use one hashed paper-account identity",
        distinct_account_hash_count=len(all_verified_account_hashes),
    )

    governance_files = {
        "research_methodology": root / "docs" / "methodology.md",
        "architecture": root / "docs" / "architecture.md",
        "data_dictionary": root / "docs" / "data_dictionary.md",
    }
    for name, path in governance_files.items():
        add(
            "governance",
            name,
            "PASS" if path.is_file() else "INCOMPLETE",
            "required frozen protocol/governance evidence must exist",
            path=path,
        )
    hash_path = evidence_root / "research_bundle_manifest.json"
    hash_status, hash_details = _hash_manifest_evidence(root, hash_path)
    add(
        "governance",
        "protocol_hashes",
        hash_status,
        "every frozen protocol manifest entry must exist and match its SHA-256",
        **hash_details,
    )
    add(
        "governance",
        "strategy_and_configuration_frozen",
        hash_status,
        "the frozen config/protocol/strategy/risk manifest must fully revalidate",
        **hash_details,
    )
    review_path = evidence_root / "manual_code_review_attestation.md"
    review_complete = _markdown_all_checked(review_path)
    add(
        "governance",
        "manual_code_review_complete",
        "PASS" if review_complete else ("FAIL" if review_path.is_file() else "INCOMPLETE"),
        "every manual review checklist item must be explicitly checked",
        path=review_path,
    )
    dedicated_path = evidence_root / "dedicated_paper_account_attestation.json"
    dedicated, dedicated_details = _dedicated_account_attestation(
        dedicated_path,
        configuration_hash=config_hash,
        verified_account_hashes=all_verified_account_hashes,
    )
    add(
        "governance",
        "dedicated_paper_account",
        "PASS" if dedicated else ("FAIL" if dedicated_path.is_file() else "INCOMPLETE"),
        "a checked, hash-linked attestation for the verified paper account is required",
        **dedicated_details,
    )
    accepted_run_ids = {str(value) for value in summary.get("accepted_session_run_ids", ())}
    backup_ok, backup_details = _backup_evidence(
        root,
        database_path=database,
        configuration_hash=config_hash,
        accepted_run_ids=accepted_run_ids,
        evidence_directory=evidence_root,
    )
    backup_manifest = evidence_root / "observer_database_backup.json"
    add(
        "governance",
        "primary_database_backup",
        "PASS"
        if backup_ok
        else ("FAIL" if backup_manifest.is_file() and accepted_run_ids else "INCOMPLETE"),
        "a hash-bound backup must contain every accepted observer-session run",
        **backup_details,
    )

    final_status = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in checks)
        else ("INCOMPLETE" if any(item["status"] == "INCOMPLETE" for item in checks) else "PASS")
    )
    safe_report = _json_safe(
        {
            "schema_version": OBSERVER_EVIDENCE_SCHEMA_VERSION,
            "kind": "observer_readiness",
            "checked_at": datetime.now(UTC).isoformat(),
            "status": final_status,
            "configuration_hash": config_hash,
            "database_path": database,
            "evidence_directory": evidence_root,
            "checks": checks,
            "summary": {
                "pass": sum(item["status"] == "PASS" for item in checks),
                "incomplete": sum(item["status"] == "INCOMPLETE" for item in checks),
                "fail": sum(item["status"] == "FAIL" for item in checks),
                "observer_sessions": session_count,
                "real_data_dry_runs": dry_count,
            },
            "disclaimer": (
                f"{PAPER_TRADING_BANNER}. Readiness is operational evidence only, "
                "not investment-performance evidence or authorization to enable orders."
            ),
        }
    )
    if not isinstance(safe_report, dict):  # pragma: no cover - mapping invariant
        raise TypeError("Observer readiness report did not serialize to an object")
    return safe_report
