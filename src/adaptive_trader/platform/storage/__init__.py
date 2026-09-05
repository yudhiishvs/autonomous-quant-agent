"""Persistence infrastructure for the generic platform."""

from adaptive_trader.platform.storage.datasets import (
    DatasetManifestPersistenceError,
    DatasetManifestRegistration,
    DatasetManifestRepository,
    freeze_and_register_dataset,
)
from adaptive_trader.platform.storage.engine import (
    create_platform_engine,
    create_platform_read_only_engine,
)
from adaptive_trader.platform.storage.repositories import AuditRepository, verify_audit_chain
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
)

__all__ = [
    "AuditRepository",
    "DatasetManifestPersistenceError",
    "DatasetManifestRegistration",
    "DatasetManifestRepository",
    "PostgresAdvisoryLockNamespace",
    "PostgresAdvisoryLockRequest",
    "SerializedTransactionCoordinator",
    "create_platform_engine",
    "create_platform_read_only_engine",
    "freeze_and_register_dataset",
    "verify_audit_chain",
]
