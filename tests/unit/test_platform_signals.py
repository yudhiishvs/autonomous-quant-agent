"""Strict signal-envelope, provider, discovery, and persistence tests."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
)

from adaptive_trader.platform.config import (
    BrokerAdapter,
    ExecutionMode,
    ExperimentDefinition,
    load_experiment,
)
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.scheduling import DecisionType, build_session_schedule
from adaptive_trader.platform.signals import (
    SIGNAL_PROVIDER_ENTRY_POINT_GROUP,
    AlwaysFlatSignalProvider,
    DecisionContext,
    FixtureSignalScenario,
    OfflineFixtureSignalProvider,
    PaperAuthorizationReason,
    ProviderDiscoveryError,
    SignalAction,
    SignalEnvelope,
    SignalEnvelopeRepository,
    SignalPersistenceError,
    SignalProviderRegistry,
    SignalSchemaError,
    SignalSourceMode,
    SignalValidationError,
    verify_paper_authorization,
)
from adaptive_trader.platform.storage.tables import UTCDateTime

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "configs"
_SESSION_DATE = date(2026, 7, 6)
_DATA_HASH = sha256_hex(("canonical-data-contract", 1))
_POLICY_HASH = sha256_hex(("signed-risk-policy", 1))


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_CONFIG_ROOT,
    )


def _context(
    experiment: ExperimentDefinition,
    *,
    provider_id: str = "deterministic_fixture",
    ordinal: int | None = 0,
    forced_flat: bool = False,
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE,
    broker_adapter: BrokerAdapter = BrokerAdapter.FAKE,
    submission_enabled: bool = False,
) -> DecisionContext:
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id=provider_id,
        signal_provider_version="1",
        session_date=_SESSION_DATE,
        calendar=XnasExchangeCalendar(),
    )
    slot = schedule.forced_flat_slot if forced_flat else schedule.strategy_slots[ordinal or 0]
    assert slot is not None
    return DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=_DATA_HASH,
        policy_hash=_POLICY_HASH,
        execution_mode=execution_mode,
        broker_adapter=broker_adapter,
        submission_enabled=submission_enabled,
        strategy_slot_ordinal=None if forced_flat else ordinal,
    )


def _fixture_provider(context: DecisionContext, *, offset_seconds: int = 0):
    return OfflineFixtureSignalProvider(
        clock=lambda: context.slot.ready_at + timedelta(seconds=offset_seconds),
        scenario=FixtureSignalScenario(
            first_slot_long_symbol="NVDA",
            first_slot_short_symbol="AMD",
            expected_edge_bps=Decimal("25"),
        ),
    )


def _rehash(envelope: SignalEnvelope, **changes: object) -> SignalEnvelope:
    payload = {
        field: changes.get(field, getattr(envelope, field))
        for field in envelope.__dataclass_fields__
        if field not in {"signal_id", "content_hash"}
    }
    digest = sha256_hex(payload)
    return replace(
        envelope,
        **changes,  # type: ignore[arg-type]
        signal_id=f"signal_{digest}",
        content_hash=digest,
    )


def test_always_flat_provider_emits_exact_active_set_and_zero_targets(
    experiment: ExperimentDefinition,
) -> None:
    context = _context(experiment, provider_id="always_flat")
    provider = AlwaysFlatSignalProvider(clock=lambda: context.slot.ready_at)

    envelope = provider.signal_for(context)

    assert envelope.active_symbols == experiment.active_tradable
    assert envelope.availability_mask == (True,) * 8
    assert envelope.actions == (SignalAction.FLAT,) * 8
    assert envelope.expected_edge_bps == (None,) * 8
    assert envelope.proposed_signed_target_inputs == (Decimal(0),) * 8
    assert envelope.provider_source_mode is SignalSourceMode.BUILTIN
    assert not envelope.promotable
    assert not envelope.paper_submission_eligible


def test_offline_fixture_emits_exact_first_slot_and_later_flat_signals(
    experiment: ExperimentDefinition,
) -> None:
    first_context = _context(experiment, ordinal=0)
    first = _fixture_provider(first_context).signal_for(first_context)

    assert first.action_for("NVDA") is SignalAction.LONG
    assert first.edge_for("NVDA") == Decimal("25")
    assert first.action_for("AMD") is SignalAction.SHORT
    assert first.edge_for("AMD") == Decimal("-25")
    for symbol in set(experiment.active_tradable).difference({"NVDA", "AMD"}):
        assert first.action_for(symbol) is SignalAction.FLAT
        assert first.edge_for(symbol) is None
    assert first.provider_source_mode is SignalSourceMode.OFFLINE_FIXTURE
    assert first.artifact_id is first.artifact_hash is None
    assert not first.promotable
    assert not first.paper_submission_eligible

    later_context = _context(experiment, ordinal=1)
    later = _fixture_provider(later_context).signal_for(later_context)
    forced_context = _context(experiment, ordinal=None, forced_flat=True)
    forced = _fixture_provider(forced_context).signal_for(forced_context)
    assert later.actions == (SignalAction.FLAT,) * 8
    assert forced.actions == (SignalAction.FLAT,) * 8
    assert forced_context.slot.decision_type is DecisionType.FORCED_FLAT


@pytest.mark.parametrize(
    ("mode", "broker", "submission"),
    [
        (ExecutionMode.SHADOW, BrokerAdapter.NONE, False),
        (ExecutionMode.PAPER, BrokerAdapter.ALPACA_PAPER, False),
    ],
)
def test_offline_fixture_rejects_every_external_or_submission_capable_context(
    experiment: ExperimentDefinition,
    mode: ExecutionMode,
    broker: BrokerAdapter,
    submission: bool,
) -> None:
    context = _context(
        experiment,
        execution_mode=mode,
        broker_adapter=broker,
        submission_enabled=submission,
    )
    with pytest.raises(SignalValidationError, match="offline mode"):
        _fixture_provider(context).signal_for(context)


def test_signal_is_frozen_deterministic_and_rejects_tampering(
    experiment: ExperimentDefinition,
) -> None:
    context = _context(experiment)
    first = _fixture_provider(context).signal_for(context)
    replay = _fixture_provider(context).signal_for(context)

    assert first == replay
    assert first.signal_id == f"signal_{first.content_hash}"
    assert first.signal_id == (
        "signal_57c1171b4f79e42dcdac3c5f7c4f14f2477fdd2fb07f45de9d7939b3273d4d91"
    )
    with pytest.raises(FrozenInstanceError):
        first.provider_id = "changed"
    with pytest.raises(SignalValidationError, match="SHORT"):
        replace(
            first,
            expected_edge_bps=tuple(
                Decimal("25") if symbol == "AMD" else first.edge_for(symbol)
                for symbol in first.active_symbols
            ),
        )
    with pytest.raises(SignalValidationError, match="finite"):
        replace(
            first,
            expected_edge_bps=tuple(
                Decimal("NaN") if symbol == "AMD" else first.edge_for(symbol)
                for symbol in first.active_symbols
            ),
        )
    with pytest.raises(TypeError):
        replace(first, unknown_field=True)


def test_context_validation_rejects_wrong_symbols_hashes_and_deadlines(
    experiment: ExperimentDefinition,
) -> None:
    context = _context(experiment)
    envelope = _fixture_provider(context).signal_for(context)

    with pytest.raises(SignalValidationError, match="exact active-symbol"):
        _rehash(envelope, active_symbols=(*envelope.active_symbols[:-1], "SPY")).validate_for(
            context
        )
    with pytest.raises(SignalValidationError, match="data-contract hash"):
        _rehash(envelope, data_contract_hash="0" * 64).validate_for(context)
    with pytest.raises(SignalValidationError, match="deadline"):
        _rehash(
            envelope,
            created_at=context.slot.ready_at - timedelta(seconds=1),
        ).validate_for(context)


def test_paper_authorization_is_unconditionally_default_deny(
    experiment: ExperimentDefinition,
) -> None:
    context = _context(experiment)
    envelope = _fixture_provider(context).signal_for(context)

    authorization = verify_paper_authorization(envelope, context=context)

    assert authorization.approved is False
    assert authorization.reason is PaperAuthorizationReason.MODEL_APPROVAL_NOT_IMPLEMENTED
    assert authorization.reason.value == "model_approval_not_implemented"


class _EntryPoint:
    name = "local_provider"
    value = "installed_distribution:Provider"

    def __init__(self, provider: object) -> None:
        self._provider = provider

    def load(self) -> object:
        return self._provider


class _LocalProvider:
    provider_id = "local_provider"
    provider_version = "1"

    def signal_for(self, context: DecisionContext) -> SignalEnvelope:
        count = len(context.active_symbols)
        return SignalEnvelope.create(
            context=context,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            provider_source_mode=SignalSourceMode.REGISTERED_PLUGIN,
            created_at=context.slot.ready_at,
            availability_mask=(True,) * count,
            actions=(SignalAction.FLAT,) * count,
            expected_edge_bps=(None,) * count,
            proposed_signed_target_inputs=(Decimal(0),) * count,
        )


def test_registry_discovers_only_the_fixed_entry_point_group_and_registered_ids(
    monkeypatch: pytest.MonkeyPatch,
    experiment: ExperimentDefinition,
) -> None:
    calls: list[dict[str, object]] = []

    def entry_points(**kwargs: object) -> tuple[_EntryPoint, ...]:
        calls.append(kwargs)
        return (_EntryPoint(_LocalProvider()),)

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    scenario = FixtureSignalScenario("NVDA", "AMD", Decimal("25"))
    registry = SignalProviderRegistry.discover(
        clock=lambda: datetime(2026, 7, 6, 14, 0, tzinfo=UTC),
        fixture_scenario=scenario,
    )

    assert calls == [{"group": SIGNAL_PROVIDER_ENTRY_POINT_GROUP}]
    assert registry.provider_ids == ("always_flat", "deterministic_fixture", "local_provider")
    context = _context(experiment, provider_id="local_provider")
    assert registry.select("local_provider").signal_for(context).actions == (SignalAction.FLAT,) * 8
    for unregistered in (
        "package.module:Provider",
        "package.module.Provider",
        "https://example.invalid/provider",
        "unknown_provider",
    ):
        with pytest.raises(ProviderDiscoveryError):
            registry.select(unregistered)


def test_registry_rejects_duplicate_and_mismatched_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **kwargs: (_EntryPoint(AlwaysFlatSignalProvider(clock=lambda: datetime.now(UTC))),),
    )
    with pytest.raises(ProviderDiscoveryError, match="identity"):
        SignalProviderRegistry.discover(
            clock=lambda: datetime.now(UTC),
            fixture_scenario=FixtureSignalScenario("NVDA", "AMD", Decimal("25")),
        )


def _signal_table(metadata: MetaData) -> Table:
    return Table(
        "phase4_signal_envelopes",
        metadata,
        Column("contract_version", Integer, nullable=False),
        Column("signal_id", String(128), primary_key=True),
        Column("slot_id", String(128), nullable=False, unique=True),
        Column("correlation_id", String(128), nullable=False),
        Column("provider_id", String(64), nullable=False),
        Column("provider_version", String(64), nullable=False),
        Column("provider_source_mode", String(32), nullable=False),
        Column("experiment_id", String(64), nullable=False),
        Column("experiment_version", BigInteger, nullable=False),
        Column("experiment_hash", String(64), nullable=False),
        Column("data_contract_hash", String(64), nullable=False),
        Column("policy_hash", String(64), nullable=False),
        Column("source_bar_end", UTCDateTime(), nullable=False),
        Column("created_at", UTCDateTime(), nullable=False),
        Column("expires_at", UTCDateTime(), nullable=False),
        Column("active_symbols", JSON, nullable=False),
        Column("availability_mask", JSON, nullable=False),
        Column("actions", JSON, nullable=False),
        Column("expected_edge_bps", JSON, nullable=False),
        Column("proposed_signed_target_inputs", JSON, nullable=False),
        Column("artifact_id", String(128)),
        Column("artifact_hash", String(64)),
        Column("promotable", Boolean, nullable=False),
        Column("paper_submission_eligible", Boolean, nullable=False),
        Column("content_hash", String(64), nullable=False, unique=True),
    )


class _SignalRecorder:
    def __init__(self) -> None:
        self.signal_ids: list[str] = []

    def record(self, connection: object, *, envelope: SignalEnvelope) -> None:
        del connection
        self.signal_ids.append(envelope.signal_id)


@pytest.fixture
def signal_repository(
    tmp_path: Path,
) -> Iterator[tuple[SignalEnvelopeRepository, _SignalRecorder]]:
    metadata = MetaData()
    table = _signal_table(metadata)
    engine: Engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'signals.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(engine)
    recorder = _SignalRecorder()
    try:
        yield (
            SignalEnvelopeRepository(
                engine,
                table=table,
                persistence_recorder=recorder,
            ),
            recorder,
        )
    finally:
        engine.dispose()


def test_signal_repository_is_append_once_and_round_trips_exact_decimals(
    signal_repository: tuple[SignalEnvelopeRepository, _SignalRecorder],
    experiment: ExperimentDefinition,
) -> None:
    repository, recorder = signal_repository
    context = _context(experiment)
    envelope = _fixture_provider(context).signal_for(context)

    assert repository.persist_once(envelope, context=context) == envelope
    assert repository.persist_once(envelope, context=context) == envelope
    assert repository.get_for_slot(envelope.slot_id) == envelope
    assert recorder.signal_ids == [envelope.signal_id]

    changed = _fixture_provider(context, offset_seconds=1).signal_for(context)
    with pytest.raises(SignalPersistenceError, match="different"):
        repository.persist_once(changed, context=context)
    assert repository.get_for_slot(envelope.slot_id) == envelope


def test_concurrent_signal_retries_materialize_one_envelope(
    signal_repository: tuple[SignalEnvelopeRepository, _SignalRecorder],
    experiment: ExperimentDefinition,
) -> None:
    repository, recorder = signal_repository
    context = _context(experiment)
    envelope = _fixture_provider(context).signal_for(context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda _: repository.persist_once(envelope, context=context), range(2))
        )

    assert results == (envelope, envelope)
    assert recorder.signal_ids == [envelope.signal_id]


def test_signal_repository_refuses_a_legacy_schema(tmp_path: Path) -> None:
    metadata = MetaData()
    legacy = Table("legacy_signals", metadata, Column("signal_id", String(128), primary_key=True))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy-signal.sqlite3'}")
    metadata.create_all(engine)
    try:
        with pytest.raises(SignalSchemaError, match="Phase 4"):
            SignalEnvelopeRepository(
                engine,
                table=legacy,
                persistence_recorder=_SignalRecorder(),
            )
    finally:
        engine.dispose()
