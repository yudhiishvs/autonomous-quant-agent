"""Structured JSON logging with a centralized, closed redaction boundary."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TextIO, cast

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes

REDACTED = "<redacted>"

_SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "password",
    "token",
    "authorization",
    "credential",
    "api_key",
    "private_key",
    "cookie",
    "connection_string",
    "database_url",
)
_SENSITIVE_COMPACT_FRAGMENTS = ("apikey", "privatekey", "connectionstring", "databaseurl")
_SECRET_TEXT = re.compile(
    r"(?:bearer\s+\S+|-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----|"
    r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@)",
    re.ASCII | re.IGNORECASE,
)
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", re.ASCII)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", re.ASCII)
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_SERVICE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", re.ASCII)
_STATE_TRANSITION = re.compile(r"^[A-Z_]{2,32}->[A-Z_]{2,32}$", re.ASCII)
_REQUIRED_FIELDS = frozenset({"event_type", "service", "severity"})
_IDENTIFIER_FIELDS = frozenset(
    {
        "client_order_id",
        "correlation_id",
        "execution_plan_id",
        "experiment_id",
        "risk_decision_id",
        "signal_id",
        "slot_id",
    }
)
_OPTIONAL_FIELDS = _IDENTIFIER_FIELDS | frozenset({"reason_code", "state_transition", "symbol"})
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_EXTERNAL_LOGGERS = (
    "sqlalchemy",
    "sqlalchemy.engine",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)


class LogSeverity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class StructuredLogEvent:
    """The complete set of fields allowed into operational logs."""

    severity: LogSeverity
    event_type: str
    correlation_id: str | None = None
    experiment_id: str | None = None
    slot_id: str | None = None
    signal_id: str | None = None
    risk_decision_id: str | None = None
    execution_plan_id: str | None = None
    client_order_id: str | None = None
    symbol: str | None = None
    state_transition: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.severity) is not LogSeverity:
            raise ValueError("log severity is invalid")
        if type(self.event_type) is not str or _EVENT_TYPE.fullmatch(self.event_type) is None:
            raise ValueError("log event type is invalid")
        for field_name in (
            "correlation_id",
            "experiment_id",
            "slot_id",
            "signal_id",
            "risk_decision_id",
            "execution_plan_id",
            "client_order_id",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                type(value) is not str
                or _IDENTIFIER.fullmatch(value) is None
                or _SECRET_TEXT.search(value) is not None
            ):
                raise ValueError(f"log {field_name} is invalid")
        if self.reason_code is not None and (
            type(self.reason_code) is not str or _REASON_CODE.fullmatch(self.reason_code) is None
        ):
            raise ValueError("log reason_code is invalid")
        if self.symbol is not None and (
            type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None
        ):
            raise ValueError("log symbol is invalid")
        if self.state_transition is not None and (
            type(self.state_transition) is not str
            or _STATE_TRANSITION.fullmatch(self.state_transition) is None
        ):
            raise ValueError("log state transition is invalid")

    def fields(self, *, service: str) -> dict[str, JsonValue]:
        if type(service) is not str or _SERVICE.fullmatch(service) is None:
            raise ValueError("log service is invalid")
        values: dict[str, JsonValue] = {
            "severity": self.severity.value,
            "service": service,
            "event_type": self.event_type,
        }
        for field_name in (
            "correlation_id",
            "experiment_id",
            "slot_id",
            "signal_id",
            "risk_decision_id",
            "execution_plan_id",
            "client_order_id",
            "symbol",
            "state_transition",
            "reason_code",
        ):
            value = getattr(self, field_name)
            if value is not None:
                values[field_name] = cast(str, value)
        return values


def redact_sensitive(value: object, *, key: str | None = None) -> JsonValue:
    """Return a bounded JSON-safe copy with secret-bearing keys and values redacted."""

    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if value is None or type(value) in {bool, int, float}:
        return cast(JsonValue, value)
    if type(value) is str:
        text = value
        return REDACTED if _SECRET_TEXT.search(text) is not None else text[:512]
    if type(value) in {list, tuple}:
        return [
            redact_sensitive(item) for item in cast(list[object] | tuple[object, ...], value)[:64]
        ]
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        redacted: dict[str, JsonValue] = {}
        for raw_key, item in list(mapping.items())[:64]:
            safe_key = str(raw_key)[:128]
            redacted[safe_key] = redact_sensitive(item, key=safe_key)
        return redacted
    return REDACTED


class RedactingJsonFormatter(logging.Formatter):
    """Render only closed structured fields and never render exceptions or headers."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="microseconds"
        )
        timestamp = timestamp.replace("+00:00", "Z")
        fields = _validated_log_fields(record)
        if fields is None:
            fields = _rejected_log_fields(
                "exception_redacted" if record.exc_info is not None else "unstructured_message"
            )
        payload = {"timestamp": timestamp, **fields}
        if record.exc_info is not None:
            payload["reason_code"] = "exception_redacted"
        return canonical_json_bytes(payload).decode("utf-8")


