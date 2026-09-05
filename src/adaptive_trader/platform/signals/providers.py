"""Built-in providers, entry-point discovery, and append-once signal persistence."""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from sqlalchemy import Connection, Engine, Table, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Executable

from adaptive_trader.platform.config import BrokerAdapter, ExecutionMode
from adaptive_trader.platform.domain import (
    AuditPayload,
    AuditWriter,
    DeterministicId,
    require_finite_decimal,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.signals.models import (
    DecisionContext,
    SignalAction,
    SignalEnvelope,
    SignalSourceMode,
    SignalValidationError,
)
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import aqa_signal_envelopes
from adaptive_trader.platform.storage.transactions import SerializedTransactionCoordinator

SIGNAL_PROVIDER_ENTRY_POINT_GROUP = "autonomous_quant_agent.signal_providers"

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", flags=re.ASCII)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_REQUIRED_SIGNAL_COLUMNS = frozenset(
    {
        "contract_version",
        "signal_id",
        "slot_id",
        "correlation_id",
        "provider_id",
        "provider_version",
        "provider_source_mode",
        "experiment_id",
        "experiment_version",
        "experiment_hash",
        "data_contract_hash",
        "policy_hash",
        "source_bar_end",
        "created_at",
        "expires_at",
        "active_symbols",
        "availability_mask",
        "actions",
        "expected_edge_bps",
        "proposed_signed_target_inputs",
        "artifact_id",
        "artifact_hash",
        "promotable",
        "paper_submission_eligible",
        "content_hash",
    }
)


class ProviderDiscoveryError(RuntimeError):
    """Raised when a configured provider is unknown or ambiguously registered."""


class SignalSchemaError(RuntimeError):
    """Raised when persistence has not been migrated to the signal contract."""


class SignalPersistenceError(RuntimeError):
    """Raised when an immutable signal cannot be persisted safely."""


@runtime_checkable
class SignalProvider(Protocol):
    """Stable local extension contract with no broker or credential capability."""

    provider_id: str
    provider_version: str

    def signal_for(self, context: DecisionContext) -> SignalEnvelope:
        """Return only a declarative signal envelope."""


@runtime_checkable
class SignalPersistenceRecorder(Protocol):
    """Records signal persistence in the caller's atomic transaction."""

    def record(
        self,
        connection: Connection,
        *,
        envelope: SignalEnvelope,
    ) -> None:
        """Append immutable evidence for a newly inserted envelope."""


@dataclass(frozen=True, slots=True)
class FixtureSignalScenario:
    """Experiment fixture inputs kept outside generic provider algorithms."""

    first_slot_long_symbol: str
    first_slot_short_symbol: str
    expected_edge_bps: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.first_slot_long_symbol) is not str
            or type(self.first_slot_short_symbol) is not str
            or self.first_slot_long_symbol == self.first_slot_short_symbol
            or _SYMBOL.fullmatch(self.first_slot_long_symbol) is None
            or _SYMBOL.fullmatch(self.first_slot_short_symbol) is None
        ):
            raise SignalValidationError("fixture direction symbols must be distinct and normalized")
        try:
            edge = require_finite_decimal(self.expected_edge_bps, field_name="expected_edge_bps")
        except DomainValidationError:
            raise SignalValidationError("fixture edge must be an exact finite Decimal") from None
        if edge <= 0:
            raise SignalValidationError("fixture edge magnitude must be positive")


class AlwaysFlatSignalProvider:
    """Built-in provider which declaratively requests zero exposure."""

    provider_id = "always_flat"
    provider_version = "1"

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        if not callable(clock):
            raise TypeError("always-flat provider requires an injected clock")
        self._clock = clock

    def signal_for(self, context: DecisionContext) -> SignalEnvelope:
        count = len(context.active_symbols)
        return SignalEnvelope.create(
            context=context,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            provider_source_mode=SignalSourceMode.BUILTIN,
            created_at=self._clock(),
            availability_mask=(True,) * count,
            actions=(SignalAction.FLAT,) * count,
            expected_edge_bps=(None,) * count,
            proposed_signed_target_inputs=(Decimal(0),) * count,
            promotable=False,
            paper_submission_eligible=False,
        )


