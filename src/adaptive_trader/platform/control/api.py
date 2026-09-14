"""Private, authenticated FastAPI control plane with no direct trading routes."""

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.control.auth import (
    OperatorPrincipal,
    OperatorScope,
    OperatorTokenAuthenticator,
)
from adaptive_trader.platform.control.models import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ControlMutationResponse,
    DataQualityAuditRequest,
    DatasetFreezeRequest,
    ErrorDetail,
    ErrorResponse,
    GapRepairRequest,
    JobResponse,
    OfflineDemoRequest,
    OperatorHaltRequest,
    OperatorResumeRequest,
    SafePageResponse,
)
from adaptive_trader.platform.control.operator import (
    OperatorControlConflictError,
    OperatorControlError,
    OperatorControlPort,
)
from adaptive_trader.platform.control.queries import (
    ControlQueryError,
    ControlQueryPort,
    ReadResource,
)
from adaptive_trader.platform.control.rate_limit import OperatorRateLimiter, RateClass
from adaptive_trader.platform.jobs import (
    JobConflictError,
    JobCreateRequest,
    JobPayload,
    JobPersistenceError,
    JobRepository,
    JobValidationError,
)
from adaptive_trader.platform.observability.health import HealthService
from adaptive_trader.platform.observability.metrics import LatchMetricType, PlatformMetrics
from adaptive_trader.platform.risk.latches import RiskLatchKind

MAX_REQUEST_BYTES = 65_536
REQUEST_BODY_TIMEOUT_SECONDS = 10.0
_RESOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9._:-]{0,127}$"
_CORRELATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.ASCII,
)
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (
        b"content-security-policy",
        b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    ),
)


