"""Deterministic, credential-free vertical slice for platform verification.

The demo deliberately uses a local temporary SQLite database and the deterministic fake paper
broker.  It never constructs a provider transport, reads a secret, or grants Alpaca submission
authority.  Every timestamp and input value that participates in identity is fixed so two fresh
runs produce the same logical evidence hash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import Engine, create_engine, event, select

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import (
    BrokerAdapter,
    ExecutionMode,
    ExperimentDefinition,
    load_experiment,
)
from adaptive_trader.platform.control import ReadResource, SQLAlchemyControlQueryService
from adaptive_trader.platform.data import (
    CanonicalBar,
    EffectiveBar,
    FifteenMinuteMaterializer,
    NormalizationPolicy,
    SessionWindow,
    normalize_fixture_bar,
)
from adaptive_trader.platform.data.calendar import TradingInterval, XnasExchangeCalendar
from adaptive_trader.platform.data.watermarks import (
    DataSeries,
    GapDetection,
    GapRepairCoverage,
    GapRepository,
    GapStatus,
    WatermarkRepository,
)
from adaptive_trader.platform.execution import (
    AccountState,
    DeterministicFakePaperBroker,
    ExecutionPlanningRequest,
    ExecutionPlanningResult,
    ExecutionService,
    FakeBrokerScenario,
    ForcedFlattenRequest,
    ForcedFlattenService,
    OrderIntent,
    Position,
    ReconciliationReceipt,
    ReconciliationRequest,
    SubmissionSafetySnapshot,
    plan_signed_orders,
    reconcile_and_persist,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    ANNUALIZATION_FACTOR,
    DEFAULT_EIGENVALUE_FLOOR,
    RETURNS_PER_SYMBOL,
    AccountSnapshot,
    ExposureSnapshot,
    MarketIntegritySnapshot,
    OpenOrderSnapshot,
    PlanningPrice,
    ReconciliationSnapshot,
    RiskDecision,
    RiskEvaluationRequest,
    RiskExecutionScope,
    RiskLatchState,
    RiskStatistics,
    SecurityMetadataSnapshot,
    SignedPosition,
    evaluate_signed_risk,
    policy_hash,
)
from adaptive_trader.platform.scheduling import (
    DecisionSlot,
    DecisionSlotRepository,
    DecisionType,
    build_session_schedule,
)
from adaptive_trader.platform.signals import (
    DecisionContext,
    FixtureSignalScenario,
    OfflineFixtureSignalProvider,
    SignalEnvelope,
    SignalEnvelopeRepository,
)
from adaptive_trader.platform.storage.execution import SignedExecutionRepository
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteStatus,
    MarketDataRepository,
    StoredBarEvent,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.risk import SignedRiskRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_bar_identities,
    metadata,
)

EVIDENCE_LABEL = "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE"
_RECOVERY_POINTS = (
    "before_event_persistence",
    "after_event_persistence_before_watermark",
    "correction_during_aggregation",
    "after_slot_claim_before_signal_persistence",
    "after_signal_persistence_before_risk_decision",
    "after_intent_persistence_before_submission",
    "fake_broker_acceptance_before_response_persistence",
    "before_reconciliation",
    "before_forced_flatten_completion",
)
DEMO_SESSION_DATE = date(2026, 7, 6)
DEMO_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
DEMO_END = DEMO_START + timedelta(minutes=15)
DEMO_RECEIVED_AT = DEMO_END + timedelta(seconds=30)
_CONFIG_PATH = Path("experiments/semiconductor_network_intraday_v1.yaml")
_CRASH_EXIT_CODE = 86
_CRASH_PROGRAM = f"import os; os._exit({_CRASH_EXIT_CODE})"


class DemoError(RuntimeError):
    """Raised when the offline demonstration cannot prove its required invariants."""


class _DemoForcedFlatPlanProbe:
    """Expose the in-memory demo's persisted forced plan to the durable slot repository."""

    def __init__(self, planned: list[ExecutionPlanningResult]) -> None:
        self._planned = planned

    def exists(self, connection: object, *, slot_id: str) -> bool:
        del connection
        return any(result.risk_decision.slot_id == slot_id for result in self._planned)


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    """One exercised restart boundary and its safe deterministic outcome."""

    failure_point: str
    outcome: str
    injected_failure_count: int
    duplicate_side_effects: int
    incident_count: int
    restart_state_hash: str
    restart_mechanism: str

    def as_dict(self) -> dict[str, object]:
        return {
            "duplicate_side_effects": self.duplicate_side_effects,
            "failure_point": self.failure_point,
            "incident_count": self.incident_count,
            "injected_failure_count": self.injected_failure_count,
            "outcome": self.outcome,
            "restart_mechanism": self.restart_mechanism,
            "restart_state_hash": self.restart_state_hash,
        }


