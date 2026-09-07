"""Bounded domain cycles for the platform's least-privilege worker processes.

The default offline topology advances one deterministic XNAS fixture session through collection,
aggregation, scheduling, proposal persistence, signed risk, fake execution and reconciliation.
Twenty complete prior fixture sessions establish the deterministic risk input. Live data is owned by the separate canonical collection
service lifecycle; this module does not construct a provider-backed live cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import Connection, Engine, and_, select, text

from adaptive_trader.platform.config import ExecutionMode, RuntimeService, RuntimeSettings

if TYPE_CHECKING:
    from adaptive_trader.platform.data import EffectiveBar
    from adaptive_trader.platform.data.calendar import TradingSession, XnasExchangeCalendar
    from adaptive_trader.platform.execution import (
        DeterministicFakePaperBroker,
        ExecutionPlan,
        ReconciliationReceipt,
    )
    from adaptive_trader.platform.risk import FullSessionCloses
    from adaptive_trader.platform.scheduling import DecisionSlot
    from adaptive_trader.platform.signals import SignalEnvelope
    from adaptive_trader.platform.storage.execution import SignedExecutionRepository
    from adaptive_trader.platform.storage.market_data import MarketDataRepository, StoredBarEvent

_OFFLINE_SESSION_DATE = date(2026, 7, 6)
_OFFLINE_SESSION_OPEN = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_OFFLINE_SESSION_CLOSE = _OFFLINE_SESSION_OPEN + timedelta(hours=6, minutes=30)
_OFFLINE_RECEIVED_AT = _OFFLINE_SESSION_CLOSE + timedelta(minutes=1)
_SIGNAL_PROVIDER_VERSION = "1"
_SCHEDULER_OWNER = "scheduler_worker"
_STRATEGY_OWNER = "strategy_worker"
_EXECUTION_OWNER = "execution_worker"
_MINUTES_PER_SESSION = 390
_MINUTES_PER_AGGREGATE = 15
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)


class WorkerCycleError(RuntimeError):
    """A bounded worker cycle could not preserve its domain guarantees."""


class WorkerCycleState(StrEnum):
    """Closed, bounded-cardinality cycle outcomes used by operations logging."""

    PROGRESSED = "progressed"
    IDLE = "idle"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    """Safe operational summary containing no payloads, symbols, or credentials."""

    state: WorkerCycleState
    reason_code: str
    work_units: int = 0

    def __post_init__(self) -> None:
        if type(self.state) is not WorkerCycleState:
            raise TypeError("worker cycle state is invalid")
        if type(self.reason_code) is not str or _REASON.fullmatch(self.reason_code) is None:
            raise ValueError("worker cycle reason code is invalid")
        if type(self.work_units) is not int or not 0 <= self.work_units <= 1_000_000:
            raise ValueError("worker cycle work count is invalid")


class WorkerCycle(Protocol):
    """One restart-safe, bounded unit of domain work."""

    def run_cycle(self) -> WorkerCycleResult: ...

    def close(self) -> None: ...


class OfflineMarketDataCycle:
    """Persist and materialize the deterministic default fixture without network access."""

    def __init__(self, settings: RuntimeSettings, engine: Engine) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.MARKET_DATA_WORKER
            or settings.platform.profile.mode is not ExecutionMode.OFFLINE
        ):
            raise TypeError("offline market-data cycle requires offline collector settings")
        if not isinstance(engine, Engine):
            raise TypeError("offline market-data cycle requires a database engine")
        self._settings = settings
        self._engine = engine

    def run_cycle(self) -> WorkerCycleResult:
        return self._run_cycle(ingest=True, aggregate=True)

    def ingest_fixture(self) -> WorkerCycleResult:
        """Append only the fixture minute events through the canonical collector."""
        return self._run_cycle(ingest=True, aggregate=False)

    def aggregate_fixture(self) -> WorkerCycleResult:
        """Materialize only already-persisted fixture minutes; never seed missing data."""
        return self._run_cycle(ingest=False, aggregate=True)

    def _run_cycle(self, *, ingest: bool, aggregate: bool) -> WorkerCycleResult:
        from adaptive_trader.platform.data import FifteenMinuteMaterializer, SessionWindow
        from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
        from adaptive_trader.platform.data.collector import MarketDataCollector
        from adaptive_trader.platform.data.provider import (
            FixtureMarketDataProvider,
            RawBarEnvelope,
        )
        from adaptive_trader.platform.data.watermarks import (
            STRICT_COMPLETE_QUALITY,
            GapRepository,
            WatermarkRepository,
        )
        from adaptive_trader.platform.storage.market_data import (
            BarIdentity,
            BarWriteStatus,
            EligibleWatermark,
            MarketDataRepository,
        )

        experiment = self._settings.platform.experiment.definition
        calendar = XnasExchangeCalendar()
        repository = MarketDataRepository(self._engine)
        watermarks = WatermarkRepository(self._engine, calendar=calendar)
        current_basket = watermarks.active_basket_watermark(
            experiment_hash=experiment.content_hash,
            timeframe="15Min",
        )
        if (
            ingest
            and aggregate
            and current_basket is not None
            and current_basket.is_ready
            and current_basket.contiguous_through == _OFFLINE_SESSION_CLOSE
            and _complete_history(
                self._engine,
                active_symbols=experiment.active_tradable,
                before_session=_OFFLINE_SESSION_DATE,
            )
            is not None
        ):
            return WorkerCycleResult(WorkerCycleState.IDLE, "market_data_current")
        ingested_changes = 0
        if ingest:
            clock_tick = 0

            def fixture_clock() -> datetime:
                nonlocal clock_tick
                clock_tick += 1
                return _OFFLINE_RECEIVED_AT + timedelta(microseconds=clock_tick)

            bars = tuple(
                RawBarEnvelope(
                    payload=_offline_fixture_payload(symbol=symbol, minute=minute),
                    received_at=_OFFLINE_RECEIVED_AT,
                )
                for symbol in experiment.collection_allowlist
                for minute in range(_MINUTES_PER_SESSION)
            )
            collector = MarketDataCollector(
                experiment=experiment,
                provider=FixtureMarketDataProvider(bars=bars),
                calendar=calendar,
                market_data_repository=repository,
                gap_repository=GapRepository(self._engine, calendar=calendar),
                watermark_repository=watermarks,
                readiness_start_at=_OFFLINE_SESSION_OPEN,
                clock=fixture_clock,
                sleep=lambda _seconds: None,
            )
            collected = collector.collect_historical(
                start_at=_OFFLINE_SESSION_OPEN,
                end_at=_OFFLINE_SESSION_CLOSE,
            )
            ingested_changes = collected.inserted + collected.corrected
        if not aggregate:
            return WorkerCycleResult(
                WorkerCycleState.PROGRESSED if ingested_changes else WorkerCycleState.IDLE,
                "fixture_minutes_persisted" if ingested_changes else "fixture_minutes_current",
                ingested_changes,
            )
        materializer = FifteenMinuteMaterializer.from_repository(repository)
        session = calendar.require_entry_session(_OFFLINE_SESSION_DATE)
        session_window = SessionWindow(
            session_open_utc=session.open_at,
            session_close_utc=session.close_at,
        )
        aggregate_changes = 0
        for symbol in experiment.active_tradable:
            for bucket_start in range(0, _MINUTES_PER_SESSION, _MINUTES_PER_AGGREGATE):
                constituents: list[EffectiveBar] = []
                for minute in range(bucket_start, bucket_start + _MINUTES_PER_AGGREGATE):
                    start_at = _OFFLINE_SESSION_OPEN + timedelta(minutes=minute)
                    event = repository.latest(
                        BarIdentity(
                            provider="fixture",
                            feed="iex",
                            adjustment="raw",
                            symbol=symbol,
                            timeframe="1Min",
                            start_at=start_at,
                            end_at=start_at + timedelta(minutes=1),
                        )
                    )
                    if event is None:
                        raise WorkerCycleError("offline aggregate source data is incomplete")
                    constituents.append(_stored_as_effective(event))
                receipt = materializer.materialize(
                    tuple(constituents),
                    session=session_window,
                    eligible_watermark=EligibleWatermark(
                        experiment_hash=experiment.content_hash,
                        quality_hash=STRICT_COMPLETE_QUALITY.policy_hash,
                        updated_at=_OFFLINE_RECEIVED_AT
                        + timedelta(seconds=1, microseconds=bucket_start + 1),
                    ),
                )
                aggregate_changes += int(
                    receipt.write_result.status
                    in {BarWriteStatus.INSERTED, BarWriteStatus.CORRECTED}
                )
        basket = watermarks.recompute_active_basket(
            experiment_hash=experiment.content_hash,
            universe=experiment,
            provider="fixture",
            feed="iex",
            adjustment="raw",
            timeframe="15Min",
            updated_at=_OFFLINE_RECEIVED_AT + timedelta(seconds=2),
            required_through=_OFFLINE_SESSION_CLOSE,
        )
        if not basket.is_ready:
            raise WorkerCycleError("offline active basket did not become ready")
        history_changes = 0
        if ingest:
            history_changes = _seed_offline_risk_history(
                repository=repository,
                calendar=calendar,
                active_symbols=experiment.active_tradable,
            )
        changed = ingested_changes + aggregate_changes + history_changes
        return WorkerCycleResult(
            state=WorkerCycleState.PROGRESSED if changed else WorkerCycleState.IDLE,
            reason_code="market_data_persisted" if changed else "market_data_current",
            work_units=changed,
        )

    def close(self) -> None:
        """Release no resources; the offline provider owns no external connection."""


class SchedulerCycle:
    """Persist a complete schedule and advance the earliest actionable offline slot."""

    def __init__(self, settings: RuntimeSettings, engine: Engine) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.SCHEDULER_WORKER
            or settings.platform.profile.mode is not ExecutionMode.OFFLINE
        ):
            raise TypeError("scheduler cycle requires offline scheduler settings")
        if not isinstance(engine, Engine):
            raise TypeError("scheduler cycle requires a database engine")
        self._settings = settings
        self._engine = engine

    def run_cycle(self) -> WorkerCycleResult:
        from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
        from adaptive_trader.platform.data.watermarks import WatermarkRepository
        from adaptive_trader.platform.scheduling import (
            DecisionSlotRepository,
            SlotState,
            build_session_schedule,
        )

        experiment = self._settings.platform.experiment.definition
        calendar = XnasExchangeCalendar()
        schedule = build_session_schedule(
            experiment=experiment,
            signal_provider_id=self._settings.platform.profile.signal_provider.id,
            signal_provider_version=_SIGNAL_PROVIDER_VERSION,
            session_date=_OFFLINE_SESSION_DATE,
            calendar=calendar,
        )
        if not schedule.strategy_slots:
            raise WorkerCycleError("offline fixture session did not produce a schedule")
        repository = DecisionSlotRepository(
            self._engine,
            materialization_probe=_SignalViewMaterializationProbe(self._engine),
        )
        before = repository.list_for_session(
            experiment_hash=experiment.content_hash,
            session_date=_OFFLINE_SESSION_DATE,
        )
        repository.create_schedule(
            schedule,
            recorded_at=_OFFLINE_SESSION_OPEN - timedelta(minutes=1),
        )
        work_units = len(schedule.slots) if not before else 0
        basket = WatermarkRepository(
            self._engine,
            calendar=calendar,
        ).active_basket_watermark(
            experiment_hash=experiment.content_hash,
            timeframe="15Min",
        )
        watermark = None if basket is None or not basket.is_ready else basket.contiguous_through
        terminal = {SlotState.COMPLETED, SlotState.SKIPPED, SlotState.EXPIRED, SlotState.FAILED}
        for scheduled in schedule.strategy_slots:
            slot = repository.get(scheduled.slot_id)
            if slot is None:
                raise WorkerCycleError("strategy slot was not persisted")
            if slot.state in terminal:
                continue
            if slot.state is SlotState.CLAIMED:
                if slot.claim_owner != _STRATEGY_OWNER:
                    raise WorkerCycleError("claimed strategy slot has an unexpected owner")
                if not _signal_exists(self._engine, slot.slot_id):
                    return WorkerCycleResult(
                        WorkerCycleState.IDLE,
                        "strategy_slot_claimed",
                        work_units,
                    )
                receipt = _slot_execution_receipt(self._engine, slot.slot_id)
                if receipt is None:
                    return WorkerCycleResult(WorkerCycleState.IDLE, "execution_pending")
                if receipt[0] != "CLEAN":
                    return WorkerCycleResult(
                        WorkerCycleState.BLOCKED, "execution_reconciliation_blocked"
                    )
                repository.complete(
                    slot.slot_id,
                    owner=_STRATEGY_OWNER,
                    now=receipt[1],
                )
                return WorkerCycleResult(
                    WorkerCycleState.PROGRESSED,
                    "strategy_slot_completed",
                    work_units + 1,
                )
            evaluated = repository.evaluate_readiness(
                slot.slot_id,
                active_basket_watermark=watermark,
                now=slot.ready_at + timedelta(seconds=1),
            )
            if evaluated.state is SlotState.WAITING_FOR_DATA:
                return WorkerCycleResult(
                    WorkerCycleState.BLOCKED,
                    "active_basket_not_ready",
                    work_units,
                )
            if evaluated.state is not SlotState.READY:
                continue
            claimed = repository.claim(
                evaluated.slot_id,
                owner=_STRATEGY_OWNER,
                now=evaluated.ready_at + timedelta(seconds=2),
            ).slot
            if claimed.state is not SlotState.CLAIMED:
                raise WorkerCycleError("ready strategy slot could not be claimed")
            return WorkerCycleResult(
                WorkerCycleState.PROGRESSED,
                "strategy_slot_claimed",
                work_units + 1,
            )

        forced = schedule.forced_flat_slot
        if forced is None:
            raise WorkerCycleError("forced-flat slot was not scheduled")
        persisted = repository.get(forced.slot_id)
        if persisted is None:
            raise WorkerCycleError("forced-flat slot was not persisted")
        if persisted.state in terminal:
            return WorkerCycleResult(WorkerCycleState.IDLE, "schedule_current", work_units)
        if persisted.state is SlotState.CLAIMED:
            if persisted.claim_owner != _STRATEGY_OWNER:
                raise WorkerCycleError("forced-flat slot has an unexpected owner")
            receipt = _slot_execution_receipt(self._engine, persisted.slot_id)
            if receipt is None:
                return WorkerCycleResult(WorkerCycleState.IDLE, "forced_flat_execution_pending")
            if receipt[0] != "CLEAN":
                repository.fail(
                    persisted.slot_id,
                    owner=_STRATEGY_OWNER,
                    reason_code="forced_flat_reconciliation_blocked",
                    now=receipt[1],
                )
                return WorkerCycleResult(WorkerCycleState.BLOCKED, "forced_flat_failed_closed")
            repository.complete(
                persisted.slot_id,
                owner=_STRATEGY_OWNER,
                now=receipt[1],
            )
            return WorkerCycleResult(
                WorkerCycleState.PROGRESSED,
                "forced_flat_completed",
                work_units + 1,
            )
        claimed = repository.claim(
            persisted.slot_id,
            owner=_STRATEGY_OWNER,
            now=persisted.ready_at + timedelta(seconds=1),
        ).slot
        if claimed.state is not SlotState.CLAIMED:
            raise WorkerCycleError("forced-flat slot could not be claimed")
        return WorkerCycleResult(
            WorkerCycleState.PROGRESSED,
            "forced_flat_claimed",
            work_units + 1,
        )

    def close(self) -> None:
        """Release no resources; scheduler cycles are transaction bounded."""


class StrategyCycle:
    """Read a claimed safe-view slot and append one declarative offline proposal."""

    def __init__(self, settings: RuntimeSettings, engine: Engine) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.STRATEGY_WORKER
            or settings.platform.profile.mode is not ExecutionMode.OFFLINE
        ):
            raise TypeError("strategy cycle requires offline strategy settings")
        if not isinstance(engine, Engine):
            raise TypeError("strategy cycle requires a database engine")
        self._settings = settings
        self._engine = engine

    def run_cycle(self) -> WorkerCycleResult:
        from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
        from adaptive_trader.platform.hashing import sha256_hex
        from adaptive_trader.platform.risk import policy_hash
        from adaptive_trader.platform.scheduling import SlotState, build_session_schedule
        from adaptive_trader.platform.signals import (
            DecisionContext,
            FixtureSignalScenario,
            OfflineFixtureSignalProvider,
            SignalEnvelopeRepository,
        )

        experiment = self._settings.platform.experiment.definition
        schedule = build_session_schedule(
            experiment=experiment,
            signal_provider_id=self._settings.platform.profile.signal_provider.id,
            signal_provider_version=_SIGNAL_PROVIDER_VERSION,
            session_date=_OFFLINE_SESSION_DATE,
            calendar=XnasExchangeCalendar(),
        )
        selected: tuple[int | None, DecisionSlot] | None = None
        repository = SignalEnvelopeRepository(self._engine)
        for index, scheduled in enumerate(schedule.slots):
            slot = _read_slot(self._engine, scheduled.slot_id)
            if slot is None or slot.state is not SlotState.CLAIMED:
                continue
            if slot.claim_owner != _STRATEGY_OWNER:
                raise WorkerCycleError("strategy slot claim owner is invalid")
            if repository.get_for_slot(slot.slot_id) is None:
                selected = (index if scheduled in schedule.strategy_slots else None, slot)
                break
        if selected is None:
            return WorkerCycleResult(WorkerCycleState.IDLE, "strategy_slot_unavailable")
        ordinal, slot = selected
        if slot.claim_owner != _STRATEGY_OWNER:
            raise WorkerCycleError("strategy slot claim owner is invalid")
        aggregate_rows = tuple(
            row
            for row in _effective_aggregate_rows(
                self._engine,
                provider="fixture",
                start_at=slot.source_interval_start,
                end_at=slot.source_interval_end,
                timeframe="15Min" if ordinal is not None else "1Min",
            )
            if row.symbol in experiment.active_tradable
        )
        by_symbol = {row.symbol: row for row in aggregate_rows}
        if set(by_symbol) != set(experiment.active_tradable) or any(
            row.quality_flags != ("complete",) or row.source_mode != "offline_fixture"
            for row in aggregate_rows
        ):
            return WorkerCycleResult(WorkerCycleState.BLOCKED, "strategy_data_incomplete")
        data_hash = sha256_hex(tuple(sorted(row.source_payload_hash for row in aggregate_rows)))
        context = DecisionContext.from_experiment(
            slot=slot,
            experiment=experiment,
            data_contract_hash=data_hash,
            policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
            execution_mode=self._settings.platform.profile.mode,
            broker_adapter=self._settings.platform.profile.execution.broker,
            submission_enabled=self._settings.platform.profile.execution.submission_enabled,
            strategy_slot_ordinal=ordinal,
        )
        if self._settings.platform.profile.signal_provider.id != "deterministic_fixture":
            raise WorkerCycleError("offline signal provider is not implemented")
        signal = OfflineFixtureSignalProvider(
            clock=lambda: slot.ready_at + timedelta(seconds=3),
            scenario=FixtureSignalScenario(
                first_slot_long_symbol=experiment.active_tradable[-2],
                first_slot_short_symbol=experiment.active_tradable[1],
                expected_edge_bps=Decimal(25),
            ),
        ).signal_for(context)
        repository.persist_once(signal, context=context)
        return WorkerCycleResult(
            WorkerCycleState.PROGRESSED,
            "signal_persisted",
            1,
        )

    def close(self) -> None:
        """Release no resources; strategy cycles are transaction bounded."""


class ExecutionCycle:
    """Apply the execution preflight and fail closed until exact risk history exists."""

    def __init__(self, settings: RuntimeSettings, engine: Engine) -> None:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.EXECUTION_WORKER
            or settings.platform.profile.mode is not ExecutionMode.OFFLINE
        ):
            raise TypeError("execution cycle requires offline fake-execution settings")
        if not isinstance(engine, Engine):
            raise TypeError("execution cycle requires a database engine")
        self._settings = settings
        self._engine = engine

    def run_cycle(self) -> WorkerCycleResult:
        from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
        from adaptive_trader.platform.execution import ReconciliationStatus
        from adaptive_trader.platform.scheduling import build_session_schedule
        from adaptive_trader.platform.signals import SignalEnvelopeRepository
        from adaptive_trader.platform.storage.execution import SignedExecutionRepository

        experiment = self._settings.platform.experiment.definition
        schedule = build_session_schedule(
            experiment=experiment,
            signal_provider_id=self._settings.platform.profile.signal_provider.id,
            signal_provider_version=_SIGNAL_PROVIDER_VERSION,
            session_date=_OFFLINE_SESSION_DATE,
            calendar=XnasExchangeCalendar(),
        )
        signals = SignalEnvelopeRepository(self._engine)
        execution = SignedExecutionRepository(self._engine)
        for ordinal, scheduled in enumerate(schedule.slots):
            slot = _read_slot(self._engine, scheduled.slot_id)
            if slot is None:
                continue
            signal = signals.get_for_slot(slot.slot_id)
            if signal is None:
                continue
            receipts = tuple(
                receipt
                for receipt in execution.reconciliations()
                if receipt.slot_id == slot.slot_id
            )
            if receipts:
                if any(receipt.status is not ReconciliationStatus.CLEAN for receipt in receipts):
                    return WorkerCycleResult(
                        WorkerCycleState.BLOCKED,
                        "execution_reconciliation_blocked",
                    )
                latest = max(receipts, key=lambda receipt: receipt.completed_at)
                previous_plan = execution.get_plan(_required_plan_id(latest))
                if not _plan_requires_reversal(previous_plan):
                    continue
            else:
                latest = None
            history = _complete_history(
                self._engine,
                active_symbols=experiment.active_tradable,
                before_session=slot.session_date,
            )
            if history is None:
                # The fake broker is intentionally not constructed before this condition passes.
                return WorkerCycleResult(WorkerCycleState.BLOCKED, "risk_history_insufficient")
            return _execute_fake_plan(
                settings=self._settings,
                engine=self._engine,
                slot=slot,
                signal=signal,
                history=history,
                ordinal=ordinal if scheduled in schedule.strategy_slots else None,
                preceding_reconciliation=latest,
            )
        return WorkerCycleResult(WorkerCycleState.IDLE, "execution_current")

    def close(self) -> None:
        """Release no resources; fake execution has no external connection."""


@dataclass(frozen=True, slots=True)
class _AggregateRow:
    symbol: str
    start_at: datetime
    end_at: datetime
    close: Decimal
    quality_flags: tuple[str, ...]
    source_mode: str
    source_payload_hash: str
    received_at: datetime


def _stored_as_effective(event: StoredBarEvent) -> EffectiveBar:
    from adaptive_trader.platform.data import CanonicalBar, EffectiveBar

    source_event_id = event.bar.source_event_id
    if source_event_id is None:
        raise WorkerCycleError("aggregate source event identity is missing")
    stored = event.bar
    return EffectiveBar(
        bar_event_id=event.bar_event_id,
        revision=event.revision,
        bar=CanonicalBar(
            provider=stored.identity.provider,
            feed=stored.identity.feed,
            adjustment=stored.identity.adjustment,
            symbol=stored.identity.symbol,
            timeframe=stored.identity.timeframe,
            source_mode=stored.source_mode,
            interval_start_utc=stored.identity.start_at,
            interval_end_utc=stored.identity.end_at,
            receipt_timestamp_utc=stored.received_at,
            provider_event_timestamp_utc=stored.provider_timestamp,
            open=stored.open,
            high=stored.high,
            low=stored.low,
            close=stored.close,
            volume=stored.volume,
            trade_count=stored.trade_count,
            vwap=stored.vwap,
            schema_version=stored.schema_version,
            source_event_id=source_event_id,
            quality_flags=stored.quality_flags,
            is_correction=stored.is_correction,
            correction_of_source_event_id=stored.correction_of_source_event_id,
        ),
    )


class _SignalViewMaterializationProbe:
    """Give the scheduler only existence evidence from the safe signal view."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def exists(self, connection: Connection, *, slot_id: str) -> bool:
        relation = (
            "aqa.aqa_signals_v"
            if connection.dialect.name == "postgresql"
            else "aqa_signal_envelopes"
        )
        statement = text(f"SELECT signal_id FROM {relation} WHERE slot_id = :slot_id LIMIT 1")
        return connection.scalar(statement, {"slot_id": slot_id}) is not None


