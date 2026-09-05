"""Pure deterministic identity, time, and numeric domain boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DecimalException,
    localcontext,
)
from enum import StrEnum
from typing import cast
from uuid import UUID

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.constants import (
    MAX_AUDIT_CONTAINER_ITEMS,
    MAX_AUDIT_EVENT_TYPE_LENGTH,
    MAX_AUDIT_PAYLOAD_BYTES,
    MAX_AUDIT_STREAM_ID_LENGTH,
    MAX_DETERMINISTIC_ID_PREFIX_LENGTH,
    MAX_DOMAIN_DECIMAL_DIGITS,
    MAX_DOMAIN_DECIMAL_EXPONENT,
    MAX_SIGNED_64_BIT_INTEGER,
    MIN_DOMAIN_DECIMAL_EXPONENT,
    SHA256_HEX_LENGTH,
)
from adaptive_trader.platform.errors import (
    AuditValidationError,
    CanonicalizationError,
    DomainValidationError,
)
from adaptive_trader.platform.hashing import sha256_hex

_ID_PREFIX_PATTERN = re.compile(
    rf"^[a-z][a-z0-9_]{{0,{MAX_DETERMINISTIC_ID_PREFIX_LENGTH - 1}}}$",
    flags=re.ASCII,
)
_SHA256_PATTERN = re.compile(rf"^[0-9a-f]{{{SHA256_HEX_LENGTH}}}$", flags=re.ASCII)
_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$", flags=re.ASCII)
_AUDIT_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:/-]*$", flags=re.ASCII)
_AUDIT_EVENT_TYPE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$",
    flags=re.ASCII,
)
_AUDIT_RESOURCE_REF_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9_.:/-]*[a-z0-9])?$",
    flags=re.ASCII,
)
_AUDIT_EVENT_ID_PATTERN = re.compile(rf"^audit_[0-9a-f]{{{SHA256_HEX_LENGTH}}}$", re.ASCII)
_AUDIT_PAYLOAD_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$", re.ASCII)
_AUDIT_CONTENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}_[0-9a-f]{64}$", re.ASCII)
_AUDIT_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.ASCII,
)
_AUDIT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$", re.ASCII)
_AUDIT_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", re.ASCII)
_AUDIT_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", re.ASCII)
_AUDIT_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$",
    re.ASCII,
)
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])", re.ASCII)
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])", re.ASCII)
_AUDIT_KEY_SEPARATOR = re.compile(r"[-.]+", re.ASCII)
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "connection_string",
    "cookie",
    "credential",
    "database_url",
    "password",
    "private_key",
    "secret",
    "token",
)
_SENSITIVE_COMPACT_KEY_FRAGMENTS = ("apikey", "connectionstring", "databaseurl", "privatekey")
_SAFE_FENCING_KEY = "fencing_token"
_AUDIT_SLUG_KEYS = frozenset(
    {
        "action",
        "adjustment",
        "decision_type",
        "error_code",
        "event_type",
        "feed",
        "from_state",
        "job_type",
        "mode",
        "outcome",
        "provider",
        "reason_code",
        "role",
        "service",
        "source",
        "state",
        "status",
        "timeframe",
        "to_state",
    }
)
_AUDIT_INTEGER_KEYS = frozenset(
    {
        "attempt",
        "attempt_count",
        "count",
        "fencing_token",
        "ordinal",
        "revision",
        "sequence",
        "version",
    }
)
_AUDIT_COUNT_OBJECT_KEYS = frozenset({"counts"})
_AUDIT_SYMBOL_KEYS = frozenset({"symbol"})
_AUDIT_SYMBOL_LIST_KEYS = frozenset({"symbols"})
_AUDIT_SLUG_LIST_KEYS = frozenset({"flags", "reason_codes"})
_AUDIT_ID_KEYS = frozenset(
    {
        "bar_id",
        "broker_order_id",
        "client_order_id",
        "correlation_id",
        "dataset_id",
        "execution_plan_id",
        "experiment_id",
        "fill_id",
        "gap_id",
        "idempotency_key",
        "incident_id",
        "job_id",
        "order_intent_id",
        "reconciliation_id",
        "risk_decision_id",
        "run_id",
        "signal_id",
        "slot_id",
        "strategy_id",
    }
)
_AUDIT_ID_LIST_KEYS = frozenset(
    {
        "bar_ids",
        "event_ids",
        "fill_ids",
        "order_ids",
        "signal_ids",
    }
)
_AUDIT_HASH_KEYS = frozenset(
    {
        "artifact_hash",
        "configuration_hash",
        "content_hash",
        "dataset_hash",
        "experiment_hash",
        "manifest_hash",
        "payload_hash",
        "source_hash",
    }
)
_AUDIT_HASH_LIST_KEYS = frozenset({"artifact_hashes", "content_hashes", "source_hashes"})
_AUDIT_TIMESTAMP_KEYS = frozenset(
    {
        "completed_at",
        "created_at",
        "detected_at",
        "effective_at",
        "filled_at",
        "occurred_at",
        "received_at",
        "session_close_at",
        "session_open_at",
        "started_at",
        "submitted_at",
        "updated_at",
    }
)
_AUDIT_BOOLEAN_KEYS = frozenset(
    {
        "has_gap",
        "is_correction",
        "is_replay",
        "submission_allowed",
        "submission_enabled",
        "was_recovered",
    }
)
_AUDIT_DECIMAL_KEYS = frozenset(
    {
        "available_cash",
        "average_price",
        "close_price",
        "equity",
        "fee",
        "filled_quantity",
        "high_price",
        "limit_price",
        "low_price",
        "notional",
        "open_price",
        "quantity",
        "stop_price",
        "target_price",
        "target_weight",
        "volume",
    }
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?:secret|password|token|authorization|credential|api[-_.]?key|private[-_.]?key|"
    r"cookie|connection[-_.]?string|database[-_.]?url)\s*[:=]",
    re.ASCII | re.IGNORECASE,
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@",
    re.ASCII | re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://", re.ASCII | re.IGNORECASE)
_PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----", re.ASCII)
_BEARER_PATTERN = re.compile(r"(?:^|\s)bearer\s+\S+", re.ASCII | re.IGNORECASE)
_JWT_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$",
    re.ASCII,
)


class DecimalRounding(StrEnum):
    """Closed decimal rounding modes accepted by platform quantization."""

    CEILING = ROUND_CEILING
    DOWN = ROUND_DOWN
    FLOOR = ROUND_FLOOR
    HALF_DOWN = ROUND_HALF_DOWN
    HALF_EVEN = ROUND_HALF_EVEN
    HALF_UP = ROUND_HALF_UP
    UP = ROUND_UP
    ZERO_FIVE_UP = ROUND_05UP


class AuditWriter(StrEnum):
    """Closed runtime identities permitted to author audit evidence."""

    COLLECTOR = "aqa_collector"
    CONTROL = "aqa_control"
    EXECUTION = "aqa_execution"
    SCHEDULER = "aqa_scheduler"
    STRATEGY = "aqa_strategy"


_AUDIT_EVENT_FAMILIES: dict[AuditWriter, tuple[str, ...]] = {
    AuditWriter.COLLECTOR: (
        "bar.",
        "collector.",
        "data.",
        "dataset.",
        "gap.",
        "security.",
    ),
    AuditWriter.SCHEDULER: ("scheduler.", "security.", "slot."),
    AuditWriter.STRATEGY: ("security.", "signal.", "strategy."),
    AuditWriter.EXECUTION: (
        "execution.",
        "fill.",
        "incident.",
        "intent.",
        "latch.",
        "order.",
        "reconciliation.",
        "risk.",
        "security.",
    ),
    AuditWriter.CONTROL: ("control.", "experiment.", "job.", "operator.", "security."),
}


@dataclass(frozen=True, slots=True)
class AuditPayload:
    """Closed, immutable, canonical metadata accepted by the audit boundary.

    Every field name selects a bounded value semantic (identifier, hash, timestamp, numeric,
    state slug, symbol, or explicitly typed container). Unknown/free-form fields are rejected,
    which prevents accidental persistence of arbitrary request or exception text.
    """

    payload_json: str
    payload_hash: str

    def __post_init__(self) -> None:
        computed_hash = _validate_stored_audit_payload(self.payload_json)
        _require_sha256(self.payload_hash, field_name="payload hash")
        if self.payload_hash != computed_hash:
            raise AuditValidationError("audit payload hash is invalid")

    @classmethod
    def from_mapping(cls, value: object) -> AuditPayload:
        """Validate a closed metadata mapping and freeze its canonical representation."""

        payload_json, payload_hash = _prepare_audit_payload(value)
        return cls(payload_json=payload_json, payload_hash=payload_hash)

    @property
    def value(self) -> dict[str, JsonValue]:
        """Return a fresh decoded copy without exposing mutable internal state."""

        return cast(dict[str, JsonValue], json.loads(self.payload_json))

    @property
    def idempotency_key(self) -> str:
        """Return the validated caller-stable key bound into the payload hash."""

        return cast(str, self.value["idempotency_key"])


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Immutable audit evidence with deterministic identity and canonical payload.

    ``payload_hash`` is SHA-256 over the canonical payload object. ``event_hash`` is SHA-256
    over the canonical tuple ``(stream_id, sequence, previous_hash, event_type, actor,
    occurred_at, payload_hash)``. The replayable ID is exactly ``audit_<event_hash>``.

    Construction validates representation and secret-safety but deliberately does not assert
    that the stored hashes and ID agree. That derived relationship is checked independently by
    the audit verifier so persisted tampering cannot be normalized away while loading rows.
    """

    audit_event_id: str
    stream_id: str
    sequence: int
    previous_hash: str
    event_type: str
    actor: AuditWriter
    occurred_at: datetime
    payload_hash: str
    event_hash: str
    content_hash: str
    payload_json: str

    def __post_init__(self) -> None:
        if (
            type(self.audit_event_id) is not str
            or _AUDIT_EVENT_ID_PATTERN.fullmatch(self.audit_event_id) is None
        ):
            raise AuditValidationError("audit event ID is invalid")
        _validate_audit_authority(
            stream_id=self.stream_id,
            event_type=self.event_type,
            actor=self.actor,
        )
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or self.sequence > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise AuditValidationError("audit sequence is invalid")
        _require_sha256(self.previous_hash, field_name="previous hash")
        _require_sha256(self.payload_hash, field_name="payload hash")
        _require_sha256(self.event_hash, field_name="event hash")
        _require_sha256(self.content_hash, field_name="content hash")
        try:
            occurred_at = require_utc_instant(self.occurred_at, field_name="occurred_at")
        except DomainValidationError:
            raise AuditValidationError("audit timestamp is invalid") from None
        object.__setattr__(self, "occurred_at", occurred_at)
        _validate_stored_audit_payload(self.payload_json)

    @classmethod
    def create(
        cls,
        *,
        stream_id: str,
        sequence: int,
        previous_hash: str,
        event_type: str,
        actor: AuditWriter,
        occurred_at: datetime,
        payload: AuditPayload,
    ) -> AuditEvent:
        """Create a new event after bounded validation and deterministic hashing."""

        _validate_audit_authority(stream_id=stream_id, event_type=event_type, actor=actor)
        if type(sequence) is not int or sequence < 1 or sequence > MAX_SIGNED_64_BIT_INTEGER:
            raise AuditValidationError("audit sequence is invalid")
        _require_sha256(previous_hash, field_name="previous hash")
        try:
            normalized_occurred_at = require_utc_instant(occurred_at, field_name="occurred_at")
        except DomainValidationError:
            raise AuditValidationError("audit timestamp is invalid") from None
        if type(payload) is not AuditPayload:
            raise AuditValidationError("audit payload must use the typed payload contract")
        event_hash = audit_event_hash(
            stream_id=stream_id,
            sequence=sequence,
            previous_hash=previous_hash,
            event_type=event_type,
            actor=actor,
            occurred_at=normalized_occurred_at,
            payload_hash=payload.payload_hash,
        )
        return cls(
            audit_event_id=f"audit_{event_hash}",
            stream_id=stream_id,
            sequence=sequence,
            previous_hash=previous_hash,
            event_type=event_type,
            actor=actor,
            occurred_at=normalized_occurred_at,
            payload_hash=payload.payload_hash,
            event_hash=event_hash,
            content_hash=event_hash,
            payload_json=payload.payload_json,
        )

    @property
    def payload(self) -> dict[str, JsonValue]:
        """Return a fresh decoded copy; callers cannot mutate the stored canonical payload."""

        return cast(dict[str, JsonValue], json.loads(self.payload_json))

    @property
    def audit_payload(self) -> AuditPayload:
        """Reconstruct the validated typed payload without trusting stored hashes."""

        computed = AuditPayload.from_mapping(self.payload)
        if computed.payload_hash != self.payload_hash:
            raise AuditValidationError("audit payload hash is invalid")
        return computed

    @property
    def idempotency_key(self) -> str:
        """Return the caller-stable key independently recovered from the payload."""

        return self.audit_payload.idempotency_key


