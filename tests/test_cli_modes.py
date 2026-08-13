"""Focused, subprocess-free checks for the operational CLI safety boundary."""

from __future__ import annotations

import signal
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from adaptive_trader import cli
from adaptive_trader.config import AppConfig, load_config
from adaptive_trader.constants import (
    IEX_FEED_BANNER,
    PAPER_API_KEY_ENV,
    PAPER_FLATTEN_ACKNOWLEDGEMENT,
    PAPER_RESUME_ACKNOWLEDGEMENT,
    PAPER_SECRET_KEY_ENV,
    PAPER_TRADING_BANNER,
    SIP_FEED_BANNER,
)
from adaptive_trader.exceptions import SafetyViolation
from adaptive_trader.persistence import AuditRepository, Database

RUNNER = CliRunner()


@pytest.fixture
def paper_config(tmp_path: Path, project_root: Path) -> Path:
    config = load_config(project_root / "configs" / "paper.yaml").to_canonical_dict()
    config["project"]["database_path"] = str(tmp_path / "paper.db")
    config["project"]["output_directory"] = str(tmp_path / "outputs")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


def _forbid_live_service(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise AssertionError("a live service must not be constructed in this test")


def _clear_paper_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAPER_API_KEY_ENV, raising=False)
    monkeypatch.delenv(PAPER_SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv("APA_ENABLE_PAPER_ORDERS", raising=False)


def test_zero_mutation_broker_blocks_every_mutation_and_raw_client_path() -> None:
    delegate = SimpleNamespace(paper_only=True, read_marker="readable")
    broker = cli._ZeroMutationBroker(delegate)

    assert broker.paper_only is True
    assert broker.read_marker == "readable"

    mutation_methods = (
        "submit_order",
        "cancel_all_orders",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
        "liquidate",
    )
    for name in mutation_methods:
        with pytest.raises(SafetyViolation, match="zero-mutation broker guard"):
            getattr(broker, name)()

    raw_adapter_paths = (
        "api",
        "client",
        "_client",
        "rest_client",
        "session",
        "trade_client",
        "trading_api",
        "trading_client",
        "_delegate",
        "_ZeroMutationBroker__delegate",
    )
    for name in raw_adapter_paths:
        with pytest.raises(SafetyViolation, match="prohibited"):
            getattr(broker, name)

    assert len(broker.mutation_attempts) == len(mutation_methods) + len(raw_adapter_paths)


def test_service_signal_handler_requests_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[int, object] = {}
    restored = object()

    def fake_getsignal(signum: int) -> object:
        del signum
        return restored

    def fake_signal(signum: int, handler: object) -> object:
        installed[int(signum)] = handler
        return restored

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)
    service = SimpleNamespace(stop_calls=0)

    def stop() -> None:
        service.stop_calls += 1

    service.stop = stop
    with cli._service_signal_handlers(service):
        handler = installed[int(signal.SIGTERM)]
        assert callable(handler)
        handler(int(signal.SIGTERM), None)

    assert service.stop_calls == 1
    assert installed[int(signal.SIGINT)] is restored
    assert installed[int(signal.SIGTERM)] is restored


def test_help_exposes_complete_safe_command_surface() -> None:
    result = RUNNER.invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert PAPER_TRADING_BANNER in result.output
    assert IEX_FEED_BANNER in result.output
    assert SIP_FEED_BANNER not in result.output
    assert "exact SIP disclosure is" in result.output
    for command in (
        "doctor",
        "backtest",
        "replay",
        "observe",
        "paper-once",
        "paper-run",
        "status",
        "reconcile",
        "halt",
        "resume",
        "report",
        "validate-config",
        "refresh-data",
    ):
        assert command in result.output


