from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from adaptive_trader.config import AppConfig, load_config
from adaptive_trader.constants import (
    DATABASE_SCHEMA_VERSION,
    PAPER_API_KEY_ENV,
    PAPER_ORDER_ENABLEMENT_ENV,
    PAPER_SECRET_KEY_ENV,
)
from adaptive_trader.live import _sanitize_audit_value
from adaptive_trader.observer_evidence import (
    _backup_evidence,
    _current_quality_source_hash,
    _dedicated_account_attestation,
    _file_sha256,
    _hash_manifest_evidence,
    _quality_evidence,
    _read_junit,
    _valid_dry_run_evidence,
    _valid_session_evidence,
    audit_observer_session,
    derive_real_data_dry_run_config,
    evaluate_observer_readiness,
    evidence_content_hash,
)
from adaptive_trader.persistence import Database, canonical_json

ROOT = Path(__file__).resolve().parents[1]
SESSION_DATE = date(2026, 8, 7)
SESSION_OPEN = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)


def _observer_config(tmp_path: Path) -> AppConfig:
    values = load_config(ROOT / "configs" / "observer.yaml").to_canonical_dict()
    values["project"] = {
        **values["project"],
        "database_path": str(tmp_path / "observer.db"),
        "output_directory": str(tmp_path / "observer-output"),
    }
    # A one-symbol universe keeps the complete 390-minute evidence fixture small.
    values["universe"] = {
        **values["universe"],
        "tickers": ["SPY"],
        "benchmark": "SPY",
    }
    values["momentum"] = {**values["momentum"], "top_n": 1}
    values["mean_reversion"] = {**values["mean_reversion"], "top_n": 1}
    return AppConfig.from_dict(values)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _signed(record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    result["evidence_sha256"] = evidence_content_hash(result)
    return result


def _write_signed(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_signed(record), sort_keys=True), encoding="utf-8")


