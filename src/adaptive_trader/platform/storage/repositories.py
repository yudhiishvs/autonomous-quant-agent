"""Transactional persistence boundaries for the generic platform."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from sqlalchemy import Column, Connection, Engine, MetaData, Select, Table, func, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.constants import AUDIT_GENESIS_HASH
from adaptive_trader.platform.domain import (
    AuditEvent,
    AuditPayload,
    AuditStreamHead,
    AuditVerificationReport,
    AuditWriter,
    audit_event_hash,
)
from adaptive_trader.platform.errors import (
    AuditIntegrityError,
    AuditPersistenceError,
    AuditValidationError,
    CanonicalizationError,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, aqa_audit_events
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
    TransactionBoundaryError,
    TransactionViolation,
)

_SUPPORTED_DIALECTS = frozenset({"postgresql", "sqlite"})
_AUDIT_VIEW_METADATA = MetaData()
_POSTGRES_AUDIT_EVENTS_VIEW = Table(
    "aqa_audit_events_v",
    _AUDIT_VIEW_METADATA,
    *(Column(column.name, column.type) for column in aqa_audit_events.columns),
    schema=aqa_audit_events.schema,
)
_POSTGRES_WRITER_AUDIT_VIEWS = {
    writer: Table(
        f"{writer.value}_audit_events_v",
        _AUDIT_VIEW_METADATA,
        *(Column(column.name, column.type) for column in aqa_audit_events.columns),
        schema=aqa_audit_events.schema,
    )
    for writer in (
        AuditWriter.COLLECTOR,
        AuditWriter.EXECUTION,
        AuditWriter.SCHEDULER,
        AuditWriter.STRATEGY,
    )
}


class AuditRepository:
    """Append and verify immutable per-stream audit evidence.

    PostgreSQL writers acquire a transaction-scoped advisory lock derived from the stream ID.
    SQLite writers must use :meth:`transaction`, which obtains ``BEGIN IMMEDIATE`` before the
    chain head is read. This prevents two writers from deriving the same next sequence.
    """

    def __init__(self, engine: Engine, *, writer: AuditWriter | None = None) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("audit repository requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in _SUPPORTED_DIALECTS:
            raise ValueError("audit repository requires PostgreSQL or SQLite")
        if engine.dialect.name == "sqlite":
            schema_map = engine.get_execution_options().get("schema_translate_map")
            if (
                not isinstance(schema_map, dict)
                or schema_map.get(PLATFORM_SCHEMA, object()) is not None
            ):
                raise ValueError("SQLite audit repository requires the platform schema map")
        if writer is not None and type(writer) is not AuditWriter:
            raise TypeError("audit repository writer must use the closed contract")
        self._engine = engine
        self._writer = writer
        self._transactions = SerializedTransactionCoordinator(engine)

    @property
    def engine(self) -> Engine:
        """Return the configured engine for explicit cross-repository transactions."""

        return self._engine

    def transaction(self) -> AbstractContextManager[Connection]:
        """Open a serialized write transaction suitable for one or more audit appends."""

        if self._writer is None:
            raise AuditPersistenceError("audit verifier cannot open a write transaction")
        return self._transactions.transaction()

    def append(
        self,
        *,
        stream_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: AuditPayload,
        connection: Connection | None = None,
    ) -> AuditEvent:
        """Append one event, optionally inside the caller's serialized transaction."""

        if self._writer is None:
            raise AuditPersistenceError("audit repository is verify-only")
        candidate = AuditEvent.create(
            stream_id=stream_id,
            sequence=1,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type=event_type,
            actor=self._writer,
            occurred_at=occurred_at,
            payload=payload,
        )
        if connection is not None:
            return self._append_on_connection(connection, candidate)
        try:
            with self.transaction() as owned_connection:
                return self._append_on_connection(owned_connection, candidate)
        except AuditValidationError:
            raise
        except AuditPersistenceError:
            raise
        except SQLAlchemyError:
            raise AuditPersistenceError("audit event could not be persisted") from None

    def list_events(
        self,
        *,
        stream_id: str | None = None,
        connection: Connection | None = None,
    ) -> tuple[AuditEvent, ...]:
        """Load immutable domain events in deterministic stream/sequence order."""

        if stream_id is not None:
            _validate_stream_id(stream_id)
        try:
            if connection is not None:
                self._validate_connection(connection, require_serialized_sqlite=False)
                return _load_selected_events(
                    connection,
                    stream_id=stream_id,
                    writer=self._writer,
                )
            with self._engine.begin() as owned_connection:
                return _load_selected_events(
                    owned_connection,
                    stream_id=stream_id,
                    writer=self._writer,
                )
        except AuditIntegrityError:
            raise
        except (AuditValidationError, CanonicalizationError):
            raise AuditIntegrityError("persisted audit evidence is malformed") from None
        except SQLAlchemyError:
            raise AuditPersistenceError("audit events could not be read") from None

    def verify(
        self,
        *,
        stream_id: str | None = None,
        expected_sequence: int | None = None,
        expected_hash: str | None = None,
        connection: Connection | None = None,
    ) -> AuditVerificationReport:
        """Independently verify payloads, IDs, hashes, sequences, and continuity."""

        expected_head = _expected_stream_head(
            stream_id=stream_id,
            expected_sequence=expected_sequence,
            expected_hash=expected_hash,
        )
        events = self.list_events(stream_id=stream_id, connection=connection)
        if stream_id is not None and not events:
            raise AuditIntegrityError("requested audit stream does not exist")
        report = verify_audit_chain(events)
        if expected_head is not None and report.stream_heads != (expected_head,):
            raise AuditIntegrityError("audit stream does not match the expected head")
        return report

    def _append_on_connection(
        self,
        connection: Connection,
        candidate: AuditEvent,
    ) -> AuditEvent:
        self._validate_connection(connection, require_serialized_sqlite=True)
        try:
            self._transactions.acquire_postgres_advisory_lock(
                connection,
                PostgresAdvisoryLockRequest.for_resource(
                    PostgresAdvisoryLockNamespace.AUDIT,
                    candidate.stream_id,
                ),
            )
            relation = _audit_read_relation(connection, writer=self._writer)
            existing = _load_events(
                connection,
                select(relation)
                .where(relation.c.stream_id == candidate.stream_id)
                .order_by(relation.c.sequence),
            )
            if not existing:
                event = candidate
            else:
                verify_audit_chain(existing)
                retry = next(
                    (
                        prior
                        for prior in existing
                        if prior.idempotency_key == candidate.idempotency_key
                    ),
                    None,
                )
                if retry is not None:
                    if _same_append_request(retry, candidate):
                        return retry
                    raise AuditValidationError(
                        "audit idempotency key was reused with different content"
                    )
                latest = existing[-1]
                event = AuditEvent.create(
                    stream_id=candidate.stream_id,
                    sequence=latest.sequence + 1,
                    previous_hash=latest.event_hash,
                    event_type=candidate.event_type,
                    actor=candidate.actor,
                    occurred_at=candidate.occurred_at,
                    payload=candidate.audit_payload,
                )
            connection.execute(
                insert(aqa_audit_events).values(
                    audit_event_id=event.audit_event_id,
                    stream_id=event.stream_id,
                    sequence=event.sequence,
                    previous_hash=event.previous_hash,
                    event_type=event.event_type,
                    actor=event.actor.value,
                    occurred_at=event.occurred_at,
                    payload=event.payload,
                    payload_hash=event.payload_hash,
                    event_hash=event.event_hash,
                    content_hash=event.content_hash,
                )
            )
            return event
        except (AuditValidationError, AuditPersistenceError):
            raise
        except TransactionBoundaryError as error:
            if error.violation is TransactionViolation.ADVISORY_LOCK_ORDER:
                raise AuditPersistenceError(
                    "audit advisory locks must follow the global acquisition order"
                ) from None
            raise AuditPersistenceError("audit stream lock could not be acquired") from None
        except SQLAlchemyError:
            raise AuditPersistenceError("audit event could not be persisted") from None

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
                    "audit connection does not belong to this repository engine"
                ),
                TransactionViolation.INACTIVE_TRANSACTION: (
                    "audit operation requires an active transaction"
                ),
                TransactionViolation.ADVISORY_LOCK_ORDER: (
                    "audit advisory locks must follow the global acquisition order"
                ),
                TransactionViolation.UNSERIALIZED_SQLITE: (
                    "SQLite audit append requires a serialized transaction"
                ),
                TransactionViolation.UNSUPPORTED_DIALECT: (
                    "audit connection uses an unsupported database"
                ),
            }
            raise AuditPersistenceError(messages[error.violation]) from None


