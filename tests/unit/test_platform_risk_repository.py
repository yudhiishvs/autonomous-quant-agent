"""Atomic persistence, replay, and tamper tests for signed-risk evidence."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, func, insert, select, update

from adaptive_trader.platform.risk.latches import (
    RiskLatchAction,
    RiskLatchEvent,
    RiskLatchKind,
    RiskLatchState,
)
from adaptive_trader.platform.risk.models import (
    RiskDecision,
    RiskExecutionScope,
    SignedRiskValidationError,
)
from adaptive_trader.platform.risk.policy import ExposureSnapshot
from adaptive_trader.platform.storage.risk import RiskPersistenceError, SignedRiskRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_audit_events,
    aqa_decision_slots,
    aqa_experiments,
    aqa_risk_decisions,
    aqa_risk_latch_events,
    aqa_signal_envelopes,
    metadata,
)

_NOW = datetime(2026, 9, 5, 14, 0, tzinfo=UTC)
_EXPERIMENT_HASH = "a" * 64
_SLOT_ID = f"slot_{'1' * 64}"
_SIGNAL_ID = f"signal_{'2' * 64}"
_CORRELATION_ID = f"correlation_{'3' * 64}"
_SIGNAL_HASH = "4" * 64
_POLICY_HASH = "5" * 64


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'risk.sqlite3'}",
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
    _seed_authoritative_signal(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def repository(sqlite_engine: Engine) -> SignedRiskRepository:
    return SignedRiskRepository(sqlite_engine)


def _seed_authoritative_signal(engine: Engine) -> None:
    source_start = _NOW - timedelta(minutes=15)
    ready_at = _NOW + timedelta(seconds=1)
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="risk_repository_fixture",
                experiment_version=1,
                schema_version=1,
                configuration={"mode": "offline_fixture"},
                content_hash=_EXPERIMENT_HASH,
                registered_at=source_start,
            )
        )
        connection.execute(
            insert(aqa_decision_slots).values(
                slot_id=_SLOT_ID,
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="risk_repository_fixture",
                experiment_version=1,
                signal_provider_id="always_flat",
                signal_provider_version="1",
                session_date=date(2026, 9, 5),
                source_interval_start=source_start,
                source_interval_end=_NOW,
                decision_type="strategy",
                ready_at=ready_at,
                deadline_at=ready_at + timedelta(minutes=1),
                required_completion_at=ready_at + timedelta(minutes=2),
                state="READY",
                claim_owner=None,
                claimed_at=None,
                lease_expires_at=None,
                attempt_count=0,
                completed_at=None,
                reason_code=None,
                correlation_id=_CORRELATION_ID,
                content_hash="6" * 64,
                version=1,
                created_at=source_start,
                updated_at=source_start,
            )
        )
        connection.execute(
            insert(aqa_signal_envelopes).values(
                signal_id=_SIGNAL_ID,
                slot_id=_SLOT_ID,
                experiment_hash=_EXPERIMENT_HASH,
                provider_id="always_flat",
                provider_version="1",
                contract_version=1,
                correlation_id=_CORRELATION_ID,
                provider_source_mode="builtin",
                experiment_id="risk_repository_fixture",
                experiment_version=1,
                data_contract_hash="7" * 64,
                policy_hash=_POLICY_HASH,
                source_bar_end=_NOW,
                created_at=ready_at,
                expires_at=ready_at + timedelta(minutes=1),
                active_symbols=["AMD"],
                availability_mask=[True],
                actions=["FLAT"],
                expected_edge_bps=[None],
                proposed_signed_target_inputs=["0"],
                artifact_id=None,
                artifact_hash=None,
                promotable=True,
                paper_submission_eligible=False,
                content_hash=_SIGNAL_HASH,
            )
        )


def _zero_exposure() -> ExposureSnapshot:
    return ExposureSnapshot(
        gross=Decimal(0),
        net=Decimal(0),
        positive=Decimal(0),
        short_abs=Decimal(0),
        group_gross=(),
        cluster_gross=(),
    )


def _latch_event(*, reason_code: str = "operator_requested") -> RiskLatchEvent:
    return RiskLatchEvent.create(
        experiment_hash=_EXPERIMENT_HASH,
        latch_type=RiskLatchKind.OPERATOR_HALT,
        sequence=1,
        action=RiskLatchAction.ENGAGED,
        reason_code=reason_code,
        actor="operator",
        occurred_at=_NOW + timedelta(seconds=2),
        correlation_id=_CORRELATION_ID,
        idempotency_key="operator_halt_fixture",
    )


def _decision(
    *,
    latch_state: RiskLatchState | None = None,
    required_latch_events: tuple[RiskLatchEvent, ...] = (),
) -> RiskDecision:
    state = latch_state or RiskLatchState.empty(experiment_hash=_EXPERIMENT_HASH)
    active = state.active
    if required_latch_events:
        active = tuple(sorted((*active, *(event.latch_type for event in required_latch_events))))
    blocked = bool(active)
    exposure = _zero_exposure()
    return RiskDecision.create(
        slot_id=_SLOT_ID,
        signal_id=_SIGNAL_ID,
        signal_hash=_SIGNAL_HASH,
        experiment_hash=_EXPERIMENT_HASH,
        policy_id="signed_risk",
        policy_version=1,
        policy_hash=_POLICY_HASH,
        correlation_id=_CORRELATION_ID,
        decided_at=_NOW + timedelta(seconds=3),
        input_hash="8" * 64,
        statistics_hash="9" * 64,
        original_proposal=(("AMD", "FLAT", Decimal(0), Decimal(0)),),
        proposed_targets=(("AMD", Decimal(0)),),
        final_targets=(("AMD", Decimal(0)),),
        before_exposure=exposure,
        after_exposure=exposure,
        ordered_controls=(),
        block_reasons=("active_risk_latch",) if blocked else (),
        flatten_reasons=("operator_halt",) if blocked else (),
        source_timestamps=(("signal_created", _NOW + timedelta(seconds=1)),),
        latch_state_hash=state.content_hash,
        active_latches=active,
        required_latch_events=required_latch_events,
        execution_scope=(
            RiskExecutionScope.RISK_REDUCING_ONLY if blocked else RiskExecutionScope.FULL
        ),
    )


def test_risk_decision_is_append_once_and_hash_verified(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    decision = _decision()

    assert repository.persist(decision) == decision
    assert repository.persist(decision) == decision
    assert repository.decision_for_signal(_SIGNAL_ID) == decision

    with sqlite_engine.begin() as connection:
        decision_count = connection.scalar(select(func.count()).select_from(aqa_risk_decisions))
        audit_count = connection.scalar(select(func.count()).select_from(aqa_audit_events))
    assert decision_count == 1
    assert audit_count == 1


def test_concurrent_identical_risk_retries_create_one_receipt(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    decision = _decision()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: repository.persist(decision), range(2)))

    assert results == (decision, decision)
    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_decisions)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 1


def test_required_latch_and_decision_are_atomic_and_restart_safe(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    event_to_append = _latch_event()
    decision = _decision(required_latch_events=(event_to_append,))

    assert repository.persist(decision) == decision
    restarted = SignedRiskRepository(sqlite_engine)
    assert restarted.latch_state(_EXPERIMENT_HASH).active == (RiskLatchKind.OPERATOR_HALT,)
    assert restarted.decision_for_signal(_SIGNAL_ID) == decision
    assert restarted.persist(decision) == decision

    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_risk_decisions)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 2


def test_stale_latch_state_rejects_decision_without_partial_write(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    stale_decision = _decision()
    repository.append_latch(_latch_event())

    with pytest.raises(RiskPersistenceError, match="stale latch state"):
        repository.persist(stale_decision)

    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_decisions)) == 0
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 1


def test_latch_idempotency_collision_is_rejected_without_duplication(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    first = _latch_event()
    conflicting = _latch_event(reason_code="different_reason")
    repository.append_latch(first)

    with pytest.raises(RiskPersistenceError, match="different content"):
        repository.append_latch(conflicting)

    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 1


def test_persisted_risk_tampering_is_detected(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    decision = _decision()
    repository.persist(decision)
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_risk_decisions)
            .where(aqa_risk_decisions.c.signal_id == _SIGNAL_ID)
            .values(proposed_targets=[["AMD", "0.25"]])
        )

    with pytest.raises((RiskPersistenceError, SignedRiskValidationError)):
        repository.decision_for_signal(_SIGNAL_ID)


def test_authoritative_signal_binding_cannot_be_substituted(
    repository: SignedRiskRepository,
    sqlite_engine: Engine,
) -> None:
    decision = _decision()
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_signal_envelopes)
            .where(aqa_signal_envelopes.c.signal_id == _SIGNAL_ID)
            .values(content_hash="f" * 64)
        )
    with pytest.raises(RiskPersistenceError, match="authoritative signal"):
        repository.persist(decision)

    with sqlite_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_decisions)) == 0