@dataclass(frozen=True, slots=True)
class AuditStreamHead:
    """Verified terminal position for one audit stream."""

    stream_id: str
    sequence: int
    event_hash: str

    def __post_init__(self) -> None:
        _audit_writer_for_stream(self.stream_id)
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or self.sequence > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise AuditValidationError("audit sequence is invalid")
        _require_sha256(self.event_hash, field_name="event hash")


@dataclass(frozen=True, slots=True)
class AuditVerificationReport:
    """Summary returned only after every selected stream verifies successfully."""

    event_count: int
    stream_heads: tuple[AuditStreamHead, ...]

    def __post_init__(self) -> None:
        if type(self.event_count) is not int or self.event_count < 0:
            raise AuditValidationError("audit event count is invalid")
        if type(self.stream_heads) is not tuple:
            raise AuditValidationError("audit stream heads must be an immutable tuple")
        if any(type(head) is not AuditStreamHead for head in self.stream_heads):
            raise AuditValidationError("audit stream heads contain an invalid element")
        stream_ids = tuple(head.stream_id for head in self.stream_heads)
        if stream_ids != tuple(sorted(stream_ids)) or len(stream_ids) != len(set(stream_ids)):
            raise AuditValidationError("audit stream heads must be uniquely ordered")
        if self.event_count != sum(head.sequence for head in self.stream_heads):
            raise AuditValidationError("audit report count does not match its stream heads")