def _create_complete_observer_session(config: AppConfig, database_path: Path) -> None:
    database = Database(database_path)
    database.close()
    run_id = "observer-run"
    account_hash = hashlib.sha256(b"dedicated-paper-account").hexdigest()
    minimum_history = int(config.market_data.minimum_completed_sessions)
    decision_at = SESSION_OPEN + timedelta(minutes=35)
    receipt = {
        "freshness": {
            "stream_healthy": True,
            "missing_symbols": [],
            "stale_symbols": [],
            "unresolved_gap": False,
        }
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO application_runs "
            "(run_id, started_at, ended_at, mode, configuration_hash, schema_version, "
            "python_version, dependency_metadata, market_data_feed, host_identifier, "
            "shutdown_reason) VALUES (?, ?, ?, 'observe', ?, ?, 'test', '{}', 'IEX', "
            "'test-host', 'clean_test_shutdown')",
            (
                run_id,
                (SESSION_OPEN - timedelta(minutes=1)).isoformat(),
                (SESSION_CLOSE + timedelta(minutes=1)).isoformat(),
                config.configuration_hash,
                DATABASE_SCHEMA_VERSION,
            ),
        )
        connection.execute(
            "INSERT INTO configuration_snapshots "
            "(snapshot_id, run_id, created_at, configuration_hash, configuration) "
            "VALUES ('config-snapshot', ?, ?, ?, ?)",
            (
                run_id,
                (SESSION_OPEN - timedelta(minutes=1)).isoformat(),
                config.configuration_hash,
                _json(config.to_canonical_dict()),
            ),
        )
        connection.execute(
            "INSERT INTO account_snapshots "
            "(snapshot_id, run_id, timestamp, account_id_hash, status, equity, cash, "
            "buying_power, trading_blocked, source) "
            "VALUES ('account-snapshot', ?, ?, ?, 'ACTIVE', '100000', '100000', "
            "'100000', 0, 'broker')",
            (run_id, SESSION_OPEN.isoformat(), account_hash),
        )
        startup_events = (
            (
                "feed",
                "market_data",
                "feed_entitlement_confirmed",
                "SPY",
                {"feed": "IEX", "fallback_used": False},
            ),
            (
                "assets",
                "system",
                "asset_validation_confirmed",
                None,
                {"symbols": ["SPY"]},
            ),
            (
                "history",
                "strategy",
                "history_preflight_confirmed",
                None,
                {
                    "observations": minimum_history,
                    "minimum_required": minimum_history,
                    "cutoff": (SESSION_OPEN - timedelta(days=1)).isoformat(),
                },
            ),
            (
                "account",
                "paper_broker",
                "paper_account_verified",
                None,
                {
                    "adapter": "AlpacaPaperBroker",
                    "paper_only": True,
                    "account_status": "ACTIVE",
                    "trading_blocked": False,
                    "account_snapshot_id": "account-snapshot",
                },
            ),
            ("market-connected", "market_data", "connected", None, {"reason": "connected"}),
            ("trade-connected", "trade_updates", "connected", None, {"status": "connected"}),
        )
        connection.executemany(
            "INSERT INTO stream_events "
            "(event_id, run_id, created_at, stream, event_type, symbol, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    event_id,
                    run_id,
                    (SESSION_OPEN - timedelta(seconds=30)).isoformat(),
                    stream,
                    event_type,
                    symbol,
                    _json(payload),
                )
                for event_id, stream, event_type, symbol, payload in startup_events
            ],
        )
        bar_rows: list[tuple[Any, ...]] = []
        event_rows: list[tuple[Any, ...]] = []
        for offset in range(390):
            started = SESSION_OPEN + timedelta(minutes=offset)
            bar_rows.append(
                (
                    f"bar-{offset}",
                    "SPY",
                    started.isoformat(),
                    (started + timedelta(minutes=1)).isoformat(),
                    "100",
                    "101",
                    "99",
                    "100",
                    1000,
                    "IEX",
                    (started + timedelta(seconds=5)).isoformat(),
                    "alpaca_stream",
                    0,
                    0,
                )
            )
            event_rows.append(
                (
                    f"bar-event-{offset}",
                    run_id,
                    (started + timedelta(seconds=5)).isoformat(),
                    "market_data",
                    "bar_inserted",
                    "SPY",
                    _json({"start": started.isoformat(), "feed": "IEX"}),
                )
            )
        connection.executemany(
            "INSERT INTO market_bars "
            "(bar_id, symbol, start_at, end_at, open, high, low, close, volume, feed, "
            "received_at, source, is_correction, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            bar_rows,
        )
        connection.executemany(
            "INSERT INTO stream_events "
            "(event_id, run_id, created_at, stream, event_type, symbol, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            event_rows,
        )
        connection.executemany(
            "INSERT INTO heartbeats "
            "(heartbeat_id, run_id, created_at, mode, healthy, components) "
            "VALUES (?, ?, ?, 'observe', 1, '{}')",
            [
                (
                    f"heartbeat-{offset}",
                    run_id,
                    (SESSION_OPEN + timedelta(minutes=offset)).isoformat(),
                )
                for offset in range(391)
            ],
        )
        connection.execute(
            "INSERT INTO rebalance_decisions "
            "(decision_id, run_id, idempotency_key, session_date, strategy_version, mode, "
            "scheduled_at, created_at, completed_at, status, payload) "
            "VALUES ('decision', ?, 'observer:test:2026-08-07', ?, 'v1', 'observe', ?, ?, ?, "
            "'observed', '{}')",
            (
                run_id,
                SESSION_DATE.isoformat(),
                decision_at.isoformat(),
                decision_at.isoformat(),
                (decision_at + timedelta(seconds=1)).isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO decision_receipts "
            "(receipt_id, decision_id, run_id, created_at, receipt_hash, payload) "
            "VALUES ('receipt', 'decision', ?, ?, ?, ?)",
            (
                run_id,
                decision_at.isoformat(),
                hashlib.sha256(_json(receipt).encode()).hexdigest(),
                _json(receipt),
            ),
        )
        connection.execute(
            "INSERT INTO reconciliation_runs "
            "(reconciliation_id, run_id, started_at, completed_at, clean, blocking, summary) "
            "VALUES ('reconciliation', ?, ?, ?, 1, 0, '{}')",
            (
                run_id,
                (SESSION_CLOSE - timedelta(minutes=1)).isoformat(),
                SESSION_CLOSE.isoformat(),
            ),
        )
        marker = {
            "session_date": SESSION_DATE.isoformat(),
            "session_open_at": SESSION_OPEN.isoformat(),
            "session_close_at": SESSION_CLOSE.isoformat(),
            "finalized": True,
        }
        connection.executemany(
            "INSERT INTO generated_reports "
            "(report_id, run_id, created_at, report_type, path, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    "close-marker",
                    run_id,
                    SESSION_CLOSE.isoformat(),
                    f"session_close_finalization:{SESSION_DATE.isoformat()}",
                    str(database_path),
                    _json(marker),
                ),
                (
                    "daily-report",
                    run_id,
                    SESSION_CLOSE.isoformat(),
                    f"daily_forward_session:{SESSION_DATE.isoformat()}",
                    "daily-report.md",
                    _json({"generated": True}),
                ),
            ),
        )


