"""Closed contracts for durable control-plane jobs and outbox delivery."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import cast

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

JOB_SCHEMA_VERSION = 1
JOB_MAX_ATTEMPTS = 3
JOB_LEASE_DURATION = timedelta(seconds=60)
JOB_RETRY_DELAYS = (timedelta(seconds=5), timedelta(seconds=10), timedelta(seconds=20))
MAX_JOB_PAYLOAD_BYTES = 4_096

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$", re.ASCII)
_CORRELATION_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"[a-z][a-z0-9_]{0,31}_[0-9a-f]{64})$",
    re.ASCII,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ARTIFACT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$", re.ASCII)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_SAFE_ERROR_MESSAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,;:_()/-]{0,255}$", re.ASCII)
_SENSITIVE_ERROR_FRAGMENTS = (
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
    "://",
)


class JobValidationError(ValueError):
    """A job contract failed closed before persistence or execution."""


class JobType(StrEnum):
    """The complete public job allowlist."""

    DATA_QUALITY_AUDIT = "DATA_QUALITY_AUDIT"
    GAP_REPAIR = "GAP_REPAIR"
    DATASET_FREEZE = "DATASET_FREEZE"
    OFFLINE_DEMO = "OFFLINE_DEMO"


class JobState(StrEnum):
    """Durable job lifecycle states."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CANCELED = "CANCELED"


def require_job_types(value: frozenset[JobType]) -> frozenset[JobType]:
    """Validate the worker's immutable, nonempty job capability selection."""

    if (
        type(value) is not frozenset
        or not value
        or any(type(item) is not JobType for item in value)
    ):
        raise JobValidationError("job type selection must use a nonempty frozen set of JobType")
    return value