def audit_event_hash(
    *,
    stream_id: str,
    sequence: int,
    previous_hash: str,
    event_type: str,
    actor: AuditWriter,
    occurred_at: datetime,
    payload_hash: str,
) -> str:
    """Hash exactly the canonical per-stream audit tuple defined by the platform contract."""

    _validate_audit_authority(stream_id=stream_id, event_type=event_type, actor=actor)
    if type(sequence) is not int or sequence < 1 or sequence > MAX_SIGNED_64_BIT_INTEGER:
        raise AuditValidationError("audit sequence is invalid")
    _require_sha256(previous_hash, field_name="previous hash")
    _require_sha256(payload_hash, field_name="payload hash")
    try:
        normalized_occurred_at = require_utc_instant(occurred_at, field_name="occurred_at")
    except DomainValidationError:
        raise AuditValidationError("audit timestamp is invalid") from None
    return sha256_hex(
        (
            stream_id,
            sequence,
            previous_hash,
            event_type,
            actor,
            normalized_occurred_at,
            payload_hash,
        )
    )


@dataclass(frozen=True, slots=True)
class DeterministicId:
    """A bounded identifier rendered as ``<prefix>_<canonical SHA-256>``.

    ``from_hash_input`` hashes the complete supplied value with ``sha256_hex``. Public domain
    contracts must still document which of their fields form that hash input.
    """

    prefix: str
    digest: str

    def __post_init__(self) -> None:
        _validate_id_prefix(self.prefix)
        if type(self.digest) is not str or _SHA256_PATTERN.fullmatch(self.digest) is None:
            raise DomainValidationError("deterministic ID digest must be lowercase SHA-256")

    @classmethod
    def from_hash_input(cls, *, prefix: str, hash_input: object) -> DeterministicId:
        """Create an ID from the platform's canonical serialization and SHA-256 boundary."""

        _validate_id_prefix(prefix)
        return cls(prefix=prefix, digest=sha256_hex(hash_input))

    @property
    def value(self) -> str:
        """Return the stable storage and wire representation."""

        return f"{self.prefix}_{self.digest}"

    def __str__(self) -> str:
        return self.value


