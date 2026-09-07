"""Strict external request and safe response models for the control plane."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import JsonValue as PydanticJsonValue

from adaptive_trader.platform.jobs.models import JobRecord

MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 50
OPERATOR_RESUME_ACKNOWLEDGEMENT = "I_HAVE_REVIEWED_AQA_PAPER_STATE"

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._:-]*$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$", re.ASCII)


class StrictApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class MutationMetadata(StrictApiModel):
    idempotency_key: Identifier
    correlation_id: str

    @field_validator("correlation_id", mode="before")
    @classmethod
    def validate_correlation_id(cls, value: object) -> str:
        if type(value) is not str or len(value) != 36:
            raise ValueError("correlation_id must be a canonical UUID")
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError("correlation_id must be a canonical UUID") from None
        if str(parsed) != value:
            raise ValueError("correlation_id must be a canonical UUID")
        return value


class DataQualityAuditRequest(MutationMetadata):
    experiment_hash: Sha256


class GapRepairRequest(MutationMetadata):
    gap_id: Identifier


class DatasetFreezeRequest(MutationMetadata):
    experiment_hash: Sha256


class OfflineDemoRequest(MutationMetadata):
    demo_id: Identifier = "default"


class OperatorHaltRequest(MutationMetadata):
    reason_code: Literal["operator_requested"] = "operator_requested"


class OperatorResumeRequest(MutationMetadata):
    acknowledgement: Literal["I_HAVE_REVIEWED_AQA_PAPER_STATE"]
    latch_type: Literal[
        "deployment_drawdown",
        "operator_halt",
        "session_loss",
    ] = "operator_halt"


class JobResponse(StrictApiModel):
    job_id: str
    job_type: str
    state: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str | None
    lease_expires_at: str | None
    completed_at: str | None
    safe_last_error_code: str | None
    safe_last_error_message: str | None
    result_artifact_id: str | None
    created_at: str
    updated_at: str
    version: int

    @classmethod
    def from_record(cls, job: JobRecord) -> Self:
        if type(job) is not JobRecord:
            raise TypeError("job response requires a validated record")
        return cls(
            job_id=job.job_id,
            job_type=job.job_type.value,
            state=job.state.value,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            next_attempt_at=_instant(job.next_attempt_at),
            lease_expires_at=_instant(job.lease_expires_at),
            completed_at=_instant(job.completed_at),
            safe_last_error_code=job.safe_last_error_code,
            safe_last_error_message=job.safe_last_error_message,
            result_artifact_id=job.result_artifact_id,
            created_at=_instant(job.created_at) or "",
            updated_at=_instant(job.updated_at) or "",
            version=job.version,
        )


class SafePageResponse(StrictApiModel):
    items: tuple[dict[str, PydanticJsonValue], ...]
    limit: int = Field(ge=1, le=MAX_PAGE_LIMIT)
    offset: int = Field(ge=0)
    count: int = Field(ge=0)


class ControlMutationResponse(StrictApiModel):
    event_id: str
    state: Literal["engaged", "cleared"]
    reason_code: str
    latch_type: Literal[
        "deployment_drawdown",
        "operator_halt",
        "session_loss",
    ]


class ErrorDetail(StrictApiModel):
    code: str
    message: str
    correlation_id: str


class ErrorResponse(StrictApiModel):
    error: ErrorDetail


def validate_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _instant(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