class JobAttemptTransition(StrEnum):
    """Immutable attempt transitions written by a worker."""

    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class OutboxState(StrEnum):
    """Delivery lifecycle for one immutable outbox message payload."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    DEAD = "DEAD"


@dataclass(frozen=True, slots=True)
class JobPayload:
    """Bounded schema-versioned routing data, never an authorization document."""

    job_type: JobType
    payload_json: str
    payload_hash: str

    def __post_init__(self) -> None:
        if type(self.job_type) is not JobType:
            raise JobValidationError("job payload type is invalid")
        if type(self.payload_json) is not str:
            raise JobValidationError("job payload encoding is invalid")
        try:
            encoded = self.payload_json.encode("utf-8")
            decoded = json.loads(encoded)
            canonical = canonical_json_bytes(decoded)
        except (TypeError, UnicodeError, ValueError):
            raise JobValidationError("job payload encoding is invalid") from None
        if (
            type(decoded) is not dict
            or canonical != encoded
            or len(encoded) > MAX_JOB_PAYLOAD_BYTES
            or type(self.payload_hash) is not str
            or _SHA256.fullmatch(self.payload_hash) is None
            or sha256_hex(decoded) != self.payload_hash
        ):
            raise JobValidationError("job payload content is invalid")
        _validate_payload_shape(self.job_type, cast(dict[str, JsonValue], decoded))

    @property
    def value(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], json.loads(self.payload_json))

    @classmethod
    def data_quality_audit(cls, *, experiment_hash: str) -> JobPayload:
        return cls._create(
            JobType.DATA_QUALITY_AUDIT,
            {"contract_version": JOB_SCHEMA_VERSION, "experiment_hash": experiment_hash},
        )

    @classmethod
    def gap_repair(cls, *, gap_id: str) -> JobPayload:
        return cls._create(
            JobType.GAP_REPAIR,
            {"contract_version": JOB_SCHEMA_VERSION, "gap_id": gap_id},
        )

    @classmethod
    def dataset_freeze(cls, *, experiment_hash: str) -> JobPayload:
        return cls._create(
            JobType.DATASET_FREEZE,
            {"contract_version": JOB_SCHEMA_VERSION, "experiment_hash": experiment_hash},
        )

    @classmethod
    def offline_demo(cls, *, demo_id: str = "default") -> JobPayload:
        return cls._create(
            JobType.OFFLINE_DEMO,
            {"contract_version": JOB_SCHEMA_VERSION, "demo_id": demo_id},
        )

    @classmethod
    def _create(cls, job_type: JobType, value: dict[str, object]) -> JobPayload:
        encoded = canonical_json_bytes(value)
        return cls(
            job_type=job_type,
            payload_json=encoded.decode("utf-8"),
            payload_hash=sha256_hex(value),
        )


@dataclass(frozen=True, slots=True)
class JobCreateRequest:
    """One idempotent request to create an allowlisted job."""

    payload: JobPayload
    idempotency_key: str
    correlation_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if type(self.payload) is not JobPayload:
            raise JobValidationError("job request payload is invalid")
        _require_identifier(self.idempotency_key, field_name="job idempotency key")
        _require_correlation_id(self.correlation_id)
        object.__setattr__(
            self,
            "requested_at",
            _require_utc(self.requested_at, field_name="job request timestamp"),
        )


@dataclass(frozen=True, slots=True)
class SafeJobError:
    """Pre-sanitized worker failure metadata safe for storage and operator display."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _SAFE_ERROR_CODE.fullmatch(self.code) is None:
            raise JobValidationError("safe job error code is invalid")
        if (
            type(self.message) is not str
            or _SAFE_ERROR_MESSAGE.fullmatch(self.message) is None
            or any(fragment in self.message.lower() for fragment in _SENSITIVE_ERROR_FRAGMENTS)
        ):
            raise JobValidationError("safe job error message is invalid")


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Validated durable state returned outside the persistence boundary."""

    job_id: str
    job_type: JobType
    schema_version: int
    payload: JobPayload
    idempotency_key: str
    correlation_id: str
    state: JobState
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    claimed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    safe_last_error_code: str | None
    safe_last_error_message: str | None
    result_artifact_id: str | None
    created_at: datetime
    updated_at: datetime
    content_hash: str
    version: int

    def __post_init__(self) -> None:
        _require_identifier(self.job_id, field_name="job ID")
        if type(self.job_type) is not JobType or self.payload.job_type is not self.job_type:
            raise JobValidationError("persisted job type is invalid")
        if self.schema_version != JOB_SCHEMA_VERSION:
            raise JobValidationError("persisted job schema version is unsupported")
        _require_identifier(self.idempotency_key, field_name="job idempotency key")
        _require_correlation_id(self.correlation_id)
        if type(self.state) is not JobState:
            raise JobValidationError("persisted job state is invalid")
        if (
            type(self.attempt_count) is not int
            or not 0 <= self.attempt_count <= JOB_MAX_ATTEMPTS
            or self.max_attempts != JOB_MAX_ATTEMPTS
            or type(self.version) is not int
            or self.version < 1
        ):
            raise JobValidationError("persisted job counters are invalid")
        for field_name in ("created_at", "updated_at"):
            object.__setattr__(
                self,
                field_name,
                _require_utc(getattr(self, field_name), field_name=f"job {field_name}"),
            )
        for field_name in (
            "next_attempt_at",
            "lease_expires_at",
            "claimed_at",
            "started_at",
            "completed_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _require_utc(value, field_name=f"job {field_name}"),
                )
        if self.updated_at < self.created_at:
            raise JobValidationError("persisted job timestamps are not monotonic")
        _validate_job_state(self)
        if (self.safe_last_error_code is None) != (self.safe_last_error_message is None):
            raise JobValidationError("persisted job error fields are inconsistent")
        if self.safe_last_error_code is not None:
            SafeJobError(self.safe_last_error_code, cast(str, self.safe_last_error_message))
        if self.result_artifact_id is not None and (
            type(self.result_artifact_id) is not str
            or _ARTIFACT_ID.fullmatch(self.result_artifact_id) is None
        ):
            raise JobValidationError("persisted result artifact ID is invalid")
        if type(self.content_hash) is not str or _SHA256.fullmatch(self.content_hash) is None:
            raise JobValidationError("persisted job content hash is invalid")

    @property
    def is_terminal(self) -> bool:
        return self.state in {JobState.SUCCEEDED, JobState.DEAD, JobState.CANCELED}


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    outbox_event_id: str
    event_type: str
    aggregate_id: str
    payload: dict[str, JsonValue]
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.outbox_event_id, field_name="outbox event ID")
        _require_identifier(self.event_type, field_name="outbox event type")
        _require_identifier(self.aggregate_id, field_name="outbox aggregate ID")
        if type(self.payload) is not dict:
            raise JobValidationError("outbox event payload is invalid")
        try:
            canonical_json_bytes(self.payload)
        except (TypeError, UnicodeError, ValueError):
            raise JobValidationError("outbox event payload is invalid") from None
        if type(self.attempt_count) is not int or not 1 <= self.attempt_count <= JOB_MAX_ATTEMPTS:
            raise JobValidationError("outbox event attempt is invalid")
        _require_identifier(self.lease_owner, field_name="outbox lease owner")
        object.__setattr__(
            self,
            "lease_expires_at",
            _require_utc(self.lease_expires_at, field_name="outbox lease expiry"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _require_utc(self.updated_at, field_name="outbox update timestamp"),
        )
        if self.updated_at >= self.lease_expires_at:
            raise JobValidationError("outbox event lease timestamp is invalid")


def retry_delay(attempt_count: int) -> timedelta:
    """Return the declared delay for a one-based failed attempt."""

    if type(attempt_count) is not int or not 1 <= attempt_count <= JOB_MAX_ATTEMPTS:
        raise JobValidationError("job retry attempt is invalid")
    return JOB_RETRY_DELAYS[attempt_count - 1]


def require_artifact_id(value: str) -> str:
    if type(value) is not str or _ARTIFACT_ID.fullmatch(value) is None:
        raise JobValidationError("result artifact ID is invalid")
    return value


def _require_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise JobValidationError(f"{field_name} is invalid")
    return value


def _require_correlation_id(value: object) -> str:
    if type(value) is not str or _CORRELATION_ID.fullmatch(value) is None:
        raise JobValidationError("job correlation ID is invalid")
    return value


def _require_utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name="timestamp")
    except DomainValidationError:
        raise JobValidationError(f"{field_name} is invalid") from None


def _validate_payload_shape(job_type: JobType, payload: dict[str, JsonValue]) -> None:
    expected_keys = {
        JobType.DATA_QUALITY_AUDIT: frozenset({"contract_version", "experiment_hash"}),
        JobType.GAP_REPAIR: frozenset({"contract_version", "gap_id"}),
        JobType.DATASET_FREEZE: frozenset({"contract_version", "experiment_hash"}),
        JobType.OFFLINE_DEMO: frozenset({"contract_version", "demo_id"}),
    }[job_type]
    if payload.keys() != expected_keys or payload.get("contract_version") != JOB_SCHEMA_VERSION:
        raise JobValidationError("job payload fields are invalid")
    key = next(key for key in expected_keys if key != "contract_version")
    value = payload[key]
    if key == "experiment_hash":
        valid = type(value) is str and _SHA256.fullmatch(value) is not None
    else:
        valid = type(value) is str and _IDENTIFIER.fullmatch(value) is not None
    if not valid:
        raise JobValidationError("job payload routing identifier is invalid")


def _validate_job_state(job: JobRecord) -> None:
    leased = job.state in {JobState.CLAIMED, JobState.RUNNING}
    if leased != all(
        value is not None for value in (job.lease_owner, job.lease_expires_at, job.claimed_at)
    ):
        raise JobValidationError("persisted job lease fields are inconsistent")
    if job.lease_owner is not None:
        _require_identifier(job.lease_owner, field_name="job lease owner")
    if leased and (
        job.attempt_count < 1
        or job.claimed_at is None
        or job.lease_expires_at is None
        or not job.created_at <= job.claimed_at <= job.updated_at < job.lease_expires_at
    ):
        raise JobValidationError("persisted job lease timestamps are invalid")
    if job.state is JobState.RUNNING and job.started_at is None:
        raise JobValidationError("running job has no start timestamp")
    if job.state is JobState.CLAIMED and job.started_at is not None:
        raise JobValidationError("claimed job has an unexpected start timestamp")
    if job.started_at is not None and (
        job.claimed_at is None or not job.claimed_at <= job.started_at <= job.updated_at
    ):
        raise JobValidationError("persisted job start timestamp is invalid")
    terminal = job.state in {JobState.SUCCEEDED, JobState.DEAD, JobState.CANCELED}
    if terminal != (job.completed_at is not None):
        raise JobValidationError("persisted job completion fields are inconsistent")
    if job.completed_at is not None and job.completed_at != job.updated_at:
        raise JobValidationError("persisted job completion timestamp is invalid")
    if job.state in {JobState.PENDING, JobState.FAILED} and job.next_attempt_at is None:
        raise JobValidationError("retryable job has no next-attempt timestamp")
    if job.state not in {JobState.PENDING, JobState.FAILED} and job.next_attempt_at is not None:
        raise JobValidationError("non-retryable job has a next-attempt timestamp")
    if job.next_attempt_at is not None and job.next_attempt_at < job.updated_at:
        raise JobValidationError("persisted job retry timestamp is invalid")
    failed = job.state in {JobState.FAILED, JobState.DEAD}
    if failed != (job.safe_last_error_code is not None):
        raise JobValidationError("persisted job error state is inconsistent")
    if (job.result_artifact_id is not None) and job.state is not JobState.SUCCEEDED:
        raise JobValidationError("persisted job result state is inconsistent")
    if job.state is JobState.PENDING and job.attempt_count != 0:
        raise JobValidationError("pending job attempt count is invalid")
    if job.state not in {JobState.PENDING, JobState.CANCELED} and job.attempt_count < 1:
        raise JobValidationError("persisted job attempt count is invalid")
    if job.state is JobState.DEAD and job.attempt_count != JOB_MAX_ATTEMPTS:
        raise JobValidationError("dead job attempt count is invalid")
