#!/usr/bin/env python3
"""Validate one real-data paper dry run with before/after read-only evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from adaptive_trader.broker import AlpacaPaperBroker
from adaptive_trader.config import load_config
from adaptive_trader.constants import (
    PAPER_API_KEY_ENV,
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    PAPER_SECRET_KEY_ENV,
)
from adaptive_trader.live_models import PaperCredentials
from adaptive_trader.logging_config import redact
from adaptive_trader.observer_evidence import (
    _asset_evidence_valid,
    _history_evidence_valid,
    derive_real_data_dry_run_config,
    write_evidence_json,
    write_evidence_markdown,
)
from adaptive_trader.persistence import canonical_json


def _hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(broker: AlpacaPaperBroker) -> dict[str, Any]:
    account = broker.get_account()
    positions = sorted(broker.get_positions(), key=lambda item: item.symbol)
    orders = sorted(
        broker.get_orders(include_closed=True),
        key=lambda item: (item.client_order_id, item.updated_at),
    )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "account_id_hash": hashlib.sha256(account.account_id.encode()).hexdigest(),
        "account_status": account.status,
        "trading_blocked": account.trading_blocked,
        "equity": str(account.equity),
        "cash": str(account.cash),
        "positions": {
            item.symbol: {"quantity": str(item.quantity), "market_value": str(item.market_value)}
            for item in positions
        },
        "orders": {
            item.client_order_id: {
                "broker_order_id_hash": hashlib.sha256(item.broker_order_id.encode()).hexdigest(),
                "symbol": item.symbol,
                "side": item.side.value,
                "status": item.status,
                "filled_quantity": str(item.filled_quantity),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in orders
        },
    }


def _position_projection(snapshot: Mapping[str, Any]) -> dict[str, str]:
    positions = snapshot.get("positions")
    if not isinstance(positions, Mapping):
        return {}
    return {
        str(symbol): str(values.get("quantity"))
        for symbol, values in positions.items()
        if isinstance(values, Mapping)
    }


def _order_projection(snapshot: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    orders = snapshot.get("orders")
    if not isinstance(orders, Mapping):
        return {}
    return {
        str(client_id): (str(values.get("status")), str(values.get("filled_quantity")))
        for client_id, values in orders.items()
        if isinstance(values, Mapping)
    }


def _account_projection(snapshot: Mapping[str, Any]) -> tuple[str, str, bool, str]:
    return (
        str(snapshot.get("account_id_hash") or ""),
        str(snapshot.get("account_status") or ""),
        bool(snapshot.get("trading_blocked")),
        str(snapshot.get("cash") or ""),
    )


def _load_approved_dotenv(root: Path) -> None:
    """Load only the three approved paper variables without printing values."""

    dotenv_path = root / ".env"
    if not dotenv_path.is_file():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    values = dotenv_values(dotenv_path)
    for name in (PAPER_API_KEY_ENV, PAPER_SECRET_KEY_ENV, PAPER_ORDER_ENABLEMENT_ENV):
        value = values.get(name)
        if name not in os.environ and isinstance(value, str) and value:
            os.environ[name] = value


def _derived_database(primary: Path) -> Path:
    suffix = primary.suffix or ".db"
    return primary.with_name(f"{primary.stem}.real-data-dry-run{suffix}")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _existing_run_ids(path: Path, configuration_hash: str) -> set[str]:
    if not path.is_file():
        return set()
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT run_id FROM application_runs WHERE configuration_hash = ?",
                    (configuration_hash,),
                ).fetchall()
            }
    except sqlite3.Error:
        return set()


def _database_evidence(
    path: Path,
    configuration_hash: str,
    *,
    previous_run_ids: set[str],
    invocation_started_at: datetime,
    configuration: Any,
    expected_account_hash: str,
) -> dict[str, Any]:
    feed = str(configuration.market_data.feed).upper()
    symbols = tuple(configuration.data.tickers)
    result: dict[str, Any] = {
        "database_integrity": "missing",
        "run_id": None,
        "application_run_started_at": None,
        "application_run_ended_at": None,
        "new_run_identity": False,
        "decision_receipt_complete": False,
        "risk_decision_complete": False,
        "startup_preflight_complete": False,
        "real_stream_symbols": [],
        "broker_submissions": 0,
        "broker_cancellations": 0,
        "broker_fills": 0,
        "orphan_fills": 0,
        "hypothetical_order_intents": 0,
        "signal_cutoff": None,
        "execution_timestamp": None,
        "strategy_outputs": {},
        "regime": None,
        "proposed_portfolio": {},
        "risk_interventions": [],
        "final_target": {},
    }
    if not path.is_file():
        return result
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        result["database_integrity"] = None if integrity is None else str(integrity[0])
        runs = connection.execute(
            "SELECT run_id, started_at, ended_at, market_data_feed FROM application_runs "
            "WHERE mode = 'paper_once' AND configuration_hash = ? ORDER BY started_at",
            (configuration_hash,),
        ).fetchall()
        new_runs = [row for row in runs if str(row["run_id"]) not in previous_run_ids]
        if len(new_runs) != 1:
            return result
        run = new_runs[0]
        run_id = str(run["run_id"])
        started = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        started = started.astimezone(UTC)
        ended = (
            None
            if run["ended_at"] is None
            else datetime.fromisoformat(str(run["ended_at"]).replace("Z", "+00:00"))
        )
        if ended is not None:
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=UTC)
            ended = ended.astimezone(UTC)
        result["run_id"] = run_id
        result["application_run_started_at"] = started.isoformat()
        result["application_run_ended_at"] = None if ended is None else ended.isoformat()
        result["new_run_identity"] = bool(
            started >= invocation_started_at
            and ended is not None
            and ended >= started
            and str(run["market_data_feed"] or "").upper() == feed.upper()
        )
        decisions = connection.execute(
            "SELECT decision_id FROM rebalance_decisions WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        decision_ids = tuple(str(row["decision_id"]) for row in decisions)
        if len(decision_ids) != 1:
            return result
        decision_id = decision_ids[0]
        result["broker_submissions"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM broker_orders WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        result["broker_cancellations"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM stream_events WHERE run_id = ? "
                "AND lower(event_type) LIKE '%cancel%'",
                (run_id,),
            ).fetchone()[0]
        )
        end_bound = ended or datetime.now(UTC)
        result["broker_fills"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM fill_events WHERE decision_id = ? "
                "OR (created_at >= ? AND created_at <= ?)",
                (decision_id, started.isoformat(), end_bound.isoformat()),
            ).fetchone()[0]
        )
        result["orphan_fills"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM fill_events f LEFT JOIN broker_orders o "
                "ON o.client_order_id = f.client_order_id WHERE o.client_order_id IS NULL "
                "AND (f.decision_id = ? OR (f.created_at >= ? AND f.created_at <= ?))",
                (decision_id, started.isoformat(), end_bound.isoformat()),
            ).fetchone()[0]
        )
        result["hypothetical_order_intents"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM order_intents WHERE decision_id = ? "
                "AND reason LIKE 'hypothetical_%'",
                (decision_id,),
            ).fetchone()[0]
        )
        receipt = connection.execute(
            "SELECT payload, receipt_hash, created_at FROM decision_receipts "
            "WHERE run_id = ? AND decision_id = ?",
            (run_id, decision_id),
        ).fetchone()
        receipt_payload = {} if receipt is None else _json_object(receipt["payload"])
        receipt_created = (
            None
            if receipt is None
            else datetime.fromisoformat(str(receipt["created_at"]).replace("Z", "+00:00"))
        )
        if receipt_created is not None and receipt_created.tzinfo is None:
            receipt_created = receipt_created.replace(tzinfo=UTC)
        actual_at_raw = receipt_payload.get("actual_at")
        try:
            actual_at = datetime.fromisoformat(str(actual_at_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            actual_at = None
        if actual_at is not None and actual_at.tzinfo is None:
            actual_at = actual_at.replace(tzinfo=UTC)
        receipt_hash_valid = bool(
            receipt is not None
            and str(receipt["receipt_hash"])
            == hashlib.sha256(canonical_json(receipt_payload).encode()).hexdigest()
        )
        facts: dict[str, dict[str, Any]] = {}
        facts_unique = True
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
            facts_unique = facts_unique and len(rows) == 1
            facts[table] = {} if len(rows) != 1 else _json_object(rows[0]["payload"])
        metadata = receipt_payload.get("decision_metadata")
        result["decision_receipt_complete"] = bool(
            receipt_payload.get("decision_id") == decision_id
            and receipt_hash_valid
            and receipt_payload.get("run_id") == run_id
            and receipt_payload.get("configuration_hash") == configuration_hash
            and receipt_payload.get("execution_phase") == "hypothetical_not_submitted"
            and str(receipt_payload.get("feed") or "").upper() == feed.upper()
            and receipt_payload.get("signal_cutoff") is not None
            and isinstance(metadata, Mapping)
            and isinstance(metadata.get("strategy_outputs"), Mapping)
            and bool(metadata.get("strategy_outputs"))
            and receipt_payload.get("regime") is not None
            and isinstance(receipt_payload.get("allocation"), Mapping)
            and isinstance(receipt_payload.get("final_target"), Mapping)
            and receipt_created is not None
            and actual_at is not None
            and ended is not None
            and started <= receipt_created.astimezone(UTC) <= ended
            and started <= actual_at.astimezone(UTC) <= ended
        )
        strategy = facts["strategy_signals"].get("strategy_outputs")
        allocation = facts["allocation_results"].get("allocation")
        result["risk_decision_complete"] = bool(
            facts_unique
            and isinstance(strategy, Mapping)
            and bool(strategy)
            and facts["regime_states"].get("regime") is not None
            and isinstance(allocation, Mapping)
            and isinstance(facts["allocation_results"].get("final_target"), Mapping)
            and isinstance(facts["risk_decisions"].get("risk_decision"), Mapping)
            and isinstance(facts["risk_decisions"].get("operational_risk_state"), Mapping)
            and isinstance(facts["risk_decisions"].get("final_target"), Mapping)
        )
        if isinstance(receipt_payload, Mapping):
            result["signal_cutoff"] = receipt_payload.get("signal_cutoff")
            result["execution_timestamp"] = receipt_payload.get("actual_at")
            metadata = receipt_payload.get("decision_metadata")
            if isinstance(metadata, Mapping):
                result["strategy_outputs"] = metadata.get("strategy_outputs", {})
                result["regime"] = metadata.get("regime")
                result["proposed_portfolio"] = metadata.get("allocation", {})
                result["risk_interventions"] = metadata.get("risk_actions", [])
            result["final_target"] = receipt_payload.get("final_target", {})
        event_rows = connection.execute(
            "SELECT stream, event_type, symbol, payload FROM stream_events WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        stream_types = {(str(row["stream"]), str(row["event_type"])) for row in event_rows}
        feed_verified = any(
            str(payload.get("feed") or "").upper() == feed.upper()
            and payload.get("fallback_used") is False
            for payload in (
                _json_object(row["payload"])
                for row in event_rows
                if row["event_type"] == "feed_entitlement_confirmed"
            )
        )
        snapshot_rows = connection.execute(
            "SELECT snapshot_id, account_id_hash, status, trading_blocked "
            "FROM account_snapshots WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        snapshots = {str(row["snapshot_id"]): row for row in snapshot_rows}
        account_verified = False
        for row in event_rows:
            if row["event_type"] != "paper_account_verified":
                continue
            payload = _json_object(row["payload"])
            snapshot = snapshots.get(str(payload.get("account_snapshot_id") or ""))
            if (
                payload.get("adapter") == "AlpacaPaperBroker"
                and payload.get("paper_only") is True
                and str(payload.get("account_status") or "").upper() == "ACTIVE"
                and payload.get("trading_blocked") is False
                and snapshot is not None
                and str(snapshot["account_id_hash"]) == expected_account_hash
                and str(snapshot["status"]).upper() == "ACTIVE"
                and not bool(snapshot["trading_blocked"])
            ):
                account_verified = True
                break
        assets_verified = any(
            _asset_evidence_valid(payload, symbols)
            for payload in (
                _json_object(row["payload"])
                for row in event_rows
                if row["event_type"] == "asset_validation_confirmed"
            )
        )
        history_verified = any(
            _history_evidence_valid(
                payload,
                configuration=configuration,
                session_date=started.astimezone(ZoneInfo("America/New_York")).date(),
            )
            for payload in (
                _json_object(row["payload"])
                for row in event_rows
                if row["event_type"] == "history_preflight_confirmed"
            )
        )
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
        bad_provenance = False
        for row in connection.execute(
            "SELECT symbol, start_at, feed, source FROM market_bars"
        ).fetchall():
            key = (
                str(row["symbol"]).upper(),
                str(row["start_at"]),
                str(row["feed"]).upper(),
            )
            if key not in bar_keys:
                continue
            if (
                str(row["source"])
                not in {"alpaca_stream", "alpaca_stream_update", "alpaca_historical"}
                or str(row["feed"]).upper() != feed.upper()
            ):
                bad_provenance = True
            else:
                real_symbols.add(str(row["symbol"]).upper())
        result["real_stream_symbols"] = sorted(real_symbols)
        result["startup_preflight_complete"] = bool(
            feed_verified
            and account_verified
            and assets_verified
            and history_verified
            and ("market_data", "connected") in stream_types
            and set(str(symbol).upper() for symbol in symbols) <= real_symbols
            and not bad_provenance
        )
    return result


def _write_report(root: Path, report: Mapping[str, Any]) -> tuple[Path, Path]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    json_path = (
        root / "outputs" / "observer_evidence" / "dry_runs" / (f"real_data_dry_run_{stamp}.json")
    )
    markdown_path = root / "audit" / f"real_data_dry_run_{stamp}.md"
    write_evidence_json(report, json_path)
    write_evidence_markdown(report, markdown_path, title="Real-Data Paper Dry-Run Audit")
    return json_path, markdown_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/observer.yaml"))
    args = parser.parse_args(argv)
    root = args.config.resolve().parent.parent
    _load_approved_dotenv(root)
    config = load_config(args.config)
    primary = (root / config.project.database_path).resolve()
    derived = _derived_database(primary)
    derived_output = (root / config.project.output_directory).with_name(
        f"{Path(config.project.output_directory).name}.real-data-dry-run"
    )
    dry_run_config = derive_real_data_dry_run_config(
        config,
        database_path=derived,
        output_directory=derived_output,
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "real_data_dry_run",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "INCOMPLETE",
        "configuration_hash": config.configuration_hash,
        "dry_run_configuration_hash": dry_run_config.configuration_hash,
        "provider": str(config.market_data.provider).lower(),
        "feed": str(config.market_data.feed).upper(),
        "real_market_data": False,
        "database_path": str(derived),
        "output_directory": str(derived_output.resolve()),
        "primary_database_path": str(primary),
        "broker_submissions": 0,
        "broker_cancellations": 0,
        "broker_fills": 0,
        "orphan_fills": 0,
        "position_mutations": 0,
        "account_mutations": 0,
        "order_mutations": 0,
        "decision_receipt_complete": False,
        "risk_decision_complete": False,
        "checks": [],
    }

    def incomplete(detail: str) -> int:
        report["checks"] = [
            {
                "group": "real_data_dry_run",
                "name": "preconditions",
                "status": "INCOMPLETE",
                "detail": detail,
                "evidence": {},
            }
        ]
        json_path, markdown_path = _write_report(root, report)
        print(f"Real-data dry-run status: INCOMPLETE — {detail}")
        print(f"JSON evidence: {json_path}")
        print(f"Markdown audit: {markdown_path}")
        return 1

    key = os.environ.get(PAPER_API_KEY_ENV, "")
    secret = os.environ.get(PAPER_SECRET_KEY_ENV, "")
    if not key or not secret:
        return incomplete("dedicated paper credentials are absent; no network request attempted")
    if config.execution.paper_order_submission_enabled:
        return incomplete("paper-order submission is enabled; validation refused")
    if not config.execution.paper_only:
        return incomplete("configuration is not structurally paper-only")
    if str(config.market_data.provider).lower() != "alpaca":
        return incomplete("real-data validation requires the explicit Alpaca provider")
    if os.environ.get(PAPER_ORDER_ENABLEMENT_ENV) == PAPER_ORDER_ACKNOWLEDGEMENT:
        return incomplete("paper-order acknowledgement token is active; validation refused")
    if not primary.is_file():
        return incomplete("primary observer database is absent; immutability cannot be proven")

    credentials = PaperCredentials(key, secret)
    broker = AlpacaPaperBroker(credentials)
    try:
        clock = broker.get_clock()
        if not clock.is_open:
            report["market_state"] = "CLOSED"
            report["session_date"] = (
                clock.timestamp.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
                .date()
                .isoformat()
            )
            return incomplete("regular US equity market is closed; dry run was not executed")
        before = _snapshot(broker)
        primary_before = _hash_file(primary)
        prior_run_ids = _existing_run_ids(derived, dry_run_config.configuration_hash)
        environment = dict(os.environ)
        environment[PAPER_ORDER_ENABLEMENT_ENV] = "NO"
        command = [
            sys.executable,
            "-m",
            "adaptive_trader.cli",
            "paper-once",
            "--config",
            str(args.config),
            "--dry-run",
        ]
        invocation_started_at = datetime.now(UTC)
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        after = _snapshot(broker)
        primary_after = _hash_file(primary)
    except Exception as exc:
        return incomplete(
            "read-only Alpaca/dry-run validation failed: "
            + redact(str(exc) or type(exc).__name__, (key, secret))
        )

    database = _database_evidence(
        derived,
        dry_run_config.configuration_hash,
        previous_run_ids=prior_run_ids,
        invocation_started_at=invocation_started_at,
        configuration=dry_run_config,
        expected_account_hash=str(before["account_id_hash"]),
    )
    report.update(database)
    application_started = report.get("application_run_started_at")
    session_timestamp = (
        None
        if application_started is None
        else datetime.fromisoformat(str(application_started).replace("Z", "+00:00"))
    )
    report["session_date"] = (
        clock.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        if session_timestamp is None
        else session_timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    )
    report["market_state"] = "OPEN"
    report["before_account_snapshot"] = before
    report["after_account_snapshot"] = after
    report["position_mutations"] = int(_position_projection(before) != _position_projection(after))
    report["account_mutations"] = int(_account_projection(before) != _account_projection(after))
    report["order_mutations"] = int(_order_projection(before) != _order_projection(after))
    report["primary_database_sha256_before"] = primary_before
    report["primary_database_sha256_after"] = primary_after
    primary_unchanged = primary_before == primary_after
    zero_mutations = bool(
        int(report["broker_submissions"]) == 0
        and int(report["broker_cancellations"]) == 0
        and int(report["broker_fills"]) == 0
        and int(report["orphan_fills"]) == 0
        and int(report["position_mutations"]) == 0
        and int(report["account_mutations"]) == 0
        and int(report["order_mutations"]) == 0
    )
    checks = [
        ("cli_exit", completed.returncode == 0, "paper-once --dry-run must exit successfully"),
        ("primary_database_unchanged", primary_unchanged, "observer DB must be byte-identical"),
        ("zero_broker_mutations", zero_mutations, "orders/positions/mutations must be unchanged"),
        (
            "decision_receipt_complete",
            bool(report["decision_receipt_complete"]),
            "a complete hypothetical decision receipt is required",
        ),
        (
            "risk_decision_complete",
            bool(report["risk_decision_complete"]),
            "a complete independent risk decision is required",
        ),
        (
            "database_integrity",
            report["database_integrity"] == "ok",
            "the separate dry-run database must pass integrity_check",
        ),
        (
            "startup_preflight",
            bool(report["startup_preflight_complete"]),
            "paper account/feed/assets/history/real-stream/no-fallback evidence is required",
        ),
        (
            "new_run_identity",
            bool(report["new_run_identity"]),
            "exactly one post-invocation run with the derived config hash is required",
        ),
        (
            "account_identity_and_cash_unchanged",
            int(report["account_mutations"]) == 0,
            "account identity/status/blocking/cash must remain unchanged",
        ),
    ]
    report["checks"] = [
        {
            "group": "real_data_dry_run",
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "detail": detail,
            "evidence": {},
        }
        for name, passed, detail in checks
    ]
    report["real_market_data"] = bool(
        completed.returncode == 0
        and str(config.market_data.provider).lower() == "alpaca"
        and report.get("run_id")
        and report.get("startup_preflight_complete") is True
    )
    report["status"] = (
        "PASS" if report["real_market_data"] and all(passed for _, passed, _ in checks) else "FAIL"
    )
    json_path, markdown_path = _write_report(root, report)
    print(f"Real-data dry-run status: {report['status']}")
    print(f"JSON evidence: {json_path}")
    print(f"Markdown audit: {markdown_path}")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
