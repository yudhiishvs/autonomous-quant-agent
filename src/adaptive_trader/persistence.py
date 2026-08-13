"""SQLAlchemy-backed durable state and append-only audit repositories.

SQLite is intentionally the default.  The schema separates immutable facts
(decisions, intents, events, and fills) from the mutable ``broker_orders``
projection used for efficient status queries.
"""

from __future__ import annotations

import hashlib
import json
import platform
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    insert,
    literal_column,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from adaptive_trader.clock import as_utc
from adaptive_trader.constants import DATABASE_SCHEMA_VERSION, NEW_YORK, UTC
from adaptive_trader.exceptions import PersistenceError, SchemaVersionError
from adaptive_trader.live_models import (
    AccountState,
    LocalOrderState,
    MarketBar,
    OrderIntent,
    PositionState,
    ReconciliationResult,
    TradeUpdate,
)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store aware UTC datetimes as ISO text and restore their timezone."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        del dialect
        return None if value is None else as_utc(value).isoformat()

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        return as_utc(datetime.fromisoformat(value))


metadata = MetaData()

schema_info = Table(
    "schema_info",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", String(255), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
)

application_runs = Table(
    "application_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("ended_at", UTCDateTime()),
    Column("mode", String(32), nullable=False),
    Column("configuration_hash", String(64), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("git_commit", String(64)),
    Column("python_version", String(64), nullable=False),
    Column("dependency_metadata", JSON, nullable=False, default=dict),
    Column("market_data_feed", String(32)),
    Column("host_identifier", String(64), nullable=False),
    Column("shutdown_reason", Text),
)

configuration_snapshots = Table(
    "configuration_snapshots",
    metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("run_id", ForeignKey("application_runs.run_id"), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("configuration_hash", String(64), nullable=False),
    Column("configuration", JSON, nullable=False),
    UniqueConstraint("run_id", "configuration_hash"),
)

strategy_versions = Table(
    "strategy_versions",
    metadata,
    Column("record_id", String(64), primary_key=True),
    Column("run_id", ForeignKey("application_runs.run_id"), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("strategy_name", String(128), nullable=False),
    Column("version", String(128), nullable=False),
    Column("metadata", JSON, nullable=False, default=dict),
    UniqueConstraint("run_id", "strategy_name", "version"),
)

market_bars = Table(
    "market_bars",
    metadata,
    Column("bar_id", String(64), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("start_at", UTCDateTime(), nullable=False),
    Column("end_at", UTCDateTime()),
    Column("open", String(64), nullable=False),
    Column("high", String(64), nullable=False),
    Column("low", String(64), nullable=False),
    Column("close", String(64), nullable=False),
    Column("volume", Integer, nullable=False),
    Column("trade_count", Integer),
    Column("vwap", String(64)),
    Column("feed", String(32), nullable=False),
    Column("received_at", UTCDateTime(), nullable=False),
    Column("source", String(64), nullable=False),
    Column("is_correction", Boolean, nullable=False, default=False),
    Column("revision", Integer, nullable=False, default=0),
    UniqueConstraint("symbol", "start_at", "feed", name="uq_market_bar_symbol_start_feed"),
)

market_data_gaps = Table(
    "market_data_gaps",
    metadata,
    Column("gap_id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("gap_start", UTCDateTime(), nullable=False),
    Column("gap_end", UTCDateTime(), nullable=False),
    Column("feed", String(32), nullable=False),
    Column("resolved_at", UTCDateTime()),
    Column("details", JSON, nullable=False, default=dict),
)

stream_events = Table(
    "stream_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("stream", String(32), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("symbol", String(32)),
    Column("payload", JSON, nullable=False, default=dict),
)

account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("timestamp", UTCDateTime(), nullable=False),
    Column("account_id_hash", String(64), nullable=False),
    Column("status", String(64), nullable=False),
    Column("equity", String(64), nullable=False),
    Column("cash", String(64), nullable=False),
    Column("buying_power", String(64), nullable=False),
    Column("last_equity", String(64)),
    Column("trading_blocked", Boolean, nullable=False),
    Column("source", String(32), nullable=False, default="broker"),
)

position_snapshots = Table(
    "position_snapshots",
    metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("account_snapshot_id", ForeignKey("account_snapshots.snapshot_id")),
    Column("run_id", String(64)),
    Column("timestamp", UTCDateTime(), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("quantity", String(64), nullable=False),
    Column("market_value", String(64), nullable=False),
    Column("average_entry_price", String(64)),
    Column("current_price", String(64)),
    Column("unrealized_pl", String(64)),
)


def _decision_fact_table(name: str, id_name: str) -> Table:
    return Table(
        name,
        metadata,
        Column(id_name, String(64), primary_key=True),
        Column("run_id", String(64), nullable=False),
        Column("decision_id", String(64), nullable=False),
        Column("created_at", UTCDateTime(), nullable=False),
        Column("as_of_at", UTCDateTime()),
        Column("payload", JSON, nullable=False),
    )


strategy_signals = _decision_fact_table("strategy_signals", "signal_id")
regime_states = _decision_fact_table("regime_states", "regime_state_id")
allocation_results = _decision_fact_table("allocation_results", "allocation_id")
risk_decisions = _decision_fact_table("risk_decisions", "risk_decision_id")

risk_actions = Table(
    "risk_actions",
    metadata,
    Column("risk_action_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("decision_id", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("control", String(128), nullable=False),
    Column("description", Text, nullable=False),
    Column("details", JSON, nullable=False, default=dict),
)

rebalance_decisions = Table(
    "rebalance_decisions",
    metadata,
    Column("decision_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("session_date", String(10), nullable=False),
    Column("strategy_version", String(128), nullable=False),
    Column("mode", String(32), nullable=False),
    Column("scheduled_at", UTCDateTime()),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime()),
    Column("status", String(32), nullable=False),
    Column("skip_reason", Text),
    Column("payload", JSON, nullable=False, default=dict),
)

decision_receipts = Table(
    "decision_receipts",
    metadata,
    Column("receipt_id", String(64), primary_key=True),
    Column("decision_id", String(64), nullable=False, unique=True),
    Column("run_id", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("receipt_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)

order_intents = Table(
    "order_intents",
    metadata,
    Column("intent_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("decision_id", String(64), nullable=False),
    Column("client_order_id", String(48), nullable=False, unique=True),
    Column("session_date", String(10), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("notional", String(64)),
    Column("quantity", String(64)),
    Column("reference_price", String(64), nullable=False),
    Column("reason", String(128), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    UniqueConstraint("decision_id", "symbol", "side", name="uq_intent_decision_symbol_side"),
)

broker_orders = Table(
    "broker_orders",
    metadata,
    Column("client_order_id", String(48), primary_key=True),
    Column("broker_order_id", String(64), unique=True),
    Column("run_id", String(64), nullable=False),
    Column("decision_id", String(64), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("state", String(32), nullable=False),
    Column("raw_status", String(64)),
    Column("requested_notional", String(64)),
    Column("requested_quantity", String(64)),
    Column("filled_quantity", String(64), nullable=False, default="0"),
    Column("average_fill_price", String(64)),
    Column("submission_started_at", UTCDateTime()),
    Column("submitted_at", UTCDateTime()),
    Column("last_update_at", UTCDateTime(), nullable=False),
    Column("error_code", String(128)),
    Column("error_message", Text),
)

order_events = Table(
    "order_events",
    metadata,
    Column("order_event_id", String(64), primary_key=True),
    Column("event_key", String(128), nullable=False, unique=True),
    Column("client_order_id", String(48), nullable=False),
    Column("broker_order_id", String(64)),
    Column("decision_id", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("from_state", String(32)),
    Column("to_state", String(32), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSON, nullable=False, default=dict),
)

fill_events = Table(
    "fill_events",
    metadata,
    Column("fill_id", String(128), primary_key=True),
    Column("client_order_id", String(48), nullable=False),
    Column("broker_order_id", String(64), nullable=False),
    Column("decision_id", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("quantity", String(64), nullable=False),
    Column("price", String(64), nullable=False),
    Column("payload", JSON, nullable=False, default=dict),
)

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    Column("reconciliation_id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime(), nullable=False),
    Column("clean", Boolean, nullable=False),
    Column("blocking", Boolean, nullable=False),
    Column("summary", JSON, nullable=False, default=dict),
)

reconciliation_discrepancies = Table(
    "reconciliation_discrepancies",
    metadata,
    Column("discrepancy_id", String(64), primary_key=True),
    Column("reconciliation_id", ForeignKey("reconciliation_runs.reconciliation_id")),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("kind", String(128), nullable=False),
    Column("severity", String(32), nullable=False),
    Column("symbol", String(32)),
    Column("client_order_id", String(48)),
    Column("message", Text, nullable=False),
    Column("details", JSON, nullable=False, default=dict),
    Column("resolved_at", UTCDateTime()),
)

halt_events = Table(
    "halt_events",
    metadata,
    Column("halt_event_id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("action", String(32), nullable=False),
    Column("latch_type", String(32), nullable=False),
    Column("initiator", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("acknowledgement", String(128)),
    Column("session_date", String(10)),
    Column("details", JSON, nullable=False, default=dict),
)

system_incidents = Table(
    "system_incidents",
    metadata,
    Column("incident_id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("incident_type", String(128), nullable=False),
    Column("severity", String(32), nullable=False),
    Column("message", Text, nullable=False),
    Column("details", JSON, nullable=False, default=dict),
    Column("resolved_at", UTCDateTime()),
)

heartbeats = Table(
    "heartbeats",
    metadata,
    Column("heartbeat_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("mode", String(32), nullable=False),
    Column("healthy", Boolean, nullable=False),
    Column("components", JSON, nullable=False, default=dict),
)


def _performance_table(name: str, id_name: str) -> Table:
    return Table(
        name,
        metadata,
        Column(id_name, String(64), primary_key=True),
        Column("run_id", String(64), nullable=False),
        Column("session_date", String(10), nullable=False),
        Column("created_at", UTCDateTime(), nullable=False),
        Column("value", Float),
        Column("payload", JSON, nullable=False, default=dict),
        UniqueConstraint("run_id", "session_date"),
    )


daily_performance = _performance_table("daily_performance", "performance_id")
benchmark_performance = _performance_table("benchmark_performance", "benchmark_id")

generated_reports = Table(
    "generated_reports",
    metadata,
    Column("report_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("report_type", String(128), nullable=False),
    Column("path", Text, nullable=False),
    Column("content_hash", String(64)),
    Column("metadata", JSON, nullable=False, default=dict),
)

TABLES: dict[str, Table] = {table.name: table for table in metadata.tables.values()}


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_configuration(configuration: Any) -> Any:
    """Return the exact canonical configuration persisted for a run."""

    canonical = getattr(configuration, "to_canonical_dict", None)
    if callable(canonical):
        return canonical()
    compatible = getattr(configuration, "to_dict", None)
    if callable(compatible):
        return compatible()
    return configuration


def configuration_hash(configuration: Any) -> str:
    configured = getattr(configuration, "configuration_hash", None)
    if callable(configured):
        configured = configured()
    if isinstance(configured, str) and configured:
        return configured
    return hashlib.sha256(
        canonical_json(canonical_configuration(configuration)).encode()
    ).hexdigest()


class Database:
    """SQLite database lifecycle with explicit schema versioning.

    ``source`` may be a path, an AppConfig-like object, or an object exposing
    ``database_path``.  No credentials are read here.
    """

    def __init__(self, source: str | Path | Any = "runtime/adaptive_portfolio_agent.db") -> None:
        project = getattr(source, "project", source)
        raw_path = getattr(project, "database_path", source)
        if not isinstance(raw_path, (str, Path)):
            raw_path = "runtime/adaptive_portfolio_agent.db"
        self.path = Path(raw_path) if str(raw_path) != ":memory:" else Path(":memory:")
        if str(self.path) == ":memory:":
            self.engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                future=True,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                f"sqlite+pysqlite:///{self.path}",
                future=True,
                connect_args={"check_same_thread": False, "timeout": 30},
            )
        event.listen(self.engine, "connect", self._configure_connection)
        self._lock = threading.RLock()
        self.initialize_schema()

    @staticmethod
    def _configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        # In-memory SQLite and read-only test handles may not support WAL.
        with suppress(Exception):
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def initialize_schema(self) -> None:
        with self._lock, self.engine.begin() as connection:
            metadata.create_all(connection)
            row = connection.execute(
                select(schema_info.c.value).where(schema_info.c.key == "schema_version")
            ).scalar_one_or_none()
            if row is None:
                connection.execute(
                    insert(schema_info).values(
                        key="schema_version",
                        value=str(DATABASE_SCHEMA_VERSION),
                        updated_at=utc_now(),
                    )
                )
            elif int(row) != DATABASE_SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"Database schema version {row} is not supported; "
                    f"expected {DATABASE_SCHEMA_VERSION}"
                )

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self._lock, self.engine.begin() as connection:
            yield connection

    def accessible(self) -> bool:
        try:
            with self.engine.connect() as connection:
                return bool(
                    connection.execute(select(func.count()).select_from(schema_info)).scalar()
                )
        except Exception:
            return False

    def close(self) -> None:
        self.engine.dispose()


class AuditRepository:
    """Small transactional API used by all live and replay components."""

    def __init__(self, database: Database | str | Path | Any) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def start_run(
        self,
        *,
        mode: str,
        configuration: Any,
        market_data_feed: str | None = None,
        run_id: str | None = None,
        git_commit: str | None = None,
        dependencies: Mapping[str, str] | None = None,
        strategy_name: str = "adaptive_portfolio",
        strategy_version: str | None = None,
    ) -> str:
        identifier = run_id or uuid.uuid4().hex
        config_hash = configuration_hash(configuration)
        config_values = canonical_configuration(configuration)
        host_hash = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]
        resolved_dependencies = dict(runtime_metadata())
        resolved_dependencies.update(
            {str(name): str(version) for name, version in (dependencies or {}).items()}
        )
        resolved_git_commit = git_revision() if git_commit is None else git_commit
        with self.database.begin() as connection:
            connection.execute(
                insert(application_runs).values(
                    run_id=identifier,
                    started_at=utc_now(),
                    mode=str(mode),
                    configuration_hash=config_hash,
                    schema_version=DATABASE_SCHEMA_VERSION,
                    git_commit=resolved_git_commit,
                    python_version=platform.python_version(),
                    dependency_metadata=resolved_dependencies,
                    market_data_feed=market_data_feed,
                    host_identifier=host_hash,
                )
            )
            connection.execute(
                insert(configuration_snapshots).values(
                    snapshot_id=uuid.uuid4().hex,
                    run_id=identifier,
                    created_at=utc_now(),
                    configuration_hash=config_hash,
                    configuration=_json_value(config_values),
                )
            )
            if strategy_version is not None:
                connection.execute(
                    insert(strategy_versions).values(
                        record_id=uuid.uuid4().hex,
                        run_id=identifier,
                        created_at=utc_now(),
                        strategy_name=str(strategy_name),
                        version=str(strategy_version),
                        metadata={"source": "application_run_start", "mode": str(mode)},
                    )
                )
        return identifier

    def record_strategy_version(
        self,
        *,
        run_id: str,
        strategy_name: str,
        version: str,
        metadata_values: Mapping[str, Any] | None = None,
    ) -> bool:
        """Register a strategy version once per run."""

        with self.database.begin() as connection:
            try:
                connection.execute(
                    insert(strategy_versions).values(
                        record_id=uuid.uuid4().hex,
                        run_id=run_id,
                        created_at=utc_now(),
                        strategy_name=str(strategy_name),
                        version=str(version),
                        metadata=_json_value(metadata_values or {}),
                    )
                )
                return True
            except IntegrityError:
                return False

    def end_run(self, run_id: str, reason: str) -> None:
        with self.database.begin() as connection:
            connection.execute(
                update(application_runs)
                .where(application_runs.c.run_id == run_id)
                .values(ended_at=utc_now(), shutdown_reason=str(reason))
            )

    def append_fact(
        self,
        table_name: str,
        *,
        run_id: str,
        decision_id: str,
        payload: Mapping[str, Any],
        as_of_at: datetime | None = None,
    ) -> str:
        if table_name not in {
            "strategy_signals",
            "regime_states",
            "allocation_results",
            "risk_decisions",
        }:
            raise ValueError(f"Unsupported immutable fact table: {table_name}")
        table = TABLES[table_name]
        primary = next(iter(table.primary_key.columns)).name
        record_id = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(table).values(
                    **{
                        primary: record_id,
                        "run_id": run_id,
                        "decision_id": decision_id,
                        "created_at": utc_now(),
                        "as_of_at": as_of_at,
                        "payload": _json_value(payload),
                    }
                )
            )
        return record_id

    def append_risk_actions(
        self,
        *,
        run_id: str,
        decision_id: str,
        actions: Sequence[Mapping[str, Any]],
        created_at: datetime | None = None,
    ) -> tuple[str, ...]:
        """Append normalized risk actions with deterministic deduplication."""

        identifiers: list[str] = []
        timestamp = created_at or utc_now()
        with self.database.begin() as connection:
            for action in actions:
                normalized = _json_value(action)
                digest = hashlib.sha256(
                    f"{decision_id}:{canonical_json(normalized)}".encode()
                ).hexdigest()
                control = str(action.get("control") or action.get("type") or "unspecified")
                description = str(
                    action.get("description")
                    or action.get("message")
                    or action.get("reason")
                    or control
                )
                try:
                    connection.execute(
                        insert(risk_actions).values(
                            risk_action_id=digest,
                            run_id=run_id,
                            decision_id=decision_id,
                            created_at=timestamp,
                            control=control,
                            description=description,
                            details=normalized,
                        )
                    )
                    identifiers.append(digest)
                except IntegrityError:
                    continue
        return tuple(identifiers)

    def claim_rebalance(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        session_date: date,
        strategy_version: str,
        mode: str,
        scheduled_at: datetime | None = None,
        decision_id: str | None = None,
    ) -> tuple[str, bool]:
        identifier = decision_id or uuid.uuid4().hex
        values: dict[str, Any] = {
            "decision_id": identifier,
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "session_date": session_date.isoformat(),
            "strategy_version": strategy_version,
            "mode": str(mode),
            "scheduled_at": scheduled_at,
            "created_at": utc_now(),
            "status": "claimed",
            "payload": {},
        }
        with self.database.begin() as connection:
            try:
                connection.execute(insert(rebalance_decisions).values(**values))
                return identifier, True
            except IntegrityError:
                existing = connection.execute(
                    select(rebalance_decisions.c.decision_id).where(
                        rebalance_decisions.c.idempotency_key == idempotency_key
                    )
                ).scalar_one()
                return str(existing), False

    def complete_rebalance(
        self,
        decision_id: str,
        *,
        status: str,
        payload: Mapping[str, Any] | None = None,
        skip_reason: str | None = None,
    ) -> None:
        with self.database.begin() as connection:
            connection.execute(
                update(rebalance_decisions)
                .where(rebalance_decisions.c.decision_id == decision_id)
                .values(
                    completed_at=utc_now(),
                    status=str(status),
                    skip_reason=skip_reason,
                    payload=_json_value(payload or {}),
                )
            )

    def get_rebalance(self, decision_id: str) -> Mapping[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(rebalance_decisions).where(
                        rebalance_decisions.c.decision_id == decision_id
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    def get_rebalance_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(rebalance_decisions).where(
                        rebalance_decisions.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def list_rebalances(self, *, status: str | None = None) -> list[Mapping[str, Any]]:
        query = select(rebalance_decisions)
        if status is not None:
            query = query.where(rebalance_decisions.c.status == status)
        query = query.order_by(rebalance_decisions.c.created_at.asc())
        with self.database.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def store_decision_receipt(
        self, *, run_id: str, decision_id: str, payload: Mapping[str, Any]
    ) -> str:
        normalized = _json_value(payload)
        digest = hashlib.sha256(canonical_json(normalized).encode()).hexdigest()
        receipt_id = uuid.uuid4().hex
        with self.database.begin() as connection:
            try:
                connection.execute(
                    insert(decision_receipts).values(
                        receipt_id=receipt_id,
                        decision_id=decision_id,
                        run_id=run_id,
                        created_at=utc_now(),
                        receipt_hash=digest,
                        payload=normalized,
                    )
                )
            except IntegrityError as exc:
                raise PersistenceError(
                    f"Decision receipt already exists for {decision_id}; receipts are immutable"
                ) from exc
        return receipt_id

    def has_decision_receipt(self, decision_id: str) -> bool:
        with self.database.engine.connect() as connection:
            return (
                connection.execute(
                    select(decision_receipts.c.receipt_id).where(
                        decision_receipts.c.decision_id == decision_id
                    )
                ).first()
                is not None
            )

    def store_market_bar(self, bar: MarketBar, *, run_id: str | None = None) -> str:
        """Insert, deduplicate, or revise one bar and return the action taken."""

        key = (bar.symbol, bar.start, bar.feed)
        values = {
            "bar_id": uuid.uuid4().hex,
            "symbol": bar.symbol,
            "start_at": bar.start,
            "end_at": bar.end,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "trade_count": bar.trade_count,
            "vwap": None if bar.vwap is None else str(bar.vwap),
            "feed": bar.feed,
            "received_at": bar.received_at,
            "source": bar.source,
            "is_correction": bar.is_correction,
            "revision": 0,
        }
        compare_fields = ("open", "high", "low", "close", "volume", "trade_count", "vwap")
        with self.database.begin() as connection:
            existing = (
                connection.execute(
                    select(market_bars).where(
                        market_bars.c.symbol == key[0],
                        market_bars.c.start_at == key[1],
                        market_bars.c.feed == key[2],
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                connection.execute(insert(market_bars).values(**values))
                action = "inserted"
            elif all(existing[name] == values[name] for name in compare_fields):
                action = "duplicate"
            else:
                connection.execute(
                    update(market_bars)
                    .where(market_bars.c.bar_id == existing["bar_id"])
                    .values(
                        **{
                            name: values[name]
                            for name in (
                                "end_at",
                                *compare_fields,
                                "received_at",
                                "source",
                            )
                        },
                        is_correction=True,
                        revision=int(existing["revision"]) + 1,
                    )
                )
                action = "corrected"
            connection.execute(
                insert(stream_events).values(
                    event_id=uuid.uuid4().hex,
                    run_id=run_id,
                    created_at=bar.received_at,
                    stream="market_data",
                    event_type=f"bar_{action}",
                    symbol=bar.symbol,
                    payload={"start": bar.start.isoformat(), "feed": bar.feed},
                )
            )
        return action

    def latest_bar_times(self, symbols: Sequence[str] | None = None) -> dict[str, datetime]:
        query = select(market_bars.c.symbol, func.max(market_bars.c.start_at).label("latest"))
        if symbols:
            query = query.where(market_bars.c.symbol.in_(list(symbols)))
        query = query.group_by(market_bars.c.symbol)
        with self.database.engine.connect() as connection:
            return {str(row.symbol): row.latest for row in connection.execute(query)}

    def record_stream_event(
        self,
        *,
        run_id: str | None,
        stream: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        symbol: str | None = None,
        created_at: datetime | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(stream_events).values(
                    event_id=identifier,
                    run_id=run_id,
                    created_at=created_at or utc_now(),
                    stream=stream,
                    event_type=event_type,
                    symbol=symbol,
                    payload=_json_value(payload or {}),
                )
            )
        return identifier

    def has_operator_halt_cancel_success(self, halt_event_id: str) -> bool:
        """Return whether cancel-all succeeded for one exact durable halt.

        JSON payload filtering is deliberately performed after selecting the
        narrow event type.  This stays portable across SQLite versions and
        avoids treating a cancellation belonging to another halt as evidence.
        """

        with self.database.engine.connect() as connection:
            payloads = connection.execute(
                select(stream_events.c.payload).where(
                    stream_events.c.event_type == "operator_halt_cancel_all_requested"
                )
            ).scalars()
            return any(
                isinstance(payload, Mapping)
                and str(payload.get("halt_event_id", "")) == str(halt_event_id)
                for payload in payloads
            )

    def record_gap(
        self,
        *,
        run_id: str | None,
        symbol: str,
        start: datetime,
        end: datetime,
        feed: str,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(market_data_gaps).values(
                    gap_id=identifier,
                    run_id=run_id,
                    created_at=utc_now(),
                    symbol=symbol,
                    gap_start=start,
                    gap_end=end,
                    feed=feed,
                    details=_json_value(details or {}),
                )
            )
        return identifier

    def unresolved_gaps(
        self,
        symbols: Sequence[str] | None = None,
    ) -> list[Mapping[str, Any]]:
        """Return durable market-data gaps that still require verified backfill."""

        query = select(market_data_gaps).where(market_data_gaps.c.resolved_at.is_(None))
        if symbols:
            query = query.where(
                market_data_gaps.c.symbol.in_([str(symbol).strip().upper() for symbol in symbols])
            )
        query = query.order_by(market_data_gaps.c.gap_start.asc())
        with self.database.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def resolve_gaps(
        self,
        *,
        gap_ids: Sequence[str],
        resolved_at: datetime | None = None,
    ) -> int:
        """Resolve exact gap IDs after explicit coverage or operator review."""

        identifiers = [str(gap_id) for gap_id in gap_ids if str(gap_id)]
        if not identifiers:
            return 0
        with self.database.begin() as connection:
            result = connection.execute(
                update(market_data_gaps)
                .where(
                    market_data_gaps.c.resolved_at.is_(None),
                    market_data_gaps.c.gap_id.in_(identifiers),
                )
                .values(resolved_at=resolved_at or utc_now())
            )
        return int(result.rowcount)

    def resolve_covered_gaps(
        self,
        *,
        symbols: Sequence[str],
        resolved_at: datetime | None = None,
    ) -> tuple[str, ...]:
        """Resolve only gaps whose entire minute interval is durably present."""

        normalized = [str(symbol).strip().upper() for symbol in symbols]
        if not normalized:
            return ()
        resolved: list[str] = []
        timestamp = resolved_at or utc_now()
        with self.database.begin() as connection:
            gaps = (
                connection.execute(
                    select(market_data_gaps).where(
                        market_data_gaps.c.resolved_at.is_(None),
                        market_data_gaps.c.symbol.in_(normalized),
                    )
                )
                .mappings()
                .all()
            )
            for gap in gaps:
                gap_start = gap["gap_start"]
                gap_end = gap["gap_end"]
                observed = set(
                    connection.execute(
                        select(market_bars.c.start_at).where(
                            market_bars.c.symbol == gap["symbol"],
                            market_bars.c.feed == gap["feed"],
                            market_bars.c.start_at >= gap_start,
                            market_bars.c.start_at <= gap_end,
                        )
                    ).scalars()
                )
                expected: set[datetime] = set()
                cursor = gap_start
                while cursor <= gap_end:
                    expected.add(cursor)
                    cursor += timedelta(minutes=1)
                if expected and expected.issubset(observed):
                    resolved.append(str(gap["gap_id"]))
            if resolved:
                connection.execute(
                    update(market_data_gaps)
                    .where(market_data_gaps.c.gap_id.in_(resolved))
                    .values(resolved_at=timestamp)
                )
        return tuple(resolved)

    def record_account_state(
        self,
        account: AccountState,
        positions: Sequence[PositionState],
        *,
        run_id: str | None,
    ) -> str:
        snapshot_id = uuid.uuid4().hex
        account_hash = hashlib.sha256(account.account_id.encode()).hexdigest()
        with self.database.begin() as connection:
            connection.execute(
                insert(account_snapshots).values(
                    snapshot_id=snapshot_id,
                    run_id=run_id,
                    timestamp=account.timestamp,
                    account_id_hash=account_hash,
                    status=account.status,
                    equity=str(account.equity),
                    cash=str(account.cash),
                    buying_power=str(account.buying_power),
                    last_equity=None if account.last_equity is None else str(account.last_equity),
                    trading_blocked=account.trading_blocked,
                    source="broker",
                )
            )
            for position in positions:
                connection.execute(
                    insert(position_snapshots).values(
                        snapshot_id=uuid.uuid4().hex,
                        account_snapshot_id=snapshot_id,
                        run_id=run_id,
                        timestamp=position.timestamp,
                        symbol=position.symbol,
                        quantity=str(position.quantity),
                        market_value=str(position.market_value),
                        average_entry_price=(
                            None
                            if position.average_entry_price is None
                            else str(position.average_entry_price)
                        ),
                        current_price=(
                            None if position.current_price is None else str(position.current_price)
                        ),
                        unrealized_pl=(
                            None if position.unrealized_pl is None else str(position.unrealized_pl)
                        ),
                    )
                )
        return snapshot_id

    def latest_account_state(
        self,
        *,
        before: datetime | None = None,
    ) -> Mapping[str, Any] | None:
        query = select(account_snapshots)
        if before is not None:
            query = query.where(account_snapshots.c.timestamp < as_utc(before))
        # Broker timestamps may repeat (especially across rapid reconciliation
        # passes). SQLite rowid supplies deterministic append order for ties.
        query = query.order_by(
            account_snapshots.c.timestamp.desc(),
            literal_column("rowid").desc(),
        ).limit(1)
        with self.database.engine.connect() as connection:
            row = connection.execute(query).mappings().first()
            return dict(row) if row is not None else None

    def account_equity_high_water(self) -> Decimal | None:
        """Return the highest durably observed paper-account equity."""

        with self.database.engine.connect() as connection:
            values = connection.execute(select(account_snapshots.c.equity)).scalars()
            equities = [Decimal(str(value)) for value in values]
        return max(equities) if equities else None

    def fill_cash_effect(
        self,
        *,
        after: datetime,
        through: datetime,
    ) -> Decimal:
        """Return signed cash generated by known fills in ``(after, through]``."""

        query = select(
            fill_events.c.side,
            fill_events.c.quantity,
            fill_events.c.price,
        ).where(
            fill_events.c.created_at > as_utc(after),
            fill_events.c.created_at <= as_utc(through),
        )
        effect = Decimal("0")
        with self.database.engine.connect() as connection:
            for row in connection.execute(query):
                notional = Decimal(str(row.quantity)) * Decimal(str(row.price))
                effect += notional if str(row.side) == "sell" else -notional
        return effect

    def latest_positions(self) -> dict[str, Mapping[str, Any]]:
        latest_account = self.latest_account_state()
        if latest_account is None:
            return {}
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(position_snapshots).where(
                    position_snapshots.c.account_snapshot_id == latest_account["snapshot_id"]
                )
            ).mappings()
            return {str(row["symbol"]): dict(row) for row in rows}

    def reserve_order_intent(self, *, run_id: str, intent: OrderIntent) -> bool:
        """Atomically persist an immutable intent and its local reservation."""

        event_key = f"reserve:{intent.client_order_id}"
        with self.database.begin() as connection:
            try:
                connection.execute(
                    insert(order_intents).values(
                        intent_id=uuid.uuid4().hex,
                        run_id=run_id,
                        decision_id=intent.decision_id,
                        client_order_id=intent.client_order_id,
                        session_date=intent.session_date.isoformat(),
                        symbol=intent.symbol,
                        side=intent.side.value,
                        sequence=intent.sequence,
                        notional=None if intent.notional is None else str(intent.notional),
                        quantity=None if intent.quantity is None else str(intent.quantity),
                        reference_price=str(intent.reference_price),
                        reason=intent.reason,
                        created_at=intent.created_at,
                    )
                )
                connection.execute(
                    insert(broker_orders).values(
                        client_order_id=intent.client_order_id,
                        run_id=run_id,
                        decision_id=intent.decision_id,
                        symbol=intent.symbol,
                        side=intent.side.value,
                        state=LocalOrderState.LOCALLY_RESERVED.value,
                        requested_notional=(
                            None if intent.notional is None else str(intent.notional)
                        ),
                        requested_quantity=(
                            None if intent.quantity is None else str(intent.quantity)
                        ),
                        filled_quantity="0",
                        last_update_at=intent.created_at,
                    )
                )
                connection.execute(
                    insert(order_events).values(
                        order_event_id=uuid.uuid4().hex,
                        event_key=event_key,
                        client_order_id=intent.client_order_id,
                        decision_id=intent.decision_id,
                        created_at=intent.created_at,
                        from_state=LocalOrderState.PLANNED.value,
                        to_state=LocalOrderState.LOCALLY_RESERVED.value,
                        event_type="local_reservation",
                        payload={},
                    )
                )
                return True
            except IntegrityError:
                return False

    def record_hypothetical_order_intents(
        self,
        *,
        run_id: str,
        intents: Sequence[OrderIntent],
        mode: str,
    ) -> tuple[str, ...]:
        """Persist observer/dry-run intents without creating executable orders."""

        recorded: list[str] = []
        with self.database.begin() as connection:
            for intent in intents:
                try:
                    connection.execute(
                        insert(order_intents).values(
                            intent_id=uuid.uuid4().hex,
                            run_id=run_id,
                            decision_id=intent.decision_id,
                            client_order_id=intent.client_order_id,
                            session_date=intent.session_date.isoformat(),
                            symbol=intent.symbol,
                            side=intent.side.value,
                            sequence=intent.sequence,
                            notional=(None if intent.notional is None else str(intent.notional)),
                            quantity=(None if intent.quantity is None else str(intent.quantity)),
                            reference_price=str(intent.reference_price),
                            reason=f"hypothetical_{mode}:{intent.reason}",
                            created_at=intent.created_at,
                        )
                    )
                    recorded.append(intent.client_order_id)
                except IntegrityError:
                    continue
        return tuple(recorded)

    def get_order(self, client_order_id: str) -> Mapping[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(broker_orders).where(broker_orders.c.client_order_id == client_order_id)
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    def has_order_event(self, event_key: str) -> bool:
        """Return whether an immutable broker event was already projected locally."""

        with self.database.engine.connect() as connection:
            return (
                connection.execute(
                    select(order_events.c.order_event_id).where(
                        order_events.c.event_key == event_key
                    )
                ).first()
                is not None
            )

    def list_orders(self, *, open_only: bool = False) -> list[Mapping[str, Any]]:
        query = select(broker_orders)
        if open_only:
            terminal = {
                LocalOrderState.FILLED.value,
                LocalOrderState.CANCELED.value,
                LocalOrderState.REJECTED.value,
                LocalOrderState.EXPIRED.value,
                LocalOrderState.REPLACED.value,
            }
            query = query.where(~broker_orders.c.state.in_(terminal))
        with self.database.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def transition_order(
        self,
        *,
        client_order_id: str,
        to_state: LocalOrderState,
        event_type: str,
        event_key: str,
        allowed_from: Iterable[LocalOrderState],
        created_at: datetime,
        broker_order_id: str | None = None,
        raw_status: str | None = None,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        payload: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Append one unique transition and update the current order projection."""

        created_at = as_utc(created_at)
        with self.database.begin() as connection:
            existing_event = connection.execute(
                select(order_events.c.order_event_id).where(order_events.c.event_key == event_key)
            ).scalar_one_or_none()
            if existing_event is not None:
                return False
            row = (
                connection.execute(
                    select(broker_orders).where(broker_orders.c.client_order_id == client_order_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise PersistenceError(f"Unknown local order: {client_order_id}")
            current = LocalOrderState(str(row["state"]))
            allowed = set(allowed_from)
            if current not in allowed:
                raise PersistenceError(
                    f"Concurrent/invalid order transition {current.value} -> {to_state.value}"
                )
            values: dict[str, Any] = {
                "state": to_state.value,
                "last_update_at": created_at,
                "raw_status": raw_status,
                "error_code": error_code,
                "error_message": error_message,
            }
            if broker_order_id is not None:
                values["broker_order_id"] = broker_order_id
            if filled_quantity is not None:
                values["filled_quantity"] = str(filled_quantity)
            if average_fill_price is not None:
                values["average_fill_price"] = str(average_fill_price)
            if to_state is LocalOrderState.SUBMISSION_STARTED:
                values["submission_started_at"] = created_at
            if (
                to_state
                in {
                    LocalOrderState.SUBMITTED,
                    LocalOrderState.ACCEPTED,
                    LocalOrderState.PENDING,
                    LocalOrderState.PARTIALLY_FILLED,
                    LocalOrderState.FILLED,
                }
                and row["submitted_at"] is None
            ):
                values["submitted_at"] = created_at
            connection.execute(
                update(broker_orders)
                .where(broker_orders.c.client_order_id == client_order_id)
                .values(**values)
            )
            connection.execute(
                insert(order_events).values(
                    order_event_id=uuid.uuid4().hex,
                    event_key=event_key,
                    client_order_id=client_order_id,
                    broker_order_id=broker_order_id or row["broker_order_id"],
                    decision_id=row["decision_id"],
                    created_at=created_at,
                    from_state=current.value,
                    to_state=to_state.value,
                    event_type=event_type,
                    payload=_json_value(payload or {}),
                )
            )
            return True

    def record_fill(
        self,
        update_event: TradeUpdate,
        *,
        source: str = "trade_update",
    ) -> bool:
        if update_event.fill_quantity is None or update_event.fill_price is None:
            return False
        row = self.get_order(update_event.order.client_order_id)
        if row is None:
            raise PersistenceError(
                f"Cannot attach fill to unknown order {update_event.order.client_order_id}"
            )
        fill_id = update_event.execution_id or update_event.fingerprint
        with self.database.begin() as connection:
            try:
                connection.execute(
                    insert(fill_events).values(
                        fill_id=fill_id,
                        client_order_id=update_event.order.client_order_id,
                        broker_order_id=update_event.order.broker_order_id,
                        decision_id=row["decision_id"],
                        created_at=update_event.timestamp,
                        symbol=update_event.order.symbol,
                        side=update_event.order.side.value,
                        quantity=str(update_event.fill_quantity),
                        price=str(update_event.fill_price),
                        payload={"event": update_event.event, "source": str(source)},
                    )
                )
                return True
            except IntegrityError:
                return False

    def cumulative_fill_quantity(self, client_order_id: str) -> Decimal:
        """Return the immutable fill-ledger total for one client order ID."""

        quantity, _ = self.fill_ledger_totals(client_order_id)
        return quantity

    def fill_ledger_totals(self, client_order_id: str) -> tuple[Decimal, Decimal]:
        """Return immutable cumulative quantity and notional for one order."""

        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(fill_events.c.quantity, fill_events.c.price).where(
                    fill_events.c.client_order_id == str(client_order_id)
                )
            ).all()
        quantity = sum((Decimal(str(row.quantity)) for row in rows), Decimal("0"))
        notional = sum(
            (Decimal(str(row.quantity)) * Decimal(str(row.price)) for row in rows),
            Decimal("0"),
        )
        return quantity, notional

    def record_reconciliation(self, result: ReconciliationResult, *, run_id: str | None) -> None:
        with self.database.begin() as connection:
            connection.execute(
                insert(reconciliation_runs).values(
                    reconciliation_id=result.reconciliation_id,
                    run_id=run_id,
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    clean=result.clean,
                    blocking=result.blocking,
                    summary={"discrepancy_count": len(result.discrepancies)},
                )
            )
            for item in result.discrepancies:
                connection.execute(
                    insert(reconciliation_discrepancies).values(
                        discrepancy_id=uuid.uuid4().hex,
                        reconciliation_id=result.reconciliation_id,
                        created_at=result.completed_at,
                        kind=item.kind,
                        severity=item.severity.value,
                        symbol=item.symbol,
                        client_order_id=item.client_order_id,
                        message=item.message,
                        details=_json_value(item.details),
                    )
                )

    def latest_reconciliation(self) -> Mapping[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(reconciliation_runs)
                    .order_by(reconciliation_runs.c.completed_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    def _record_performance(
        self,
        table: Table,
        primary_key: str,
        *,
        run_id: str,
        session_date: date,
        value: float | Decimal | None,
        payload: Mapping[str, Any],
        created_at: datetime | None = None,
    ) -> str:
        timestamp = created_at or utc_now()
        identifier = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"adaptive-trader:{table.name}:{run_id}:{session_date.isoformat()}",
        ).hex
        normalized_value = None if value is None else float(value)
        with self.database.begin() as connection:
            existing = connection.execute(
                select(table.c[primary_key]).where(
                    table.c.run_id == run_id,
                    table.c.session_date == session_date.isoformat(),
                )
            ).scalar_one_or_none()
            values = {
                "created_at": timestamp,
                "value": normalized_value,
                "payload": _json_value(payload),
            }
            if existing is None:
                connection.execute(
                    insert(table).values(
                        **{
                            primary_key: identifier,
                            "run_id": run_id,
                            "session_date": session_date.isoformat(),
                            **values,
                        }
                    )
                )
            else:
                identifier = str(existing)
                connection.execute(
                    update(table).where(table.c[primary_key] == existing).values(**values)
                )
        return identifier

    def record_daily_performance(
        self,
        *,
        run_id: str,
        session_date: date,
        metrics: Mapping[str, Any],
        created_at: datetime | None = None,
    ) -> str:
        """Idempotently store one honest forward-performance segment per session."""

        return self._record_performance(
            daily_performance,
            "performance_id",
            run_id=run_id,
            session_date=session_date,
            value=metrics.get("end_equity"),
            payload=metrics,
            created_at=created_at,
        )

    def record_benchmark_performance(
        self,
        *,
        run_id: str,
        session_date: date,
        metrics: Mapping[str, Any],
        created_at: datetime | None = None,
    ) -> str:
        """Store measured benchmark performance or an explicit unavailable reason."""

        return self._record_performance(
            benchmark_performance,
            "benchmark_id",
            run_id=run_id,
            session_date=session_date,
            value=metrics.get("benchmark_cumulative_return"),
            payload=metrics,
            created_at=created_at,
        )

    def latest_performance(
        self,
        table_name: str = "daily_performance",
        *,
        run_id: str | None = None,
        series_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        if table_name not in {"daily_performance", "benchmark_performance"}:
            raise ValueError("Unsupported performance table")
        table = TABLES[table_name]
        with self.database.engine.connect() as connection:
            query = select(table)
            if run_id is not None:
                query = query.where(table.c.run_id == run_id)
            rows = connection.execute(query.order_by(table.c.created_at.desc())).mappings()
            for row in rows:
                if series_id is None or str((row.get("payload") or {}).get("series_id")) == str(
                    series_id
                ):
                    return dict(row)
        return None

    def get_performance(
        self,
        *,
        run_id: str,
        session_date: date,
        table_name: str = "daily_performance",
    ) -> Mapping[str, Any] | None:
        if table_name not in {"daily_performance", "benchmark_performance"}:
            raise ValueError("Unsupported performance table")
        table = TABLES[table_name]
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.run_id == run_id,
                        table.c.session_date == session_date.isoformat(),
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    def record_halt(
        self,
        *,
        run_id: str | None,
        action: str,
        latch_type: str,
        initiator: str,
        reason: str,
        acknowledgement: str | None = None,
        session_date: date | None = None,
        details: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(halt_events).values(
                    halt_event_id=identifier,
                    run_id=run_id,
                    created_at=created_at or utc_now(),
                    action=str(action),
                    latch_type=str(latch_type),
                    initiator=str(initiator),
                    reason=str(reason),
                    acknowledgement=acknowledgement,
                    session_date=None if session_date is None else session_date.isoformat(),
                    details=_json_value(details or {}),
                )
            )
        return identifier

    def active_halts(self, now: datetime | None = None) -> dict[str, Mapping[str, Any]]:
        del now  # Reserved for session-expiring daily-loss latches.
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(halt_events).order_by(halt_events.c.created_at.asc())
            ).mappings()
            state: dict[str, Mapping[str, Any]] = {}
            for row in rows:
                latch_type = str(row["latch_type"])
                if row["action"] in {"halt", "hard_stop", "daily_loss"}:
                    state[latch_type] = dict(row)
                elif row["action"] in {"resume", "expired"}:
                    state.pop(latch_type, None)
            return state

    def expire_daily_loss_latch(
        self,
        *,
        run_id: str | None,
        next_session: date,
        created_at: datetime,
        reconciliation_id: str,
    ) -> str | None:
        """Expire a daily-loss latch only with current clean reconciliation evidence."""

        created_at = as_utc(created_at)
        with self.database.engine.connect() as connection:
            evidence = (
                connection.execute(
                    select(reconciliation_runs).where(
                        reconciliation_runs.c.reconciliation_id == reconciliation_id
                    )
                )
                .mappings()
                .first()
            )
        if evidence is None or not bool(evidence["clean"]) or bool(evidence["blocking"]):
            return None
        if run_id is not None and str(evidence.get("run_id")) != str(run_id):
            return None
        completed_at = as_utc(evidence["completed_at"])
        if completed_at > created_at:
            return None
        if completed_at.astimezone(NEW_YORK).date() != next_session:
            return None

        active = self.active_halts(created_at).get("daily_loss")
        if active is None:
            return None
        latched = active.get("session_date")
        if not latched or date.fromisoformat(str(latched)) >= next_session:
            return None
        return self.record_halt(
            run_id=run_id,
            action="expired",
            latch_type="daily_loss",
            initiator="session_calendar",
            reason="daily-loss latch expired at the next market session",
            session_date=next_session,
            created_at=created_at,
            details={
                "reconciliation_id": reconciliation_id,
                "reconciliation_completed_at": completed_at,
                "reconciliation_clean": True,
            },
        )

    def record_incident(
        self,
        *,
        run_id: str | None,
        incident_type: str,
        severity: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(system_incidents).values(
                    incident_id=identifier,
                    run_id=run_id,
                    created_at=utc_now(),
                    incident_type=incident_type,
                    severity=severity,
                    message=message,
                    details=_json_value(details or {}),
                )
            )
        return identifier

    def resolve_incident(
        self,
        incident_id: str,
        *,
        resolved_at: datetime | None = None,
    ) -> bool:
        """Resolve an incident projection without deleting its audit record."""

        with self.database.begin() as connection:
            result = connection.execute(
                update(system_incidents)
                .where(
                    system_incidents.c.incident_id == incident_id,
                    system_incidents.c.resolved_at.is_(None),
                )
                .values(resolved_at=resolved_at or utc_now())
            )
        return bool(result.rowcount)

    def active_incidents(
        self,
        *,
        run_id: str | None = None,
        configuration_hash: str | None = None,
    ) -> list[Mapping[str, Any]]:
        """Return unresolved incidents, optionally scoped to a logical deployment.

        A daemon restart intentionally creates a new ``run_id``.  Scoping by
        configuration hash lets the new process rehydrate unresolved incidents
        from compatible prior runs instead of incorrectly reporting healthy.
        """

        query = select(system_incidents).where(system_incidents.c.resolved_at.is_(None))
        if run_id is not None:
            query = query.where(system_incidents.c.run_id == run_id)
        if configuration_hash is not None:
            query = query.select_from(
                system_incidents.join(
                    application_runs,
                    system_incidents.c.run_id == application_runs.c.run_id,
                )
            ).where(application_runs.c.configuration_hash == configuration_hash)
        query = query.order_by(system_incidents.c.created_at.asc())
        with self.database.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def heartbeat(
        self,
        *,
        run_id: str,
        mode: str,
        healthy: bool,
        components: Mapping[str, Any],
        created_at: datetime | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(heartbeats).values(
                    heartbeat_id=identifier,
                    run_id=run_id,
                    created_at=created_at or utc_now(),
                    mode=mode,
                    healthy=healthy,
                    components=_json_value(components),
                )
            )
        return identifier

    def latest_heartbeat(self) -> Mapping[str, Any] | None:
        with self.database.engine.connect() as connection:
            row = (
                connection.execute(
                    select(heartbeats)
                    .order_by(
                        heartbeats.c.created_at.desc(),
                        literal_column("rowid").desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    def has_generated_report(self, report_type: str) -> bool:
        with self.database.engine.connect() as connection:
            return (
                connection.execute(
                    select(generated_reports.c.report_id).where(
                        generated_reports.c.report_type == report_type
                    )
                ).first()
                is not None
            )

    def record_generated_report(
        self,
        *,
        run_id: str,
        report_type: str,
        path: str,
        metadata_values: Mapping[str, Any] | None = None,
        content_hash: str | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.begin() as connection:
            connection.execute(
                insert(generated_reports).values(
                    report_id=identifier,
                    run_id=run_id,
                    created_at=utc_now(),
                    report_type=report_type,
                    path=str(path),
                    content_hash=content_hash,
                    metadata=_json_value(metadata_values or {}),
                )
            )
        return identifier

    def count(self, table_name: str) -> int:
        table = TABLES[table_name]
        with self.database.engine.connect() as connection:
            return int(connection.execute(select(func.count()).select_from(table)).scalar_one())

    def table_names(self) -> set[str]:
        return set(TABLES)


def evaluate_observer_readiness(
    configuration: Any,
    *,
    database_path: str | Path,
    evidence_directory: str | Path = "outputs/observer_evidence",
    project_root: str | Path = ".",
) -> dict[str, Any]:
    """Compatibility wrapper around the formal, read-only observer gate.

    The implementation lives in :mod:`adaptive_trader.observer_evidence` so
    session-audit scripts and the CLI use exactly the same evidence contract.
    Importing lazily avoids coupling database initialization to report-only
    commands.  The evaluator never opens :class:`Database`, and therefore can
    never create or migrate the primary observer database.
    """

    from adaptive_trader.observer_evidence import evaluate_observer_readiness as evaluate

    return evaluate(
        configuration,
        database_path=database_path,
        evidence_directory=evidence_directory,
        project_root=project_root,
    )


@lru_cache(maxsize=1)
def runtime_metadata() -> dict[str, str]:
    """Return non-secret interpreter metadata for application-run records."""

    result = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
    }
    for distribution in (
        "adaptive-portfolio-agent",
        "alpaca-py",
        "numpy",
        "pandas",
        "SQLAlchemy",
    ):
        with suppress(importlib_metadata.PackageNotFoundError):
            result[distribution] = importlib_metadata.version(distribution)
    return result


@lru_cache(maxsize=1)
def git_revision() -> str | None:
    """Return the available workspace commit, marked dirty when it is not reproducible."""

    try:
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if not revision:
            return None
        dirty = bool(
            subprocess.run(
                ("git", "status", "--porcelain", "--", "."),
                cwd=Path.cwd(),
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
        return f"{revision}+dirty" if dirty else revision
    except (OSError, subprocess.SubprocessError):
        return None