def build_worker_cycle(settings: RuntimeSettings, engine: Engine) -> WorkerCycle:
    """Compose exactly one domain cycle from an already validated service capability set."""

    if type(settings) is not RuntimeSettings or not isinstance(engine, Engine):
        raise TypeError("worker cycle wiring requires validated settings and engine")
    if settings.service is RuntimeService.MARKET_DATA_WORKER:
        return OfflineMarketDataCycle(settings, engine)
    if settings.service is RuntimeService.SCHEDULER_WORKER:
        if settings.platform.profile.mode is not ExecutionMode.OFFLINE:
            from adaptive_trader.platform.operational_scheduler import OperationalSchedulerCycle

            return OperationalSchedulerCycle(settings, engine)
        return SchedulerCycle(settings, engine)
    if settings.service is RuntimeService.STRATEGY_WORKER:
        if settings.platform.profile.mode in {ExecutionMode.SHADOW, ExecutionMode.PAPER}:
            from adaptive_trader.platform.operational_strategy import OperationalStrategyCycle

            return OperationalStrategyCycle(settings, engine)
        return StrategyCycle(settings, engine)
    if settings.service is RuntimeService.EXECUTION_WORKER:
        return ExecutionCycle(settings, engine)
    if settings.service is RuntimeService.MARKET_DATA_LIVE:
        raise WorkerCycleError(
            "live data requires the canonical collection service lifecycle; use collection.cli run"
        )
    raise WorkerCycleError("service has no bounded domain cycle")


