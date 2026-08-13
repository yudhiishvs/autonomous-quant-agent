"""Structured logging with credential and authorization redaction."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from adaptive_trader.constants import UTC

_SENSITIVE_PATTERNS = (
    # Consume the complete authorization value, including schemes with a
    # separate credential token (Bearer, Basic, and similar forms).
    re.compile(
        r"(?i)([\"']?authorization[\"']?\s*[:=]\s*)"
        r"(?:[\"']?)([^,\r\n;}]+)(?:[\"']?)"
    ),
    # Mapping and key/value forms. Optional quotes are consumed deliberately:
    # preserving syntactic JSON is less important than removing the value.
    re.compile(
        r"(?i)([\"']?(?:"
        r"secret(?:[_-]?key)?|"
        r"api[_-]?(?:key(?:[_-]?id)?|secret(?:[_-]?key)?)|"
        r"client[_-]?secret|access[_-]?token|token"
        r")[\"']?\s*[:=]\s*)"
        r"(?:[\"']?)([^\"'\s,;}\]&]+)(?:[\"']?)"
    ),
    re.compile(r"(?i)(bearer\s+)([^\"'\s,;}\]&]+)"),
)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """Return text with known credentials and common authorization forms removed."""

    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Redact secrets from both formatted arguments and exception text."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(
                logging.Formatter().formatException(record.exc_info), self._secrets
            )
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per operational event."""

    _CONTEXT_FIELDS = ("event_type", "run_id", "decision_id", "order_id", "symbol", "mode")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in self._CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def configure_logging(
    runtime_directory: str | Path,
    *,
    level: int = logging.INFO,
    secrets: tuple[str, ...] = (),
) -> Path:
    """Configure human console logs and rotating JSON files.

    The function is idempotent for handlers it owns and never inspects or logs
    the environment.
    """

    log_directory = Path(runtime_directory) / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "adaptive_portfolio_agent.jsonl"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_adaptive_trader_handler", False):
            root.removeHandler(handler)
            handler.close()

    redactor = RedactingFilter(secrets)
    # A host process (pytest, a notebook, a process supervisor, or another
    # library) may have installed a root handler before this application is
    # configured.  Filter those handlers too: otherwise the same LogRecord can
    # be written once by an unredacted pre-existing handler before reaching our
    # console/file handlers.  Replace only this application's filter type so
    # repeated configuration does not accumulate stale credential sets.
    for handler in root.handlers:
        for existing_filter in tuple(handler.filters):
            if isinstance(existing_filter, RedactingFilter):
                handler.removeFilter(existing_filter)
        handler.addFilter(redactor)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console.addFilter(redactor)
    console._adaptive_trader_handler = True  # type: ignore[attr-defined]

    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(redactor)
    file_handler._adaptive_trader_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)
    root.addHandler(file_handler)
    return log_path
