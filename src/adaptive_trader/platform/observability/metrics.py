"""Bounded-cardinality Prometheus metrics for platform control and workers."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from adaptive_trader.platform.jobs.models import JobState
from adaptive_trader.platform.observability.operational import (
    BASKET_WATERMARK_STATES,
    DATA_GAP_STATES,
    DECISION_SLOT_STATES,
    INCIDENT_SEVERITIES,
    INCIDENT_STATES,
    JOB_STATES,
    ORDER_STATES,
    OUTBOX_STATES,
    RECONCILIATION_STATES,
    RISK_EXECUTION_SCOPES,
    RISK_LATCH_TYPES,
    OperationalMetricsReader,
)


class SlotMetricState(StrEnum):
    READY = "ready"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class RiskBlockReason(StrEnum):
    INVALID_SIGNAL = "invalid_signal"
    STALE_DATA = "stale_data"
    SECURITY_INELIGIBLE = "security_ineligible"
    POLICY_CONSTRAINT = "policy_constraint"
    LATCH_ACTIVE = "latch_active"
    STATE_UNAVAILABLE = "state_unavailable"


class LatchMetricType(StrEnum):
    OPERATOR = "operator"
    SESSION_LOSS = "session_loss"
    DRAWDOWN = "drawdown"
    RECONCILIATION = "reconciliation"


class OrderMetricState(StrEnum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class DiscrepancySeverity(StrEnum):
    NONBLOCKING = "nonblocking"
    BLOCKING = "blocking"
    CRITICAL = "critical"


class PlatformMetrics:
    """Metric facade whose only labels are values from closed enumerations."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        authoritative_reader: OperationalMetricsReader | None = None,
    ) -> None:
        self.registry = CollectorRegistry(auto_describe=True) if registry is None else registry
        self.bars_received = Counter(
            "aqa_bars_received_total", "Canonical bars received", registry=self.registry
        )
        self.bar_duplicates = Counter(
            "aqa_bar_duplicates_total", "Duplicate bars observed", registry=self.registry
        )
        self.bar_corrections = Counter(
            "aqa_bar_corrections_total", "Bar corrections persisted", registry=self.registry
        )
        self.invalid_bars = Counter(
            "aqa_invalid_bars_total", "Invalid bars rejected", registry=self.registry
        )
        self.gaps_created = Counter(
            "aqa_gaps_created_total", "Data gaps created", registry=self.registry
        )
        self.gaps_resolved = Counter(
            "aqa_gaps_resolved_total", "Data gaps resolved", registry=self.registry
        )
        self.watermark_lag = Gauge(
            "aqa_watermark_lag_seconds", "Active basket watermark lag", registry=self.registry
        )
        self.slots = Counter(
            "aqa_slots_total",
            "Decision slot transitions",
            ("state",),
            registry=self.registry,
        )
        self.risk_blocks = Counter(
            "aqa_risk_blocks_total",
            "Risk blocks by bounded reason",
            ("reason",),
            registry=self.registry,
        )
        self.latch_state = Gauge(
            "aqa_latch_state",
            "Current latch state where one means engaged",
            ("latch_type",),
            registry=self.registry,
        )
        self.intents = Counter(
            "aqa_order_intents_total", "Order intents persisted", registry=self.registry
        )
        self.order_states = Counter(
            "aqa_order_states_total",
            "Order state transitions",
            ("state",),
            registry=self.registry,
        )
        self.ambiguous_submissions = Counter(
            "aqa_ambiguous_submissions_total",
            "Ambiguous broker submissions",
            registry=self.registry,
        )
        self.reconciliation_discrepancies = Counter(
            "aqa_reconciliation_discrepancies_total",
            "Reconciliation discrepancies",
            ("severity",),
            registry=self.registry,
        )
        self.job_states = Counter(
            "aqa_job_states_total",
            "Job state transitions",
            ("state",),
            registry=self.registry,
        )
        self.job_retries = Counter(
            "aqa_job_retries_total", "Job retries scheduled", registry=self.registry
        )
        self.dead_jobs = Counter("aqa_dead_jobs_total", "Jobs made dead", registry=self.registry)
        self.api_authentication_failures = Counter(
            "aqa_api_authentication_failures_total",
            "API authentication failures",
            registry=self.registry,
        )
        self.api_rate_limits = Counter(
            "aqa_api_rate_limits_total", "API rate limit rejections", registry=self.registry
        )
        self.redaction_failures = Counter(
            "aqa_redaction_failures_total",
            "Log redaction validation failures",
            registry=self.registry,
        )
        self.security_validation_failures = Counter(
            "aqa_security_validation_failures_total",
            "Security validation failures",
            registry=self.registry,
        )
        if authoritative_reader is not None:
            if not callable(getattr(authoritative_reader, "read", None)):
                raise TypeError("authoritative metrics reader must provide read")
            self.registry.register(_AuthoritativeMetricsCollector(authoritative_reader))

    def record_slot(self, state: SlotMetricState) -> None:
        self.slots.labels(state=_enum_value(state, SlotMetricState)).inc()

    def record_risk_block(self, reason: RiskBlockReason) -> None:
        self.risk_blocks.labels(reason=_enum_value(reason, RiskBlockReason)).inc()

    def set_latch(self, latch_type: LatchMetricType, *, engaged: bool) -> None:
        if type(engaged) is not bool:
            raise TypeError("metric latch state must be boolean")
        self.latch_state.labels(latch_type=_enum_value(latch_type, LatchMetricType)).set(
            1 if engaged else 0
        )

    def record_order_state(self, state: OrderMetricState) -> None:
        self.order_states.labels(state=_enum_value(state, OrderMetricState)).inc()

    def record_discrepancy(self, severity: DiscrepancySeverity) -> None:
        self.reconciliation_discrepancies.labels(
            severity=_enum_value(severity, DiscrepancySeverity)
        ).inc()

    def record_job_state(self, state: JobState) -> None:
        self.job_states.labels(state=_enum_value(state, JobState).lower()).inc()
        if state is JobState.FAILED:
            self.job_retries.inc()
        elif state is JobState.DEAD:
            self.dead_jobs.inc()

    def record_api_authentication_failure(self) -> None:
        self.api_authentication_failures.inc()

    def record_api_rate_limit(self) -> None:
        self.api_rate_limits.inc()

    def record_redaction_failure(self) -> None:
        self.redaction_failures.inc()

    def record_security_validation_failure(self) -> None:
        self.security_validation_failures.inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)