class _RecoveryTracker:
    """Require each named crash to recover from byte-equivalent reopened state."""

    def __init__(self) -> None:
        self._injected: dict[str, str] = {}
        self._recovered: list[RecoveryEvidence] = []

    def inject(self, failure_point: str, *, durable_state: object) -> None:
        if failure_point not in _RECOVERY_POINTS or failure_point in self._injected:
            raise DemoError("demo failure injection sequence is invalid")
        state_hash = sha256_hex(durable_state)
        try:
            crashed = subprocess.run(
                (sys.executable, "-I", "-c", _CRASH_PROGRAM),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env={"PYTHONHASHSEED": "0"},
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise DemoError("demo crash process could not be exercised") from None
        if crashed.returncode != _CRASH_EXIT_CODE or crashed.stdout != b"" or crashed.stderr != b"":
            raise DemoError("demo crash process did not terminate at the requested boundary")
        self._injected[failure_point] = state_hash

    def recover(
        self,
        failure_point: str,
        *,
        reopened_state: object,
        side_effect_ids: tuple[str, ...],
        incident_ids: tuple[str, ...],
    ) -> None:
        if failure_point not in self._injected or any(
            item.failure_point == failure_point for item in self._recovered
        ):
            raise DemoError("demo recovery was recorded without one injected failure")
        if any(type(value) is not str or not value for value in (*side_effect_ids, *incident_ids)):
            raise DemoError("demo recovery identifiers are invalid")
        restart_state_hash = sha256_hex(reopened_state)
        if restart_state_hash != self._injected[failure_point]:
            raise DemoError("demo restart did not reopen byte-equivalent durable state")
        duplicate_side_effects = len(side_effect_ids) - len(set(side_effect_ids))
        if duplicate_side_effects:
            raise DemoError("demo recovery duplicated an externally visible side effect")
        incident_count = len(set(incident_ids))
        self._recovered.append(
            RecoveryEvidence(
                failure_point=failure_point,
                outcome=(
                    "RECOVERED_NO_DUPLICATES"
                    if duplicate_side_effects == 0 and incident_count == 0
                    else "FAIL_CLOSED_INCIDENT"
                ),
                injected_failure_count=1,
                duplicate_side_effects=duplicate_side_effects,
                incident_count=incident_count,
                restart_state_hash=restart_state_hash,
                restart_mechanism="SUBPROCESS_CRASH_DURABLE_REOPEN",
            )
        )

    def evidence(self) -> tuple[RecoveryEvidence, ...]:
        by_point = {item.failure_point: item for item in self._recovered}
        if set(by_point) != set(_RECOVERY_POINTS) or len(by_point) != len(self._recovered):
            raise DemoError("demo did not recover every required failure point")
        return tuple(by_point[failure_point] for failure_point in _RECOVERY_POINTS)


def _interrupt_once(
    tracker: _RecoveryTracker,
    failure_point: str,
    *,
    durable_state: object,
) -> None:
    tracker.inject(failure_point, durable_state=durable_state)


@dataclass(frozen=True, slots=True)
class DemoEvidence:
    """Logical evidence from one fresh demo state directory."""

    canonical_event_hashes: tuple[str, ...]
    effective_event_hashes: tuple[str, ...]
    aggregate_hashes: tuple[str, ...]
    aggregate_revision_hashes: tuple[str, ...]
    gap_hashes: tuple[str, ...]
    watermark_values: tuple[tuple[str, str, str], ...]
    slot_ids: tuple[str, ...]
    signal_hashes: tuple[str, ...]
    risk_decision_hashes: tuple[str, ...]
    execution_plan_hashes: tuple[str, ...]
    client_order_ids: tuple[str, ...]
    fake_broker_event_sequence: tuple[tuple[str, int, str, str | None], ...]
    fill_hashes: tuple[str, ...]
    reconciliation_hashes: tuple[str, ...]
    final_signed_positions: tuple[tuple[str, str], ...]
    final_account_values: tuple[tuple[str, str], ...]
    audit_chain_root: str
    safe_read_model_hash: str
    recovery_evidence: tuple[RecoveryEvidence, ...]

    def logical_payload(self) -> dict[str, object]:
        """Return only portable values that participate in deterministic identity."""

        return {
            "aggregate_hashes": self.aggregate_hashes,
            "aggregate_revision_hashes": self.aggregate_revision_hashes,
            "audit_chain_root": self.audit_chain_root,
            "canonical_event_hashes": self.canonical_event_hashes,
            "client_order_ids": self.client_order_ids,
            "effective_event_hashes": self.effective_event_hashes,
            "evidence_label": EVIDENCE_LABEL,
            "execution_plan_hashes": self.execution_plan_hashes,
            "fake_broker_event_sequence": self.fake_broker_event_sequence,
            "fill_hashes": self.fill_hashes,
            "final_account_values": self.final_account_values,
            "final_signed_positions": self.final_signed_positions,
            "gap_hashes": self.gap_hashes,
            "reconciliation_hashes": self.reconciliation_hashes,
            "recovery_evidence": tuple(item.as_dict() for item in self.recovery_evidence),
            "risk_decision_hashes": self.risk_decision_hashes,
            "safe_read_model_hash": self.safe_read_model_hash,
            "schema": "offline-demo-evidence-v1",
            "signal_hashes": self.signal_hashes,
            "slot_ids": self.slot_ids,
            "watermark_values": self.watermark_values,
        }

    @property
    def manifest_hash(self) -> str:
        return sha256_hex(self.logical_payload())

    def manifest(self) -> dict[str, object]:
        return {**self.logical_payload(), "evidence_manifest_hash": self.manifest_hash}


@dataclass(frozen=True, slots=True)
class DemoComparison:
    """Proof that two fresh-state executions have identical logical evidence."""

    first: DemoEvidence
    second: DemoEvidence

    def __post_init__(self) -> None:
        if self.first.manifest_hash != self.second.manifest_hash:
            raise DemoError("fresh demo runs produced different logical evidence")

    @property
    def manifest_hash(self) -> str:
        return self.first.manifest_hash

    def report(self) -> dict[str, object]:
        return {
            "deterministic": True,
            "evidence_label": EVIDENCE_LABEL,
            "evidence_manifest_hash": self.manifest_hash,
            "first_run_hash": self.first.manifest_hash,
            "second_run_hash": self.second.manifest_hash,
            "status": "ok",
        }


def run_demo_twice(*, config_root: Path = Path("configs")) -> DemoComparison:
    """Run two isolated demos and require byte-equivalent logical evidence."""

    with TemporaryDirectory(prefix="aqa-offline-demo-first-") as first_directory:
        first = run_demo_once(Path(first_directory), config_root=config_root)
    with TemporaryDirectory(prefix="aqa-offline-demo-second-") as second_directory:
        second = run_demo_once(Path(second_directory), config_root=config_root)
    return DemoComparison(first=first, second=second)


def publish_demo_evidence(
    evidence: DemoEvidence,
    destination: Path,
    *,
    application_root: Path,
) -> Path:
    """Publish one immutable manifest beneath an explicit trusted application root."""

    if type(evidence) is not DemoEvidence or not isinstance(destination, Path):
        raise DemoError("demo evidence publication inputs are invalid")
    if destination.is_absolute() or not destination.parts or ".." in destination.parts:
        raise DemoError("demo evidence path must remain relative to the application root")
    root = application_root.resolve(strict=True)
    parent = root.joinpath(*destination.parts[:-1])
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(root)
    except ValueError:
        raise DemoError("demo evidence path escapes the application root") from None
    target = resolved_parent / destination.name
    payload = canonical_json_bytes(evidence.manifest()) + b"\n"
    if target.is_symlink():
        raise DemoError("demo evidence destination cannot be a symbolic link")
    if target.exists():
        if not target.is_file() or target.read_bytes() != payload:
            raise DemoError("demo evidence destination already contains different content")
        return target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise DemoError("demo evidence could not be written completely")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        raise DemoError("demo evidence destination was created concurrently") from None
    return target


def run_demo_once(state_directory: Path, *, config_root: Path = Path("configs")) -> DemoEvidence:
    """Execute one isolated, deterministic, no-network platform slice."""

    state_root = _require_fresh_directory(state_directory)
    experiment = load_experiment(_CONFIG_PATH, config_root=config_root.resolve(strict=True))
    database_path = state_root / "platform.sqlite3"
    engine = _sqlite_engine(database_path)
    recovery = _RecoveryTracker()
    try:
        metadata.create_all(engine)
        _register_experiment(engine, experiment)
        calendar = XnasExchangeCalendar()
        policy = NormalizationPolicy.for_offline_fixture(experiment)
        engine, latest, _initial_hashes, duplicate_count, gap_hash = _persist_fixture_window(
            experiment=experiment,
            policy=policy,
            engine=engine,
            calendar=calendar,
            recovery=recovery,
        )
        persisted_state = _combined_restart_state(engine)
        _interrupt_once(
            recovery,
            "after_event_persistence_before_watermark",
            durable_state=persisted_state,
        )
        engine = _reopen_sqlite_engine(engine)
        reopened_persisted_state = _combined_restart_state(engine)
        market_data = MarketDataRepository(engine)
        engine, aggregates, aggregate_revisions = _materialize_aggregates(
            experiment=experiment,
            policy=policy,
            market_data=market_data,
            engine=engine,
            latest=latest,
            recovery=recovery,
        )
        canonical_hashes = _one_minute_event_hashes(engine)
        watermark_values, basket_through = _watermarks(
            engine=engine,
            experiment=experiment,
            calendar=calendar,
        )
        recovery.recover(
            "after_event_persistence_before_watermark",
            reopened_state=reopened_persisted_state,
            side_effect_ids=_table_effect_ids(
                engine,
                "aqa_symbol_watermarks",
                "aqa_basket_watermarks",
            ),
            incident_ids=_incident_ids(engine),
        )
        (
            engine,
            slot_ids,
            signal_hash,
            risk_hash,
            risk_decision,
        ) = _decision_pipeline(
            engine=engine,
            experiment=experiment,
            basket_through=basket_through,
            aggregates=aggregates,
            recovery=recovery,
        )
        engine, execution = _execution_pipeline(
            engine=engine,
            experiment=experiment,
            risk_decision=risk_decision,
            marks=_planning_marks(aggregates),
            recovery=recovery,
        )
        platform_audit = AuditRepository(engine).verify()
        platform_heads = tuple(
            (head.stream_id, head.sequence, head.event_hash)
            for head in platform_audit.stream_heads
            if not head.stream_id.startswith("aqa_execution:")
        )
        execution_heads = tuple(
            (head.stream_id, head.sequence, head.event_hash)
            for head in platform_audit.stream_heads
            if head.stream_id.startswith("aqa_execution:")
        )
        audit_root = sha256_hex(
            {
                "execution": execution_heads,
                "platform": platform_heads,
                "schema": "offline-demo-combined-audit-root-v1",
            }
        )
        effective_hashes = tuple(sorted(event.content_hash for _, event in latest.values()))
        safe_read_model_hash = _control_read_model_hash(engine)
        if duplicate_count != 1 or not execution.ambiguous_recovered or execution.final_positions:
            raise DemoError("demo recovery evidence is incomplete")
        return DemoEvidence(
            canonical_event_hashes=tuple(sorted(canonical_hashes)),
            effective_event_hashes=effective_hashes,
            aggregate_hashes=tuple(
                sorted(item.aggregate.result_hash for item in aggregates.values())
            ),
            aggregate_revision_hashes=tuple(aggregate_revisions),
            gap_hashes=(gap_hash,),
            watermark_values=watermark_values,
            slot_ids=slot_ids,
            signal_hashes=(signal_hash,),
            risk_decision_hashes=(risk_hash,),
            execution_plan_hashes=execution.plan_hashes,
            client_order_ids=execution.client_order_ids,
            fake_broker_event_sequence=execution.broker_events,
            fill_hashes=execution.fill_hashes,
            reconciliation_hashes=execution.reconciliation_hashes,
            final_signed_positions=execution.final_positions,
            final_account_values=execution.final_account_values,
            audit_chain_root=audit_root,
            safe_read_model_hash=safe_read_model_hash,
            recovery_evidence=recovery.evidence(),
        )
    finally:
        engine.dispose()


@dataclass(frozen=True, slots=True)
class _ExecutionEvidence:
    plan_hashes: tuple[str, ...]
    client_order_ids: tuple[str, ...]
    broker_events: tuple[tuple[str, int, str, str | None], ...]
    fill_hashes: tuple[str, ...]
    reconciliation_hashes: tuple[str, ...]
    final_positions: tuple[tuple[str, str], ...]
    final_account_values: tuple[tuple[str, str], ...]
    ambiguous_recovered: bool


def _require_fresh_directory(path: Path) -> Path:
    if not isinstance(path, Path):
        raise DemoError("demo state directory must be a Path")
    if path.is_symlink():
        raise DemoError("demo state directory cannot be a symbolic link")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if any(path.iterdir()):
        raise DemoError("demo state directory must be empty")
    return path.resolve(strict=True)


def _sqlite_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
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

    return engine


def _reopen_sqlite_engine(engine: Engine) -> Engine:
    """Dispose every pooled connection and construct a new runtime over the same file."""

    database = engine.url.database
    if type(database) is not str or not database or database == ":memory:":
        raise DemoError("demo restart requires a durable SQLite file")
    database_path = Path(database)
    if not database_path.is_absolute() or not database_path.is_file():
        raise DemoError("demo restart database is unavailable")
    engine.dispose()
    reopened = _sqlite_engine(database_path)
    with reopened.connect() as connection:
        if connection.exec_driver_sql("SELECT 1").scalar_one() != 1:
            raise DemoError("demo restart database is not readable")
    return reopened


def _snapshot_value(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if type(value) is Decimal:
        return format(value, "f")
    if type(value) is datetime:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if type(value) is date:
        return value.isoformat()
    raise DemoError("demo durable-state snapshot encountered an unsafe value")


def _durable_database_state(engine: Engine) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """Read only identities, state versions, and hashes from every durable relation."""

    snapshots: list[tuple[str, tuple[object, ...]]] = []
    proof_names = ("content_hash", "event_hash", "version", "state", "status")
    with engine.connect() as connection:
        for table in sorted(metadata.tables.values(), key=lambda item: item.name):
            primary_key = tuple(table.primary_key.columns)
            proof_columns = tuple(
                table.c[name]
                for name in proof_names
                if name in table.c and name not in {column.name for column in primary_key}
            )
            columns = (*primary_key, *proof_columns)
            if not columns:
                continue
            statement = select(*columns).order_by(*primary_key)
            rows = tuple(
                tuple(_snapshot_value(row[index]) for index in range(len(columns)))
                for row in connection.execute(statement)
            )
            snapshots.append((str(table.name), rows))
    return tuple(snapshots)


def _broker_state(broker: DeterministicFakePaperBroker) -> object:
    try:
        decoded = json.loads(broker.snapshot())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DemoError("fake broker restart state is malformed") from None
    if type(decoded) is not dict:
        raise DemoError("fake broker restart state is malformed")
    return decoded


def _combined_restart_state(
    engine: Engine,
    broker: DeterministicFakePaperBroker | None = None,
) -> dict[str, object]:
    state: dict[str, object] = {"database": _durable_database_state(engine)}
    if broker is not None:
        state["fake_broker"] = _broker_state(broker)
    return state


def _table_effect_ids(engine: Engine, *table_names: str) -> tuple[str, ...]:
    effects: list[str] = []
    by_name = {table.name: table for table in metadata.tables.values()}
    with engine.connect() as connection:
        for table_name in table_names:
            table = by_name.get(table_name)
            if table is None:
                raise DemoError("demo side-effect relation is unavailable")
            primary_key = tuple(table.primary_key.columns)
            if not primary_key:
                raise DemoError("demo side-effect relation has no durable identity")
            rows = connection.execute(select(*primary_key).order_by(*primary_key))
            for row in rows:
                identity = sha256_hex((table_name, tuple(_snapshot_value(value) for value in row)))
                effects.append(f"{table_name}:{identity}")
    return tuple(effects)


def _broker_effect_ids(broker: DeterministicFakePaperBroker) -> tuple[str, ...]:
    decoded = _broker_state(broker)
    if not isinstance(decoded, dict):
        raise DemoError("fake broker restart state is malformed")
    payload = decoded.get("payload")
    if not isinstance(payload, dict) or type(payload.get("orders")) is not list:
        raise DemoError("fake broker restart state has no order ledger")
    identifiers: list[str] = []
    for order in payload["orders"]:
        if type(order) is not dict:
            raise DemoError("fake broker restart order is malformed")
        broker_order_id = order.get("broker_order_id")
        if type(broker_order_id) is not str or not broker_order_id:
            raise DemoError("fake broker restart order has no side-effect identity")
        identifiers.append(f"fake_broker:{broker_order_id}")
    return tuple(identifiers)


def _incident_ids(engine: Engine) -> tuple[str, ...]:
    return _table_effect_ids(engine, "aqa_incidents")


_DEMO_CONTROL_VIEW_DDL = (
    """
    CREATE VIEW aqa_experiment_context_v AS
    SELECT experiment.experiment_hash, experiment.experiment_id,
           experiment.experiment_version, experiment.schema_version,
           experiment.configuration,
           experiment.content_hash AS experiment_content_hash,
           experiment.registered_at, symbol.experiment_symbol_id, symbol.symbol,
           symbol.role, symbol.ordinal, symbol.content_hash AS symbol_content_hash
    FROM aqa_experiments AS experiment
    JOIN aqa_experiment_symbols AS symbol
      ON symbol.experiment_hash = experiment.experiment_hash
    """,
    """
    CREATE VIEW aqa_basket_watermarks_v AS
    SELECT basket_watermark_id, experiment_hash, role, timeframe, status,
           contiguous_through, component_hash, version, updated_at
    FROM aqa_basket_watermarks
    """,
    """
    CREATE VIEW aqa_data_gaps_v AS
    SELECT gap_id, experiment_hash, provider, feed, adjustment, symbol, timeframe,
           gap_start_at, gap_end_at, status, reason_code, attempt_count,
           detected_at, last_attempt_at, resolved_at, version
    FROM aqa_data_gaps
    """,
    """
    CREATE VIEW aqa_datasets_v AS
    SELECT dataset_id, artifact_id, experiment_hash, provider, feed, adjustment,
           timeframe, range_start_at, range_end_at, roles, symbols, row_counts,
           gap_summary, correction_summary, schema_version, logical_hash,
           physical_hash, manifest_hash, source_git_commit, dirty_worktree,
           uv_lock_hash, promotable, status, created_at
    FROM aqa_dataset_manifests
    """,
    """
    CREATE VIEW aqa_decision_slots_v AS
    SELECT slot_id, experiment_hash, experiment_id, experiment_version,
           signal_provider_id, signal_provider_version, session_date,
           source_interval_start, source_interval_end, decision_type, ready_at,
           deadline_at, required_completion_at, state, claim_owner, claimed_at,
           lease_expires_at, attempt_count, reason_code, completed_at,
           correlation_id, version, created_at, updated_at
    FROM aqa_decision_slots
    """,
    """
    CREATE VIEW aqa_signals_v AS
    SELECT signal_id, slot_id, contract_version, correlation_id, provider_id,
           provider_version, provider_source_mode, experiment_id,
           experiment_version, experiment_hash, data_contract_hash, policy_hash,
           source_bar_end, created_at, expires_at, active_symbols,
           availability_mask, actions, expected_edge_bps,
           proposed_signed_target_inputs, artifact_id, artifact_hash, promotable,
           paper_submission_eligible, content_hash
    FROM aqa_signal_envelopes
    """,
    """
    CREATE VIEW aqa_risk_decisions_v AS
    SELECT risk_decision_id, slot_id, signal_id, experiment_hash, policy_id,
           policy_version, decided_at, approved_targets, controls, reason_codes,
           gross_exposure, net_exposure, cash_weight, content_hash
    FROM aqa_risk_decisions
    """,
    """
    CREATE VIEW aqa_risk_latches_v AS
    SELECT latch_event_id, experiment_hash, latch_type, sequence, action,
           reason_code, actor, occurred_at, content_hash
    FROM aqa_risk_latch_events
    """,
    """
    CREATE VIEW aqa_orders_v AS
    SELECT intent.order_intent_id, intent.execution_plan_id,
           intent.risk_decision_id, intent.experiment_hash, intent.correlation_id,
           intent.client_order_id, intent.symbol, intent.side, intent.effect,
           intent.phase, intent.sequence, intent.target_version, intent.quantity,
           intent.notional, intent.reference_price, intent.final_target_quantity,
           intent.forced_flat, intent.created_at, intent.deadline_at,
           broker.broker_order_id, broker.state, broker.submitted_at,
           broker.accepted_at, broker.updated_at,
           broker.cumulative_filled_quantity, broker.average_fill_price,
           broker.last_event_sequence, broker.safe_error_code, broker.version
    FROM aqa_order_intents AS intent
    LEFT JOIN aqa_broker_orders AS broker
      ON broker.client_order_id = intent.client_order_id
    """,
    """
    CREATE VIEW aqa_fills_v AS
    SELECT fill_id, client_order_id, broker_execution_id, symbol, side, quantity,
           price, fee, occurred_at, content_hash
    FROM aqa_fills
    """,
    """
    CREATE VIEW aqa_reconciliations_v AS
    SELECT reconciliation_id, experiment_hash, slot_id, execution_plan_id,
           correlation_id, account_id_hash, started_at, completed_at, status,
           expected_positions, observed_positions, expected_cash, observed_cash,
           expected_equity, observed_equity, fill_hashes, order_hashes,
           discrepancies, content_hash
    FROM aqa_reconciliations
    """,
    """
    CREATE VIEW aqa_incidents_v AS
    SELECT incident_id, idempotency_key, experiment_hash, incident_type, severity,
           status, reason_code, opened_at, resolved_at, version
    FROM aqa_incidents
    """,
    """
    CREATE VIEW aqa_audit_status_v AS
    SELECT current.stream_id, current.sequence, current.event_hash,
           current.event_type, current.actor, current.occurred_at
    FROM aqa_audit_events AS current
    WHERE current.sequence = (
        SELECT MAX(candidate.sequence)
        FROM aqa_audit_events AS candidate
        WHERE candidate.stream_id = current.stream_id
    )
    """,
)


def _install_demo_control_views(engine: Engine) -> None:
    """Install SQLite equivalents of the production least-privilege projections."""

    if engine.dialect.name != "sqlite":
        raise DemoError("offline demo control views require SQLite")
    with engine.begin() as connection:
        for statement in _DEMO_CONTROL_VIEW_DDL:
            connection.exec_driver_sql(statement)


def _control_read_model_hash(engine: Engine) -> str:
    """Hash bounded responses returned by the real read-only query service."""

    _install_demo_control_views(engine)
    queries = SQLAlchemyControlQueryService(engine)
    if queries.ready() is not True:
        raise DemoError("demo control query service is not ready")
    responses: list[tuple[str, tuple[dict[str, object], ...]]] = []
    for resource in ReadResource:
        pages: list[dict[str, object]] = []
        for page_number in range(16):
            response = queries.page(resource, limit=100, offset=page_number * 100)
            pages.append(response.model_dump(mode="json"))
            if response.count < 100:
                break
        else:
            raise DemoError("demo control response exceeded its bounded page budget")
        responses.append((resource.value, tuple(pages)))
    read_model: dict[str, object] = {
        "resources": tuple(responses),
        "schema": "offline-demo-control-responses-v1",
    }
    payload = canonical_json_bytes(read_model)
    if any(token in payload.lower() for token in (b"secret", b"api_key", b"token")):
        raise DemoError("safe demo control response contains a secret-shaped field")
    return sha256_hex(read_model)


def _register_experiment(engine: Engine, experiment: ExperimentDefinition) -> None:
    registered = ExperimentRepository(engine).register(
        experiment,
        registered_at=DEMO_START - timedelta(minutes=1),
    )
    if registered != experiment:
        raise DemoError("demo experiment registration changed immutable configuration")


def _fixture_payload(*, symbol: str, minute: int, correction: bool = False) -> dict[str, object]:
    symbol_offset = Decimal(sum(ord(character) for character in symbol) % 40)
    base = Decimal(80) + symbol_offset + (Decimal(minute) / Decimal(10))
    close = base + (Decimal("0.50") if correction else Decimal("0.25"))
    return {
        "S": symbol,
        "t": (DEMO_START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "o": base,
        "h": base + Decimal(1),
        "l": base - Decimal(1),
        "c": close,
        "v": 1_000 + minute,
        "n": 20 + minute,
        "vw": base + Decimal("0.10"),
    }


def _canonical_bar(
    *,
    policy: NormalizationPolicy,
    symbol: str,
    minute: int,
    correction: bool = False,
) -> CanonicalBar:
    suffix = "correction" if correction else "original"
    return normalize_fixture_bar(
        _fixture_payload(symbol=symbol, minute=minute, correction=correction),
        policy=policy,
        receipt_timestamp_utc=(
            DEMO_RECEIVED_AT + timedelta(seconds=1) if correction else DEMO_RECEIVED_AT
        ),
        execution_reference=True,
        quality_flags=("complete",),
        source_event_id=f"fixture:{symbol}:{minute:03d}:{suffix}",
    )


def _bar_write(bar: CanonicalBar) -> BarWrite:
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
        schema_version=bar.schema_version,
    )


def _persist_fixture_window(
    *,
    experiment: ExperimentDefinition,
    policy: NormalizationPolicy,
    engine: Engine,
    calendar: XnasExchangeCalendar,
    recovery: _RecoveryTracker,
) -> tuple[
    Engine,
    dict[tuple[str, int], tuple[CanonicalBar, StoredBarEvent]],
    list[str],
    int,
    str,
]:
    market_data = MarketDataRepository(engine)
    latest: dict[tuple[str, int], tuple[CanonicalBar, StoredBarEvent]] = {}
    canonical_hashes: list[str] = []
    reopened_state: object | None = None
    correction_symbol = experiment.active_tradable[1]
    duplicate_symbol = experiment.active_tradable[-2]
    missing = (correction_symbol, 5)
    for symbol in experiment.active_tradable:
        for minute in range(15):
            if (symbol, minute) == missing:
                continue
            bar = _canonical_bar(policy=policy, symbol=symbol, minute=minute)
            if not canonical_hashes:
                durable_state = _combined_restart_state(engine)
                _interrupt_once(
                    recovery,
                    "before_event_persistence",
                    durable_state=durable_state,
                )
                engine = _reopen_sqlite_engine(engine)
                market_data = MarketDataRepository(engine)
                reopened_state = _combined_restart_state(engine)
            result = market_data.append(_bar_write(bar))
            if result.status is not BarWriteStatus.INSERTED:
                raise DemoError("first fixture persistence was not an insertion")
            latest[(symbol, minute)] = (bar, result.event)
            canonical_hashes.append(result.event.content_hash)

    duplicate_bar, duplicate_event = latest[(duplicate_symbol, 0)]
    duplicate = market_data.append(_bar_write(duplicate_bar))
    if duplicate.status is not BarWriteStatus.DUPLICATE or duplicate.event != duplicate_event:
        raise DemoError("fixture duplicate did not converge on its original event")
    if reopened_state is None:
        raise DemoError("event-persistence restart was not exercised")
    recovery.recover(
        "before_event_persistence",
        reopened_state=reopened_state,
        side_effect_ids=_table_effect_ids(engine, "aqa_bar_events"),
        incident_ids=_incident_ids(engine),
    )

    series = DataSeries("fixture", "iex", "raw", missing[0], "1Min")
    gap_repository = GapRepository(engine, calendar=calendar)
    detection = GapDetection(
        experiment_hash=experiment.content_hash,
        series=series,
        start_at=DEMO_START + timedelta(minutes=missing[1]),
        end_at=DEMO_START + timedelta(minutes=missing[1] + 1),
        reason_code="fixture_missing_interval",
        detected_at=DEMO_RECEIVED_AT,
    )
    gap = gap_repository.record(detection)
    gap_repository.begin_repair(
        gap.gap_id,
        attempted_at=DEMO_RECEIVED_AT + timedelta(seconds=1),
    )
    repaired_bar = _canonical_bar(policy=policy, symbol=missing[0], minute=missing[1])
    repaired = market_data.append(_bar_write(repaired_bar))
    latest[missing] = (repaired_bar, repaired.event)
    canonical_hashes.append(repaired.event.content_hash)
    resolved = gap_repository.complete_repair(
        gap.gap_id,
        coverage=GapRepairCoverage(
            series=series,
            start_at=detection.start_at,
            end_at=detection.end_at,
            observed_intervals=(TradingInterval(detection.start_at, detection.end_at),),
            completed_at=DEMO_RECEIVED_AT + timedelta(seconds=2),
        ),
    )
    if resolved.status is not GapStatus.RESOLVED:
        raise DemoError("fixture gap did not resolve after exact repair coverage")
    return engine, latest, canonical_hashes, 1, resolved.content_hash


def _constituents(
    latest: dict[tuple[str, int], tuple[CanonicalBar, StoredBarEvent]],
    symbol: str,
) -> tuple[EffectiveBar, ...]:
    return tuple(
        EffectiveBar(
            bar_event_id=latest[(symbol, minute)][1].bar_event_id,
            revision=latest[(symbol, minute)][1].revision,
            bar=latest[(symbol, minute)][0],
        )
        for minute in range(15)
    )


def _materialize_aggregates(
    *,
    experiment: ExperimentDefinition,
    policy: NormalizationPolicy,
    market_data: MarketDataRepository,
    engine: Engine,
    latest: dict[tuple[str, int], tuple[CanonicalBar, StoredBarEvent]],
    recovery: _RecoveryTracker,
) -> tuple[Engine, dict[str, Any], tuple[str, ...]]:
    materializer = FifteenMinuteMaterializer.from_repository(market_data)
    session = SessionWindow(
        session_open_utc=DEMO_START,
        session_close_utc=datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
    )
    aggregates = {
        symbol: materializer.materialize(_constituents(latest, symbol), session=session)
        for symbol in experiment.active_tradable
    }
    correction_symbol = experiment.active_tradable[1]
    initial_aggregate_hash = aggregates[correction_symbol].aggregate.result_hash

    corrected_bar = _canonical_bar(
        policy=policy,
        symbol=correction_symbol,
        minute=7,
        correction=True,
    )
    corrected = market_data.append(_bar_write(corrected_bar))
    if corrected.status is not BarWriteStatus.CORRECTED or corrected.event.revision != 2:
        raise DemoError("fixture correction did not append a second revision")
    latest[(correction_symbol, 7)] = (corrected_bar, corrected.event)
    durable_state = _combined_restart_state(engine)
    _interrupt_once(
        recovery,
        "correction_during_aggregation",
        durable_state=durable_state,
    )
    engine = _reopen_sqlite_engine(engine)
    reopened_state = _combined_restart_state(engine)
    market_data = MarketDataRepository(engine)
    materializer = FifteenMinuteMaterializer.from_repository(market_data)
    corrected_aggregate = materializer.materialize(
        _constituents(latest, correction_symbol),
        session=session,
    )
    if corrected_aggregate.write_result.status is not BarWriteStatus.CORRECTED:
        raise DemoError("aggregate did not append a correction after constituent revision")
    replayed = materializer.materialize(
        _constituents(latest, correction_symbol),
        session=session,
    )
    if replayed.write_result.status is not BarWriteStatus.DUPLICATE:
        raise DemoError("aggregate recovery retry did not converge without a new revision")
    recovery.recover(
        "correction_during_aggregation",
        reopened_state=reopened_state,
        side_effect_ids=_table_effect_ids(engine, "aqa_bar_events"),
        incident_ids=_incident_ids(engine),
    )
    aggregates[correction_symbol] = corrected_aggregate
    return (
        engine,
        aggregates,
        (
            initial_aggregate_hash,
            corrected_aggregate.aggregate.result_hash,
        ),
    )


def _one_minute_event_hashes(engine: Engine) -> tuple[str, ...]:
    statement = (
        select(aqa_bar_events.c.content_hash)
        .select_from(
            aqa_bar_events.join(
                aqa_bar_identities,
                aqa_bar_events.c.bar_identity_id == aqa_bar_identities.c.bar_identity_id,
            )
        )
        .where(aqa_bar_identities.c.timeframe == "1Min")
        .order_by(aqa_bar_events.c.content_hash)
    )
    with engine.begin() as connection:
        return tuple(connection.scalars(statement))


def _watermarks(
    *,
    engine: Engine,
    experiment: ExperimentDefinition,
    calendar: XnasExchangeCalendar,
) -> tuple[tuple[tuple[str, str, str], ...], datetime]:
    repository = WatermarkRepository(engine, calendar=calendar)
    rows: list[tuple[str, str, str]] = []
    updated_at = DEMO_RECEIVED_AT + timedelta(seconds=3)
    for symbol in experiment.active_tradable:
        readiness, watermark = repository.recompute_symbol(
            experiment_hash=experiment.content_hash,
            series=DataSeries("fixture", "iex", "raw", symbol, "1Min"),
            start_at=DEMO_START,
            end_at=DEMO_END,
            updated_at=updated_at,
        )
        if watermark is None or not readiness.ready_through_range or not watermark.is_ready:
            raise DemoError("fixture symbol did not reach contiguous readiness")
        rows.append((symbol, watermark.contiguous_through.isoformat(), watermark.content_hash))
    basket = repository.recompute_active_basket(
        experiment_hash=experiment.content_hash,
        universe=experiment,
        provider="fixture",
        feed="iex",
        adjustment="raw",
        timeframe="1Min",
        updated_at=updated_at,
        required_through=DEMO_END,
    )
    basket_watermark = basket.watermark
    if (
        not basket.is_ready
        or basket_watermark is None
        or basket_watermark.contiguous_through != DEMO_END
    ):
        raise DemoError("active fixture basket did not reach the required watermark")
    rows.append(
        (
            "ACTIVE_BASKET",
            basket_watermark.contiguous_through.isoformat(),
            basket_watermark.content_hash,
        )
    )
    return tuple(rows), DEMO_END


def _decision_pipeline(
    *,
    engine: Engine,
    experiment: ExperimentDefinition,
    basket_through: datetime,
    aggregates: dict[str, Any],
    recovery: _RecoveryTracker,
) -> tuple[Engine, tuple[str, ...], str, str, Any]:
    calendar = XnasExchangeCalendar()
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="deterministic_fixture",
        signal_provider_version="1",
        session_date=DEMO_SESSION_DATE,
        calendar=calendar,
    )
    slots = DecisionSlotRepository(engine)
    slots.create_schedule(schedule, recorded_at=DEMO_START - timedelta(minutes=1))
    first = schedule.strategy_slots[0]
    ready = slots.evaluate_readiness(
        first.slot_id,
        active_basket_watermark=basket_through,
        now=first.ready_at + timedelta(seconds=1),
    )
    claimed = slots.claim(
        ready.slot_id,
        owner="offline_demo",
        now=first.ready_at + timedelta(seconds=2),
    ).slot
    durable_claim_state = _combined_restart_state(engine)
    _interrupt_once(
        recovery,
        "after_slot_claim_before_signal_persistence",
        durable_state=durable_claim_state,
    )
    engine = _reopen_sqlite_engine(engine)
    reopened_claim_state = _combined_restart_state(engine)
    slots = DecisionSlotRepository(engine)
    claimed = slots.renew_lease(
        claimed.slot_id,
        owner="offline_demo",
        now=first.ready_at + timedelta(seconds=3),
    )
    data_hash = sha256_hex(
        tuple(sorted(item.aggregate.result_hash for item in aggregates.values()))
    )
    selected_policy_hash = policy_hash(experiment.risk_policy, experiment.risk_groups)
    context = DecisionContext.from_experiment(
        slot=claimed,
        experiment=experiment,
        data_contract_hash=data_hash,
        policy_hash=selected_policy_hash,
        execution_mode=ExecutionMode.OFFLINE,
        broker_adapter=BrokerAdapter.FAKE,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )
    signal = OfflineFixtureSignalProvider(
        clock=lambda: first.ready_at + timedelta(seconds=3),
        scenario=FixtureSignalScenario(
            first_slot_long_symbol=experiment.active_tradable[-2],
            first_slot_short_symbol=experiment.active_tradable[1],
            expected_edge_bps=Decimal(25),
        ),
    ).signal_for(context)
    signal_repository = SignalEnvelopeRepository(engine)
    signal_repository.persist_once(signal, context=context)
    recovery.recover(
        "after_slot_claim_before_signal_persistence",
        reopened_state=reopened_claim_state,
        side_effect_ids=_table_effect_ids(engine, "aqa_signal_envelopes"),
        incident_ids=_incident_ids(engine),
    )
    durable_signal_state = _combined_restart_state(engine)
    _interrupt_once(
        recovery,
        "after_signal_persistence_before_risk_decision",
        durable_state=durable_signal_state,
    )
    engine = _reopen_sqlite_engine(engine)
    reopened_signal_state = _combined_restart_state(engine)
    signal_repository = SignalEnvelopeRepository(engine)
    if signal_repository.persist_once(signal, context=context) != signal:
        raise DemoError("signal recovery retry did not return the durable envelope")

    evaluated_at = first.ready_at + timedelta(seconds=4)
    risk = evaluate_signed_risk(
        request=RiskEvaluationRequest(
            signal=signal,
            decision_context=context,
            positions=tuple(
                SignedPosition(symbol=symbol, quantity=Decimal(0))
                for symbol in experiment.active_tradable
            ),
            open_orders=OpenOrderSnapshot.create(
                reserved_signed_notional={
                    symbol: Decimal(0) for symbol in experiment.active_tradable
                },
                conflicting_symbols=(),
                ambiguous_order_exists=False,
                observed_at=evaluated_at,
            ),
            account=AccountSnapshot(
                account_id_hash=sha256_hex(
                    ("broker-account-v1", DeterministicFakePaperBroker.INITIAL_ACCOUNT_ID)
                ),
                equity=Decimal("100000"),
                cash=Decimal("100000"),
                buying_power=Decimal("100000"),
                observed_at=evaluated_at,
            ),
            prices=tuple(
                PlanningPrice(
                    symbol=symbol,
                    price=aggregates[symbol].aggregate.bar.close,
                    observed_at=evaluated_at,
                    validated=True,
                )
                for symbol in experiment.active_tradable
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
                    observed_at=evaluated_at,
                )
                for symbol in experiment.active_tradable
            ),
            reconciliation=ReconciliationSnapshot.create(
                reconciled=True,
                ambiguous_order_exists=False,
                observed_at=evaluated_at,
            ),
            market_integrity=MarketIntegritySnapshot.create(
                active_basket_complete=True,
                unresolved_gap=False,
                correction_uncertainty=False,
                supported_session=True,
            ),
            session_start_equity=Decimal("100000"),
            deployment_high_water_equity=Decimal("100000"),
            statistics=_risk_statistics(experiment.active_tradable),
            latch_state=RiskLatchState.empty(experiment_hash=experiment.content_hash),
            operator_halt=False,
            evaluated_at=evaluated_at,
        ),
        experiment=experiment,
    )
    if risk.execution_scope is not RiskExecutionScope.FULL:
        raise DemoError("deterministic fixture did not pass signed-risk evaluation")
    if SignedRiskRepository(engine).persist(risk) != risk:
        raise DemoError("risk recovery did not persist the deterministic decision")
    recovery.recover(
        "after_signal_persistence_before_risk_decision",
        reopened_state=reopened_signal_state,
        side_effect_ids=_table_effect_ids(engine, "aqa_risk_decisions"),
        incident_ids=_incident_ids(engine),
    )
    slots.complete(
        claimed.slot_id,
        owner="offline_demo",
        now=first.ready_at + timedelta(seconds=5),
    )
    return (
        engine,
        tuple(slot.slot_id for slot in schedule.slots),
        signal.content_hash,
        risk.content_hash,
        risk,
    )