def _offline_fixture_payload(*, symbol: str, minute: int) -> dict[str, object]:
    symbol_offset = Decimal(sum(ord(character) for character in symbol) % 40)
    base = Decimal(80) + symbol_offset + (Decimal(minute) / Decimal(10))
    return {
        "S": symbol,
        "t": (_OFFLINE_SESSION_OPEN + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "o": base,
        "h": base + Decimal(1),
        "l": base - Decimal(1),
        "c": base + Decimal("0.25"),
        "v": 1_000 + minute,
        "n": 20 + minute,
        "vw": base + Decimal("0.10"),
    }


def _seed_offline_risk_history(
    *,
    repository: MarketDataRepository,
    calendar: XnasExchangeCalendar,
    active_symbols: tuple[str, ...],
) -> int:
    """Append the exact twenty full prior sessions required by signed-risk statistics."""

    from adaptive_trader.platform.hashing import sha256_hex
    from adaptive_trader.platform.risk.statistics import BARS_PER_SESSION, SESSION_COUNT
    from adaptive_trader.platform.storage.market_data import (
        BarIdentity,
        BarWrite,
        BarWriteStatus,
    )

    sessions: list[TradingSession] = []
    cursor = _OFFLINE_SESSION_DATE - timedelta(days=1)
    while len(sessions) < SESSION_COUNT:
        candidate = calendar.session(cursor)
        if candidate is not None and candidate.is_standard_full_session:
            sessions.append(candidate)
        cursor -= timedelta(days=1)
    changes = 0
    # Each append owns its lock scope; replay resumes an interrupted fixture seed safely.
    for session_number, session in enumerate(reversed(sessions), start=1):
        for symbol in active_symbols:
            symbol_offset = Decimal(sum(ord(character) for character in symbol) % 40)
            for bucket in range(BARS_PER_SESSION):
                start_at = session.open_at + timedelta(minutes=15 * bucket)
                end_at = start_at + timedelta(minutes=15)
                close = (
                    Decimal(80)
                    + symbol_offset
                    + Decimal(session_number) / Decimal(10)
                    + Decimal(bucket) / Decimal(100)
                )
                source_event_id = (
                    f"fixture_history_{session.session_date:%Y%m%d}_{symbol}_{bucket:02d}"
                )
                result = repository.append(
                    BarWrite(
                        identity=BarIdentity(
                            provider="fixture",
                            feed="iex",
                            adjustment="raw",
                            symbol=symbol,
                            timeframe="15Min",
                            start_at=start_at,
                            end_at=end_at,
                        ),
                        received_at=session.close_at + timedelta(minutes=1),
                        open=close - Decimal("0.05"),
                        high=close + Decimal("0.10"),
                        low=close - Decimal("0.10"),
                        close=close,
                        volume=Decimal(15_000 + bucket),
                        trade_count=300 + bucket,
                        vwap=close,
                        quality_flags=("complete",),
                        source="fixture",
                        source_payload_hash=sha256_hex(
                            ("offline_risk_history_v1", source_event_id, close)
                        ),
                        source_mode="offline_fixture",
                        source_event_id=source_event_id,
                    ),
                )
                changes += int(result.status in {BarWriteStatus.INSERTED, BarWriteStatus.CORRECTED})
    return changes


def _signal_exists(engine: Engine, slot_id: str) -> bool:
    relation = (
        "aqa.aqa_signals_v" if engine.dialect.name == "postgresql" else "aqa_signal_envelopes"
    )
    with engine.begin() as connection:
        return (
            connection.scalar(
                text(f"SELECT signal_id FROM {relation} WHERE slot_id = :slot_id LIMIT 1"),
                {"slot_id": slot_id},
            )
            is not None
        )


def _required_plan_id(receipt: ReconciliationReceipt) -> str:
    if receipt.execution_plan_id is None:
        raise WorkerCycleError("slot reconciliation has no execution plan")
    return receipt.execution_plan_id


def _plan_requires_reversal(plan: ExecutionPlan) -> bool:
    current = {item.symbol: item.quantity for item in plan.current_positions}
    return any(current[item.symbol] * item.quantity < 0 for item in plan.target_quantities)


def _slot_execution_receipt(engine: Engine, slot_id: str) -> tuple[str, datetime] | None:
    """Read execution-owned completion evidence through the scheduler's safe view."""
    if engine.dialect.name == "sqlite":
        from adaptive_trader.platform.storage.execution import SignedExecutionRepository

        repository = SignedExecutionRepository(engine)
        receipts = sorted(
            (receipt for receipt in repository.reconciliations() if receipt.slot_id == slot_id),
            key=lambda receipt: receipt.completed_at,
            reverse=True,
        )
        if not receipts:
            return None
        receipt = receipts[0]
        if receipt.status.value == "CLEAN" and _plan_requires_reversal(
            repository.get_plan(_required_plan_id(receipt))
        ):
            return None
        return receipt.status.value, receipt.completed_at
    else:
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT status, completed_at FROM aqa.aqa_execution_completion_v "
                    "WHERE slot_id = :slot_id ORDER BY completed_at DESC LIMIT 1"
                ),
                {"slot_id": slot_id},
            ).one_or_none()
    return None if row is None else (row[0], row[1])


