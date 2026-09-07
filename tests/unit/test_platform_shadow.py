"""Shadow uses actual durable data/risk/planning without broker construction."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, func, select

from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import policy_hash
from adaptive_trader.platform.scheduling import DecisionSlotRepository, build_session_schedule
from adaptive_trader.platform.service_cycles import _seed_offline_risk_history
from adaptive_trader.platform.shadow import run_shadow_once
from adaptive_trader.platform.shadow_settings import load_shadow_execution_settings
from adaptive_trader.platform.signals import (
    AlwaysFlatSignalProvider,
    DecisionContext,
    SignalEnvelopeRepository,
)
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.market_data import BarIdentity, BarWrite, MarketDataRepository
from adaptive_trader.platform.storage.tables import aqa_audit_events, aqa_execution_plans, metadata

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 6, 13, 46, tzinfo=UTC)


def settings():
    return load_shadow_execution_settings({}, application_root=ROOT, fixture=True)


@pytest.fixture
def engine(tmp_path: Path):
    result = create_engine(f"sqlite:///{tmp_path / 'shadow.db'}").execution_options(
        schema_translate_map={"aqa": None}
    )
    metadata.create_all(result)
    ExperimentRepository(result).register(
        settings().platform.experiment.definition, registered_at=NOW
    )
    yield result
    result.dispose()


def seed(engine: Engine, *, persist_signal: bool = True):
    configured = settings()
    experiment = configured.platform.experiment.definition
    calendar = XnasExchangeCalendar()
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="always_flat",
        signal_provider_version="1",
        session_date=date(2026, 7, 6),
        calendar=calendar,
    )
    DecisionSlotRepository(engine).create_schedule(schedule, recorded_at=NOW)
    slot = schedule.strategy_slots[0]
    repository = MarketDataRepository(engine)
    _seed_offline_risk_history(
        repository=repository, calendar=calendar, active_symbols=experiment.active_tradable
    )
    hashes = []
    for symbol in experiment.active_tradable:
        digest = sha256_hex(("shadow_fixture", symbol))
        hashes.append(digest)
        repository.append(
            BarWrite(
                identity=BarIdentity(
                    "fixture",
                    "iex",
                    "raw",
                    symbol,
                    "15Min",
                    slot.source_interval_start,
                    slot.source_interval_end,
                ),
                received_at=slot.source_interval_end + timedelta(seconds=1),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=Decimal(1000),
                trade_count=100,
                vwap=Decimal(100),
                quality_flags=("complete",),
                source="fixture",
                source_payload_hash=digest,
                source_mode="offline_fixture",
                source_event_id=f"shadow_fixture_{symbol}",
            )
        )
    if not persist_signal:
        return slot
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=sha256_hex(tuple(sorted(hashes))),
        policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
        execution_mode=configured.platform.profile.mode,
        broker_adapter=configured.platform.profile.execution.broker,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )
    signal = AlwaysFlatSignalProvider(
        clock=lambda: slot.ready_at + timedelta(seconds=3)
    ).signal_for(context)
    SignalEnvelopeRepository(engine).persist_once(signal, context=context)
    return slot


def test_shadow_missing_slot_records_blocked_evidence(engine: Engine) -> None:
    result = run_shadow_once(
        engine=engine, settings=settings(), slot_id=None, now=NOW, fixture=True
    )
    assert result["reason_code"] == "decision_slot_required"
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(aqa_audit_events)
                .where(aqa_audit_events.c.event_type == "execution.shadow_blocked")
            )
            == 1
        )


def test_shadow_rejects_fixture_without_explicit_mode(engine: Engine) -> None:
    with pytest.raises(ValueError, match="exact diagnostic mode"):
        run_shadow_once(engine=engine, settings=settings(), slot_id=None, now=NOW)


def test_shadow_persists_idempotent_dry_plan_without_broker(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adaptive_trader.platform.execution.broker import DeterministicFakePaperBroker

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("shadow constructed a broker")

    monkeypatch.setattr(DeterministicFakePaperBroker, "__init__", forbidden)
    slot = seed(engine)
    now = slot.ready_at + timedelta(seconds=4)
    result = run_shadow_once(
        engine=engine, settings=settings(), slot_id=slot.slot_id, now=now, fixture=True
    )
    assert result["status"] == "dry_run_persisted"
    assert result["broker_constructed"] is False
    assert result["intent_count"] == 0
    assert (
        run_shadow_once(
            engine=engine, settings=settings(), slot_id=slot.slot_id, now=now, fixture=True
        )
        == result
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_execution_plans)) == 1


def test_shadow_rejects_expired_slot(engine: Engine) -> None:
    slot = seed(engine)
    result = run_shadow_once(
        engine=engine, settings=settings(), slot_id=slot.slot_id, now=slot.deadline_at, fixture=True
    )
    assert result["reason_code"] == "decision_slot_not_current"


def test_shadow_import_has_no_broker_modules() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import adaptive_trader.platform.shadow; assert not any(name in sys.modules for name in ('adaptive_trader.platform.execution.broker', 'adaptive_trader.platform.execution.alpaca_paper', 'alpaca.trading.client'))",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_shadow_cli_missing_runtime_fails_instead_of_claiming_configuration_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from adaptive_trader.platform.cli import app

    monkeypatch.delenv("AQA_DATABASE_URL_FILE", raising=False)
    result = CliRunner().invoke(
        app, ["shadow", "run-once", "--config-root", str(ROOT / "configs"), "--json"]
    )
    assert result.exit_code == 2
    assert "diagnostic evaluation failed" in result.output
    assert "configuration only" not in result.output


@pytest.mark.parametrize("role", ("aqa_strategy", "aqa_control", "collector_test"))
def test_shadow_execution_rejects_cross_role_database_credentials(role: str) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from adaptive_trader.platform.shadow_settings import require_shadow_database_role

    connection = SimpleNamespace(scalar=lambda _statement: role)
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"), connect=lambda: nullcontext(connection)
    )
    with pytest.raises(ValueError, match="process authority"):
        require_shadow_database_role(engine, role="aqa_execution", fixture=False)


def test_shadow_adapter_rejects_strategy_settings_and_provider_secrets() -> None:
    from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
    from adaptive_trader.platform.errors import RuntimeSettingsError
    from adaptive_trader.platform.shadow_settings import ShadowExecutionSettings

    ordinary = load_runtime_settings(
        {}, service=RuntimeService.STRATEGY_WORKER, application_root=ROOT
    )
    with pytest.raises(TypeError, match="database-only"):
        ShadowExecutionSettings(ordinary, fixture=True)
    with pytest.raises(RuntimeSettingsError):
        load_shadow_execution_settings(
            {"AQA_ALPACA_PAPER_API_KEY_FILE": "/run/secrets/paper_key"},
            application_root=ROOT,
            fixture=True,
        )


def test_operational_strategy_dispatch_accepts_shadow_database_only_settings() -> None:
    from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
    from adaptive_trader.platform.operational_strategy import OperationalStrategyCycle
    from adaptive_trader.platform.service_cycles import build_worker_cycle

    configured = load_runtime_settings(
        {
            "AQA_CONFIG": "configs/platform/shadow.yaml",
            "AQA_DATABASE_URL_FILE": "/run/secrets/database_url",
        },
        service=RuntimeService.STRATEGY_WORKER,
        application_root=ROOT,
    )
    engine = create_engine("sqlite://")
    try:
        assert isinstance(build_worker_cycle(configured, engine), OperationalStrategyCycle)
        with pytest.raises(ValueError, match="requires PostgreSQL"):
            build_worker_cycle(configured, engine).run_cycle()
    finally:
        engine.dispose()
