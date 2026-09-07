"""Public process command dispatch never widens service or network authority."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from adaptive_trader.platform import cli, paper_gate_runtime, runtime, worker_cli, worker_runtime
from adaptive_trader.platform.config import RuntimeService


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["run"],
        ["evil", "scheduler-worker"],
        ["run", "scheduler-worker", "--import"],
        ["health", "scheduler-worker", "--once"],
        ["run", "x", "--once", "extra"],
        ["run", "module:callable"],
        ["run", "control-api"],
    ],
)
def test_worker_command_rejects_unclosed_invocations(monkeypatch, arguments):
    monkeypatch.setattr(worker_cli.sys, "argv", ["worker", *arguments])
    with pytest.raises(SystemExit):
        worker_cli.main()


@pytest.mark.parametrize(
    "service", [RuntimeService.SCHEDULER_WORKER, RuntimeService.PAPER_EXECUTION_WORKER]
)
@pytest.mark.parametrize("healthy", [False, True])
def test_worker_health_status_is_truthful(monkeypatch, capsys, service, healthy):
    monkeypatch.setattr(worker_cli.sys, "argv", ["worker", "health", service.value])
    monkeypatch.setattr(worker_runtime, "service_is_healthy", lambda **kwargs: healthy)
    monkeypatch.setattr(paper_gate_runtime, "paper_gate_is_healthy", lambda: healthy)
    if healthy:
        worker_cli.main()
        assert capsys.readouterr().out.strip() == "worker health: ready"
    else:
        with pytest.raises(SystemExit, match="not ready"):
            worker_cli.main()
        assert not capsys.readouterr().out


@pytest.mark.parametrize("once", [False, True])
@pytest.mark.parametrize(
    "service", [RuntimeService.SCHEDULER_WORKER, RuntimeService.PAPER_EXECUTION_WORKER]
)
def test_worker_run_preserves_closed_identity_and_finite_intent(monkeypatch, service, once):
    monkeypatch.setattr(
        worker_cli.sys, "argv", ["worker", "run", service.value, *(["--once"] if once else [])]
    )
    calls = []
    settings = object()
    monkeypatch.setattr(worker_cli, "load_runtime_settings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(
        paper_gate_runtime, "run_paper_gate", lambda value, **kwargs: calls.append((value, kwargs))
    )
    monkeypatch.setattr(
        worker_runtime, "run_service_worker", lambda **kwargs: calls.append((None, kwargs))
    )
    worker_cli.main()
    assert len(calls) == 1
    assert calls[0][1]["once"] is once
    if service is RuntimeService.PAPER_EXECUTION_WORKER:
        assert calls[0][0] is settings
    else:
        assert calls[0][1]["service"] is service


@pytest.mark.parametrize(
    "command, target", [("api", "serve_control_api"), ("dashboard", "serve_dashboard")]
)
@pytest.mark.parametrize("failure", [OSError, RuntimeError, TypeError, ValueError])
def test_public_server_cli_redacts_dependency_failures(monkeypatch, command, target, failure):
    def fail(**kwargs):
        raise failure("private-operator-token")

    monkeypatch.setattr(runtime, target, fail)
    result = CliRunner().invoke(cli.app, [command, "serve"])
    assert result.exit_code == 2
    assert "private-operator-token" not in result.output
    assert "error" in result.output


@pytest.mark.parametrize(
    "function,port", [(runtime.serve_control_api, 8000), (runtime.serve_dashboard, 8501)]
)
@pytest.mark.parametrize(
    "host, override",
    [
        ("remote.example", None),
        ("::", None),
        (True, None),
        ("127.0.0.1", True),
        ("127.0.0.1", 9999),
    ],
)
def test_server_network_authority_rejected_before_settings(
    monkeypatch, tmp_path, function, port, host, override
):
    def forbidden(**kwargs):
        pytest.fail("invalid listen address reached credential settings")

    monkeypatch.setattr(runtime, "_settings", forbidden)
    with pytest.raises(runtime.ServiceRuntimeError):
        function(host=host, port=port if override is None else override, application_root=tmp_path)


@pytest.mark.parametrize("failure_at", ["build", "serve"])
def test_control_server_disposes_engine_when_composition_or_runner_fails(
    monkeypatch, tmp_path, failure_at
):
    disposed = []
    monkeypatch.setattr(runtime, "_settings", lambda **kwargs: object())
    monkeypatch.setattr(
        runtime,
        "create_platform_engine",
        lambda *args, **kwargs: SimpleNamespace(dispose=lambda: disposed.append(True)),
    )

    def fail(*args, **kwargs):
        raise ValueError("fixture-failure")

    monkeypatch.setattr(
        runtime,
        "build_control_runtime_app",
        fail if failure_at == "build" else lambda *args, **kwargs: object(),
    )
    with pytest.raises(ValueError, match="fixture-failure"):
        runtime.serve_control_api(
            host="127.0.0.1", port=8000, application_root=tmp_path, runner=fail
        )
    assert disposed == [True]


@pytest.mark.parametrize("json_output", [False, True])
def test_demo_cli_redacts_failed_evidence_provider(monkeypatch, json_output):
    from adaptive_trader.platform import demo

    def fail(**kwargs):
        raise OSError("private-artifact-root")

    monkeypatch.setattr(demo, "run_demo_twice", fail)
    result = CliRunner().invoke(cli.app, ["demo", *(["--json"] if json_output else [])])
    assert result.exit_code == 2
    assert "private-artifact-root" not in result.output
    assert "offline verification failed" in result.output


@pytest.mark.parametrize("outcome", ["blocked", "ok", "raise"])
@pytest.mark.parametrize("json_output", [False, True])
def test_shadow_proposal_cli_disposes_engine_and_preserves_denial(
    monkeypatch, outcome, json_output
):
    from adaptive_trader.platform import operational_strategy
    from adaptive_trader.platform.storage import engine as engine_module

    disposed = []
    monkeypatch.setattr(cli, "load_runtime_settings", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        engine_module,
        "create_platform_engine",
        lambda *args, **kwargs: SimpleNamespace(dispose=lambda: disposed.append(True)),
    )

    def propose(**kwargs):
        if outcome == "raise":
            raise RuntimeError("private-database-url")
        return {"status": outcome}

    monkeypatch.setattr(operational_strategy, "propose_once", propose)
    result = CliRunner().invoke(
        cli.app,
        [
            "shadow",
            "propose-once",
            "--slot-id",
            "fixture-slot",
            *(["--json"] if json_output else []),
        ],
    )
    assert result.exit_code == (0 if outcome == "ok" else 2)
    assert "private-database-url" not in result.output
    assert disposed == [True]


@pytest.mark.parametrize("once, failing", [(True, True), (True, False), (False, True)])
def test_job_service_loop_redacts_failure_and_disposes(
    monkeypatch, tmp_path, capsys, once, failing
):
    import threading

    stop = threading.Event()
    events = []
    monkeypatch.setattr(runtime, "_settings", lambda **kwargs: object())
    monkeypatch.setattr(
        runtime,
        "create_platform_engine",
        lambda *a, **k: SimpleNamespace(dispose=lambda: events.append("disposed")),
    )

    def cycle():
        events.append("cycle")
        if not once:
            stop.set()
        if failing:
            raise RuntimeError("private-outbox-payload")

    monkeypatch.setattr(
        runtime, "_job_worker_service", lambda **kwargs: SimpleNamespace(run_cycle=cycle)
    )
    arguments = dict(
        application_root=tmp_path,
        environment={},
        once=once,
        stop=stop,
        health_root=tmp_path / "health",
    )
    if failing and once:
        with pytest.raises(runtime.ServiceRuntimeError, match="job worker cycle failed"):
            runtime.run_job_worker(**arguments)
    else:
        runtime.run_job_worker(**arguments)
    assert events == ["cycle", "disposed"]
    assert "private-outbox-payload" not in capsys.readouterr().err


def test_job_runtime_disposes_before_rejecting_invalid_stop_boundary(monkeypatch, tmp_path):
    disposed = []
    monkeypatch.setattr(runtime, "_settings", lambda **kwargs: object())
    monkeypatch.setattr(
        runtime,
        "create_platform_engine",
        lambda *a, **k: SimpleNamespace(dispose=lambda: disposed.append(True)),
    )
    with pytest.raises(TypeError, match="threading Event"):
        runtime.run_job_worker(application_root=tmp_path, stop=object())
    assert disposed == [True]


@pytest.mark.parametrize("outcome", ["blocked", "ok", "raise"])
@pytest.mark.parametrize("fixture", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_shadow_evaluation_cli_disposes_and_preserves_fixture_authority(
    monkeypatch, outcome, fixture, json_output
):
    from adaptive_trader.platform import shadow, shadow_settings

    disposed = []
    environments = []

    def settings(environment, **kwargs):
        environments.append((environment, kwargs))
        return object()

    monkeypatch.setattr(shadow_settings, "load_shadow_execution_settings", settings)
    monkeypatch.setattr(
        shadow_settings,
        "create_shadow_execution_engine",
        lambda *args: SimpleNamespace(dispose=lambda: disposed.append(True)),
    )

    def evaluate(**kwargs):
        assert kwargs["fixture"] is fixture
        if outcome == "raise":
            raise RuntimeError("private-diagnostic-state")
        return {"status": outcome}

    monkeypatch.setattr(shadow, "run_shadow_once", evaluate)
    result = CliRunner().invoke(
        cli.app,
        [
            "shadow",
            "run-once",
            *(["--fixture"] if fixture else []),
            *(["--json"] if json_output else []),
        ],
    )
    assert result.exit_code == (0 if outcome == "ok" else 2)
    assert "private-diagnostic-state" not in result.output
    assert disposed == [True]
    assert environments[0][0]["AQA_CONFIG"] == (
        "configs/platform/offline.yaml" if fixture else "configs/platform/shadow.yaml"
    )


def test_paper_worker_cli_reports_default_deny_failure(monkeypatch):
    monkeypatch.setattr(
        worker_cli.sys,
        "argv",
        ["worker", "run", RuntimeService.PAPER_EXECUTION_WORKER.value, "--once"],
    )
    monkeypatch.setattr(worker_cli, "load_runtime_settings", lambda *a, **k: object())

    def denied(*args, **kwargs):
        raise paper_gate_runtime.PaperGateRuntimeError("paper authorization unavailable")

    monkeypatch.setattr(paper_gate_runtime, "run_paper_gate", denied)
    with pytest.raises(SystemExit, match="paper authorization unavailable"):
        worker_cli.main()


@pytest.mark.parametrize("action", ["run", "health"])
def test_collector_worker_cli_delegates_only_closed_collector_command(monkeypatch, action):
    from adaptive_trader.collection import cli as collector

    calls = []
    monkeypatch.setattr(collector, "ready", lambda: calls.append("health"))
    monkeypatch.setattr(collector, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        worker_cli.sys, "argv", ["worker", action, RuntimeService.MARKET_DATA_LIVE.value]
    )
    worker_cli.main()
    assert calls == (
        ["health"] if action == "health" else [{"start_if_empty": None, "verbose": False}]
    )


@pytest.mark.parametrize("module", [runtime, worker_runtime])
@pytest.mark.parametrize("failure_stage", ["install", "restore"])
def test_worker_signal_failure_always_disposes_database(
    monkeypatch, tmp_path, module, failure_stage
):
    events = []
    engine = SimpleNamespace(dispose=lambda: events.append("dispose"))
    monkeypatch.setattr(module, "create_platform_engine", lambda *a, **k: engine)

    def restore():
        events.append("restore")
        raise RuntimeError("signal restore failed")

    def install(stop):
        events.append("install")
        if failure_stage == "install":
            raise RuntimeError("signal install failed")
        stop.set()
        return restore

    monkeypatch.setattr(module, "_install_stop_handlers", install)
    if module is runtime:
        monkeypatch.setattr(module, "_settings", lambda **k: object())
        monkeypatch.setattr(module, "_job_worker_service", lambda **k: object())

        def run():
            return module.run_job_worker(application_root=tmp_path, environment={})
    else:
        monkeypatch.setattr(module, "load_runtime_settings", lambda *a, **k: object())
        monkeypatch.setattr(module, "_prepare_offline_database", lambda *a: None)
        monkeypatch.setattr(
            module,
            "build_worker_cycle",
            lambda *a: SimpleNamespace(close=lambda: events.append("close")),
        )

        def run():
            return module.run_service_worker(
                service=RuntimeService.SCHEDULER_WORKER,
                application_root=tmp_path,
                environment={},
                health_root=tmp_path / "health",
            )

    with pytest.raises(RuntimeError, match=f"signal {failure_stage} failed"):
        run()
    assert events[0] == "install"
    assert events[-1] == "dispose"
    assert events.count("dispose") == 1
    if module is worker_runtime and failure_stage == "restore":
        assert events == ["install", "close", "restore", "dispose"]