def _enum_value(value: object, expected: type[StrEnum]) -> str:
    if type(value) is not expected:
        raise TypeError("metric label must use the closed enumeration")
    return str(value.value)


class _AuthoritativeMetricsCollector:
    """Render a new durable-state snapshot for every Prometheus collection."""

    def __init__(self, reader: OperationalMetricsReader) -> None:
        self._reader = reader

    def describe(self) -> Iterable[Metric]:
        # Registration must never perform database I/O.
        return ()

    def collect(self) -> Iterable[Metric]:
        available = GaugeMetricFamily(
            "aqa_authoritative_metrics_up",
            "Whether the authoritative operational snapshot was read successfully",
        )
        try:
            snapshot = self._reader.read()
        except Exception:
            # The scrape remains safe and diagnostic without exposing database exceptions or
            # publishing misleading zeroes for state that could not be read.
            available.add_metric([], 0)
            yield available
            return

        available.add_metric([], 1)
        yield available
        yield _counter_family(
            "aqa_persisted_bar_events_total",
            "Persisted canonical bar revisions",
            snapshot.bar_events,
        )
        yield _counter_family(
            "aqa_persisted_bar_corrections_total",
            "Persisted canonical bar corrections",
            snapshot.bar_corrections,
        )
        yield _state_gauge(
            "aqa_persisted_data_gaps",
            "Current persisted data gaps",
            "state",
            DATA_GAP_STATES,
            snapshot.data_gaps,
        )
        yield _gauge_family(
            "aqa_persisted_symbol_watermarks",
            "Current persisted symbol watermarks",
            snapshot.symbol_watermarks,
        )
        yield _state_gauge(
            "aqa_persisted_basket_watermarks",
            "Current persisted basket watermarks",
            "state",
            BASKET_WATERMARK_STATES,
            snapshot.basket_watermarks,
        )
        yield _state_gauge(
            "aqa_persisted_decision_slots",
            "Current persisted decision slots",
            "state",
            DECISION_SLOT_STATES,
            snapshot.decision_slots,
        )
        yield _counter_family(
            "aqa_persisted_signals_total",
            "Persisted signal envelopes",
            snapshot.signals,
        )
        yield _state_counter(
            "aqa_persisted_risk_decisions_total",
            "Persisted risk decisions",
            "execution_scope",
            RISK_EXECUTION_SCOPES,
            snapshot.risk_decisions,
        )
        yield _state_gauge(
            "aqa_persisted_active_latches",
            "Currently engaged persisted risk latches",
            "latch_type",
            RISK_LATCH_TYPES,
            snapshot.active_latches,
        )
        yield _counter_family(
            "aqa_persisted_execution_plans_total",
            "Persisted execution plans",
            snapshot.execution_plans,
        )
        yield _counter_family(
            "aqa_persisted_order_intents_total",
            "Persisted order intents",
            snapshot.order_intents,
        )
        yield _state_gauge(
            "aqa_persisted_orders",
            "Current persisted orders",
            "state",
            ORDER_STATES,
            snapshot.orders,
        )
        yield _gauge_family(
            "aqa_persisted_ambiguous_orders",
            "Current persisted orders with ambiguous broker submission state",
            snapshot.ambiguous_orders,
        )
        yield _counter_family(
            "aqa_persisted_fills_total",
            "Persisted fills",
            snapshot.fills,
        )
        yield _state_gauge(
            "aqa_persisted_reconciliations",
            "Persisted reconciliation results",
            "state",
            RECONCILIATION_STATES,
            snapshot.reconciliations,
        )
        incidents = GaugeMetricFamily(
            "aqa_persisted_incidents",
            "Current persisted incidents",
            labels=["state", "severity"],
        )
        incident_counts = {
            (state, severity): count for state, severity, count in snapshot.incidents
        }
        for state in INCIDENT_STATES:
            for severity in INCIDENT_SEVERITIES:
                incidents.add_metric(
                    [state, severity],
                    incident_counts[(state, severity)],
                )
        yield incidents
        yield _state_gauge(
            "aqa_persisted_jobs",
            "Current persisted durable jobs",
            "state",
            JOB_STATES,
            snapshot.jobs,
        )
        yield _counter_family(
            "aqa_persisted_job_attempts_total",
            "Persisted durable job attempt transitions",
            snapshot.job_attempts,
        )
        yield _state_gauge(
            "aqa_persisted_outbox_events",
            "Current persisted outbox events",
            "state",
            OUTBOX_STATES,
            snapshot.outbox_events,
        )


def _counter_family(name: str, documentation: str, value: int) -> CounterMetricFamily:
    family = CounterMetricFamily(name, documentation)
    family.add_metric([], value)
    return family


def _gauge_family(name: str, documentation: str, value: int) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, documentation)
    family.add_metric([], value)
    return family


def _state_counter(
    name: str,
    documentation: str,
    label: str,
    states: tuple[str, ...],
    values: tuple[tuple[str, int], ...],
) -> CounterMetricFamily:
    family = CounterMetricFamily(name, documentation, labels=[label])
    counts = dict(values)
    for state in states:
        family.add_metric([state], counts[state])
    return family


def _state_gauge(
    name: str,
    documentation: str,
    label: str,
    states: tuple[str, ...],
    values: tuple[tuple[str, int], ...],
) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, documentation, labels=[label])
    counts = dict(values)
    for state in states:
        family.add_metric([state], counts[state])
    return family