def _read_slot(engine: Engine, slot_id: str) -> DecisionSlot | None:
    from adaptive_trader.platform.scheduling import DecisionSlot, DecisionSlotRepository
    from adaptive_trader.platform.scheduling.models import DecisionType, SlotState

    if engine.dialect.name == "sqlite":
        return DecisionSlotRepository(engine).get(slot_id)
    statement = text(
        "SELECT slot_id, experiment_id, experiment_version, experiment_hash, "
        "signal_provider_id, signal_provider_version, session_date, source_interval_start, "
        "source_interval_end, decision_type, ready_at, deadline_at, required_completion_at, "
        "state, claim_owner, claimed_at, lease_expires_at, attempt_count, completed_at, "
        "reason_code, correlation_id, content_hash, version "
        "FROM aqa.aqa_decision_slots_v WHERE slot_id = :slot_id"
    )
    with engine.begin() as connection:
        row = connection.execute(statement, {"slot_id": slot_id}).mappings().one_or_none()
    if row is None:
        return None
    return DecisionSlot(
        slot_id=row["slot_id"],
        experiment_id=row["experiment_id"],
        experiment_version=row["experiment_version"],
        experiment_hash=row["experiment_hash"],
        signal_provider_id=row["signal_provider_id"],
        signal_provider_version=row["signal_provider_version"],
        session_date=row["session_date"],
        source_interval_start=_slot_timestamp(row["source_interval_start"]),
        source_interval_end=_slot_timestamp(row["source_interval_end"]),
        ready_at=_slot_timestamp(row["ready_at"]),
        deadline_at=_slot_timestamp(row["deadline_at"]),
        required_completion_at=_slot_timestamp(row["required_completion_at"]),
        decision_type=DecisionType(row["decision_type"]),
        state=SlotState(row["state"]),
        claim_owner=row["claim_owner"],
        claimed_at=_optional_slot_timestamp(row["claimed_at"]),
        lease_expires_at=_optional_slot_timestamp(row["lease_expires_at"]),
        attempt_count=row["attempt_count"],
        completed_at=_optional_slot_timestamp(row["completed_at"]),
        reason_code=row["reason_code"],
        correlation_id=row["correlation_id"],
        content_hash=row["content_hash"],
        version=row["version"],
    )


