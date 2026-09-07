"""Authentication, authorization, request-boundary, and route tests for the control API."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select

from adaptive_trader.platform.control import (
    OperatorControlResult,
    OperatorRateLimiter,
    OperatorScope,
    OperatorTokenAuthenticator,
    ReadResource,
    create_control_app,
    derive_dashboard_read_token,
)
from adaptive_trader.platform.control.models import (
    OPERATOR_RESUME_ACKNOWLEDGEMENT,
    OperatorResumeRequest,
    SafePageResponse,
)
from adaptive_trader.platform.control.queries import ControlQueryError
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.jobs import JobRepository
from adaptive_trader.platform.jobs.repository import initialized_sqlite_job_schema
from adaptive_trader.platform.jobs.schema import aqa_jobs_contract
from adaptive_trader.platform.observability.health import HealthService, ReadinessCheck
from adaptive_trader.platform.observability.metrics import PlatformMetrics
from adaptive_trader.platform.risk.latches import RiskLatchKind
from adaptive_trader.platform.security import RedactedSecret, SecretFileVariable, load_secret_file
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA

_NOW = datetime(2026, 9, 5, 17, 0, tzinfo=UTC)
_TOKEN = "TEST_AQA_OPERATOR_TOKEN_DO_NOT_LEAK"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_CORRELATION_ID = "0198fa2d-7b8c-7123-8abc-0123456789ab"
_EXPERIMENT_HASH = "a" * 64


class Queries:
    def __init__(self, *, ready: bool = True, fail: bool = False) -> None:
        self.is_ready = ready
        self.fail = fail
        self.calls: list[tuple[ReadResource, int, int]] = []

    def ready(self) -> bool:
        return self.is_ready

    def page(self, resource: ReadResource, *, limit: int, offset: int) -> SafePageResponse:
        if self.fail:
            raise ControlQueryError("safe operational state is unavailable")
        self.calls.append((resource, limit, offset))
        return SafePageResponse(
            items=({"resource": resource.value, "broker_authority": "none"},),
            limit=limit,
            offset=offset,
            count=1,
        )


@dataclass
class Controls:
    halts: int = 0
    resumes: int = 0

    def halt(self, *, request: object, occurred_at: datetime) -> OperatorControlResult:
        del request, occurred_at
        self.halts += 1
        return OperatorControlResult("latch_" + "a" * 64, "engaged", "operator_requested")

    def resume(self, *, request: object, occurred_at: datetime) -> OperatorControlResult:
        del occurred_at
        self.resumes += 1
        if type(request) is not OperatorResumeRequest:
            raise TypeError("test control received the wrong request contract")
        latch_type = RiskLatchKind(request.latch_type)
        return OperatorControlResult(
            "latch_" + "b" * 64,
            "cleared",
            "operator_acknowledged",
            latch_type,
        )


def _engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure(connection: object, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()

    with initialized_sqlite_job_schema(engine):
        pass
    return engine


def _authenticator(tmp_path: Path, token: str = _TOKEN) -> OperatorTokenAuthenticator:
    path = tmp_path / "operator-token"
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return OperatorTokenAuthenticator(
        load_secret_file(path, source=SecretFileVariable.OPERATOR_TOKEN)
    )


def _operator_secret(tmp_path: Path, token: str = _TOKEN) -> RedactedSecret:
    path = tmp_path / "scoped-operator-token"
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return load_secret_file(path, source=SecretFileVariable.OPERATOR_TOKEN)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, Engine, Controls, PlatformMetrics, Queries]]:
    engine = _engine(tmp_path / "api.sqlite3")
    jobs = JobRepository(engine, audit=AuditRepository(engine, writer=AuditWriter.CONTROL))
    controls = Controls()
    metrics = PlatformMetrics()
    queries = Queries()
    app = create_control_app(
        authenticator=_authenticator(tmp_path),
        rate_limiter=OperatorRateLimiter(monotonic=lambda: 1.0),
        jobs=jobs,
        queries=queries,
        operator_controls=controls,
        health=HealthService(
            clock=lambda: _NOW,
            readiness_checks=(ReadinessCheck("database", queries.ready),),
        ),
        metrics=metrics,
        clock=lambda: _NOW,
        correlation_id_factory=lambda: _CORRELATION_ID,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, engine, controls, metrics, queries
    engine.dispose()


def _job_body(*, key: str = "request-1", correlation_id: str = _CORRELATION_ID) -> dict[str, str]:
    return {
        "idempotency_key": key,
        "correlation_id": correlation_id,
        "experiment_hash": _EXPERIMENT_HASH,
    }


def test_health_is_unauthenticated_while_every_control_route_requires_bearer_token(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, _, _, _ = api
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200

    for path in (
        "/v1/system/status",
        "/v1/experiment",
        "/v1/data/status",
        "/v1/data/gaps",
        "/v1/datasets",
        "/v1/decision-slots",
        "/v1/signals",
        "/v1/risk-decisions",
        "/v1/risk/latches",
        "/v1/orders",
        "/v1/fills",
        "/v1/reconciliations",
        "/v1/incidents",
        "/v1/jobs/job_missing",
        "/v1/audit/status",
        "/metrics",
    ):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "authentication_required"
        assert "WWW-Authenticate" in response.headers


def test_exact_authenticated_read_routes_are_paginated_and_security_headered(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, _, _, queries = api
    paths = {
        "/v1/system/status",
        "/v1/experiment",
        "/v1/data/status",
        "/v1/data/gaps",
        "/v1/datasets",
        "/v1/decision-slots",
        "/v1/signals",
        "/v1/risk-decisions",
        "/v1/risk/latches",
        "/v1/orders",
        "/v1/fills",
        "/v1/reconciliations",
        "/v1/incidents",
        "/v1/audit/status",
    }
    for path in paths:
        response = client.get(f"{path}?limit=1&offset=2", headers=_AUTH)
        assert response.status_code == 200, path
        assert response.json()["limit"] == 1
        assert response.json()["offset"] == 2
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-correlation-id"] == _CORRELATION_ID
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert len(queries.calls) == len(paths)


def test_dashboard_read_token_is_deterministic_and_scope_bound(tmp_path: Path) -> None:
    operator_secret = _operator_secret(tmp_path)
    derived = derive_dashboard_read_token(operator_secret)
    authenticator = OperatorTokenAuthenticator(operator_secret)
    operator = authenticator.authenticate(f"Bearer {_TOKEN}")
    read_only = authenticator.authenticate(f"Bearer {derived}")

    assert len(derived) == 64
    assert derived == derive_dashboard_read_token(operator_secret)
    assert derived != _TOKEN
    assert all(character in "0123456789abcdef" for character in derived)
    assert operator is not None
    assert operator.scope is OperatorScope.OPERATOR
    assert read_only is not None
    assert read_only.scope is OperatorScope.READ_ONLY


def test_dashboard_read_token_can_read_but_every_mutation_is_forbidden(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "read-only-api.sqlite3")
    jobs = JobRepository(engine, audit=AuditRepository(engine, writer=AuditWriter.CONTROL))
    queries = Queries()
    controls = Controls()
    operator_secret = _operator_secret(tmp_path)
    read_headers = {"Authorization": f"Bearer {derive_dashboard_read_token(operator_secret)}"}
    app = create_control_app(
        authenticator=OperatorTokenAuthenticator(operator_secret),
        rate_limiter=OperatorRateLimiter(monotonic=lambda: 1.0),
        jobs=jobs,
        queries=queries,
        operator_controls=controls,
        health=HealthService(clock=lambda: _NOW, readiness_checks=()),
        metrics=PlatformMetrics(),
        clock=lambda: _NOW,
        correlation_id_factory=lambda: _CORRELATION_ID,
    )
    read_paths = (
        "/v1/system/status",
        "/v1/experiment",
        "/v1/data/status",
        "/v1/data/gaps",
        "/v1/datasets",
        "/v1/decision-slots",
        "/v1/signals",
        "/v1/risk-decisions",
        "/v1/risk/latches",
        "/v1/orders",
        "/v1/fills",
        "/v1/reconciliations",
        "/v1/incidents",
        "/v1/audit/status",
        "/metrics",
    )
    metadata = {"idempotency_key": "dashboard-forbidden", "correlation_id": _CORRELATION_ID}
    mutations = (
        ("/v1/jobs/data-quality-audit", {**metadata, "experiment_hash": _EXPERIMENT_HASH}),
        ("/v1/jobs/gap-repair", {**metadata, "gap_id": "gap-1"}),
        ("/v1/jobs/dataset-freeze", {**metadata, "experiment_hash": _EXPERIMENT_HASH}),
        ("/v1/jobs/offline-demo", {**metadata, "demo_id": "fixture-1"}),
        ("/v1/risk/halt", metadata),
        (
            "/v1/risk/resume",
            {**metadata, "acknowledgement": OPERATOR_RESUME_ACKNOWLEDGEMENT},
        ),
    )

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for path in read_paths:
                assert client.get(path, headers=read_headers).status_code == 200, path
            for path, body in mutations:
                response = client.post(path, headers=read_headers, json=body)
                assert response.status_code == 403, path
                assert response.json()["error"]["code"] == "insufficient_scope"
                assert response.headers["cache-control"] == "no-store"
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(aqa_jobs_contract)) == 0
        assert controls.halts == controls.resumes == 0
    finally:
        engine.dispose()


def test_docs_cors_and_direct_order_mutations_do_not_exist(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, _, _, _ = api
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    for path in (
        "/v1/orders/submit",
        "/v1/orders/cancel",
        "/v1/orders/replace",
        "/v1/orders/liquidate",
        "/v1/orders/flatten",
    ):
        assert client.post(path, headers=_AUTH, json={}).status_code == 404
    response = client.options(
        "/v1/system/status",
        headers={"Origin": "https://example.invalid", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_job_creation_is_idempotent_and_job_status_omits_routing_payload(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, engine, _, metrics, _ = api
    created = client.post("/v1/jobs/data-quality-audit", headers=_AUTH, json=_job_body())
    repeated = client.post("/v1/jobs/data-quality-audit", headers=_AUTH, json=_job_body())

    assert created.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json() == created.json()
    assert "payload" not in created.json()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_jobs_contract)) == 1
    loaded = client.get(f"/v1/jobs/{created.json()['job_id']}", headers=_AUTH)
    assert loaded.status_code == 200
    assert loaded.json() == created.json()
    assert 'aqa_job_states_total{state="pending"}' not in metrics.render().decode("utf-8")


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://localhost",
        "http://169.254.169.254",
        "http://[::1]",
    ),
)
def test_public_mutations_reject_every_url_and_unknown_field(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
    url: str,
) -> None:
    client, engine, _, _, _ = api
    response = client.post(
        "/v1/jobs/data-quality-audit",
        headers=_AUTH,
        json={**_job_body(), "url": url},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert url not in response.text
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_jobs_contract)) == 0


def test_request_size_page_limits_and_malformed_auth_fail_closed(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, _, _, _ = api
    oversized = client.post(
        "/v1/jobs/offline-demo",
        headers=_AUTH,
        content=b"x" * 65_537,
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"
    assert client.get("/v1/data/gaps?limit=101", headers=_AUTH).status_code == 422
    assert client.get("/v1/data/gaps?offset=-1", headers=_AUTH).status_code == 422
    for authorization in ("", "bearer " + _TOKEN, "Bearer short", "Basic " + _TOKEN):
        assert (
            client.get("/v1/system/status", headers={"Authorization": authorization}).status_code
            == 401
        )


def test_read_and_mutation_rate_limits_are_independent_and_exact(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "rate-api.sqlite3")
    jobs = JobRepository(engine, audit=AuditRepository(engine, writer=AuditWriter.CONTROL))
    queries = Queries()
    controls = Controls()
    app = create_control_app(
        authenticator=_authenticator(tmp_path),
        rate_limiter=OperatorRateLimiter(monotonic=lambda: 10.0),
        jobs=jobs,
        queries=queries,
        operator_controls=controls,
        health=HealthService(clock=lambda: _NOW, readiness_checks=()),
        metrics=PlatformMetrics(),
        clock=lambda: _NOW,
        correlation_id_factory=lambda: _CORRELATION_ID,
    )
    with TestClient(app) as client:
        assert all(
            client.get("/v1/system/status", headers=_AUTH).status_code == 200 for _ in range(120)
        )
        assert client.get("/v1/system/status", headers=_AUTH).status_code == 429
        assert client.get("/health/live").status_code == 200

        for index in range(10):
            body = _job_body(
                key=f"request-{index}",
                correlation_id=f"00000000-0000-4000-8000-{index:012d}",
            )
            assert (
                client.post("/v1/jobs/data-quality-audit", headers=_AUTH, json=body).status_code
                == 202
            )
        response = client.post(
            "/v1/jobs/data-quality-audit",
            headers=_AUTH,
            json=_job_body(
                key="request-11",
                correlation_id="00000000-0000-4000-8000-000000000011",
            ),
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert response.headers["cache-control"] == "no-store"
    engine.dispose()


def test_operator_resume_requires_exact_acknowledgement_and_routes_never_execute_orders(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, controls, _, _ = api
    base = {"idempotency_key": "operator-1", "correlation_id": _CORRELATION_ID}
    halt = client.post("/v1/risk/halt", headers=_AUTH, json=base)
    rejected = client.post(
        "/v1/risk/resume",
        headers=_AUTH,
        json={**base, "acknowledgement": "yes"},
    )
    resumed = client.post(
        "/v1/risk/resume",
        headers=_AUTH,
        json={**base, "acknowledgement": OPERATOR_RESUME_ACKNOWLEDGEMENT},
    )

    assert halt.status_code == 200
    assert halt.json()["state"] == "engaged"
    assert halt.json()["latch_type"] == "operator_halt"
    assert rejected.status_code == 422
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "cleared"
    assert resumed.json()["latch_type"] == "operator_halt"
    assert controls.halts == controls.resumes == 1


@pytest.mark.parametrize("latch_type", ("session_loss", "deployment_drawdown"))
def test_resume_route_clears_only_closed_financial_latch_kinds(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
    latch_type: str,
) -> None:
    client, _, _, metrics, _ = api
    response = client.post(
        "/v1/risk/resume",
        headers=_AUTH,
        json={
            "idempotency_key": f"resume-{latch_type}",
            "correlation_id": _CORRELATION_ID,
            "acknowledgement": OPERATOR_RESUME_ACKNOWLEDGEMENT,
            "latch_type": latch_type,
        },
    )

    assert response.status_code == 200
    assert response.json()["latch_type"] == latch_type
    metric_type = "session_loss" if latch_type == "session_loss" else "drawdown"
    assert f'aqa_latch_state{{latch_type="{metric_type}"}} 0.0' in metrics.render().decode("utf-8")


def test_resume_route_rejects_system_owned_reconciliation_latch(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, controls, _, _ = api
    response = client.post(
        "/v1/risk/resume",
        headers=_AUTH,
        json={
            "idempotency_key": "resume-reconciliation",
            "correlation_id": _CORRELATION_ID,
            "acknowledgement": OPERATOR_RESUME_ACKNOWLEDGEMENT,
            "latch_type": "reconciliation",
        },
    )

    assert response.status_code == 422
    assert controls.resumes == 0


def test_readiness_and_query_failures_are_safe_and_do_not_affect_liveness(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "failure-api.sqlite3")
    jobs = JobRepository(engine, audit=AuditRepository(engine, writer=AuditWriter.CONTROL))
    queries = Queries(ready=False, fail=True)
    app = create_control_app(
        authenticator=_authenticator(tmp_path),
        rate_limiter=OperatorRateLimiter(monotonic=lambda: 1.0),
        jobs=jobs,
        queries=queries,
        operator_controls=Controls(),
        health=HealthService(
            clock=lambda: _NOW,
            readiness_checks=(ReadinessCheck("database", queries.ready),),
        ),
        metrics=PlatformMetrics(),
        clock=lambda: _NOW,
        correlation_id_factory=lambda: _CORRELATION_ID,
    )
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        response = client.get("/v1/orders", headers=_AUTH)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "state_unavailable"
        assert "database" not in response.text.lower()
    engine.dispose()


def test_metrics_endpoint_is_authenticated_and_contains_no_user_labels(
    api: tuple[TestClient, Engine, Controls, PlatformMetrics, Queries],
) -> None:
    client, _, _, _, _ = api
    assert client.get("/metrics").status_code == 401
    invalid = client.get("/v1/data/gaps?limit=101", headers=_AUTH)
    assert invalid.status_code == 422
    response = client.get("/metrics", headers=_AUTH)
    assert response.status_code == 200
    assert "aqa_api_authentication_failures_total" in response.text
    assert "aqa_api_authentication_failures_total 1.0" in response.text
    assert "aqa_security_validation_failures_total 1.0" in response.text
    assert _TOKEN not in response.text


def test_short_operator_token_is_rejected_at_startup(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="32 to 4096"):
        _authenticator(tmp_path, token="too-short")
