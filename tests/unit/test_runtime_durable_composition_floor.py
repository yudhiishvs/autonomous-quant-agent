"""Exercise durable worker composition and process boundaries without external services."""

import io
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from adaptive_trader.platform import runtime, worker_runtime
from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.jobs import JobCreateRequest, JobPayload
from adaptive_trader.platform.jobs.models import JobState
from adaptive_trader.platform.observability.logging import configure_json_logging
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA


def test_real_job_service_publishes_verified_demo_and_outbox_then_idles(tmp_path, monkeypatch):
    shutil.copytree(Path(__file__).resolve().parents[2] / "configs", tmp_path / "configs")
    settings = load_runtime_settings(
        {}, service=RuntimeService.JOB_WORKER, application_root=tmp_path
    )
    engine = create_engine("sqlite://").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )
    worker_runtime._prepare_offline_database(settings, engine)
    unsigned = {
        "schema": "offline-demo-evidence-v1",
        "evidence_label": "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE",
        "proof": "fixture",
    }
    manifest = {**unsigned, "evidence_manifest_hash": sha256_hex(unsigned)}
    monkeypatch.setattr(
        runtime,
        "run_demo_twice",
        lambda **kwargs: SimpleNamespace(first=SimpleNamespace(manifest=lambda: manifest)),
    )
    logger_output = io.StringIO()
    logger = configure_json_logging(service="job_worker", stream=logger_output)
    try:
        repository = runtime._job_repository(engine)
        created = repository.create(
            JobCreateRequest(
                payload=JobPayload.offline_demo(demo_id="composition"),
                idempotency_key="composition",
                correlation_id="0198fa2d-7b8c-7123-8abc-0123456789ab",
                requested_at=datetime.now(UTC),
            )
        )
        service = runtime._job_worker_service(
            settings=settings,
            engine=engine,
            application_root=tmp_path,
            logger=logger,
            health_root=tmp_path / "health",
        )
        result = service.run_cycle()
        assert result is not None and result.state is JobState.SUCCEEDED
        assert repository.get(created.job_id).result_artifact_id is not None
        assert service.run_cycle() is None
        assert runtime.job_worker_is_healthy(
            service=RuntimeService.JOB_WORKER, health_root=tmp_path / "health"
        )
        assert "outbox.published" in logger_output.getvalue()
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", ["none", "schema", "snapshot", "construct"])
def test_collector_health_closes_repository_on_every_acquired_path(monkeypatch, failure):
    from adaptive_trader.collection import migrations, operations, postgres
    from adaptive_trader.collection.runtime import CollectorEnvironment

    events = []
    monkeypatch.setattr(
        CollectorEnvironment, "from_environment", lambda: SimpleNamespace(database_url="fixture")
    )

    def require(*args):
        if failure == "schema":
            raise RuntimeError("private-migration-detail")

    def snapshot(*args, **kwargs):
        if failure == "snapshot":
            raise RuntimeError("private-snapshot-detail")
        return {"service_ready": True}

    def repository(*args, **kwargs):
        if failure == "construct":
            raise RuntimeError("private-connection-detail")
        return SimpleNamespace(
            engine=object(),
            verify_schema=lambda: events.append("verified"),
            close=lambda: events.append("closed"),
        )

    monkeypatch.setattr(migrations, "require_database_at_head", require)
    monkeypatch.setattr(operations, "health_snapshot", snapshot)
    monkeypatch.setattr(postgres, "PostgresMarketDataRepository", repository)
    assert worker_runtime.service_is_healthy(service=RuntimeService.MARKET_DATA_LIVE) is (
        failure == "none"
    )
    assert events.count("closed") == (0 if failure == "construct" else 1)


@pytest.mark.parametrize("module", [runtime, worker_runtime])
def test_signal_handlers_stop_cooperatively_and_restore_exact_previous_handlers(
    monkeypatch, module
):
    previous = object()
    handlers = {}
    monkeypatch.setattr(module.signal, "getsignal", lambda selected: previous)
    monkeypatch.setattr(
        module.signal, "signal", lambda selected, handler: handlers.__setitem__(selected, handler)
    )
    stop = threading.Event()
    restore = module._install_stop_handlers(stop)
    handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
    assert stop.is_set()
    restore()
    assert all(handler is previous for handler in handlers.values())


@pytest.mark.parametrize("module", [runtime, worker_runtime])
@pytest.mark.parametrize("root_kind", ["wrong-type", "missing", "file"])
def test_application_root_denial_precedes_runtime_settings(
    monkeypatch, tmp_path, module, root_kind
):
    root = tmp_path / "absent"
    if root_kind == "wrong-type":
        root = str(tmp_path)
    elif root_kind == "file":
        root.write_text("fixture")
    with pytest.raises(runtime.ServiceRuntimeError, match="application root is unavailable"):
        module._canonical_application_root(root)


@pytest.mark.parametrize("service", [RuntimeService.CONTROL_API, RuntimeService.DASHBOARD])
def test_unimplemented_worker_services_have_no_health_authority(tmp_path, service):
    assert not runtime.platform_worker_is_healthy(service=service, health_root=tmp_path / "health")
    assert not worker_runtime.service_is_healthy(service=service, health_root=tmp_path / "health")
    with pytest.raises(runtime.ServiceRuntimeError):
        runtime.run_platform_worker(
            service=service, application_root=tmp_path, environment={}, once=True
        )
