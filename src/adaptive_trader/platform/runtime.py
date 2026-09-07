"""Production entry-point wiring for the private API, dashboard, and durable jobs."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Protocol, cast

from fastapi import FastAPI
from sqlalchemy import Engine

from adaptive_trader.platform.canonical import JsonValue
from adaptive_trader.platform.config import (
    RuntimeService,
    RuntimeSettings,
    load_runtime_settings,
)
from adaptive_trader.platform.control import (
    OperatorRateLimiter,
    OperatorTokenAuthenticator,
    SQLAlchemyControlQueryService,
    SQLAlchemyOperatorControls,
    create_control_app,
)
from adaptive_trader.platform.demo import run_demo_twice
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.jobs import (
    DurableJobWorker,
    DurableOutboxWorker,
    JobRecord,
    JobRepository,
)
from adaptive_trader.platform.jobs.artifacts import ImmutableJobArtifactStore
from adaptive_trader.platform.jobs.handlers import PlatformJobHandlerSet
from adaptive_trader.platform.observability import (
    LogSeverity,
    StructuredLogEvent,
    StructuredLogger,
    configure_json_logging,
)
from adaptive_trader.platform.observability.health import HealthService, ReadinessCheck
from adaptive_trader.platform.observability.metrics import PlatformMetrics
from adaptive_trader.platform.observability.operational import SQLAlchemyOperationalMetricsReader
from adaptive_trader.platform.service_health import (
    _HEALTH_ROOT as _HEALTH_ROOT,
)
from adaptive_trader.platform.service_health import (
    DOMAIN_WORKER_SERVICES as _DOMAIN_WORKERS,
)
from adaptive_trader.platform.service_health import (
    ServiceHealthMarker as ServiceHealthMarker,
)
from adaptive_trader.platform.service_health import (
    ServiceRuntimeError as ServiceRuntimeError,
)
from adaptive_trader.platform.service_health import (
    _process_exists as _process_exists,
)
from adaptive_trader.platform.storage import AuditRepository, create_platform_engine

# The wildcard bind is accepted only through this closed container-service allowlist;
# Compose controls host publication and keeps the control network private.
_API_HOSTS = frozenset({"127.0.0.1", "0.0.0.0"})  # nosec B104
_DASHBOARD_HOSTS = _API_HOSTS
_API_PORT = 8000
_DASHBOARD_PORT = 8501
_JOB_POLL_SECONDS = 1.0
_LOG_SERVICE_NAMES = {
    RuntimeService.CONTROL_API: "control_api",
    RuntimeService.DASHBOARD: "dashboard",
    RuntimeService.JOB_WORKER: "job_worker",
}


class ServerRunner(Protocol):
    def __call__(self, app: FastAPI, **options: object) -> None: ...


class DashboardRunner(Protocol):
    def __call__(
        self,
        main_script_path: str,
        is_hello: bool,
        args: list[str],
        flag_options: dict[str, Any],
    ) -> None: ...


class StructuredOutboxPublisher:
    """Deliver outbox transitions to the bounded structured operations log."""

    def __init__(self, logger: StructuredLogger) -> None:
        if not isinstance(logger, StructuredLogger):
            raise TypeError("outbox publisher requires a structured logger")
        self._logger = logger

    def publish(
        self,
        *,
        outbox_event_id: str,
        event_type: str,
        aggregate_id: str,
        payload: object,
    ) -> None:
        del aggregate_id, payload
        self._logger.emit(
            StructuredLogEvent(
                severity=LogSeverity.INFO,
                event_type="outbox.published",
                correlation_id=outbox_event_id,
                reason_code="delivery_succeeded",
            )
        )


class JobWorkerService:
    """Perform one job and one outbox delivery per bounded service cycle."""

    def __init__(
        self,
        *,
        jobs: DurableJobWorker,
        outbox: DurableOutboxWorker,
        health: ServiceHealthMarker,
        logger: StructuredLogger,
    ) -> None:
        if not isinstance(jobs, DurableJobWorker) or not isinstance(outbox, DurableOutboxWorker):
            raise TypeError("job service requires durable workers")
        if not isinstance(health, ServiceHealthMarker) or not isinstance(logger, StructuredLogger):
            raise TypeError("job service requires health and logging boundaries")
        self._jobs = jobs
        self._outbox = outbox
        self._health = health
        self._logger = logger

    def run_cycle(self) -> JobRecord | None:
        result = self._jobs.run_one()
        self._outbox.run_one()
        self._health.record_ready()
        if result is not None:
            self._logger.emit(
                StructuredLogEvent(
                    severity=LogSeverity.INFO,
                    event_type="job.completed_cycle",
                    correlation_id=result.job_id,
                    reason_code=result.state.value.lower(),
                )
            )
        return result


def build_control_runtime_app(
    settings: RuntimeSettings,
    *,
    engine: Engine,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Compose the private API from one validated control-service capability set."""

    if type(settings) is not RuntimeSettings or settings.service is not RuntimeService.CONTROL_API:
        raise TypeError("control API wiring requires control-service settings")
    token_reference = settings.operator_token_file
    if token_reference is None:
        raise ServiceRuntimeError("control API authentication is not configured")
    queries = SQLAlchemyControlQueryService(engine)
    return create_control_app(
        authenticator=OperatorTokenAuthenticator.from_reference(token_reference),
        rate_limiter=OperatorRateLimiter(monotonic=monotonic),
        jobs=_job_repository(engine),
        queries=queries,
        operator_controls=SQLAlchemyOperatorControls(
            engine,
            experiment_hash=settings.platform.experiment.definition_hash,
        ),
        health=HealthService(
            clock=clock,
            readiness_checks=(ReadinessCheck(name="database", probe=queries.ready),),
        ),
        metrics=PlatformMetrics(
            authoritative_reader=SQLAlchemyOperationalMetricsReader(engine),
        ),
        clock=clock,
        docs_enabled=settings.api_docs_enabled,
    )