def require_utc_instant(value: object, *, field_name: str) -> datetime:
    """Return a UTC-normalized datetime or reject a naive/non-UTC public instant."""

    label = _validate_field_name(field_name)
    if type(value) is not datetime:
        raise DomainValidationError(f"{label} must be a datetime")
    if value.tzinfo is UTC:
        return value
    try:
        offset = value.utcoffset()
    except Exception:
        raise DomainValidationError(f"{label} must be timezone-aware UTC") from None
    if offset != timedelta(0):
        raise DomainValidationError(f"{label} must be timezone-aware UTC")
    return value.replace(tzinfo=UTC)


def require_finite_decimal(value: object, *, field_name: str) -> Decimal:
    """Return a bounded exact ``Decimal`` without coercing floats or secret-like wrappers."""

    label = _validate_field_name(field_name)
    if type(value) is not Decimal:
        raise DomainValidationError(f"{label} must be an exact Decimal")
    if not value.is_finite():
        raise DomainValidationError(f"{label} must be finite")

    _, digits, exponent = value.as_tuple()
    if (
        len(digits) > MAX_DOMAIN_DECIMAL_DIGITS
        or not isinstance(exponent, int)
        or not MIN_DOMAIN_DECIMAL_EXPONENT <= exponent <= MAX_DOMAIN_DECIMAL_EXPONENT
    ):
        raise DomainValidationError(f"{label} exceeds the decimal representation limit")
    return value


