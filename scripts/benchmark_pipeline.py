#!/usr/bin/env python3
"""Measure deterministic offline platform stages without enforcing timing thresholds."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from typing import Any

from sqlalchemy import create_engine, event, insert

from adaptive_trader.platform.config import BrokerAdapter, ExecutionMode, load_experiment
from adaptive_trader.platform.data import (
    EffectiveBar,
    NormalizationPolicy,
    SessionWindow,
    aggregate_one_minute_bars,
    normalize_fixture_bar,
)
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.execution import (
    DeterministicFakePaperBroker,
    ExecutionPlanningRequest,
    ExecutionService,
    MemoryExecutionRepository,
    OrderIntent,
    Position,
    ReconciliationRequest,
    SubmissionSafetySnapshot,
    plan_signed_orders,
    reconcile,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    ANNUALIZATION_FACTOR,
    DEFAULT_EIGENVALUE_FLOOR,
    RETURNS_PER_SYMBOL,
    AccountSnapshot,
    MarketIntegritySnapshot,
    OpenOrderSnapshot,
    PlanningPrice,
    ReconciliationSnapshot,
    RiskDecision,
    RiskEvaluationRequest,
    RiskLatchState,
    RiskStatistics,
    SecurityMetadataSnapshot,
    SignedPosition,
    evaluate_signed_risk,
    policy_hash,
)
from adaptive_trader.platform.scheduling import DecisionSlotRepository, build_session_schedule
from adaptive_trader.platform.signals import (
    DecisionContext,
    FixtureSignalScenario,
    OfflineFixtureSignalProvider,
)
from adaptive_trader.platform.storage.market_data import BarIdentity, BarWrite, MarketDataRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, aqa_experiments, metadata

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_ROOT = _ROOT / "configs"
_EXPERIMENT_PATH = Path("experiments/semiconductor_network_intraday_v1.yaml")
_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_END = _START + timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class _Measurement:
    operation_count: int
    elapsed_ns: int


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure deterministic offline pipeline stages and emit JSON.",
    )
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=25)
    return parser.parse_args()


def _positive_count(value: int, *, name: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= 100:
        raise ValueError(f"{name} must be between {minimum} and 100")
    return value


def _measure(
    operation: Callable[[], int],
    *,
    warmups: int,
    repeats: int,
) -> dict[str, object]:
    for _ in range(warmups):
        operation()
    samples: list[_Measurement] = []
    for _ in range(repeats):
        started = perf_counter_ns()
        count = operation()
        elapsed = perf_counter_ns() - started
        samples.append(_Measurement(operation_count=count, elapsed_ns=elapsed))
    counts = {sample.operation_count for sample in samples}
    if len(counts) != 1:
        raise RuntimeError("benchmark operation count changed between repeats")
    operation_count = samples[0].operation_count
    elapsed_seconds = [sample.elapsed_ns / 1_000_000_000 for sample in samples]
    throughput = [operation_count / value for value in elapsed_seconds]
    latency_ms = [(value * 1_000) / operation_count for value in elapsed_seconds]
    return {
        "elapsed_seconds": {
            "maximum": max(elapsed_seconds),
            "median": statistics.median(elapsed_seconds),
            "minimum": min(elapsed_seconds),
        },
        "latency_ms_per_operation": {
            "maximum": max(latency_ms),
            "median": statistics.median(latency_ms),
            "minimum": min(latency_ms),
        },
        "operation_count_per_repeat": operation_count,
        "repeat_count": repeats,
        "throughput_operations_per_second": {
            "maximum": max(throughput),
            "median": statistics.median(throughput),
            "minimum": min(throughput),
        },
    }


def _payload(symbol: str, minute: int) -> dict[str, object]:
    start = _START + timedelta(minutes=minute)
    base = Decimal(100) + (Decimal(minute) / Decimal(10))
    return {
        "S": symbol,
        "t": start.isoformat().replace("+00:00", "Z"),
        "o": base,
        "h": base + Decimal(1),
        "l": base - Decimal(1),
        "c": base + Decimal("0.25"),
        "v": 1_000 + minute,
        "n": 20 + minute,
        "vw": base + Decimal("0.10"),
    }


def _normalized_bars(policy: NormalizationPolicy, count: int) -> tuple[Any, ...]:
    receipt = _START + timedelta(minutes=count + 1)
    return tuple(
        normalize_fixture_bar(
            _payload("AMD", minute),
            policy=policy,
            receipt_timestamp_utc=receipt,
            execution_reference=True,
            quality_flags=("complete",),
            source_event_id=f"benchmark:AMD:{minute:03d}",
        )
        for minute in range(count)
    )


def _bar_write(bar: Any) -> BarWrite:
    return BarWrite(
        identity=BarIdentity(
            provider=bar.provider,
            feed=bar.feed,
            adjustment=bar.adjustment,
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            start_at=bar.interval_start_utc,
            end_at=bar.interval_end_utc,
        ),
        received_at=bar.receipt_timestamp_utc,
        provider_timestamp=bar.provider_event_timestamp_utc,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        trade_count=bar.trade_count,
        vwap=bar.vwap,
        quality_flags=bar.quality_flags,
        source="fixture",
        source_payload_hash=bar.payload_hash,
        source_mode=bar.source_mode,
        source_event_id=bar.source_event_id,
    )


def _sqlite_engine(path: Path) -> Any:
    engine = create_engine(f"sqlite+pysqlite:///{path}").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )

    @event.listens_for(engine, "connect")
    def configure(connection: Any, record: object) -> None:
        del record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine


def _register(engine: Any, experiment: Any) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=experiment.content_hash,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.experiment_version,
                schema_version=experiment.schema_version,
                configuration={"benchmark": True},
                content_hash=experiment.content_hash,
                registered_at=_START - timedelta(minutes=1),
            )
        )


def _normalization_operation(policy: NormalizationPolicy, iterations: int) -> Callable[[], int]:
    def operation() -> int:
        bars = _normalized_bars(policy, iterations)
        if len({bar.payload_hash for bar in bars}) != iterations:
            raise RuntimeError("normalization benchmark produced duplicate identities")
        return len(bars)

    return operation


def _persistence_operation(
    experiment: Any,
    bars: tuple[Any, ...],
) -> Callable[[], int]:
    def operation() -> int:
        with TemporaryDirectory(prefix="aqa-benchmark-ingest-") as directory:
            engine = _sqlite_engine(Path(directory) / "state.sqlite3")
            try:
                metadata.create_all(engine)
                _register(engine, experiment)
                repository = MarketDataRepository(engine)
                results = tuple(repository.append(_bar_write(bar)) for bar in bars)
                if len({item.event.content_hash for item in results}) != len(bars):
                    raise RuntimeError("persistence benchmark lost an event")
                return len(results)
            finally:
                engine.dispose()

    return operation


def _aggregation_operation(
    policy: NormalizationPolicy,
    iterations: int,
) -> Callable[[], int]:
    bars = _normalized_bars(policy, 15)
    effective = tuple(
        EffectiveBar(
            bar_event_id=f"bar_event_{sha256_hex(('benchmark-event', index))}",
            revision=1,
            bar=bar,
        )
        for index, bar in enumerate(bars)
    )
    session = SessionWindow(
        session_open_utc=_START,
        session_close_utc=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )

    def operation() -> int:
        hashes = tuple(
            aggregate_one_minute_bars(effective, session=session).result_hash
            for _ in range(iterations)
        )
        if len(set(hashes)) != 1:
            raise RuntimeError("aggregation benchmark is not deterministic")
        return len(hashes)

    return operation


def _slot_claim_operation(experiment: Any) -> Callable[[], int]:
    def operation() -> int:
        with TemporaryDirectory(prefix="aqa-benchmark-slot-") as directory:
            engine = _sqlite_engine(Path(directory) / "state.sqlite3")
            try:
                metadata.create_all(engine)
                _register(engine, experiment)
                repository = DecisionSlotRepository(engine)
                schedule = build_session_schedule(
                    experiment=experiment,
                    signal_provider_id="deterministic_fixture",
                    signal_provider_version="1",
                    session_date=date(2026, 7, 6),
                    calendar=XnasExchangeCalendar(),
                )
                repository.create_schedule(
                    schedule,
                    recorded_at=_START - timedelta(minutes=1),
                )
                slot = schedule.strategy_slots[0]
                repository.evaluate_readiness(
                    slot.slot_id,
                    active_basket_watermark=slot.source_interval_end,
                    now=slot.ready_at + timedelta(seconds=1),
                )
                claimed = repository.claim(
                    slot.slot_id,
                    owner="benchmark",
                    now=slot.ready_at + timedelta(seconds=2),
                )
                if claimed.slot.claim_owner != "benchmark":
                    raise RuntimeError("slot benchmark did not acquire its lease")
                return 1
            finally:
                engine.dispose()

    return operation


def _statistics(symbols: tuple[str, ...]) -> RiskStatistics:
    covariance = tuple(
        tuple(Decimal("0.04") if left == right else Decimal(0) for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    correlation = tuple(
        tuple(Decimal(1) if left == right else Decimal(0) for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    sigma = (Decimal("0.2"),) * len(symbols)
    input_hash = sha256_hex(("benchmark-risk-input", symbols))
    output_hash = sha256_hex(
        {
            "annualization_factor": ANNUALIZATION_FACTOR,
            "annualized_covariance": covariance,
            "annualized_sigma": sigma,
            "eigenvalue_floor": DEFAULT_EIGENVALUE_FLOOR,
            "input_hash": input_hash,
            "observation_count": RETURNS_PER_SYMBOL,
            "prior_correlation": correlation,
            "schema": "signed-risk-statistics-output-v1",
            "symbols": symbols,
        }
    )
    return RiskStatistics(
        symbols=symbols,
        observation_count=RETURNS_PER_SYMBOL,
        annualization_factor=ANNUALIZATION_FACTOR,
        eigenvalue_floor=DEFAULT_EIGENVALUE_FLOOR,
        annualized_covariance=covariance,
        prior_correlation=correlation,
        annualized_sigma=sigma,
        input_hash=input_hash,
        output_hash=output_hash,
    )


def _risk_request(experiment: Any) -> RiskEvaluationRequest:
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="deterministic_fixture",
        signal_provider_version="1",
        session_date=date(2026, 7, 6),
        calendar=XnasExchangeCalendar(),
    )
    slot = schedule.strategy_slots[0]
    selected_policy_hash = policy_hash(experiment.risk_policy, experiment.risk_groups)
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=sha256_hex(("benchmark-data", 1)),
        policy_hash=selected_policy_hash,
        execution_mode=ExecutionMode.OFFLINE,
        broker_adapter=BrokerAdapter.FAKE,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )
    signal = OfflineFixtureSignalProvider(
        clock=lambda: slot.ready_at,
        scenario=FixtureSignalScenario(
            first_slot_long_symbol="NVDA",
            first_slot_short_symbol="AMD",
            expected_edge_bps=Decimal(25),
        ),
    ).signal_for(context)
    now = slot.ready_at + timedelta(seconds=1)
    symbols = experiment.active_tradable
    return RiskEvaluationRequest(
        signal=signal,
        decision_context=context,
        positions=tuple(SignedPosition(symbol, Decimal(0)) for symbol in symbols),
        open_orders=OpenOrderSnapshot.create(
            reserved_signed_notional={symbol: Decimal(0) for symbol in symbols},
            conflicting_symbols=(),
            ambiguous_order_exists=False,
            observed_at=now,
        ),
        account=AccountSnapshot(
            account_id_hash=sha256_hex(
                ("broker-account-v1", DeterministicFakePaperBroker.INITIAL_ACCOUNT_ID)
            ),
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("100000"),
            observed_at=now,
        ),
        prices=tuple(PlanningPrice(symbol, Decimal(100), now, True) for symbol in symbols),
        security_metadata=tuple(
            SecurityMetadataSnapshot(symbol, True, True, True, True, True, True, now)
            for symbol in symbols
        ),
        reconciliation=ReconciliationSnapshot.create(
            reconciled=True,
            ambiguous_order_exists=False,
            observed_at=now,
        ),
        market_integrity=MarketIntegritySnapshot.create(
            active_basket_complete=True,
            unresolved_gap=False,
            correction_uncertainty=False,
            supported_session=True,
        ),
        session_start_equity=Decimal("100000"),
        deployment_high_water_equity=Decimal("100000"),
        statistics=_statistics(symbols),
        latch_state=RiskLatchState.empty(experiment_hash=experiment.content_hash),
        operator_halt=False,
        evaluated_at=now,
    )


def _risk_operation(
    experiment: Any, request: RiskEvaluationRequest, iterations: int
) -> Callable[[], int]:
    def operation() -> int:
        decisions = tuple(
            evaluate_signed_risk(request=request, experiment=experiment) for _ in range(iterations)
        )
        if len({decision.content_hash for decision in decisions}) != 1:
            raise RuntimeError("risk benchmark is not deterministic")
        return len(decisions)

    return operation


def _safety(decision: RiskDecision, now: datetime) -> SubmissionSafetySnapshot:
    symbols = tuple(symbol for symbol, _ in decision.final_targets)
    source_timestamps = dict(decision.source_timestamps)
    reconciliation_at = source_timestamps.get("reconciliation")
    if reconciliation_at is None:
        raise RuntimeError("benchmark risk decision lacks reconciliation evidence")
    return SubmissionSafetySnapshot(
        evaluated_at=now,
        session_open=True,
        data_complete=True,
        account_observed_at=now,
        security_observed_at=min(item.observed_at for item in decision.security_metadata),
        reconciliation_observed_at=reconciliation_at,
        price_observed_at=min(item.observed_at for item in decision.planning_prices),
        reconciliation_clean=True,
        ambiguous_order_exists=False,
        blocking_latch_exists=False,
        entry_disabled=False,
        active_symbols=symbols,
        shortable_symbols=tuple(
            item.symbol
            for item in decision.security_metadata
            if item.asset_active
            and item.tradable
            and item.shortable
            and item.easy_to_borrow
            and item.primary_listing_eligible
            and item.broker_capability_known
        ),
    )


def _safety_provider(
    decision: RiskDecision,
    now: datetime,
) -> Callable[[OrderIntent], SubmissionSafetySnapshot]:
    snapshot = _safety(decision, now)

    def provide(_intent: OrderIntent) -> SubmissionSafetySnapshot:
        return snapshot

    return provide


def _execution_operation(
    experiment: Any,
    risk_request: RiskEvaluationRequest,
    iterations: int,
) -> Callable[[], int]:
    decision = evaluate_signed_risk(request=risk_request, experiment=experiment)
    symbols = experiment.active_tradable
    marks = tuple((symbol, Decimal(100)) for symbol in symbols)
    created_at = risk_request.evaluated_at + timedelta(seconds=1)
    planning = ExecutionPlanningRequest(
        risk_decision=decision,
        current_positions=tuple(Position(symbol, Decimal(0)) for symbol in symbols),
        reference_prices=marks,
        equity=Decimal("100000"),
        target_version=1,
        created_at=created_at,
        deadline_at=created_at + timedelta(seconds=30),
    )

    def operation() -> int:
        receipt_hashes: list[str] = []
        for _ in range(iterations):
            now = created_at
            result = plan_signed_orders(planning)
            repository = MemoryExecutionRepository()
            broker = DeterministicFakePaperBroker(initial_time=now)
            broker.set_mark_prices(marks)
            ExecutionService(repository=repository, broker=broker).submit_plan(
                result,
                safety_provider=_safety_provider(decision, now),
            )
            account = broker.account(observed_at=now)
            receipt = reconcile(
                ReconciliationRequest(
                    experiment_hash=experiment.content_hash,
                    slot_id=decision.slot_id,
                    execution_plan_id=result.plan.execution_plan_id,
                    correlation_id=decision.correlation_id,
                    active_symbols=symbols,
                    short_eligible_symbols=symbols,
                    baseline_positions=result.plan.current_positions,
                    baseline_cash=decision.account_snapshot.cash,
                    fills=repository.fills(),
                    order_fills=repository.fills(),
                    intents=repository.all_intents(),
                    durable_orders=repository.all_orders(),
                    broker_orders=repository.all_orders(),
                    broker_positions=broker.positions(),
                    broker_account=account,
                    expected_account_id_hash=account.account_id_hash,
                    mark_prices=marks,
                    started_at=now,
                    completed_at=now,
                )
            )
            receipt_hashes.append(receipt.content_hash)
        if len(receipt_hashes) != iterations:
            raise RuntimeError("execution benchmark lost a reconciliation receipt")
        return len(receipt_hashes)

    return operation


def main() -> None:
    arguments = _arguments()
    warmups = _positive_count(arguments.warmups, name="warmups", allow_zero=True)
    repeats = _positive_count(arguments.repeats, name="repeats")
    iterations = _positive_count(arguments.iterations, name="iterations")
    experiment = load_experiment(_EXPERIMENT_PATH, config_root=_CONFIG_ROOT)
    policy = NormalizationPolicy.for_offline_fixture(experiment)
    bars = _normalized_bars(policy, iterations)
    risk_request = _risk_request(experiment)
    measurements = {
        "canonical_normalization": _measure(
            _normalization_operation(policy, iterations),
            warmups=warmups,
            repeats=repeats,
        ),
        "decision_slot_claim": _measure(
            _slot_claim_operation(experiment),
            warmups=warmups,
            repeats=repeats,
        ),
        "fake_order_reconciliation": _measure(
            _execution_operation(experiment, risk_request, iterations),
            warmups=warmups,
            repeats=repeats,
        ),
        "fifteen_minute_aggregation": _measure(
            _aggregation_operation(policy, iterations),
            warmups=warmups,
            repeats=repeats,
        ),
        "one_minute_ingestion_persistence": _measure(
            _persistence_operation(experiment, bars),
            warmups=warmups,
            repeats=repeats,
        ),
        "risk_decision": _measure(
            _risk_operation(experiment, risk_request, iterations),
            warmups=warmups,
            repeats=repeats,
        ),
    }
    payload = {
        "environment": {
            "implementation": platform.python_implementation(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "input": {
            "fixture": "deterministic_synthetic",
            "iterations": iterations,
            "repeats": repeats,
            "warmups": warmups,
        },
        "measurements": measurements,
        "notes": [
            "Wall-clock results are observational and are not CI pass/fail thresholds.",
            "No native optimization is justified until repeatable profiling identifies a bottleneck.",
        ],
        "schema": "offline-pipeline-benchmark-v1",
    }
    json.dump(payload, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