def serve_control_api(
    *,
    host: str,
    port: int,
    application_root: Path,
    environment: Mapping[str, str] | None = None,
    runner: ServerRunner | None = None,
) -> None:
    """Validate control authority, build the app, and run one Uvicorn process."""

    _require_server_address(host=host, port=port, allowed_hosts=_API_HOSTS, expected_port=_API_PORT)
    configure_json_logging(
        service=_LOG_SERVICE_NAMES[RuntimeService.CONTROL_API], stream=sys.stderr
    )
    settings = _settings(
        service=RuntimeService.CONTROL_API,
        application_root=application_root,
        environment=environment,
    )
    engine = create_platform_engine(settings, application_name="aqa-control-api")
    try:
        api = build_control_runtime_app(settings, engine=engine)
        selected_runner = _uvicorn_runner() if runner is None else runner
        selected_runner(
            api,
            host=host,
            port=port,
            access_log=False,
            log_config=None,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=30,
            workers=1,
        )
    finally:
        engine.dispose()


def serve_dashboard(
    *,
    host: str,
    port: int,
    application_root: Path,
    environment: Mapping[str, str] | None = None,
    runner: DashboardRunner | None = None,
) -> None:
    """Validate read-only dashboard authority and run Streamlit in process."""

    _require_server_address(
        host=host,
        port=port,
        allowed_hosts=_DASHBOARD_HOSTS,
        expected_port=_DASHBOARD_PORT,
    )
    configure_json_logging(service=_LOG_SERVICE_NAMES[RuntimeService.DASHBOARD], stream=sys.stderr)
    root = _canonical_application_root(application_root)
    _settings(
        service=RuntimeService.DASHBOARD,
        application_root=root,
        environment=environment,
    )
    from adaptive_trader.platform.dashboard import app as dashboard_app

    if dashboard_app.__file__ is None:
        raise ServiceRuntimeError("dashboard entry point is unavailable")
    selected_runner = _streamlit_runner() if runner is None else runner
    selected_runner(
        str(Path(dashboard_app.__file__).resolve(strict=True)),
        False,
        [str(root)],
        {
            "browser.gatherUsageStats": False,
            "server.address": host,
            "server.enableCORS": True,
            "server.enableXsrfProtection": True,
            "server.headless": True,
            "server.port": port,
        },
    )


