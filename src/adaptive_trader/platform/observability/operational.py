"""Authoritative, restart-safe operational metric snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import BigInteger, Column, MetaData, String, Table, and_, func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_basket_watermarks,
    aqa_broker_orders,
    aqa_data_gaps,
    aqa_decision_slots,
    aqa_execution_plans,
    aqa_fills,
    aqa_incidents,
    aqa_job_attempts,
    aqa_jobs,
    aqa_order_intents,
    aqa_outbox_events,
    aqa_reconciliations,
    aqa_risk_decisions,
    aqa_risk_latch_events,
    aqa_signal_envelopes,
    aqa_symbol_watermarks,
)

DATA_GAP_STATES = ("open", "repairing", "resolved", "waived")
BASKET_WATERMARK_STATES = ("blocked", "ready")
DECISION_SLOT_STATES = (
    "pending",
    "waiting_for_data",
    "ready",
    "claimed",
    "completed",
    "skipped",
    "expired",
    "failed",
    "flatten_required",
)
RISK_EXECUTION_SCOPES = ("full", "none", "risk_reducing_only")
RISK_LATCH_TYPES = (
    "deployment_drawdown",
    "operator_halt",
    "reconciliation",
    "session_loss",
)
ORDER_STATES = (
    "planned",
    "intent_committed",
    "submission_started",
    "submitted",
    "accepted",
    "pending",
    "partially_filled",
    "filled",
    "cancel_requested",
    "canceled",
    "rejected",
    "expired",
    "submission_unknown",
    "reconciliation_required",
    "intent_only",
)
RECONCILIATION_STATES = ("clean", "blocking")
INCIDENT_STATES = ("open", "resolved")
INCIDENT_SEVERITIES = ("info", "warning", "error", "critical")
JOB_STATES = (
    "pending",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "dead",
    "canceled",
)
OUTBOX_STATES = ("pending", "claimed", "published", "failed", "dead")

_LATCH_ACTIONS = frozenset({"ENGAGED", "CLEARED"})


class OperationalMetricsReadError(RuntimeError):
    """The authoritative database snapshot could not be read safely."""


@dataclass(frozen=True, slots=True)
class OperationalMetricsSnapshot:
    """One transactionally coherent view of bounded persisted platform state."""

    bar_events: int
    bar_corrections: int
    data_gaps: tuple[tuple[str, int], ...]
    symbol_watermarks: int
    basket_watermarks: tuple[tuple[str, int], ...]
    decision_slots: tuple[tuple[str, int], ...]
    signals: int
    risk_decisions: tuple[tuple[str, int], ...]
    active_latches: tuple[tuple[str, int], ...]
    execution_plans: int
    order_intents: int
    orders: tuple[tuple[str, int], ...]
    ambiguous_orders: int
    fills: int
    reconciliations: tuple[tuple[str, int], ...]
    incidents: tuple[tuple[str, str, int], ...]
    jobs: tuple[tuple[str, int], ...]
    job_attempts: int
    outbox_events: tuple[tuple[str, int], ...]


class OperationalMetricsReader(Protocol):
    """Read a fresh authoritative snapshot for one Prometheus scrape."""

    def read(self) -> OperationalMetricsSnapshot: ...


@dataclass(frozen=True, slots=True)
class _MetricSources:
    bars: Table
    gaps: Table
    symbol_watermarks: Table
    basket_watermarks: Table
    decision_slots: Table
    signals: Table
    risk_latches: Table
    risk_decisions: Table
    execution_plans: Table
    order_intents: Table
    orders: Table
    fills: Table
    reconciliations: Table
    incidents: Table
    jobs: Table
    job_attempts: Table
    outbox_events: Table
    bars_are_effective_view: bool
    orders_are_joined_view: bool


_view_metadata = MetaData(schema=PLATFORM_SCHEMA)
_effective_bars_view = Table(
    "aqa_effective_bars_v",
    _view_metadata,
    Column("revision", BigInteger, nullable=False),
)
_data_gaps_view = Table(
    "aqa_data_gaps_v",
    _view_metadata,
    Column("status", String(16), nullable=False),
)
_symbol_watermarks_view = Table("aqa_symbol_watermarks_v", _view_metadata)
_basket_watermarks_view = Table(
    "aqa_basket_watermarks_v",
    _view_metadata,
    Column("status", String(16), nullable=False),
)
_decision_slots_view = Table(
    "aqa_decision_slots_v",
    _view_metadata,
    Column("state", String(16), nullable=False),
)
_signals_view = Table("aqa_signals_v", _view_metadata)
_risk_latches_view = Table(
    "aqa_risk_latches_v",
    _view_metadata,
    Column("experiment_hash", String(64), nullable=False),
    Column("latch_type", String(32), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("action", String(16), nullable=False),
)
_risk_decisions_view = Table(
    "aqa_risk_decisions_v",
    _view_metadata,
    Column("execution_scope", String(24), nullable=False),
)
_execution_plans_view = Table("aqa_execution_plans_v", _view_metadata)
_orders_view = Table(
    "aqa_orders_v",
    _view_metadata,
    Column("order_intent_id", String(128), nullable=False),
    Column("state", String(32)),
)
_fills_view = Table("aqa_fills_v", _view_metadata)
_reconciliations_view = Table(
    "aqa_reconciliations_v",
    _view_metadata,
    Column("status", String(16), nullable=False),
)
_incidents_view = Table(
    "aqa_incidents_v",
    _view_metadata,
    Column("status", String(16), nullable=False),
    Column("severity", String(16), nullable=False),
)
_jobs_view = Table(
    "aqa_jobs_v",
    _view_metadata,
    Column("state", String(16), nullable=False),
)

_SQLITE_SOURCES = _MetricSources(
    bars=aqa_bar_events,
    gaps=aqa_data_gaps,
    symbol_watermarks=aqa_symbol_watermarks,
    basket_watermarks=aqa_basket_watermarks,
    decision_slots=aqa_decision_slots,
    signals=aqa_signal_envelopes,
    risk_latches=aqa_risk_latch_events,
    risk_decisions=aqa_risk_decisions,
    execution_plans=aqa_execution_plans,
    order_intents=aqa_order_intents,
    orders=aqa_broker_orders,
    fills=aqa_fills,
    reconciliations=aqa_reconciliations,
    incidents=aqa_incidents,
    jobs=aqa_jobs,
    job_attempts=aqa_job_attempts,
    outbox_events=aqa_outbox_events,
    bars_are_effective_view=False,
    orders_are_joined_view=False,
)
_POSTGRES_SOURCES = _MetricSources(
    bars=_effective_bars_view,
    gaps=_data_gaps_view,
    symbol_watermarks=_symbol_watermarks_view,
    basket_watermarks=_basket_watermarks_view,
    decision_slots=_decision_slots_view,
    signals=_signals_view,
    risk_latches=_risk_latches_view,
    risk_decisions=_risk_decisions_view,
    execution_plans=_execution_plans_view,
    order_intents=_orders_view,
    orders=_orders_view,
    fills=_fills_view,
    reconciliations=_reconciliations_view,
    incidents=_incidents_view,
    jobs=_jobs_view,
    job_attempts=aqa_job_attempts,
    outbox_events=aqa_outbox_events,
    bars_are_effective_view=True,
    orders_are_joined_view=True,
)


class SQLAlchemyOperationalMetricsReader:
    """Derive metrics from durable state without retaining process-local totals."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("operational metrics require a SQLAlchemy engine")
        if engine.dialect.name == "sqlite":
            sources = _SQLITE_SOURCES
        elif engine.dialect.name == "postgresql":
            sources = _POSTGRES_SOURCES
        else:
            raise ValueError("operational metrics require PostgreSQL or SQLite")
        self._engine = engine
        self._sources = sources

    def read(self) -> OperationalMetricsSnapshot:
        """Read one fresh snapshot, failing without leaking database diagnostics."""

        try:
            with self._engine.connect() as raw_connection:
                connection = raw_connection
                if self._engine.dialect.name == "postgresql":
                    connection = raw_connection.execution_options(isolation_level="REPEATABLE READ")
                with connection.begin():
                    return self._read_transaction(connection)
        except OperationalMetricsReadError:
            raise
        except (SQLAlchemyError, TypeError, ValueError, ArithmeticError):
            raise OperationalMetricsReadError(
                "authoritative operational metrics are unavailable"
            ) from None

    def _read_transaction(self, connection: Connection) -> OperationalMetricsSnapshot:
        sources = self._sources
        bar_events, bar_corrections = self._bar_counts(connection)
        order_intents = _count(connection, sources.order_intents)
        orders = self._order_counts(connection, order_intents=order_intents)
        return OperationalMetricsSnapshot(
            bar_events=bar_events,
            bar_corrections=bar_corrections,
            data_gaps=_group_counts(
                connection,
                sources.gaps.c.status,
                allowed=frozenset(DATA_GAP_STATES),
                uppercase=False,
            ),
            symbol_watermarks=_count(connection, sources.symbol_watermarks),
            basket_watermarks=_group_counts(
                connection,
                sources.basket_watermarks.c.status,
                allowed=frozenset(BASKET_WATERMARK_STATES),
                uppercase=False,
            ),
            decision_slots=_group_counts(
                connection,
                sources.decision_slots.c.state,
                allowed=frozenset(state.upper() for state in DECISION_SLOT_STATES),
                uppercase=True,
            ),
            signals=_count(connection, sources.signals),
            risk_decisions=_group_counts(
                connection,
                sources.risk_decisions.c.execution_scope,
                allowed=frozenset(scope.upper() for scope in RISK_EXECUTION_SCOPES),
                uppercase=True,
            ),
            active_latches=self._active_latch_counts(connection),
            execution_plans=_count(connection, sources.execution_plans),
            order_intents=order_intents,
            orders=orders,
            ambiguous_orders=dict(orders)["submission_unknown"],
            fills=_count(connection, sources.fills),
            reconciliations=_group_counts(
                connection,
                sources.reconciliations.c.status,
                allowed=frozenset(state.upper() for state in RECONCILIATION_STATES),
                uppercase=True,
            ),
            incidents=_incident_counts(connection, sources.incidents),
            jobs=_group_counts(
                connection,
                sources.jobs.c.state,
                allowed=frozenset(state.upper() for state in JOB_STATES),
                uppercase=True,
            ),
            job_attempts=_count(connection, sources.job_attempts),
            outbox_events=_group_counts(
                connection,
                sources.outbox_events.c.state,
                allowed=frozenset(state.upper() for state in OUTBOX_STATES),
                uppercase=True,
            ),
        )

    def _bar_counts(self, connection: Connection) -> tuple[int, int]:
        bars = self._sources.bars
        if not self._sources.bars_are_effective_view:
            total = _count(connection, bars)
            corrections = _count(connection, bars, where=bars.c.revision > 1)
            return total, corrections
        latest_revisions = _nonnegative_integer(
            connection.scalar(select(func.coalesce(func.sum(bars.c.revision), 0)))
        )
        identities = _count(connection, bars)
        corrections = latest_revisions - identities
        if corrections < 0:
            raise OperationalMetricsReadError("authoritative bar revisions are invalid")
        return latest_revisions, corrections

    def _active_latch_counts(self, connection: Connection) -> tuple[tuple[str, int], ...]:
        latches = self._sources.risk_latches
        latest = (
            select(
                latches.c.experiment_hash,
                latches.c.latch_type,
                func.max(latches.c.sequence).label("latest_sequence"),
            )
            .group_by(latches.c.experiment_hash, latches.c.latch_type)
            .subquery()
        )
        rows = connection.execute(
            select(latches.c.latch_type, latches.c.action, func.count())
            .select_from(
                latches.join(
                    latest,
                    and_(
                        latches.c.experiment_hash == latest.c.experiment_hash,
                        latches.c.latch_type == latest.c.latch_type,
                        latches.c.sequence == latest.c.latest_sequence,
                    ),
                )
            )
            .group_by(latches.c.latch_type, latches.c.action)
        )
        counts = {latch_type: 0 for latch_type in RISK_LATCH_TYPES}
        for row in rows:
            latch_type = row[0]
            action = row[1]
            count = _nonnegative_integer(row[2])
            if type(latch_type) is not str or latch_type not in counts:
                raise OperationalMetricsReadError("authoritative latch type is invalid")
            if type(action) is not str or action not in _LATCH_ACTIONS:
                raise OperationalMetricsReadError("authoritative latch action is invalid")
            if action == "ENGAGED":
                counts[latch_type] += count
        return tuple(counts.items())

    def _order_counts(
        self,
        connection: Connection,
        *,
        order_intents: int,
    ) -> tuple[tuple[str, int], ...]:
        orders = self._sources.orders
        rows = connection.execute(
            select(orders.c.state, func.count()).select_from(orders).group_by(orders.c.state)
        )
        counts = {state: 0 for state in ORDER_STATES}
        null_count = 0
        broker_order_count = 0
        allowed = frozenset(state.upper() for state in ORDER_STATES if state != "intent_only")
        for row in rows:
            state = row[0]
            count = _nonnegative_integer(row[1])
            if state is None and self._sources.orders_are_joined_view:
                null_count += count
                continue
            if type(state) is not str or state not in allowed:
                raise OperationalMetricsReadError("authoritative order state is invalid")
            counts[state.lower()] += count
            broker_order_count += count
        intent_only = order_intents - broker_order_count
        if intent_only < 0 or (self._sources.orders_are_joined_view and null_count != intent_only):
            raise OperationalMetricsReadError("authoritative order projection is inconsistent")
        counts["intent_only"] = intent_only
        return tuple(counts.items())


