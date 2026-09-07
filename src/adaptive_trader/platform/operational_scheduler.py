"""Current-clock PostgreSQL scheduling without market/provider or execution authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select, text

from adaptive_trader.platform.config import ExecutionMode, RuntimeService, RuntimeSettings
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.scheduling import (
    DecisionSlotRepository,
    SlotState,
    build_session_schedule,
)
from adaptive_trader.platform.service_cycles import (
    WorkerCycleResult,
    WorkerCycleState,
    _signal_exists,
    _SignalViewMaterializationProbe,
)
from adaptive_trader.platform.storage.tables import aqa_decision_slots

_OWNER = "strategy_worker"
_TERMINAL = {SlotState.COMPLETED, SlotState.SKIPPED, SlotState.EXPIRED, SlotState.FAILED}


class OperationalSchedulerCycle:
    """Bounded scheduler over current sessions, never replaying overdue entry decisions.

    Each cycle creates at most one day's exact schedule and inspects at most 64 abandoned
    claims plus that day's slots. Claim/reclaim/deadline transactions remain repository-owned.
    Safe readiness is a read-only projection, not authority to mutate collector state.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        isolated_test: bool = False,
    ) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.SCHEDULER_WORKER
            or settings.platform.profile.mode not in {ExecutionMode.SHADOW, ExecutionMode.PAPER}
        ):
            raise ValueError("operational scheduler requires shadow or paper scheduler settings")
        if not callable(clock) or type(isolated_test) is not bool:
            raise TypeError("scheduler clock or test boundary is invalid")
        if engine.dialect.name != "postgresql" and not (
            isolated_test and engine.dialect.name == "sqlite"
        ):
            raise ValueError("operational scheduler requires PostgreSQL")
        self.settings, self.engine, self.clock = settings, engine, clock
        self.repository = DecisionSlotRepository(
            engine, materialization_probe=_SignalViewMaterializationProbe(engine)
        )

    def run_cycle(self) -> WorkerCycleResult:
        now = require_utc_instant(self.clock(), field_name="scheduler_clock")
        if self.engine.dialect.name == "postgresql":
            with self.engine.connect() as connection:
                role = connection.scalar(text("SELECT current_user"))
            if role not in {"aqa_scheduler", "aqa_scheduler_login"}:
                raise ValueError("operational scheduler requires its dedicated database role")
        experiment = self.settings.platform.experiment.definition
        with self.engine.connect() as connection:
            abandoned = tuple(
                connection.scalars(
                    select(aqa_decision_slots.c.slot_id)
                    .where(
                        aqa_decision_slots.c.experiment_hash == experiment.content_hash,
                        aqa_decision_slots.c.state == SlotState.CLAIMED.value,
                        aqa_decision_slots.c.lease_expires_at <= now,
                    )
                    .order_by(aqa_decision_slots.c.deadline_at, aqa_decision_slots.c.slot_id)
                    .limit(64)
                )
            )
        watermark = self._watermark(now)
        changed = 0
        recovery_waiting = False
        for slot_id in abandoned:
            before = self.repository.get(slot_id)
            if (
                before is not None
                and before.decision_type.value != "FORCED_FLAT"
                and now < before.deadline_at
                and not _signal_exists(self.engine, before.slot_id)
                and (watermark is None or watermark < before.source_interval_end)
            ):
                recovery_waiting = True
                continue
            after = self.repository.claim(slot_id, owner=_OWNER, now=now).slot
            changed += int(before != after)
        session_date = now.astimezone(ZoneInfo("America/New_York")).date()
        schedule = build_session_schedule(
            experiment=experiment,
            signal_provider_id=self.settings.platform.profile.signal_provider.id,
            signal_provider_version="1",
            session_date=session_date,
            calendar=XnasExchangeCalendar(),
        )
        existing_schedule = self.repository.list_for_session(
            experiment_hash=experiment.content_hash, session_date=session_date
        )
        self.repository.create_schedule(schedule, recorded_at=now)
        changed += len(schedule.slots) if not existing_schedule else 0
        claimed = False
        waiting = recovery_waiting
        for expected in schedule.slots:
            slot = self.repository.get(expected.slot_id)
            assert slot is not None
            if slot.state in _TERMINAL or slot.state is SlotState.CLAIMED:
                continue
            if slot.decision_type.value != "FORCED_FLAT":
                evaluated = self.repository.evaluate_readiness(
                    slot.slot_id, active_basket_watermark=watermark, now=now
                )
                changed += int(evaluated != slot)
                slot = evaluated
            if slot.state in {SlotState.READY, SlotState.FLATTEN_REQUIRED} and slot.ready_at <= now:
                result = self.repository.claim(slot.slot_id, owner=_OWNER, now=now)
                changed += int(result.slot != slot)
                claimed |= result.slot.state is SlotState.CLAIMED
            waiting |= slot.state is SlotState.WAITING_FOR_DATA
        return WorkerCycleResult(
            WorkerCycleState.PROGRESSED
            if changed
            else (WorkerCycleState.BLOCKED if waiting else WorkerCycleState.IDLE),
            "strategy_slot_claimed"
            if claimed
            else ("active_basket_not_ready" if waiting else "schedule_current"),
            changed,
        )

    def _watermark(self, now: datetime) -> datetime | None:
        relation = (
            "aqa.aqa_operational_readiness_v"
            if self.engine.dialect.name == "postgresql"
            else "aqa_operational_readiness_v"
        )
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        f"SELECT contiguous_through, updated_at FROM {relation} "
                        "WHERE experiment_hash = :experiment AND timeframe = '15Min' AND role = 'active' "
                        "AND status = 'ready' AND NOT pending_work AND NOT unresolved_gaps"
                    ),
                    {"experiment": self.settings.platform.experiment.definition.content_hash},
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["contiguous_through"] is None:
            return None

        def utc(value: object) -> datetime:
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if (
                isinstance(value, datetime)
                and self.engine.dialect.name == "sqlite"
                and value.tzinfo is None
            ):
                value = value.replace(tzinfo=UTC)
            return require_utc_instant(value, field_name="readiness_time")

        return None if utc(row["updated_at"]) > now else utc(row["contiguous_through"])

    def close(self) -> None:
        """Repository transactions own their connection lifetimes."""