def verify_audit_chain(events: Sequence[AuditEvent]) -> AuditVerificationReport:
    """Verify a complete set of audit streams without trusting stored derived fields."""

    if type(events) not in {list, tuple}:
        raise AuditIntegrityError("audit verification input is invalid")
    if any(type(event) is not AuditEvent for event in events):
        raise AuditIntegrityError("audit verification input is invalid")
    heads: dict[str, AuditStreamHead] = {}
    idempotency_keys: set[tuple[str, str]] = set()
    previous_sort_key: tuple[str, int] | None = None
    for event in events:
        sort_key = (event.stream_id, event.sequence)
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise AuditIntegrityError("audit verification input is not deterministically ordered")
        previous = heads.get(event.stream_id)
        expected_sequence = 1 if previous is None else previous.sequence + 1
        expected_previous_hash = AUDIT_GENESIS_HASH if previous is None else previous.event_hash
        _verify_event_content(event)
        if event.sequence != expected_sequence or event.previous_hash != expected_previous_hash:
            raise AuditIntegrityError("audit chain verification failed")
        idempotency_identity = (event.stream_id, event.idempotency_key)
        if idempotency_identity in idempotency_keys:
            raise AuditIntegrityError("audit chain contains a duplicate idempotency key")
        idempotency_keys.add(idempotency_identity)
        heads[event.stream_id] = AuditStreamHead(
            stream_id=event.stream_id,
            sequence=event.sequence,
            event_hash=event.event_hash,
        )
        previous_sort_key = sort_key

    return AuditVerificationReport(
        event_count=len(events),
        stream_heads=tuple(heads.values()),
    )


