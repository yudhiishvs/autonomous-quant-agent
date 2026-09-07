"""Guarded PostgreSQL integration coverage for signed execution persistence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.migrations import (
    _alembic_config,
    database_revision,
    upgrade_database,
)
from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as COLLECTION_SCHEMA
from adaptive_trader.platform.execution import (
    DeterministicFakePaperBroker,
    FakeBrokerScenario,
    Position,
    plan_signed_orders,
    reconcile,
)
from adaptive_trader.platform.risk.latches import RiskLatchState
from adaptive_trader.platform.storage.execution import SignedExecutionRepository
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA
from tests.unit.test_platform_execution_persistence import (
    _fake_submission_authority,
    _reconciliation_request,
    _seed_risk_decision,
)
from tests.unit.test_platform_execution_planner import planning_request

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

_DATABASE_URL = os.environ.get("APA_TEST_POSTGRES_URL", "").strip()
if not _DATABASE_URL:
    pytest.skip(
        "APA_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
        allow_module_level=True,
    )
if os.environ.get("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE") != "YES":
    raise RuntimeError(
        "PostgreSQL integration tests require APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES"
    )
_TEST_DATABASE = normalize_postgres_url(_DATABASE_URL)
if _TEST_DATABASE.host not in {"127.0.0.1", "::1", "localhost"}:
    raise RuntimeError("PostgreSQL integration tests require a loopback database host")
if _TEST_DATABASE.database != "collector_test":
    raise RuntimeError("PostgreSQL integration tests require the collector_test database")

_NOW = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)


def _signed_plan(
    engine: Engine,
    *,
    current: tuple[tuple[str, Decimal], ...],
    target_weights: tuple[tuple[str, Decimal], ...],
    slot_fragment: str,
    signal_fragment: str,
    correlation_fragment: str,
    signal_hash: str,
    decided_at: datetime,
    include_experiment: bool,
) -> Any:
    request = planning_request(current=current, target_weights=target_weights)
    original = request.risk_decision
    decision = original.create(
        slot_id=f"slot_{slot_fragment * 64}",
        signal_id=f"signal_{signal_fragment * 64}",
        signal_hash=signal_hash,
        experiment_hash=original.experiment_hash,
        policy_id=original.policy_id,
        policy_version=original.policy_version,
        policy_hash=original.policy_hash,
        correlation_id=f"correlation_{correlation_fragment * 64}",
        decided_at=decided_at,
        input_hash=original.input_hash,
        statistics_hash=original.statistics_hash,
        account_snapshot=original.account_snapshot,
        planning_positions=original.planning_positions,
        planning_prices=original.planning_prices,
        security_metadata=original.security_metadata,
        original_proposal=original.original_proposal,
        proposed_targets=original.proposed_targets,
        final_targets=original.final_targets,
        before_exposure=original.before_exposure,
        after_exposure=original.after_exposure,
        ordered_controls=original.ordered_controls,
        block_reasons=original.block_reasons,
        flatten_reasons=original.flatten_reasons,
        source_timestamps=original.source_timestamps,
        latch_state_hash=RiskLatchState.empty(
            experiment_hash=original.experiment_hash
        ).content_hash,
        active_latches=original.active_latches,
        required_latch_events=original.required_latch_events,
        execution_scope=original.execution_scope,
    )
    _seed_risk_decision(engine, decision, include_experiment=include_experiment)
    return plan_signed_orders(
        replace(
            request,
            risk_decision=decision,
            created_at=decided_at,
            deadline_at=decided_at + timedelta(minutes=1),
        )
    )


def _engine(*, application_name: str) -> Engine:
    return create_engine(
        _TEST_DATABASE,
        hide_parameters=True,
        connect_args=postgres_connect_args(application_name, migration=True),
    )


def _drop_disposable_test_schemas() -> None:
    engine = _engine(application_name="platform-execution-test-reset")
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(COLLECTION_SCHEMA, cascade=True, if_exists=True))
    finally:
        engine.dispose()


@pytest.fixture
def empty_database() -> Iterator[str]:
    _drop_disposable_test_schemas()
    try:
        yield _DATABASE_URL
    finally:
        _drop_disposable_test_schemas()


@pytest.fixture
def migrated_engine(empty_database: str) -> Iterator[Engine]:
    upgrade_database(empty_database)
    engine = _engine(application_name="platform-execution-persistence-test")
    try:
        yield engine
    finally:
        engine.dispose()


def test_signed_execution_survives_restart_and_converges_broker_replay(
    migrated_engine: Engine,
) -> None:
    signed = _signed_plan(
        migrated_engine,
        current=(("AAA", Decimal(0)),),
        target_weights=(("AAA", Decimal("0.10")),),
        slot_fragment="1",
        signal_fragment="2",
        correlation_fragment="d",
        signal_hash="a" * 64,
        decided_at=_NOW,
        include_experiment=True,
    )
    repository = SignedExecutionRepository(migrated_engine)

    repository.persist_plan_and_intents(
        signed.plan,
        signed.intents,
        risk_decision=signed.risk_decision,
    )
    restarted = SignedExecutionRepository(migrated_engine)
    restarted.persist_plan_and_intents(
        signed.plan,
        signed.intents,
        risk_decision=signed.risk_decision,
    )
    intent = signed.intents[0]
    broker = DeterministicFakePaperBroker(initial_time=_NOW)
    broker.set_mark_prices((("AAA", Decimal("100")),))
    restarted.record_submission_started(
        intent.client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(restarted, broker, intent.client_order_id),
    )
    update = broker.submit(intent, submitted_at=_NOW)

    first = restarted.apply_broker_update(update)
    replayed = SignedExecutionRepository(migrated_engine).apply_broker_update(update)

    assert replayed == first
    assert first.state.value == "FILLED"
    assert len(restarted.all_intents()) == 1
    assert len(restarted.all_orders()) == 1
    assert len(restarted.order_events()) == 2
    assert len(restarted.fills()) == 1
    # Immutable experiment registration contributes its own audit event.
    assert AuditRepository(migrated_engine).verify().event_count == 5


def test_postgres_reconciliation_separates_prior_active_order_fill_evidence(
    migrated_engine: Engine,
) -> None:
    opening = _signed_plan(
        migrated_engine,
        current=(("AAA", Decimal(0)),),
        target_weights=(("AAA", Decimal("0.10")),),
        slot_fragment="1",
        signal_fragment="2",
        correlation_fragment="d",
        signal_hash="a" * 64,
        decided_at=_NOW,
        include_experiment=True,
    )
    repository = SignedExecutionRepository(migrated_engine)
    repository.persist_plan_and_intents(
        opening.plan,
        opening.intents,
        risk_decision=opening.risk_decision,
    )
    broker = DeterministicFakePaperBroker(
        initial_time=_NOW,
        initial_cash=Decimal("1000"),
        default_scenario=FakeBrokerScenario.PARTIAL_FILL,
    )
    broker.set_mark_prices((("AAA", Decimal("100")),))
    prior_intent = opening.intents[0]
    repository.record_submission_started(
        prior_intent.client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(repository, broker, prior_intent.client_order_id),
    )
    prior_order = repository.apply_broker_update(broker.submit(prior_intent, submitted_at=_NOW))

    current = _signed_plan(
        migrated_engine,
        current=(("AAA", Decimal("0.5")),),
        target_weights=(("AAA", Decimal("0.10")),),
        slot_fragment="3",
        signal_fragment="4",
        correlation_fragment="5",
        signal_hash="e" * 64,
        decided_at=_NOW + timedelta(minutes=1),
        include_experiment=False,
    )
    repository.persist_plan_and_intents(
        current.plan,
        current.intents,
        risk_decision=current.risk_decision,
    )
    request = _reconciliation_request(
        repository,
        current,
        offset=61,
        observed_positions=broker.positions(),
    )
    request = replace(
        request,
        broker_orders=(prior_order,),
        broker_account=broker.account(observed_at=request.completed_at),
    )
    receipt = reconcile(request)

    repository.record_reconciliation_bundle(
        receipt,
        request=request,
        latch_event=None,
        incident=None,
    )

    assert request.fills == ()
    assert request.order_fills == repository.fills()
    assert receipt.status.value == "CLEAN"
    assert receipt.expected_positions == (Position("AAA", Decimal("0.5")),)
    assert receipt.expected_cash == Decimal("950")


def test_revision_eight_refuses_nonempty_provisional_execution_tables(
    empty_database: str,
) -> None:
    config = _alembic_config(empty_database)
    command.upgrade(config, "20260905_0007")
    engine = _engine(application_name="platform-execution-migration-guard-test")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO aqa.aqa_incidents (
                        incident_id, idempotency_key, experiment_hash, incident_type,
                        severity, status, reason_code, details, opened_at, resolved_at,
                        content_hash, version
                    ) VALUES (
                        'legacy-incident', 'legacy-incident-key', NULL, 'legacy',
                        'warning', 'open', 'migration_guard', '{}'::jsonb, :now, NULL,
                        :content_hash, 1
                    )
                    """
                ),
                {"now": _NOW, "content_hash": "f" * 64},
            )

        with pytest.raises(DBAPIError, match="explicit signed-execution backfill"):
            command.upgrade(config, "20260905_0008")

        current, expected = database_revision(empty_database)
        assert current == "20260905_0007"
        assert expected == "20260906_0015"
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM aqa.aqa_incidents")) == 1
    finally:
        engine.dispose()