def quantize_decimal(
    value: object,
    *,
    quantum: object,
    rounding: DecimalRounding,
    field_name: str,
) -> Decimal:
    """Round to a multiple of an explicit positive quantum with an explicit mode."""

    number = require_finite_decimal(value, field_name=field_name)
    unit = require_finite_decimal(quantum, field_name="quantum")
    if unit <= 0:
        raise DomainValidationError("quantum must be positive")
    if type(rounding) is not DecimalRounding:
        raise DomainValidationError("rounding must be a DecimalRounding member")

    try:
        arithmetic_context = Context(prec=(MAX_DOMAIN_DECIMAL_DIGITS * 4) + 4)
        with localcontext(arithmetic_context):
            # The accepted representation is bounded, so a fixed local precision makes the
            # outcome independent of process-global Decimal context without unbounded work.
            unit_count = (number / unit).quantize(Decimal(1), rounding=rounding.value)
            result = unit_count * unit
    except DecimalException:
        raise DomainValidationError(f"{field_name} could not be quantized") from None
    if result.is_zero():
        result = result.copy_abs()
    return require_finite_decimal(result, field_name=field_name)


def _validate_id_prefix(prefix: object) -> str:
    if type(prefix) is not str or _ID_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise DomainValidationError("deterministic ID prefix is invalid")
    return prefix


