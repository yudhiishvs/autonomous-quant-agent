"""Atomic, restart-safe persistence tests for the signed execution ledger."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, func, insert, select, update

from adaptive_trader.platform.execution import (
    AccountState,
    BrokerUpdate,
    DeterministicFakePaperBroker,
    ExecutionPlan,
    ExecutionValidationError,
    FakeBrokerScenario,
    Incident,
    Position,
    ReconciliationRequest,
    SubmissionAuthoritySnapshot,
    SubmissionSafetySnapshot,
    plan_signed_orders,
    reconcile,
)
from adaptive_trader.platform.risk.latches import (
    RiskLatchKind,
    RiskLatchState,
    create_latch_engagement,
)
from adaptive_trader.platform.storage.execution import (
    ExecutionPersistenceError,
    SignedExecutionRepository,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.risk import SignedRiskRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_audit_events,
    aqa_broker_orders,
    aqa_decision_slots,
    aqa_experiments,
    aqa_incidents,
    aqa_reconciliations,
    aqa_risk_latch_events,
    aqa_signal_envelopes,
    metadata,
)
from tests.unit.test_platform_execution_planner import planning_request

_NOW = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'execution.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        pool_pre_ping=True,
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def signed_plan(sqlite_engine: Engine):
    request = planning_request(
        current=(("AAA", Decimal(0)),),
        target_weights=(("AAA", Decimal("0.10")),),
    )
    request = replace(request, risk_decision=_with_empty_latch_state(request.risk_decision))
    _seed_risk_decision(sqlite_engine, request.risk_decision)
    return plan_signed_orders(request)


def _with_empty_latch_state(original: Any) -> Any:
    return original.create(
        slot_id=original.slot_id,
        signal_id=original.signal_id,
        signal_hash=original.signal_hash,
        experiment_hash=original.experiment_hash,
        policy_id=original.policy_id,
        policy_version=original.policy_version,
        policy_hash=original.policy_hash,
        correlation_id=original.correlation_id,
        decided_at=original.decided_at,
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


def _seed_risk_decision(
    engine: Engine,
    decision: Any,
    *,
    include_experiment: bool = True,
) -> None:
    source_start = decision.decided_at - timedelta(minutes=15)
    with engine.begin() as connection:
        if include_experiment:
            connection.execute(
                insert(aqa_experiments).values(
                    experiment_hash=decision.experiment_hash,
                    experiment_id="execution_repository_fixture",
                    experiment_version=1,
                    schema_version=1,
                    configuration={"mode": "offline_fixture"},
                    content_hash=decision.experiment_hash,
                    registered_at=source_start,
                )
            )
        connection.execute(
            insert(aqa_decision_slots).values(
                slot_id=decision.slot_id,
                experiment_hash=decision.experiment_hash,
                experiment_id="execution_repository_fixture",
                experiment_version=1,
                signal_provider_id="always_flat",
                signal_provider_version="1",
                session_date=date(2026, 7, 6),
                source_interval_start=source_start,
                source_interval_end=decision.decided_at,
                decision_type="strategy",
                ready_at=decision.decided_at,
                deadline_at=decision.decided_at + timedelta(minutes=1),
                required_completion_at=decision.decided_at + timedelta(minutes=2),
                state="READY",
                claim_owner=None,
                claimed_at=None,
                lease_expires_at=None,
                attempt_count=0,
                completed_at=None,
                reason_code=None,
                correlation_id=decision.correlation_id,
                content_hash="6" * 64,
                version=1,
                created_at=source_start,
                updated_at=source_start,
            )
        )
        connection.execute(
            insert(aqa_signal_envelopes).values(
                signal_id=decision.signal_id,
                slot_id=decision.slot_id,
                experiment_hash=decision.experiment_hash,
                provider_id="always_flat",
                provider_version="1",
                contract_version=1,
                correlation_id=decision.correlation_id,
                provider_source_mode="builtin",
                experiment_id="execution_repository_fixture",
                experiment_version=1,
                data_contract_hash="7" * 64,
                policy_hash=decision.policy_hash,
                source_bar_end=decision.decided_at,
                created_at=decision.decided_at,
                expires_at=decision.decided_at + timedelta(minutes=1),
                active_symbols=["AAA"],
                availability_mask=[True],
                actions=["LONG"],
                expected_edge_bps=[None],
                proposed_signed_target_inputs=["0.10"],
                artifact_id=None,
                artifact_hash=None,
                promotable=True,
                paper_submission_eligible=False,
                content_hash=decision.signal_hash,
            )
        )
    SignedRiskRepository(engine).persist(decision)


def _reconciliation_request(
    repository: SignedExecutionRepository,
    signed_plan: Any,
    *,
    offset: int = 0,
    observed_positions: tuple[Position, ...] = (),
) -> ReconciliationRequest:
    completed_at = _NOW + timedelta(seconds=offset + 1)
    account = AccountState(
        account_id="fake-paper-account-v1",
        cash=Decimal("1000"),
        equity=Decimal("1000")
        + sum(
            (position.quantity * Decimal("100") for position in observed_positions),
            start=Decimal(0),
        ),
        buying_power=Decimal("1000"),
        restricted_short_proceeds=Decimal(0),
        observed_at=completed_at,
    )
    plan_client_ids = {intent.client_order_id for intent in signed_plan.intents}
    return ReconciliationRequest(
        experiment_hash=signed_plan.plan.experiment_hash,
        slot_id=signed_plan.risk_decision.slot_id,
        execution_plan_id=signed_plan.plan.execution_plan_id,
        correlation_id=signed_plan.plan.correlation_id,
        active_symbols=("AAA",),
        short_eligible_symbols=("AAA",),
        baseline_positions=signed_plan.plan.current_positions,
        baseline_cash=signed_plan.risk_decision.account_snapshot.cash,
        fills=tuple(fill for fill in repository.fills() if fill.client_order_id in plan_client_ids),
        order_fills=repository.fills(),
        intents=repository.all_intents(),
        durable_orders=repository.all_orders(),
        broker_orders=(),
        broker_positions=observed_positions,
        broker_account=account,
        expected_account_id_hash=signed_plan.risk_decision.account_snapshot.account_id_hash,
        mark_prices=(("AAA", Decimal("100")),),
        started_at=completed_at - timedelta(seconds=1),
        completed_at=completed_at,
    )


def _fake_submission_authority(
    repository: SignedExecutionRepository,
    broker: DeterministicFakePaperBroker,
    client_order_id: str,
) -> SubmissionAuthoritySnapshot:
    safety = SubmissionSafetySnapshot(
        evaluated_at=_NOW,
        session_open=True,
        data_complete=True,
        account_observed_at=_NOW,
        security_observed_at=_NOW,
        reconciliation_observed_at=_NOW,
        price_observed_at=_NOW,
        reconciliation_clean=True,
        ambiguous_order_exists=False,
        blocking_latch_exists=False,
        entry_disabled=False,
        active_symbols=("AAA",),
        shortable_symbols=("AAA",),
    )
    return SubmissionAuthoritySnapshot.create(
        ledger=repository.submission_ledger_snapshot(client_order_id),
        safety=safety,
        account=broker.account(observed_at=_NOW),
        positions=broker.positions(),
        open_client_order_ids=broker.open_client_order_ids(),
    )


def test_plan_intents_and_projections_are_atomic_idempotent_and_restart_safe(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)

    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    restarted = SignedExecutionRepository(sqlite_engine)

    assert restarted.get_plan(signed_plan.plan.execution_plan_id) == signed_plan.plan
    assert restarted.all_intents() == signed_plan.intents
    assert restarted.get_order(signed_plan.intents[0].client_order_id).state.value == (
        "INTENT_COMMITTED"
    )
    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_broker_orders)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 2


def test_sqlite_persists_exact_decimal_intent_without_binary_float_rejection(
    sqlite_engine: Engine,
) -> None:
    request = planning_request(
        current=(("AAA", Decimal(0)),),
        target_weights=(("AAA", Decimal("0.15")),),
        prices=(("AAA", Decimal("98.65")),),
    )
    request = replace(request, risk_decision=_with_empty_latch_state(request.risk_decision))
    _seed_risk_decision(sqlite_engine, request.risk_decision)
    planned = plan_signed_orders(request)

    SignedExecutionRepository(sqlite_engine).persist_plan_and_intents(
        planned.plan,
        planned.intents,
        risk_decision=planned.risk_decision,
    )

    assert planned.intents[0].notional == (
        planned.intents[0].quantity * planned.intents[0].reference_price
    )


def test_concurrent_plan_retries_converge_without_duplicates(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(
            pool.map(
                lambda _: repository.persist_plan_and_intents(
                    signed_plan.plan,
                    signed_plan.intents,
                    risk_decision=signed_plan.risk_decision,
                ),
                range(2),
            )
        )

    assert repository.all_intents() == signed_plan.intents
    assert len(repository.all_orders()) == 1


def test_plan_is_rejected_when_authoritative_risk_hash_does_not_match(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    plan = ExecutionPlan.create(
        risk_decision_id=signed_plan.plan.risk_decision_id,
        risk_decision_hash="f" * 64,
        experiment_hash=signed_plan.plan.experiment_hash,
        correlation_id=signed_plan.plan.correlation_id,
        target_version=2,
        forced_flat=False,
        target_quantities=signed_plan.plan.target_quantities,
        current_positions=signed_plan.plan.current_positions,
        reference_prices=signed_plan.plan.reference_prices,
        equity=signed_plan.plan.equity,
        created_at=signed_plan.plan.created_at,
        deadline_at=signed_plan.plan.deadline_at,
    )

    with pytest.raises(ExecutionPersistenceError, match="signed risk"):
        SignedExecutionRepository(sqlite_engine).persist_plan_and_intents(
            plan,
            (),
            risk_decision=signed_plan.risk_decision,
        )

    assert SignedExecutionRepository(sqlite_engine).all_intents() == ()


def test_broker_transition_and_fills_are_atomic_and_idempotent(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    intent = signed_plan.intents[0]
    broker = DeterministicFakePaperBroker(initial_time=_NOW)
    broker.set_mark_prices((("AAA", Decimal("100")),))

    repository.record_submission_started(
        intent.client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(repository, broker, intent.client_order_id),
    )
    update_value = broker.submit(intent, submitted_at=_NOW)
    first = repository.apply_broker_update(update_value)
    repeated = repository.apply_broker_update(update_value)

    assert repeated == first
    assert first.state.value == "FILLED"
    assert len(repository.order_events()) == 2
    assert len(repository.fills()) == 1
    assert SignedExecutionRepository(sqlite_engine).get_order(intent.client_order_id) == first


def test_duplicate_execution_id_with_different_content_fails_closed(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    intent = signed_plan.intents[0]
    broker = DeterministicFakePaperBroker(initial_time=_NOW)
    broker.set_mark_prices((("AAA", Decimal("100")),))
    repository.record_submission_started(
        intent.client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(repository, broker, intent.client_order_id),
    )
    update_value = broker.submit(intent, submitted_at=_NOW)
    repository.apply_broker_update(update_value)
    changed_fill = update_value.fills[0].create(
        client_order_id=intent.client_order_id,
        broker_execution_id=update_value.fills[0].broker_execution_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=update_value.fills[0].quantity,
        price=Decimal("101"),
        fee=Decimal(0),
        occurred_at=update_value.fills[0].occurred_at,
    )
    conflicting = BrokerUpdate(
        client_order_id=update_value.client_order_id,
        broker_order_id=update_value.broker_order_id,
        broker_event_id=update_value.broker_event_id,
        state=update_value.state,
        occurred_at=update_value.occurred_at,
        cumulative_filled_quantity=update_value.cumulative_filled_quantity,
        average_fill_price=update_value.average_fill_price,
        fills=(changed_fill,),
    )

    with pytest.raises(ExecutionValidationError, match="execution ID"):
        repository.apply_broker_update(conflicting)


def test_clean_reconciliation_is_append_once(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    request = _reconciliation_request(repository, signed_plan)
    receipt = reconcile(request)

    repository.record_reconciliation_bundle(
        receipt,
        request=request,
        latch_event=None,
        incident=None,
    )
    repository.record_reconciliation_bundle(
        receipt,
        request=request,
        latch_event=None,
        incident=None,
    )

    assert repository.reconciliations() == (receipt,)
    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_reconciliations)) == 1


def test_reconciliation_separates_current_plan_deltas_from_prior_active_order_fills(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    prior_intent = signed_plan.intents[0]
    broker = DeterministicFakePaperBroker(
        initial_time=_NOW,
        initial_cash=Decimal("1000"),
        default_scenario=FakeBrokerScenario.PARTIAL_FILL,
    )
    broker.set_mark_prices((("AAA", Decimal("100")),))
    repository.record_submission_started(
        prior_intent.client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(repository, broker, prior_intent.client_order_id),
    )
    prior_order = repository.apply_broker_update(broker.submit(prior_intent, submitted_at=_NOW))
    assert prior_order.state.value == "PARTIALLY_FILLED"

    next_request = planning_request(
        current=(("AAA", Decimal("0.5")),),
        target_weights=(("AAA", Decimal("0.10")),),
    )
    original = next_request.risk_decision
    next_created_at = _NOW + timedelta(minutes=1)
    next_decision = original.create(
        slot_id=f"slot_{'3' * 64}",
        signal_id=f"signal_{'4' * 64}",
        signal_hash="e" * 64,
        experiment_hash=original.experiment_hash,
        policy_id=original.policy_id,
        policy_version=original.policy_version,
        policy_hash=original.policy_hash,
        correlation_id=f"correlation_{'5' * 64}",
        decided_at=next_created_at,
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
    next_signed = plan_signed_orders(
        replace(
            next_request,
            risk_decision=next_decision,
            created_at=next_created_at,
            deadline_at=next_created_at + timedelta(minutes=1),
        )
    )
    _seed_risk_decision(sqlite_engine, next_decision, include_experiment=False)
    repository.persist_plan_and_intents(
        next_signed.plan,
        next_signed.intents,
        risk_decision=next_decision,
    )
    request = _reconciliation_request(
        repository,
        next_signed,
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


def test_blocking_reconciliation_persists_latch_incident_and_receipt_atomically(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    request = _reconciliation_request(
        repository,
        signed_plan,
        observed_positions=(Position("AAA", Decimal(1)),),
    )
    receipt = reconcile(request)
    latch = create_latch_engagement(
        latch_state=RiskLatchState.empty(experiment_hash=receipt.experiment_hash),
        latch_type=RiskLatchKind.RECONCILIATION,
        reason_code="reconciliation_blocking",
        actor="execution_worker",
        occurred_at=receipt.completed_at,
        correlation_id=receipt.correlation_id,
        idempotency_key=f"reconciliation_{receipt.content_hash[:32]}",
    )
    incident = Incident.create(
        idempotency_key=f"reconciliation:{receipt.content_hash[:32]}",
        experiment_hash=receipt.experiment_hash,
        correlation_id=receipt.correlation_id,
        reason_code="reconciliation_blocking",
        opened_at=receipt.completed_at,
    )

    with pytest.raises(ExecutionValidationError, match="requires an incident"):
        repository.record_reconciliation_bundle(
            receipt,
            request=request,
            latch_event=latch,
            incident=None,
        )
    repository.record_reconciliation_bundle(
        receipt,
        request=request,
        latch_event=latch,
        incident=incident,
    )
    repository.record_reconciliation_bundle(
        receipt,
        request=request,
        latch_event=latch,
        incident=incident,
    )

    assert repository.reconciliations() == (receipt,)
    assert repository.incidents() == (incident,)
    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_incidents)) == 1


def test_standalone_incident_is_idempotent_and_appends_one_audit_event(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    incident = Incident.create(
        idempotency_key=f"flatten_failure_{'9' * 64}",
        experiment_hash=signed_plan.plan.experiment_hash,
        correlation_id=signed_plan.plan.correlation_id,
        reason_code="forced_flat_planning_failed",
        opened_at=_NOW,
    )

    assert repository.record_incident(incident) == incident
    assert repository.record_incident(incident) == incident
    assert repository.incidents() == (incident,)
    with sqlite_engine.begin() as connection:
        incident_count = connection.scalar(select(func.count()).select_from(aqa_incidents))
        audit_count = connection.scalar(
            select(func.count())
            .select_from(aqa_audit_events)
            .where(aqa_audit_events.c.event_type == "incident.opened")
        )
    assert incident_count == 1
    assert audit_count == 1


def test_later_blocking_reconciliation_reuses_an_active_latch(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    first_request = _reconciliation_request(
        repository,
        signed_plan,
        observed_positions=(Position("AAA", Decimal(1)),),
    )
    first = reconcile(first_request)
    latch = create_latch_engagement(
        latch_state=RiskLatchState.empty(experiment_hash=first.experiment_hash),
        latch_type=RiskLatchKind.RECONCILIATION,
        reason_code="reconciliation_blocking",
        actor="execution_worker",
        occurred_at=first.completed_at,
        correlation_id=first.correlation_id,
        idempotency_key=f"reconciliation_{first.content_hash[:32]}",
    )
    first_incident = Incident.create(
        idempotency_key=f"reconciliation:{first.content_hash[:32]}",
        experiment_hash=first.experiment_hash,
        correlation_id=first.correlation_id,
        reason_code="reconciliation_blocking",
        opened_at=first.completed_at,
    )
    repository.record_reconciliation_bundle(
        first,
        request=first_request,
        latch_event=latch,
        incident=first_incident,
    )
    second_request = _reconciliation_request(
        repository,
        signed_plan,
        offset=2,
        observed_positions=(Position("AAA", Decimal(1)),),
    )
    second = reconcile(second_request)
    second_incident = Incident.create(
        idempotency_key=f"reconciliation:{second.content_hash[:32]}",
        experiment_hash=second.experiment_hash,
        correlation_id=second.correlation_id,
        reason_code="reconciliation_blocking",
        opened_at=second.completed_at,
    )

    repository.record_reconciliation_bundle(
        second,
        request=second_request,
        latch_event=None,
        incident=second_incident,
    )

    assert repository.reconciliations() == tuple(
        sorted((first, second), key=lambda item: item.reconciliation_id)
    )
    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 1


def test_persisted_projection_tampering_is_detected(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    client_order_id = signed_plan.intents[0].client_order_id
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_broker_orders)
            .where(aqa_broker_orders.c.client_order_id == client_order_id)
            .values(content_hash="f" * 64)
        )

    with pytest.raises(ExecutionValidationError, match="content hash"):
        SignedExecutionRepository(sqlite_engine).get_order(client_order_id)


def test_execution_audit_chain_verifies_after_restart(
    sqlite_engine: Engine,
    signed_plan: Any,
) -> None:
    repository = SignedExecutionRepository(sqlite_engine)
    repository.persist_plan_and_intents(
        signed_plan.plan,
        signed_plan.intents,
        risk_decision=signed_plan.risk_decision,
    )
    broker = DeterministicFakePaperBroker(initial_time=_NOW)
    broker.set_mark_prices((("AAA", Decimal("100")),))
    repository.record_submission_started(
        signed_plan.intents[0].client_order_id,
        started_at=_NOW,
        authority=_fake_submission_authority(
            repository,
            broker,
            signed_plan.intents[0].client_order_id,
        ),
    )

    report = AuditRepository(sqlite_engine).verify()

    assert report.event_count == 3
