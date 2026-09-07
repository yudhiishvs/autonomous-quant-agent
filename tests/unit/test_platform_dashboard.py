"""Read-only dashboard API boundary tests."""

from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from urllib.request import HTTPHandler, Request

import pytest

from adaptive_trader.platform.dashboard.client import (
    _JOB_FIELDS,
    _PAGE_FIELDS,
    DashboardApiClient,
    DashboardApiError,
    DashboardRoute,
    _NoRedirectHandler,
)
from adaptive_trader.platform.security import (
    RedactedSecret,
    SecretFileReference,
    SecretFileVariable,
)


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.code = status
        self.msg = "OK"
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._payload = payload

    def read(self, amount: int = -1) -> bytes:
        return self._payload if amount < 0 else self._payload[:amount]

    def info(self) -> Message:
        return self.headers

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args


@pytest.fixture
def token(tmp_path: Path) -> RedactedSecret:
    token_path = tmp_path / "operator_token"
    token_path.write_text("T" * 32, encoding="ascii")
    token_path.chmod(0o600)
    return SecretFileReference.from_path(
        token_path,
        source=SecretFileVariable.OPERATOR_TOKEN,
        application_root=tmp_path,
    ).load()


def test_dashboard_client_uses_only_enumerated_get_routes(token: RedactedSecret) -> None:
    captured: list[tuple[Request, float]] = []

    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        captured.append((request, timeout))
        return _FakeResponse(b'{"count":0,"items":[],"limit":50,"offset":0}')

    client = DashboardApiClient(
        base_url="http://control-api:8000",
        token=token,
        opener=opener,
    )
    assert client.get(DashboardRoute.ORDERS) == {
        "count": 0,
        "items": [],
        "limit": 50,
        "offset": 0,
    }

    request, timeout = captured[0]
    assert request.full_url == "http://control-api:8000/v1/orders"
    assert request.method == "GET"
    assert request.get_header("Authorization") == f"Bearer {'T' * 32}"
    assert timeout == 5.0


def test_dashboard_transport_ignores_ambient_proxy_with_bearer(
    token: RedactedSecret,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("http_proxy", "http://untrusted-proxy.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted-proxy.invalid:8080")
    monkeypatch.setenv("no_proxy", "")
    captured: list[Request] = []

    def capture_transport(_handler: HTTPHandler, request: Request) -> _FakeResponse:
        captured.append(request)
        return _FakeResponse(b'{"count":0,"items":[],"limit":50,"offset":0}')

    monkeypatch.setattr(HTTPHandler, "http_open", capture_transport)
    client = DashboardApiClient(base_url="http://control-api:8000", token=token)
    client.get(DashboardRoute.ORDERS)
    assert len(captured) == 1
    assert captured[0].host == "control-api:8000"
    assert captured[0].get_header("Authorization") == f"Bearer {'T' * 32}"


def _job_payload() -> dict[str, object]:
    return {
        "job_id": "job-1",
        "job_type": "OFFLINE_DEMO",
        "state": "SUCCEEDED",
        "attempt_count": 1,
        "max_attempts": 3,
        "next_attempt_at": None,
        "lease_expires_at": None,
        "completed_at": "2026-07-06T20:00:00Z",
        "safe_last_error_code": None,
        "safe_last_error_message": None,
        "result_artifact_id": "demo-evidence-1",
        "created_at": "2026-07-06T19:59:00Z",
        "updated_at": "2026-07-06T20:00:00Z",
        "version": 4,
    }


def test_job_lookup_uses_only_validated_identifier_and_get(token: RedactedSecret) -> None:
    captured: list[Request] = []

    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        captured.append(request)
        return _FakeResponse(json.dumps(_job_payload()).encode())

    client = DashboardApiClient(base_url="http://control-api:8000", token=token, opener=opener)
    assert client.get_job("job-1") == _job_payload()
    assert captured[0].full_url == "http://control-api:8000/v1/jobs/job-1"
    assert captured[0].method == "GET"


@pytest.mark.parametrize(
    "job_id",
    (
        "../orders",
        "/v1/orders",
        "https://untrusted.invalid",
        "job?override=1",
        "job%2F1",
        "job\x00",
        "",
        "a" * 129,
    ),
)
def test_invalid_job_identifier_never_reaches_transport(job_id: str, token: RedactedSecret) -> None:
    def forbidden_opener(request: Request, *, timeout: float) -> _FakeResponse:
        raise AssertionError("invalid job ID reached transport")

    client = DashboardApiClient(
        base_url="http://control-api:8000",
        token=token,
        opener=forbidden_opener,
    )
    with pytest.raises(DashboardApiError, match="identifier is invalid"):
        client.get_job(job_id)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("job_id", "different-job"),
        ("state", "UNKNOWN"),
        ("attempt_count", True),
        ("attempt_count", 4),
        ("created_at", "invalidZ"),
        ("created_at", None),
        ("result_artifact_id", "../escape"),
        ("safe_last_error_message", "Bearer sentinel"),
        ("unexpected_field", "value"),
    ),
)
def test_job_lookup_rejects_malformed_or_secret_bearing_responses(
    field: str,
    value: object,
    token: RedactedSecret,
) -> None:
    payload = {**_job_payload(), field: value}

    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        return _FakeResponse(json.dumps(payload).encode())

    client = DashboardApiClient(base_url="http://control-api:8000", token=token, opener=opener)
    with pytest.raises(DashboardApiError, match="response was invalid"):
        client.get_job("job-1")


