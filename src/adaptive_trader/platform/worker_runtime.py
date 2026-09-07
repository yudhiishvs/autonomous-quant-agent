"""Long-running, restart-safe runtime shared by isolated platform worker images."""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from sqlalchemy import Engine

from adaptive_trader.platform.config import RuntimeService, RuntimeSettings, load_runtime_settings
from adaptive_trader.platform.observability.logging import (
    LogSeverity,
    StructuredLogEvent,
    configure_json_logging,
)
from adaptive_trader.platform.service_cycles import WorkerCycle, build_worker_cycle
from adaptive_trader.platform.service_health import (
    _HEALTH_ROOT as _HEALTH_ROOT,
)
from adaptive_trader.platform.service_health import (
    _HEALTH_WORKERS as HEALTH_MARKER_SERVICES,
)
from adaptive_trader.platform.service_health import (
    DOMAIN_WORKER_SERVICES as DOMAIN_WORKER_SERVICES,
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
from adaptive_trader.platform.storage.engine import create_platform_engine

_POLL_SECONDS = 1.0


def run_service_worker(
    *,
    service: RuntimeService,
    application_root: Path,
    environment: Mapping[str, str] | None = None,
    once: bool = False,
    stop: threading.Event | None = None,
    health_root: Path = _HEALTH_ROOT,
) -> None:
    """Run one least-privilege domain worker until signalled or for one bounded cycle."""

    if type(service) is not RuntimeService or service not in DOMAIN_WORKER_SERVICES:
        raise ServiceRuntimeError("service has no implemented domain runtime")
    if service is RuntimeService.MARKET_DATA_LIVE:
        if once or environment is not None or stop is not None:
            raise ServiceRuntimeError(
                "canonical collector uses its own bounded backfill and shutdown commands"
            )
        from adaptive_trader.collection.cli import run

        run(start_if_empty=None, verbose=False)
        return
    root = _canonical_application_root(application_root)
    selected_environment = dict(os.environ) if environment is None else environment
    settings = load_runtime_settings(
        selected_environment,
        service=service,
        application_root=root,
    )
    engine = create_platform_engine(settings, application_name=f"aqa-{service.value}")
    stop_event = threading.Event() if stop is None else stop
    if not isinstance(stop_event, threading.Event):
        engine.dispose()
        raise TypeError("worker stop boundary must be a threading Event")
    restore_handlers: Callable[[], None] | None = None
    cycle: WorkerCycle | None = None
    try:
        restore_handlers = _install_stop_handlers(stop_event) if stop is None else None
        logger = configure_json_logging(service=service.value.replace("-", "_"), stream=sys.stderr)
        _prepare_offline_database(settings, engine)
        cycle = build_worker_cycle(settings, engine)
        marker = ServiceHealthMarker(service=service, root=health_root)
        while not stop_event.is_set():
            try:
                outcome = cycle.run_cycle()
                if outcome.state.value == "blocked":
                    marker.invalidate()
                else:
                    marker.record_ready()
                logger.emit(
                    StructuredLogEvent(
                        severity=(
                            LogSeverity.WARNING
                            if outcome.state.value == "blocked"
                            else LogSeverity.INFO
                        ),
                        event_type="worker.cycle_completed",
                        reason_code=outcome.reason_code,
                    )
                )
            except Exception:
                marker.invalidate()
                logger.emit(
                    StructuredLogEvent(
                        severity=LogSeverity.ERROR,
                        event_type="worker.cycle_failed",
                        reason_code="bounded_runtime_failure",
                    )
                )
                if once:
                    raise ServiceRuntimeError("worker cycle failed") from None
            if once:
                return
            stop_event.wait(_POLL_SECONDS)
    finally:
        try:
            if cycle is not None:
                try:
                    cycle.close()
                except Exception:
                    raise ServiceRuntimeError("worker shutdown failed") from None
        finally:
            try:
                if restore_handlers is not None:
                    restore_handlers()
            finally:
                engine.dispose()


def service_is_healthy(
    *,
    service: RuntimeService,
    health_root: Path = _HEALTH_ROOT,
) -> bool:
    """Return whether a recent heartbeat belongs to the selected live worker process."""

    if service is RuntimeService.MARKET_DATA_LIVE:
        from adaptive_trader.collection.migrations import require_database_at_head
        from adaptive_trader.collection.operations import health_snapshot
        from adaptive_trader.collection.postgres import PostgresMarketDataRepository
        from adaptive_trader.collection.runtime import CollectorEnvironment

        try:
            environment = CollectorEnvironment.from_environment()
            repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
            try:
                require_database_at_head(environment.database_url)
                repository.verify_schema()
                return bool(
                    health_snapshot(repository.engine, now=datetime.now(UTC))["service_ready"]
                )
            finally:
                repository.close()
        except Exception:
            # A health probe fails closed without exposing database/provider diagnostics.
            return False
    try:
        return ServiceHealthMarker(service=service, root=health_root).is_ready()
    except (ServiceRuntimeError, TypeError, ValueError):
        return False


def _prepare_offline_database(settings: RuntimeSettings, engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        from alembic.migration import MigrationContext

        from adaptive_trader.platform.storage.migration_runner import platform_migration_head

        with engine.connect() as connection:
            heads = MigrationContext.configure(
                connection, opts={"version_table_schema": "market_data"}
            ).get_current_heads()
        if heads != (platform_migration_head(),):
            raise ServiceRuntimeError("worker database is not at the installed migration head")
        return
    from adaptive_trader.platform.storage.experiments import ExperimentRepository
    from adaptive_trader.platform.storage.tables import metadata

    metadata.create_all(engine)
    ExperimentRepository(engine).register(
        settings.platform.experiment.definition,
        registered_at=datetime.now(UTC),
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
        raise ServiceRuntimeError("worker signal handling could not be installed") from None

    def restore() -> None:
        for selected, handler in previous.items():
            signal.signal(selected, handler)

    return restore


__all__ = [
    "DOMAIN_WORKER_SERVICES",
    "HEALTH_MARKER_SERVICES",
    "ServiceHealthMarker",
    "ServiceRuntimeError",
    "run_service_worker",
    "service_is_healthy",
]