class OfflineFixtureSignalProvider:
    """Deterministic fake-broker fixture provider, permanently non-promotable."""

    provider_id = "deterministic_fixture"
    provider_version = "1"

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        scenario: FixtureSignalScenario,
    ) -> None:
        if not callable(clock):
            raise TypeError("fixture provider requires an injected clock")
        if type(scenario) is not FixtureSignalScenario:
            raise TypeError("fixture provider requires an immutable scenario")
        self._clock = clock
        self._scenario = scenario

    def signal_for(self, context: DecisionContext) -> SignalEnvelope:
        if (
            context.execution_mode is not ExecutionMode.OFFLINE
            or context.broker_adapter is not BrokerAdapter.FAKE
            or context.submission_enabled
        ):
            raise SignalValidationError(
                "fixture signals require offline mode, fake broker, and disabled submission"
            )
        required = {
            self._scenario.first_slot_long_symbol,
            self._scenario.first_slot_short_symbol,
        }
        if not required.issubset(context.active_symbols):
            raise SignalValidationError("fixture direction symbols must be active tradables")

        first_strategy_slot = context.strategy_slot_ordinal == 0
        actions: list[SignalAction] = []
        edges: list[Decimal | None] = []
        for symbol in context.active_symbols:
            if first_strategy_slot and symbol == self._scenario.first_slot_long_symbol:
                actions.append(SignalAction.LONG)
                edges.append(self._scenario.expected_edge_bps)
            elif first_strategy_slot and symbol == self._scenario.first_slot_short_symbol:
                actions.append(SignalAction.SHORT)
                edges.append(-self._scenario.expected_edge_bps)
            else:
                actions.append(SignalAction.FLAT)
                edges.append(None)
        count = len(context.active_symbols)
        return SignalEnvelope.create(
            context=context,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            provider_source_mode=SignalSourceMode.OFFLINE_FIXTURE,
            created_at=self._clock(),
            availability_mask=(True,) * count,
            actions=tuple(actions),
            expected_edge_bps=tuple(edges),
            proposed_signed_target_inputs=(None,) * count,
            artifact_id=None,
            artifact_hash=None,
            promotable=False,
            paper_submission_eligible=False,
        )


class _RegisteredProvider:
    """Validate a locally installed provider's output before it crosses the boundary."""

    def __init__(self, provider: SignalProvider) -> None:
        self._provider = provider
        self.provider_id = provider.provider_id
        self.provider_version = provider.provider_version

    def signal_for(self, context: DecisionContext) -> SignalEnvelope:
        result = self._provider.signal_for(context)
        if type(result) is not SignalEnvelope:
            raise SignalValidationError("registered provider must return a SignalEnvelope")
        if (
            result.provider_id != self.provider_id
            or result.provider_version != self.provider_version
            or result.provider_source_mode is not SignalSourceMode.REGISTERED_PLUGIN
        ):
            raise SignalValidationError("registered provider output has invalid provenance")
        return result.validate_for(context)


class SignalProviderRegistry:
    """Immutable provider registry populated only from built-ins and one entry-point group."""

    def __init__(self, providers: Mapping[str, SignalProvider]) -> None:
        if not isinstance(providers, Mapping) or not providers:
            raise ProviderDiscoveryError("signal provider registry cannot be empty")
        validated: dict[str, SignalProvider] = {}
        for provider_id, provider in providers.items():
            _validate_provider(provider, registered_id=provider_id)
            if provider_id in validated:
                raise ProviderDiscoveryError("signal provider ID is registered more than once")
            validated[provider_id] = provider
        self._providers = MappingProxyType(dict(sorted(validated.items())))

    @classmethod
    def discover(
        cls,
        *,
        clock: Callable[[], datetime],
        fixture_scenario: FixtureSignalScenario,
    ) -> SignalProviderRegistry:
        """Load only the fixed packaging entry-point group plus safe built-ins."""

        if type(fixture_scenario) is not FixtureSignalScenario:
            raise ProviderDiscoveryError("provider registry requires an offline fixture scenario")
        fixture = OfflineFixtureSignalProvider(clock=clock, scenario=fixture_scenario)
        providers: dict[str, SignalProvider] = {
            AlwaysFlatSignalProvider.provider_id: AlwaysFlatSignalProvider(clock=clock),
            fixture.provider_id: fixture,
        }

        try:
            entry_points = importlib.metadata.entry_points(group=SIGNAL_PROVIDER_ENTRY_POINT_GROUP)
        except Exception:
            raise ProviderDiscoveryError(
                "signal provider entry points could not be inspected"
            ) from None
        for entry_point in sorted(entry_points, key=lambda item: (item.name, item.value)):
            if _IDENTIFIER.fullmatch(entry_point.name) is None:
                raise ProviderDiscoveryError("signal provider entry-point name is invalid")
            if entry_point.name in providers:
                raise ProviderDiscoveryError("signal provider ID is registered more than once")
            try:
                loaded = entry_point.load()
                candidate = loaded() if isinstance(loaded, type) else loaded
            except Exception:
                raise ProviderDiscoveryError(
                    "registered signal provider could not be loaded"
                ) from None
            if not isinstance(candidate, SignalProvider):
                raise ProviderDiscoveryError("registered object is not a signal provider")
            _validate_provider(candidate, registered_id=entry_point.name)
            providers[entry_point.name] = _RegisteredProvider(candidate)
        return cls(providers)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        """Return registered IDs in stable order."""

        return tuple(self._providers)

    def select(self, provider_id: str) -> SignalProvider:
        """Select only a pre-registered ID; module paths and source strings are never accepted."""

        if type(provider_id) is not str or _IDENTIFIER.fullmatch(provider_id) is None:
            raise ProviderDiscoveryError("configured signal provider ID is invalid")
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ProviderDiscoveryError(
                "configured signal provider ID is not registered"
            ) from None