def _count(
    connection: Connection,
    table: Table,
    *,
    where: ColumnElement[bool] | None = None,
) -> int:
    statement = select(func.count()).select_from(table)
    if where is not None:
        statement = statement.where(where)
    return _nonnegative_integer(connection.scalar(statement))


def _group_counts(
    connection: Connection,
    column: ColumnElement[Any],
    *,
    allowed: frozenset[str],
    uppercase: bool,
) -> tuple[tuple[str, int], ...]:
    counts = {value.lower(): 0 for value in sorted(allowed)}
    rows = connection.execute(select(column, func.count()).group_by(column))
    for row in rows:
        value = row[0]
        if type(value) is not str or value not in allowed:
            raise OperationalMetricsReadError("authoritative bounded state is invalid")
        normalized = value.lower() if uppercase else value
        counts[normalized] += _nonnegative_integer(row[1])
    return tuple(counts.items())


def _incident_counts(
    connection: Connection,
    incidents: Table,
) -> tuple[tuple[str, str, int], ...]:
    counts = {
        (status, severity): 0 for status in INCIDENT_STATES for severity in INCIDENT_SEVERITIES
    }
    rows = connection.execute(
        select(incidents.c.status, incidents.c.severity, func.count()).group_by(
            incidents.c.status,
            incidents.c.severity,
        )
    )
    for row in rows:
        status = row[0]
        severity = row[1]
        if (
            type(status) is not str
            or status not in INCIDENT_STATES
            or type(severity) is not str
            or severity not in INCIDENT_SEVERITIES
        ):
            raise OperationalMetricsReadError("authoritative incident state is invalid")
        counts[(status.lower(), severity.lower())] += _nonnegative_integer(row[2])
    return tuple((status, severity, count) for (status, severity), count in counts.items())


def _nonnegative_integer(value: object) -> int:
    if type(value) is int:
        result = value
    elif type(value) is Decimal and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        raise OperationalMetricsReadError("authoritative metric count is invalid")
    if result < 0:
        raise OperationalMetricsReadError("authoritative metric count is invalid")
    return result
