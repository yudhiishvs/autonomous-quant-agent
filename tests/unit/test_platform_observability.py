"""Known-answer and adversarial checks for logging, metrics, and health."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

import pytest

from adaptive_trader.platform.jobs import JobState
from adaptive_trader.platform.observability import (
    REDACTED,
    LogSeverity,
    StructuredLogEvent,
    configure_json_logging,
    redact_sensitive,
)
from adaptive_trader.platform.observability.health import HealthService, ReadinessCheck
from adaptive_trader.platform.observability.metrics import (
    DiscrepancySeverity,
    LatchMetricType,
    OrderMetricState,
    PlatformMetrics,
    RiskBlockReason,
    SlotMetricState,
)

_NOW = datetime(2026, 9, 5, 16, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "key",
    [
        "secret",
        "db_password",
        "accessToken",
        "Authorization",
        "broker_credential",
        "api-key",
        "privateKey",
        "session_cookie",
        "connectionString",
        "DATABASE_URL",
    ],
)
def test_central_redaction_recognizes_every_required_sensitive_key(key: str) -> None:
    result = redact_sensitive({key: "TEST_VALUE_MUST_NOT_LEAK", "safe": "visible"})
    assert result == {key: REDACTED, "safe": "visible"}


def test_central_redaction_also_catches_secret_like_values_and_nested_fields() -> None:
    value = {
        "safe": "Bearer TEST_VALUE_MUST_NOT_LEAK",
        "nested": {"database_url": "postgresql://worker:example@database/app"},
    }
    rendered = json.dumps(redact_sensitive(value), sort_keys=True)
    assert "TEST_VALUE_MUST_NOT_LEAK" not in rendered
    assert "worker:example" not in rendered
    assert rendered.count(REDACTED) == 2


def test_structured_logger_emits_only_closed_safe_fields() -> None:
    stream = io.StringIO()
    logger = configure_json_logging(service="control_api", stream=stream)
    logger.emit(
        StructuredLogEvent(
            severity=LogSeverity.INFO,
            event_type="job.created",
            correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ab",
            experiment_id="semiconductor_network_intraday_v1",
            state_transition="PENDING->CLAIMED",
            reason_code="operator_requested",
        )
    )

    payload = json.loads(stream.getvalue())
    assert payload["service"] == "control_api"
    assert payload["event_type"] == "job.created"
    assert payload["severity"] == "INFO"
    assert payload["timestamp"].endswith("Z")
    assert set(payload) == {
        "timestamp",
        "severity",
        "service",
        "event_type",
        "correlation_id",
        "experiment_id",
        "state_transition",
        "reason_code",
    }


def test_unstructured_message_and_raw_exception_are_never_rendered() -> None:
    stream = io.StringIO()
    configure_json_logging(service="control_api", stream=stream)
    raw_logger = logging.getLogger("adaptive_trader.platform.control_api")
    try:
        raise RuntimeError("Bearer TEST_VALUE_MUST_NOT_LEAK")
    except RuntimeError:
        raw_logger.exception("database_url=TEST_VALUE_MUST_NOT_LEAK")

    output = stream.getvalue()
    assert "TEST_VALUE_MUST_NOT_LEAK" not in output
    assert "exception_redacted" in output
    assert "unstructured_log_rejected" in output


def test_structured_event_rejects_secret_like_identifiers_and_invalid_symbols() -> None:
    with pytest.raises(ValueError, match="correlation_id"):
        StructuredLogEvent(
            severity=LogSeverity.INFO,
            event_type="security.rejected",
            correlation_id="Bearer TEST_VALUE_MUST_NOT_LEAK",
        )
    with pytest.raises(ValueError, match="symbol"):
        StructuredLogEvent(
            severity=LogSeverity.INFO,
            event_type="bar.received",
            symbol="../../NVDA",
        )
    for reason_code in ("UPPER_CASE", "contains.dot", "contains/slash"):
        with pytest.raises(ValueError, match="reason_code"):
            StructuredLogEvent(
                severity=LogSeverity.INFO,
                event_type="security.rejected",
                reason_code=reason_code,
            )


def test_formatter_rejects_untrusted_structured_fields_and_values() -> None:
    stream = io.StringIO()
    configure_json_logging(service="control_api", stream=stream)
    raw_logger = logging.getLogger("adaptive_trader.platform.control_api")

    raw_logger.info(
        {
            "severity": "INFO",
            "service": "control_api",
            "event_type": "security.looks_valid",
            "reason_code": "NOT_CLOSED",
            "database_url": "postgresql://user:TEST_VALUE_MUST_NOT_LEAK@database/app",  # pragma: allowlist secret
        }
    )

    output = stream.getvalue()
    assert "TEST_VALUE_MUST_NOT_LEAK" not in output
    payload = json.loads(output)
    assert payload["event_type"] == "security.unstructured_log_rejected"
    assert payload["reason_code"] == "unstructured_message"


def test_root_uvicorn_and_sqlalchemy_logs_share_the_safe_formatter() -> None:
    stream = io.StringIO()
    configure_json_logging(service="control_api", stream=stream)

    for name in ("", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(name).error("Authorization: Bearer TEST_VALUE_MUST_NOT_LEAK")

    output = stream.getvalue()
    assert "TEST_VALUE_MUST_NOT_LEAK" not in output
    payloads = tuple(json.loads(line) for line in output.splitlines())
    assert len(payloads) == 3
    assert all(
        payload["event_type"] == "security.unstructured_log_rejected" for payload in payloads
    )


def test_prometheus_metrics_expose_required_families_with_bounded_labels() -> None:
    metrics = PlatformMetrics()
    metrics.bars_received.inc()
    metrics.bar_duplicates.inc()
    metrics.bar_corrections.inc()
    metrics.invalid_bars.inc()
    metrics.gaps_created.inc()
    metrics.gaps_resolved.inc()
    metrics.watermark_lag.set(12)
    metrics.record_slot(SlotMetricState.READY)
    metrics.record_risk_block(RiskBlockReason.STALE_DATA)
    metrics.set_latch(LatchMetricType.OPERATOR, engaged=True)
    metrics.intents.inc()
    metrics.record_order_state(OrderMetricState.ACCEPTED)
    metrics.ambiguous_submissions.inc()
    metrics.record_discrepancy(DiscrepancySeverity.BLOCKING)
    metrics.record_job_state(JobState.FAILED)
    metrics.record_job_state(JobState.DEAD)
    metrics.record_api_authentication_failure()
    metrics.record_api_rate_limit()
    metrics.record_redaction_failure()
    metrics.record_security_validation_failure()

    output = metrics.render().decode("utf-8")
    for metric_name in (
        "aqa_bars_received_total",
        "aqa_bar_duplicates_total",
        "aqa_bar_corrections_total",
        "aqa_invalid_bars_total",
        "aqa_gaps_created_total",
        "aqa_gaps_resolved_total",
        "aqa_watermark_lag_seconds",
        "aqa_slots_total",
        "aqa_risk_blocks_total",
        "aqa_latch_state",
        "aqa_order_intents_total",
        "aqa_order_states_total",
        "aqa_ambiguous_submissions_total",
        "aqa_reconciliation_discrepancies_total",
        "aqa_job_states_total",
        "aqa_job_retries_total",
        "aqa_dead_jobs_total",
        "aqa_api_authentication_failures_total",
        "aqa_api_rate_limits_total",
        "aqa_redaction_failures_total",
        "aqa_security_validation_failures_total",
    ):
        assert metric_name in output
    assert 'reason="stale_data"' in output
    assert 'state="failed"' in output

    with pytest.raises(TypeError, match="closed enumeration"):
        metrics.record_risk_block("user-controlled-label")  # type: ignore[arg-type]


def test_liveness_is_process_local_and_readiness_fails_closed() -> None:
    def raises() -> bool:
        raise RuntimeError("database_url=TEST_VALUE_MUST_NOT_LEAK")

    health = HealthService(
        clock=lambda: _NOW,
        readiness_checks=(
            ReadinessCheck("database", lambda: True),
            ReadinessCheck("migration", raises),
        ),
    )

    live = health.live()
    ready = health.ready()
    assert live.status == "live"
    assert live.healthy is True
    assert ready.status == "not_ready"
    assert ready.healthy is False
    assert ready.checks == (("database", True), ("migration", False))
    assert "TEST_VALUE_MUST_NOT_LEAK" not in repr(ready)


def test_routine_database_queries_do_not_become_security_error_noise() -> None:
    stream = io.StringIO()
    configure_json_logging(service="control_api", stream=stream)
    logging.getLogger("sqlalchemy.engine").info("SELECT synthetic_query")
    assert stream.getvalue() == ""
    logging.getLogger("sqlalchemy.engine").error("TEST_VALUE_MUST_NOT_LEAK")
    assert "TEST_VALUE_MUST_NOT_LEAK" not in stream.getvalue()
    assert json.loads(stream.getvalue())["event_type"] == "security.unstructured_log_rejected"