def test_cli_synthetic_backtest_preserves_prestart_warmup_and_configured_start(
    fast_config: AppConfig,
    project_root: Path,
    tmp_path: Path,
) -> None:
    from adaptive_trader.backtest import run_backtest_suite

    values = fast_config.to_dict()
    values["data"]["end_date"] = "2018-03-30"
    config = AppConfig.from_dict(values)
    context = cli.CommandContext(
        config_path=project_root / "configs" / "backtest.yaml",
        project_root=project_root,
        config=config,
        database_path=tmp_path / "audit.db",
        output_directory=tmp_path / "outputs",
    )

    market_data = cli._synthetic_market_data(context, seed=9981)
    configured_start = pd.Timestamp(config.data.start_date)
    assert market_data.prices.index.min() < configured_start

    suite = run_backtest_suite(config, market_data)
    expected_first_session = market_data.prices.index[market_data.prices.index >= configured_start][
        0
    ]
    for run in suite.runs.values():
        assert run.daily.index[0] == expected_first_session
    assert pd.Timestamp(suite.runs["adaptive"].decision_receipts[0]["execution_date"]) == (
        expected_first_session
    )


def test_validate_config_prints_exact_banners_and_main_is_compatible(
    paper_config: Path,
) -> None:
    result = RUNNER.invoke(cli.app, ["validate-config", "--config", str(paper_config)])

    assert result.exit_code == 0, result.output
    assert PAPER_TRADING_BANNER in result.output
    assert IEX_FEED_BANNER in result.output
    assert "Configuration is valid" in result.output
    assert cli.main(["validate-config", "--config", str(paper_config)]) == 0