def test_complete_session_requires_full_minutes_and_revalidates_substantive_claims(
    tmp_path: Path,
) -> None:
    config = _observer_config(tmp_path)
    database_path = tmp_path / "observer.db"
    _create_complete_observer_session(config, database_path)

    report = audit_observer_session(
        config,
        database_path=database_path,
        session_date=SESSION_DATE,
    )
    assert report["status"] == "PASS"
    assert report["bars_received_by_symbol"] == {"SPY": 390}
    assert _valid_session_evidence(
        _signed(report), configuration=config, database_path=database_path
    )

    forged = _signed({**report, "clean_shutdown": False})
    assert not _valid_session_evidence(forged, configuration=config, database_path=database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM market_bars WHERE bar_id = 'bar-120'")
    failed = audit_observer_session(
        config,
        database_path=database_path,
        session_date=SESSION_DATE,
    )
    assert failed["status"] == "FAIL"
    assert failed["bars_received_by_symbol"] == {"SPY": 389}
    assert not _valid_session_evidence(
        _signed(report), configuration=config, database_path=database_path
    )


def test_real_data_dry_run_config_is_strictly_isolated(tmp_path: Path) -> None:
    config = _observer_config(tmp_path)
    dry_database = tmp_path / "dry" / "run.db"
    dry_output = tmp_path / "dry-output"
    derived = derive_real_data_dry_run_config(
        config,
        database_path=dry_database,
        output_directory=dry_output,
    )

    assert Path(derived.project.database_path) == dry_database.resolve()
    assert Path(derived.project.output_directory) == dry_output.resolve()
    assert derived.project.database_path != config.project.database_path
    assert derived.configuration_hash != config.configuration_hash
    assert derived.execution.paper_only is True
    assert derived.execution.paper_order_submission_enabled is False
    assert config.execution.paper_order_submission_enabled is False


def _create_valid_dry_run_record(
    config: AppConfig,
    tmp_path: Path,
) -> tuple[dict[str, Any], Path, Path]:
    primary = tmp_path / "observer.db"
    primary_database = Database(primary)
    primary_database.close()
    dry_database = tmp_path / "observer.real-data-dry-run.db"
    dry_output = tmp_path / "observer-output.real-data-dry-run"
    derived = derive_real_data_dry_run_config(
        config,
        database_path=dry_database,
        output_directory=dry_output,
    )
    database = Database(dry_database)
    database.close()
    run_id = "dry-run"
    decision_id = "dry-decision"
    started = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)
    ended = started + timedelta(minutes=10)
    actual_at = started + timedelta(minutes=5)
    signal_cutoff = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
    account_hash = hashlib.sha256(b"dedicated-paper-account").hexdigest()
    strategy_outputs = {"momentum": {"SPY": "1"}}
    regime = "bull_low_vol"
    proposed = {"SPY": "1"}
    risk_actions = [{"control": "position_cap", "applied": False}]
    final_target = {"SPY": "1"}
    receipt = {
        "decision_id": decision_id,
        "run_id": run_id,
        "configuration_hash": derived.configuration_hash,
        "execution_phase": "hypothetical_not_submitted",
        "feed": "IEX",
        "signal_cutoff": signal_cutoff.isoformat(),
        "actual_at": actual_at.isoformat(),
        "decision_metadata": {
            "strategy_outputs": strategy_outputs,
            "regime": regime,
            "allocation": proposed,
            "risk_actions": risk_actions,
        },
        "regime": regime,
        "allocation": proposed,
        "final_target": final_target,
    }
    minimum_history = int(config.market_data.minimum_completed_sessions)
    with sqlite3.connect(dry_database) as connection:
        connection.execute(
            "INSERT INTO application_runs "
            "(run_id, started_at, ended_at, mode, configuration_hash, schema_version, "
            "python_version, dependency_metadata, market_data_feed, host_identifier, "
            "shutdown_reason) VALUES (?, ?, ?, 'paper_once', ?, ?, 'test', '{}', 'IEX', "
            "'test-host', 'dry_run_complete')",
            (
                run_id,
                started.isoformat(),
                ended.isoformat(),
                derived.configuration_hash,
                DATABASE_SCHEMA_VERSION,
            ),
        )
        connection.execute(
            "INSERT INTO configuration_snapshots "
            "(snapshot_id, run_id, created_at, configuration_hash, configuration) "
            "VALUES ('dry-config', ?, ?, ?, ?)",
            (
                run_id,
                started.isoformat(),
                derived.configuration_hash,
                _json(derived.to_canonical_dict()),
            ),
        )
        connection.execute(
            "INSERT INTO rebalance_decisions "
            "(decision_id, run_id, idempotency_key, session_date, strategy_version, mode, "
            "scheduled_at, created_at, completed_at, status, payload) "
            "VALUES (?, ?, 'dry:test:2026-08-07', ?, 'v1', 'paper_once', ?, ?, ?, "
            "'observed', '{}')",
            (
                decision_id,
                run_id,
                SESSION_DATE.isoformat(),
                actual_at.isoformat(),
                actual_at.isoformat(),
                (actual_at + timedelta(seconds=1)).isoformat(),
            ),
        )
        fact_rows = (
            (
                "strategy_signals",
                "signal_id",
                "signal",
                {"strategy_outputs": strategy_outputs},
            ),
            ("regime_states", "regime_state_id", "regime", {"regime": regime}),
            (
                "allocation_results",
                "allocation_id",
                "allocation",
                {"allocation": proposed, "final_target": final_target},
            ),
            (
                "risk_decisions",
                "risk_decision_id",
                "risk",
                {
                    "risk_decision": {"allowed": True},
                    "operational_risk_state": {"healthy": True},
                    "final_target": final_target,
                },
            ),
        )
        for table, id_column, identifier, payload in fact_rows:
            connection.execute(
                f"INSERT INTO {table} "
                f"({id_column}, run_id, decision_id, created_at, as_of_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    run_id,
                    decision_id,
                    actual_at.isoformat(),
                    signal_cutoff.isoformat(),
                    _json(payload),
                ),
            )
        connection.execute(
            "INSERT INTO decision_receipts "
            "(receipt_id, decision_id, run_id, created_at, receipt_hash, payload) "
            "VALUES ('dry-receipt', ?, ?, ?, ?, ?)",
            (
                decision_id,
                run_id,
                actual_at.isoformat(),
                hashlib.sha256(canonical_json(receipt).encode()).hexdigest(),
                _json(receipt),
            ),
        )
        connection.execute(
            "INSERT INTO account_snapshots "
            "(snapshot_id, run_id, timestamp, account_id_hash, status, equity, cash, "
            "buying_power, trading_blocked, source) "
            "VALUES ('dry-account', ?, ?, ?, 'ACTIVE', '100000', '100000', '100000', "
            "0, 'broker')",
            (run_id, started.isoformat(), account_hash),
        )
        events = (
            (
                "dry-account-event",
                "paper_broker",
                "paper_account_verified",
                None,
                {
                    "adapter": "AlpacaPaperBroker",
                    "paper_only": True,
                    "account_status": "ACTIVE",
                    "trading_blocked": False,
                    "account_snapshot_id": "dry-account",
                },
            ),
            (
                "dry-feed",
                "market_data",
                "feed_entitlement_confirmed",
                "SPY",
                {"feed": "IEX", "fallback_used": False},
            ),
            (
                "dry-assets",
                "system",
                "asset_validation_confirmed",
                None,
                {"symbols": ["SPY"]},
            ),
            (
                "dry-history",
                "strategy",
                "history_preflight_confirmed",
                None,
                {
                    "observations": minimum_history,
                    "minimum_required": minimum_history,
                    "cutoff": signal_cutoff.isoformat(),
                },
            ),
            ("dry-connected", "market_data", "connected", None, {"reason": "connected"}),
            (
                "dry-bar-event",
                "market_data",
                "bar_inserted",
                "SPY",
                {
                    "start": (started - timedelta(minutes=1)).isoformat(),
                    "feed": "IEX",
                },
            ),
        )
        connection.executemany(
            "INSERT INTO stream_events "
            "(event_id, run_id, created_at, stream, event_type, symbol, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (event_id, run_id, started.isoformat(), stream, event_type, symbol, _json(payload))
                for event_id, stream, event_type, symbol, payload in events
            ],
        )
        bar_at = started - timedelta(minutes=1)
        connection.execute(
            "INSERT INTO market_bars "
            "(bar_id, symbol, start_at, end_at, open, high, low, close, volume, feed, "
            "received_at, source, is_correction, revision) "
            "VALUES ('dry-bar', 'SPY', ?, ?, '100', '101', '99', '100', 1000, 'IEX', "
            "?, 'alpaca_historical', 0, 0)",
            (
                bar_at.isoformat(),
                (bar_at + timedelta(minutes=1)).isoformat(),
                started.isoformat(),
            ),
        )
    snapshot = {
        "captured_at": started.isoformat(),
        "account_id_hash": account_hash,
        "account_status": "ACTIVE",
        "trading_blocked": False,
        "equity": "100000",
        "cash": "100000",
        "positions": {},
        "orders": {},
    }
    primary_hash = _file_sha256(primary)
    checks = [
        {
            "group": "real_data_dry_run",
            "name": name,
            "status": "PASS",
            "detail": "fixture",
            "evidence": {},
        }
        for name in (
            "cli_exit",
            "primary_database_unchanged",
            "zero_broker_mutations",
            "decision_receipt_complete",
            "risk_decision_complete",
            "database_integrity",
            "startup_preflight",
            "new_run_identity",
        )
    ]
    record = {
        "schema_version": 1,
        "kind": "real_data_dry_run",
        "generated_at": ended.isoformat(),
        "status": "PASS",
        "configuration_hash": config.configuration_hash,
        "dry_run_configuration_hash": derived.configuration_hash,
        "provider": "alpaca",
        "feed": "IEX",
        "real_market_data": True,
        "database_path": str(dry_database.resolve()),
        "output_directory": str(dry_output.resolve()),
        "primary_database_path": str(primary.resolve()),
        "run_id": run_id,
        "application_run_started_at": started.isoformat(),
        "application_run_ended_at": ended.isoformat(),
        "session_date": SESSION_DATE.isoformat(),
        "market_state": "OPEN",
        "database_integrity": "ok",
        "new_run_identity": True,
        "startup_preflight_complete": True,
        "real_stream_symbols": ["SPY"],
        "decision_receipt_complete": True,
        "risk_decision_complete": True,
        "signal_cutoff": signal_cutoff.isoformat(),
        "execution_timestamp": actual_at.isoformat(),
        "strategy_outputs": strategy_outputs,
        "regime": regime,
        "proposed_portfolio": proposed,
        "risk_interventions": risk_actions,
        "final_target": final_target,
        "hypothetical_order_intents": 0,
        "broker_submissions": 0,
        "broker_cancellations": 0,
        "broker_fills": 0,
        "orphan_fills": 0,
        "position_mutations": 0,
        "account_mutations": 0,
        "order_mutations": 0,
        "primary_database_sha256_before": primary_hash,
        "primary_database_sha256_after": primary_hash,
        "before_account_snapshot": copy.deepcopy(snapshot),
        "after_account_snapshot": copy.deepcopy(snapshot),
        "checks": checks,
    }
    return record, primary, dry_database


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("signal_cutoff", "2020-01-01T00:00:00+00:00"),
        ("execution_timestamp", "2020-01-01T00:00:00+00:00"),
        ("strategy_outputs", {"forged": True}),
        ("regime", "forged_regime"),
        ("proposed_portfolio", {"SPY": "0"}),
        ("risk_interventions", [{"control": "forged"}]),
        ("final_target", {"SPY": "0"}),
        ("hypothetical_order_intents", 99),
        ("application_run_ended_at", "2020-01-01T00:00:00+00:00"),
    ),
)
def test_dry_run_displayed_claims_are_bound_to_linked_database(
    tmp_path: Path,
    field: str,
    forged_value: Any,
) -> None:
    config = _observer_config(tmp_path)
    record, primary, _ = _create_valid_dry_run_record(config, tmp_path)
    assert _valid_dry_run_evidence(
        _signed(record),
        observer_configuration=config,
        observer_database_path=primary,
    )

    record[field] = forged_value
    assert not _valid_dry_run_evidence(
        _signed(record),
        observer_configuration=config,
        observer_database_path=primary,
    )


