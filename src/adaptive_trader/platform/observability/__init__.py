"""Central structured logging contracts shared by every runtime image.

Health and metrics are imported from their concrete modules. Keeping this initializer limited to
logging lets the data-only image reuse the redaction boundary without control-plane dependencies
or dynamic-import authority.
"""

from __future__ import annotations

from adaptive_trader.platform.observability.logging import (
    REDACTED,
    LogSeverity,
    RedactingJsonFormatter,
    StructuredLogEvent,
    StructuredLogger,
    configure_json_logging,
    redact_sensitive,
)

__all__ = [
    "REDACTED",
    "LogSeverity",
    "RedactingJsonFormatter",
    "StructuredLogEvent",
    "StructuredLogger",
    "configure_json_logging",
    "redact_sensitive",
]
