"""Durable operator halt and resume adapter tests."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, func, insert, select

from adaptive_trader.platform.control import (
    OPERATOR_RESUME_ACKNOWLEDGEMENT,
    OperatorControlConflictError,
    OperatorControlError,
    SQLAlchemyOperatorControls,
)
from adaptive_trader.platform.control.models import OperatorHaltRequest, OperatorResumeRequest
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.errors import AuditPersistenceError
from adaptive_trader.platform.risk.latches import (
    RiskLatchKind,
    RiskLatchState,
    create_latch_engagement,
)
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_audit_events,
    aqa_experiments,
    aqa_risk_latch_events,
    metadata,
)

_NOW = datetime(2026, 9, 5, 18, 0, tzinfo=UTC)
_EXPERIMENT_HASH = "a" * 64
_CORRELATION_ID = "0198fa2d-7b8c-7123-8abc-0123456789ab"


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seed_latch(engine: Engine, latch_type: RiskLatchKind) -> None:
    event_record = create_latch_engagement(
        latch_state=RiskLatchState.empty(experiment_hash=_EXPERIMENT_HASH),
        latch_type=latch_type,
        reason_code=latch_type.value,
        actor="risk_engine",
        occurred_at=_NOW,
        correlation_id="correlation_" + "c" * 64,
        idempotency_key=f"seed_{latch_type.value}",
    )
    payload = {
        "action": event_record.action.value,
        "actor": event_record.actor,
        "correlation_id": event_record.correlation_id,
        "experiment_hash": event_record.experiment_hash,
        "idempotency_key": event_record.idempotency_key,
        "latch_type": event_record.latch_type.value,
        "occurred_at": _timestamp(event_record.occurred_at),
        "reason_code": event_record.reason_code,
        "schema": "risk-latch-event-v1",
        "sequence": event_record.sequence,
    }
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_risk_latch_events).values(
                latch_event_id=event_record.latch_event_id,
                experiment_hash=event_record.experiment_hash,
                latch_type=event_record.latch_type.value,
                sequence=event_record.sequence,
                action=event_record.action.value,
                correlation_id=event_record.correlation_id,
                idempotency_key=event_record.idempotency_key,
                reason_code=event_record.reason_code,
                actor=event_record.actor,
                occurred_at=event_record.occurred_at,
                payload=payload,
                payload_hash=event_record.payload_hash,
                content_hash=event_record.content_hash,
            )
        )


@pytest.fixture
def operator_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'operator.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()

    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="operator_control_fixture",
                experiment_version=1,
                schema_version=1,
                configuration={"mode": "offline_fixture"},
                content_hash=_EXPERIMENT_HASH,
                registered_at=_NOW,
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def test_operator_controls_append_audited_idempotent_latch_events(
    operator_engine: Engine,
) -> None:
    controls = SQLAlchemyOperatorControls(
        operator_engine,
        experiment_hash=_EXPERIMENT_HASH,
    )
    halt_request = OperatorHaltRequest(
        idempotency_key="operator-halt-1",
        correlation_id=_CORRELATION_ID,
    )
    halted = controls.halt(request=halt_request, occurred_at=_NOW)
    repeated = controls.halt(
        request=halt_request,
        occurred_at=_NOW + timedelta(seconds=10),
    )

    assert halted == repeated
    assert halted.state == "engaged"
    with pytest.raises(OperatorControlConflictError, match="conflicts"):
        controls.halt(
            request=OperatorHaltRequest(
                idempotency_key="operator-halt-2",
                correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ac",
            ),
            occurred_at=_NOW + timedelta(seconds=20),
        )

    resume_request = OperatorResumeRequest(
        idempotency_key="operator-resume-1",
        correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ad",
        acknowledgement=OPERATOR_RESUME_ACKNOWLEDGEMENT,
    )
    resumed = controls.resume(
        request=resume_request,
        occurred_at=_NOW + timedelta(seconds=30),
    )
    assert resumed.state == "cleared"
    assert (
        controls.resume(
            request=resume_request,
            occurred_at=_NOW + timedelta(seconds=40),
        )
        == resumed
    )

    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 2
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 2
        audit_rows = connection.execute(
            select(
                aqa_audit_events.c.actor,
                aqa_audit_events.c.stream_id,
                aqa_audit_events.c.event_type,
            ).order_by(aqa_audit_events.c.sequence)
        ).all()
    assert audit_rows == [
        (
            AuditWriter.CONTROL.value,
            f"aqa_control:operator:{_EXPERIMENT_HASH}:operator_halt",
            "operator.halted",
        ),
        (
            AuditWriter.CONTROL.value,
            f"aqa_control:operator:{_EXPERIMENT_HASH}:operator_halt",
            "operator.resumed",
        ),
    ]


@pytest.mark.parametrize(
    "latch_type",
    (RiskLatchKind.SESSION_LOSS, RiskLatchKind.DEPLOYMENT_DRAWDOWN),
)
def test_operator_can_acknowledge_and_idempotently_clear_financial_latches(
    operator_engine: Engine,
    latch_type: RiskLatchKind,
) -> None:
    _seed_latch(operator_engine, latch_type)
    controls = SQLAlchemyOperatorControls(
        operator_engine,
        experiment_hash=_EXPERIMENT_HASH,
    )
    request = OperatorResumeRequest(
        idempotency_key=f"clear-{latch_type.value}",
        correlation_id=_CORRELATION_ID,
        acknowledgement=OPERATOR_RESUME_ACKNOWLEDGEMENT,
        latch_type=latch_type.value,
    )

    cleared = controls.resume(request=request, occurred_at=_NOW + timedelta(seconds=1))
    repeated = controls.resume(request=request, occurred_at=_NOW + timedelta(seconds=2))

    assert repeated == cleared
    assert cleared.state == "cleared"
    assert cleared.latch_type is latch_type
    with operator_engine.connect() as connection:
        events = connection.execute(
            select(
                aqa_risk_latch_events.c.action,
                aqa_risk_latch_events.c.latch_type,
            ).order_by(aqa_risk_latch_events.c.sequence)
        ).all()
        audit = connection.execute(
            select(aqa_audit_events.c.event_type, aqa_audit_events.c.payload)
        ).one()
    assert events == [("ENGAGED", latch_type.value), ("CLEARED", latch_type.value)]
    assert audit[0] == "operator.resumed"
    assert audit[1]["acknowledgement"] == "accepted"
    assert audit[1]["latch_type"] == latch_type.value


def test_operator_control_rolls_back_latch_when_audit_append_fails(
    operator_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls = SQLAlchemyOperatorControls(
        operator_engine,
        experiment_hash=_EXPERIMENT_HASH,
    )

    def reject_audit(**values: object) -> None:
        del values
        raise AuditPersistenceError("injected audit failure")

    monkeypatch.setattr(controls._audit, "append", reject_audit)

    with pytest.raises(OperatorControlError, match="unavailable"):
        controls.halt(
            request=OperatorHaltRequest(
                idempotency_key="operator-halt-rollback",
                correlation_id=_CORRELATION_ID,
            ),
            occurred_at=_NOW,
        )

    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 0
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 0


def test_concurrent_operator_halt_retries_converge_on_one_audited_event(
    operator_engine: Engine,
) -> None:
    controls = SQLAlchemyOperatorControls(
        operator_engine,
        experiment_hash=_EXPERIMENT_HASH,
    )
    request = OperatorHaltRequest(
        idempotency_key="operator-halt-concurrent",
        correlation_id=_CORRELATION_ID,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda _: controls.halt(request=request, occurred_at=_NOW),
                range(2),
            )
        )

    assert results[0] == results[1]
    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_risk_latch_events)) == 1
        assert connection.scalar(select(func.count()).select_from(aqa_audit_events)) == 1
