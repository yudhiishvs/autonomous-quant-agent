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
