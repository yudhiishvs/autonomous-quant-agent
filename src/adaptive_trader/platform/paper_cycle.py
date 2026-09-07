"""Bounded paper proposal consumption with durable, independent authorization evidence.

The execution process reads declarative envelopes only. It never discovers providers or
loads artifacts. This release's authorization contract cannot represent approval; denied
proposals are audited before readiness is advertised. The downstream durable execution
service remains the sole broker boundary when a separately implemented approval policy
can provide authority. Runtime flags never stand in for that policy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sqlalchemy import Engine, column, select, table

from adaptive_trader.platform.config import (
    BrokerAdapter,
    ExecutionMode,
    RuntimeService,
    RuntimeSettings,
)
from adaptive_trader.platform.domain import AuditPayload, AuditWriter, require_utc_instant
from adaptive_trader.platform.scheduling.models import DecisionSlot, DecisionType, SlotState
from adaptive_trader.platform.signals.models import (
    DecisionContext,
    SignalAction,
    SignalEnvelope,
    SignalSourceMode,
    verify_paper_authorization,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import aqa_decision_slots, aqa_signal_envelopes


class PaperCycleState(StrEnum):
    """Paper readiness describes service health independently from permission to trade."""

    IDLE = "idle"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PaperCycleResult:
    """A payload-free bounded operational outcome."""

    state: PaperCycleState
    reason_code: str
    work_units: int = 0


class PaperCycle(Protocol):
    """One finite paper-boundary operation and its resource cleanup."""

    def run_cycle(self) -> PaperCycleResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PaperCandidate:
    """A persisted proposal paired with independently reconstructed runtime context."""

    signal: SignalEnvelope
    context: DecisionContext

    def __post_init__(self) -> None:
        if type(self.signal) is not SignalEnvelope or type(self.context) is not DecisionContext:
            raise TypeError("paper candidate requires validated domain inputs")
        self.signal.validate_for(self.context)


class PaperCandidateSource(Protocol):
    """Read at most one current declarative proposal; expose no provider capability."""

    def current(self, *, observed_at: datetime) -> PaperCandidate | None: ...


class PaperDenialRecorder(Protocol):
    """Persist idempotent evidence before reporting a successfully processed denial."""

    def record(self, candidate: PaperCandidate, *, reason_code: str) -> None: ...


class SQLPaperCandidateSource:
    """Read the newest current proposal through the execution role's signal view."""

    def __init__(self, settings: RuntimeSettings, engine: Engine) -> None:
        self._settings = settings
        self._engine = engine

    def current(self, *, observed_at: datetime) -> PaperCandidate | None:
        require_utc_instant(observed_at, field_name="observed_at")
        experiment = self._settings.platform.experiment.definition
        signals = aqa_signal_envelopes
        if self._engine.dialect.name == "postgresql":
            signals = table(  # type: ignore[assignment]
                "aqa_signals_v",
                *(column(item.name, item.type) for item in aqa_signal_envelopes.columns),
                schema="aqa",
            )
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(signals)
                    .where(
                        signals.c.experiment_hash == experiment.content_hash,
                        signals.c.provider_id == self._settings.platform.profile.signal_provider.id,
                        signals.c.created_at <= observed_at,
                        signals.c.expires_at > observed_at,
                    )
                    .order_by(signals.c.created_at.desc(), signals.c.signal_id)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        values = dict(row)
        values["provider_source_mode"] = SignalSourceMode(values["provider_source_mode"])
        values["actions"] = tuple(SignalAction(value) for value in values["actions"])
        for name in ("active_symbols", "availability_mask"):
            values[name] = tuple(values[name])
        for name in ("expected_edge_bps", "proposed_signed_target_inputs"):
            values[name] = tuple(
                None if value is None else Decimal(value) for value in values[name]
            )
        signal = SignalEnvelope(**values)
        slot = _read_slot(self._engine, signal.slot_id)
        if slot is None or not slot.ready_at <= observed_at < slot.deadline_at:
            return None
        from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
        from adaptive_trader.platform.risk.policy import policy_hash
        from adaptive_trader.platform.scheduling.models import build_session_schedule

        schedule = build_session_schedule(
            experiment=experiment,
            signal_provider_id=slot.signal_provider_id,
            signal_provider_version=slot.signal_provider_version,
            session_date=slot.session_date,
            calendar=XnasExchangeCalendar(),
        )
        ordinal = next(
            (
                index
                for index, item in enumerate(schedule.strategy_slots)
                if item.slot_id == slot.slot_id
            ),
            None,
        )
        if slot.slot_id not in {item.slot_id for item in schedule.slots}:
            raise ValueError("paper candidate is outside the canonical schedule")
        context = DecisionContext.from_experiment(
            slot=slot,
            experiment=experiment,
            data_contract_hash=signal.data_contract_hash,
            policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
            execution_mode=ExecutionMode.PAPER,
            broker_adapter=BrokerAdapter.ALPACA_PAPER,
            submission_enabled=self._settings.platform.profile.execution.submission_enabled,
            strategy_slot_ordinal=ordinal,
        )
        return PaperCandidate(signal, context)