def run_job_worker(
    *,
    application_root: Path,
    environment: Mapping[str, str] | None = None,
    once: bool = False,
    stop: threading.Event | None = None,
    health_root: Path = _HEALTH_ROOT,
) -> None:
    """Run the control-authority job and outbox worker until signalled."""

    root = _canonical_application_root(application_root)
    settings = _settings(
        service=RuntimeService.JOB_WORKER,
        application_root=root,
        environment=environment,
    )
    engine = create_platform_engine(settings, application_name="aqa-job-worker")
    stop_event = threading.Event() if stop is None else stop
    if not isinstance(stop_event, threading.Event):
        engine.dispose()
        raise TypeError("job worker stop boundary must be a threading Event")
    restore_handlers: Callable[[], None] | None = None
    try:
        restore_handlers = _install_stop_handlers(stop_event) if stop is None else None
        logger = configure_json_logging(
            service=_LOG_SERVICE_NAMES[RuntimeService.JOB_WORKER], stream=sys.stderr
        )
        worker = _job_worker_service(
            settings=settings,
            engine=engine,
            application_root=root,
            logger=logger,
            health_root=health_root,
        )
        while not stop_event.is_set():
            try:
                worker.run_cycle()
            except Exception:
                logger.emit(
                    StructuredLogEvent(
                        severity=LogSeverity.ERROR,
                        event_type="job.cycle_failed",
                        reason_code="bounded_runtime_failure",
                    )
                )
                if once:
                    raise ServiceRuntimeError("job worker cycle failed") from None
            if once:
                return
            stop_event.wait(_JOB_POLL_SECONDS)
    finally:
        try:
            if restore_handlers is not None:
                restore_handlers()
        finally:
            engine.dispose()


def job_worker_is_healthy(
    *,
    service: RuntimeService,
    health_root: Path = _HEALTH_ROOT,
) -> bool:
    """Return whether a recent heartbeat belongs to a currently live job worker."""

    try:
        return ServiceHealthMarker(service=service, root=health_root).is_ready()
    except (ServiceRuntimeError, TypeError, ValueError):
        return False


def run_platform_worker(
    *,
    service: RuntimeService,
    application_root: Path,
    environment: Mapping[str, str] | None = None,
    once: bool = False,
    stop: threading.Event | None = None,
    health_root: Path = _HEALTH_ROOT,
) -> None:
    """Dispatch one exact implemented worker without expanding its capability set."""

    if service is RuntimeService.JOB_WORKER:
        run_job_worker(
            application_root=application_root,
            environment=environment,
            once=once,
            stop=stop,
            health_root=health_root,
        )
        return
    if service not in _DOMAIN_WORKERS:
        raise ServiceRuntimeError("service has no implemented runtime")
    from adaptive_trader.platform.worker_runtime import run_service_worker

    run_service_worker(
        service=service,
        application_root=application_root,
        environment=environment,
        once=once,
        stop=stop,
        health_root=health_root,
    )


def platform_worker_is_healthy(
    *,
    service: RuntimeService,
    health_root: Path = _HEALTH_ROOT,
) -> bool:
    """Return readiness only for an implemented bounded worker."""

    if service is RuntimeService.JOB_WORKER:
        return job_worker_is_healthy(service=service, health_root=health_root)
    if service not in _DOMAIN_WORKERS:
        return False
    from adaptive_trader.platform.worker_runtime import service_is_healthy

    return service_is_healthy(service=service, health_root=health_root)