def _slot_timestamp(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise WorkerCycleError("durable slot timestamp is invalid")
    return value.astimezone(UTC)


def _optional_slot_timestamp(value: object) -> datetime | None:
    return None if value is None else _slot_timestamp(value)


def _effective_aggregate_rows(
    engine: Engine,
    *,
    provider: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timeframe: str = "15Min",
    range_start_at: datetime | None = None,
    range_end_at: datetime | None = None,
    symbols: tuple[str, ...] | None = None,
) -> tuple[_AggregateRow, ...]:
    from adaptive_trader.platform.storage.tables import (
        aqa_bar_events,
        aqa_bar_identities,
        aqa_bar_latest,
    )

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            clauses = [
                "provider = :provider",
                "timeframe = :timeframe",
                "feed = 'iex'",
                "adjustment = 'raw'",
            ]
            parameters: dict[str, object] = {"provider": provider, "timeframe": timeframe}
            if start_at is not None:
                clauses.append("start_at = :start_at")
                parameters["start_at"] = start_at
            if end_at is not None:
                clauses.append("end_at = :end_at")
                parameters["end_at"] = end_at
            if range_start_at is not None:
                clauses.append("start_at >= :range_start_at")
                parameters["range_start_at"] = range_start_at
            if range_end_at is not None:
                clauses.append("end_at <= :range_end_at")
                parameters["range_end_at"] = range_end_at
            if symbols is not None:
                clauses.append("symbol = ANY(:symbols)")
                parameters["symbols"] = list(symbols)
            mappings = (
                connection.execute(
                    text(
                        "SELECT symbol, start_at, end_at, close, quality_flags, source_mode, "
                        "source_payload_hash, received_at FROM aqa.aqa_effective_bars_v WHERE "
                        + " AND ".join(clauses)
                    ),
                    parameters,
                )
                .mappings()
                .all()
            )
        else:
            selected = (
                select(
                    aqa_bar_identities.c.symbol,
                    aqa_bar_identities.c.start_at,
                    aqa_bar_identities.c.end_at,
                    aqa_bar_events.c.close,
                    aqa_bar_events.c.quality_flags,
                    aqa_bar_events.c.source_mode,
                    aqa_bar_events.c.source_payload_hash,
                    aqa_bar_events.c.received_at,
                )
                .select_from(
                    aqa_bar_latest.join(
                        aqa_bar_identities,
                        aqa_bar_latest.c.bar_identity_id == aqa_bar_identities.c.bar_identity_id,
                    ).join(
                        aqa_bar_events,
                        and_(
                            aqa_bar_events.c.bar_identity_id == aqa_bar_latest.c.bar_identity_id,
                            aqa_bar_events.c.bar_event_id == aqa_bar_latest.c.bar_event_id,
                            aqa_bar_events.c.revision == aqa_bar_latest.c.revision,
                        ),
                    )
                )
                .where(
                    aqa_bar_identities.c.provider == provider,
                    aqa_bar_identities.c.timeframe == timeframe,
                    aqa_bar_identities.c.feed == "iex",
                    aqa_bar_identities.c.adjustment == "raw",
                )
            )
            if start_at is not None:
                selected = selected.where(aqa_bar_identities.c.start_at == start_at)
            if end_at is not None:
                selected = selected.where(aqa_bar_identities.c.end_at == end_at)
            if range_start_at is not None:
                selected = selected.where(aqa_bar_identities.c.start_at >= range_start_at)
            if range_end_at is not None:
                selected = selected.where(aqa_bar_identities.c.end_at <= range_end_at)
            if symbols is not None:
                selected = selected.where(aqa_bar_identities.c.symbol.in_(symbols))
            mappings = connection.execute(selected).mappings().all()
    rows: list[_AggregateRow] = []
    for row in mappings:
        flags = row["quality_flags"]
        rows.append(
            _AggregateRow(
                symbol=row["symbol"],
                start_at=row["start_at"],
                end_at=row["end_at"],
                close=Decimal(row["close"]),
                quality_flags=tuple(flags),
                source_mode=row["source_mode"],
                source_payload_hash=row["source_payload_hash"],
                received_at=row["received_at"],
            )
        )
    return tuple(sorted(rows, key=lambda item: (item.start_at, item.symbol)))


def _complete_history(
    engine: Engine,
    *,
    active_symbols: tuple[str, ...],
    before_session: date,
    provider: str = "fixture",
    observed_at: datetime | None = None,
) -> dict[str, tuple[FullSessionCloses, ...]] | None:
    from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
    from adaptive_trader.platform.risk import FullSessionCloses
    from adaptive_trader.platform.risk.statistics import BARS_PER_SESSION, SESSION_COUNT

    if not active_symbols:
        return None
    calendar = XnasExchangeCalendar()
    sessions = []
    cursor = before_session - timedelta(days=1)
    for _ in range(370):
        session = calendar.session(cursor)
        if session is not None and session.is_standard_full_session:
            sessions.append(session)
            if len(sessions) == SESSION_COUNT:
                break
        cursor -= timedelta(days=1)
    if len(sessions) != SESSION_COUNT:
        return None
    grouped: dict[str, dict[date, list[_AggregateRow]]] = {symbol: {} for symbol in active_symbols}
    for row in _effective_aggregate_rows(
        engine,
        provider=provider,
        range_start_at=sessions[-1].open_at,
        range_end_at=sessions[0].close_at,
        symbols=active_symbols,
    ):
        session_date = row.start_at.date()
        if (
            row.symbol not in grouped
            or session_date >= before_session
            or row.quality_flags != ("complete",)
            or row.source_mode
            != ("offline_fixture" if provider == "fixture" else "external_provider")
            or (observed_at is not None and row.received_at > observed_at)
        ):
            continue
        grouped[row.symbol].setdefault(session_date, []).append(row)
    common_dates = set.intersection(*(set(grouped[symbol]) for symbol in active_symbols))
    complete_dates = tuple(
        session_date
        for session_date in sorted(common_dates)
        if all(len(grouped[symbol][session_date]) == BARS_PER_SESSION for symbol in active_symbols)
    )
    if len(complete_dates) < SESSION_COUNT:
        return None
    selected_dates = complete_dates[-SESSION_COUNT:]
    return {
        symbol: tuple(
            FullSessionCloses(
                session_date=session_date,
                closes=tuple(
                    row.close
                    for row in sorted(
                        grouped[symbol][session_date],
                        key=lambda item: item.start_at,
                    )
                ),
            )
            for session_date in selected_dates
        )
        for symbol in active_symbols
    }


def _execute_fake_plan(
    *,
    settings: RuntimeSettings,
    engine: Engine,
    slot: DecisionSlot,
    signal: SignalEnvelope,
    history: Mapping[str, tuple[FullSessionCloses, ...]],
    ordinal: int | None,
    preceding_reconciliation: ReconciliationReceipt | None = None,
) -> WorkerCycleResult:
    """Evaluate, persist, submit, and reconcile one fake-broker plan after full preflight."""

    from adaptive_trader.platform.execution import (
        DeterministicFakePaperBroker,
        ExecutionPlanningRequest,
        ExecutionService,
        Position,
        ReconciliationRequest,
        ReconciliationStatus,
        SubmissionSafetySnapshot,
        plan_signed_orders,
        reconcile_and_persist,
    )
    from adaptive_trader.platform.hashing import sha256_hex
    from adaptive_trader.platform.risk import (
        AccountSnapshot,
        MarketIntegritySnapshot,
        OpenOrderSnapshot,
        PlanningPrice,
        ReconciliationSnapshot,
        RiskEvaluationRequest,
        SecurityMetadataSnapshot,
        SignedPosition,
        compute_risk_statistics,
        evaluate_signed_risk,
        policy_hash,
    )
    from adaptive_trader.platform.signals import DecisionContext
    from adaptive_trader.platform.storage.execution import SignedExecutionRepository
    from adaptive_trader.platform.storage.risk import SignedRiskRepository

    experiment = settings.platform.experiment.definition
    current_rows = tuple(
        row
        for row in _effective_aggregate_rows(
            engine,
            provider="fixture",
            start_at=slot.source_interval_start,
            end_at=slot.source_interval_end,
            timeframe="15Min" if ordinal is not None else "1Min",
        )
        if row.symbol in experiment.active_tradable
    )
    if {row.symbol for row in current_rows} != set(experiment.active_tradable):
        return WorkerCycleResult(WorkerCycleState.BLOCKED, "execution_marks_incomplete")
    if any(
        row.quality_flags != ("complete",) or row.source_mode != "offline_fixture"
        for row in current_rows
    ):
        return WorkerCycleResult(WorkerCycleState.BLOCKED, "execution_marks_invalid")
    if (
        sha256_hex(tuple(sorted(row.source_payload_hash for row in current_rows)))
        != signal.data_contract_hash
    ):
        return WorkerCycleResult(WorkerCycleState.BLOCKED, "decision_data_revision_changed")
    marks = tuple(sorted((row.symbol, row.close) for row in current_rows))
    decided_at = (
        slot.ready_at + timedelta(seconds=4)
        if preceding_reconciliation is None
        else preceding_reconciliation.completed_at + timedelta(seconds=1)
    )
    if decided_at + timedelta(seconds=4) >= slot.deadline_at:
        return WorkerCycleResult(WorkerCycleState.BLOCKED, "reversal_deadline_passed")
    execution_stage = 1 if preceding_reconciliation is None else 2
    selected_policy_hash = policy_hash(experiment.risk_policy, experiment.risk_groups)
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=signal.data_contract_hash,
        policy_hash=selected_policy_hash,
        execution_mode=settings.platform.profile.mode,
        broker_adapter=settings.platform.profile.execution.broker,
        submission_enabled=settings.platform.profile.execution.submission_enabled,
        strategy_slot_ordinal=ordinal,
    )
    signal.validate_for(context)
    statistics = compute_risk_statistics(
        active_symbols=experiment.active_tradable,
        history=history,
        as_of_date=slot.session_date,
        eigenvalue_floor=experiment.risk_policy.covariance_eigenvalue_floor,
    )
    repository = SignedExecutionRepository(engine)
    broker = _restore_fake_broker(repository, experiment_hash=experiment.content_hash)
    broker.set_mark_prices(marks)
    account_state = broker.account(observed_at=decided_at)
    risk_repository = SignedRiskRepository(engine)
    decision = risk_repository.decision_for_signal(
        signal.signal_id, execution_stage=execution_stage
    )
    if decision is None:
        decision = evaluate_signed_risk(
            request=RiskEvaluationRequest(
                signal=signal,
                decision_context=context,
                positions=tuple(
                    SignedPosition(
                        symbol=symbol,
                        quantity=dict(
                            (position.symbol, position.quantity) for position in broker.positions()
                        ).get(symbol, Decimal(0)),
                    )
                    for symbol in experiment.active_tradable
                ),
                open_orders=OpenOrderSnapshot.create(
                    reserved_signed_notional={
                        symbol: Decimal(0) for symbol in experiment.active_tradable
                    },
                    conflicting_symbols=(),
                    ambiguous_order_exists=False,
                    observed_at=decided_at,
                ),
                account=AccountSnapshot(
                    account_id_hash=account_state.account_id_hash,
                    equity=account_state.equity,
                    cash=account_state.cash,
                    buying_power=account_state.buying_power,
                    observed_at=decided_at,
                ),
                prices=tuple(
                    PlanningPrice(
                        symbol=symbol,
                        price=price,
                        observed_at=decided_at,
                        validated=True,
                    )
                    for symbol, price in marks
                ),
                security_metadata=tuple(
                    SecurityMetadataSnapshot(
                        symbol=symbol,
                        asset_active=True,
                        tradable=True,
                        shortable=True,
                        easy_to_borrow=True,
                        primary_listing_eligible=True,
                        broker_capability_known=True,
                        observed_at=decided_at,
                    )
                    for symbol in experiment.active_tradable
                ),
                reconciliation=ReconciliationSnapshot.create(
                    reconciled=True,
                    ambiguous_order_exists=False,
                    observed_at=decided_at,
                ),
                market_integrity=MarketIntegritySnapshot.create(
                    active_basket_complete=True,
                    unresolved_gap=False,
                    correction_uncertainty=False,
                    supported_session=True,
                ),
                session_start_equity=DeterministicFakePaperBroker.INITIAL_CASH,
                deployment_high_water_equity=max(
                    (
                        DeterministicFakePaperBroker.INITIAL_CASH,
                        account_state.equity,
                        *(
                            receipt.observed_equity
                            for receipt in repository.reconciliations()
                            if receipt.experiment_hash == experiment.content_hash
                        ),
                    )
                ),
                statistics=statistics,
                latch_state=risk_repository.latch_state(experiment.content_hash),
                operator_halt=False,
                evaluated_at=decided_at,
            ),
            experiment=experiment,
        )
        risk_repository.persist(
            decision,
            preceding_reconciliation_id=(
                None
                if preceding_reconciliation is None
                else preceding_reconciliation.reconciliation_id
            ),
        )
    planning = plan_signed_orders(
        ExecutionPlanningRequest(
            risk_decision=decision,
            current_positions=tuple(
                Position(item.symbol, item.quantity) for item in decision.planning_positions
            ),
            reference_prices=tuple((item.symbol, item.price) for item in decision.planning_prices),
            equity=decision.account_snapshot.equity,
            target_version=execution_stage,
            created_at=decided_at + timedelta(seconds=1),
            deadline_at=slot.deadline_at,
            forced_flat=ordinal is None,
        )
    )
    service = ExecutionService(repository=repository, broker=broker)
    safety = SubmissionSafetySnapshot(
        evaluated_at=decided_at + timedelta(seconds=2),
        session_open=True,
        data_complete=True,
        account_observed_at=decided_at + timedelta(seconds=2),
        security_observed_at=decided_at,
        reconciliation_observed_at=decided_at,
        price_observed_at=decided_at,
        reconciliation_clean=True,
        ambiguous_order_exists=False,
        blocking_latch_exists=bool(risk_repository.latch_state(experiment.content_hash).active),
        entry_disabled=ordinal is None,
        active_symbols=experiment.active_tradable,
        shortable_symbols=experiment.active_tradable,
    )
    outcomes = service.submit_plan(planning, safety_provider=lambda _intent: safety)
    if any(
        not outcome.submitted and "already_terminal" not in outcome.reason_codes
        for outcome in outcomes
    ):
        raise WorkerCycleError("fake execution submission was blocked")
    client_ids = {intent.client_order_id for intent in planning.intents}
    all_intents = tuple(
        item for item in repository.all_intents() if item.experiment_hash == experiment.content_hash
    )
    all_ids = {item.client_order_id for item in all_intents}
    durable_orders = tuple(
        order for order in repository.all_orders() if order.client_order_id in all_ids
    )
    order_fills = tuple(fill for fill in repository.fills() if fill.client_order_id in all_ids)
    fills = tuple(fill for fill in order_fills if fill.client_order_id in client_ids)
    completed_at = decided_at + timedelta(seconds=4)
    reconciliation = reconcile_and_persist(
        repository=repository,
        request=ReconciliationRequest(
            experiment_hash=experiment.content_hash,
            slot_id=slot.slot_id,
            execution_plan_id=planning.plan.execution_plan_id,
            correlation_id=slot.correlation_id,
            active_symbols=experiment.active_tradable,
            short_eligible_symbols=experiment.active_tradable,
            baseline_positions=planning.plan.current_positions,
            baseline_cash=decision.account_snapshot.cash,
            fills=fills,
            order_fills=order_fills,
            intents=all_intents,
            durable_orders=durable_orders,
            broker_orders=durable_orders,
            broker_positions=broker.positions(),
            broker_account=broker.account(observed_at=completed_at),
            expected_account_id_hash=decision.account_snapshot.account_id_hash,
            mark_prices=planning.plan.reference_prices,
            started_at=completed_at - timedelta(seconds=1),
            completed_at=completed_at,
            require_flat=ordinal is None,
            required_flat_at=slot.required_completion_at if ordinal is None else None,
        ),
        latch_state=risk_repository.latch_state(experiment.content_hash),
    )
    if reconciliation.receipt.status is not ReconciliationStatus.CLEAN:
        raise WorkerCycleError("fake execution reconciliation was not clean")
    return WorkerCycleResult(
        WorkerCycleState.PROGRESSED,
        "reversal_close_reconciled"
        if planning.reversal_barrier_required
        else "fake_execution_reconciled",
        len(planning.intents),
    )


