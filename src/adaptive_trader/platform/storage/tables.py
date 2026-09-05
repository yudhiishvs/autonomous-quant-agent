"""SQLAlchemy metadata for the additive generic-platform schema."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from adaptive_trader.platform.domain import require_finite_decimal, require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError

PLATFORM_SCHEMA = "aqa"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema=PLATFORM_SCHEMA, naming_convention=NAMING_CONVENTION)
json_value = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    """Persist only UTC instants and restore SQLite's discarded timezone marker."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        normalized = require_utc_instant(value, field_name="timestamp")
        if dialect.name == "sqlite":
            return normalized.replace(tzinfo=None)
        return normalized

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if dialect.name == "sqlite" and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        try:
            if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            return require_utc_instant(value.astimezone(UTC), field_name="timestamp")
        except Exception:
            raise DomainValidationError("database timestamp must be timezone-aware UTC") from None


class FiniteNumeric(TypeDecorator[Decimal]):
    """Bind and return only exact finite Decimal values on every supported dialect."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 38, scale: int = 18) -> None:
        if (
            type(precision) is not int
            or type(scale) is not int
            or precision < 1
            or scale < 0
            or scale > precision
        ):
            raise ValueError("finite numeric precision and scale are invalid")
        self.precision = precision
        self.scale = scale
        super().__init__(precision=precision, scale=scale)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(160))
        return dialect.type_descriptor(Numeric(self.precision, self.scale))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> object:
        if value is None:
            return None
        normalized = require_finite_decimal(value, field_name="database_numeric")
        if not self._fits_declared_shape(normalized):
            raise DomainValidationError("database numeric value exceeds its precision or scale")
        return format(normalized, "f") if dialect.name == "sqlite" else normalized

    def process_result_value(self, value: object, dialect: Dialect) -> Decimal | None:
        del dialect
        if value is None:
            return None
        try:
            parsed = Decimal(value) if type(value) is str else value
        except DecimalException:
            raise DomainValidationError("database numeric value must be finite") from None
        try:
            normalized = require_finite_decimal(parsed, field_name="database_numeric")
        except DomainValidationError:
            raise DomainValidationError("database numeric value must be finite") from None
        if not self._fits_declared_shape(normalized):
            raise DomainValidationError("database numeric value exceeds its precision or scale")
        return normalized

    def _fits_declared_shape(self, value: Decimal) -> bool:
        _, raw_digits, raw_exponent = value.as_tuple()
        if not isinstance(raw_exponent, int):
            return False
        digits = list(raw_digits)
        exponent = raw_exponent
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if not digits:
            return True
        fractional_places = max(-exponent, 0)
        integer_places = max(len(digits) + exponent, 0)
        return (
            fractional_places <= self.scale
            and integer_places <= self.precision - self.scale
            and len(digits) <= self.precision
        )


def _hash_constraint(column_name: str) -> CheckConstraint:
    return CheckConstraint(
        f"length({column_name}) = 64",
        name=f"{column_name}_sha256_length",
    )


def _finite_numeric_constraint(
    *column_names: str,
    nullable: frozenset[str] = frozenset(),
) -> CheckConstraint:
    terms = [
        (
            f"({column_name} IS NULL OR "
            f"CAST({column_name} AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity'))"
            if column_name in nullable
            else f"CAST({column_name} AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')"
        )
        for column_name in column_names
    ]
    return CheckConstraint(" AND ".join(terms), name="financial_values_finite")


aqa_experiments = Table(
    "aqa_experiments",
    metadata,
    Column("experiment_hash", String(64), primary_key=True),
    Column("experiment_id", String(64), nullable=False),
    Column("experiment_version", BigInteger, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("configuration", json_value, nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("registered_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "experiment_id",
        "experiment_version",
        name="experiment_id_version",
    ),
    CheckConstraint("experiment_version >= 1", name="experiment_version_positive"),
    CheckConstraint("schema_version >= 1", name="schema_version_positive"),
    _hash_constraint("experiment_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_experiment_symbols = Table(
    "aqa_experiment_symbols",
    metadata,
    Column("experiment_symbol_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("symbol", String(10), nullable=False),
    Column("role", String(16), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    UniqueConstraint("experiment_hash", "symbol", name="experiment_symbol"),
    UniqueConstraint("experiment_hash", "role", "ordinal", name="experiment_role_ordinal"),
    CheckConstraint(
        "role IN ('active', 'benchmark', 'context', 'excluded')",
        name="symbol_role",
    ),
    CheckConstraint("ordinal >= 0", name="symbol_ordinal_nonnegative"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_security_metadata_events = Table(
    "aqa_security_metadata_events",
    metadata,
    Column("security_event_id", String(128), primary_key=True),
    Column("symbol", String(10), nullable=False),
    Column("source", String(32), nullable=False),
    Column("source_event_id", String(128)),
    Column("effective_at", UTCDateTime(), nullable=False),
    Column("observed_at", UTCDateTime(), nullable=False),
    Column("tradable", Boolean, nullable=False),
    Column("fractionable", Boolean, nullable=False),
    Column("shortable", Boolean, nullable=False),
    Column("attributes", json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("source", "source_event_id", name="security_source_event"),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)
Index(
    "ix_aqa_security_metadata_symbol_effective",
    aqa_security_metadata_events.c.symbol,
    aqa_security_metadata_events.c.effective_at,
)

aqa_bar_identities = Table(
    "aqa_bar_identities",
    metadata,
    Column("bar_identity_id", String(128), primary_key=True),
    Column("provider", String(32), nullable=False),
    Column("feed", String(16), nullable=False),
    Column("adjustment", String(16), nullable=False),
    Column("symbol", String(10), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("start_at", UTCDateTime(), nullable=False),
    Column("end_at", UTCDateTime(), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "provider",
        "feed",
        "adjustment",
        "symbol",
        "timeframe",
        "start_at",
        name="bar_series_start",
    ),
    CheckConstraint("start_at < end_at", name="bar_interval_positive"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_bar_events = Table(
    "aqa_bar_events",
    metadata,
    Column("bar_event_id", String(128), primary_key=True),
    Column(
        "bar_identity_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_bar_identities.bar_identity_id"),
        nullable=False,
    ),
    Column("revision", BigInteger, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("provider_timestamp", UTCDateTime()),
    Column("received_at", UTCDateTime(), nullable=False),
    Column("open", FiniteNumeric(38, 18), nullable=False),
    Column("high", FiniteNumeric(38, 18), nullable=False),
    Column("low", FiniteNumeric(38, 18), nullable=False),
    Column("close", FiniteNumeric(38, 18), nullable=False),
    Column("volume", FiniteNumeric(38, 0), nullable=False),
    Column("trade_count", BigInteger),
    Column("vwap", FiniteNumeric(38, 18)),
    Column("quality_flags", json_value, nullable=False),
    Column("source", String(32), nullable=False),
    Column("source_mode", String(32), nullable=False),
    Column("source_event_id", String(128)),
    Column("is_correction", Boolean, nullable=False),
    Column("correction_of_source_event_id", String(128)),
    Column("source_payload_hash", String(64), nullable=False),
    Column("lineage_hash", String(64)),
    Column("normalized_payload_hash", String(64), nullable=False),
    Column("correction_of_event_id", String(128)),
    Column("content_hash", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    ForeignKeyConstraint(
        ["correction_of_event_id"],
        [f"{PLATFORM_SCHEMA}.aqa_bar_events.bar_event_id"],
        name="bar_event_correction",
    ),
    UniqueConstraint("bar_identity_id", "revision", name="bar_identity_revision"),
    UniqueConstraint(
        "bar_identity_id",
        "bar_event_id",
        "revision",
        name="bar_latest_reference",
    ),
    CheckConstraint("revision >= 1", name="bar_revision_positive"),
    CheckConstraint("schema_version >= 1", name="bar_schema_version_positive"),
    CheckConstraint(
        "CAST(open AS NUMERIC) > 0 AND CAST(high AS NUMERIC) > 0 "
        "AND CAST(low AS NUMERIC) > 0 AND CAST(close AS NUMERIC) > 0",
        name="bar_prices_positive",
    ),
    CheckConstraint(
        "CAST(high AS NUMERIC) >= CAST(open AS NUMERIC) "
        "AND CAST(high AS NUMERIC) >= CAST(close AS NUMERIC) "
        "AND CAST(high AS NUMERIC) >= CAST(low AS NUMERIC)",
        name="bar_high_coherent",
    ),
    CheckConstraint(
        "CAST(low AS NUMERIC) <= CAST(open AS NUMERIC) "
        "AND CAST(low AS NUMERIC) <= CAST(close AS NUMERIC) "
        "AND CAST(low AS NUMERIC) <= CAST(high AS NUMERIC)",
        name="bar_low_coherent",
    ),
    CheckConstraint("CAST(volume AS NUMERIC) >= 0", name="bar_volume_nonnegative"),
    CheckConstraint("trade_count IS NULL OR trade_count >= 0", name="bar_trades_nonnegative"),
    CheckConstraint(
        "vwap IS NULL OR CAST(vwap AS NUMERIC) >= 0",
        name="bar_vwap_nonnegative",
    ),
    CheckConstraint(
        "source_mode IN ('external_provider', 'offline_fixture')",
        name="bar_source_mode",
    ),
    CheckConstraint(
        "correction_of_source_event_id IS NULL OR is_correction",
        name="bar_source_correction_consistent",
    ),
    CheckConstraint(
        "correction_of_source_event_id IS NULL OR source_event_id IS NULL "
        "OR correction_of_source_event_id <> source_event_id",
        name="bar_source_correction_not_self",
    ),
    _finite_numeric_constraint(
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        nullable=frozenset({"vwap"}),
    ),
    _hash_constraint("source_payload_hash"),
    CheckConstraint(
        "lineage_hash IS NULL OR length(lineage_hash) = 64",
        name="lineage_hash_sha256_length",
    ),
    _hash_constraint("normalized_payload_hash"),
    _hash_constraint("content_hash"),
    CheckConstraint(
        "content_hash <> normalized_payload_hash",
        name="event_hash_domain_separation",
    ),
    info={"append_only": True},
)
aqa_bar_latest = Table(
    "aqa_bar_latest",
    metadata,
    Column(
        "bar_identity_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_bar_identities.bar_identity_id"),
        primary_key=True,
    ),
    Column("bar_event_id", String(128), nullable=False, unique=True),
    Column("revision", BigInteger, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("projected_at", UTCDateTime(), nullable=False),
    ForeignKeyConstraint(
        ["bar_identity_id", "bar_event_id", "revision"],
        [
            f"{PLATFORM_SCHEMA}.aqa_bar_events.bar_identity_id",
            f"{PLATFORM_SCHEMA}.aqa_bar_events.bar_event_id",
            f"{PLATFORM_SCHEMA}.aqa_bar_events.revision",
        ],
        name="bar_latest_event",
    ),
    CheckConstraint("revision >= 1", name="bar_latest_revision_positive"),
    CheckConstraint("version >= 1", name="bar_latest_version_positive"),
    _hash_constraint("content_hash"),
    info={"state": True},
)

aqa_data_gaps = Table(
    "aqa_data_gaps",
    metadata,
    Column("gap_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("provider", String(32), nullable=False),
    Column("feed", String(16), nullable=False),
    Column("adjustment", String(16), nullable=False),
    Column("symbol", String(10), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("gap_start_at", UTCDateTime(), nullable=False),
    Column("gap_end_at", UTCDateTime(), nullable=False),
    Column("status", String(16), nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("detected_at", UTCDateTime(), nullable=False),
    Column("last_attempt_at", UTCDateTime()),
    Column("resolved_at", UTCDateTime()),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    UniqueConstraint(
        "experiment_hash",
        "provider",
        "feed",
        "adjustment",
        "symbol",
        "timeframe",
        "gap_start_at",
        "gap_end_at",
        name="gap_series_interval",
    ),
    CheckConstraint("gap_start_at < gap_end_at", name="gap_interval_positive"),
    CheckConstraint("status IN ('open', 'repairing', 'resolved', 'waived')", name="gap_status"),
    CheckConstraint("attempt_count >= 0", name="gap_attempts_nonnegative"),
    CheckConstraint("version >= 1", name="gap_version_positive"),
    _hash_constraint("content_hash"),
    info={"state": True},
)
Index("ix_aqa_data_gaps_status_start", aqa_data_gaps.c.status, aqa_data_gaps.c.gap_start_at)

aqa_symbol_watermarks = Table(
    "aqa_symbol_watermarks",
    metadata,
    Column("symbol_watermark_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("provider", String(32), nullable=False),
    Column("feed", String(16), nullable=False),
    Column("adjustment", String(16), nullable=False),
    Column("symbol", String(10), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("contiguous_through", UTCDateTime(), nullable=False),
    Column("quality_hash", String(64), nullable=False),
    Column(
        "latest_bar_event_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_bar_events.bar_event_id"),
    ),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "experiment_hash",
        "provider",
        "feed",
        "adjustment",
        "symbol",
        "timeframe",
        name="symbol_watermark_series",
    ),
    CheckConstraint("version >= 1", name="symbol_watermark_version_positive"),
    _hash_constraint("quality_hash"),
    _hash_constraint("content_hash"),
    info={"state": True, "monotonic_column": "contiguous_through"},
)

aqa_basket_watermarks = Table(
    "aqa_basket_watermarks",
    metadata,
    Column("basket_watermark_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("role", String(16), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("contiguous_through", UTCDateTime()),
    Column("component_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    UniqueConstraint("experiment_hash", "role", "timeframe", name="basket_watermark_role"),
    CheckConstraint(
        "role IN ('active', 'benchmark', 'context')", name="basket_watermark_role_value"
    ),
    CheckConstraint("status IN ('ready', 'blocked')", name="basket_watermark_status"),
    CheckConstraint(
        "(status = 'ready' AND contiguous_through IS NOT NULL) "
        "OR (status = 'blocked' AND contiguous_through IS NULL)",
        name="basket_watermark_readiness_consistent",
    ),
    CheckConstraint("version >= 1", name="basket_watermark_version_positive"),
    _hash_constraint("component_hash"),
    _hash_constraint("content_hash"),
    info={"state": True, "monotonic_column": "contiguous_through"},
)

aqa_dataset_manifests = Table(
    "aqa_dataset_manifests",
    metadata,
    Column("dataset_id", String(128), primary_key=True),
    Column("artifact_id", String(128), nullable=False, unique=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("provider", String(32), nullable=False),
    Column("feed", String(16), nullable=False),
    Column("adjustment", String(16), nullable=False),
    Column("timeframe", String(16), nullable=False),
    Column("range_start_at", UTCDateTime(), nullable=False),
    Column("range_end_at", UTCDateTime(), nullable=False),
    Column("roles", json_value, nullable=False),
    Column("symbols", json_value, nullable=False),
    Column("row_counts", json_value, nullable=False),
    Column("gap_summary", json_value, nullable=False),
    Column("correction_summary", json_value, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("logical_hash", String(64), nullable=False, unique=True),
    Column("physical_hash", String(64), nullable=False),
    Column("manifest_hash", String(64), nullable=False, unique=True),
    Column("source_git_commit", String(40), nullable=False),
    Column("dirty_worktree", Boolean, nullable=False),
    Column("uv_lock_hash", String(64), nullable=False),
    Column("promotable", Boolean, nullable=False),
    Column("status", String(16), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    CheckConstraint("range_start_at < range_end_at", name="dataset_interval_positive"),
    CheckConstraint("schema_version >= 1", name="dataset_schema_version_positive"),
    CheckConstraint("status IN ('promotable', 'diagnostic')", name="dataset_status"),
    _hash_constraint("logical_hash"),
    _hash_constraint("physical_hash"),
    _hash_constraint("manifest_hash"),
    _hash_constraint("uv_lock_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_decision_slots = Table(
    "aqa_decision_slots",
    metadata,
    Column("slot_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("experiment_id", String(64), nullable=False),
    Column("experiment_version", BigInteger, nullable=False),
    Column("signal_provider_id", String(64), nullable=False),
    Column("signal_provider_version", String(64), nullable=False),
    Column("session_date", Date(), nullable=False),
    Column("source_interval_start", UTCDateTime(), nullable=False),
    Column("source_interval_end", UTCDateTime(), nullable=False),
    Column("decision_type", String(32), nullable=False),
    Column("ready_at", UTCDateTime(), nullable=False),
    Column("deadline_at", UTCDateTime(), nullable=False),
    Column("required_completion_at", UTCDateTime(), nullable=False),
    Column("state", String(16), nullable=False),
    Column("claim_owner", String(128)),
    Column("claimed_at", UTCDateTime()),
    Column("lease_expires_at", UTCDateTime()),
    Column("attempt_count", Integer, nullable=False),
    Column("completed_at", UTCDateTime()),
    Column("reason_code", String(64)),
    Column("correlation_id", String(128), nullable=False, unique=True),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    UniqueConstraint(
        "experiment_hash",
        "source_interval_end",
        "decision_type",
        name="decision_source_type",
    ),
    CheckConstraint(
        "state IN ('PENDING', 'WAITING_FOR_DATA', 'READY', 'CLAIMED', 'COMPLETED', "
        "'SKIPPED', 'EXPIRED', 'FAILED', 'FLATTEN_REQUIRED')",
        name="decision_state",
    ),
    CheckConstraint(
        "((state = 'CLAIMED' AND claim_owner IS NOT NULL AND claimed_at IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR (state <> 'CLAIMED' AND claim_owner IS NULL "
        "AND claimed_at IS NULL AND lease_expires_at IS NULL))",
        name="decision_claim_consistent",
    ),
    CheckConstraint(
        "((state IN ('COMPLETED', 'SKIPPED', 'EXPIRED', 'FAILED') "
        "AND completed_at IS NOT NULL) OR (state NOT IN "
        "('COMPLETED', 'SKIPPED', 'EXPIRED', 'FAILED') AND completed_at IS NULL))",
        name="decision_completion_consistent",
    ),
    CheckConstraint(
        "state NOT IN ('WAITING_FOR_DATA', 'SKIPPED', 'EXPIRED', 'FAILED') "
        "OR reason_code IS NOT NULL",
        name="decision_reason_consistent",
    ),
    CheckConstraint("attempt_count >= 0", name="decision_attempts_nonnegative"),
    CheckConstraint("experiment_version >= 1", name="decision_experiment_version_positive"),
    CheckConstraint("version >= 1", name="decision_version_positive"),
    CheckConstraint(
        "source_interval_start < source_interval_end "
        "AND source_interval_end <= ready_at AND ready_at < deadline_at "
        "AND deadline_at <= required_completion_at",
        name="decision_deadlines_ordered",
    ),
    _hash_constraint("content_hash"),
    info={"state": True},
)
Index("ix_aqa_decision_slots_claim", aqa_decision_slots.c.state, aqa_decision_slots.c.ready_at)
Index("ix_aqa_decision_slots_lease", aqa_decision_slots.c.lease_expires_at)
Index(
    "ix_aqa_decision_slots_session",
    aqa_decision_slots.c.experiment_hash,
    aqa_decision_slots.c.session_date,
    aqa_decision_slots.c.ready_at,
)

aqa_signal_envelopes = Table(
    "aqa_signal_envelopes",
    metadata,
    Column("signal_id", String(128), primary_key=True),
    Column(
        "slot_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_decision_slots.slot_id"),
        nullable=False,
        unique=True,
    ),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("provider_id", String(64), nullable=False),
    Column("provider_version", String(64), nullable=False),
    Column("contract_version", Integer, nullable=False),
    Column("correlation_id", String(128), nullable=False),
    Column("provider_source_mode", String(32), nullable=False),
    Column("experiment_id", String(64), nullable=False),
    Column("experiment_version", BigInteger, nullable=False),
    Column("data_contract_hash", String(64), nullable=False),
    Column("policy_hash", String(64), nullable=False),
    Column("source_bar_end", UTCDateTime(), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("expires_at", UTCDateTime(), nullable=False),
    Column("active_symbols", json_value, nullable=False),
    Column("availability_mask", json_value, nullable=False),
    Column("actions", json_value, nullable=False),
    Column("expected_edge_bps", json_value, nullable=False),
    Column("proposed_signed_target_inputs", json_value, nullable=False),
    Column("artifact_id", String(128)),
    Column("artifact_hash", String(64)),
    Column("promotable", Boolean, nullable=False),
    Column("paper_submission_eligible", Boolean, nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    CheckConstraint("contract_version >= 1", name="signal_contract_version_positive"),
    CheckConstraint("experiment_version >= 1", name="signal_experiment_version_positive"),
    CheckConstraint(
        "provider_source_mode IN ('builtin', 'offline_fixture', 'registered_plugin')",
        name="signal_source_mode",
    ),
    CheckConstraint(
        "source_bar_end <= created_at AND created_at < expires_at", name="signal_times_ordered"
    ),
    CheckConstraint(
        "(artifact_id IS NULL AND artifact_hash IS NULL) "
        "OR (artifact_id IS NOT NULL AND artifact_hash IS NOT NULL)",
        name="signal_artifact_consistent",
    ),
    _hash_constraint("data_contract_hash"),
    _hash_constraint("policy_hash"),
    CheckConstraint(
        "artifact_hash IS NULL OR length(artifact_hash) = 64",
        name="signal_artifact_hash_sha256_length",
    ),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_risk_latch_events = Table(
    "aqa_risk_latch_events",
    metadata,
    Column("latch_event_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("latch_type", String(32), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("action", String(16), nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    Column("payload", json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("experiment_hash", "latch_type", "sequence", name="risk_latch_sequence"),
    CheckConstraint("sequence >= 1", name="risk_latch_sequence_positive"),
    CheckConstraint("action IN ('engage', 'clear')", name="risk_latch_action"),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_risk_decisions = Table(
    "aqa_risk_decisions",
    metadata,
    Column("risk_decision_id", String(128), primary_key=True),
    Column(
        "slot_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_decision_slots.slot_id"),
        nullable=False,
    ),
    Column(
        "signal_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_signal_envelopes.signal_id"),
        nullable=False,
        unique=True,
    ),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("policy_id", String(64), nullable=False),
    Column("policy_version", BigInteger, nullable=False),
    Column("decided_at", UTCDateTime(), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("proposed_targets", json_value, nullable=False),
    Column("approved_targets", json_value, nullable=False),
    Column("controls", json_value, nullable=False),
    Column("reason_codes", json_value, nullable=False),
    Column("gross_exposure", FiniteNumeric(38, 18), nullable=False),
    Column("net_exposure", FiniteNumeric(38, 18), nullable=False),
    Column("cash_weight", FiniteNumeric(38, 18), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("signature", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    CheckConstraint("policy_version >= 1", name="risk_policy_version_positive"),
    _finite_numeric_constraint("gross_exposure", "net_exposure", "cash_weight"),
    _hash_constraint("input_hash"),
    _hash_constraint("payload_hash"),
    _hash_constraint("signature"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_execution_plans = Table(
    "aqa_execution_plans",
    metadata,
    Column("execution_plan_id", String(128), primary_key=True),
    Column(
        "risk_decision_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_risk_decisions.risk_decision_id"),
        nullable=False,
    ),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("target_version", BigInteger, nullable=False),
    Column("forced_flat", Boolean, nullable=False),
    Column("targets", json_value, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("signature", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("risk_decision_id", "target_version", name="execution_risk_target_version"),
    CheckConstraint("target_version >= 1", name="execution_target_version_positive"),
    _hash_constraint("payload_hash"),
    _hash_constraint("signature"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_order_intents = Table(
    "aqa_order_intents",
    metadata,
    Column("order_intent_id", String(128), primary_key=True),
    Column(
        "execution_plan_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_execution_plans.execution_plan_id"),
        nullable=False,
    ),
    Column("client_order_id", String(48), nullable=False, unique=True),
    Column("symbol", String(10), nullable=False),
    Column("side", String(4), nullable=False),
    Column("effect", String(8), nullable=False),
    Column("phase", String(16), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("quantity", FiniteNumeric(38, 18), nullable=False),
    Column("notional", FiniteNumeric(38, 18), nullable=False),
    Column("reference_price", FiniteNumeric(38, 18), nullable=False),
    Column("order_type", String(16), nullable=False),
    Column("time_in_force", String(16), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint(
        "execution_plan_id",
        "phase",
        "sequence",
        "symbol",
        name="order_plan_phase_sequence_symbol",
    ),
    CheckConstraint("side IN ('buy', 'sell')", name="order_side"),
    CheckConstraint("effect IN ('open', 'close', 'reduce')", name="order_effect"),
    CheckConstraint("phase IN ('exit', 'entry', 'flatten')", name="order_phase"),
    CheckConstraint("sequence >= 0", name="order_sequence_nonnegative"),
    CheckConstraint(
        "CAST(quantity AS NUMERIC) > 0 AND CAST(notional AS NUMERIC) >= 0 "
        "AND CAST(reference_price AS NUMERIC) > 0",
        name="order_numbers_valid",
    ),
    _finite_numeric_constraint("quantity", "notional", "reference_price"),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_broker_orders = Table(
    "aqa_broker_orders",
    metadata,
    Column(
        "client_order_id",
        String(48),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_order_intents.client_order_id"),
        primary_key=True,
    ),
    Column("broker_order_id", String(128), unique=True),
    Column("state", String(32), nullable=False),
    Column("submitted_at", UTCDateTime()),
    Column("accepted_at", UTCDateTime()),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("cumulative_filled_quantity", FiniteNumeric(38, 18), nullable=False),
    Column("average_fill_price", FiniteNumeric(38, 18)),
    Column("last_event_sequence", BigInteger, nullable=False),
    Column("safe_error_code", String(64)),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    CheckConstraint(
        "state IN ('planned', 'submitting', 'accepted', 'partially_filled', 'filled', "
        "'cancel_pending', 'cancelled', 'rejected', 'expired', 'unknown')",
        name="broker_order_state",
    ),
    CheckConstraint(
        "CAST(cumulative_filled_quantity AS NUMERIC) >= 0",
        name="broker_order_filled_nonnegative",
    ),
    CheckConstraint(
        "average_fill_price IS NULL OR CAST(average_fill_price AS NUMERIC) > 0",
        name="broker_order_average_positive",
    ),
    _finite_numeric_constraint(
        "cumulative_filled_quantity",
        "average_fill_price",
        nullable=frozenset({"average_fill_price"}),
    ),
    CheckConstraint("last_event_sequence >= 0", name="broker_order_event_sequence_nonnegative"),
    CheckConstraint("version >= 1", name="broker_order_version_positive"),
    _hash_constraint("content_hash"),
    info={"state": True},
)

aqa_order_events = Table(
    "aqa_order_events",
    metadata,
    Column("order_event_id", String(128), primary_key=True),
    Column(
        "client_order_id",
        String(48),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_broker_orders.client_order_id"),
        nullable=False,
    ),
    Column("sequence", BigInteger, nullable=False),
    Column("from_state", String(32)),
    Column("to_state", String(32), nullable=False),
    Column("broker_event_id", String(128), unique=True),
    Column("occurred_at", UTCDateTime(), nullable=False),
    Column("payload", json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("client_order_id", "sequence", name="order_event_sequence"),
    CheckConstraint("sequence >= 1", name="order_event_sequence_positive"),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_fills = Table(
    "aqa_fills",
    metadata,
    Column("fill_id", String(128), primary_key=True),
    Column(
        "client_order_id",
        String(48),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_broker_orders.client_order_id"),
        nullable=False,
    ),
    Column("broker_execution_id", String(128), nullable=False, unique=True),
    Column("symbol", String(10), nullable=False),
    Column("side", String(4), nullable=False),
    Column("quantity", FiniteNumeric(38, 18), nullable=False),
    Column("price", FiniteNumeric(38, 18), nullable=False),
    Column("fee", FiniteNumeric(38, 18), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    CheckConstraint("side IN ('buy', 'sell')", name="fill_side"),
    CheckConstraint(
        "CAST(quantity AS NUMERIC) > 0 AND CAST(price AS NUMERIC) > 0 "
        "AND CAST(fee AS NUMERIC) >= 0",
        name="fill_numbers_valid",
    ),
    _finite_numeric_constraint("quantity", "price", "fee"),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_reconciliations = Table(
    "aqa_reconciliations",
    metadata,
    Column("reconciliation_id", String(128), primary_key=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
        nullable=False,
    ),
    Column("slot_id", String(128), ForeignKey(f"{PLATFORM_SCHEMA}.aqa_decision_slots.slot_id")),
    Column(
        "execution_plan_id",
        String(128),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_execution_plans.execution_plan_id"),
    ),
    Column("account_id_hash", String(64), nullable=False),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime(), nullable=False),
    Column("status", String(16), nullable=False),
    Column("blocking", Boolean, nullable=False),
    Column("positions", json_value, nullable=False),
    Column("orders", json_value, nullable=False),
    Column("discrepancies", json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    CheckConstraint("started_at <= completed_at", name="reconciliation_times_ordered"),
    CheckConstraint("status IN ('clean', 'blocking')", name="reconciliation_status"),
    CheckConstraint("(status = 'blocking') = blocking", name="reconciliation_blocking_consistent"),
    _hash_constraint("account_id_hash"),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_incidents = Table(
    "aqa_incidents",
    metadata,
    Column("incident_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), nullable=False, unique=True),
    Column(
        "experiment_hash",
        String(64),
        ForeignKey(f"{PLATFORM_SCHEMA}.aqa_experiments.experiment_hash"),
    ),
    Column("incident_type", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("details", json_value, nullable=False),
    Column("opened_at", UTCDateTime(), nullable=False),
    Column("resolved_at", UTCDateTime()),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    CheckConstraint(
        "severity IN ('info', 'warning', 'error', 'critical')", name="incident_severity"
    ),
    CheckConstraint("status IN ('open', 'resolved')", name="incident_status"),
    CheckConstraint(
        "(status = 'resolved' AND resolved_at IS NOT NULL) OR "
        "(status = 'open' AND resolved_at IS NULL)",
        name="incident_resolution_consistent",
    ),
    CheckConstraint("version >= 1", name="incident_version_positive"),
    _hash_constraint("content_hash"),
    info={"state": True},
)

aqa_jobs = Table(
    "aqa_jobs",
    metadata,
    Column("job_id", String(128), primary_key=True),
    Column("job_type", String(32), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("payload", json_value, nullable=False),
    Column("result", json_value),
    Column("lease_owner", String(128)),
    Column("lease_expires_at", UTCDateTime()),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("next_attempt_at", UTCDateTime()),
    Column("safe_error_code", String(64)),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    UniqueConstraint("job_type", "idempotency_key", name="job_type_idempotency"),
    CheckConstraint(
        "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="job_state"
    ),
    CheckConstraint(
        "((state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))",
        name="job_lease_consistent",
    ),
    CheckConstraint("attempt_count >= 0", name="job_attempts_nonnegative"),
    CheckConstraint("max_attempts = 3", name="job_max_attempts"),
    CheckConstraint("version >= 1", name="job_version_positive"),
    _hash_constraint("content_hash"),
    info={"state": True},
)
Index("ix_aqa_jobs_claim", aqa_jobs.c.state, aqa_jobs.c.next_attempt_at, aqa_jobs.c.created_at)

aqa_job_attempts = Table(
    "aqa_job_attempts",
    metadata,
    Column("job_attempt_id", String(128), primary_key=True),
    Column("job_id", String(128), ForeignKey(f"{PLATFORM_SCHEMA}.aqa_jobs.job_id"), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("owner", String(128), nullable=False),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("completed_at", UTCDateTime()),
    Column("outcome", String(16)),
    Column("safe_error_code", String(64)),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("job_id", "attempt_number", name="job_attempt_number"),
    CheckConstraint("attempt_number >= 1", name="job_attempt_number_positive"),
    CheckConstraint(
        "outcome IS NULL OR outcome IN ('succeeded', 'failed', 'abandoned')",
        name="job_attempt_outcome",
    ),
    CheckConstraint(
        "completed_at IS NULL OR completed_at >= started_at", name="job_attempt_times_ordered"
    ),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)

aqa_outbox_events = Table(
    "aqa_outbox_events",
    metadata,
    Column("outbox_event_id", String(128), primary_key=True),
    Column("aggregate_type", String(32), nullable=False),
    Column("aggregate_id", String(128), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("next_attempt_at", UTCDateTime()),
    Column("published_at", UTCDateTime()),
    Column("content_hash", String(64), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    CheckConstraint("state IN ('pending', 'published', 'failed')", name="outbox_state"),
    CheckConstraint("attempt_count >= 0", name="outbox_attempts_nonnegative"),
    CheckConstraint("version >= 1", name="outbox_version_positive"),
    CheckConstraint(
        "(state = 'published' AND published_at IS NOT NULL) OR "
        "(state <> 'published' AND published_at IS NULL)",
        name="outbox_publication_consistent",
    ),
    _hash_constraint("payload_hash"),
    _hash_constraint("content_hash"),
    info={"state": True},
)
Index("ix_aqa_outbox_delivery", aqa_outbox_events.c.state, aqa_outbox_events.c.next_attempt_at)

aqa_audit_events = Table(
    "aqa_audit_events",
    metadata,
    Column("audit_event_id", String(128), primary_key=True),
    Column("stream_id", String(128), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("occurred_at", UTCDateTime(), nullable=False),
    Column("payload", json_value, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False, unique=True),
    Column("content_hash", String(64), nullable=False),
    UniqueConstraint("stream_id", "sequence", name="audit_stream_sequence"),
    CheckConstraint("sequence >= 1", name="audit_sequence_positive"),
    CheckConstraint("content_hash = event_hash", name="audit_content_matches_event"),
    _hash_constraint("previous_hash"),
    _hash_constraint("payload_hash"),
    _hash_constraint("event_hash"),
    _hash_constraint("content_hash"),
    info={"append_only": True},
)
PLATFORM_TABLE_NAMES = frozenset(table.name for table in metadata.tables.values())


def platform_tables() -> tuple[Table, ...]:
    """Return platform tables in foreign-key dependency order."""

    return tuple(metadata.sorted_tables)


def table_contracts() -> dict[str, dict[str, Any]]:
    """Return a read-only-copy-friendly summary for migration and architecture checks."""

    return {
        table.name: {
            "append_only": bool(table.info.get("append_only")),
            "state": bool(table.info.get("state")),
            "monotonic_column": table.info.get("monotonic_column"),
        }
        for table in platform_tables()
    }