class AuditSignalPersistenceRecorder:
    """Write strategy-scoped hash-chained evidence for a new signal."""

    def __init__(self, engine: Engine) -> None:
        self._audit = AuditRepository(engine, writer=AuditWriter.STRATEGY)

    def record(self, connection: Connection, *, envelope: SignalEnvelope) -> None:
        self._audit.append(
            stream_id=f"aqa_strategy:{envelope.signal_id}",
            event_type="signal.persisted",
            occurred_at=envelope.created_at,
            payload=AuditPayload.from_mapping(
                {
                    "content_hash": envelope.content_hash,
                    "idempotency_key": DeterministicId.from_hash_input(
                        prefix="signal_persist",
                        hash_input=("signal-persist-v1", envelope.signal_id),
                    ).value,
                    "signal_id": envelope.signal_id,
                    "slot_id": envelope.slot_id,
                }
            ),
            connection=connection,
        )


class SignalEnvelopeRepository:
    """Append each slot's validated envelope exactly once with concurrency-safe idempotency."""

    def __init__(
        self,
        engine: Engine,
        *,
        persistence_recorder: SignalPersistenceRecorder | None = None,
        table: Table = aqa_signal_envelopes,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("signal repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("signal repository requires PostgreSQL or SQLite")
        if not isinstance(table, Table):
            raise TypeError("signal repository requires a concrete SQLAlchemy Table")
        if _REQUIRED_SIGNAL_COLUMNS.difference(table.c.keys()):
            raise SignalSchemaError("signal relation is missing the Phase 4 contract")
        selected_recorder = persistence_recorder or AuditSignalPersistenceRecorder(engine)
        if not isinstance(selected_recorder, SignalPersistenceRecorder):
            raise TypeError("signal repository requires a transactional persistence recorder")
        self._engine = engine
        self._table = table
        self._recorder = selected_recorder
        self._transactions = SerializedTransactionCoordinator(engine)

    def transaction(self) -> AbstractContextManager[Connection]:
        """Open a repository-owned serialized transaction."""

        return self._transactions.transaction()

    def persist_once(
        self,
        envelope: SignalEnvelope,
        *,
        context: DecisionContext,
        connection: Connection | None = None,
    ) -> SignalEnvelope:
        """Insert one envelope or return the byte-equivalent durable retry."""

        if type(envelope) is not SignalEnvelope:
            raise SignalValidationError("signal persistence requires an immutable envelope")
        envelope.validate_for(context)
        if connection is not None:
            self._transactions.validate_connection(connection, require_serialized_sqlite=True)
            return self._persist_on_connection(connection, envelope)
        try:
            with self.transaction() as owned_connection:
                return self._persist_on_connection(owned_connection, envelope)
        except (SignalPersistenceError, SignalValidationError):
            raise
        except SQLAlchemyError:
            raise SignalPersistenceError("signal envelope could not be persisted") from None

    def get_for_slot(
        self,
        slot_id: str,
        *,
        connection: Connection | None = None,
    ) -> SignalEnvelope | None:
        """Return the immutable envelope materialized for a slot."""

        if type(slot_id) is not str or not slot_id.startswith("slot_"):
            raise SignalPersistenceError("slot ID is invalid")
        statement = select(self._table).where(self._table.c.slot_id == slot_id)
        try:
            if connection is not None:
                self._transactions.validate_connection(connection, require_serialized_sqlite=False)
                row = connection.execute(statement).mappings().one_or_none()
            else:
                with self._engine.begin() as owned_connection:
                    row = owned_connection.execute(statement).mappings().one_or_none()
            return None if row is None else _envelope_from_row(row)
        except SignalValidationError:
            raise SignalPersistenceError("persisted signal envelope is malformed") from None
        except SQLAlchemyError:
            raise SignalPersistenceError("signal envelope could not be read") from None

    def _persist_on_connection(
        self,
        connection: Connection,
        envelope: SignalEnvelope,
    ) -> SignalEnvelope:
        existing = self.get_for_slot(envelope.slot_id, connection=connection)
        if existing is not None:
            if existing == envelope:
                return existing
            raise SignalPersistenceError("slot already has a different signal envelope")
        statement = _conflict_safe_insert(
            dialect=connection.dialect.name,
            table=self._table,
            values=_envelope_row(envelope),
        )
        result = connection.execute(statement)
        if result.rowcount == 0:
            raced = self.get_for_slot(envelope.slot_id, connection=connection)
            if raced == envelope:
                return envelope
            raise SignalPersistenceError("slot signal insertion conflicted with durable state")
        self._recorder.record(connection, envelope=envelope)
        return envelope


def _conflict_safe_insert(*, dialect: str, table: Table, values: dict[str, object]) -> Executable:
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as postgresql_insert

        return (
            postgresql_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[table.c.slot_id])
        )
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[table.c.slot_id])
        )
    return insert(table).values(**values)


