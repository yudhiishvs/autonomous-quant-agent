"""Stable platform exceptions exposed without importing implementation modules."""

from __future__ import annotations


class CanonicalizationError(ValueError):
    """Raised when a value cannot safely enter a canonical hash input."""


class DomainValidationError(ValueError):
    """Raised when a value violates an internal platform domain invariant."""


class ExperimentConfigError(ValueError):
    """Raised when an experiment file cannot be safely loaded and validated."""


class ExperimentHashMismatchError(ExperimentConfigError):
    """Raised when a validated experiment does not match its pinned digest."""


class RuntimeSettingsError(ValueError):
    """Raised when a service runtime cannot be composed through the safe boundary."""


class SecretFileError(ValueError):
    """Raised when a secret file cannot be loaded without weakening its boundary."""


class LocalSecretBootstrapError(ValueError):
    """Raised when local secret material cannot be created or preserved safely."""


class AuditValidationError(ValueError):
    """Raised when an audit event cannot safely enter the persistence boundary."""


class AuditIntegrityError(RuntimeError):
    """Raised when persisted audit evidence fails independent chain verification."""


class AuditPersistenceError(RuntimeError):
    """Raised when an audit event cannot be persisted without weakening its invariants."""
