"""Credential-free paper worker checks against durable signal and audit state."""

import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from adaptive_trader.platform.config import (
    BrokerAdapter,
    ExecutionMode,
    RuntimeService,
    load_runtime_settings,
)
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.paper_cycle import (
    AuditPaperDenialRecorder,
    PaperCycleResult,
    PaperCycleState,
    PaperExecutionCycle,
    SQLPaperCandidateSource,
)
from adaptive_trader.platform.paper_gate_runtime import (
    PaperGateRuntimeError,
    paper_gate_is_healthy,
    run_paper_gate,
)
from adaptive_trader.platform.risk import policy_hash
from adaptive_trader.platform.scheduling import DecisionSlotRepository, build_session_schedule
from adaptive_trader.platform.signals import (
    AlwaysFlatSignalProvider,
    DecisionContext,
    SignalEnvelopeRepository,
)
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import metadata

ROOT = Path(__file__).resolve().parents[2]


def _settings(*, root=ROOT, enabled=False):
    return load_runtime_settings(
        {
            "AQA_CONFIG": "configs/platform/paper.yaml",
            "AQA_ENABLE_PAPER_ORDERS": "I_ACKNOWLEDGE_AQA_PAPER_ONLY" if enabled else "NO",
            "AQA_DATABASE_URL_FILE": "/run/secrets/database_url",
            "AQA_ALPACA_PAPER_API_KEY_FILE": "/run/secrets/paper_key",
            "AQA_ALPACA_PAPER_SECRET_KEY_FILE": "/run/secrets/paper_secret",
            "AQA_PAPER_ACCOUNT_ID_HASH_FILE": "/run/secrets/account_hash",
        },
        service=RuntimeService.PAPER_EXECUTION_WORKER,
        application_root=root,
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_paper_cycle_reads_durable_signal_and_retries_one_audited_denial(tmp_path, enabled):
    import shutil

    root = tmp_path / "application"
    root.mkdir()
    shutil.copytree(ROOT / "configs", root / "configs")
    profile = root / "configs/platform/paper.yaml"
    if enabled:
        profile.write_text(
            profile.read_text().replace("submission_enabled: false", "submission_enabled: true")
        )
    settings = _settings(root=root, enabled=enabled)
    experiment = settings.platform.experiment.definition
    engine = create_engine(f"sqlite:///{tmp_path / 'state.sqlite3'}").execution_options(
        schema_translate_map={"aqa": None}
    )
    metadata.create_all(engine)
    ExperimentRepository(engine).register(
        experiment, registered_at=datetime(2026, 7, 6, 12, tzinfo=UTC)
    )
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="always_flat",
        signal_provider_version="1",
        session_date=date(2026, 7, 6),
        calendar=XnasExchangeCalendar(),
    )
    DecisionSlotRepository(engine).create_schedule(
        schedule, recorded_at=datetime(2026, 7, 6, 12, tzinfo=UTC)
    )
    slot = schedule.strategy_slots[0]
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash="a" * 64,
        policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
        execution_mode=ExecutionMode.PAPER,
        broker_adapter=BrokerAdapter.ALPACA_PAPER,
        submission_enabled=enabled,
        strategy_slot_ordinal=0,
    )
    signal = AlwaysFlatSignalProvider(clock=lambda: slot.ready_at).signal_for(context)
    SignalEnvelopeRepository(engine).persist_once(signal, context=context)
    for _ in range(2):
        cycle = PaperExecutionCycle(
            settings,
            candidates=SQLPaperCandidateSource(settings, engine),
            denials=AuditPaperDenialRecorder(engine),
            clock=lambda: slot.ready_at + timedelta(seconds=1),
        )
        result = cycle.run_cycle()
        assert result.reason_code == "model_approval_not_implemented"
        assert result.state is PaperCycleState.BLOCKED
    events = AuditRepository(engine, writer=AuditWriter.EXECUTION).list_events(
        stream_id=f"aqa_execution:{signal.signal_id}"
    )
    assert len(events) == 1
    assert events[0].payload["reason_code"] == "model_approval_not_implemented"
    assert SQLPaperCandidateSource(settings, engine).current(observed_at=signal.expires_at) is None
    engine.dispose()