def _validate_field_name(field_name: object) -> str:
    if type(field_name) is not str or _FIELD_NAME_PATTERN.fullmatch(field_name) is None:
        raise DomainValidationError("domain field name is invalid")
    return field_name


def _require_audit_identifier(
    value: object,
    *,
    field_name: str,
    maximum_length: int,
) -> str:
    if (
        type(value) is not str
        or len(value) > maximum_length
        or _AUDIT_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise AuditValidationError(f"audit {field_name} is invalid")
    return value


def _audit_writer_for_stream(stream_id: object) -> AuditWriter:
    validated = _require_audit_identifier(
        stream_id,
        field_name="stream ID",
        maximum_length=MAX_AUDIT_STREAM_ID_LENGTH,
    )
    actor_text, separator, resource_ref = validated.partition(":")
    if not separator or not resource_ref:
        raise AuditValidationError("audit stream ID is invalid")
    try:
        writer = AuditWriter(actor_text)
    except ValueError:
        raise AuditValidationError("audit stream ID has an invalid writer") from None
    if _AUDIT_RESOURCE_REF_PATTERN.fullmatch(resource_ref) is None:
        raise AuditValidationError("audit stream ID has an invalid resource reference")
    return writer


def _validate_audit_authority(
    *,
    stream_id: object,
    event_type: object,
    actor: object,
) -> AuditWriter:
    stream_writer = _audit_writer_for_stream(stream_id)
    validated_event_type = _require_audit_identifier(
        event_type,
        field_name="event type",
        maximum_length=MAX_AUDIT_EVENT_TYPE_LENGTH,
    )
    if _AUDIT_EVENT_TYPE_PATTERN.fullmatch(validated_event_type) is None:
        raise AuditValidationError("audit event type is invalid")
    if type(actor) is not AuditWriter:
        raise AuditValidationError("audit actor must use the closed writer contract")
    if actor is not stream_writer:
        raise AuditValidationError("audit actor does not own the stream")
    if not any(validated_event_type.startswith(prefix) for prefix in _AUDIT_EVENT_FAMILIES[actor]):
        raise AuditValidationError("audit event type is not permitted for the writer")
    return actor


def _require_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise AuditValidationError(f"audit {field_name} is invalid")
    return value


def _prepare_audit_payload(value: object) -> tuple[str, str]:
    if type(value) is not dict:
        raise AuditValidationError("audit payload must be an object")
    try:
        encoded = canonical_json_bytes(value)
    except CanonicalizationError:
        raise AuditValidationError("audit payload is invalid") from None
    if len(encoded) > MAX_AUDIT_PAYLOAD_BYTES:
        raise AuditValidationError("audit payload exceeds the size limit")
    try:
        normalized = json.loads(encoded)
    except (UnicodeError, ValueError):
        raise AuditValidationError("audit payload is invalid") from None
    if type(normalized) is not dict:
        raise AuditValidationError("audit payload must be an object")
    _validate_audit_object(cast(dict[object, object], normalized), require_idempotency_key=True)
    normalized_payload = cast(dict[str, JsonValue], normalized)
    return encoded.decode("utf-8"), sha256_hex(normalized_payload)


def _validate_stored_audit_payload(payload_json: object) -> str:
    if type(payload_json) is not str:
        raise AuditValidationError("audit payload encoding is invalid")
    try:
        encoded = payload_json.encode("utf-8")
    except UnicodeEncodeError:
        raise AuditValidationError("audit payload encoding is invalid") from None
    if len(encoded) > MAX_AUDIT_PAYLOAD_BYTES:
        raise AuditValidationError("audit payload exceeds the size limit")
    try:
        normalized = json.loads(payload_json)
    except (RecursionError, ValueError):
        raise AuditValidationError("audit payload encoding is invalid") from None
    if type(normalized) is not dict:
        raise AuditValidationError("audit payload must be an object")
    _validate_audit_object(cast(dict[object, object], normalized), require_idempotency_key=True)
    try:
        canonical = canonical_json_bytes(normalized)
    except CanonicalizationError:
        raise AuditValidationError("audit payload encoding is invalid") from None
    if canonical != encoded:
        raise AuditValidationError("audit payload encoding is not canonical")
    return sha256_hex(cast(dict[str, JsonValue], normalized))


def _validate_audit_object(
    value: dict[object, object],
    *,
    require_idempotency_key: bool = False,
) -> None:
    if len(value) > MAX_AUDIT_CONTAINER_ITEMS:
        raise AuditValidationError("audit payload container exceeds the item limit")
    if require_idempotency_key and "idempotency_key" not in value:
        raise AuditValidationError("audit payload requires an idempotency key")
    for raw_key, item in value.items():
        normalized_key = _normalize_audit_payload_key(raw_key)
        _reject_sensitive_audit_key(normalized_key)
        if raw_key != normalized_key:
            raise AuditValidationError("audit payload key is not canonical")
        _validate_audit_payload_value(item, semantic_key=normalized_key)


def _validate_audit_payload_value(value: object, *, semantic_key: str) -> None:
    if type(value) is str:
        _reject_secret_like_audit_text(value)

    if value is None:
        raise AuditValidationError("audit payload null values are prohibited")

    if semantic_key in _AUDIT_COUNT_OBJECT_KEYS:
        _validate_audit_counts(value)
        return

    if semantic_key in _AUDIT_SYMBOL_LIST_KEYS:
        _validate_audit_list(value, item_pattern=_AUDIT_SYMBOL_PATTERN)
        return

    if semantic_key in _AUDIT_SLUG_LIST_KEYS:
        _validate_audit_list(value, item_pattern=_AUDIT_SLUG_PATTERN, reject_bare_hash=True)
        return

    if semantic_key in _AUDIT_ID_LIST_KEYS:
        _validate_audit_id_list(value)
        return

    if semantic_key in _AUDIT_HASH_LIST_KEYS:
        _validate_audit_list(value, item_pattern=_SHA256_PATTERN)
        return

    if semantic_key in _AUDIT_SYMBOL_KEYS:
        _require_audit_text(value, pattern=_AUDIT_SYMBOL_PATTERN)
        return

    if semantic_key in _AUDIT_HASH_KEYS:
        _require_audit_text(value, pattern=_SHA256_PATTERN)
        return

    if semantic_key in _AUDIT_ID_KEYS:
        _require_audit_id(value)
        return

    if semantic_key in _AUDIT_TIMESTAMP_KEYS:
        _require_audit_timestamp(value)
        return

    if semantic_key in _AUDIT_INTEGER_KEYS:
        if type(value) is not int or value < 0 or value > MAX_SIGNED_64_BIT_INTEGER:
            raise AuditValidationError("audit payload integer is invalid")
        return

    if semantic_key in _AUDIT_BOOLEAN_KEYS:
        if type(value) is not bool:
            raise AuditValidationError("audit payload boolean is invalid")
        return

    if semantic_key in _AUDIT_SLUG_KEYS:
        _require_audit_text(value, pattern=_AUDIT_SLUG_PATTERN, reject_bare_hash=True)
        return

    if semantic_key in _AUDIT_DECIMAL_KEYS:
        _require_audit_text(value, pattern=_AUDIT_DECIMAL_PATTERN)
        return

    raise AuditValidationError("audit payload field is not in the closed contract")


def _normalize_audit_payload_key(value: object) -> str:
    if type(value) is not str or _AUDIT_PAYLOAD_KEY_PATTERN.fullmatch(value) is None:
        raise AuditValidationError("audit payload key is invalid")
    with_acronyms = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", value)
    with_words = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", with_acronyms)
    return _AUDIT_KEY_SEPARATOR.sub("_", with_words).lower()


def _reject_sensitive_audit_key(normalized_key: str) -> None:
    compact = normalized_key.replace("_", "")
    if (
        any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS)
        or any(fragment in compact for fragment in _SENSITIVE_COMPACT_KEY_FRAGMENTS)
    ) and normalized_key != _SAFE_FENCING_KEY:
        raise AuditValidationError("audit payload contains a secret-like key")