class AuditPaperDenialRecorder:
    """Append one stable denial per envelope; retries preserve the original audit chain."""

    def __init__(self, engine: Engine) -> None:
        self._audit = AuditRepository(engine, writer=AuditWriter.EXECUTION)

    def record(self, candidate: PaperCandidate, *, reason_code: str) -> None:
        signal = candidate.signal
        self._audit.append(
            stream_id=f"aqa_execution:{signal.signal_id}",
            event_type="execution.paper_authorization_denied",
            occurred_at=signal.created_at,
            payload=AuditPayload.from_mapping(
                {
                    "idempotency_key": f"paper_deny_{signal.content_hash}",
                    "content_hash": signal.content_hash,
                    "signal_id": signal.signal_id,
                    "slot_id": signal.slot_id,
                    "reason_code": reason_code,
                }
            ),
        )


class PaperExecutionCycle:
    """Inspect one persisted proposal and independently deny/audit its paper authority.

    No broker credentials are loaded here. Enabling submission does not bypass the
    immutable default-deny approval contract, and an unexpected approval is an error.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        candidates: PaperCandidateSource,
        denials: PaperDenialRecorder,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.PAPER_EXECUTION_WORKER
            or settings.platform.profile.mode is not ExecutionMode.PAPER
            or settings.platform.profile.execution.broker != "alpaca_paper"
        ):
            raise TypeError("paper cycle requires isolated paper runtime settings")
        self._settings = settings
        self._candidates = candidates
        self._denials = denials
        self._clock = clock

    def run_cycle(self) -> PaperCycleResult:
        now = self._clock()
        require_utc_instant(now, field_name="observed_at")
        candidate = self._candidates.current(observed_at=now)
        if candidate is None:
            return PaperCycleResult(PaperCycleState.IDLE, "paper_signal_unavailable")
        if type(candidate) is not PaperCandidate:
            raise TypeError("paper candidate source returned invalid evidence")
        if (
            candidate.context.execution_mode is not ExecutionMode.PAPER
            or candidate.context.broker_adapter is not BrokerAdapter.ALPACA_PAPER
            or candidate.signal.experiment_hash
            != self._settings.platform.experiment.definition.content_hash
            or candidate.signal.provider_id != self._settings.platform.profile.signal_provider.id
        ):
            raise ValueError("paper candidate does not match runtime authority")
        if (
            not candidate.signal.created_at <= now < candidate.signal.expires_at
            or not candidate.context.slot.ready_at <= now < candidate.context.slot.deadline_at
        ):
            raise ValueError("paper candidate is stale")
        authorization = verify_paper_authorization(candidate.signal, context=candidate.context)
        if authorization.approved:
            raise RuntimeError("paper authorization violated the closed release contract")
        reason = authorization.reason.value
        self._denials.record(candidate, reason_code=reason)
        return PaperCycleResult(PaperCycleState.BLOCKED, reason, 1)

    def close(self) -> None:
        """Dependencies remain owned by the surrounding runtime."""


def _read_slot(engine: Engine, slot_id: str) -> DecisionSlot | None:
    fields = DecisionSlot.__dataclass_fields__
    slots = table(
        "aqa_decision_slots_v" if engine.dialect.name == "postgresql" else "aqa_decision_slots",
        *(
            column(item.name, item.type)
            for item in aqa_decision_slots.columns
            if item.name in fields
        ),
        schema="aqa" if engine.dialect.name == "postgresql" else None,
    )
    with engine.begin() as connection:
        row = (
            connection.execute(select(slots).where(slots.c.slot_id == slot_id))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    values = dict(row)
    values["state"] = SlotState(values["state"])
    values["decision_type"] = DecisionType(values["decision_type"])
    return DecisionSlot(**values)
