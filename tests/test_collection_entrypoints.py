"""Every supported live worker entry point selects the canonical collector lifecycle."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

from adaptive_trader.collection import cli as collection_cli
from adaptive_trader.platform import service_cycles, worker_cli, worker_runtime
from adaptive_trader.platform.cli import app as platform_app
from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.runtime import run_platform_worker

_ROOT = Path(__file__).resolve().parents[1]


def test_collect_once_cli_delegates_to_canonical_finite_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(collection_cli, "collect_once", lambda *, verbose: calls.append(verbose))
    result = CliRunner().invoke(platform_app, ["data", "collect-once"])
    assert result.exit_code == 0, result.output
    assert calls == [False]


def test_collect_once_without_secret_files_rejects_before_database_or_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unconfigured one-shot collection must not open external resources")

    monkeypatch.setattr(collection_cli, "PostgresMarketDataRepository", forbidden)
    monkeypatch.setattr(collection_cli, "AlpacaHistoricalBarSource", forbidden)
    result = CliRunner().invoke(platform_app, ["data", "collect-once"])
    assert result.exit_code == 1
    assert "all three data-service secret file references" in result.output


@pytest.mark.parametrize("entrypoint", ("worker_cli", "worker_runtime", "runtime", "platform_cli"))
def test_supported_live_entrypoints_delegate_canonical_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entrypoint: str
) -> None:
    selected: list[tuple[object, bool]] = []

    def canonical_run(*, start_if_empty: object, verbose: bool) -> None:
        selected.append((start_if_empty, verbose))

    def forbidden_cycle(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("live entrypoint constructed an obsolete bounded worker cycle")

    monkeypatch.setattr(collection_cli, "run", canonical_run)
    monkeypatch.setattr(worker_runtime, "build_worker_cycle", forbidden_cycle)
    monkeypatch.setattr(worker_runtime, "create_platform_engine", forbidden_cycle)
    if entrypoint == "worker_cli":
        monkeypatch.setattr(sys, "argv", ["worker_cli", "run", "market-data-live"])
        worker_cli.main()
    elif entrypoint == "worker_runtime":
        worker_runtime.run_service_worker(
            service=RuntimeService.MARKET_DATA_LIVE, application_root=tmp_path
        )
    elif entrypoint == "runtime":
        run_platform_worker(service=RuntimeService.MARKET_DATA_LIVE, application_root=tmp_path)
    else:
        result = CliRunner().invoke(platform_app, ["service", "run", "market-data-live"])
        assert result.exit_code == 0, result.output
    assert selected == [(None, False)]


@pytest.mark.parametrize("entrypoint", ("worker_cli", "worker_runtime", "runtime", "platform_cli"))
def test_live_once_cannot_reactivate_the_obsolete_stream_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entrypoint: str
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("finite live mode must not start any provider pipeline")

    monkeypatch.setattr(collection_cli, "run", forbidden)
    monkeypatch.setattr(worker_runtime, "build_worker_cycle", forbidden)
    if entrypoint == "worker_cli":
        monkeypatch.setattr(sys, "argv", ["worker_cli", "run", "market-data-live", "--once"])
        with pytest.raises(SystemExit, match="explicit backfill"):
            worker_cli.main()
    elif entrypoint == "platform_cli":
        result = CliRunner().invoke(platform_app, ["service", "run", "market-data-live", "--once"])
        assert result.exit_code == 2
        assert "error" in result.output
    else:
        run = (
            worker_runtime.run_service_worker
            if entrypoint == "worker_runtime"
            else run_platform_worker
        )
        with pytest.raises(worker_runtime.ServiceRuntimeError, match="own bounded backfill"):
            run(service=RuntimeService.MARKET_DATA_LIVE, application_root=tmp_path, once=True)


@pytest.mark.parametrize("override", ("environment", "stop"))
def test_live_runtime_rejects_old_loop_controls(tmp_path: Path, override: str) -> None:
    kwargs = {"environment": {}} if override == "environment" else {"stop": threading.Event()}
    with pytest.raises(worker_runtime.ServiceRuntimeError, match="own bounded backfill"):
        worker_runtime.run_service_worker(
            service=RuntimeService.MARKET_DATA_LIVE, application_root=tmp_path, **kwargs
        )


def test_live_worker_health_delegates_canonical_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []
    monkeypatch.setattr(collection_cli, "ready", lambda: observed.append("canonical_readiness"))
    monkeypatch.setattr(sys, "argv", ["worker_cli", "health", "market-data-live"])
    worker_cli.main()
    assert observed == ["canonical_readiness"]


def test_obsolete_factory_rejects_live_settings_without_changing_offline_factory() -> None:
    settings = load_runtime_settings(
        {
            "AQA_CONFIG": "configs/platform/shadow.yaml",
            "AQA_DATABASE_URL_FILE": "/run/secrets/collector_database_url",
            "AQA_ALPACA_DATA_API_KEY_FILE": "/run/secrets/alpaca_data_api_key",
            "AQA_ALPACA_DATA_SECRET_KEY_FILE": "/run/secrets/alpaca_data_secret_key",
        },
        service=RuntimeService.MARKET_DATA_LIVE,
        application_root=_ROOT,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(service_cycles.WorkerCycleError, match="canonical collection service"):
            service_cycles.build_worker_cycle(settings, engine)
        offline_settings = load_runtime_settings(
            {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=_ROOT
        )
        assert isinstance(
            service_cycles.build_worker_cycle(offline_settings, engine),
            service_cycles.OfflineMarketDataCycle,
        )
    finally:
        engine.dispose()
    assert not hasattr(service_cycles, "LiveMarketDataCycle")
    assert not hasattr(service_cycles, "_materialize_recent_alpaca_buckets")