def _reject_secret_like_audit_text(value: str) -> None:
    if (
        _URL_PATTERN.search(value) is not None
        or _CREDENTIAL_URL_PATTERN.search(value) is not None
        or _SENSITIVE_ASSIGNMENT_PATTERN.search(value) is not None
        or _PRIVATE_KEY_PATTERN.search(value) is not None
        or _BEARER_PATTERN.search(value) is not None
        or _JWT_PATTERN.fullmatch(value) is not None
    ):
        raise AuditValidationError("audit payload contains secret-like data")


def _require_audit_text(
    value: object,
    *,
    pattern: re.Pattern[str],
    reject_bare_hash: bool = False,
) -> str:
    if (
        type(value) is not str
        or pattern.fullmatch(value) is None
        or (reject_bare_hash and _SHA256_PATTERN.fullmatch(value) is not None)
    ):
        raise AuditValidationError("audit payload field is invalid")
    return value


def _require_audit_id(value: object) -> str:
    if type(value) is not str:
        raise AuditValidationError("audit payload ID is invalid")
    if _AUDIT_CONTENT_ID_PATTERN.fullmatch(value) is not None:
        return value
    if _AUDIT_UUID_PATTERN.fullmatch(value) is not None:
        try:
            parsed = UUID(value)
        except ValueError:
            pass
        else:
            if str(parsed) == value:
                return value
    raise AuditValidationError("audit payload ID is invalid")


