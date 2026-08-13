"""Typed failures for safe live paper operation."""

from __future__ import annotations


class AdaptiveTraderError(RuntimeError):
    """Base class for operational failures raised by the live subsystem."""


class SafetyViolation(AdaptiveTraderError):
    """Raised when an operation would violate a non-negotiable safety rule."""


class CredentialError(SafetyViolation):
    """Raised when explicit paper-account credentials are absent or unusable."""


class BrokerConnectionError(AdaptiveTraderError):
    """Raised when a paper-broker read or stream operation fails."""


class AmbiguousSubmissionError(AdaptiveTraderError):
    """Raised when it is unknown whether a paper broker accepted an order."""


class InvalidOrderTransition(SafetyViolation):
    """Raised when an order event does not follow the local state machine."""


class PersistenceError(AdaptiveTraderError):
    """Raised when durable state cannot be safely read or written."""


class SchemaVersionError(PersistenceError):
    """Raised when a database schema cannot be used by this application version."""


class ReconciliationBlocked(SafetyViolation):
    """Raised when unresolved broker/local discrepancies prohibit new orders."""


class DataFreshnessError(SafetyViolation):
    """Raised when required market data is missing, stale, or unhealthy."""