def _audit_read_relation(
    connection: Connection,
    *,
    writer: AuditWriter | None,
) -> Table:
    if connection.dialect.name != "postgresql":
        return aqa_audit_events
    if writer is None or writer is AuditWriter.CONTROL:
        return _POSTGRES_AUDIT_EVENTS_VIEW
    return _POSTGRES_WRITER_AUDIT_VIEWS[writer]


def _load_selected_events(
    connection: Connection,
    *,
    stream_id: str | None,
    writer: AuditWriter | None,
) -> tuple[AuditEvent, ...]:
    relation = _audit_read_relation(connection, writer=writer)
    statement = select(relation).order_by(relation.c.stream_id, relation.c.sequence)
    if writer is not None:
        stream_prefix = f"{writer.value}:"
        statement = statement.where(
            relation.c.actor == writer.value,
            func.substr(relation.c.stream_id, 1, len(stream_prefix)) == stream_prefix,
        )
    if stream_id is not None:
        statement = statement.where(relation.c.stream_id == stream_id)
    events = _load_events(connection, statement)
    return tuple(sorted(events, key=lambda event: (event.stream_id, event.sequence)))


def _same_append_request(prior: AuditEvent, candidate: AuditEvent) -> bool:
    return (
        prior.stream_id == candidate.stream_id
        and prior.event_type == candidate.event_type
        and prior.actor == candidate.actor
        and prior.occurred_at == candidate.occurred_at
        and prior.payload_hash == candidate.payload_hash
    )


def _expected_stream_head(
    *,
    stream_id: str | None,
    expected_sequence: int | None,
    expected_hash: str | None,
) -> AuditStreamHead | None:
    supplied = (expected_sequence is not None, expected_hash is not None)
    if supplied == (False, False):
        return None
    if (
        stream_id is None
        or expected_sequence is None
        or expected_hash is None
        or supplied != (True, True)
    ):
        raise AuditValidationError(
            "expected audit sequence and hash require exactly one requested stream"
        )
    return AuditStreamHead(
        stream_id=stream_id,
        sequence=expected_sequence,
        event_hash=expected_hash,
    )


def _load_events(connection: Connection, statement: Select[Any]) -> tuple[AuditEvent, ...]:
    rows = connection.execute(statement).mappings()
    try:
        events: list[AuditEvent] = []
        for row in rows:
            events.append(_event_from_row(row))
        return tuple(events)
    except AuditIntegrityError:
        raise
    except (
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise AuditIntegrityError("persisted audit evidence is malformed") from None


def _event_from_row(row: RowMapping) -> AuditEvent:
    try:
        payload_json = canonical_json_bytes(row["payload"]).decode("utf-8")
        return AuditEvent(
            audit_event_id=row["audit_event_id"],
            stream_id=row["stream_id"],
            sequence=row["sequence"],
            previous_hash=row["previous_hash"],
            event_type=row["event_type"],
            actor=AuditWriter(row["actor"]),
            occurred_at=row["occurred_at"],
            payload_hash=row["payload_hash"],
            event_hash=row["event_hash"],
            content_hash=row["content_hash"],
            payload_json=payload_json,
        )
    except (AuditValidationError, CanonicalizationError, KeyError, TypeError, ValueError):
        raise AuditIntegrityError("persisted audit evidence is malformed") from None


def _verify_event_content(event: AuditEvent) -> None:
    try:
        payload_hash = sha256_hex(event.payload)
        event_hash = audit_event_hash(
            stream_id=event.stream_id,
            sequence=event.sequence,
            previous_hash=event.previous_hash,
            event_type=event.event_type,
            actor=event.actor,
            occurred_at=event.occurred_at,
            payload_hash=event.payload_hash,
        )
    except (AuditValidationError, CanonicalizationError):
        raise AuditIntegrityError("audit chain verification failed") from None
    if (
        event.payload_hash != payload_hash
        or event.event_hash != event_hash
        or event.content_hash != event_hash
        or event.audit_event_id != f"audit_{event_hash}"
    ):
        raise AuditIntegrityError("audit chain verification failed")


def _validate_stream_id(stream_id: str) -> None:
    AuditStreamHead(
        stream_id=stream_id,
        sequence=1,
        event_hash=AUDIT_GENESIS_HASH,
    )
