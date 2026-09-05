"""Atomic append-only persistence for canonical market-bar revisions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, Engine, and_, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.platform.constants import MAX_SIGNED_64_BIT_INTEGER
from adaptive_trader.platform.domain import (
    AuditPayload,
    AuditWriter,
    require_finite_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import (
    AuditPersistenceError,
    AuditValidationError,
    DomainValidationError,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_symbol_watermarks,
)
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
    TransactionBoundaryError,
    TransactionViolation,
)

_SUPPORTED_DIALECTS = frozenset({"postgresql", "sqlite"})
_TIMEFRAME_DURATIONS = {"1Min": timedelta(minutes=1), "15Min": timedelta(minutes=15)}
_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]*$", flags=re.ASCII)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_HASH = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_EVENT_ID = re.compile(r"^bar_event_[0-9a-f]{64}$", flags=re.ASCII)
_IDENTITY_ID = re.compile(r"^bar_identity_[0-9a-f]{64}$", flags=re.ASCII)
_WATERMARK_ID = re.compile(r"^symbol_watermark_[0-9a-f]{64}$", flags=re.ASCII)
_QUALITY_FLAG = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", flags=re.ASCII)

_MAX_QUALITY_FLAGS = 64


class MarketDataValidationError(DomainValidationError):
    """Raised when a market-data value cannot enter persistence safely."""


class MarketDataIntegrityError(RuntimeError):
    """Raised when durable market-data state violates repository invariants."""


class MarketDataPersistenceError(RuntimeError):
    """Raised when a market-data transaction cannot complete safely."""


class BarWriteStatus(StrEnum):
    """Outcome of comparing a normalized payload with the current effective revision."""

    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    CORRECTED = "CORRECTED"


@dataclass(frozen=True, slots=True)
class BarIdentity:
    """Canonical identity and interval for one start-inclusive, end-exclusive bar."""

    provider: str
    feed: str
    adjustment: str
    symbol: str
    timeframe: str
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        _require_token(self.provider, field_name="provider", maximum_length=32)
        _require_token(self.feed, field_name="feed", maximum_length=16)
        _require_token(self.adjustment, field_name="adjustment", maximum_length=16)
        if (
            type(self.symbol) is not str
            or len(self.symbol) > 10
            or _SYMBOL.fullmatch(self.symbol) is None
        ):
            raise MarketDataValidationError("bar symbol is invalid")
        duration = _TIMEFRAME_DURATIONS.get(self.timeframe)
        if duration is None:
            raise MarketDataValidationError("bar timeframe is unsupported")
        try:
            start_at = require_utc_instant(self.start_at, field_name="start_at")
            end_at = require_utc_instant(self.end_at, field_name="end_at")
        except DomainValidationError:
            raise MarketDataValidationError("bar interval must use UTC instants") from None
        if end_at - start_at != duration:
            raise MarketDataValidationError("bar interval does not match its timeframe")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)

    @property
    def hash_input(self) -> tuple[str, str, str, str, str, datetime]:
        """Return the exact identity tuple required by the platform contract."""

        return (
            self.provider,
            self.feed,
            self.adjustment,
            self.symbol,
            self.timeframe,
            self.start_at,
        )

    @property
    def identity_hash(self) -> str:
        return sha256_hex(self.hash_input)

    @property
    def bar_identity_id(self) -> str:
        return f"bar_identity_{self.identity_hash}"


@dataclass(frozen=True, slots=True)
class BarWrite:
    """Validated normalized payload presented to the append transaction.

    Receipt and raw-source provenance are deliberately excluded from ``normalized_payload_hash``
    so replaying the same normalized provider event is idempotent. The hash otherwise matches the
    canonical-bar semantic payload exactly, including source identity and correction metadata. A
    persisted event has a separate content hash covering that semantic hash and all immutable
    storage provenance.
    """

    identity: BarIdentity
    received_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quality_flags: tuple[str, ...]
    source: str
    source_payload_hash: str
    source_mode: str = "offline_fixture"
    lineage_hash: str | None = None
    trade_count: int | None = None
    vwap: Decimal | None = None
    provider_timestamp: datetime | None = None
    source_event_id: str | None = None
    is_correction: bool = False
    correction_of_source_event_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.identity) is not BarIdentity:
            raise MarketDataValidationError("bar identity is invalid")
        try:
            received_at = require_utc_instant(self.received_at, field_name="received_at")
        except DomainValidationError:
            raise MarketDataValidationError("bar receipt timestamp must be UTC") from None
        if received_at < self.identity.end_at:
            raise MarketDataValidationError("bar receipt timestamp precedes interval end")
        object.__setattr__(self, "received_at", received_at)

        if self.provider_timestamp is not None:
            try:
                provider_timestamp = require_utc_instant(
                    self.provider_timestamp,
                    field_name="provider_timestamp",
                )
            except DomainValidationError:
                raise MarketDataValidationError("bar provider timestamp must be UTC") from None
            object.__setattr__(self, "provider_timestamp", provider_timestamp)

        prices = {
            field_name: _require_storage_decimal(getattr(self, field_name), field_name=field_name)
            for field_name in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in prices.values()):
            raise MarketDataValidationError("bar prices must be positive")
        if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
            raise MarketDataValidationError("bar OHLC values are incoherent")
        for field_name, value in prices.items():
            object.__setattr__(self, field_name, value)

        volume = _require_storage_decimal(self.volume, field_name="volume", scale=0)
        if volume < 0:
            raise MarketDataValidationError("bar volume must be nonnegative")
        object.__setattr__(self, "volume", volume)
        if self.trade_count is not None and (
            type(self.trade_count) is not int
            or self.trade_count < 0
            or self.trade_count > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise MarketDataValidationError("bar trade count is invalid")
        if self.vwap is not None:
            vwap = _require_storage_decimal(self.vwap, field_name="vwap")
            if vwap < 0:
                raise MarketDataValidationError("bar VWAP must be nonnegative")
            object.__setattr__(self, "vwap", vwap)

        if type(self.schema_version) is not int or self.schema_version != 1:
            raise MarketDataValidationError("bar schema version is unsupported")
        _require_quality_flags(self.quality_flags)
        _require_token(self.source, field_name="source", maximum_length=32)
        if self.source_mode not in {"external_provider", "offline_fixture"}:
            raise MarketDataValidationError("bar source mode is invalid")
        _require_hash(self.source_payload_hash, field_name="source payload hash")
        if self.lineage_hash is not None:
            _require_hash(self.lineage_hash, field_name="lineage hash")
        if self.source_event_id is not None:
            _require_opaque_text(
                self.source_event_id,
                field_name="source event ID",
                maximum_length=128,
            )
        if type(self.is_correction) is not bool:
            raise MarketDataValidationError("bar correction flag must be boolean")
        if self.correction_of_source_event_id is not None:
            _require_opaque_text(
                self.correction_of_source_event_id,
                field_name="correction source event ID",
                maximum_length=128,
            )
            if not self.is_correction:
                raise MarketDataValidationError(
                    "bar correction reference requires correction metadata"
                )
            if self.correction_of_source_event_id == self.source_event_id:
                raise MarketDataValidationError("bar correction cannot reference itself")

    @property
    def normalized_payload_hash(self) -> str:
        """Hash canonical semantics, excluding receipt and raw-source delivery metadata."""

        payload: dict[str, Any] = {
            "adjustment": self.identity.adjustment,
            "close": self.close,
            "correction": {
                "correction_of_source_event_id": self.correction_of_source_event_id,
                "is_correction": self.is_correction,
            },
            "feed": self.identity.feed,
            "high": self.high,
            "interval_end_utc": self.identity.end_at,
            "interval_start_utc": self.identity.start_at,
            "low": self.low,
            "open": self.open,
            "provider": self.identity.provider,
            "provider_event_timestamp_utc": self.provider_timestamp,
            "quality_flags": self.quality_flags,
            "schema_version": self.schema_version,
            "source_event_id": self.source_event_id,
            "source_mode": self.source_mode,
            "symbol": self.identity.symbol,
            "timeframe": self.identity.timeframe,
            "trade_count": self.trade_count,
            "volume": self.volume,
            "vwap": self.vwap,
        }
        if self.lineage_hash is not None:
            payload["aggregate_lineage_hash"] = self.lineage_hash
        return sha256_hex(payload)


@dataclass(frozen=True, slots=True)
class StoredBarEvent:
    """One independently validated immutable revision returned by the repository."""

    bar_event_id: str
    revision: int
    correction_of_event_id: str | None
    bar: BarWrite
    content_hash: str

    def __post_init__(self) -> None:
        if type(self.bar) is not BarWrite:
            raise MarketDataIntegrityError("persisted bar payload is invalid")
        if (
            type(self.revision) is not int
            or self.revision < 1
            or self.revision > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise MarketDataIntegrityError("persisted bar revision is invalid")
        if self.revision == 1:
            if self.correction_of_event_id is not None:
                raise MarketDataIntegrityError("first bar revision cannot be a correction")
        elif (
            type(self.correction_of_event_id) is not str
            or _EVENT_ID.fullmatch(self.correction_of_event_id) is None
        ):
            raise MarketDataIntegrityError("bar correction reference is invalid")
        expected_id = _bar_event_id(
            self.bar.identity.bar_identity_id,
            self.revision,
            self.bar.normalized_payload_hash,
        )
        if self.bar_event_id != expected_id:
            raise MarketDataIntegrityError("persisted bar event identity is invalid")
        if type(self.content_hash) is not str or _HASH.fullmatch(self.content_hash) is None:
            raise MarketDataIntegrityError("persisted bar event content hash is invalid")
        if self.content_hash != _bar_event_content_hash(
            bar_event_id=self.bar_event_id,
            revision=self.revision,
            correction_of_event_id=self.correction_of_event_id,
            bar=self.bar,
        ):
            raise MarketDataIntegrityError("persisted bar event content hash is invalid")

    @property
    def identity(self) -> BarIdentity:
        return self.bar.identity

    @property
    def normalized_payload_hash(self) -> str:
        return self.bar.normalized_payload_hash


@dataclass(frozen=True, slots=True)
class EligibleWatermark:
    """Caller assertion that this bar is quality-approved for contiguous readiness."""

    experiment_hash: str
    quality_hash: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_hash(self.experiment_hash, field_name="experiment hash")
        _require_hash(self.quality_hash, field_name="quality hash")
        try:
            updated_at = require_utc_instant(self.updated_at, field_name="updated_at")
        except DomainValidationError:
            raise MarketDataValidationError("watermark timestamp must be UTC") from None
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class SymbolWatermark:
    """Validated durable contiguous-readiness state for one experiment series."""

    symbol_watermark_id: str
    experiment_hash: str
    provider: str
    feed: str
    adjustment: str
    symbol: str
    timeframe: str
    contiguous_through: datetime
    quality_hash: str
    latest_bar_event_id: str | None
    content_hash: str
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.symbol_watermark_id) is not str
            or _WATERMARK_ID.fullmatch(self.symbol_watermark_id) is None
        ):
            raise MarketDataIntegrityError("persisted watermark identity is invalid")
        _require_hash(self.experiment_hash, field_name="experiment hash")
        _require_hash(self.quality_hash, field_name="quality hash")
        _require_hash(self.content_hash, field_name="watermark content hash")
        if self.latest_bar_event_id is not None and (
            type(self.latest_bar_event_id) is not str
            or _EVENT_ID.fullmatch(self.latest_bar_event_id) is None
        ):
            raise MarketDataIntegrityError("persisted watermark event reference is invalid")
        _require_token(self.provider, field_name="provider", maximum_length=32)
        _require_token(self.feed, field_name="feed", maximum_length=16)
        _require_token(self.adjustment, field_name="adjustment", maximum_length=16)
        if type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None:
            raise MarketDataIntegrityError("persisted watermark symbol is invalid")
        if self.timeframe not in _TIMEFRAME_DURATIONS:
            raise MarketDataIntegrityError("persisted watermark timeframe is invalid")
        try:
            contiguous = require_utc_instant(
                self.contiguous_through,
                field_name="contiguous_through",
            )
            updated = require_utc_instant(self.updated_at, field_name="updated_at")
        except DomainValidationError:
            raise MarketDataIntegrityError("persisted watermark timestamp is invalid") from None
        object.__setattr__(self, "contiguous_through", contiguous)
        object.__setattr__(self, "updated_at", updated)
        if type(self.version) is not int or not 1 <= self.version <= MAX_SIGNED_64_BIT_INTEGER:
            raise MarketDataIntegrityError("persisted watermark version is invalid")
        if self.symbol_watermark_id != _watermark_id(
            self.experiment_hash,
            self.provider,
            self.feed,
            self.adjustment,
            self.symbol,
            self.timeframe,
        ):
            raise MarketDataIntegrityError("persisted watermark identity is inconsistent")
        if self.content_hash != _watermark_content_hash(self):
            raise MarketDataIntegrityError("persisted watermark content hash is invalid")

    @property
    def is_ready(self) -> bool:
        """Return false for a durable fail-closed floor without an eligible event."""

        return self.latest_bar_event_id is not None


@dataclass(frozen=True, slots=True)
class BarWriteResult:
    """Complete durable outcome of one atomic bar-write attempt."""

    status: BarWriteStatus
    event: StoredBarEvent
    latest_version: int
    watermark: SymbolWatermark | None
    watermark_changed: bool

    def __post_init__(self) -> None:
        if type(self.status) is not BarWriteStatus or type(self.event) is not StoredBarEvent:
            raise MarketDataIntegrityError("bar write result is invalid")
        if type(self.latest_version) is not int or self.latest_version < 1:
            raise MarketDataIntegrityError("bar projection version is invalid")
        if self.watermark is not None and type(self.watermark) is not SymbolWatermark:
            raise MarketDataIntegrityError("bar write watermark is invalid")
        if type(self.watermark_changed) is not bool:
            raise MarketDataIntegrityError("bar write watermark outcome is invalid")


class MarketDataRepository:
    """Serialize revisions and atomically maintain latest and eligible watermark state."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("market-data repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in _SUPPORTED_DIALECTS:
            raise ValueError("market-data repository requires PostgreSQL or SQLite")
        if engine.dialect.name == "sqlite":
            schema_map = engine.get_execution_options().get("schema_translate_map")
            if (
                not isinstance(schema_map, dict)
                or schema_map.get(PLATFORM_SCHEMA, object()) is not None
            ):
                raise ValueError("SQLite market-data repository requires the platform schema map")
        self._engine = engine
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.COLLECTOR)

    @property
    def engine(self) -> Engine:
        return self._engine

    def transaction(self) -> AbstractContextManager[Connection]:
        """Open a serialized write transaction composable across platform repositories."""

        return self._transactions.transaction()

    def append(
        self,
        bar: BarWrite,
        *,
        eligible_watermark: EligibleWatermark | None = None,
        connection: Connection | None = None,
    ) -> BarWriteResult:
        """Persist one effective revision and optional contiguous watermark atomically."""

        if type(bar) is not BarWrite:
            raise MarketDataValidationError("bar write requires a validated payload")
        if eligible_watermark is not None:
            if type(eligible_watermark) is not EligibleWatermark:
                raise MarketDataValidationError("eligible watermark request is invalid")
            if eligible_watermark.updated_at < bar.received_at:
                raise MarketDataValidationError("watermark timestamp precedes bar receipt")
        try:
            if connection is not None:
                return self._append_on_connection(connection, bar, eligible_watermark)
            with self.transaction() as owned_connection:
                return self._append_on_connection(owned_connection, bar, eligible_watermark)
        except (MarketDataValidationError, MarketDataIntegrityError, MarketDataPersistenceError):
            raise
        except (ValueError, RecursionError):
            raise MarketDataIntegrityError("persisted market-data state is malformed") from None
        except SQLAlchemyError:
            raise MarketDataPersistenceError("market-data write could not be persisted") from None

    def list_events(
        self,
        identity: BarIdentity,
        *,
        connection: Connection | None = None,
    ) -> tuple[StoredBarEvent, ...]:
        """Read and verify the complete revision chain for one bar identity."""

        _require_identity_instance(identity)
        try:
            if connection is not None:
                self._validate_connection(connection, require_serialized_sqlite=False)
                return self._read_events(connection, identity)
            with self._engine.begin() as owned_connection:
                return self._read_events(owned_connection, identity)
        except (MarketDataValidationError, MarketDataIntegrityError, MarketDataPersistenceError):
            raise
        except (ValueError, RecursionError):
            raise MarketDataIntegrityError("persisted market-data state is malformed") from None
        except SQLAlchemyError:
            raise MarketDataPersistenceError("market-data events could not be read") from None

    def latest(
        self,
        identity: BarIdentity,
        *,
        connection: Connection | None = None,
    ) -> StoredBarEvent | None:
        """Return the independently verified current effective revision, if present."""

        _require_identity_instance(identity)
        try:
            if connection is not None:
                self._validate_connection(connection, require_serialized_sqlite=False)
                return self._read_latest(connection, identity)
            with self._engine.begin() as owned_connection:
                return self._read_latest(owned_connection, identity)
        except (MarketDataValidationError, MarketDataIntegrityError, MarketDataPersistenceError):
            raise
        except (ValueError, RecursionError):
            raise MarketDataIntegrityError("persisted market-data state is malformed") from None
        except SQLAlchemyError:
            raise MarketDataPersistenceError("latest market-data event could not be read") from None

    def watermark(
        self,
        *,
        experiment_hash: str,
        identity: BarIdentity,
        connection: Connection | None = None,
    ) -> SymbolWatermark | None:
        """Return verified readiness state for the identity's experiment series."""

        _require_hash(experiment_hash, field_name="experiment hash")
        _require_identity_instance(identity)
        try:
            if connection is not None:
                self._validate_connection(connection, require_serialized_sqlite=False)
                return self._read_watermark(connection, experiment_hash, identity)
            with self._engine.begin() as owned_connection:
                return self._read_watermark(owned_connection, experiment_hash, identity)
        except (MarketDataValidationError, MarketDataIntegrityError, MarketDataPersistenceError):
            raise
        except (ValueError, RecursionError):
            raise MarketDataIntegrityError("persisted market-data state is malformed") from None
        except SQLAlchemyError:
            raise MarketDataPersistenceError("market-data watermark could not be read") from None

    def _append_on_connection(
        self,
        connection: Connection,
        bar: BarWrite,
        eligible_watermark: EligibleWatermark | None,
    ) -> BarWriteResult:
        self._validate_connection(connection, require_serialized_sqlite=True)
        try:
            if eligible_watermark is not None:
                _lock_watermark_series(
                    self._transactions,
                    connection,
                    eligible_watermark.experiment_hash,
                    bar.identity,
                )
                referenced_identity_id = self._watermark_reference_identity_id(
                    connection,
                    eligible_watermark.experiment_hash,
                    bar.identity,
                )
                _lock_bar_identities(
                    self._transactions,
                    connection,
                    tuple(
                        identity_id
                        for identity_id in (
                            referenced_identity_id,
                            bar.identity.bar_identity_id,
                        )
                        if identity_id is not None
                    ),
                )
            else:
                _lock_bar_identity(
                    self._transactions,
                    connection,
                    bar.identity.bar_identity_id,
                )
            identity_created = self._ensure_identity(connection, bar)
            events = self._load_events(connection, bar.identity)
            if not identity_created and not events:
                raise MarketDataIntegrityError("bar identity has no revision history")
            latest_version = self._verify_latest_projection(connection, bar.identity, events)

            if events and events[-1].normalized_payload_hash == bar.normalized_payload_hash:
                status = BarWriteStatus.DUPLICATE
                effective_event = events[-1]
            else:
                status = BarWriteStatus.INSERTED if not events else BarWriteStatus.CORRECTED
                effective_event = self._insert_revision(connection, bar, events)
                latest_version = self._write_latest_projection(
                    connection,
                    effective_event,
                    prior_version=latest_version,
                )

            watermark = None
            watermark_changed = False
            if eligible_watermark is not None:
                watermark, watermark_changed = self._apply_eligible_watermark(
                    connection,
                    effective_event,
                    eligible_watermark,
                )
            if status is BarWriteStatus.CORRECTED:
                self._append_correction_audit(connection, effective_event)
            return BarWriteResult(
                status=status,
                event=effective_event,
                latest_version=latest_version,
                watermark=watermark,
                watermark_changed=watermark_changed,
            )
        except (MarketDataValidationError, MarketDataIntegrityError, MarketDataPersistenceError):
            raise
        except SQLAlchemyError:
            raise MarketDataPersistenceError("market-data write could not be persisted") from None

    def _append_correction_audit(
        self,
        connection: Connection,
        event: StoredBarEvent,
    ) -> None:
        try:
            self._audit.append(
                stream_id=(f"{AuditWriter.COLLECTOR.value}:bar:{event.identity.bar_identity_id}"),
                event_type="bar.corrected",
                occurred_at=event.bar.received_at,
                payload=AuditPayload.from_mapping(
                    {
                        "idempotency_key": event.bar_event_id,
                        "bar_id": event.bar_event_id,
                        "content_hash": event.content_hash,
                        "revision": event.revision,
                        "symbol": event.identity.symbol,
                    }
                ),
                connection=connection,
            )
        except (AuditPersistenceError, AuditValidationError):
            raise MarketDataPersistenceError(
                "bar correction audit could not be persisted"
            ) from None

    def _ensure_identity(self, connection: Connection, bar: BarWrite) -> bool:
        identity = bar.identity
        natural_key = and_(
            aqa_bar_identities.c.provider == identity.provider,
            aqa_bar_identities.c.feed == identity.feed,
            aqa_bar_identities.c.adjustment == identity.adjustment,
            aqa_bar_identities.c.symbol == identity.symbol,
            aqa_bar_identities.c.timeframe == identity.timeframe,
            aqa_bar_identities.c.start_at == identity.start_at,
        )
        rows = (
            connection.execute(
                select(aqa_bar_identities).where(
                    or_(
                        aqa_bar_identities.c.bar_identity_id == identity.bar_identity_id,
                        natural_key,
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 1:
            raise MarketDataIntegrityError("bar identity indexes disagree")
        if rows:
            stored = _identity_from_row(rows[0])
            if stored != identity:
                raise MarketDataIntegrityError("persisted bar identity is inconsistent")
            return False
        connection.execute(
            insert(aqa_bar_identities).values(
                bar_identity_id=identity.bar_identity_id,
                provider=identity.provider,
                feed=identity.feed,
                adjustment=identity.adjustment,
                symbol=identity.symbol,
                timeframe=identity.timeframe,
                start_at=identity.start_at,
                end_at=identity.end_at,
                content_hash=identity.identity_hash,
                created_at=bar.received_at,
            )
        )
        return True

    def _load_events(
        self,
        connection: Connection,
        identity: BarIdentity,
    ) -> tuple[StoredBarEvent, ...]:
        statement = (
            select(
                aqa_bar_events,
                aqa_bar_identities.c.created_at.label("identity_created_at"),
            )
            .join(
                aqa_bar_identities,
                aqa_bar_identities.c.bar_identity_id == aqa_bar_events.c.bar_identity_id,
            )
            .where(aqa_bar_events.c.bar_identity_id == identity.bar_identity_id)
            .order_by(aqa_bar_events.c.revision)
        )
        rows = connection.execute(statement).mappings().all()
        events = tuple(_event_from_row(identity, row) for row in rows)
        _verify_revision_chain(events)
        if events:
            try:
                identity_created_at = require_utc_instant(
                    rows[0]["identity_created_at"],
                    field_name="identity_created_at",
                )
            except (DomainValidationError, KeyError, TypeError, ValueError):
                raise MarketDataIntegrityError("persisted bar identity is malformed") from None
            if identity_created_at != events[0].bar.received_at:
                raise MarketDataIntegrityError("bar identity origin timestamp is inconsistent")
        return events

    def _verify_latest_projection(
        self,
        connection: Connection,
        identity: BarIdentity,
        events: Sequence[StoredBarEvent],
    ) -> int:
        statement = select(aqa_bar_latest).where(
            aqa_bar_latest.c.bar_identity_id == identity.bar_identity_id
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if not events:
            if row is not None:
                raise MarketDataIntegrityError("bar latest projection has no event history")
            return 0
        if row is None:
            raise MarketDataIntegrityError("bar revision history has no latest projection")
        latest = events[-1]
        try:
            version = row["version"]
            projected_at = require_utc_instant(row["projected_at"], field_name="projected_at")
        except (DomainValidationError, KeyError, TypeError, ValueError):
            raise MarketDataIntegrityError("bar latest projection is malformed") from None
        if (
            type(version) is not int
            or version != latest.revision
            or row["bar_event_id"] != latest.bar_event_id
            or row["revision"] != latest.revision
            or row["content_hash"] != latest.content_hash
            or projected_at != latest.bar.received_at
        ):
            raise MarketDataIntegrityError("bar latest projection is inconsistent")
        return version

    def _insert_revision(
        self,
        connection: Connection,
        bar: BarWrite,
        events: Sequence[StoredBarEvent],
    ) -> StoredBarEvent:
        previous = events[-1] if events else None
        if previous is not None and bar.received_at < previous.bar.received_at:
            raise MarketDataValidationError("bar correction receipt precedes current revision")
        revision = 1 if previous is None else previous.revision + 1
        if revision > MAX_SIGNED_64_BIT_INTEGER:
            raise MarketDataIntegrityError("bar revision capacity is exhausted")
        bar_event_id = _bar_event_id(
            bar.identity.bar_identity_id,
            revision,
            bar.normalized_payload_hash,
        )
        correction_of_event_id = None if previous is None else previous.bar_event_id
        event = StoredBarEvent(
            bar_event_id=bar_event_id,
            revision=revision,
            correction_of_event_id=correction_of_event_id,
            bar=bar,
            content_hash=_bar_event_content_hash(
                bar_event_id=bar_event_id,
                revision=revision,
                correction_of_event_id=correction_of_event_id,
                bar=bar,
            ),
        )
        connection.execute(
            insert(aqa_bar_events).values(
                bar_event_id=event.bar_event_id,
                bar_identity_id=bar.identity.bar_identity_id,
                revision=event.revision,
                schema_version=bar.schema_version,
                provider_timestamp=bar.provider_timestamp,
                received_at=bar.received_at,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                trade_count=bar.trade_count,
                vwap=bar.vwap,
                quality_flags=list(bar.quality_flags),
                source=bar.source,
                source_event_id=bar.source_event_id,
                source_payload_hash=bar.source_payload_hash,
                source_mode=bar.source_mode,
                is_correction=bar.is_correction,
                correction_of_source_event_id=bar.correction_of_source_event_id,
                lineage_hash=bar.lineage_hash,
                normalized_payload_hash=bar.normalized_payload_hash,
                correction_of_event_id=event.correction_of_event_id,
                content_hash=event.content_hash,
                created_at=bar.received_at,
            )
        )
        return event

    def _write_latest_projection(
        self,
        connection: Connection,
        event: StoredBarEvent,
        *,
        prior_version: int,
    ) -> int:
        next_version = prior_version + 1
        if next_version != event.revision:
            raise MarketDataIntegrityError("bar projection revision is inconsistent")
        values = {
            "bar_event_id": event.bar_event_id,
            "revision": event.revision,
            "content_hash": event.content_hash,
            "version": next_version,
            "projected_at": event.bar.received_at,
        }
        if prior_version == 0:
            connection.execute(
                insert(aqa_bar_latest).values(
                    bar_identity_id=event.identity.bar_identity_id,
                    **values,
                )
            )
        else:
            result = connection.execute(
                update(aqa_bar_latest)
                .where(
                    aqa_bar_latest.c.bar_identity_id == event.identity.bar_identity_id,
                    aqa_bar_latest.c.version == prior_version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise MarketDataPersistenceError("bar projection lost its concurrency fence")
        return next_version

    def _apply_eligible_watermark(
        self,
        connection: Connection,
        event: StoredBarEvent,
        request: EligibleWatermark,
    ) -> tuple[SymbolWatermark, bool]:
        current = self._select_watermark(
            connection,
            request.experiment_hash,
            event.identity,
            lock=True,
            allowed_successor=event,
        )
        if current is None:
            watermark = _new_watermark(event, request, version=1)
            connection.execute(insert(aqa_symbol_watermarks).values(**_watermark_values(watermark)))
            return watermark, True

        end_at = event.identity.end_at
        advances = event.identity.start_at == current.contiguous_through
        corrects_current = end_at == current.contiguous_through
        if not advances and not corrects_current:
            return current, False
        if (
            corrects_current
            and current.latest_bar_event_id == event.bar_event_id
            and current.quality_hash == request.quality_hash
        ):
            return current, False
        if request.updated_at < current.updated_at:
            if corrects_current and current.latest_bar_event_id != event.bar_event_id:
                raise MarketDataValidationError(
                    "watermark correction timestamp precedes durable state"
                )
            return current, False
        if request.updated_at == current.updated_at:
            raise MarketDataValidationError(
                "watermark update conflicts with durable state at the same timestamp"
            )
        if current.version >= MAX_SIGNED_64_BIT_INTEGER:
            raise MarketDataIntegrityError("watermark version capacity is exhausted")
        watermark = _new_watermark(
            event,
            request,
            version=current.version + 1,
        )
        result = connection.execute(
            update(aqa_symbol_watermarks)
            .where(
                aqa_symbol_watermarks.c.symbol_watermark_id == current.symbol_watermark_id,
                aqa_symbol_watermarks.c.version == current.version,
            )
            .values(**_watermark_values(watermark, include_identity=False))
        )
        if result.rowcount != 1:
            raise MarketDataPersistenceError("watermark lost its concurrency fence")
        return watermark, True

    def _read_events(
        self,
        connection: Connection,
        identity: BarIdentity,
    ) -> tuple[StoredBarEvent, ...]:
        _lock_bar_identity(self._transactions, connection, identity.bar_identity_id)
        stored_identity = self._select_identity(connection, identity)
        if stored_identity is None:
            return ()
        events = self._load_events(connection, stored_identity)
        if not events:
            raise MarketDataIntegrityError("bar identity has no revision history")
        self._verify_latest_projection(connection, stored_identity, events)
        return events

    def _read_latest(
        self,
        connection: Connection,
        identity: BarIdentity,
    ) -> StoredBarEvent | None:
        events = self._read_events(connection, identity)
        return events[-1] if events else None

    def _read_watermark(
        self,
        connection: Connection,
        experiment_hash: str,
        identity: BarIdentity,
    ) -> SymbolWatermark | None:
        _lock_watermark_series(self._transactions, connection, experiment_hash, identity)
        return self._select_watermark(connection, experiment_hash, identity)

    def _select_identity(
        self,
        connection: Connection,
        identity: BarIdentity,
    ) -> BarIdentity | None:
        row = (
            connection.execute(
                select(aqa_bar_identities).where(
                    aqa_bar_identities.c.bar_identity_id == identity.bar_identity_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        stored = _identity_from_row(row)
        if stored != identity:
            raise MarketDataIntegrityError("persisted bar identity is inconsistent")
        return stored

    def _select_watermark(
        self,
        connection: Connection,
        experiment_hash: str,
        identity: BarIdentity,
        *,
        lock: bool = False,
        allowed_successor: StoredBarEvent | None = None,
    ) -> SymbolWatermark | None:
        statement = select(aqa_symbol_watermarks).where(
            aqa_symbol_watermarks.c.experiment_hash == experiment_hash,
            aqa_symbol_watermarks.c.provider == identity.provider,
            aqa_symbol_watermarks.c.feed == identity.feed,
            aqa_symbol_watermarks.c.adjustment == identity.adjustment,
            aqa_symbol_watermarks.c.symbol == identity.symbol,
            aqa_symbol_watermarks.c.timeframe == identity.timeframe,
        )
        if lock and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        watermark = _watermark_from_row(row)
        self._verify_watermark_reference(
            connection,
            watermark,
            allowed_successor=allowed_successor,
        )
        return watermark

    def _watermark_reference_identity_id(
        self,
        connection: Connection,
        experiment_hash: str,
        identity: BarIdentity,
    ) -> str | None:
        event_id = connection.scalar(
            select(aqa_symbol_watermarks.c.latest_bar_event_id).where(
                aqa_symbol_watermarks.c.experiment_hash == experiment_hash,
                aqa_symbol_watermarks.c.provider == identity.provider,
                aqa_symbol_watermarks.c.feed == identity.feed,
                aqa_symbol_watermarks.c.adjustment == identity.adjustment,
                aqa_symbol_watermarks.c.symbol == identity.symbol,
                aqa_symbol_watermarks.c.timeframe == identity.timeframe,
            )
        )
        if event_id is None:
            return None
        if type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None:
            raise MarketDataIntegrityError("watermark event reference is malformed")
        identity_id = connection.scalar(
            select(aqa_bar_events.c.bar_identity_id).where(
                aqa_bar_events.c.bar_event_id == event_id
            )
        )
        if type(identity_id) is not str or _IDENTITY_ID.fullmatch(identity_id) is None:
            raise MarketDataIntegrityError("watermark event reference is missing")
        return identity_id

    def _verify_watermark_reference(
        self,
        connection: Connection,
        watermark: SymbolWatermark,
        *,
        allowed_successor: StoredBarEvent | None = None,
    ) -> None:
        if watermark.latest_bar_event_id is None:
            return
        row = (
            connection.execute(
                select(aqa_bar_identities)
                .join(
                    aqa_bar_events,
                    aqa_bar_events.c.bar_identity_id == aqa_bar_identities.c.bar_identity_id,
                )
                .where(aqa_bar_events.c.bar_event_id == watermark.latest_bar_event_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise MarketDataIntegrityError("watermark event reference is missing")
        identity = _identity_from_row(row)
        if (
            identity.provider != watermark.provider
            or identity.feed != watermark.feed
            or identity.adjustment != watermark.adjustment
            or identity.symbol != watermark.symbol
            or identity.timeframe != watermark.timeframe
            or identity.end_at != watermark.contiguous_through
        ):
            raise MarketDataIntegrityError("watermark event reference is inconsistent")
        _lock_bar_identity(self._transactions, connection, identity.bar_identity_id)
        stored_identity = self._select_identity(connection, identity)
        if stored_identity is None:
            raise MarketDataIntegrityError("watermark event reference is missing")
        events = self._load_events(connection, stored_identity)
        self._verify_latest_projection(connection, stored_identity, events)
        if not events:
            raise MarketDataIntegrityError("watermark event reference is missing")
        latest = events[-1]
        referenced = next(
            (event for event in events if event.bar_event_id == watermark.latest_bar_event_id),
            None,
        )
        if referenced is None:
            raise MarketDataIntegrityError("watermark event reference is missing")
        if watermark.updated_at < referenced.bar.received_at:
            raise MarketDataIntegrityError("watermark timestamp precedes referenced event")
        references_current = latest.bar_event_id == watermark.latest_bar_event_id
        references_allowed_ancestor = (
            allowed_successor is not None
            and latest == allowed_successor
            and latest.identity == identity
        )
        if not references_current and not references_allowed_ancestor:
            raise MarketDataIntegrityError("watermark event reference is not the latest revision")

    def _validate_connection(
        self,
        connection: Connection,
        *,
        require_serialized_sqlite: bool,
    ) -> None:
        try:
            self._transactions.validate_connection(
                connection,
                require_serialized_sqlite=require_serialized_sqlite,
            )
        except TransactionBoundaryError as error:
            messages = {
                TransactionViolation.FOREIGN_CONNECTION: (
                    "market-data connection belongs to another repository engine"
                ),
                TransactionViolation.INACTIVE_TRANSACTION: (
                    "market-data operation requires an active transaction"
                ),
                TransactionViolation.ADVISORY_LOCK_ORDER: (
                    "market-data advisory locks must follow the global acquisition order"
                ),
                TransactionViolation.UNSERIALIZED_SQLITE: (
                    "SQLite market-data append requires a serialized transaction"
                ),
                TransactionViolation.UNSUPPORTED_DIALECT: (
                    "market-data connection uses an unsupported database"
                ),
            }
            raise MarketDataPersistenceError(messages[error.violation]) from None


def _require_identity_instance(identity: object) -> BarIdentity:
    if type(identity) is not BarIdentity:
        raise MarketDataValidationError("bar identity is invalid")
    return identity


def _require_token(value: object, *, field_name: str, maximum_length: int) -> str:
    if (
        type(value) is not str
        or len(value) > maximum_length
        or _LOWER_TOKEN.fullmatch(value) is None
    ):
        raise MarketDataValidationError(f"bar {field_name} is invalid")
    return value


def _require_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise MarketDataValidationError(f"{field_name} is invalid")
    return value


def _require_opaque_text(value: object, *, field_name: str, maximum_length: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MarketDataValidationError(f"bar {field_name} is invalid")
    return value


def _require_quality_flags(flags: object) -> tuple[str, ...]:
    if type(flags) is not tuple or len(flags) > _MAX_QUALITY_FLAGS:
        raise MarketDataValidationError("bar quality flags are invalid")
    if any(type(flag) is not str or _QUALITY_FLAG.fullmatch(flag) is None for flag in flags):
        raise MarketDataValidationError("bar quality flags are invalid")
    if tuple(sorted(set(flags))) != flags:
        raise MarketDataValidationError("bar quality flags must be sorted and unique")
    return flags


def _require_storage_decimal(
    value: object,
    *,
    field_name: str,
    scale: int = 18,
) -> Decimal:
    try:
        number = require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise MarketDataValidationError(f"bar {field_name} must be a finite Decimal") from None
    _, raw_digits, raw_exponent = number.as_tuple()
    if not isinstance(raw_exponent, int):
        raise MarketDataValidationError(f"bar {field_name} must be finite")
    digits = list(raw_digits)
    exponent = raw_exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits:
        fractional_places = max(-exponent, 0)
        integer_places = max(len(digits) + exponent, 0)
        if fractional_places > scale or integer_places > 38 - scale or len(digits) > 38:
            raise MarketDataValidationError(f"bar {field_name} exceeds storage precision")
    return number


def _bar_event_id(identity_id: str, revision: int, normalized_payload_hash: str) -> str:
    return f"bar_event_{sha256_hex((identity_id, revision, normalized_payload_hash))}"


def _bar_event_content_hash(
    *,
    bar_event_id: str,
    revision: int,
    correction_of_event_id: str | None,
    bar: BarWrite,
) -> str:
    """Hash every immutable event field, including retry-specific provenance."""

    return sha256_hex(
        {
            "bar_event_id": bar_event_id,
            "bar_identity_id": bar.identity.bar_identity_id,
            "correction_of_event_id": correction_of_event_id,
            "created_at": bar.received_at,
            "normalized_payload_hash": bar.normalized_payload_hash,
            "received_at": bar.received_at,
            "revision": revision,
            "schema": "bar_event_v1",
            "lineage_hash": bar.lineage_hash,
            "source": bar.source,
            "source_mode": bar.source_mode,
            "source_event_id": bar.source_event_id,
            "source_payload_hash": bar.source_payload_hash,
        }
    )


def _lock_bar_identity(
    coordinator: SerializedTransactionCoordinator,
    connection: Connection,
    identity_id: str,
) -> None:
    try:
        coordinator.acquire_postgres_advisory_lock(
            connection,
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
                identity_id,
            ),
        )
    except TransactionBoundaryError as error:
        if error.violation is TransactionViolation.ADVISORY_LOCK_ORDER:
            raise MarketDataPersistenceError(
                "market-data advisory locks must follow the global acquisition order"
            ) from None
        raise MarketDataPersistenceError("bar identity lock could not be acquired") from None


def _lock_bar_identities(
    coordinator: SerializedTransactionCoordinator,
    connection: Connection,
    identity_ids: Sequence[str],
) -> None:
    try:
        coordinator.acquire_postgres_advisory_locks(
            connection,
            tuple(
                PostgresAdvisoryLockRequest.for_resource(
                    PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
                    identity_id,
                )
                for identity_id in identity_ids
            ),
        )
    except TransactionBoundaryError as error:
        if error.violation is TransactionViolation.ADVISORY_LOCK_ORDER:
            raise MarketDataPersistenceError(
                "market-data advisory locks must follow the global acquisition order"
            ) from None
        raise MarketDataPersistenceError("bar identity locks could not be acquired") from None


def _watermark_lock_resource(experiment_hash: str, identity: BarIdentity) -> str:
    series_id = _watermark_id(
        experiment_hash,
        identity.provider,
        identity.feed,
        identity.adjustment,
        identity.symbol,
        identity.timeframe,
    )
    return series_id


def _lock_watermark_series(
    coordinator: SerializedTransactionCoordinator,
    connection: Connection,
    experiment_hash: str,
    identity: BarIdentity,
) -> None:
    try:
        coordinator.acquire_postgres_advisory_lock(
            connection,
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
                _watermark_lock_resource(experiment_hash, identity),
            ),
        )
    except TransactionBoundaryError as error:
        if error.violation is TransactionViolation.ADVISORY_LOCK_ORDER:
            raise MarketDataPersistenceError(
                "market-data advisory locks must follow the global acquisition order"
            ) from None
        raise MarketDataPersistenceError("watermark lock could not be acquired") from None


def _identity_from_row(row: RowMapping) -> BarIdentity:
    try:
        identity_id = row["bar_identity_id"]
        content_hash = row["content_hash"]
        if type(identity_id) is not str or _IDENTITY_ID.fullmatch(identity_id) is None:
            raise ValueError
        if type(content_hash) is not str:
            raise ValueError
        identity = BarIdentity(
            provider=row["provider"],
            feed=row["feed"],
            adjustment=row["adjustment"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            start_at=row["start_at"],
            end_at=row["end_at"],
        )
        created_at = require_utc_instant(row["created_at"], field_name="created_at")
        del created_at
    except (DomainValidationError, KeyError, RecursionError, TypeError, ValueError):
        raise MarketDataIntegrityError("persisted bar identity is malformed") from None
    if identity_id != identity.bar_identity_id or content_hash != identity.identity_hash:
        raise MarketDataIntegrityError("persisted bar identity hash is invalid")
    return identity


def _event_from_row(identity: BarIdentity, row: RowMapping) -> StoredBarEvent:
    try:
        raw_flags = row["quality_flags"]
        if type(raw_flags) is not list:
            raise ValueError
        bar = BarWrite(
            identity=identity,
            schema_version=row["schema_version"],
            provider_timestamp=row["provider_timestamp"],
            received_at=row["received_at"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            trade_count=row["trade_count"],
            vwap=row["vwap"],
            quality_flags=tuple(raw_flags),
            source=row["source"],
            source_mode=row["source_mode"],
            source_event_id=row["source_event_id"],
            source_payload_hash=row["source_payload_hash"],
            is_correction=row["is_correction"],
            correction_of_source_event_id=row["correction_of_source_event_id"],
            lineage_hash=row["lineage_hash"],
        )
        event = StoredBarEvent(
            bar_event_id=row["bar_event_id"],
            revision=row["revision"],
            correction_of_event_id=row["correction_of_event_id"],
            bar=bar,
            content_hash=row["content_hash"],
        )
        created_at = require_utc_instant(row["created_at"], field_name="created_at")
        normalized_payload_hash = row["normalized_payload_hash"]
    except (DomainValidationError, KeyError, RecursionError, TypeError, ValueError):
        raise MarketDataIntegrityError("persisted bar event is malformed") from None
    if (
        row["bar_identity_id"] != identity.bar_identity_id
        or created_at != event.bar.received_at
        or normalized_payload_hash != event.normalized_payload_hash
    ):
        raise MarketDataIntegrityError("persisted bar event content is inconsistent")
    return event


def _verify_revision_chain(events: Sequence[StoredBarEvent]) -> None:
    previous: StoredBarEvent | None = None
    for expected_revision, event in enumerate(events, start=1):
        if event.revision != expected_revision:
            raise MarketDataIntegrityError("bar revision sequence is not contiguous")
        if previous is None:
            if event.correction_of_event_id is not None:
                raise MarketDataIntegrityError("bar revision chain has an invalid origin")
        elif (
            event.correction_of_event_id != previous.bar_event_id
            or event.normalized_payload_hash == previous.normalized_payload_hash
            or event.bar.received_at < previous.bar.received_at
        ):
            raise MarketDataIntegrityError("bar revision chain is inconsistent")
        previous = event


def _watermark_id(
    experiment_hash: str,
    provider: str,
    feed: str,
    adjustment: str,
    symbol: str,
    timeframe: str,
) -> str:
    digest = sha256_hex((experiment_hash, provider, feed, adjustment, symbol, timeframe))
    return f"symbol_watermark_{digest}"


def _watermark_content_hash(watermark: SymbolWatermark) -> str:
    return _watermark_hash_for_fields(
        symbol_watermark_id=watermark.symbol_watermark_id,
        experiment_hash=watermark.experiment_hash,
        provider=watermark.provider,
        feed=watermark.feed,
        adjustment=watermark.adjustment,
        symbol=watermark.symbol,
        timeframe=watermark.timeframe,
        contiguous_through=watermark.contiguous_through,
        quality_hash=watermark.quality_hash,
        latest_bar_event_id=watermark.latest_bar_event_id,
        version=watermark.version,
        updated_at=watermark.updated_at,
    )


def _watermark_hash_for_fields(
    *,
    symbol_watermark_id: str,
    experiment_hash: str,
    provider: str,
    feed: str,
    adjustment: str,
    symbol: str,
    timeframe: str,
    contiguous_through: datetime,
    quality_hash: str,
    latest_bar_event_id: str | None,
    version: int,
    updated_at: datetime,
) -> str:
    return sha256_hex(
        (
            symbol_watermark_id,
            experiment_hash,
            provider,
            feed,
            adjustment,
            symbol,
            timeframe,
            contiguous_through,
            quality_hash,
            latest_bar_event_id,
            version,
            updated_at,
        )
    )


def _new_watermark(
    event: StoredBarEvent,
    request: EligibleWatermark,
    *,
    version: int,
    updated_at: datetime | None = None,
) -> SymbolWatermark:
    identity = event.identity
    watermark_id = _watermark_id(
        request.experiment_hash,
        identity.provider,
        identity.feed,
        identity.adjustment,
        identity.symbol,
        identity.timeframe,
    )
    values: dict[str, Any] = {
        "symbol_watermark_id": watermark_id,
        "experiment_hash": request.experiment_hash,
        "provider": identity.provider,
        "feed": identity.feed,
        "adjustment": identity.adjustment,
        "symbol": identity.symbol,
        "timeframe": identity.timeframe,
        "contiguous_through": identity.end_at,
        "quality_hash": request.quality_hash,
        "latest_bar_event_id": event.bar_event_id,
        "version": version,
        "updated_at": request.updated_at if updated_at is None else updated_at,
    }
    values["content_hash"] = _watermark_hash_for_fields(**values)
    return SymbolWatermark(**values)


def _watermark_values(
    watermark: SymbolWatermark,
    *,
    include_identity: bool = True,
) -> dict[str, Any]:
    values = {
        "contiguous_through": watermark.contiguous_through,
        "quality_hash": watermark.quality_hash,
        "latest_bar_event_id": watermark.latest_bar_event_id,
        "content_hash": watermark.content_hash,
        "version": watermark.version,
        "updated_at": watermark.updated_at,
    }
    if include_identity:
        values.update(
            {
                "symbol_watermark_id": watermark.symbol_watermark_id,
                "experiment_hash": watermark.experiment_hash,
                "provider": watermark.provider,
                "feed": watermark.feed,
                "adjustment": watermark.adjustment,
                "symbol": watermark.symbol,
                "timeframe": watermark.timeframe,
            }
        )
    return values


def _watermark_from_row(row: RowMapping) -> SymbolWatermark:
    try:
        return SymbolWatermark(
            symbol_watermark_id=row["symbol_watermark_id"],
            experiment_hash=row["experiment_hash"],
            provider=row["provider"],
            feed=row["feed"],
            adjustment=row["adjustment"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            contiguous_through=row["contiguous_through"],
            quality_hash=row["quality_hash"],
            latest_bar_event_id=row["latest_bar_event_id"],
            content_hash=row["content_hash"],
            version=row["version"],
            updated_at=row["updated_at"],
        )
    except (DomainValidationError, KeyError, RecursionError, TypeError, ValueError):
        raise MarketDataIntegrityError("persisted watermark is malformed") from None