def test_postgres_reversal_keeps_close_intermediate_and_persists_fresh_stage(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adaptive_trader.platform.signals import SignalEnvelopeRepository
    from adaptive_trader.platform.storage.experiments import ExperimentRepository
    from adaptive_trader.platform.storage.risk import SignedRiskRepository
    from tests.unit.test_platform_worker_cycles import SERVICES, _reversal_signal, _run, _settings

    ExperimentRepository(migrated_engine).register(
        _settings(SERVICES[0]).platform.experiment.definition,
        registered_at=_NOW,
    )
    _run(migrated_engine, SERVICES[0])
    _reversal_signal(migrated_engine, monkeypatch)
    assert _run(migrated_engine, SERVICES[3]).reason_code == "reversal_close_reconciled"
    assert _run(migrated_engine, SERVICES[1]).reason_code == "execution_pending"
    repository = SignedExecutionRepository(migrated_engine)
    close = max(repository.reconciliations(), key=lambda row: row.completed_at)
    assert close.expected_positions == ()
    assert _run(migrated_engine, SERVICES[3]).reason_code == "fake_execution_reconciled"
    assert _run(migrated_engine, SERVICES[1]).reason_code == "strategy_slot_completed"
    signal = SignalEnvelopeRepository(migrated_engine).get_for_slot(close.slot_id)
    risk = SignedRiskRepository(migrated_engine)
    first = risk.decision_for_signal(signal.signal_id)
    second = risk.decision_for_signal(signal.signal_id, execution_stage=2)
    assert first.risk_decision_id != second.risk_decision_id
    assert second.decided_at > close.completed_at
    assert risk.decision_by_id(first.risk_decision_id) == first