def _envelope_row(envelope: SignalEnvelope) -> dict[str, object]:
    return {
        "contract_version": envelope.contract_version,
        "signal_id": envelope.signal_id,
        "slot_id": envelope.slot_id,
        "correlation_id": envelope.correlation_id,
        "provider_id": envelope.provider_id,
        "provider_version": envelope.provider_version,
        "provider_source_mode": envelope.provider_source_mode.value,
        "experiment_id": envelope.experiment_id,
        "experiment_version": envelope.experiment_version,
        "experiment_hash": envelope.experiment_hash,
        "data_contract_hash": envelope.data_contract_hash,
        "policy_hash": envelope.policy_hash,
        "source_bar_end": envelope.source_bar_end,
        "created_at": envelope.created_at,
        "expires_at": envelope.expires_at,
        "active_symbols": list(envelope.active_symbols),
        "availability_mask": list(envelope.availability_mask),
        "actions": [action.value for action in envelope.actions],
        "expected_edge_bps": [
            None if edge is None else format(edge, "f") for edge in envelope.expected_edge_bps
        ],
        "proposed_signed_target_inputs": [
            None if target is None else format(target, "f")
            for target in envelope.proposed_signed_target_inputs
        ],
        "artifact_id": envelope.artifact_id,
        "artifact_hash": envelope.artifact_hash,
        "promotable": envelope.promotable,
        "paper_submission_eligible": envelope.paper_submission_eligible,
        "content_hash": envelope.content_hash,
    }


def _envelope_from_row(row: RowMapping) -> SignalEnvelope:
    return SignalEnvelope(
        contract_version=row["contract_version"],
        signal_id=row["signal_id"],
        slot_id=row["slot_id"],
        correlation_id=row["correlation_id"],
        provider_id=row["provider_id"],
        provider_version=row["provider_version"],
        provider_source_mode=SignalSourceMode(row["provider_source_mode"]),
        experiment_id=row["experiment_id"],
        experiment_version=row["experiment_version"],
        experiment_hash=row["experiment_hash"],
        data_contract_hash=row["data_contract_hash"],
        policy_hash=row["policy_hash"],
        source_bar_end=row["source_bar_end"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        active_symbols=tuple(row["active_symbols"]),
        availability_mask=tuple(row["availability_mask"]),
        actions=tuple(SignalAction(value) for value in row["actions"]),
        expected_edge_bps=tuple(
            None if value is None else Decimal(value) for value in row["expected_edge_bps"]
        ),
        proposed_signed_target_inputs=tuple(
            None if value is None else Decimal(value)
            for value in row["proposed_signed_target_inputs"]
        ),
        artifact_id=row["artifact_id"],
        artifact_hash=row["artifact_hash"],
        promotable=row["promotable"],
        paper_submission_eligible=row["paper_submission_eligible"],
        content_hash=row["content_hash"],
    )


def _validate_provider(provider: object, *, registered_id: str) -> SignalProvider:
    if not isinstance(provider, SignalProvider):
        raise ProviderDiscoveryError("registered object is not a signal provider")
    if (
        type(provider.provider_id) is not str
        or _IDENTIFIER.fullmatch(provider.provider_id) is None
        or provider.provider_id != registered_id
    ):
        raise ProviderDiscoveryError("signal provider identity does not match its registration")
    if (
        type(provider.provider_version) is not str
        or _VERSION.fullmatch(provider.provider_version) is None
    ):
        raise ProviderDiscoveryError("signal provider version is invalid")
    return provider


__all__ = [
    "SIGNAL_PROVIDER_ENTRY_POINT_GROUP",
    "AlwaysFlatSignalProvider",
    "AuditSignalPersistenceRecorder",
    "FixtureSignalScenario",
    "OfflineFixtureSignalProvider",
    "ProviderDiscoveryError",
    "SignalEnvelopeRepository",
    "SignalPersistenceError",
    "SignalPersistenceRecorder",
    "SignalProvider",
    "SignalProviderRegistry",
    "SignalSchemaError",
]