def test_dashboard_job_field_contract_matches_public_response() -> None:
    from adaptive_trader.platform.control.models import JobResponse

    assert frozenset(JobResponse.model_fields) == _JOB_FIELDS


@pytest.mark.parametrize(
    "base_url",
    (
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://127.0.0.1:80",
        "http://169.254.169.254:8000",
        "http://[::1]",
        "http://operator:password@control-api:8000",  # pragma: allowlist secret
        "http://control-api:8000/path",
        "https://control-api:8000",
    ),
)
def test_dashboard_client_rejects_noncanonical_or_unapproved_origins(
    base_url: str,
    token: RedactedSecret,
) -> None:
    with pytest.raises(DashboardApiError, match="origin is invalid"):
        DashboardApiClient(base_url=base_url, token=token)


@pytest.mark.parametrize(
    ("payload", "content_type"),
    (
        (b'{"state":"ready","state":"bad"}', "application/json"),
        (b"[]", "application/json"),
        (b"not-json", "application/json"),
        (b"[" * 2_000 + b"0" + b"]" * 2_000, "application/json"),
        (b'{"status":"ready"}', "text/plain"),
    ),
)
def test_dashboard_client_rejects_untrusted_api_responses(
    payload: bytes,
    content_type: str,
    token: RedactedSecret,
) -> None:
    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        del request, timeout
        return _FakeResponse(payload, content_type=content_type)

    client = DashboardApiClient(
        base_url="http://127.0.0.1:8000",
        token=token,
        opener=opener,
    )
    with pytest.raises(DashboardApiError, match="response was invalid"):
        client.get(DashboardRoute.SYSTEM_STATUS)


def test_dashboard_client_bounds_responses_and_redacts_token_representation(
    token: RedactedSecret,
) -> None:
    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        del request, timeout
        return _FakeResponse(b"{" + b" " * 1_048_576 + b"}")

    client = DashboardApiClient(
        base_url="http://localhost:8000",
        token=token,
        opener=opener,
    )
    assert "T" * 32 not in repr(client)
    assert "T" * 32 not in repr(token)
    with pytest.raises(DashboardApiError, match="size limit"):
        client.get(DashboardRoute.SYSTEM_STATUS)


def test_dashboard_route_contract_contains_no_mutations() -> None:
    assert all(route.value.startswith(("/health/", "/v1/")) for route in DashboardRoute)
    assert not any(
        fragment in route.value
        for route in DashboardRoute
        for fragment in ("submit", "cancel", "replace", "liquidate", "flatten", "resume", "halt")
    )
    assert len(tuple(DashboardRoute)) == 16


def test_dashboard_client_accepts_only_exact_health_and_page_schemas(
    token: RedactedSecret,
) -> None:
    payloads = iter(
        (
            b'{"checked_at":"2026-09-05T12:00:00.000000Z","checks":{},"status":"live"}',
            b'{"checked_at":"2026-09-05T12:00:00.000000Z","checks":{"database":true},"status":"ready"}',
            b'{"count":1,"items":[{"broker_authority":"none","database":"reachable","service":"control_api"}],"limit":50,"offset":0}',
        )
    )

    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        del request, timeout
        return _FakeResponse(next(payloads))

    client = DashboardApiClient(
        base_url="http://control-api:8000",
        token=token,
        opener=opener,
    )
    assert client.get(DashboardRoute.HEALTH_LIVE)["status"] == "live"
    assert client.get(DashboardRoute.HEALTH_READY)["status"] == "ready"
    assert client.get(DashboardRoute.SYSTEM_STATUS)["count"] == 1


