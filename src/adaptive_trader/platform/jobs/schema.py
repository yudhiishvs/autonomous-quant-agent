"""SQLAlchemy contract for durable jobs, attempt events, and outbox delivery."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA, UTCDateTime

_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

job_metadata = MetaData(schema=PLATFORM_SCHEMA, naming_convention=_NAMING_CONVENTION)
_json_value = JSON().with_variant(JSONB(), "postgresql")


def _sha256(column: str) -> CheckConstraint:
    return CheckConstraint(f"length({column}) = 64", name=f"{column}_sha256_length")


aqa_jobs_contract = Table(
    "aqa_jobs",
    job_metadata,
    Column("job_id", String(128), primary_key=True),
    Column("job_type", String(32), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("payload", _json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("correlation_id", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("next_attempt_at", UTCDateTime()),
    Column("lease_owner", String(128)),
    Column("lease_expires_at", UTCDateTime()),
    Column("claimed_at", UTCDateTime()),
    Column("started_at", UTCDateTime()),
    Column("completed_at", UTCDateTime()),
    Column("safe_last_error_code", String(64)),
    Column("safe_last_error_message", String(256)),
    Column("result_artifact_id", String(128)),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    UniqueConstraint("job_type", "idempotency_key", name="job_type_idempotency"),
    CheckConstraint(
        "job_type IN ('DATA_QUALITY_AUDIT', 'GAP_REPAIR', 'DATASET_FREEZE', 'OFFLINE_DEMO')",
        name="job_type_allowlist",
    ),
    CheckConstraint(
        "state IN ('PENDING', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEAD', 'CANCELED')",
        name="job_state",
    ),
    CheckConstraint("schema_version = 1", name="job_schema_version"),
    CheckConstraint("attempt_count BETWEEN 0 AND 3", name="job_attempt_count"),
    CheckConstraint("max_attempts = 3", name="job_max_attempts"),
    CheckConstraint("version >= 1", name="job_version_positive"),
    CheckConstraint("updated_at >= created_at", name="job_timestamps_monotonic"),
    CheckConstraint(
        "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
        name="job_retry_timestamp_monotonic",
    ),
    CheckConstraint(
        "lease_expires_at IS NULL OR lease_expires_at > updated_at",
        name="job_lease_timestamp_monotonic",
    ),
    CheckConstraint(
        "claimed_at IS NULL OR (claimed_at >= created_at AND claimed_at <= updated_at)",
        name="job_claim_timestamp_monotonic",
    ),
    CheckConstraint(
        "started_at IS NULL OR (started_at >= claimed_at AND started_at <= updated_at)",
        name="job_start_timestamp_monotonic",
    ),
    CheckConstraint(
        "completed_at IS NULL OR completed_at = updated_at",
        name="job_completion_timestamp_monotonic",
    ),
    CheckConstraint(
        "((state IN ('CLAIMED', 'RUNNING') AND lease_owner IS NOT NULL "
        "AND lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL) OR "
        "(state NOT IN ('CLAIMED', 'RUNNING') AND lease_owner IS NULL "
        "AND lease_expires_at IS NULL AND claimed_at IS NULL))",
        name="job_lease_consistent",
    ),
    CheckConstraint(
        "((state = 'RUNNING' AND started_at IS NOT NULL) OR "
        "(state <> 'RUNNING' AND started_at IS NULL))",
        name="job_started_consistent",
    ),
    CheckConstraint(
        "((state IN ('SUCCEEDED', 'DEAD', 'CANCELED') AND completed_at IS NOT NULL) OR "
        "(state NOT IN ('SUCCEEDED', 'DEAD', 'CANCELED') AND completed_at IS NULL))",
        name="job_completed_consistent",
    ),
    CheckConstraint(
        "((state IN ('PENDING', 'FAILED') AND next_attempt_at IS NOT NULL) OR "
        "(state NOT IN ('PENDING', 'FAILED') AND next_attempt_at IS NULL))",
        name="job_retry_time_consistent",
    ),
    CheckConstraint(
        "((safe_last_error_code IS NULL) = (safe_last_error_message IS NULL))",
        name="job_error_pair",
    ),
    CheckConstraint(
        "result_artifact_id IS NULL OR "
        "(result_artifact_id NOT LIKE '%/%' AND result_artifact_id NOT LIKE '%..%')",
        name="job_result_artifact_shape",
    ),
    _sha256("payload_hash"),
    _sha256("content_hash"),
    info={"state": True, "monotonic_column": "version"},
)
Index(
    "ix_aqa_jobs_claim",
    aqa_jobs_contract.c.state,
    aqa_jobs_contract.c.next_attempt_at,
    aqa_jobs_contract.c.created_at,
    aqa_jobs_contract.c.job_id,
)

aqa_job_attempts_contract = Table(
    "aqa_job_attempts",
    job_metadata,
    Column("job_attempt_event_id", String(128), primary_key=True),
    Column(
        "job_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_jobs.job_id"),
        nullable=False,
    ),
    Column("attempt_number", Integer, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("transition", String(16), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    Column("lease_expires_at", UTCDateTime()),
    Column("safe_error_code", String(64)),
    Column("safe_error_message", String(256)),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("job_id", "attempt_number", "sequence", name="job_attempt_sequence"),
    CheckConstraint("attempt_number BETWEEN 1 AND 3", name="job_attempt_number"),
    CheckConstraint("sequence BETWEEN 1 AND 4", name="job_attempt_event_sequence"),
    CheckConstraint(
        "transition IN ('CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ABANDONED')",
        name="job_attempt_transition",
    ),
    CheckConstraint(
        "((transition = 'CLAIMED' AND lease_expires_at IS NOT NULL) OR "
        "(transition <> 'CLAIMED' AND lease_expires_at IS NULL))",
        name="job_attempt_lease_consistent",
    ),
    CheckConstraint(
        "((safe_error_code IS NULL) = (safe_error_message IS NULL))",
        name="job_attempt_error_pair",
    ),
    _sha256("content_hash"),
    info={"append_only": True},
)
Index(
    "ix_aqa_job_attempts_job_attempt",
    aqa_job_attempts_contract.c.job_id,
    aqa_job_attempts_contract.c.attempt_number,
    aqa_job_attempts_contract.c.sequence,
)

aqa_outbox_events_contract = Table(
    "aqa_outbox_events",
    job_metadata,
    Column("outbox_event_id", String(128), primary_key=True),
    Column("aggregate_type", String(32), nullable=False),
    Column(
        "aggregate_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_jobs.job_id"),
        nullable=False,
    ),
    Column("event_type", String(64), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("payload", _json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("next_attempt_at", UTCDateTime()),
    Column("lease_owner", String(128)),
    Column("lease_expires_at", UTCDateTime()),
    Column("published_at", UTCDateTime()),
    Column("safe_last_error_code", String(64)),
    Column("safe_last_error_message", String(256)),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    CheckConstraint("aggregate_type = 'job'", name="outbox_aggregate_type"),
    CheckConstraint("schema_version = 1", name="outbox_schema_version"),
    CheckConstraint(
        "state IN ('PENDING', 'CLAIMED', 'PUBLISHED', 'FAILED', 'DEAD')",
        name="outbox_state",
    ),
    CheckConstraint("attempt_count BETWEEN 0 AND 3", name="outbox_attempt_count"),
    CheckConstraint("version >= 1", name="outbox_version_positive"),
    CheckConstraint("updated_at >= created_at", name="outbox_timestamps_monotonic"),
    CheckConstraint(
        "next_attempt_at IS NULL OR next_attempt_at >= updated_at",
        name="outbox_retry_timestamp_monotonic",
    ),
    CheckConstraint(
        "lease_expires_at IS NULL OR lease_expires_at > updated_at",
        name="outbox_lease_timestamp_monotonic",
    ),
    CheckConstraint(
        "published_at IS NULL OR published_at = updated_at",
        name="outbox_publish_timestamp_monotonic",
    ),
    CheckConstraint(
        "((state = 'CLAIMED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'CLAIMED' AND lease_owner IS NULL AND lease_expires_at IS NULL))",
        name="outbox_lease_consistent",
    ),
    CheckConstraint(
        "((state = 'PUBLISHED' AND published_at IS NOT NULL) OR "
        "(state <> 'PUBLISHED' AND published_at IS NULL))",
        name="outbox_publication_consistent",
    ),
    CheckConstraint(
        "((state IN ('PENDING', 'FAILED') AND next_attempt_at IS NOT NULL) OR "
        "(state NOT IN ('PENDING', 'FAILED') AND next_attempt_at IS NULL))",
        name="outbox_retry_time_consistent",
    ),
    CheckConstraint(
        "((safe_last_error_code IS NULL) = (safe_last_error_message IS NULL))",
        name="outbox_error_pair",
    ),
    _sha256("payload_hash"),
    _sha256("content_hash"),
    info={"state": True, "monotonic_column": "version"},
)
Index(
    "ix_aqa_outbox_delivery",
    aqa_outbox_events_contract.c.state,
    aqa_outbox_events_contract.c.next_attempt_at,
    aqa_outbox_events_contract.c.created_at,
    aqa_outbox_events_contract.c.outbox_event_id,
)