def _require_audit_timestamp(value: object) -> str:
    rendered = _require_audit_text(value, pattern=_AUDIT_TIMESTAMP_PATTERN)
    try:
        parsed = datetime.fromisoformat(rendered.removesuffix("Z") + "+00:00")
    except ValueError:
        raise AuditValidationError("audit payload timestamp is invalid") from None
    if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != rendered:
        raise AuditValidationError("audit payload timestamp is invalid")
    return rendered


def _validate_audit_list(
    value: object,
    *,
    item_pattern: re.Pattern[str],
    reject_bare_hash: bool = False,
) -> None:
    if type(value) is not list:
        raise AuditValidationError("audit payload list has an invalid value type")
    items = cast(list[object], value)
    if len(items) > MAX_AUDIT_CONTAINER_ITEMS:
        raise AuditValidationError("audit payload container exceeds the item limit")
    for item in items:
        if type(item) is str:
            _reject_secret_like_audit_text(item)
        _require_audit_text(item, pattern=item_pattern, reject_bare_hash=reject_bare_hash)


def _validate_audit_id_list(value: object) -> None:
    if type(value) is not list:
        raise AuditValidationError("audit payload list has an invalid value type")
    items = cast(list[object], value)
    if len(items) > MAX_AUDIT_CONTAINER_ITEMS:
        raise AuditValidationError("audit payload container exceeds the item limit")
    for item in items:
        if type(item) is str:
            _reject_secret_like_audit_text(item)
        _require_audit_id(item)


def _validate_audit_counts(value: object) -> None:
    if type(value) is not dict:
        raise AuditValidationError("audit counts must be an object")
    counts = cast(dict[object, object], value)
    if len(counts) > MAX_AUDIT_CONTAINER_ITEMS:
        raise AuditValidationError("audit payload container exceeds the item limit")
    for raw_key, count in counts.items():
        normalized_key = _normalize_audit_payload_key(raw_key)
        _reject_sensitive_audit_key(normalized_key)
        if (
            raw_key != normalized_key
            or _AUDIT_SLUG_PATTERN.fullmatch(normalized_key) is None
            or type(count) is not int
            or count < 0
            or count > MAX_SIGNED_64_BIT_INTEGER
        ):
            raise AuditValidationError("audit count is invalid")