class _Cycle:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.closed = False
        self.calls = 0

    def run_cycle(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private payload must not escape")
        return PaperCycleResult(PaperCycleState.BLOCKED, "model_approval_not_implemented", 1)

    def close(self):
        self.closed = True


def test_paper_health_requires_successful_cycle_and_failure_removes_marker(tmp_path):
    root = tmp_path.resolve() / "health"
    cycle = _Cycle()
    run_paper_gate(_settings(), once=True, stop=threading.Event(), health_root=root, cycle=cycle)
    assert cycle.calls == 1 and cycle.closed
    assert paper_gate_is_healthy(health_root=root)
    failing = _Cycle(fail=True)
    with pytest.raises(PaperGateRuntimeError, match=r"^paper authorization cycle failed$"):
        run_paper_gate(
            _settings(), once=True, stop=threading.Event(), health_root=root, cycle=failing
        )
    assert failing.closed
    assert not paper_gate_is_healthy(health_root=root)


def test_execution_image_source_closure_imports_without_provider_code(tmp_path):
    """Mirror exact COPY inputs, so missing image modules fail without Docker/network."""
    import os
    import shlex
    import shutil
    import subprocess
    import sys

    target = (ROOT / "Dockerfile").read_text().split("FROM runtime-base AS execution\n", 1)[1]
    for line in target.replace("\\\n", " ").splitlines():
        if not line.startswith("COPY src/"):
            continue
        tokens = shlex.split(line)
        destination = tmp_path / tokens[-1].removeprefix("./")
        for source in tokens[1:-1]:
            if tokens[-1].endswith("/"):
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / source, destination / Path(source).name)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / source, destination)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import adaptive_trader.platform.paper_gate_runtime; import adaptive_trader.platform.paper_cycle; import adaptive_trader.platform.risk.policy; assert 'adaptive_trader.platform.signals.providers' not in sys.modules",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "src/adaptive_trader/platform/signals/providers.py").exists()


def test_paper_cleanup_failure_is_redacted_and_not_healthy(tmp_path):
    class FailingClose(_Cycle):
        def close(self):
            raise RuntimeError("private cleanup payload")

    root = tmp_path.resolve() / "health"
    with pytest.raises(PaperGateRuntimeError, match=r"^paper authorization cleanup failed$"):
        run_paper_gate(
            _settings(), once=True, stop=threading.Event(), health_root=root, cycle=FailingClose()
        )
    assert not paper_gate_is_healthy(health_root=root)


def test_paper_marker_rejects_group_pid_and_does_not_chmod_symlink_target(tmp_path):
    import json
    import stat

    from adaptive_trader.platform.paper_gate_runtime import _record_health

    root = tmp_path.resolve() / "health"
    _record_health(root)
    marker = root / "paper-execution-worker.json"
    payload = json.loads(marker.read_text())
    payload["pid"] = -1
    marker.write_text(json.dumps(payload))
    assert not paper_gate_is_healthy(health_root=root)
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(PaperGateRuntimeError, match="unsafe"):
        _record_health(link)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_paper_startup_cannot_unlink_a_marker_through_a_directory_symlink(tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    marker = destination / "paper-execution-worker.json"
    marker.write_text("preserve this unrelated file")
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    cycle = _Cycle()
    with pytest.raises(PaperGateRuntimeError):
        run_paper_gate(
            _settings(), once=True, stop=threading.Event(), health_root=link, cycle=cycle
        )
    assert cycle.calls == 0
    assert marker.read_text() == "preserve this unrelated file"


def test_runtime_composes_real_idle_database_cycle_without_submission_credentials(
    tmp_path, monkeypatch
):
    from adaptive_trader.platform import security
    from adaptive_trader.platform.storage import engine as engine_module

    engine = create_engine("sqlite://").execution_options(schema_translate_map={"aqa": None})
    metadata.create_all(engine)
    monkeypatch.setattr(engine_module, "create_platform_engine", lambda *args, **kwargs: engine)

    def deny_secrets(*args, **kwargs):
        raise AssertionError("paper credential loading must follow an approved policy")

    monkeypatch.setattr(security, "load_secret_file", deny_secrets)
    root = tmp_path.resolve() / "health"
    run_paper_gate(_settings(), once=True, health_root=root)
    assert paper_gate_is_healthy(health_root=root)


def test_runtime_cooperative_shutdown_stops_after_bounded_cycles(tmp_path, monkeypatch):
    import adaptive_trader.platform.paper_gate_runtime as runtime

    stop = threading.Event()

    class StopAfterTwo(_Cycle):
        def run_cycle(self):
            result = super().run_cycle()
            if self.calls == 2:
                stop.set()
            return result

    cycle = StopAfterTwo()
    monkeypatch.setattr(runtime, "_POLL_SECONDS", 0)
    run_paper_gate(_settings(), stop=stop, health_root=tmp_path.resolve() / "health", cycle=cycle)
    assert cycle.calls == 2 and cycle.closed


def test_runtime_rejects_wrong_service_and_stop_boundary(tmp_path):
    with pytest.raises(PaperGateRuntimeError, match="configuration"):
        run_paper_gate(object(), once=True, health_root=tmp_path.resolve())
    with pytest.raises(TypeError, match="stop boundary"):
        run_paper_gate(_settings(), stop=object(), once=True, health_root=tmp_path.resolve())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"ready": True},
        {"monotonic_ns": 0, "pid": 1, "ready": True, "service": "paper-execution-worker"},
        {"monotonic_ns": True, "pid": 1, "ready": True, "service": "paper-execution-worker"},
        {"monotonic_ns": 10**25, "pid": 1, "ready": True, "service": "paper-execution-worker"},
    ],
)
def test_health_rejects_malformed_or_replayed_markers(tmp_path, payload):
    import json

    from adaptive_trader.platform.paper_gate_runtime import _record_health

    root = tmp_path.resolve() / "health"
    _record_health(root)
    (root / "paper-execution-worker.json").write_text(json.dumps(payload))
    assert not paper_gate_is_healthy(health_root=root)