def test_dry_run_evidence_rejects_complete_account_identifier(tmp_path: Path) -> None:
    config = _observer_config(tmp_path)
    record, primary, _ = _create_valid_dry_run_record(config, tmp_path)
    record["before_account_snapshot"]["account_id"] = "COMPLETE-ACCOUNT-ID"
    assert not _valid_dry_run_evidence(
        _signed(record),
        observer_configuration=config,
        observer_database_path=primary,
    )


def test_account_identifier_sanitization_is_recursive() -> None:
    sentinel = "COMPLETE-ACCOUNT-IDENTIFIER-DO-NOT-PERSIST"
    sanitized = _sanitize_audit_value(
        {
            "outer": {
                "account_id": sentinel,
                "nested": [{"account_number": sentinel}, {"paper_account_id": sentinel}],
            }
        }
    )
    rendered = json.dumps(sanitized, sort_keys=True)
    assert sentinel not in rendered
    assert rendered.count(hashlib.sha256(sentinel.encode()).hexdigest()) == 3


def test_readiness_is_incomplete_read_only_and_does_not_create_primary_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (PAPER_API_KEY_ENV, PAPER_SECRET_KEY_ENV, PAPER_ORDER_ENABLEMENT_ENV):
        monkeypatch.delenv(name, raising=False)
    config = _observer_config(tmp_path)
    database_path = tmp_path / "never-create" / "observer.db"
    evidence_path = tmp_path / "never-create-evidence"

    report = evaluate_observer_readiness(
        config,
        database_path=database_path,
        evidence_directory=evidence_path,
        project_root=tmp_path,
    )

    assert report["status"] == "INCOMPLETE"
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert not evidence_path.exists()


