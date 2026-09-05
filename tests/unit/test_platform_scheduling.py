"""Exact timetable, durable lease, restart, and deadline tests."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
)

from adaptive_trader.platform.config import ExperimentDefinition, load_experiment
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.scheduling import (
    LEASE_DURATION,
    STRATEGY_CLOSE_TIMES,
    AuditSlotTransitionRecorder,
    ClaimStatus,
    DecisionSlot,
    DecisionSlotRepository,
    DecisionType,
    SlotSchemaError,
    SlotState,
    SlotTransitionError,
    build_session_schedule,
)
from adaptive_trader.platform.scheduling.models import SlotValidationError
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, UTCDateTime, aqa_audit_events

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"
_SESSION_DATE = date(2026, 7, 6)
_RECORDED_AT = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_CONFIG_ROOT,
    )


@pytest.fixture(scope="module")
def calendar() -> XnasExchangeCalendar:
    return XnasExchangeCalendar()


def _slot_table(metadata: MetaData) -> Table:
    return Table(
        "phase4_decision_slots",
        metadata,
        Column("slot_id", String(128), primary_key=True),
        Column("experiment_id", String(64), nullable=False),
        Column("experiment_version", BigInteger, nullable=False),
        Column("experiment_hash", String(64), nullable=False),
        Column("signal_provider_id", String(64), nullable=False),
        Column("signal_provider_version", String(64), nullable=False),
        Column("session_date", Date, nullable=False),
        Column("source_interval_start", UTCDateTime(), nullable=False),
        Column("source_interval_end", UTCDateTime(), nullable=False),
        Column("ready_at", UTCDateTime(), nullable=False),
        Column("deadline_at", UTCDateTime(), nullable=False),
        Column("required_completion_at", UTCDateTime(), nullable=False),
        Column("decision_type", String(32), nullable=False),
        Column("state", String(32), nullable=False),
        Column("claim_owner", String(64)),
        Column("claimed_at", UTCDateTime()),
        Column("lease_expires_at", UTCDateTime()),
        Column("attempt_count", Integer, nullable=False),
        Column("completed_at", UTCDateTime()),
        Column("reason_code", String(64)),
        Column("correlation_id", String(128), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("version", BigInteger, nullable=False),
        Column("created_at", UTCDateTime(), nullable=False),
        Column("updated_at", UTCDateTime(), nullable=False),
    )


class _Recorder:
    def __init__(self) -> None:
        self._lock = Lock()
        self.records: list[tuple[SlotState | None, SlotState, int]] = []

    def record(
        self,
        connection: object,
        *,
        previous: DecisionSlot | None,
        current: DecisionSlot,
        occurred_at: datetime,
    ) -> None:
        del connection, occurred_at
        with self._lock:
            self.records.append(
                (None if previous is None else previous.state, current.state, current.version)
            )


class _Probe:
    def __init__(self) -> None:
        self.slot_ids: set[str] = set()

    def exists(self, connection: object, *, slot_id: str) -> bool:
        del connection
        return slot_id in self.slot_ids


@pytest.fixture
def repository(
    tmp_path: Path,
) -> Iterator[tuple[DecisionSlotRepository, _Recorder, _Probe]]:
    metadata = MetaData()
    table = _slot_table(metadata)
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'slots.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(engine)
    recorder = _Recorder()
    probe = _Probe()
    try:
        yield (
            DecisionSlotRepository(
                engine,
                table=table,
                transition_recorder=recorder,
                materialization_probe=probe,
            ),
            recorder,
            probe,
        )
    finally:
        engine.dispose()


def _schedule(
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
    *,
    session_date: date = _SESSION_DATE,
    provider_id: str = "deterministic_fixture",
):
    return build_session_schedule(
        experiment=experiment,
        signal_provider_id=provider_id,
        signal_provider_version="1",
        session_date=session_date,
        calendar=calendar,
    )


def test_exact_full_session_schedule_and_forced_flat_timeline(
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    schedule = _schedule(experiment, calendar)

    assert tuple(SlotState) == (
        SlotState.PENDING,
        SlotState.WAITING_FOR_DATA,
        SlotState.READY,
        SlotState.CLAIMED,
        SlotState.COMPLETED,
        SlotState.SKIPPED,
        SlotState.EXPIRED,
        SlotState.FAILED,
        SlotState.FLATTEN_REQUIRED,
    )
    assert len(STRATEGY_CLOSE_TIMES) == len(schedule.strategy_slots) == 20
    assert schedule.strategy_slots[0].slot_id == (
        "slot_43a3133d0290f4ab3844ee53dac8f4578c9231fbb96838aa1af1264a90e86fbb"
    )
    assert schedule.strategy_slots[0].content_hash == (
        "d98cd4aa0fa4835cfa555a07f49a1aee05a33188af0d3ec7056dd6bd0cd78d36"
    )
    assert [slot.source_interval_end.strftime("%H:%M") for slot in schedule.strategy_slots] == [
        "13:45",
        "14:00",
        "14:15",
        "14:30",
        "14:45",
        "15:00",
        "15:15",
        "15:30",
        "15:45",
        "16:00",
        "16:15",
        "16:30",
        "16:45",
        "17:00",
        "17:15",
        "17:30",
        "17:45",
        "18:00",
        "18:15",
        "18:30",
    ]
    assert all(
        slot.ready_at == slot.source_interval_end + timedelta(seconds=60)
        and slot.deadline_at == slot.source_interval_end + timedelta(seconds=120)
        and slot.state is SlotState.PENDING
        and slot.decision_type is DecisionType.STRATEGY
        for slot in schedule.strategy_slots
    )
    forced = schedule.forced_flat_slot
    assert forced is not None
    assert forced.ready_at == datetime(2026, 7, 6, 19, 43, tzinfo=UTC)
    assert forced.deadline_at == datetime(2026, 7, 6, 19, 44, tzinfo=UTC)
    assert forced.required_completion_at == datetime(2026, 7, 6, 19, 45, tzinfo=UTC)
    assert forced.state is SlotState.FLATTEN_REQUIRED
    assert forced.decision_type is DecisionType.FORCED_FLAT


def test_schedule_is_deterministic_across_replay_and_dst(
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    first = _schedule(experiment, calendar)
    replay = _schedule(experiment, calendar)
    winter = _schedule(experiment, calendar, session_date=date(2026, 11, 2))

    assert first == replay
    assert len({slot.slot_id for slot in first.slots}) == 21
    assert first.strategy_slots[0].source_interval_end.hour == 13
    assert winter.strategy_slots[0].source_interval_end.hour == 14
    with pytest.raises(SlotValidationError, match="content hash"):
        replace(first.strategy_slots[0], attempt_count=99)


@pytest.mark.parametrize(
    ("session_date", "reason"),
    [
        (date(2026, 7, 3), "market_closed"),
        (date(2026, 11, 27), "unsupported_nonstandard_session"),
    ],
)
def test_closed_and_early_close_sessions_create_no_slots(
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
    session_date: date,
    reason: str,
) -> None:
    schedule = _schedule(experiment, calendar, session_date=session_date)

    assert schedule.slots == ()
    assert schedule.reason_code == reason


def test_repository_refuses_a_legacy_schema(tmp_path: Path) -> None:
    metadata = MetaData()
    legacy = Table("legacy_slots", metadata, Column("slot_id", String(128), primary_key=True))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.sqlite3'}")
    metadata.create_all(engine)
    try:
        with pytest.raises(SlotSchemaError, match="Phase 4"):
            DecisionSlotRepository(engine, table=legacy, transition_recorder=_Recorder())
    finally:
        engine.dispose()


def test_audit_recorder_hash_chains_slot_creation_atomically(
    tmp_path: Path,
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    metadata = MetaData(schema=PLATFORM_SCHEMA)
    table = _slot_table(metadata)
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'audited-slots.sqlite3'}"
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})
    metadata.create_all(engine)
    aqa_audit_events.create(engine)
    try:
        repository = DecisionSlotRepository(
            engine,
            table=table,
            transition_recorder=AuditSlotTransitionRecorder(engine),
            materialization_probe=_Probe(),
        )
        repository.create_schedule(_schedule(experiment, calendar), recorded_at=_RECORDED_AT)

        report = AuditRepository(engine).verify()
        assert report.event_count == 21
        assert len(report.stream_heads) == 21
    finally:
        engine.dispose()


def test_wait_ready_claim_and_materialized_lease_recovery_are_durable(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, recorder, probe = repository
    schedule = _schedule(experiment, calendar)
    slots = repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    assert repo.create_schedule(schedule, recorded_at=_RECORDED_AT) == slots
    slot = slots[0]

    waiting = repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=slot.source_interval_end - timedelta(minutes=1),
        now=slot.ready_at,
    )
    assert waiting.state is SlotState.WAITING_FOR_DATA
    assert waiting.reason_code == "active_basket_not_ready"

    ready = repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=slot.source_interval_end,
        now=slot.ready_at + timedelta(seconds=1),
    )
    assert ready.state is SlotState.READY
    claimed_at = slot.ready_at + timedelta(seconds=5)
    claim = repo.claim(slot.slot_id, owner="worker-1", now=claimed_at)
    assert claim.status is ClaimStatus.CLAIMED
    assert claim.slot.lease_expires_at == claimed_at + LEASE_DURATION
    assert claim.slot.attempt_count == 1
    assert (
        repo.claim(slot.slot_id, owner="worker-2", now=claimed_at).status is ClaimStatus.LEASE_HELD
    )

    probe.slot_ids.add(slot.slot_id)
    recovered = repo.recover_expired_claims(
        owner="restart-worker",
        now=claimed_at + LEASE_DURATION,
    )
    assert len(recovered) == 1
    assert recovered[0].status is ClaimStatus.MATERIALIZED
    assert recovered[0].slot.state is SlotState.COMPLETED
    assert recovered[0].slot.attempt_count == 1
    assert [current for _, current, _ in recorder.records[-4:]] == [
        SlotState.WAITING_FOR_DATA,
        SlotState.READY,
        SlotState.CLAIMED,
        SlotState.COMPLETED,
    ]


def test_expired_lease_reclaims_only_before_deadline_and_never_catches_up(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, _, _ = repository
    schedule = _schedule(experiment, calendar)
    repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    slot = schedule.strategy_slots[1]
    ready = repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=slot.source_interval_end,
        now=slot.ready_at,
    )
    first_claim = repo.claim(
        slot.slot_id,
        owner="worker-1",
        now=ready.ready_at + timedelta(seconds=1),
    )
    assert first_claim.status is ClaimStatus.CLAIMED

    reclaimed_at = first_claim.slot.lease_expires_at
    assert reclaimed_at is not None
    reclaimed = repo.claim(slot.slot_id, owner="worker-2", now=reclaimed_at)
    assert reclaimed.status is ClaimStatus.RECLAIMED
    assert reclaimed.slot.attempt_count == 2
    after_deadline = repo.claim(
        slot.slot_id,
        owner="worker-3",
        now=slot.deadline_at,
    )
    assert after_deadline.status is ClaimStatus.DEADLINE_ELAPSED
    assert after_deadline.slot.state is SlotState.EXPIRED
    assert after_deadline.slot.reason_code == "decision_deadline_elapsed"
    assert (
        repo.claim(slot.slot_id, owner="worker-4", now=slot.deadline_at + timedelta(hours=1)).status
        is ClaimStatus.NOT_AVAILABLE
    )


def test_missed_data_deadline_expires_without_a_claim(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, _, _ = repository
    schedule = _schedule(experiment, calendar)
    repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    slot = schedule.strategy_slots[2]
    waiting = repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=None,
        now=slot.ready_at,
    )
    expired = repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=slot.source_interval_end,
        now=slot.deadline_at,
    )

    assert waiting.state is SlotState.WAITING_FOR_DATA
    assert expired.state is SlotState.EXPIRED
    assert expired.attempt_count == 0
    assert (
        repo.claim(slot.slot_id, owner="late", now=slot.deadline_at).status
        is ClaimStatus.NOT_AVAILABLE
    )


def test_complete_requires_authoritative_materialization(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, _, probe = repository
    schedule = _schedule(experiment, calendar)
    repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    slot = schedule.strategy_slots[3]
    repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=slot.source_interval_end,
        now=slot.ready_at,
    )
    claimed = repo.claim(slot.slot_id, owner="worker", now=slot.ready_at).slot
    with pytest.raises(SlotTransitionError, match="materialization"):
        repo.complete(slot.slot_id, owner="worker", now=slot.ready_at + timedelta(seconds=1))

    probe.slot_ids.add(slot.slot_id)
    completed = repo.complete(
        slot.slot_id,
        owner="worker",
        now=slot.ready_at + timedelta(seconds=1),
    )
    assert completed.state is SlotState.COMPLETED
    assert completed.attempt_count == claimed.attempt_count


def test_concurrent_claimers_produce_one_claim_and_one_live_lease(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, _, _ = repository
    schedule = _schedule(experiment, calendar)
    repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    slot = schedule.strategy_slots[4]
    repo.evaluate_readiness(
        slot.slot_id,
        active_basket_watermark=slot.source_interval_end,
        now=slot.ready_at,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda owner: repo.claim(slot.slot_id, owner=owner, now=slot.ready_at),
                ("worker-1", "worker-2"),
            )
        )

    assert sorted(result.status for result in results) == [
        ClaimStatus.CLAIMED,
        ClaimStatus.LEASE_HELD,
    ]
    durable = repo.get(slot.slot_id)
    assert durable is not None
    assert durable.state is SlotState.CLAIMED
    assert durable.attempt_count == 1


def test_forced_flat_slot_is_claimable_only_at_target_and_fails_at_cutoff(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, _, _ = repository
    schedule = _schedule(experiment, calendar)
    repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    forced = schedule.forced_flat_slot
    assert forced is not None

    assert (
        repo.claim(
            forced.slot_id,
            owner="flatten-worker",
            now=forced.ready_at - timedelta(microseconds=1),
        ).status
        is ClaimStatus.NOT_READY
    )
    claimed = repo.claim(forced.slot_id, owner="flatten-worker", now=forced.ready_at)
    assert claimed.status is ClaimStatus.CLAIMED
    failed = repo.claim(
        forced.slot_id,
        owner="restart-worker",
        now=forced.deadline_at,
    )
    assert failed.status is ClaimStatus.DEADLINE_ELAPSED
    assert failed.slot.state is SlotState.FAILED
    assert failed.slot.reason_code == "forced_flat_submission_deadline_elapsed"


def test_skip_failure_renewal_and_claim_next_are_persisted_and_recorded(
    repository: tuple[DecisionSlotRepository, _Recorder, _Probe],
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> None:
    repo, recorder, _ = repository
    schedule = _schedule(experiment, calendar)
    repo.create_schedule(schedule, recorded_at=_RECORDED_AT)
    first, second, third = schedule.strategy_slots[:3]
    for slot in (first, second, third):
        repo.evaluate_readiness(
            slot.slot_id,
            active_basket_watermark=slot.source_interval_end,
            now=slot.ready_at,
        )

    skipped = repo.skip(
        first.slot_id,
        reason_code="operator_pause",
        now=first.ready_at,
    )
    claimed = repo.claim(second.slot_id, owner="worker-1", now=second.ready_at).slot
    renewed = repo.renew_lease(
        second.slot_id,
        owner="worker-1",
        now=second.ready_at + timedelta(seconds=1),
    )
    failed = repo.fail(
        second.slot_id,
        owner="worker-1",
        reason_code="provider_failure",
        now=second.ready_at + timedelta(seconds=2),
    )
    next_claim = repo.claim_next(owner="worker-2", now=third.ready_at)

    assert skipped.state is SlotState.SKIPPED
    assert renewed.lease_expires_at == second.ready_at + timedelta(seconds=31)
    assert renewed.attempt_count == claimed.attempt_count == 1
    assert failed.state is SlotState.FAILED
    assert next_claim is not None
    assert next_claim.slot.slot_id == third.slot_id
    assert {SlotState.SKIPPED, SlotState.FAILED}.issubset(
        {current for _, current, _ in recorder.records}
    )