def _risk_statistics(symbols: tuple[str, ...]) -> RiskStatistics:
    covariance = tuple(
        tuple(Decimal("0.04") if left == right else Decimal(0) for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    correlation = tuple(
        tuple(Decimal(1) if left == right else Decimal(0) for right in range(len(symbols)))
        for left in range(len(symbols))
    )
    sigma = (Decimal("0.2"),) * len(symbols)
    input_hash = sha256_hex(("offline-demo-risk-input", symbols))
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


def _planning_marks(aggregates: dict[str, Any]) -> tuple[tuple[str, Decimal], ...]:
    return tuple((symbol, aggregates[symbol].aggregate.bar.close) for symbol in sorted(aggregates))


def _safety(
    decision: RiskDecision,
    *,
    evaluated_at: datetime,
    entry_disabled: bool = False,
) -> SubmissionSafetySnapshot:
    source_timestamps = dict(decision.source_timestamps)
    reconciliation_at = source_timestamps.get("reconciliation")
    if reconciliation_at is None:
        raise DemoError("risk decision lacks reconciliation evidence")
    return SubmissionSafetySnapshot(
        evaluated_at=evaluated_at,
        session_open=True,
        data_complete=True,
        account_observed_at=evaluated_at,
        security_observed_at=min(item.observed_at for item in decision.security_metadata),
        reconciliation_observed_at=reconciliation_at,
        price_observed_at=min(item.observed_at for item in decision.planning_prices),
        reconciliation_clean=True,
        ambiguous_order_exists=False,
        blocking_latch_exists=False,
        entry_disabled=entry_disabled,
        active_symbols=tuple(symbol for symbol, _ in decision.final_targets),
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


def _reconciliation_request(
    *,
    repository: SignedExecutionRepository,
    broker: DeterministicFakePaperBroker,
    planning_result: ExecutionPlanningResult,
    completed_at: datetime,
    require_flat: bool = False,
    required_flat_at: datetime | None = None,
) -> ReconciliationRequest:
    plan = planning_result.plan
    decision = planning_result.risk_decision
    plan_intents = repository.intents_for_plan(plan.execution_plan_id)
    plan_client_ids = {intent.client_order_id for intent in plan_intents}
    durable_orders = tuple(
        order
        for order in repository.all_orders()
        if repository.get_plan(
            repository.get_intent(order.client_order_id).execution_plan_id
        ).experiment_hash
        == plan.experiment_hash
    )
    intents = tuple(
        sorted(
            {
                intent.client_order_id: intent
                for intent in (
                    *plan_intents,
                    *(repository.get_intent(order.client_order_id) for order in durable_orders),
                )
            }.values(),
            key=lambda intent: intent.client_order_id,
        )
    )
    short_eligible_symbols = tuple(
        security.symbol
        for security in decision.security_metadata
        if security.asset_active
        and security.tradable
        and security.shortable
        and security.easy_to_borrow
        and security.primary_listing_eligible
        and security.broker_capability_known
    )
    account = broker.account(observed_at=completed_at)
    return ReconciliationRequest(
        experiment_hash=plan.experiment_hash,
        slot_id=decision.slot_id,
        execution_plan_id=plan.execution_plan_id,
        correlation_id=plan.correlation_id,
        active_symbols=tuple(symbol for symbol, _ in decision.final_targets),
        short_eligible_symbols=short_eligible_symbols,
        baseline_positions=plan.current_positions,
        baseline_cash=decision.account_snapshot.cash,
        fills=tuple(fill for fill in repository.fills() if fill.client_order_id in plan_client_ids),
        order_fills=tuple(
            fill
            for fill in repository.fills()
            if fill.client_order_id in {order.client_order_id for order in durable_orders}
        ),
        intents=intents,
        durable_orders=durable_orders,
        broker_orders=durable_orders,
        broker_positions=broker.positions(),
        broker_account=account,
        expected_account_id_hash=decision.account_snapshot.account_id_hash,
        mark_prices=plan.reference_prices,
        started_at=completed_at - timedelta(seconds=1),
        completed_at=completed_at,
        require_flat=require_flat,
        required_flat_at=required_flat_at,
    )


def _execution_pipeline(
    *,
    engine: Engine,
    experiment: ExperimentDefinition,
    risk_decision: RiskDecision,
    marks: tuple[tuple[str, Decimal], ...],
    recovery: _RecoveryTracker,
) -> tuple[Engine, _ExecutionEvidence]:
    created_at = DEMO_END + timedelta(seconds=95)
    opening = plan_signed_orders(
        ExecutionPlanningRequest(
            risk_decision=risk_decision,
            current_positions=tuple(
                Position(symbol=symbol, quantity=Decimal(0))
                for symbol in experiment.active_tradable
            ),
            reference_prices=marks,
            equity=Decimal("100000"),
            target_version=1,
            created_at=created_at,
            deadline_at=DEMO_END + timedelta(seconds=120),
            forced_flat=False,
        )
    )
    repository = SignedExecutionRepository(engine)
    broker = DeterministicFakePaperBroker(initial_time=created_at)
    broker.set_mark_prices(marks)
    service = ExecutionService(repository=repository, broker=broker)
    service.persist(opening)

    persisted_broker = broker.snapshot()
    durable_intent_state = _combined_restart_state(engine, broker)
    _interrupt_once(
        recovery,
        "after_intent_persistence_before_submission",
        durable_state=durable_intent_state,
    )
    engine = _reopen_sqlite_engine(engine)
    repository = SignedExecutionRepository(engine)
    broker = DeterministicFakePaperBroker.from_snapshot(persisted_broker)
    reopened_intent_state = _combined_restart_state(engine, broker)
    service = ExecutionService(repository=repository, broker=broker)
    if len(repository.intents_for_plan(opening.plan.execution_plan_id)) != len(opening.intents):
        raise DemoError("execution restart lost durable intents")
    if repository.order_events():
        raise DemoError("intent persistence caused a broker side effect")
    recovery.recover(
        "after_intent_persistence_before_submission",
        reopened_state=reopened_intent_state,
        side_effect_ids=_table_effect_ids(engine, "aqa_order_intents"),
        incident_ids=_incident_ids(engine),
    )
    ambiguous_client_id = opening.intents[0].client_order_id
    broker.set_scenario(ambiguous_client_id, FakeBrokerScenario.TIMEOUT_AFTER_ACCEPTANCE)
    unknown = service.submit_one(
        ambiguous_client_id,
        safety=_safety(risk_decision, evaluated_at=created_at + timedelta(seconds=1)),
    )
    if unknown.reason_codes != ("submission_unknown",):
        raise DemoError("fake acceptance ambiguity was not persisted")
    ambiguous_broker_state = broker.snapshot()
    durable_ambiguous_state = _combined_restart_state(engine, broker)
    _interrupt_once(
        recovery,
        "fake_broker_acceptance_before_response_persistence",
        durable_state=durable_ambiguous_state,
    )
    engine = _reopen_sqlite_engine(engine)
    repository = SignedExecutionRepository(engine)
    broker = DeterministicFakePaperBroker.from_snapshot(ambiguous_broker_state)
    reopened_ambiguous_state = _combined_restart_state(engine, broker)
    service = ExecutionService(repository=repository, broker=broker)
    recovered = service.resolve_ambiguous(
        ambiguous_client_id,
        observed_at=created_at + timedelta(seconds=2),
    )
    for index, intent in enumerate(opening.intents[1:], start=3):
        service.submit_one(
            intent.client_order_id,
            safety=_safety(
                risk_decision,
                evaluated_at=created_at + timedelta(seconds=index),
            ),
        )
    if recovered.state.value != "ACCEPTED" or any(
        order.state.value != "FILLED"
        for order in repository.all_orders()
        if order.client_order_id != ambiguous_client_id
    ):
        raise DemoError("opening fake orders did not reach deterministic known states")
    accepted_events = tuple(
        event
        for event in repository.order_events()
        if event.client_order_id == ambiguous_client_id and event.to_state.value == "ACCEPTED"
    )
    if len(accepted_events) != 1:
        raise DemoError("ambiguous acceptance recovery duplicated the broker side effect")
    recovery.recover(
        "fake_broker_acceptance_before_response_persistence",
        reopened_state=reopened_ambiguous_state,
        side_effect_ids=_broker_effect_ids(broker),
        incident_ids=_incident_ids(engine),
    )

    pre_reconciliation_broker_state = broker.snapshot()
    durable_pre_reconciliation_state = _combined_restart_state(engine, broker)
    _interrupt_once(
        recovery,
        "before_reconciliation",
        durable_state=durable_pre_reconciliation_state,
    )
    engine = _reopen_sqlite_engine(engine)
    repository = SignedExecutionRepository(engine)
    broker = DeterministicFakePaperBroker.from_snapshot(pre_reconciliation_broker_state)
    reopened_pre_reconciliation_state = _combined_restart_state(engine, broker)
    flatten_at = datetime(2026, 7, 6, 19, 43, tzinfo=UTC)
    reconciliation_request = _reconciliation_request(
        repository=repository,
        broker=broker,
        planning_result=opening,
        completed_at=flatten_at,
    )
    initial_reconciliation = reconcile_and_persist(
        repository=repository,
        request=reconciliation_request,
        latch_state=RiskLatchState.empty(experiment_hash=experiment.content_hash),
    )
    if initial_reconciliation.receipt.status.value != "CLEAN":
        raise DemoError("opening execution did not reconcile")
    replayed_reconciliation = reconcile_and_persist(
        repository=repository,
        request=reconciliation_request,
        latch_state=RiskLatchState.empty(experiment_hash=experiment.content_hash),
    )
    if (
        replayed_reconciliation.receipt != initial_reconciliation.receipt
        or len(repository.reconciliations()) != 1
    ):
        raise DemoError("reconciliation recovery retry did not converge")
    recovery.recover(
        "before_reconciliation",
        reopened_state=reopened_pre_reconciliation_state,
        side_effect_ids=_table_effect_ids(engine, "aqa_reconciliations"),
        incident_ids=_incident_ids(engine),
    )

    planned: list[ExecutionPlanningResult] = []
    slots = DecisionSlotRepository(
        engine,
        materialization_probe=_DemoForcedFlatPlanProbe(planned),
    )
    forced_slot = next(
        slot
        for slot in slots.list_for_session(
            experiment_hash=experiment.content_hash,
            session_date=DEMO_SESSION_DATE,
        )
        if slot.decision_type is DecisionType.FORCED_FLAT
    )
    attempted_at = flatten_at + timedelta(seconds=1)

    def plan_flatten(
        pre_receipt: ReconciliationReceipt,
        account: AccountState,
        positions: tuple[Position, ...],
    ) -> ExecutionPlanningResult:
        observed_positions = {position.symbol: position.quantity for position in positions}
        complete_positions = tuple(
            Position(symbol, observed_positions.get(symbol, Decimal(0)))
            for symbol in experiment.active_tradable
        )
        forced_context = DecisionContext.from_experiment(
            slot=forced_slot,
            experiment=experiment,
            data_contract_hash=sha256_hex(
                ("offline-demo-forced-flat-data", risk_decision.content_hash)
            ),
            policy_hash=risk_decision.policy_hash,
            execution_mode=ExecutionMode.OFFLINE,
            broker_adapter=BrokerAdapter.FAKE,
            submission_enabled=False,
            strategy_slot_ordinal=None,
        )
        forced_signal = OfflineFixtureSignalProvider(
            clock=lambda: pre_receipt.completed_at,
            scenario=FixtureSignalScenario(
                first_slot_long_symbol=experiment.active_tradable[-2],
                first_slot_short_symbol=experiment.active_tradable[1],
                expected_edge_bps=Decimal(25),
            ),
        ).signal_for(forced_context)
        SignalEnvelopeRepository(engine).persist_once(forced_signal, context=forced_context)
        flatten_decision = _forced_flat_decision(
            risk_decision,
            signal=forced_signal,
            slot=forced_slot,
            decided_at=pre_receipt.completed_at,
            positions=complete_positions,
            prices=marks,
            account=account,
            reconciliation_at=pre_receipt.completed_at,
        )
        if SignedRiskRepository(engine).persist(flatten_decision) != flatten_decision:
            raise DemoError("forced flatten risk authority did not persist")
        result = plan_signed_orders(
            ExecutionPlanningRequest(
                risk_decision=flatten_decision,
                current_positions=complete_positions,
                reference_prices=marks,
                equity=account.equity,
                target_version=2,
                created_at=pre_receipt.completed_at,
                deadline_at=forced_slot.deadline_at,
                forced_flat=True,
            )
        )
        planned.append(result)
        return result

    def flatten_safety(_intent: OrderIntent) -> SubmissionSafetySnapshot:
        if len(planned) != 1:
            raise DemoError("forced flatten safety preceded planning")
        return _safety(
            planned[0].risk_decision,
            evaluated_at=flatten_at + timedelta(seconds=3),
            entry_disabled=True,
        )

    def final_reconciliation() -> ReconciliationRequest:
        if len(planned) != 1:
            raise DemoError("forced flatten reconciliation preceded planning")
        return _reconciliation_request(
            repository=repository,
            broker=broker,
            planning_result=planned[0],
            completed_at=flatten_at + timedelta(seconds=10),
            require_flat=True,
            required_flat_at=forced_slot.required_completion_at,
        )

    completion_reopened_state: list[object] = []

    def complete_after_restart(
        slot_id: str,
        *,
        owner: str,
        now: datetime,
    ) -> DecisionSlot:
        nonlocal broker, engine, repository, slots
        broker_state = broker.snapshot()
        durable_state = _combined_restart_state(engine, broker)
        _interrupt_once(
            recovery,
            "before_forced_flatten_completion",
            durable_state=durable_state,
        )
        engine = _reopen_sqlite_engine(engine)
        repository = SignedExecutionRepository(engine)
        broker = DeterministicFakePaperBroker.from_snapshot(broker_state)
        slots = DecisionSlotRepository(
            engine,
            materialization_probe=_DemoForcedFlatPlanProbe(planned),
        )
        completion_reopened_state.append(_combined_restart_state(engine, broker))
        return slots.complete(slot_id, owner=owner, now=now)

    result = ForcedFlattenService(repository=repository, broker=broker).run(
        ForcedFlattenRequest(
            slot_id=forced_slot.slot_id,
            experiment_hash=experiment.content_hash,
            claim_owner="offline_demo_flatten",
            attempted_at=attempted_at,
            claim_slot=slots.claim,
            renew_slot=slots.renew_lease,
            complete_slot=complete_after_restart,
            fail_slot=slots.fail,
            latch_state_provider=lambda: RiskLatchState.empty(
                experiment_hash=experiment.content_hash
            ),
            pre_reconciliation=lambda: _reconciliation_request(
                repository=repository,
                broker=broker,
                planning_result=opening,
                completed_at=flatten_at + timedelta(seconds=2),
            ),
            planning_provider=plan_flatten,
            safety_provider=flatten_safety,
            final_reconciliation=final_reconciliation,
        )
    )
    if not result.success or broker.positions() or result.planning_result is None:
        raise DemoError("forced flatten did not prove exact zero positions")
    if len(completion_reopened_state) != 1:
        raise DemoError("forced flatten completion restart was not exercised exactly once")
    flatten = result.planning_result
    flatten_decision = flatten.risk_decision

    replay_service = ExecutionService(repository=repository, broker=broker)
    replay = replay_service.submit_plan(
        flatten,
        safety_provider=lambda _intent: _safety(
            flatten_decision,
            evaluated_at=flatten_at + timedelta(seconds=11),
            entry_disabled=True,
        ),
    )
    if any(item.submitted for item in replay):
        raise DemoError("execution replay attempted a duplicate broker side effect")
    recovery.recover(
        "before_forced_flatten_completion",
        reopened_state=completion_reopened_state[0],
        side_effect_ids=_broker_effect_ids(broker),
        incident_ids=_incident_ids(engine),
    )

    final_account = broker.account(observed_at=flatten_at + timedelta(seconds=12))
    return engine, _ExecutionEvidence(
        plan_hashes=(opening.plan.content_hash, flatten.plan.content_hash),
        client_order_ids=tuple(intent.client_order_id for intent in repository.all_intents()),
        broker_events=tuple(
            (
                item.client_order_id,
                item.sequence,
                item.to_state.value,
                item.broker_event_id,
            )
            for item in repository.order_events()
        ),
        fill_hashes=tuple(fill.content_hash for fill in repository.fills()),
        reconciliation_hashes=tuple(
            receipt.content_hash for receipt in repository.reconciliations()
        ),
        final_positions=tuple(
            (position.symbol, format(position.quantity, "f")) for position in broker.positions()
        ),
        final_account_values=(
            (
                "buying_power",
                format(final_account.buying_power.quantize(Decimal("0.00000001")), "f"),
            ),
            ("cash", format(final_account.cash.quantize(Decimal("0.00000001")), "f")),
            ("equity", format(final_account.equity.quantize(Decimal("0.00000001")), "f")),
            (
                "restricted_short_proceeds",
                format(final_account.restricted_short_proceeds, "f"),
            ),
        ),
        ambiguous_recovered=recovered.state.value == "ACCEPTED",
    )


def _forced_flat_decision(
    decision: RiskDecision,
    *,
    signal: SignalEnvelope,
    slot: DecisionSlot,
    decided_at: datetime,
    positions: tuple[Position, ...],
    prices: tuple[tuple[str, Decimal], ...],
    account: AccountState,
    reconciliation_at: datetime,
) -> RiskDecision:
    zero_targets = tuple((symbol, Decimal(0)) for symbol, _ in decision.final_targets)
    zero_exposure = ExposureSnapshot(
        gross=Decimal(0),
        net=Decimal(0),
        positive=Decimal(0),
        short_abs=Decimal(0),
        group_gross=(),
        cluster_gross=(),
    )
    return RiskDecision.create(
        slot_id=slot.slot_id,
        signal_id=signal.signal_id,
        signal_hash=signal.content_hash,
        experiment_hash=decision.experiment_hash,
        policy_id=decision.policy_id,
        policy_version=decision.policy_version,
        policy_hash=decision.policy_hash,
        correlation_id=slot.correlation_id,
        decided_at=decided_at,
        input_hash=sha256_hex(("offline-demo-forced-flat", decision.content_hash)),
        statistics_hash=decision.statistics_hash,
        account_snapshot=AccountSnapshot(
            account_id_hash=account.account_id_hash,
            equity=account.equity,
            cash=account.cash,
            buying_power=account.buying_power,
            observed_at=account.observed_at,
        ),
        planning_positions=tuple(
            SignedPosition(position.symbol, position.quantity) for position in positions
        ),
        planning_prices=tuple(
            PlanningPrice(symbol, price, decided_at, True) for symbol, price in prices
        ),
        security_metadata=tuple(
            SecurityMetadataSnapshot(
                symbol=security.symbol,
                asset_active=security.asset_active,
                tradable=security.tradable,
                shortable=security.shortable,
                easy_to_borrow=security.easy_to_borrow,
                primary_listing_eligible=security.primary_listing_eligible,
                broker_capability_known=security.broker_capability_known,
                observed_at=decided_at,
            )
            for security in decision.security_metadata
        ),
        original_proposal=tuple(
            (
                symbol,
                signal.actions[index].value,
                signal.expected_edge_bps[index],
                signal.proposed_signed_target_inputs[index],
            )
            for index, symbol in enumerate(signal.active_symbols)
        ),
        proposed_targets=zero_targets,
        final_targets=zero_targets,
        before_exposure=zero_exposure,
        after_exposure=zero_exposure,
        ordered_controls=(),
        block_reasons=("forced_flat",),
        flatten_reasons=("forced_flat",),
        source_timestamps=(
            ("account", account.observed_at),
            ("forced_flat_target", decided_at),
            ("reconciliation", reconciliation_at),
        ),
        latch_state_hash=decision.latch_state_hash,
        active_latches=(),
        required_latch_events=(),
        execution_scope=RiskExecutionScope.RISK_REDUCING_ONLY,
    )


__all__ = [
    "DEMO_SESSION_DATE",
    "EVIDENCE_LABEL",
    "DemoComparison",
    "DemoError",
    "DemoEvidence",
    "RecoveryEvidence",
    "publish_demo_evidence",
    "run_demo_once",
    "run_demo_twice",
]
