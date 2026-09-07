"""Separate strategy-role producer of declarative, always-flat shadow proposals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Engine, text

from adaptive_trader.platform.config import ExecutionMode, RuntimeService, RuntimeSettings
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.policy import policy_hash
from adaptive_trader.platform.scheduling.models import DecisionType, SlotState
from adaptive_trader.platform.service_cycles import _effective_aggregate_rows, _read_slot
from adaptive_trader.platform.shadow_settings import (
    require_shadow_database_role,
    shadow_readiness_is_current,
)
from adaptive_trader.platform.signals import (
    AlwaysFlatSignalProvider,
    DecisionContext,
    SignalEnvelopeRepository,
)

if TYPE_CHECKING:
    from adaptive_trader.platform.service_cycles import WorkerCycleResult


def propose_once(
    *,
    engine: Engine,
    settings: RuntimeSettings,
    slot_id: str,
    now: datetime,
) -> dict[str, object]:
    """Persist a proposal using strategy credentials; never grant execution authority."""
    now = require_utc_instant(now, field_name="shadow_proposal_clock")
    if (
        type(settings) is not RuntimeSettings
        or settings.service is not RuntimeService.STRATEGY_WORKER
        or settings.platform.profile.mode not in {ExecutionMode.SHADOW, ExecutionMode.PAPER}
        or settings.platform.profile.signal_provider.id != "always_flat"
    ):
        raise ValueError("shadow proposal requires the isolated always-flat strategy configuration")
    require_shadow_database_role(engine, role="aqa_strategy", fixture=False)
    experiment = settings.platform.experiment.definition
    slot = _read_slot(engine, slot_id)
    if (
        slot is None
        or slot.experiment_hash != experiment.content_hash
        or slot.signal_provider_id != "always_flat"
        or slot.state is not SlotState.CLAIMED
        or slot.claim_owner != "strategy_worker"
        or slot.lease_expires_at is None
        or slot.lease_expires_at <= now
        or not slot.ready_at <= now < slot.deadline_at
    ):
        return {"status": "blocked", "reason_code": "decision_slot_unavailable"}
    if not shadow_readiness_is_current(
        engine,
        experiment_hash=experiment.content_hash,
        timeframe="1Min" if slot.decision_type is DecisionType.FORCED_FLAT else "15Min",
        interval_end=slot.source_interval_end,
        now=now,
    ):
        return {"status": "blocked", "reason_code": "canonical_readiness_unavailable"}
    rows = tuple(
        row
        for row in _effective_aggregate_rows(
            engine,
            provider="alpaca",
            start_at=slot.source_interval_start,
            end_at=slot.source_interval_end,
            timeframe="1Min" if slot.decision_type is DecisionType.FORCED_FLAT else "15Min",
        )
        if row.symbol in experiment.active_tradable
    )
    if (
        len(rows) != len(experiment.active_tradable)
        or {row.symbol for row in rows} != set(experiment.active_tradable)
        or any(
            row.received_at > now
            or row.quality_flags != ("complete",)
            or row.source_mode != "external_provider"
            for row in rows
        )
    ):
        return {"status": "blocked", "reason_code": "canonical_data_unavailable"}
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=sha256_hex(tuple(sorted(row.source_payload_hash for row in rows))),
        policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
        execution_mode=settings.platform.profile.mode,
        broker_adapter=settings.platform.profile.execution.broker,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )
    repository = SignalEnvelopeRepository(engine)
    signal = repository.get_for_slot(slot.slot_id)
    created = signal is None
    if signal is None:
        signal = AlwaysFlatSignalProvider(clock=lambda: now).signal_for(context)
        repository.persist_once(signal, context=context)
    signal.validate_for(context)
    return {
        "status": "signal_persisted" if created else "signal_current",
        "signal_id": signal.signal_id,
    }


class OperationalStrategyCycle:
    """Consume only scheduler-claimed current slots with the isolated strategy role."""

    def __init__(
        self,
        settings: RuntimeSettings,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.STRATEGY_WORKER
            or settings.platform.profile.mode not in {ExecutionMode.SHADOW, ExecutionMode.PAPER}
            or settings.platform.profile.signal_provider.id != "always_flat"
        ):
            raise ValueError("operational strategy requires the built-in always-flat profile")
        self._settings = settings
        self._engine = engine
        self._clock = clock

    def run_cycle(self) -> WorkerCycleResult:
        from adaptive_trader.platform.service_cycles import WorkerCycleResult, WorkerCycleState

        now = require_utc_instant(self._clock(), field_name="strategy_clock")
        require_shadow_database_role(self._engine, role="aqa_strategy", fixture=False)
        with self._engine.connect() as connection:
            slot_id = connection.scalar(
                text(
                    "SELECT slot_id FROM aqa.aqa_decision_slots_v "
                    "WHERE experiment_hash = :experiment AND signal_provider_id = 'always_flat' "
                    "AND state = 'CLAIMED' AND claim_owner = 'strategy_worker' "
                    "AND ready_at <= :now AND deadline_at > :now AND lease_expires_at > :now "
                    "ORDER BY ready_at, slot_id LIMIT 1"
                ),
                {
                    "experiment": self._settings.platform.experiment.definition.content_hash,
                    "now": now,
                },
            )
        if slot_id is None:
            return WorkerCycleResult(WorkerCycleState.IDLE, "strategy_slot_unavailable")
        outcome = propose_once(
            engine=self._engine, settings=self._settings, slot_id=slot_id, now=now
        )
        if outcome["status"] == "blocked":
            return WorkerCycleResult(WorkerCycleState.BLOCKED, str(outcome["reason_code"]))
        return WorkerCycleResult(
            WorkerCycleState.PROGRESSED
            if outcome["status"] == "signal_persisted"
            else WorkerCycleState.IDLE,
            str(outcome["status"]),
            int(outcome["status"] == "signal_persisted"),
        )

    def close(self) -> None:
        """The surrounding runtime owns the database engine."""