class StructuredLogger:
    """Small adapter that prevents arbitrary objects reaching the logging framework."""

    def __init__(self, logger: logging.Logger, *, service: str) -> None:
        if not isinstance(logger, logging.Logger):
            raise TypeError("structured logger requires a logging.Logger")
        if type(service) is not str or _SERVICE.fullmatch(service) is None:
            raise ValueError("log service is invalid")
        self._logger = logger
        self._service = service

    def emit(self, event: StructuredLogEvent) -> None:
        if type(event) is not StructuredLogEvent:
            raise TypeError("structured logger requires a structured event")
        self._logger.log(
            logging.getLevelName(event.severity.value),
            event.fields(service=self._service),
        )


def configure_json_logging(*, service: str, stream: TextIO) -> StructuredLogger:
    """Install one closed formatter on application, root, and framework loggers."""

    if type(service) is not str or _SERVICE.fullmatch(service) is None:
        raise ValueError("log service is invalid")
    if not hasattr(stream, "write"):
        raise TypeError("structured logging stream is invalid")
    logger = logging.getLogger(f"adaptive_trader.platform.{service}")
    logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _EXTERNAL_LOGGERS:
        external = logging.getLogger(name)
        external.handlers.clear()
        external.setLevel(logging.WARNING)
        external.propagate = True
    return StructuredLogger(logger, service=service)


def _validated_log_fields(record: logging.LogRecord) -> dict[str, JsonValue] | None:
    if type(record.msg) is not dict:
        return None
    raw = cast(dict[object, object], record.msg)
    if (
        any(type(key) is not str for key in raw)
        or not _REQUIRED_FIELDS.issubset(raw)
        or not frozenset(cast(str, key) for key in raw).issubset(_ALLOWED_FIELDS)
    ):
        return None
    severity = raw.get("severity")
    service = raw.get("service")
    event_type = raw.get("event_type")
    if (
        type(severity) is not str
        or severity not in {item.value for item in LogSeverity}
        or severity != record.levelname
        or type(service) is not str
        or _SERVICE.fullmatch(service) is None
        or type(event_type) is not str
        or _EVENT_TYPE.fullmatch(event_type) is None
    ):
        return None
    for field_name in _IDENTIFIER_FIELDS:
        value = raw.get(field_name)
        if value is not None and (
            type(value) is not str
            or _IDENTIFIER.fullmatch(value) is None
            or _SECRET_TEXT.search(value) is not None
        ):
            return None
    reason_code = raw.get("reason_code")
    if reason_code is not None and (
        type(reason_code) is not str or _REASON_CODE.fullmatch(reason_code) is None
    ):
        return None
    symbol = raw.get("symbol")
    if symbol is not None and (type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None):
        return None
    transition = raw.get("state_transition")
    if transition is not None and (
        type(transition) is not str or _STATE_TRANSITION.fullmatch(transition) is None
    ):
        return None
    return {cast(str, key): cast(JsonValue, value) for key, value in raw.items()}


def _rejected_log_fields(reason_code: str) -> dict[str, JsonValue]:
    return {
        "severity": LogSeverity.ERROR.value,
        "service": "logging",
        "event_type": "security.unstructured_log_rejected",
        "reason_code": reason_code,
    }


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    compact = normalized.replace("_", "")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS) or any(
        fragment in compact for fragment in _SENSITIVE_COMPACT_FRAGMENTS
    )