@pytest.mark.parametrize(
    "payload",
    (
        b'{"count":0,"items":[],"limit":50,"offset":0,"unknown":true}',
        b'{"count":1,"items":[{}],"limit":50,"offset":0}',
        b'{"count":2,"items":[],"limit":50,"offset":0}',
        b'{"count":0,"items":[],"limit":101,"offset":0}',
    ),
)
def test_dashboard_client_rejects_schema_drift(
    payload: bytes,
    token: RedactedSecret,
) -> None:
    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        del request, timeout
        return _FakeResponse(payload)

    client = DashboardApiClient(
        base_url="http://control-api:8000",
        token=token,
        opener=opener,
    )
    with pytest.raises(DashboardApiError, match="response was invalid"):
        client.get(DashboardRoute.ORDERS)


@pytest.mark.parametrize(
    "payload",
    (
        b'{"count":1,"items":[{"broker_authority":"none","database":"Bearer TEST_VALUE_MUST_NOT_LEAK","service":"control_api"}],"limit":50,"offset":0}',
        b'{"count":1,"items":[{"configuration":{"nested":{"api_key":"TEST_VALUE_MUST_NOT_LEAK"}},"experiment_content_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","experiment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","experiment_id":"fixture","experiment_symbol_id":"symbol_1","experiment_version":1,"ordinal":0,"registered_at":"2026-09-05T12:00:00.000000Z","role":"active","schema_version":1,"symbol":"AMD","symbol_content_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}],"limit":50,"offset":0}',  # pragma: allowlist secret
    ),
)
def test_dashboard_client_recursively_rejects_secret_bearing_responses(
    payload: bytes,
    token: RedactedSecret,
) -> None:
    def opener(request: Request, *, timeout: float) -> _FakeResponse:
        del request, timeout
        return _FakeResponse(payload)

    route = (
        DashboardRoute.SYSTEM_STATUS
        if b"broker_authority" in payload
        else DashboardRoute.EXPERIMENT
    )
    client = DashboardApiClient(
        base_url="http://control-api:8000",
        token=token,
        opener=opener,
    )
    with pytest.raises(DashboardApiError, match="response was invalid") as captured:
        client.get(route)
    assert "TEST_VALUE_MUST_NOT_LEAK" not in str(captured.value)


def test_dashboard_transport_refuses_redirect_requests() -> None:
    handler = _NoRedirectHandler()
    original = Request(
        "http://control-api:8000/v1/orders",
        headers={"Authorization": "Bearer TEST_VALUE_MUST_NOT_LEAK"},
    )
    redirected = handler.redirect_request(
        original,
        None,
        302,
        "Found",
        Message(),
        "http://127.0.0.1:8000/v1/orders",
    )

    assert redirected is None


def test_dashboard_page_field_contract_matches_control_view_inventory() -> None:
    from adaptive_trader.platform.control.queries import _VIEW_SPECS, ReadResource

    routes = {
        DashboardRoute.EXPERIMENT: ReadResource.EXPERIMENT,
        DashboardRoute.DATA_STATUS: ReadResource.DATA_STATUS,
        DashboardRoute.DATA_GAPS: ReadResource.DATA_GAPS,
        DashboardRoute.DATASETS: ReadResource.DATASETS,
        DashboardRoute.DECISION_SLOTS: ReadResource.DECISION_SLOTS,
        DashboardRoute.SIGNALS: ReadResource.SIGNALS,
        DashboardRoute.RISK_DECISIONS: ReadResource.RISK_DECISIONS,
        DashboardRoute.RISK_LATCHES: ReadResource.RISK_LATCHES,
        DashboardRoute.ORDERS: ReadResource.ORDERS,
        DashboardRoute.FILLS: ReadResource.FILLS,
        DashboardRoute.RECONCILIATIONS: ReadResource.RECONCILIATIONS,
        DashboardRoute.INCIDENTS: ReadResource.INCIDENTS,
        DashboardRoute.AUDIT_STATUS: ReadResource.AUDIT_STATUS,
    }

    assert set(_PAGE_FIELDS) == {DashboardRoute.SYSTEM_STATUS, *routes}
    for route, resource in routes.items():
        assert _PAGE_FIELDS[route] == frozenset(
            column.name for column in _VIEW_SPECS[resource].relation.columns
        )
