"""Exercise the deployed offline workers against real durable state and a fake broker."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine

from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.execution import ExecutionService, OrderState
from adaptive_trader.platform.scheduling import DecisionSlotRepository, SlotState
from adaptive_trader.platform.service_cycles import WorkerCycleState, build_worker_cycle
from adaptive_trader.platform.storage.execution import SignedExecutionRepository
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.tables import metadata

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
SERVICES = (
    RuntimeService.MARKET_DATA_WORKER,
    RuntimeService.SCHEDULER_WORKER,
    RuntimeService.STRATEGY_WORKER,
    RuntimeService.EXECUTION_WORKER,
)


def _engine(path: Path) -> Engine:
    return create_engine(f"sqlite:///{path}").execution_options(schema_translate_map={"aqa": None})


def _settings(service: RuntimeService):
    return load_runtime_settings({}, service=service, application_root=ROOT)


def _run(engine: Engine, service: RuntimeService):
    # A fresh composition on every invocation models independent processes and restarts.
    return build_worker_cycle(_settings(service), engine).run_cycle()


@pytest.fixture(scope="module")
def market_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("worker-seed") / "seed.sqlite3"
    engine = _engine(path)
    metadata.create_all(engine)
    ExperimentRepository(engine).register(
        _settings(SERVICES[0]).platform.experiment.definition, registered_at=NOW
    )
    assert _run(engine, SERVICES[0]).state is WorkerCycleState.PROGRESSED
    engine.dispose()
    return path


@pytest.fixture
def engine(market_fixture: Path, tmp_path: Path) -> Iterator[Engine]:
    path = tmp_path / "worker.sqlite3"
    shutil.copyfile(market_fixture, path)
    result = _engine(path)
    yield result
    result.dispose()


def _slots(engine: Engine):
    return DecisionSlotRepository(engine).list_for_session(
        experiment_hash=_settings(SERVICES[0]).platform.experiment.definition.content_hash,
        session_date=date(2026, 7, 6),
    )


def _first_signal(engine: Engine) -> None:
    assert _run(engine, SERVICES[1]).reason_code == "strategy_slot_claimed"
    assert _run(engine, SERVICES[2]).reason_code == "signal_persisted"


def test_workers_preserve_cash_positions_and_flatten_across_complete_session(
    engine: Engine,
) -> None:
    _first_signal(engine)
    assert _run(engine, SERVICES[1]).reason_code == "execution_pending"
    assert _run(engine, SERVICES[3]).reason_code == "fake_execution_reconciled"
    repository = SignedExecutionRepository(engine)
    opening = repository.reconciliations()[0]
    assert len(opening.expected_positions) == 2
    assert len(repository.fills()) == 2
    for _ in range(45):
        for service in SERVICES[1:]:
            assert _run(engine, service).state is not WorkerCycleState.BLOCKED
    assert {slot.state for slot in _slots(engine)} == {SlotState.COMPLETED}
    receipts = sorted(repository.reconciliations(), key=lambda row: row.completed_at)
    assert len(receipts) == 21
    assert all(row.status.value == "CLEAN" for row in receipts)
    assert len(repository.fills()) == 6  # Opening pair and four capped closing slices.
    assert receipts[1].expected_positions == ()
    assert receipts[1].expected_cash == Decimal("99983.57906650")
    assert receipts[-1].expected_cash == receipts[1].expected_cash
    assert receipts[-1].expected_positions == ()
    assert repository.get_plan(receipts[-1].execution_plan_id).forced_flat
    before = tuple(row.content_hash for row in receipts)
    for service in SERVICES:
        assert _run(engine, service).state is WorkerCycleState.IDLE
    assert (
        tuple(
            row.content_hash
            for row in sorted(repository.reconciliations(), key=lambda row: row.completed_at)
        )
        == before
    )


def test_forced_flat_slot_closes_positions_when_intermediate_decisions_are_skipped(
    engine: Engine,
) -> None:
    _first_signal(engine)
    _run(engine, SERVICES[3])
    _run(engine, SERVICES[1])
    slots = DecisionSlotRepository(engine)
    for slot in _slots(engine):
        if slot.state is SlotState.PENDING:
            slots.skip(slot.slot_id, reason_code="operator_skipped", now=slot.ready_at)
    assert _run(engine, SERVICES[1]).reason_code == "forced_flat_claimed"
    assert _run(engine, SERVICES[2]).reason_code == "signal_persisted"
    assert _run(engine, SERVICES[3]).reason_code == "fake_execution_reconciled"
    assert _run(engine, SERVICES[1]).reason_code == "forced_flat_completed"
    repository = SignedExecutionRepository(engine)
    final = max(repository.reconciliations(), key=lambda row: row.completed_at)
    assert final.expected_positions == ()
    flatten = repository.intents_for_plan(final.execution_plan_id)
    assert len(flatten) == 4
    assert all(intent.forced_flat for intent in flatten)
    assert len(repository.fills()) == 6


@pytest.mark.parametrize("crash_after_first_fill", (False, True))
def test_worker_recovers_interrupted_execution_without_duplicate_fills(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, crash_after_first_fill: bool
) -> None:
    _first_signal(engine)
    if crash_after_first_fill:
        original = ExecutionService.submit_one

        def interrupted(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected process interruption after durable fill")

        monkeypatch.setattr(ExecutionService, "submit_one", interrupted)
    else:

        def interrupted_plan(self, result, **kwargs):
            self.persist(result)
            raise RuntimeError("injected process interruption before submission")

        monkeypatch.setattr(ExecutionService, "submit_plan", interrupted_plan)
    with pytest.raises(RuntimeError, match="injected process interruption"):
        _run(engine, SERVICES[3])
    repository = SignedExecutionRepository(engine)
    assert len(repository.fills()) == int(crash_after_first_fill)
    monkeypatch.undo()
    assert _run(engine, SERVICES[3]).reason_code == "fake_execution_reconciled"
    assert len(repository.fills()) == 2
    assert all(order.state is OrderState.FILLED for order in repository.all_orders())
    assert _run(engine, SERVICES[3]).state is WorkerCycleState.IDLE
    assert len(repository.fills()) == 2


def _reversal_signal(engine: Engine, monkeypatch: pytest.MonkeyPatch):
    from adaptive_trader.platform.signals import (
        OfflineFixtureSignalProvider,
        SignalAction,
        SignalEnvelope,
        SignalSourceMode,
    )

    original = OfflineFixtureSignalProvider.signal_for

    def opposite(self, context):
        if context.strategy_slot_ordinal != 1:
            return original(self, context)
        symbols = context.active_symbols
        actions = tuple(
            SignalAction.SHORT
            if symbol == symbols[-2]
            else SignalAction.LONG
            if symbol == symbols[1]
            else SignalAction.FLAT
            for symbol in symbols
        )
        return SignalEnvelope.create(
            context=context,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            provider_source_mode=SignalSourceMode.OFFLINE_FIXTURE,
            created_at=context.slot.ready_at + timedelta(seconds=3),
            availability_mask=(True,) * len(symbols),
            actions=actions,
            expected_edge_bps=tuple(
                Decimal(-25)
                if action is SignalAction.SHORT
                else Decimal(25)
                if action is SignalAction.LONG
                else None
                for action in actions
            ),
            proposed_signed_target_inputs=(None,) * len(symbols),
            promotable=False,
            paper_submission_eligible=False,
        )

    monkeypatch.setattr(OfflineFixtureSignalProvider, "signal_for", opposite)
    _first_signal(engine)
    assert _run(engine, SERVICES[3]).reason_code == "fake_execution_reconciled"
    assert _run(engine, SERVICES[1]).reason_code == "strategy_slot_completed"
    assert _run(engine, SERVICES[1]).reason_code == "strategy_slot_claimed"
    assert _run(engine, SERVICES[2]).reason_code == "signal_persisted"


@pytest.mark.parametrize(
    "crash_boundary", ("after_close", "after_risk", "after_intent", "after_fill")
)
def test_reversal_restarts_with_new_risk_and_exactly_one_opening_stage(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    crash_boundary: str,
) -> None:
    from adaptive_trader.platform.signals import SignalEnvelopeRepository
    from adaptive_trader.platform.storage.risk import SignedRiskRepository

    _reversal_signal(engine, monkeypatch)
    assert _run(engine, SERVICES[3]).reason_code == "reversal_close_reconciled"
    repository = SignedExecutionRepository(engine)
    close = max(repository.reconciliations(), key=lambda receipt: receipt.completed_at)
    assert close.expected_positions == ()
    assert _run(engine, SERVICES[1]).reason_code == "execution_pending"
    initial_fill_count = len(repository.fills())
    with monkeypatch.context() as crash:
        if crash_boundary == "after_risk":
            original = SignedRiskRepository.persist

            def interrupted_risk(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise RuntimeError("interrupted reversal risk")

            crash.setattr(SignedRiskRepository, "persist", interrupted_risk)
        elif crash_boundary == "after_intent":

            def interrupted_intent(self, result, **kwargs):
                self.persist(result)
                raise RuntimeError("interrupted reversal intent")

            crash.setattr(ExecutionService, "submit_plan", interrupted_intent)
        elif crash_boundary == "after_fill":
            original_submit = ExecutionService.submit_one

            def interrupted_fill(self, *args, **kwargs):
                original_submit(self, *args, **kwargs)
                raise RuntimeError("interrupted reversal fill")

            crash.setattr(ExecutionService, "submit_one", interrupted_fill)
        if crash_boundary != "after_close":
            with pytest.raises(RuntimeError, match="interrupted reversal"):
                _run(engine, SERVICES[3])
    assert _run(engine, SERVICES[3]).reason_code == "fake_execution_reconciled"
    completed = max(repository.reconciliations(), key=lambda receipt: receipt.completed_at)
    plan = repository.get_plan(completed.execution_plan_id)
    assert plan.target_version == 2
    assert len(completed.expected_positions) == 2
    opening = min(repository.reconciliations(), key=lambda receipt: receipt.completed_at)
    first_positions = {
        position.symbol: position.quantity for position in opening.expected_positions
    }
    assert all(
        first_positions[position.symbol] * position.quantity < 0
        for position in completed.expected_positions
    )
    assert len(repository.fills()) == initial_fill_count + 2
    signal = SignalEnvelopeRepository(engine).get_for_slot(close.slot_id)
    risk = SignedRiskRepository(engine)
    first = risk.decision_for_signal(signal.signal_id)
    second = risk.decision_for_signal(signal.signal_id, execution_stage=2)
    assert first.risk_decision_id != second.risk_decision_id
    assert second.decided_at > close.completed_at
    assert all(position.quantity == 0 for position in second.planning_positions)
    assert _run(engine, SERVICES[1]).reason_code == "strategy_slot_completed"
    assert _run(engine, SERVICES[3]).state is WorkerCycleState.IDLE
    assert len(repository.fills()) == initial_fill_count + 2


@pytest.mark.parametrize(
    "invalid", ("missing_receipt", "wrong_receipt", "stale", "not_flat", "expired")
)
def test_reversal_continuation_rejects_missing_or_stale_close_authority(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    import inspect
    from dataclasses import replace

    from adaptive_trader.platform.risk.models import RiskDecision
    from adaptive_trader.platform.storage.risk import RiskPersistenceError, SignedRiskRepository

    _reversal_signal(engine, monkeypatch)
    assert _run(engine, SERVICES[3]).reason_code == "reversal_close_reconciled"
    repository = SignedExecutionRepository(engine)
    receipts = sorted(repository.reconciliations(), key=lambda row: row.completed_at)
    close = receipts[-1]
    original = SignedRiskRepository.persist

    def rejected(self, decision, *, preceding_reconciliation_id=None):
        values = {
            name: getattr(decision, name)
            for name in inspect.signature(RiskDecision.create).parameters
        }
        receipt_id = preceding_reconciliation_id
        if invalid == "missing_receipt":
            receipt_id = "reconciliation_" + "0" * 64
        elif invalid == "wrong_receipt":
            receipt_id = receipts[0].reconciliation_id
        elif invalid == "stale":
            values["security_metadata"] = tuple(
                replace(item, observed_at=close.completed_at - timedelta(seconds=1))
                for item in decision.security_metadata
            )
        elif invalid == "not_flat":
            values["planning_positions"] = tuple(
                replace(item, quantity=Decimal(1)) for item in decision.planning_positions
            )
        else:
            values["decided_at"] = repository.get_plan(close.execution_plan_id).deadline_at
        with pytest.raises(RiskPersistenceError, match="reversal"):
            original(self, RiskDecision.create(**values), preceding_reconciliation_id=receipt_id)
        raise RuntimeError("negative boundary verified")

    monkeypatch.setattr(SignedRiskRepository, "persist", rejected)
    fill_count = len(repository.fills())
    with pytest.raises(RuntimeError, match="negative boundary verified"):
        _run(engine, SERVICES[3])
    assert len(repository.fills()) == fill_count
    assert len(repository.reconciliations()) == len(receipts)
    assert _run(engine, SERVICES[1]).reason_code == "execution_pending"


def test_history_seed_resumes_committed_intervals_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import func, select

    from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
    from adaptive_trader.platform.service_cycles import _seed_offline_risk_history
    from adaptive_trader.platform.storage.market_data import MarketDataRepository
    from adaptive_trader.platform.storage.tables import aqa_bar_events

    engine = _engine(tmp_path / "interrupted-history.sqlite3")
    metadata.create_all(engine)
    repository = MarketDataRepository(engine)
    original = MarketDataRepository.append

    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("history seed interrupted")

    with monkeypatch.context() as crash:
        crash.setattr(MarketDataRepository, "append", interrupted)
        with pytest.raises(RuntimeError, match="history seed interrupted"):
            _seed_offline_risk_history(
                repository=repository,
                calendar=XnasExchangeCalendar(),
                active_symbols=("AAA",),
            )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 1
    assert (
        _seed_offline_risk_history(
            repository=repository,
            calendar=XnasExchangeCalendar(),
            active_symbols=("AAA",),
        )
        == 519
    )
    assert (
        _seed_offline_risk_history(
            repository=repository,
            calendar=XnasExchangeCalendar(),
            active_symbols=("AAA",),
        )
        == 0
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_bar_events)) == 520
    engine.dispose()
