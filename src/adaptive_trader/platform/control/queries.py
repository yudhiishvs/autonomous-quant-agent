"""Read-only control queries against explicit least-privilege PostgreSQL views."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast

from sqlalchemy import Column, Engine, MetaData, Table, literal, select
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.control.models import MAX_PAGE_LIMIT, SafePageResponse
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA


class ControlQueryError(RuntimeError):
    """A safe operational read failed without exposing database details."""


class ReadResource(StrEnum):
    SYSTEM_STATUS = "system_status"
    EXPERIMENT = "experiment"
    DATA_STATUS = "data_status"
    DATA_GAPS = "data_gaps"
    DATASETS = "datasets"
    DECISION_SLOTS = "decision_slots"
    SIGNALS = "signals"
    RISK_DECISIONS = "risk_decisions"
    RISK_LATCHES = "risk_latches"
    ORDERS = "orders"
    FILLS = "fills"
    RECONCILIATIONS = "reconciliations"
    INCIDENTS = "incidents"
    AUDIT_STATUS = "audit_status"


class ControlQueryPort(Protocol):
    def page(self, resource: ReadResource, *, limit: int, offset: int) -> SafePageResponse: ...

    def ready(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ViewSpec:
    relation: Table
    order_columns: tuple[str, ...]


_READ_METADATA = MetaData(schema=PLATFORM_SCHEMA)


def _view(name: str, *columns: str, order_by: tuple[str, ...]) -> _ViewSpec:
    relation = Table(name, _READ_METADATA, *(Column(column_name) for column_name in columns))
    return _ViewSpec(relation=relation, order_columns=order_by)


_VIEW_SPECS = {
    ReadResource.EXPERIMENT: _view(
        "aqa_experiment_context_v",
        "experiment_hash",
        "experiment_id",
        "experiment_version",
        "schema_version",
        "configuration",
        "experiment_content_hash",
        "registered_at",
        "experiment_symbol_id",
        "symbol",
        "role",
        "ordinal",
        "symbol_content_hash",
        order_by=("experiment_version", "role", "ordinal", "symbol"),
    ),
    ReadResource.DATA_STATUS: _view(
        "aqa_basket_watermarks_v",
        "basket_watermark_id",
        "experiment_hash",
        "role",
        "timeframe",
        "status",
        "contiguous_through",
        "component_hash",
        "version",
        "updated_at",
        order_by=("role", "timeframe"),
    ),
    ReadResource.DATA_GAPS: _view(
        "aqa_data_gaps_v",
        "gap_id",
        "experiment_hash",
        "provider",
        "feed",
        "adjustment",
        "symbol",
        "timeframe",
        "gap_start_at",
        "gap_end_at",
        "status",
        "reason_code",
        "attempt_count",
        "detected_at",
        "last_attempt_at",
        "resolved_at",
        "version",
        order_by=("gap_start_at", "symbol", "gap_id"),
    ),
    ReadResource.DATASETS: _view(
        "aqa_datasets_v",
        "dataset_id",
        "artifact_id",
        "experiment_hash",
        "provider",
        "feed",
        "adjustment",
        "timeframe",
        "range_start_at",
        "range_end_at",
        "roles",
        "symbols",
        "row_counts",
        "gap_summary",
        "correction_summary",
        "schema_version",
        "logical_hash",
        "physical_hash",
        "manifest_hash",
        "source_git_commit",
        "dirty_worktree",
        "uv_lock_hash",
        "promotable",
        "status",
        "created_at",
        order_by=("created_at", "dataset_id"),
    ),
    ReadResource.DECISION_SLOTS: _view(
        "aqa_decision_slots_v",
        "slot_id",
        "experiment_hash",
        "experiment_id",
        "experiment_version",
        "signal_provider_id",
        "signal_provider_version",
        "session_date",
        "source_interval_start",
        "source_interval_end",
        "decision_type",
        "ready_at",
        "deadline_at",
        "required_completion_at",
        "state",
        "claim_owner",
        "claimed_at",
        "lease_expires_at",
        "attempt_count",
        "reason_code",
        "completed_at",
        "correlation_id",
        "version",
        "created_at",
        "updated_at",
        order_by=("source_interval_end", "decision_type", "slot_id"),
    ),
    ReadResource.SIGNALS: _view(
        "aqa_signals_v",
        "signal_id",
        "slot_id",
        "contract_version",
        "correlation_id",
        "provider_id",
        "provider_version",
        "provider_source_mode",
        "experiment_id",
        "experiment_version",
        "experiment_hash",
        "data_contract_hash",
        "policy_hash",
        "source_bar_end",
        "created_at",
        "expires_at",
        "active_symbols",
        "availability_mask",
        "actions",
        "expected_edge_bps",
        "proposed_signed_target_inputs",
        "artifact_id",
        "artifact_hash",
        "promotable",
        "paper_submission_eligible",
        "content_hash",
        order_by=("created_at", "signal_id"),
    ),
    ReadResource.RISK_DECISIONS: _view(
        "aqa_risk_decisions_v",
        "risk_decision_id",
        "slot_id",
        "signal_id",
        "experiment_hash",
        "policy_id",
        "policy_version",
        "decided_at",
        "approved_targets",
        "controls",
        "reason_codes",
        "gross_exposure",
        "net_exposure",
        "cash_weight",
        "content_hash",
        order_by=("decided_at", "risk_decision_id"),
    ),
    ReadResource.RISK_LATCHES: _view(
        "aqa_risk_latches_v",
        "latch_event_id",
        "experiment_hash",
        "latch_type",
        "sequence",
        "action",
        "reason_code",
        "actor",
        "occurred_at",
        "content_hash",
        order_by=("occurred_at", "latch_event_id"),
    ),
    ReadResource.ORDERS: _view(
        "aqa_orders_v",
        "order_intent_id",
        "execution_plan_id",
        "risk_decision_id",
        "experiment_hash",
        "correlation_id",
        "client_order_id",
        "symbol",
        "side",
        "effect",
        "phase",
        "sequence",
        "target_version",
        "quantity",
        "notional",
        "reference_price",
        "final_target_quantity",
        "forced_flat",
        "created_at",
        "deadline_at",
        "broker_order_id",
        "state",
        "submitted_at",
        "accepted_at",
        "updated_at",
        "cumulative_filled_quantity",
        "average_fill_price",
        "last_event_sequence",
        "safe_error_code",
        "version",
        order_by=("created_at", "client_order_id"),
    ),
    ReadResource.FILLS: _view(
        "aqa_fills_v",
        "fill_id",
        "client_order_id",
        "broker_execution_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "fee",
        "occurred_at",
        "content_hash",
        order_by=("occurred_at", "fill_id"),
    ),
    ReadResource.RECONCILIATIONS: _view(
        "aqa_reconciliations_v",
        "reconciliation_id",
        "experiment_hash",
        "slot_id",
        "execution_plan_id",
        "correlation_id",
        "account_id_hash",
        "started_at",
        "completed_at",
        "status",
        "expected_positions",
        "observed_positions",
        "expected_cash",
        "observed_cash",
        "expected_equity",
        "observed_equity",
        "fill_hashes",
        "order_hashes",
        "discrepancies",
        "content_hash",
        order_by=("completed_at", "reconciliation_id"),
    ),
    ReadResource.INCIDENTS: _view(
        "aqa_incidents_v",
        "incident_id",
        "idempotency_key",
        "experiment_hash",
        "incident_type",
        "severity",
        "status",
        "reason_code",
        "opened_at",
        "resolved_at",
        "version",
        order_by=("opened_at", "incident_id"),
    ),
    ReadResource.AUDIT_STATUS: _view(
        "aqa_audit_status_v",
        "stream_id",
        "sequence",
        "event_hash",
        "event_type",
        "actor",
        "occurred_at",
        order_by=("stream_id",),
    ),
}


class SQLAlchemyControlQueryService:
    """Query explicit safe views through a read-only control-role engine."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("control queries require a concrete SQLAlchemy Engine")
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("control queries require PostgreSQL or SQLite")
        self._engine = engine

    def ready(self) -> bool:
        try:
            with self._engine.connect() as connection:
                return connection.scalar(select(literal(1))) == 1
        except SQLAlchemyError:
            return False

    def page(self, resource: ReadResource, *, limit: int, offset: int) -> SafePageResponse:
        if type(resource) is not ReadResource:
            raise TypeError("control resource must use the closed contract")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError("control page limit is invalid")
        if type(offset) is not int or offset < 0:
            raise ValueError("control page offset is invalid")
        if resource is ReadResource.SYSTEM_STATUS:
            return self._system_status(limit=limit, offset=offset)
        spec = _VIEW_SPECS[resource]
        relation = spec.relation
        try:
            statement = select(relation)
            statement = statement.order_by(*(relation.c[name] for name in spec.order_columns))
            statement = statement.limit(limit).offset(offset)
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings()
                items = tuple(
                    {str(column.name): _safe_json(row[column.name]) for column in relation.columns}
                    for row in rows
                )
            return SafePageResponse(items=items, limit=limit, offset=offset, count=len(items))
        except (KeyError, TypeError, ValueError):
            raise ControlQueryError("safe operational state is malformed") from None
        except SQLAlchemyError:
            raise ControlQueryError("safe operational state is unavailable") from None

    def _system_status(self, *, limit: int, offset: int) -> SafePageResponse:
        if offset > 0:
            return SafePageResponse(items=(), limit=limit, offset=offset, count=0)
        try:
            with self._engine.connect() as connection:
                reachable = connection.scalar(select(literal(1))) == 1
        except SQLAlchemyError:
            raise ControlQueryError("safe operational state is unavailable") from None
        item: dict[str, JsonValue] = {
            "service": "control_api",
            "database": "reachable" if reachable else "unreachable",
            "broker_authority": "none",
        }
        return SafePageResponse(items=(item,), limit=limit, offset=offset, count=1)


def _safe_json(value: object) -> JsonValue:
    if value is None or type(value) in {bool, int, float, str}:
        return cast(JsonValue, value)
    if type(value) is Decimal:
        return format(value, "f")
    if type(value) is datetime:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if type(value) is date:
        return value.isoformat()
    if type(value) in {dict, list, tuple}:
        return cast(JsonValue, json.loads(canonical_json_bytes(value)))
    raise ValueError("safe view returned an unsupported value")
