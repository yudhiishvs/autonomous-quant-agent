"""Shared transaction serialization for platform repositories."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from sqlalchemy import Connection, Engine, text

from adaptive_trader.platform.hashing import sha256_hex

_SQLITE_SERIALIZED_TRANSACTION_KEY = "aqa_platform_immediate_transaction"
_SQLITE_SERIALIZED_TRANSACTION_SENTINEL = object()
_POSTGRES_ADVISORY_LOCK_STATE_KEY = "aqa_platform_postgres_advisory_lock_state"
_POSTGRES_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:lock_namespace, :lock_resource)")
_LOCK_RESOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/|-]{0,511}$", re.ASCII)
_MIN_SIGNED_32_BIT_INTEGER = -(2**31)
_MAX_SIGNED_32_BIT_INTEGER = (2**31) - 1
_SUPPORTED_DIALECTS = frozenset({"postgresql", "sqlite"})


class PostgresAdvisoryLockNamespace(IntEnum):
    """Global acquisition order for platform transaction-scoped advisory locks."""

    MARKET_DATA_WATERMARK = 10
    MARKET_DATA_IDENTITY = 20
    DATASET_MANIFEST = 30
    RISK_SIGNAL = 40
    RISK_LATCH = 50
    AUDIT = 90


@dataclass(frozen=True, order=True, slots=True)
class PostgresAdvisoryLockRequest:
    """One deterministic two-integer PostgreSQL advisory lock request."""

    namespace: PostgresAdvisoryLockNamespace
    resource_key: int

    def __post_init__(self) -> None:
        if type(self.namespace) is not PostgresAdvisoryLockNamespace:
            raise TypeError("advisory lock namespace must use the closed contract")
        if (
            type(self.resource_key) is not int
            or self.resource_key < _MIN_SIGNED_32_BIT_INTEGER
            or self.resource_key > _MAX_SIGNED_32_BIT_INTEGER
        ):
            raise ValueError("advisory lock resource key must be a signed 32-bit integer")

    @classmethod
    def for_resource(
        cls,
        namespace: PostgresAdvisoryLockNamespace,
        resource_ref: str,
    ) -> PostgresAdvisoryLockRequest:
        """Derive a stable serialization key from a bounded resource reference."""

        if type(namespace) is not PostgresAdvisoryLockNamespace:
            raise TypeError("advisory lock namespace must use the closed contract")
        if type(resource_ref) is not str or _LOCK_RESOURCE_PATTERN.fullmatch(resource_ref) is None:
            raise ValueError("advisory lock resource reference is invalid")
        unsigned = int(
            sha256_hex(("postgres-advisory-lock", int(namespace), resource_ref))[:8],
            16,
        )
        signed = unsigned if unsigned < 2**31 else unsigned - 2**32
        return cls(namespace=namespace, resource_key=signed)


@dataclass(slots=True)
class _PostgresAdvisoryLockState:
    transaction: object
    acquired: set[PostgresAdvisoryLockRequest]
    maximum: PostgresAdvisoryLockRequest | None = None


class TransactionViolation(StrEnum):
    """Closed reasons a caller-supplied transaction cannot be trusted."""

    FOREIGN_CONNECTION = "foreign_connection"
    INACTIVE_TRANSACTION = "inactive_transaction"
    ADVISORY_LOCK_ORDER = "advisory_lock_order"
    UNSERIALIZED_SQLITE = "unserialized_sqlite"
    UNSUPPORTED_DIALECT = "unsupported_dialect"


class TransactionBoundaryError(RuntimeError):
    """Context-free failure raised by the shared transaction boundary."""

    def __init__(self, violation: TransactionViolation) -> None:
        if type(violation) is not TransactionViolation:
            raise TypeError("transaction violation must use the closed reason contract")
        self.violation = violation
        super().__init__("platform transaction boundary rejected the connection")


class SerializedTransactionCoordinator:
    """Own transactions whose SQLite serialization proof is shared across repositories."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("transaction coordinator requires a concrete SQLAlchemy Engine")
        if engine.dialect.name not in _SUPPORTED_DIALECTS:
            raise ValueError("transaction coordinator requires PostgreSQL or SQLite")
        self._engine = engine

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Open a commit-or-rollback transaction serialized before any SQLite read."""

        connection = self._engine.connect()
        try:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.info[_SQLITE_SERIALIZED_TRANSACTION_KEY] = (
                    _SQLITE_SERIALIZED_TRANSACTION_SENTINEL
                )
            else:
                connection.begin()
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        finally:
            if connection.dialect.name == "sqlite":
                connection.info.pop(_SQLITE_SERIALIZED_TRANSACTION_KEY, None)
            else:
                connection.info.pop(_POSTGRES_ADVISORY_LOCK_STATE_KEY, None)
            connection.close()

    def acquire_postgres_advisory_lock(
        self,
        connection: Connection,
        request: PostgresAdvisoryLockRequest,
    ) -> None:
        """Acquire one lock monotonically, rejecting inversions before sending SQL."""

        self.validate_connection(connection, require_serialized_sqlite=False)
        if type(request) is not PostgresAdvisoryLockRequest:
            raise TypeError("advisory lock request must use the typed contract")
        if connection.dialect.name != "postgresql":
            return
        transaction = connection.get_transaction()
        if transaction is None:
            raise TransactionBoundaryError(TransactionViolation.INACTIVE_TRANSACTION)
        raw_state = connection.info.get(_POSTGRES_ADVISORY_LOCK_STATE_KEY)
        if not isinstance(raw_state, _PostgresAdvisoryLockState) or (
            raw_state.transaction is not transaction
        ):
            state = _PostgresAdvisoryLockState(transaction=transaction, acquired=set())
            connection.info[_POSTGRES_ADVISORY_LOCK_STATE_KEY] = state
        else:
            state = raw_state
        if request in state.acquired:
            return
        if state.maximum is not None and request < state.maximum:
            raise TransactionBoundaryError(TransactionViolation.ADVISORY_LOCK_ORDER)
        connection.execute(
            _POSTGRES_ADVISORY_LOCK,
            {
                "lock_namespace": int(request.namespace),
                "lock_resource": request.resource_key,
            },
        )
        state.acquired.add(request)
        state.maximum = request

    def acquire_postgres_advisory_locks(
        self,
        connection: Connection,
        requests: Sequence[PostgresAdvisoryLockRequest],
    ) -> None:
        """Acquire a deterministic lock set in global order without duplicate calls."""

        if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
            raise TypeError("advisory lock requests must use a concrete sequence")
        if any(type(request) is not PostgresAdvisoryLockRequest for request in requests):
            raise TypeError("advisory lock requests must use the typed contract")
        for request in sorted(set(requests)):
            self.acquire_postgres_advisory_lock(connection, request)

    def validate_connection(
        self,
        connection: Connection,
        *,
        require_serialized_sqlite: bool,
    ) -> None:
        """Validate ownership, activity, dialect, and optional SQLite serialization proof."""

        if not isinstance(connection, Connection) or connection.engine is not self._engine:
            raise TransactionBoundaryError(TransactionViolation.FOREIGN_CONNECTION)
        dialect = connection.dialect.name
        if dialect not in _SUPPORTED_DIALECTS:
            raise TransactionBoundaryError(TransactionViolation.UNSUPPORTED_DIALECT)
        if not connection.in_transaction():
            raise TransactionBoundaryError(TransactionViolation.INACTIVE_TRANSACTION)
        if (
            require_serialized_sqlite
            and dialect == "sqlite"
            and connection.info.get(_SQLITE_SERIALIZED_TRANSACTION_KEY)
            is not _SQLITE_SERIALIZED_TRANSACTION_SENTINEL
        ):
            raise TransactionBoundaryError(TransactionViolation.UNSERIALIZED_SQLITE)