def test_corrupt_evidence_forces_readiness_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (PAPER_API_KEY_ENV, PAPER_SECRET_KEY_ENV, PAPER_ORDER_ENABLEMENT_ENV):
        monkeypatch.delenv(name, raising=False)
    config = _observer_config(tmp_path)
    evidence_path = tmp_path / "evidence"
    corrupt = evidence_path / "sessions" / "forged.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not-json", encoding="utf-8")

    report = evaluate_observer_readiness(
        config,
        database_path=tmp_path / "missing.db",
        evidence_directory=evidence_path,
        project_root=tmp_path,
    )

    assert report["status"] == "FAIL"


def _manifest_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    roles = {
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
    template: dict[str, Any] = {
        "schema_version": 1,
        "protocol_version": "phase2-research-bundle-v1",
        "frozen_at_utc": "2026-08-10T11:11:00Z",
        "manifest_generated_at_utc": "2026-08-10T11:46:18Z",
        "hash_algorithm": "SHA-256",
        "files": [{"path": path, "role": role} for path, role in roles.items()],
    }
    for entry in template["files"]:
        relative = Path(entry["path"])
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.as_posix() == "configs/backtest.yaml":
            shutil.copyfile(ROOT / relative, destination)
        else:
            destination.write_text(f"fixture for {relative.as_posix()}\n", encoding="utf-8")
        entry["sha256"] = _file_sha256(destination)
    template["file_count"] = len(template["files"])
    template["configuration_semantic_hash"] = load_config(
        tmp_path / "configs" / "backtest.yaml"
    ).configuration_hash
    path = tmp_path / "outputs" / "observer_evidence" / "research_bundle_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template), encoding="utf-8")
    return path, template


def test_manifest_requires_exact_frozen_bundle_and_handles_malformed_counts(
    tmp_path: Path,
) -> None:
    path, manifest = _manifest_fixture(tmp_path)
    assert _hash_manifest_evidence(tmp_path, path)[0] == "PASS"

    reduced = copy.deepcopy(manifest)
    reduced["files"] = [
        entry for entry in reduced["files"] if entry["path"] != "src/adaptive_trader/features.py"
    ]
    reduced["file_count"] = len(reduced["files"])
    path.write_text(json.dumps(reduced), encoding="utf-8")
    assert _hash_manifest_evidence(tmp_path, path)[0] == "FAIL"

    reduced["file_count"] = "not-an-integer"
    path.write_text(json.dumps(reduced), encoding="utf-8")
    assert _hash_manifest_evidence(tmp_path, path)[0] == "FAIL"


def test_quality_evidence_is_self_hashed_source_bound_and_malformed_junit_fails(
    tmp_path: Path,
) -> None:
    quality = tmp_path / "outputs" / "observer_evidence" / "quality"
    quality.mkdir(parents=True)
    junit = quality / "phase2_final_tests.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    record = {
        "schema_version": 1,
        "kind": "phase2_final_quality",
        "status": "PASS",
        "configuration_hash": "observer-config",
        "source_hash": _current_quality_source_hash(tmp_path),
        "junit_sha256": _file_sha256(junit),
        "checks": {
            "ruff": "PASS",
            "mypy": "PASS",
            "safety_static_checks": "PASS",
            "config_validation": "PASS",
        },
    }
    _write_signed(quality / "phase2_final_quality.json", record)
    assert _quality_evidence(tmp_path, configuration_hash="observer-config")[0] == "PASS"

    (tmp_path / ".env.example").write_text("APA_ENABLE_PAPER_ORDERS=NO\n", encoding="utf-8")
    assert _quality_evidence(tmp_path, configuration_hash="observer-config")[0] == "FAIL"

    junit.write_text('<testsuite tests="NaN" failures="0" errors="0"/>', encoding="utf-8")
    assert _read_junit(junit) == (False, {})