class ControlApiError(RuntimeError):
    """Context-free HTTP failure mapped to a stable public error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        super().__init__(code)


class RequestSecurityMiddleware:
    """Bound bodies, assign correlation IDs, and add defensive response headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        correlation_id_factory: Callable[[], str],
    ) -> None:
        self._app = app
        self._correlation_id_factory = correlation_id_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        correlation_id = self._correlation_id_factory()
        if type(correlation_id) is not str or _CORRELATION_ID.fullmatch(correlation_id) is None:
            correlation_id = "00000000-0000-0000-0000-000000000000"
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        state["authenticated"] = False

        content_lengths = [
            value for key, value in scope.get("headers", ()) if key == b"content-length"
        ]
        if len(content_lengths) > 1:
            await _send_middleware_error(
                send, status=400, code="invalid_request", correlation_id=correlation_id
            )
            return
        if content_lengths:
            try:
                declared_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeError, ValueError):
                await _send_middleware_error(
                    send,
                    status=400,
                    code="invalid_request",
                    correlation_id=correlation_id,
                )
                return
            if declared_length < 0 or declared_length > MAX_REQUEST_BYTES:
                await _send_middleware_error(
                    send,
                    status=413,
                    code="request_too_large",
                    correlation_id=correlation_id,
                )
                return

        body = bytearray()
        try:
            async with asyncio.timeout(REQUEST_BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        await _send_middleware_error(
                            send,
                            status=413,
                            code="request_too_large",
                            correlation_id=correlation_id,
                        )
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await _send_middleware_error(
                send, status=408, code="request_timeout", correlation_id=correlation_id
            )
            return

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                headers.extend(_SECURITY_HEADERS)
                headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                if state.get("authenticated") is True:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self._app(scope, replay, secure_send)


def create_control_app(
    *,
    authenticator: OperatorTokenAuthenticator,
    rate_limiter: OperatorRateLimiter,
    jobs: JobRepository,
    queries: ControlQueryPort,
    operator_controls: OperatorControlPort,
    health: HealthService,
    metrics: PlatformMetrics,
    clock: Callable[[], datetime],
    correlation_id_factory: Callable[[], str] | None = None,
    docs_enabled: bool = False,
) -> FastAPI:
    """Build the private control API from capability-scoped dependencies."""

    if not isinstance(authenticator, OperatorTokenAuthenticator):
        raise TypeError("control API requires the operator authenticator")
    if not isinstance(rate_limiter, OperatorRateLimiter):
        raise TypeError("control API requires the operator rate limiter")
    if not isinstance(jobs, JobRepository):
        raise TypeError("control API requires the bounded job repository")
    if not callable(getattr(queries, "page", None)) or not callable(
        getattr(queries, "ready", None)
    ):
        raise TypeError("control API requires the read-only query port")
    if not callable(getattr(operator_controls, "halt", None)) or not callable(
        getattr(operator_controls, "resume", None)
    ):
        raise TypeError("control API requires the operator latch port")
    if not isinstance(health, HealthService):
        raise TypeError("control API requires the health service")
    if not isinstance(metrics, PlatformMetrics):
        raise TypeError("control API requires platform metrics")
    if not callable(clock):
        raise TypeError("control API requires an injected clock")
    if type(docs_enabled) is not bool:
        raise TypeError("control API docs setting must be boolean")

    app = FastAPI(
        title="Autonomous Quant Agent Control API",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.add_middleware(
        RequestSecurityMiddleware,
        correlation_id_factory=correlation_id_factory or (lambda: str(uuid4())),
    )

    def authorize(
        request: Request,
        rate_class: RateClass,
        authorization: str | None,
        *,
        require_operator: bool,
    ) -> OperatorPrincipal:
        principal = authenticator.authenticate(authorization)
        if principal is None:
            metrics.record_api_authentication_failure()
            raise ControlApiError(
                401,
                "authentication_required",
                "Valid bearer authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.authenticated = True
        if require_operator and principal.scope is not OperatorScope.OPERATOR:
            raise ControlApiError(
                403,
                "insufficient_scope",
                "Operator authorization is required",
            )
        if not rate_limiter.allow(fingerprint=principal.fingerprint, rate_class=rate_class):
            metrics.record_api_rate_limit()
            raise ControlApiError(
                429,
                "rate_limited",
                "Request rate limit exceeded",
                headers={"Retry-After": "60"},
            )
        return principal

    def authorize_read(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorPrincipal:
        return authorize(
            request,
            RateClass.READ,
            authorization,
            require_operator=False,
        )

    def authorize_mutation(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorPrincipal:
        return authorize(
            request,
            RateClass.MUTATION,
            authorization,
            require_operator=True,
        )

    @app.exception_handler(ControlApiError)
    async def control_error(request: Request, error: ControlApiError) -> JSONResponse:
        return _error_response(
            request,
            status=error.status_code,
            code=error.code,
            message=error.message,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        del error
        metrics.record_security_validation_failure()
        return _error_response(
            request,
            status=422,
            code="invalid_request",
            message="Request validation failed",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "http_error"
        message = (
            "Resource not found" if error.status_code == 404 else "Request could not be served"
        )
        return _error_response(request, status=error.status_code, code=code, message=message)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return _error_response(
            request,
            status=500,
            code="internal_error",
            message="Request could not be completed",
        )

    @app.get("/health/live")
    def live() -> dict[str, object]:
        return health.live().as_dict()

    @app.get("/health/ready")
    def ready() -> Response:
        report = health.ready()
        return JSONResponse(report.as_dict(), status_code=200 if report.healthy else 503)

    def read_page(
        resource: ReadResource,
        *,
        limit: int,
        offset: int,
    ) -> SafePageResponse:
        try:
            return queries.page(resource, limit=limit, offset=offset)
        except ControlQueryError:
            raise ControlApiError(
                503, "state_unavailable", "Operational state is unavailable"
            ) from None

    def make_read_endpoint(resource: ReadResource) -> Callable[..., SafePageResponse]:
        def endpoint(
            principal: Annotated[OperatorPrincipal, Depends(authorize_read)],
            limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
            offset: Annotated[int, Query(ge=0)] = 0,
        ) -> SafePageResponse:
            del principal
            return read_page(resource, limit=limit, offset=offset)

        return endpoint

    read_routes = {
        "/v1/system/status": ReadResource.SYSTEM_STATUS,
        "/v1/experiment": ReadResource.EXPERIMENT,
        "/v1/data/status": ReadResource.DATA_STATUS,
        "/v1/data/gaps": ReadResource.DATA_GAPS,
        "/v1/datasets": ReadResource.DATASETS,
        "/v1/decision-slots": ReadResource.DECISION_SLOTS,
        "/v1/signals": ReadResource.SIGNALS,
        "/v1/risk-decisions": ReadResource.RISK_DECISIONS,
        "/v1/risk/latches": ReadResource.RISK_LATCHES,
        "/v1/orders": ReadResource.ORDERS,
        "/v1/fills": ReadResource.FILLS,
        "/v1/reconciliations": ReadResource.RECONCILIATIONS,
        "/v1/incidents": ReadResource.INCIDENTS,
        "/v1/audit/status": ReadResource.AUDIT_STATUS,
    }
    for path, resource in read_routes.items():
        app.add_api_route(
            path,
            make_read_endpoint(resource),
            methods=["GET"],
            response_model=SafePageResponse,
            name=resource.value,
        )

    @app.get("/v1/jobs/{job_id}", response_model=JobResponse)
    def job_status(
        job_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
        principal: Annotated[OperatorPrincipal, Depends(authorize_read)],
    ) -> JobResponse:
        del principal
        try:
            job = jobs.get(job_id)
        except JobPersistenceError:
            raise ControlApiError(503, "state_unavailable", "Job state is unavailable") from None
        if job is None:
            raise ControlApiError(404, "job_not_found", "Job does not exist")
        return JobResponse.from_record(job)

    def create_job(payload: JobPayload, request: Any) -> JobResponse:
        try:
            job = jobs.create(
                JobCreateRequest(
                    payload=payload,
                    idempotency_key=request.idempotency_key,
                    correlation_id=request.correlation_id,
                    requested_at=clock(),
                )
            )
        except JobConflictError:
            raise ControlApiError(
                409, "idempotency_conflict", "Idempotency key conflicts"
            ) from None
        except JobValidationError:
            raise ControlApiError(400, "invalid_job", "Job request is invalid") from None
        except JobPersistenceError:
            raise ControlApiError(503, "state_unavailable", "Job state is unavailable") from None
        return JobResponse.from_record(job)

    @app.post("/v1/jobs/data-quality-audit", response_model=JobResponse, status_code=202)
    def create_data_quality_job(
        body: Annotated[DataQualityAuditRequest, Body()],
        principal: Annotated[OperatorPrincipal, Depends(authorize_mutation)],
    ) -> JobResponse:
        del principal
        return create_job(
            JobPayload.data_quality_audit(experiment_hash=body.experiment_hash),
            body,
        )

    @app.post("/v1/jobs/gap-repair", response_model=JobResponse, status_code=202)
    def create_gap_repair_job(
        body: Annotated[GapRepairRequest, Body()],
        principal: Annotated[OperatorPrincipal, Depends(authorize_mutation)],
    ) -> JobResponse:
        del principal
        return create_job(JobPayload.gap_repair(gap_id=body.gap_id), body)

    @app.post("/v1/jobs/dataset-freeze", response_model=JobResponse, status_code=202)
    def create_dataset_freeze_job(
        body: Annotated[DatasetFreezeRequest, Body()],
        principal: Annotated[OperatorPrincipal, Depends(authorize_mutation)],
    ) -> JobResponse:
        del principal
        return create_job(
            JobPayload.dataset_freeze(experiment_hash=body.experiment_hash),
            body,
        )

    @app.post("/v1/jobs/offline-demo", response_model=JobResponse, status_code=202)
    def create_offline_demo_job(
        body: Annotated[OfflineDemoRequest, Body()],
        principal: Annotated[OperatorPrincipal, Depends(authorize_mutation)],
    ) -> JobResponse:
        del principal
        return create_job(JobPayload.offline_demo(demo_id=body.demo_id), body)

    @app.post("/v1/risk/halt", response_model=ControlMutationResponse)
    def halt(
        body: Annotated[OperatorHaltRequest, Body()],
        principal: Annotated[OperatorPrincipal, Depends(authorize_mutation)],
    ) -> ControlMutationResponse:
        del principal
        try:
            result = operator_controls.halt(request=body, occurred_at=clock())
        except OperatorControlConflictError:
            raise ControlApiError(
                409, "control_conflict", "Operator control conflicts with current state"
            ) from None
        except OperatorControlError:
            raise ControlApiError(
                503, "state_unavailable", "Operator control state is unavailable"
            ) from None
        metrics.set_latch(LatchMetricType.OPERATOR, engaged=True)
        return ControlMutationResponse(
            event_id=result.event_id,
            state="engaged",
            reason_code=result.reason_code,
            latch_type=_clearable_latch_name(result.latch_type),
        )

    @app.post("/v1/risk/resume", response_model=ControlMutationResponse)
    def resume(
        body: Annotated[OperatorResumeRequest, Body()],
        principal: Annotated[OperatorPrincipal, Depends(authorize_mutation)],
    ) -> ControlMutationResponse:
        del principal
        try:
            result = operator_controls.resume(request=body, occurred_at=clock())
        except OperatorControlConflictError:
            raise ControlApiError(
                409, "control_conflict", "Operator control conflicts with current state"
            ) from None
        except OperatorControlError:
            raise ControlApiError(
                503, "state_unavailable", "Operator control state is unavailable"
            ) from None
        metrics.set_latch(_latch_metric_type(result.latch_type), engaged=False)
        return ControlMutationResponse(
            event_id=result.event_id,
            state="cleared",
            reason_code=result.reason_code,
            latch_type=_clearable_latch_name(result.latch_type),
        )

    @app.get("/metrics")
    def prometheus_metrics(
        principal: Annotated[OperatorPrincipal, Depends(authorize_read)],
    ) -> Response:
        del principal
        return Response(content=metrics.render(), media_type=metrics.content_type)

    return app


def _latch_metric_type(latch_type: RiskLatchKind) -> LatchMetricType:
    if type(latch_type) is not RiskLatchKind:
        raise TypeError("latch metric mapping requires the closed contract")
    return {
        RiskLatchKind.DEPLOYMENT_DRAWDOWN: LatchMetricType.DRAWDOWN,
        RiskLatchKind.OPERATOR_HALT: LatchMetricType.OPERATOR,
        RiskLatchKind.SESSION_LOSS: LatchMetricType.SESSION_LOSS,
    }[latch_type]


def _clearable_latch_name(
    latch_type: RiskLatchKind,
) -> Literal["deployment_drawdown", "operator_halt", "session_loss"]:
    if type(latch_type) is not RiskLatchKind:
        raise TypeError("latch response mapping requires the closed contract")
    if latch_type is RiskLatchKind.DEPLOYMENT_DRAWDOWN:
        return "deployment_drawdown"
    if latch_type is RiskLatchKind.OPERATOR_HALT:
        return "operator_halt"
    if latch_type is RiskLatchKind.SESSION_LOSS:
        return "session_loss"
    raise ValueError("latch response mapping excludes the system-owned latch")


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    correlation_id = cast(str, getattr(request.state, "correlation_id", "unknown"))
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, correlation_id=correlation_id)
    )
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status,
        headers=headers,
    )


async def _send_middleware_error(
    send: Send,
    *,
    status: int,
    code: str,
    correlation_id: str,
) -> None:
    payload = canonical_json_bytes(
        {
            "error": {
                "code": code,
                "message": "Request exceeded the control-plane boundary"
                if status == 413
                else "Request body deadline exceeded"
                if status == 408
                else "Request framing is invalid",
                "correlation_id": correlation_id,
            }
        }
    )
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"x-correlation-id", correlation_id.encode("ascii")),
        *_SECURITY_HEADERS,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