def test_health_rejects_public_hardlinked_and_dead_process_markers(tmp_path, monkeypatch):
    import os

    from adaptive_trader.platform.paper_gate_runtime import _record_health

    root = tmp_path.resolve() / "health"
    _record_health(root)
    marker = root / "paper-execution-worker.json"
    marker.chmod(0o644)
    assert not paper_gate_is_healthy(health_root=root)
    marker.chmod(0o600)
    os.link(marker, root / "alias")
    assert not paper_gate_is_healthy(health_root=root)
    (root / "alias").unlink()

    def dead(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", dead)
    assert not paper_gate_is_healthy(health_root=root)


@pytest.mark.parametrize("failure", ["zero_write", "disk_error"])
def test_marker_write_failure_does_not_leave_partial_readiness(tmp_path, monkeypatch, failure):
    import adaptive_trader.platform.paper_gate_runtime as runtime

    def failed_write(*args):
        if failure == "zero_write":
            return 0
        raise OSError("simulated disk failure")

    monkeypatch.setattr(runtime.os, "write", failed_write)
    root = tmp_path.resolve() / "health"
    with pytest.raises(PaperGateRuntimeError):
        run_paper_gate(
            _settings(), once=True, stop=threading.Event(), health_root=root, cycle=_Cycle()
        )
    assert not paper_gate_is_healthy(health_root=root)
    assert not list(root.iterdir())


def test_stop_handlers_restore_process_signal_configuration():
    import signal

    from adaptive_trader.platform.paper_gate_runtime import _install_stop_handlers

    before = signal.getsignal(signal.SIGTERM)
    stop = threading.Event()
    restore = _install_stop_handlers(stop)
    try:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert stop.is_set()
    finally:
        restore()
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.parametrize("case", ["wrong_source_type", "expired", "future", "wrong_runtime"])
def test_candidate_trust_boundary_blocks_before_audit(case):
    from adaptive_trader.platform.paper_cycle import PaperCandidate
    from tests.unit.test_platform_signals import _context

    settings = _settings()
    context = _context(
        settings.platform.experiment.definition,
        provider_id="always_flat",
        execution_mode=ExecutionMode.OFFLINE if case == "wrong_runtime" else ExecutionMode.PAPER,
        broker_adapter=BrokerAdapter.FAKE
        if case == "wrong_runtime"
        else BrokerAdapter.ALPACA_PAPER,
    )
    signal = AlwaysFlatSignalProvider(clock=lambda: context.slot.ready_at).signal_for(context)
    candidate = PaperCandidate(signal, context)

    class Source:
        def current(self, *, observed_at):
            return object() if case == "wrong_source_type" else candidate

    class Recorder:
        def record(self, *args, **kwargs):
            raise AssertionError("untrusted candidate must not reach authorization evidence")

    now = signal.expires_at if case == "expired" else signal.created_at
    if case == "future":
        now -= timedelta(seconds=1)
    cycle = PaperExecutionCycle(
        settings, candidates=Source(), denials=Recorder(), clock=lambda: now
    )
    with pytest.raises((TypeError, ValueError)):
        cycle.run_cycle()


def test_execution_dependency_install_selects_only_locked_isolated_groups():
    import shlex

    dockerfile = (ROOT / "Dockerfile").read_text()
    builder = dockerfile.split("AS execution-builder\n", 1)[1].split(
        "FROM runtime-base AS execution\n", 1
    )[0]
    command = next(line for line in builder.splitlines() if line.startswith("RUN uv sync "))
    arguments = shlex.split(command)
    assert "--locked" in arguments and "--no-install-project" in arguments
    assert "--group" not in arguments  # uv rejects mixing --group with --only-group.
    assert [
        arguments[index + 1] for index, value in enumerate(arguments) if value == "--only-group"
    ] == ["execution-runtime", "market-data-runtime"]