def test_main_preserves_nonzero_command_exit_status(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_run_observer_readiness", lambda _config: 1)

    assert cli.main(["observer-readiness", "--config", str(paper_config)]) == 1


def test_doctor_without_credentials_is_truthful_and_never_constructs_broker(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    monkeypatch.setattr(cli, "_create_alpaca_broker", _forbid_live_service)

    result = RUNNER.invoke(cli.app, ["doctor", "--config", str(paper_config)])

    assert result.exit_code == 0, result.output
    assert "Paper credentials" in result.output
    assert "SKIPPED" in result.output
    assert "no network request attempted" in result.output
    assert "Last stored heartbeat" in result.output
    assert "Last stored market bar" in result.output
    assert "Paper-order enablement" in result.output
    assert "never submits, cancels, replaces, or liquidates" in result.output
    assert not Path(load_config(paper_config).project.database_path).exists()


def test_doctor_with_credentials_uses_only_reads_and_preserves_local_state(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation_calls: list[str] = []
    account_state = {"status": "ACTIVE", "trading_blocked": False}

    class ReadOnlyDoctorBroker:
        paper_only = True

        def get_account(self) -> SimpleNamespace:
            return SimpleNamespace(**account_state)

        def get_clock(self) -> SimpleNamespace:
            now = datetime.now(UTC)
            return SimpleNamespace(is_open=True, timestamp=now)

        def get_asset(self, symbol: str) -> SimpleNamespace:
            return SimpleNamespace(
                symbol=symbol,
                asset_class="us_equity",
                exchange="NYSE",
                active=True,
                tradable=True,
                fractionable=True,
            )

        def _mutation(self, name: str) -> None:
            mutation_calls.append(name)
            account_state["status"] = "MUTATED"
            raise AssertionError(f"doctor attempted broker mutation: {name}")

        def submit_order(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("submit_order")

        def cancel_all_orders(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("cancel_all_orders")

        def cancel_order(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("cancel_order")

        def replace_order(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("replace_order")

        def close_position(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("close_position")

        def close_all_positions(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("close_all_positions")

        def liquidate(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._mutation("liquidate")

    monkeypatch.setenv(PAPER_API_KEY_ENV, "doctor-paper-key")
    monkeypatch.setenv(PAPER_SECRET_KEY_ENV, "doctor-paper-secret")
    monkeypatch.setattr(cli, "_create_alpaca_broker", lambda credentials: ReadOnlyDoctorBroker())
    monkeypatch.setattr(
        cli,
        "_feed_entitlement_check",
        lambda config, credentials: (True, "configured IEX feed returned a completed bar"),
    )
    database_path = Path(load_config(paper_config).project.database_path)
    before_account_state = dict(account_state)

    assert cli._run_doctor(paper_config) == 0

    assert mutation_calls == []
    assert account_state == before_account_state
    assert not database_path.exists()


def test_alpaca_live_service_receives_forward_decision_engine(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adaptive_trader import decision_engine, live, market_data_live

    captured: dict[str, object] = {}

    class FakeProvider:
        feed = "IEX"

        def __init__(self, **values: object) -> None:
            captured["provider_values"] = values

    class FakeEngine:
        def __init__(self, config: object, market_data: object) -> None:
            captured["engine_config"] = config
            captured["engine_market_data"] = market_data

    class FakeService:
        def __init__(self, **values: object) -> None:
            captured["service_values"] = values

    paper_broker = object()
    monkeypatch.setenv(PAPER_API_KEY_ENV, "paper-key")
    monkeypatch.setenv(PAPER_SECRET_KEY_ENV, "paper-secret")
    monkeypatch.setattr(cli, "_create_alpaca_broker", lambda credentials: paper_broker)
    monkeypatch.setattr(market_data_live, "AlpacaMarketDataProvider", FakeProvider)
    monkeypatch.setattr(decision_engine, "ForwardDecisionEngine", FakeEngine)
    monkeypatch.setattr(live, "LiveService", FakeService)

    context = cli._load_context(paper_config)
    handle = cli._create_live_service(context, mode="observe")
    try:
        values = captured["service_values"]
        assert isinstance(values, dict)
        assert values["broker"] is paper_broker
        assert values["market_data"] is captured["engine_market_data"]
        assert values["target_provider"].__class__ is FakeEngine
        assert captured["engine_config"] is context.config
    finally:
        handle.database.close()


def test_sip_banner_is_emitted_only_after_verified_service_start(
    paper_config: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = yaml.safe_load(paper_config.read_text(encoding="utf-8"))
    raw["market_data"]["feed"] = "SIP"
    paper_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    context = cli._load_context(paper_config)

    class VerifiedService:
        started = False

        def start_streams(self) -> None:
            self.started = True

        def status(self) -> dict[str, object]:
            assert self.started
            return {"feed": "SIP", "feed_entitlement_verified": True}

    service = VerifiedService()
    cli._start_service_and_confirm_feed(context, service)

    assert service.started
    assert SIP_FEED_BANNER in capsys.readouterr().out


@pytest.mark.parametrize("provider", ["synthetic", "replay"])
def test_non_alpaca_sip_configuration_never_claims_unverified_entitlement(
    provider: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = SimpleNamespace(
        market_data=SimpleNamespace(feed="SIP", provider=provider),
    )

    cli._print_feed_disclosure(config)

    output = capsys.readouterr().out
    assert "SIP FEED CONFIGURED — ENTITLEMENT UNCONFIRMED" in output
    assert SIP_FEED_BANNER not in output


def test_status_reports_market_feed_and_current_sip_entitlement(
    paper_config: Path,
) -> None:
    raw = yaml.safe_load(paper_config.read_text(encoding="utf-8"))
    raw["market_data"]["feed"] = "SIP"
    paper_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(paper_config)
    database = Database(config.project.database_path)
    try:
        repository = AuditRepository(database)
        run_id = repository.start_run(
            mode="observe",
            configuration=config,
            market_data_feed="SIP",
        )
        repository.heartbeat(
            run_id=run_id,
            mode="observe",
            healthy=True,
            components={
                "feed": "SIP",
                "feed_entitlement_verified": True,
                "market_open": True,
                "stream_connected": True,
                "trade_updates_healthy": True,
                "trade_updates_status": "connected",
                "fresh": True,
                "history_preflight_verified": True,
                "health_reasons": [],
            },
            created_at=datetime.now(UTC),
        )
    finally:
        database.close()

    result = RUNNER.invoke(cli.app, ["status", "--config", str(paper_config)])

    assert result.exit_code == 0, result.output
    assert "Configured feed" in result.output
    assert "Observed feed" in result.output
    assert "Market status" in result.output
    assert "Feed entitlement verified" in result.output
    assert "Trade-update stream health" in result.output
    assert "connected" in result.output
    assert "History preflight verified" in result.output
    assert "Health reasons" in result.output
    assert SIP_FEED_BANNER in result.output


def test_observer_and_reconcile_without_credentials_fail_closed_before_network(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    monkeypatch.setattr(cli, "_create_live_service", _forbid_live_service)
    monkeypatch.setattr(cli, "_create_alpaca_broker", _forbid_live_service)

    observer = RUNNER.invoke(cli.app, ["observe", "--config", str(paper_config)])
    reconcile = RUNNER.invoke(cli.app, ["reconcile", "--config", str(paper_config)])

    assert observer.exit_code == 2, observer.output
    assert "OBSERVER UNAVAILABLE" in observer.output
    assert "No order was submitted" in observer.output
    assert reconcile.exit_code == 2, reconcile.output
    assert "RECONCILIATION SKIPPED" in reconcile.output
    assert "No network request" in reconcile.output


def test_no_credential_dry_run_shows_proposed_intents_and_submits_nothing(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    monkeypatch.setattr(cli, "_create_live_service", _forbid_live_service)

    once = RUNNER.invoke(
        cli.app,
        ["paper-once", "--config", str(paper_config), "--dry-run"],
    )
    persistent = RUNNER.invoke(cli.app, ["paper-run", "--config", str(paper_config)])

    assert once.exit_code == 0, once.output
    assert "DOWNGRADED TO DRY-RUN" in once.output
    assert "OFFLINE SYNTHETIC ENGINEERING EVIDENCE" in once.output
    assert "NOT MARKET OR PAPER PERFORMANCE" in once.output
    assert "Decision metadata:" in once.output
    assert "Risk metadata:" in once.output
    assert "Proposed intents (" in once.output
    assert "- BUY " in once.output
    assert "Fake broker submission calls: 0" in once.output
    assert "No order was submitted" in once.output
    assert persistent.exit_code == 2, persistent.output
    assert "PAPER-RUN REFUSED" in persistent.output
    assert "No order was submitted" in persistent.output

    database = Database(load_config(paper_config).project.database_path)
    try:
        repository = AuditRepository(database)
        assert repository.count("order_intents") == 0
        assert repository.count("broker_orders") == 0
    finally:
        database.close()

    configured_database = Path(load_config(paper_config).project.database_path)
    offline_database = configured_database.with_name(
        f"{configured_database.stem}.offline-dry-run{configured_database.suffix or '.db'}"
    )
    database = Database(offline_database)
    try:
        repository = AuditRepository(database)
        assert repository.count("application_runs") == 1
        assert repository.count("decision_receipts") == 1
        assert repository.count("strategy_signals") == 1
        assert repository.count("regime_states") == 1
        assert repository.count("allocation_results") == 1
        assert repository.count("risk_decisions") == 1
        assert repository.count("order_intents") == 3
        assert repository.count("broker_orders") == 0
        assert repository.count("daily_performance") == 1
        assert repository.count("benchmark_performance") == 1
    finally:
        database.close()


def test_status_is_read_only_and_unhealthy_heartbeat_returns_nonzero(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    RUNNER.invoke(cli.app, ["paper-run", "--config", str(paper_config)])
    database_path = Path(load_config(paper_config).project.database_path)
    before = database_path.read_bytes()

    result = RUNNER.invoke(cli.app, ["status", "--config", str(paper_config)])

    assert result.exit_code == 1, result.output
    assert "Local paper-system status" in result.output
    assert "unhealthy" in result.output
    assert "read-only" in result.output
    assert database_path.read_bytes() == before


def test_halt_records_latch_but_wrong_flatten_confirmation_never_calls_service(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    monkeypatch.setattr(cli, "_create_live_service", _forbid_live_service)

    result = RUNNER.invoke(
        cli.app,
        [
            "halt",
            "--config",
            str(paper_config),
            "--reason",
            "operator test",
            "--flatten-paper-positions",
            "--acknowledge",
            "WRONG",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "Persistent operator halt recorded" in result.output
    assert "LIQUIDATION REFUSED" in result.output
    assert "no order was submitted" in result.output.lower()

    database = Database(load_config(paper_config).project.database_path)
    try:
        repository = AuditRepository(database)
        assert "operator" in repository.active_halts()
        assert repository.count("broker_orders") == 0
    finally:
        database.close()


def test_nonflatten_halt_never_cancels_even_when_credentials_exist(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellablePaperBroker:
        paper_only = True

        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_all_orders(self) -> None:
            self.cancel_calls += 1

    broker = CancellablePaperBroker()
    monkeypatch.setenv(PAPER_API_KEY_ENV, "fake-paper-key")
    monkeypatch.setenv(PAPER_SECRET_KEY_ENV, "fake-paper-secret")
    monkeypatch.setattr(cli, "_create_alpaca_broker", lambda credentials: broker)

    result = RUNNER.invoke(
        cli.app,
        ["halt", "--config", str(paper_config), "--reason", "operator test"],
    )

    assert result.exit_code == 0, result.output
    assert broker.cancel_calls == 0
    assert "No broker cancellation or liquidation was requested" in result.output
    database = Database(load_config(paper_config).project.database_path)
    try:
        assert AuditRepository(database).count("stream_events") == 0
    finally:
        database.close()


def test_valid_flatten_delegates_one_halt_and_cancel_workflow_to_service(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = yaml.safe_load(paper_config.read_text(encoding="utf-8"))
    raw["execution"]["paper_order_submission_enabled"] = True
    paper_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(PAPER_API_KEY_ENV, "fake-paper-key")
    monkeypatch.setenv(PAPER_SECRET_KEY_ENV, "fake-paper-secret")
    monkeypatch.setenv("APA_ENABLE_PAPER_ORDERS", "I_ACKNOWLEDGE_PAPER_ONLY")

    class Service:
        def __init__(self) -> None:
            self.halt_calls: list[dict[str, object]] = []
            self.shutdown_calls = 0
            self.start_calls = 0

        def start_streams(self) -> None:
            self.start_calls += 1

        def halt(self, **values: object) -> str:
            self.halt_calls.append(values)
            return "service-owned-halt"

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    class ClosedDatabase:
        def close(self) -> None:
            return None

    service = Service()
    monkeypatch.setattr(
        cli,
        "_create_live_service",
        lambda context, mode: SimpleNamespace(service=service, database=ClosedDatabase()),
    )
    monkeypatch.setattr(
        cli,
        "_record_operator_halt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("eligible flatten must not create a duplicate CLI halt")
        ),
    )
    result = RUNNER.invoke(
        cli.app,
        [
            "halt",
            "--config",
            str(paper_config),
            "--reason",
            "explicit liquidation review",
            "--flatten-paper-positions",
            "--acknowledge",
            PAPER_FLATTEN_ACKNOWLEDGEMENT,
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.halt_calls == [
        {
            "reason": "explicit liquidation review",
            "flatten": True,
            "acknowledgement": PAPER_FLATTEN_ACKNOWLEDGEMENT,
        }
    ]
    assert service.shutdown_calls == 1
    assert service.start_calls == 1
    assert "service-owned-halt" in result.output


def test_resume_requires_acknowledgement_and_fresh_connected_preflight(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    halted = RUNNER.invoke(
        cli.app,
        ["halt", "--config", str(paper_config), "--reason", "review"],
    )
    wrong = RUNNER.invoke(
        cli.app,
        ["resume", "--config", str(paper_config), "--acknowledge", "WRONG"],
    )
    unavailable = RUNNER.invoke(
        cli.app,
        [
            "resume",
            "--config",
            str(paper_config),
            "--acknowledge",
            PAPER_RESUME_ACKNOWLEDGEMENT,
        ],
    )

    assert halted.exit_code == 0, halted.output
    assert wrong.exit_code == 2, wrong.output
    assert "RESUME REFUSED" in wrong.output
    assert unavailable.exit_code == 2, unavailable.output
    assert "credentials are absent" in unavailable.output

    database = Database(load_config(paper_config).project.database_path)
    try:
        repository = AuditRepository(database)
        assert "operator" in repository.active_halts()
        repository.record_halt(
            run_id=None,
            action="daily_loss",
            latch_type="daily_loss",
            initiator="test",
            reason="retain independent latch",
        )
        repository.record_halt(
            run_id=None,
            action="hard_stop",
            latch_type="hard_stop",
            initiator="test",
            reason="requires explicit manual review",
        )
        repository.record_halt(
            run_id=None,
            action="halt",
            latch_type="manual",
            initiator="test",
            reason="service-level operator halt",
        )
    finally:
        database.close()

    class FakeService:
        started = False
        reconciled = False

        def start_streams(self) -> None:
            self.started = True

        def reconcile(self) -> object:
            assert self.started
            self.reconciled = True
            return SimpleNamespace(blocking=False)

        def status(self) -> dict[str, bool]:
            assert self.reconciled
            return {"fresh": True, "stream_connected": True}

        def shutdown(self) -> None:
            pass

    class FakeDatabase:
        def close(self) -> None:
            pass

    service = FakeService()
    monkeypatch.setenv(PAPER_API_KEY_ENV, "paper-key")
    monkeypatch.setenv(PAPER_SECRET_KEY_ENV, "paper-secret")
    monkeypatch.setattr(
        cli,
        "_create_live_service",
        lambda context, *, mode, environment=None: cli.ServiceHandle(
            service=service,
            database=FakeDatabase(),
        ),
    )
    resumed = RUNNER.invoke(
        cli.app,
        [
            "resume",
            "--config",
            str(paper_config),
            "--acknowledge",
            PAPER_RESUME_ACKNOWLEDGEMENT,
        ],
    )

    assert resumed.exit_code == 0, resumed.output
    assert "Resume never submits an order" in resumed.output
    assert service.started and service.reconciled

    database = Database(load_config(paper_config).project.database_path)
    try:
        active = AuditRepository(database).active_halts()
        assert "operator" not in active
        assert "manual" not in active
        assert "hard_stop" not in active
        assert "daily_loss" in active
    finally:
        database.close()


def test_replay_is_deterministic_offline_and_uses_the_aligned_fake_broker(
    paper_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_paper_credentials(monkeypatch)
    payload = yaml.safe_load(paper_config.read_text(encoding="utf-8"))
    payload["market_data"]["provider"] = "replay"
    payload["replay"]["fixture_path"] = "missing-replay-fixture.jsonl"
    paper_config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(cli, "_create_alpaca_broker", _forbid_live_service)

    result = RUNNER.invoke(cli.app, ["replay", "--config", str(paper_config)])

    assert result.exit_code == 0, result.output
    assert "Deterministic replay completed with 9 events" in result.output
    assert "Fake broker submission calls: 3" in result.output
    assert "Replay audit database:" in result.output
    database = Database(load_config(paper_config).project.database_path)
    try:
        repository = AuditRepository(database)
        assert repository.count("application_runs") == 1
        assert repository.count("decision_receipts") == 1
        assert repository.count("broker_orders") == 3
        assert repository.count("strategy_signals") == 1
        assert repository.count("regime_states") == 1
        assert repository.count("allocation_results") == 1
        assert repository.count("risk_decisions") == 1
    finally:
        database.close()


def test_report_delegates_to_read_only_forward_generator(
    paper_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    def fake_generate(
        database_path: str | Path,
        output_directory: str | Path,
        *,
        feed: str,
    ) -> dict[str, Path]:
        called.update(database=Path(database_path), output=Path(output_directory), feed=feed)
        report_path = Path(output_directory) / "forward_report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(PAPER_TRADING_BANNER, encoding="utf-8")
        return {"forward_report.md": report_path}

    from adaptive_trader import forward_reporting

    monkeypatch.setattr(forward_reporting, "generate_forward_outputs", fake_generate)
    output = tmp_path / "explicit-report"

    result = RUNNER.invoke(
        cli.app,
        ["report", "--config", str(paper_config), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert called["feed"] == "IEX"
    assert called["output"] == output
    assert "read-only with respect to the audit database" in result.output
    assert "forward_report.md" in result.output