def test_attestation_rejects_raw_account_identifier(tmp_path: Path) -> None:
    path = tmp_path / "attestation.json"
    account_hash = hashlib.sha256(b"paper-account").hexdigest()
    record = {
        "schema_version": 1,
        "kind": "dedicated_paper_account_attestation",
        "dedicated_paper_account": True,
        "configuration_hash": "observer-config",
        "account_id_hash": account_hash,
        "reviewed_at": datetime(2026, 8, 7, tzinfo=UTC).isoformat(),
        "reviewed_by": "Quant Club risk lead",
    }
    _write_signed(path, record)
    assert _dedicated_account_attestation(
        path,
        configuration_hash="observer-config",
        verified_account_hashes={account_hash},
    )[0]

    record["account_id"] = "COMPLETE-ACCOUNT-IDENTIFIER"
    _write_signed(path, record)
    assert not _dedicated_account_attestation(
        path,
        configuration_hash="observer-config",
        verified_account_hashes={account_hash},
    )[0]


def test_backup_gate_rechecks_hash_integrity_and_run_linkage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = root / "runtime" / "observer.db"
    database = Database(source)
    database.close()
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO application_runs "
            "(run_id, started_at, mode, configuration_hash, schema_version, python_version, "
            "dependency_metadata, market_data_feed, host_identifier) "
            "VALUES ('accepted-run', ?, 'observe', 'observer-config', ?, 'test', '{}', "
            "'IEX', 'host')",
            (datetime(2026, 8, 7, tzinfo=UTC).isoformat(), DATABASE_SCHEMA_VERSION),
        )
    backup = root / "runtime" / "backups" / "observer.db"
    backup.parent.mkdir(parents=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(backup) as backup_connection:
        source_connection.backup(backup_connection)
    record = {
        "schema_version": 1,
        "kind": "observer_database_backup",
        "configuration_hash": "observer-config",
        "primary_database_path": str(source.resolve()),
        "primary_database_sha256": _file_sha256(source),
        "backup_path": str(backup.resolve()),
        "backup_sha256": _file_sha256(backup),
        "accepted_session_run_ids": ["accepted-run"],
    }
    _write_signed(root / "outputs" / "observer_evidence" / "observer_database_backup.json", record)
    assert _backup_evidence(
        root,
        database_path=source,
        configuration_hash="observer-config",
        accepted_run_ids={"accepted-run"},
    )[0]

    with backup.open("ab") as handle:
        handle.write(b"tamper")
    assert not _backup_evidence(
        root,
        database_path=source,
        configuration_hash="observer-config",
        accepted_run_ids={"accepted-run"},
    )[0]