def _job_worker_service(
    *,
    settings: RuntimeSettings,
    engine: Engine,
    application_root: Path,
    logger: StructuredLogger,
    health_root: Path,
) -> JobWorkerService:
    if settings.service is not RuntimeService.JOB_WORKER:
        raise TypeError("job worker wiring requires job-worker settings")
    clock = _utc_now
    repository = _job_repository(engine)
    artifacts = ImmutableJobArtifactStore(settings.artifact_root)

    def demo_evidence() -> dict[str, JsonValue]:
        comparison = run_demo_twice(config_root=application_root / "configs")
        return cast(dict[str, JsonValue], comparison.first.manifest())

    handlers = PlatformJobHandlerSet(
        experiment=settings.platform.experiment.definition,
        audit=AuditRepository(engine),
        queries=SQLAlchemyControlQueryService(engine),
        artifacts=artifacts,
        clock=clock,
        demo_evidence_provider=demo_evidence,
    ).bounded()
    owner = f"job-worker-{os.getpid()}"
    metrics = PlatformMetrics()
    return JobWorkerService(
        jobs=DurableJobWorker(
            repository,
            owner=owner,
            handlers=handlers,
            clock=clock,
            transition_observer=metrics.record_job_state,
        ),
        outbox=DurableOutboxWorker(
            repository,
            owner=owner,
            publisher=StructuredOutboxPublisher(logger),
            clock=clock,
        ),
        health=ServiceHealthMarker(service=RuntimeService.JOB_WORKER, root=health_root),
        logger=logger,
    )


def _job_repository(engine: Engine) -> JobRepository:
    return JobRepository(
        engine,
        audit=AuditRepository(engine, writer=AuditWriter.CONTROL),
    )


def _settings(
    *,
    service: RuntimeService,
    application_root: Path,
    environment: Mapping[str, str] | None,
) -> RuntimeSettings:
    root = _canonical_application_root(application_root)
    selected_environment = dict(os.environ) if environment is None else environment
    return load_runtime_settings(
        selected_environment,
        service=service,
        application_root=root,
    )


def _canonical_application_root(application_root: Path) -> Path:
    if type(application_root) is not type(Path()):
        raise ServiceRuntimeError("application root is unavailable")
    try:
        root = Path(os.path.abspath(os.fspath(application_root)))
        if root.resolve(strict=True) != root or not root.is_dir():
            raise ServiceRuntimeError("application root is unavailable")
        return root
    except ServiceRuntimeError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ServiceRuntimeError("application root is unavailable") from None


def _require_server_address(
    *,
    host: str,
    port: int,
    allowed_hosts: frozenset[str],
    expected_port: int,
) -> None:
    if type(host) is not str or host not in allowed_hosts or type(port) is not int:
        raise ServiceRuntimeError("server address is not allowed")
    if port != expected_port:
        raise ServiceRuntimeError("server port is not allowed")


def _uvicorn_runner() -> ServerRunner:
    import uvicorn

    return cast(ServerRunner, uvicorn.run)


def _streamlit_runner() -> DashboardRunner:
    from streamlit.web import bootstrap

    return bootstrap.run


def _install_stop_handlers(stop: threading.Event) -> Callable[[], None]:
    previous = {
        selected: signal.getsignal(selected) for selected in (signal.SIGINT, signal.SIGTERM)
    }

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    try:
        for selected in previous:
            signal.signal(selected, request_stop)
    except (OSError, RuntimeError, ValueError):
        for selected, handler in previous.items():
            with suppress(OSError, RuntimeError, ValueError):
                signal.signal(selected, handler)
        raise ServiceRuntimeError("job worker signal handling could not be installed") from None

    def restore() -> None:
        for selected, handler in previous.items():
            signal.signal(selected, handler)

    return restore


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DashboardRunner",
    "JobWorkerService",
    "ServerRunner",
    "ServiceHealthMarker",
    "ServiceRuntimeError",
    "StructuredOutboxPublisher",
    "build_control_runtime_app",
    "job_worker_is_healthy",
    "platform_worker_is_healthy",
    "run_job_worker",
    "run_platform_worker",
    "serve_control_api",
    "serve_dashboard",
]