def _restore_fake_broker(
    repository: SignedExecutionRepository, *, experiment_hash: str
) -> DeterministicFakePaperBroker:
    """Rebuild the deterministic offline account from verified intent/fill evidence.

    Replay is confined to the in-memory fake adapter. Previously accepted intents reconstruct
    their original projection; unsubmitted intents never create an economic action. Mismatched
    fill evidence and unsupported lifecycle states stop the worker before new exposure.
    """
    from adaptive_trader.platform.execution import DeterministicFakePaperBroker, OrderState

    broker = DeterministicFakePaperBroker(initial_time=_OFFLINE_SESSION_OPEN)
    durable_fills = {fill.fill_id: fill for fill in repository.fills()}
    intents = sorted(
        (item for item in repository.all_intents() if item.experiment_hash == experiment_hash),
        key=lambda item: (item.created_at, item.sequence, item.client_order_id),
    )
    for intent in intents:
        order = repository.get_order(intent.client_order_id)
        if order.state is OrderState.INTENT_COMMITTED:
            continue
        if order.state not in {
            OrderState.FILLED,
            OrderState.SUBMISSION_STARTED,
            OrderState.SUBMISSION_UNKNOWN,
            OrderState.RECONCILIATION_REQUIRED,
        }:
            raise WorkerCycleError("fake broker lifecycle requires explicit recovery")
        repository.get_plan(intent.execution_plan_id)
        if order.submitted_at is None:
            raise WorkerCycleError("fake broker submission timestamp is unavailable")
        update = broker.submit(intent, submitted_at=order.submitted_at)
        if order.state is OrderState.FILLED:
            if any(durable_fills.get(fill.fill_id) != fill for fill in update.fills):
                raise WorkerCycleError("fake broker replay disagrees with durable fills")
        else:
            repository.apply_broker_update(update)
    return broker


__all__ = [
    "ExecutionCycle",
    "OfflineMarketDataCycle",
    "SchedulerCycle",
    "StrategyCycle",
    "WorkerCycle",
    "WorkerCycleError",
    "WorkerCycleResult",
    "WorkerCycleState",
    "build_worker_cycle",
]
