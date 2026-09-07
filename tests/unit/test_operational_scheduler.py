"""Current-clock scheduler deadline/restart tests over actual durable slot state."""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.operational_scheduler import OperationalSchedulerCycle
from adaptive_trader.platform.scheduling import SlotState, build_session_schedule
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.tables import metadata

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scheduler(tmp_path: Path):
    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    secret = tmp_path / "database-url"
    secret.write_text("fixture")
    secret.chmod(0o600)
    settings = load_runtime_settings(
        {"AQA_CONFIG": "configs/platform/shadow.yaml", "AQA_DATABASE_URL_FILE": str(secret)},
        service=RuntimeService.SCHEDULER_WORKER,
        application_root=tmp_path,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}").execution_options(
        schema_translate_map={"aqa": None}
    )
    metadata.create_all(engine)
    experiment = settings.platform.experiment.definition
    ExperimentRepository(engine).register(
        experiment, registered_at=datetime(2026, 7, 6, tzinfo=UTC)
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE test_readiness (experiment_hash TEXT, timeframe TEXT, role TEXT, status TEXT, contiguous_through TEXT, updated_at TEXT, pending_work BOOLEAN, unresolved_gaps BOOLEAN)"
            )
        )
        connection.execute(
            text("CREATE VIEW aqa_operational_readiness_v AS SELECT * FROM test_readiness")
        )
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="always_flat",
        signal_provider_version="1",
        session_date=date(2026, 7, 6),
        calendar=XnasExchangeCalendar(),
    )
    clock = [schedule.strategy_slots[0].ready_at + timedelta(seconds=1)]
    cycle = OperationalSchedulerCycle(settings, engine, clock=lambda: clock[0], isolated_test=True)
    yield cycle, clock, schedule
    engine.dispose()


def ready(cycle, now, through, *, pending=False, gaps=False):
    with cycle.engine.begin() as connection:
        connection.execute(text("DELETE FROM test_readiness"))
        connection.execute(
            text(
                "INSERT INTO test_readiness VALUES (:hash, '15Min', 'active', 'ready', :through, :now, :pending, :gaps)"
            ),
            {
                "hash": cycle.settings.platform.experiment.definition.content_hash,
                "through": through.isoformat(),
                "now": now.isoformat(),
                "pending": pending,
                "gaps": gaps,
            },
        )


def test_current_clock_creates_waits_claims_and_restart_keeps_identity(scheduler):
    cycle, clock, schedule = scheduler
    cycle.run_cycle()
    first = cycle.repository.get(schedule.strategy_slots[0].slot_id)
    assert first.state is SlotState.WAITING_FOR_DATA
    ready(cycle, clock[0], first.source_interval_end)
    assert cycle.run_cycle().reason_code == "strategy_slot_claimed"
    claimed = cycle.repository.get(first.slot_id)
    assert claimed.state is SlotState.CLAIMED
    assert claimed.claim_owner == "strategy_worker"
    replacement = OperationalSchedulerCycle(
        cycle.settings, cycle.engine, clock=lambda: clock[0], isolated_test=True
    )
    replacement.run_cycle()
    assert replacement.repository.get(first.slot_id) == claimed
    assert len(
        replacement.repository.list_for_session(
            experiment_hash=first.experiment_hash, session_date=first.session_date
        )
    ) == len(schedule.slots)


@pytest.mark.parametrize("pending,gaps", [(True, False), (False, True)])
def test_dirty_or_gap_state_never_claims_even_with_watermark(scheduler, pending, gaps):
    cycle, clock, schedule = scheduler
    ready(
        cycle, clock[0], schedule.strategy_slots[0].source_interval_end, pending=pending, gaps=gaps
    )
    cycle.run_cycle()
    assert (
        cycle.repository.get(schedule.strategy_slots[0].slot_id).state is SlotState.WAITING_FOR_DATA
    )


def test_late_start_expires_entry_instead_of_catching_up(scheduler):
    cycle, clock, schedule = scheduler
    clock[0] = schedule.strategy_slots[0].deadline_at
    ready(cycle, clock[0], schedule.strategy_slots[0].source_interval_end)
    cycle.run_cycle()
    assert cycle.repository.get(schedule.strategy_slots[0].slot_id).state is SlotState.EXPIRED


def test_expired_claim_recovers_without_historical_entry(scheduler):
    cycle, clock, schedule = scheduler
    ready(cycle, clock[0], schedule.strategy_slots[0].source_interval_end)
    cycle.run_cycle()
    clock[0] = schedule.strategy_slots[0].deadline_at + timedelta(seconds=1)
    cycle.run_cycle()
    assert cycle.repository.get(schedule.strategy_slots[0].slot_id).state is SlotState.EXPIRED


def test_holiday_and_early_close_have_no_entry_claims(scheduler):
    cycle, clock, _ = scheduler
    for instant in (datetime(2026, 7, 3, 15, tzinfo=UTC), datetime(2026, 11, 27, 15, tzinfo=UTC)):
        clock[0] = instant
        cycle.run_cycle()
        slots = cycle.repository.list_for_session(
            experiment_hash=cycle.settings.platform.experiment.definition.content_hash,
            session_date=instant.date(),
        )
        assert not any(slot.state is SlotState.CLAIMED for slot in slots)


def test_operational_runtime_refuses_sqlite_without_explicit_test_boundary(scheduler):
    cycle, _, _ = scheduler
    with pytest.raises(ValueError, match="PostgreSQL"):
        OperationalSchedulerCycle(cycle.settings, cycle.engine)


def test_forced_flat_is_separate_from_entry_readiness(scheduler):
    cycle, clock, schedule = scheduler
    forced = schedule.forced_flat_slot
    assert forced is not None
    clock[0] = forced.ready_at
    cycle.run_cycle()
    assert cycle.repository.get(forced.slot_id).state is SlotState.CLAIMED


def test_future_readiness_cannot_authorize_a_current_slot(scheduler):
    cycle, clock, schedule = scheduler
    ready(cycle, clock[0] + timedelta(days=1), schedule.strategy_slots[0].source_interval_end)
    cycle.run_cycle()
    assert (
        cycle.repository.get(schedule.strategy_slots[0].slot_id).state is SlotState.WAITING_FOR_DATA
    )


@pytest.mark.parametrize("pending,gaps", [(True, False), (False, True)])
def test_dirty_readiness_blocks_live_deadline_lease_reclaim(scheduler, pending, gaps):
    cycle, clock, schedule = scheduler
    first = schedule.strategy_slots[0]
    ready(cycle, clock[0], first.source_interval_end)
    cycle.run_cycle()
    claimed = cycle.repository.get(first.slot_id)
    clock[0] = claimed.lease_expires_at + timedelta(seconds=1)
    assert clock[0] < first.deadline_at
    ready(cycle, clock[0], first.source_interval_end, pending=pending, gaps=gaps)
    assert cycle.run_cycle().reason_code == "active_basket_not_ready"
    assert cycle.repository.get(first.slot_id) == claimed
    clock[0] = first.deadline_at + timedelta(seconds=1)
    cycle.run_cycle()
    assert cycle.repository.get(first.slot_id).state is SlotState.EXPIRED
