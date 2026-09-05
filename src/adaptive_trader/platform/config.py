"""Strict, immutable experiment configuration loading."""

from __future__ import annotations

import hmac
import os
import re
import stat
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Never, Self, SupportsIndex, cast
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from yaml.constructor import ConstructorError
from yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    NodeEvent,
)
from yaml.nodes import MappingNode

from adaptive_trader.platform.canonical import CanonicalizationError
from adaptive_trader.platform.constants import MAX_SIGNED_64_BIT_INTEGER
from adaptive_trader.platform.errors import (
    ExperimentConfigError,
    ExperimentHashMismatchError,
    RuntimeSettingsError,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.security import SecretFileReference, SecretFileVariable
from adaptive_trader.platform.universe import UniverseSpec, _normalize_symbol_tuple

_MAX_CONFIG_BYTES = 65_536
_MAX_YAML_DEPTH = 32
_MAX_YAML_NODES = 2_048
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", flags=re.ASCII)
_TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$", flags=re.ASCII)
_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", flags=re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_YAML_INTEGER_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$", flags=re.ASCII)
_RELATIVE_CONFIG_PATH_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$",
    flags=re.ASCII,
)
_RUNTIME_HOST_LABEL_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    flags=re.ASCII,
)
_CONCRETE_PATH_TYPE = type(Path())
_RUNTIME_SETTINGS_CONSTRUCTION_TOKEN = object()
_MAX_RUNTIME_PATH_BYTES = 4_096
_MAX_RUNTIME_PATH_COMPONENT_BYTES = 255


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


def _validate_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase ASCII identifier")
    return value


def _validate_token(value: object, *, field_name: str) -> str:
    if type(value) is not str or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded ASCII token")
    return value


def _validate_time(value: object, *, field_name: str) -> str:
    if type(value) is not str or _TIME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use zero-padded HH:MM:SS")
    return value


def _seconds_since_midnight(value: str) -> int:
    hours, minutes, seconds = (int(component) for component in value.split(":"))
    return (hours * 60 * 60) + (minutes * 60) + seconds


def _validate_decimal(value: object, *, field_name: str) -> Decimal:
    if type(value) is Decimal:
        decimal_value = value
    elif type(value) is str and len(value) <= 128 and _DECIMAL_PATTERN.fullmatch(value) is not None:
        parse_failed = False
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            parse_failed = True
            decimal_value = Decimal(0)
        if parse_failed:
            raise ValueError(f"{field_name} must be a finite plain decimal")
    else:
        raise ValueError(f"{field_name} must be a finite plain decimal string")

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    _, digits, exponent = decimal_value.as_tuple()
    if len(digits) > 64 or not isinstance(exponent, int) or not -64 <= exponent <= 64:
        raise ValueError(f"{field_name} exceeds the decimal representation limit")
    return decimal_value


class MarketDataSpec(_StrictFrozenModel):
    """Canonical provider and bar identity for an experiment."""

    provider: str
    feed: str
    adjustment: str
    source_timeframe: str
    decision_timeframe: str
    exchange_calendar: str
    regular_hours_only: bool

    @field_validator(
        "provider",
        "feed",
        "adjustment",
        "source_timeframe",
        "decision_timeframe",
        "exchange_calendar",
        mode="before",
    )
    @classmethod
    def validate_tokens(cls, value: object, info: ValidationInfo) -> str:
        return _validate_token(value, field_name=info.field_name or "market_data")


class SessionSpec(_StrictFrozenModel):
    """Regular-session decision and forced-flat timing policy."""

    open: str
    close: str
    first_strategy_bar_close: str
    last_strategy_bar_close: str
    decision_ready_delay_seconds: int = Field(gt=0, le=3_600)
    decision_deadline_delay_seconds: int = Field(gt=0, le=3_600)
    forced_flat_target_time: str
    forced_flat_submit_deadline: str
    required_flat_time: str

    @field_validator(
        "open",
        "close",
        "first_strategy_bar_close",
        "last_strategy_bar_close",
        "forced_flat_target_time",
        "forced_flat_submit_deadline",
        "required_flat_time",
        mode="before",
    )
    @classmethod
    def validate_times(cls, value: object, info: ValidationInfo) -> str:
        return _validate_time(value, field_name=info.field_name or "session time")

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if not (
            self.open
            < self.first_strategy_bar_close
            <= self.last_strategy_bar_close
            < self.forced_flat_target_time
            < self.forced_flat_submit_deadline
            < self.required_flat_time
            < self.close
        ):
            raise ValueError("session times must follow the required chronological order")
        if self.decision_ready_delay_seconds > self.decision_deadline_delay_seconds:
            raise ValueError("decision readiness cannot follow the decision deadline")
        final_decision_deadline = (
            _seconds_since_midnight(self.last_strategy_bar_close)
            + self.decision_deadline_delay_seconds
        )
        if final_decision_deadline >= _seconds_since_midnight(self.forced_flat_target_time):
            raise ValueError("the final decision deadline must precede forced flattening")
        return self


class RiskGroupSpec(_StrictFrozenModel):
    """A deterministic group-level gross exposure constraint."""

    id: str
    symbols: tuple[str, ...]
    max_gross_weight: Decimal

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value: object) -> str:
        return _validate_identifier(value, field_name="risk group id")

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> tuple[str, ...]:
        symbols = _normalize_symbol_tuple(value)
        if not symbols:
            raise ValueError("risk group symbols must not be empty")
        return symbols

    @field_validator("max_gross_weight", mode="before")
    @classmethod
    def validate_max_gross_weight(cls, value: object) -> Decimal:
        return _validate_decimal(value, field_name="max_gross_weight")

    @model_validator(mode="after")
    def validate_limit(self) -> Self:
        if not Decimal(0) < self.max_gross_weight <= Decimal(1):
            raise ValueError("max_gross_weight must be greater than zero and at most one")
        return self


class RiskPolicySpec(_StrictFrozenModel):
    """Versioned numeric risk policy referenced by an experiment."""

    id: str
    version: int = Field(ge=1, le=MAX_SIGNED_64_BIT_INTEGER)
    max_absolute_symbol_weight: Decimal
    max_gross_weight: Decimal
    min_net_weight: Decimal
    max_net_weight: Decimal
    max_cluster_gross_weight: Decimal
    correlation_edge_threshold: Decimal
    minimum_rebalance_equity_fraction: Decimal
    session_loss_trigger: Decimal
    deployment_drawdown_trigger: Decimal
    target_annualized_volatility: Decimal
    sigma_floor: Decimal
    edge_saturation_bps: Decimal
    covariance_eigenvalue_floor: Decimal

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value: object) -> str:
        return _validate_identifier(value, field_name="risk policy id")

    @field_validator(
        "max_absolute_symbol_weight",
        "max_gross_weight",
        "min_net_weight",
        "max_net_weight",
        "max_cluster_gross_weight",
        "correlation_edge_threshold",
        "minimum_rebalance_equity_fraction",
        "session_loss_trigger",
        "deployment_drawdown_trigger",
        "target_annualized_volatility",
        "sigma_floor",
        "edge_saturation_bps",
        "covariance_eigenvalue_floor",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: object, info: ValidationInfo) -> Decimal:
        return _validate_decimal(value, field_name=info.field_name or "risk value")

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        zero = Decimal(0)
        one = Decimal(1)
        negative_one = one.copy_negate()
        negative_gross = self.max_gross_weight.copy_negate()
        if not zero < self.max_absolute_symbol_weight <= one:
            raise ValueError("max_absolute_symbol_weight must be in (0, 1]")
        if not zero < self.max_gross_weight <= one:
            raise ValueError("max_gross_weight must be in (0, 1]")
        if self.max_absolute_symbol_weight > self.max_gross_weight:
            raise ValueError("symbol weight cannot exceed the gross limit")
        if not negative_gross <= self.min_net_weight <= self.max_net_weight:
            raise ValueError("net weight limits are inconsistent with gross exposure")
        if self.max_net_weight > self.max_gross_weight:
            raise ValueError("net weight limits are inconsistent with gross exposure")
        if not zero < self.max_cluster_gross_weight <= self.max_gross_weight:
            raise ValueError("max_cluster_gross_weight must be in (0, max_gross_weight]")
        if not zero <= self.correlation_edge_threshold <= one:
            raise ValueError("correlation_edge_threshold must be in [0, 1]")
        if not zero <= self.minimum_rebalance_equity_fraction <= one:
            raise ValueError("minimum_rebalance_equity_fraction must be in [0, 1]")
        if not negative_one <= self.session_loss_trigger < zero:
            raise ValueError("session_loss_trigger must be in [-1, 0)")
        if not negative_one <= self.deployment_drawdown_trigger < zero:
            raise ValueError("deployment_drawdown_trigger must be in [-1, 0)")
        if self.deployment_drawdown_trigger > self.session_loss_trigger:
            raise ValueError("deployment drawdown must not be shallower than session loss")
        positive_fields = (
            self.target_annualized_volatility,
            self.sigma_floor,
            self.edge_saturation_bps,
            self.covariance_eigenvalue_floor,
        )
        if any(value <= zero for value in positive_fields):
            raise ValueError("volatility, saturation, and eigenvalue values must be positive")
        return self


class ExperimentDefinition(UniverseSpec):
    """Immutable, content-addressed experiment data loaded before profile composition.

    A runtime ``ExperimentSpec`` will add the signal-provider identity and execution mode selected
    by a strict platform profile. Keeping this file contract separate lets all profiles pin the
    same market, session, universe, and risk-policy definition.
    """

    schema_version: Literal[1]
    experiment_id: str
    experiment_version: int = Field(ge=1, le=MAX_SIGNED_64_BIT_INTEGER)
    market_data: MarketDataSpec
    session: SessionSpec
    risk_groups: tuple[RiskGroupSpec, ...]
    risk_policy: RiskPolicySpec

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("experiment_id", mode="before")
    @classmethod
    def validate_experiment_id(cls, value: object) -> str:
        return _validate_identifier(value, field_name="experiment_id")

    @field_validator("risk_groups", mode="before")
    @classmethod
    def freeze_risk_groups(cls, value: object) -> tuple[object, ...]:
        if type(value) not in {list, tuple}:
            raise ValueError("risk_groups must be a list or tuple")
        return tuple(cast(list[object] | tuple[object, ...], value))

    @field_validator("risk_groups")
    @classmethod
    def order_risk_groups(cls, value: tuple[RiskGroupSpec, ...]) -> tuple[RiskGroupSpec, ...]:
        return tuple(sorted(value, key=lambda group: group.id))

    @model_validator(mode="after")
    def validate_risk_groups(self) -> Self:
        if not self.risk_groups:
            raise ValueError("risk_groups must not be empty")
        group_ids = tuple(group.id for group in self.risk_groups)
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("risk group ids must be unique")

        grouped_symbols = tuple(symbol for group in self.risk_groups for symbol in group.symbols)
        if len(grouped_symbols) != len(set(grouped_symbols)):
            raise ValueError("a symbol cannot belong to multiple risk groups")
        if set(grouped_symbols) != set(self.active_tradable):
            raise ValueError("risk groups must partition active_tradable")
        if any(
            group.max_gross_weight > self.risk_policy.max_gross_weight for group in self.risk_groups
        ):
            raise ValueError("risk group gross limit cannot exceed the policy gross limit")
        return self

    def hash_payload(self) -> dict[str, object]:
        """Return the explicit semantic preimage used by ``content_hash``."""

        return {
            "active_tradable": self.active_tradable,
            "benchmark_only": self.benchmark_only,
            "context_only": self.context_only,
            "excluded": self.excluded,
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "market_data": {
                "adjustment": self.market_data.adjustment,
                "decision_timeframe": self.market_data.decision_timeframe,
                "exchange_calendar": self.market_data.exchange_calendar,
                "feed": self.market_data.feed,
                "provider": self.market_data.provider,
                "regular_hours_only": self.market_data.regular_hours_only,
                "source_timeframe": self.market_data.source_timeframe,
            },
            "risk_groups": tuple(
                {
                    "id": group.id,
                    "max_gross_weight": group.max_gross_weight,
                    "symbols": group.symbols,
                }
                for group in self.risk_groups
            ),
            "risk_policy": {
                "correlation_edge_threshold": self.risk_policy.correlation_edge_threshold,
                "covariance_eigenvalue_floor": self.risk_policy.covariance_eigenvalue_floor,
                "deployment_drawdown_trigger": self.risk_policy.deployment_drawdown_trigger,
                "edge_saturation_bps": self.risk_policy.edge_saturation_bps,
                "id": self.risk_policy.id,
                "max_absolute_symbol_weight": self.risk_policy.max_absolute_symbol_weight,
                "max_cluster_gross_weight": self.risk_policy.max_cluster_gross_weight,
                "max_gross_weight": self.risk_policy.max_gross_weight,
                "max_net_weight": self.risk_policy.max_net_weight,
                "min_net_weight": self.risk_policy.min_net_weight,
                "minimum_rebalance_equity_fraction": (
                    self.risk_policy.minimum_rebalance_equity_fraction
                ),
                "session_loss_trigger": self.risk_policy.session_loss_trigger,
                "sigma_floor": self.risk_policy.sigma_floor,
                "target_annualized_volatility": self.risk_policy.target_annualized_volatility,
                "version": self.risk_policy.version,
            },
            "schema_version": self.schema_version,
            "session": {
                "close": self.session.close,
                "decision_deadline_delay_seconds": (self.session.decision_deadline_delay_seconds),
                "decision_ready_delay_seconds": self.session.decision_ready_delay_seconds,
                "first_strategy_bar_close": self.session.first_strategy_bar_close,
                "forced_flat_submit_deadline": self.session.forced_flat_submit_deadline,
                "forced_flat_target_time": self.session.forced_flat_target_time,
                "last_strategy_bar_close": self.session.last_strategy_bar_close,
                "open": self.session.open,
                "required_flat_time": self.session.required_flat_time,
            },
        }

    @property
    def content_hash(self) -> str:
        """Lowercase SHA-256 of the normalized semantic experiment content."""

        return sha256_hex(self.hash_payload())


class ExecutionMode(StrEnum):
    """Closed runtime modes supported by the platform configuration boundary."""

    OFFLINE = "offline"
    SHADOW = "shadow"
    PAPER = "paper"


class MarketDataAdapter(StrEnum):
    """Runtime source of canonical market-data observations."""

    FIXTURE = "fixture"
    ALPACA = "alpaca"


class BrokerAdapter(StrEnum):
    """Runtime execution adapter selected by a platform profile."""

    FAKE = "fake"
    NONE = "none"
    ALPACA_PAPER = "alpaca_paper"


def _validate_enum_member(value: object, *, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{field_name} is not supported") from error


def _validate_relative_config_path(value: object, *, field_name: str) -> str:
    if type(value) is not str or _RELATIVE_CONFIG_PATH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field_name} contains a prohibited component")
    return value


class SignalProviderSpec(_StrictFrozenModel):
    """Declarative signal-provider identity without import or execution authority."""

    id: str

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value: object) -> str:
        return _validate_identifier(value, field_name="signal provider id")


class ExecutionSpec(_StrictFrozenModel):
    """Static broker selection and submission policy from one profile."""

    broker: BrokerAdapter
    submission_enabled: bool
    paper_only: Literal[True]

    @field_validator("broker", mode="before")
    @classmethod
    def validate_broker(cls, value: object) -> BrokerAdapter:
        return cast(
            BrokerAdapter,
            _validate_enum_member(value, enum_type=BrokerAdapter, field_name="broker"),
        )

    @field_validator("paper_only", mode="before")
    @classmethod
    def validate_paper_only(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("paper_only must be boolean true")
        return value

    @model_validator(mode="after")
    def validate_submission_authority(self) -> Self:
        if self.submission_enabled and self.broker is not BrokerAdapter.ALPACA_PAPER:
            raise ValueError("submission can be enabled only for the Alpaca paper adapter")
        return self


class StoragePolicySpec(_StrictFrozenModel):
    """Declarative storage sources; this model never reads a setting or secret."""

    database_url_source: Literal["AQA_DATABASE_URL_FILE"]
    database_required: bool
    offline_fallback: Literal["sqlite", "none"]
    artifact_root_source: Literal["AQA_ARTIFACT_ROOT"]


class PlatformProfile(_StrictFrozenModel):
    """Strict, content-addressed platform profile loaded before runtime settings."""

    schema_version: Literal[1]
    profile_id: str
    mode: ExecutionMode
    experiment_path: str
    expected_experiment_hash: str
    market_data_adapter: MarketDataAdapter
    signal_provider: SignalProviderSpec
    execution: ExecutionSpec
    storage: StoragePolicySpec

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("profile_id", mode="before")
    @classmethod
    def validate_profile_id(cls, value: object) -> str:
        return _validate_identifier(value, field_name="profile_id")

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, value: object) -> ExecutionMode:
        return cast(
            ExecutionMode,
            _validate_enum_member(value, enum_type=ExecutionMode, field_name="mode"),
        )

    @field_validator("experiment_path", mode="before")
    @classmethod
    def validate_experiment_path(cls, value: object) -> str:
        return _validate_relative_config_path(value, field_name="experiment_path")

    @field_validator("expected_experiment_hash", mode="before")
    @classmethod
    def validate_expected_experiment_hash(cls, value: object) -> str:
        return _validate_expected_hash(value)

    @field_validator("market_data_adapter", mode="before")
    @classmethod
    def validate_market_data_adapter(cls, value: object) -> MarketDataAdapter:
        return cast(
            MarketDataAdapter,
            _validate_enum_member(
                value,
                enum_type=MarketDataAdapter,
                field_name="market_data_adapter",
            ),
        )

    @model_validator(mode="after")
    def validate_mode_contract(self) -> Self:
        reserved_profile_ids = {mode.value for mode in ExecutionMode}
        if self.profile_id in reserved_profile_ids and self.profile_id != self.mode.value:
            raise ValueError("reserved profile_id does not match the selected mode")

        expected = {
            ExecutionMode.OFFLINE: (
                MarketDataAdapter.FIXTURE,
                BrokerAdapter.FAKE,
                False,
                "sqlite",
            ),
            ExecutionMode.SHADOW: (
                MarketDataAdapter.ALPACA,
                BrokerAdapter.NONE,
                True,
                "none",
            ),
            ExecutionMode.PAPER: (
                MarketDataAdapter.ALPACA,
                BrokerAdapter.ALPACA_PAPER,
                True,
                "none",
            ),
        }
        data_adapter, broker, database_required, fallback = expected[self.mode]
        if self.market_data_adapter is not data_adapter:
            raise ValueError("market-data adapter does not match the selected mode")
        if (
            self.mode is not ExecutionMode.OFFLINE
            and self.signal_provider.id == "deterministic_fixture"
        ):
            raise ValueError("the deterministic fixture signal provider is offline-only")
        if self.execution.broker is not broker:
            raise ValueError("broker adapter does not match the selected mode")
        if self.mode is not ExecutionMode.PAPER and self.execution.submission_enabled:
            raise ValueError("submission must be disabled outside paper mode")
        if (
            self.storage.database_required is not database_required
            or self.storage.offline_fallback != fallback
        ):
            raise ValueError("storage policy does not match the selected mode")
        return self

    def hash_payload(self) -> dict[str, object]:
        """Return every semantic profile field in a stable canonical preimage."""

        return {
            "execution": {
                "broker": self.execution.broker,
                "paper_only": self.execution.paper_only,
                "submission_enabled": self.execution.submission_enabled,
            },
            "expected_experiment_hash": self.expected_experiment_hash,
            "experiment_path": self.experiment_path,
            "market_data_adapter": self.market_data_adapter,
            "mode": self.mode,
            "profile_id": self.profile_id,
            "schema_version": self.schema_version,
            "signal_provider": {"id": self.signal_provider.id},
            "storage": {
                "artifact_root_source": self.storage.artifact_root_source,
                "database_required": self.storage.database_required,
                "database_url_source": self.storage.database_url_source,
                "offline_fallback": self.storage.offline_fallback,
            },
        }

    @property
    def content_hash(self) -> str:
        """Lowercase SHA-256 of the complete normalized profile."""

        return sha256_hex(self.hash_payload())


class ExperimentSpec(_StrictFrozenModel):
    """Verified experiment definition composed with runtime decision identity."""

    schema_version: Literal[1]
    definition: ExperimentDefinition
    definition_hash: str
    signal_provider: SignalProviderSpec
    execution_mode: ExecutionMode

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("definition_hash", mode="before")
    @classmethod
    def validate_definition_hash(cls, value: object) -> str:
        return _validate_expected_hash(value)

    @model_validator(mode="after")
    def validate_composition(self) -> Self:
        if not hmac.compare_digest(self.definition.content_hash, self.definition_hash):
            raise ValueError("definition hash does not match the composed definition")
        if (
            self.execution_mode is not ExecutionMode.OFFLINE
            and self.signal_provider.id == "deterministic_fixture"
        ):
            raise ValueError("the deterministic fixture signal provider is offline-only")
        if self.execution_mode is not ExecutionMode.OFFLINE and (
            self.definition.market_data.provider != "alpaca"
            or self.definition.market_data.feed != "iex"
            or self.definition.market_data.adjustment != "raw"
        ):
            raise ValueError("external execution mode conflicts with the experiment definition")
        return self

    @property
    def experiment_id(self) -> str:
        return self.definition.experiment_id

    @property
    def experiment_version(self) -> int:
        return self.definition.experiment_version

    def hash_payload(self) -> dict[str, object]:
        """Return the identity required to reproduce one decision experiment."""

        return {
            "definition_hash": self.definition_hash,
            "execution_mode": self.execution_mode,
            "schema_version": self.schema_version,
            "signal_provider": {"id": self.signal_provider.id},
        }

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.hash_payload())


class PlatformConfig(_StrictFrozenModel):
    """Fully composed static configuration with no loaded runtime secrets."""

    profile: PlatformProfile
    experiment: ExperimentSpec

    @model_validator(mode="after")
    def validate_alignment(self) -> Self:
        if not hmac.compare_digest(
            self.profile.expected_experiment_hash,
            self.experiment.definition_hash,
        ):
            raise ValueError("profile and experiment hashes do not match")
        if (
            self.profile.mode is not self.experiment.execution_mode
            or self.profile.signal_provider != self.experiment.signal_provider
        ):
            raise ValueError("profile and composed experiment identities do not match")
        if self.profile.market_data_adapter is MarketDataAdapter.ALPACA and (
            self.experiment.definition.market_data.provider != "alpaca"
            or self.experiment.definition.market_data.feed != "iex"
            or self.experiment.definition.market_data.adjustment != "raw"
        ):
            raise ValueError("external adapter identity conflicts with the experiment definition")
        return self

    def hash_payload(self) -> dict[str, object]:
        """Bind the selected profile and the complete composed experiment identity."""

        return {
            "experiment_hash": self.experiment.content_hash,
            "profile_hash": self.profile.content_hash,
        }

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.hash_payload())


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


_UniqueKeySafeLoader.yaml_implicit_resolvers = {
    key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _resolver_key, _resolvers in _UniqueKeySafeLoader.yaml_implicit_resolvers.items():
    _UniqueKeySafeLoader.yaml_implicit_resolvers[_resolver_key] = [
        resolver
        for resolver in _resolvers
        if resolver[0]
        not in {
            "tag:yaml.org,2002:bool",
            "tag:yaml.org,2002:int",
            "tag:yaml.org,2002:timestamp",
        }
    ]
_UniqueKeySafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$"),
    list("tf"),
)
_UniqueKeySafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    _YAML_INTEGER_PATTERN,
    list("0123456789"),
)


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[str, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(None, None, "expected a mapping", node.start_mark)

    mapping: dict[str, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge" or key_node.value == "<<":
            raise ConstructorError(None, None, "mapping merges are prohibited", key_node.start_mark)
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str:
            raise ConstructorError(None, None, "mapping keys must be strings", key_node.start_mark)
        if key in mapping:
            raise ConstructorError(None, None, "duplicate mapping key", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _safe_relative_parts(path: Path) -> tuple[str, ...]:
    if not isinstance(path, Path):
        raise ExperimentConfigError("experiment path must be a pathlib Path")
    rendered = os.fspath(path)
    if type(rendered) is not str:
        raise ExperimentConfigError("experiment path must use a text representation")
    safe_path = Path(rendered)
    if not rendered or "\x00" in rendered or safe_path.is_absolute():
        raise ExperimentConfigError("experiment path must be a nonempty relative path")
    parts = safe_path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ExperimentConfigError("experiment path contains a prohibited component")
    return parts


def _open_nonsymlink_directory(path: Path) -> int:
    if path.anchor != os.sep or path.as_posix() != os.fspath(path):
        raise OSError("directory root is not a canonical POSIX path")
    if any(component in {"", ".", ".."} for component in path.parts[1:]):
        raise OSError("directory root contains a prohibited component")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        resolved_before = path.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise OSError("directory root could not be resolved") from error
    if resolved_before != path:
        raise OSError("directory root contains a symbolic link")

    directory_fd = os.open(path, flags)
    try:
        directory_stat = os.fstat(directory_fd)
        path_stat = os.stat(path, follow_symlinks=False)
        resolved_after = path.resolve(strict=True)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or resolved_after != path
            or (directory_stat.st_dev, directory_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise OSError("directory root changed during validation")
        return directory_fd
    except (OSError, RuntimeError, TypeError, ValueError):
        os.close(directory_fd)
        raise


def _read_config_file(path: Path, *, config_root: Path) -> bytes:
    parts = _safe_relative_parts(path)
    if not isinstance(config_root, Path):
        raise ExperimentConfigError("config_root must be an absolute pathlib Path")
    root_rendered = os.fspath(config_root)
    if type(root_rendered) is not str:
        raise ExperimentConfigError("config_root must use a text representation")
    safe_root = Path(root_rendered)
    if not safe_root.is_absolute():
        raise ExperimentConfigError("config_root must be an absolute pathlib Path")
    root_fd = -1
    current_fd = -1
    read_failed = False
    payload = b""
    try:
        root_fd = _open_nonsymlink_directory(safe_root)
        current_fd = root_fd
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd

        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current_fd,
        )
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                read_failed = True
            else:
                with os.fdopen(file_fd, "rb", closefd=False) as stream:
                    payload = stream.read(_MAX_CONFIG_BYTES + 1)
        finally:
            os.close(file_fd)
    except (OSError, TypeError, ValueError):
        read_failed = True
    finally:
        if current_fd >= 0 and current_fd != root_fd:
            os.close(current_fd)
        if root_fd >= 0:
            os.close(root_fd)

    if read_failed:
        raise ExperimentConfigError("experiment configuration could not be read")
    if len(payload) > _MAX_CONFIG_BYTES:
        raise ExperimentConfigError("experiment configuration exceeds the size limit")
    return payload


def _yaml_events(text: str) -> Iterator[object]:
    return cast(Iterator[object], yaml.parse(text, Loader=_UniqueKeySafeLoader))


def _inspect_yaml(text: str) -> None:
    document_count = 0
    depth = 0
    nodes = 0
    for event in _yaml_events(text):
        if isinstance(event, DocumentStartEvent):
            document_count += 1
        if isinstance(event, AliasEvent) or (
            isinstance(event, NodeEvent) and getattr(event, "anchor", None) is not None
        ):
            raise ExperimentConfigError("YAML anchors and aliases are prohibited")
        if isinstance(event, NodeEvent) and getattr(event, "tag", None) is not None:
            raise ExperimentConfigError("explicit YAML tags are prohibited")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise ExperimentConfigError("YAML nesting exceeds the depth limit")
        elif isinstance(event, CollectionEndEvent):
            depth -= 1
        if isinstance(event, NodeEvent):
            nodes += 1
            if nodes > _MAX_YAML_NODES:
                raise ExperimentConfigError("YAML content exceeds the node limit")
    if document_count != 1:
        raise ExperimentConfigError("experiment configuration must contain one YAML document")


def _decode_and_load_yaml(payload: bytes) -> dict[str, object]:
    decode_failed = False
    text = ""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed:
        raise ExperimentConfigError("experiment configuration must be valid UTF-8")
    if text.startswith("\ufeff") or "\x00" in text:
        raise ExperimentConfigError("experiment configuration contains prohibited text")

    parse_failed = False
    loaded: object = None
    try:
        _inspect_yaml(text)
        loaded = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except ExperimentConfigError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, yaml.YAMLError):
        parse_failed = True
    if parse_failed:
        raise ExperimentConfigError("experiment configuration is not valid strict YAML")
    if type(loaded) is not dict:
        raise ExperimentConfigError("experiment configuration root must be a mapping")
    return cast(dict[str, object], loaded)


def _validate_expected_hash(expected_hash: object) -> str:
    if type(expected_hash) is not str or _SHA256_PATTERN.fullmatch(expected_hash) is None:
        raise ExperimentConfigError("expected_hash must be lowercase hexadecimal SHA-256")
    return expected_hash


def load_experiment(
    path: Path,
    *,
    config_root: Path,
    expected_hash: str | None = None,
) -> ExperimentDefinition:
    """Load one root-confined, strict YAML experiment and optionally pin its content hash.

    ``path`` is interpreted only relative to the trusted ``config_root``. Symlink traversal,
    anchors, aliases, merge keys, duplicate keys, multiple documents, and unknown model fields
    are rejected. This loader performs no environment lookup, client construction, or network I/O.
    """

    validated_expected = (
        _validate_expected_hash(expected_hash) if expected_hash is not None else None
    )
    payload = _read_config_file(path, config_root=config_root)
    raw = _decode_and_load_yaml(payload)

    validation_failed = False
    experiment: ExperimentDefinition | None = None
    try:
        experiment = ExperimentDefinition.model_validate(raw)
    except ValidationError:
        validation_failed = True
    if validation_failed or experiment is None:
        raise ExperimentConfigError("experiment configuration failed strict validation")

    hash_failed = False
    actual_hash = ""
    try:
        actual_hash = experiment.content_hash
    except CanonicalizationError:
        hash_failed = True
    if hash_failed:
        raise ExperimentConfigError("experiment configuration could not be canonically hashed")
    if validated_expected is not None and not hmac.compare_digest(actual_hash, validated_expected):
        raise ExperimentHashMismatchError("experiment content hash does not match expected_hash")
    return experiment


def load_platform_config(
    path: Path,
    *,
    config_root: Path,
) -> PlatformConfig:
    """Load and compose one strict profile without resolving runtime settings or clients.

    Both the profile and its experiment are read beneath ``config_root`` through the same
    descriptor-relative, no-symlink boundary. The profile's experiment hash is mandatory and is
    verified before composition. No environment variable, secret, database, provider, plugin, or
    broker is read or constructed.
    """

    payload = _read_config_file(path, config_root=config_root)
    raw = _decode_and_load_yaml(payload)

    profile_failed = False
    profile: PlatformProfile | None = None
    try:
        profile = PlatformProfile.model_validate(raw)
    except ValidationError:
        profile_failed = True
    if profile_failed or profile is None:
        raise ExperimentConfigError("platform profile failed strict validation")

    definition = load_experiment(
        Path(profile.experiment_path),
        config_root=config_root,
        expected_hash=profile.expected_experiment_hash,
    )

    composition_failed = False
    platform_config: PlatformConfig | None = None
    try:
        experiment = ExperimentSpec(
            schema_version=1,
            definition=definition,
            definition_hash=profile.expected_experiment_hash,
            signal_provider=profile.signal_provider,
            execution_mode=profile.mode,
        )
        platform_config = PlatformConfig(profile=profile, experiment=experiment)
        _ = (
            profile.content_hash,
            experiment.content_hash,
            platform_config.content_hash,
        )
    except (CanonicalizationError, ValidationError):
        composition_failed = True
    if composition_failed or platform_config is None:
        raise ExperimentConfigError("platform configuration could not be safely composed")
    return platform_config


class RuntimeService(StrEnum):
    """Closed process identities used to enforce least-privilege startup settings."""

    MIGRATE = "migrate"
    CONTROL_API = "control-api"
    MARKET_DATA_WORKER = "market-data-worker"
    SCHEDULER_WORKER = "scheduler-worker"
    STRATEGY_WORKER = "strategy-worker"
    EXECUTION_WORKER = "execution-worker"
    DASHBOARD = "dashboard"
    MARKET_DATA_LIVE = "market-data-live"
    PAPER_EXECUTION_WORKER = "paper-execution-worker"


class PaperOrderEnablement(StrEnum):
    """Derived paper-order acknowledgement without retaining its raw environment value."""

    DISABLED = "disabled"
    ACKNOWLEDGED = "acknowledged"


_NONSECRET_RUNTIME_VARIABLES = (
    "AQA_CONFIG",
    "AQA_ARTIFACT_ROOT",
    "AQA_API_BASE_URL",
    "AQA_LOG_FORMAT",
    "AQA_ENABLE_PAPER_ORDERS",
    "AQA_API_DOCS_ENABLED",
)
_FORBIDDEN_ALPACA_VARIABLES = frozenset(
    {
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "APCA_API_BASE_URL",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
    }
)
_RUNTIME_DEFAULTS = {
    "AQA_CONFIG": "configs/platform/offline.yaml",
    "AQA_ARTIFACT_ROOT": "outputs/artifacts",
    "AQA_API_BASE_URL": "http://127.0.0.1:8000",
    "AQA_LOG_FORMAT": "json",
    "AQA_ENABLE_PAPER_ORDERS": "NO",
    "AQA_API_DOCS_ENABLED": "NO",
}
_SERVICE_SECRET_SOURCES = {
    RuntimeService.MIGRATE: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.CONTROL_API: frozenset(
        {SecretFileVariable.DATABASE_URL, SecretFileVariable.OPERATOR_TOKEN}
    ),
    RuntimeService.MARKET_DATA_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.SCHEDULER_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.STRATEGY_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.EXECUTION_WORKER: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.DASHBOARD: frozenset({SecretFileVariable.OPERATOR_TOKEN}),
    RuntimeService.MARKET_DATA_LIVE: frozenset(
        {
            SecretFileVariable.DATABASE_URL,
            SecretFileVariable.ALPACA_DATA_API_KEY,
            SecretFileVariable.ALPACA_DATA_SECRET_KEY,
        }
    ),
    RuntimeService.PAPER_EXECUTION_WORKER: frozenset(
        {
            SecretFileVariable.DATABASE_URL,
            SecretFileVariable.ALPACA_PAPER_API_KEY,
            SecretFileVariable.ALPACA_PAPER_SECRET_KEY,
            SecretFileVariable.PAPER_ACCOUNT_ID_HASH,
        }
    ),
}
_SERVICE_BASE_REQUIRED_SOURCES = {
    RuntimeService.MIGRATE: frozenset({SecretFileVariable.DATABASE_URL}),
    RuntimeService.CONTROL_API: frozenset({SecretFileVariable.OPERATOR_TOKEN}),
    RuntimeService.MARKET_DATA_WORKER: frozenset(),
    RuntimeService.SCHEDULER_WORKER: frozenset(),
    RuntimeService.STRATEGY_WORKER: frozenset(),
    RuntimeService.EXECUTION_WORKER: frozenset(),
    RuntimeService.DASHBOARD: frozenset({SecretFileVariable.OPERATOR_TOKEN}),
    RuntimeService.MARKET_DATA_LIVE: frozenset(
        {
            SecretFileVariable.ALPACA_DATA_API_KEY,
            SecretFileVariable.ALPACA_DATA_SECRET_KEY,
        }
    ),
    RuntimeService.PAPER_EXECUTION_WORKER: frozenset(
        {
            SecretFileVariable.ALPACA_PAPER_API_KEY,
            SecretFileVariable.ALPACA_PAPER_SECRET_KEY,
            SecretFileVariable.PAPER_ACCOUNT_ID_HASH,
        }
    ),
}
_SERVICE_MODES = {
    RuntimeService.MIGRATE: frozenset(ExecutionMode),
    RuntimeService.CONTROL_API: frozenset(ExecutionMode),
    RuntimeService.MARKET_DATA_WORKER: frozenset({ExecutionMode.OFFLINE}),
    RuntimeService.SCHEDULER_WORKER: frozenset(ExecutionMode),
    RuntimeService.STRATEGY_WORKER: frozenset(ExecutionMode),
    RuntimeService.EXECUTION_WORKER: frozenset({ExecutionMode.OFFLINE}),
    RuntimeService.DASHBOARD: frozenset(ExecutionMode),
    RuntimeService.MARKET_DATA_LIVE: frozenset({ExecutionMode.SHADOW, ExecutionMode.PAPER}),
    RuntimeService.PAPER_EXECUTION_WORKER: frozenset({ExecutionMode.PAPER}),
}
_SECRET_FIELD_BY_SOURCE = {
    SecretFileVariable.DATABASE_URL: "database_url_file",
    SecretFileVariable.OPERATOR_TOKEN: "operator_token_file",
    SecretFileVariable.ALPACA_DATA_API_KEY: "alpaca_data_api_key_file",
    SecretFileVariable.ALPACA_DATA_SECRET_KEY: "alpaca_data_secret_key_file",
    SecretFileVariable.ALPACA_PAPER_API_KEY: "alpaca_paper_api_key_file",
    SecretFileVariable.ALPACA_PAPER_SECRET_KEY: "alpaca_paper_secret_key_file",
    SecretFileVariable.PAPER_ACCOUNT_ID_HASH: "paper_account_id_hash_file",
}


def _runtime_config_path(value: object) -> str:
    failed = False
    validated = ""
    try:
        validated = _validate_relative_config_path(value, field_name="AQA_CONFIG")
    except ValueError:
        failed = True
    if failed:
        raise RuntimeSettingsError("AQA_CONFIG must be a canonical path beneath configs")
    if not validated.startswith("configs/") or validated == "configs/":
        raise RuntimeSettingsError("AQA_CONFIG must be a canonical path beneath configs")
    return validated


def _runtime_base_url(value: object) -> str:
    if type(value) is not str or not value or len(value) > 2_048:
        raise RuntimeSettingsError("AQA_API_BASE_URL must be a bounded HTTP origin")
    encoding_failed = False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        encoding_failed = True
        encoded = b""
    if (
        encoding_failed
        or not encoded
        or any(byte < 0x21 or byte == 0x7F for byte in encoded)
        or "\\" in value
    ):
        raise RuntimeSettingsError("AQA_API_BASE_URL must be a bounded HTTP origin")

    parse_failed = False
    scheme = ""
    hostname = ""
    port: int | None = None
    parsed = None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        parse_failed = True
    hostname_is_valid = (
        bool(hostname)
        and len(hostname) <= 253
        and all(
            _RUNTIME_HOST_LABEL_PATTERN.fullmatch(label) is not None
            for label in hostname.split(".")
        )
    )
    if (
        parse_failed
        or parsed is None
        or scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not hostname_is_valid
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise RuntimeSettingsError("AQA_API_BASE_URL must be a bounded HTTP origin")
    authority = hostname.lower() if port is None else f"{hostname.lower()}:{port}"
    if parsed.netloc.lower() != authority:
        raise RuntimeSettingsError("AQA_API_BASE_URL must be a bounded HTTP origin")
    return f"{scheme}://{authority}"


def _required_secret_sources(
    *,
    service: RuntimeService,
    platform: PlatformConfig,
) -> frozenset[SecretFileVariable]:
    required = set(_SERVICE_BASE_REQUIRED_SOURCES[service])
    database_allowed = SecretFileVariable.DATABASE_URL in _SERVICE_SECRET_SOURCES[service]
    if database_allowed and platform.profile.storage.database_required:
        required.add(SecretFileVariable.DATABASE_URL)
    return frozenset(required)


class RuntimeSettings(_StrictFrozenModel):
    """Validated, service-scoped startup configuration containing only opaque secret references."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        revalidate_instances="never",
        hide_input_in_errors=True,
    )

    platform: PlatformConfig
    service: RuntimeService
    config_path: str
    artifact_root: Path
    api_base_url: str
    log_format: Literal["json"]
    paper_order_enablement: PaperOrderEnablement
    api_docs_enabled: bool
    database_url_file: SecretFileReference | None = None
    operator_token_file: SecretFileReference | None = None
    alpaca_data_api_key_file: SecretFileReference | None = None
    alpaca_data_secret_key_file: SecretFileReference | None = None
    alpaca_paper_api_key_file: SecretFileReference | None = None
    alpaca_paper_secret_key_file: SecretFileReference | None = None
    paper_account_id_hash_file: SecretFileReference | None = None
    offline_database_path: Path | None = None

    def __init__(
        self,
        *,
        _construction_token: object | None = None,
        **data: Any,
    ) -> None:
        if _construction_token is not _RUNTIME_SETTINGS_CONSTRUCTION_TOKEN:
            raise TypeError("runtime settings must be created with load_runtime_settings")
        super().__init__(**data)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return this immutable instance and reject Pydantic's unvalidated update path."""

        del deep
        if update is not None:
            raise TypeError("runtime settings cannot be copied with updates")
        return self

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        del memo
        return self

    def __replace__(self, **changes: Any) -> Self:
        del changes
        raise TypeError("runtime settings cannot be replaced")

    def __getstate__(self) -> Never:
        raise TypeError("runtime settings cannot be serialized")

    def __setstate__(self, state: object) -> Never:
        del state
        raise TypeError("runtime settings cannot be deserialized")

    def __reduce__(self) -> Never:
        raise TypeError("runtime settings cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Never:
        del protocol
        raise TypeError("runtime settings cannot be serialized")

    @classmethod
    def model_construct(
        cls,
        _fields_set: set[str] | None = None,
        **values: Any,
    ) -> Self:
        del _fields_set, values
        raise TypeError("runtime settings cannot bypass validation")

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_: Any,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Publish an honest output-only schema for factory-composed settings."""

        if handler.mode == "validation":
            return {
                "not": {},
                "description": "Factory-created RuntimeSettings instance only; JSON input is rejected.",
            }
        rendered = dict(handler(core_schema_))
        rendered["readOnly"] = True
        return rendered

    @field_validator("service", mode="before")
    @classmethod
    def validate_service(cls, value: object) -> RuntimeService:
        if type(value) is not RuntimeService:
            raise ValueError("service must be selected by trusted startup code")
        return value

    @field_validator("config_path", mode="before")
    @classmethod
    def validate_config_path(cls, value: object) -> str:
        return _runtime_config_path(value)

    @field_validator("api_base_url", mode="before")
    @classmethod
    def validate_api_base_url(cls, value: object) -> str:
        return _runtime_base_url(value)

    @field_validator("artifact_root", "offline_database_path", mode="before")
    @classmethod
    def validate_runtime_paths(cls, value: object) -> Path | None:
        if value is None:
            return None
        if type(value) is not _CONCRETE_PATH_TYPE or not value.is_absolute():
            raise ValueError("runtime paths must be trusted absolute pathlib.Path values")
        return value

    @model_validator(mode="after")
    def validate_service_scope(self) -> Self:
        if self.platform.profile.mode not in _SERVICE_MODES[self.service]:
            raise ValueError("service is incompatible with the selected platform mode")

        references = {
            source: getattr(self, field_name)
            for source, field_name in _SECRET_FIELD_BY_SOURCE.items()
        }
        configured = frozenset(source for source, reference in references.items() if reference)
        allowed = _SERVICE_SECRET_SOURCES[self.service]
        required = _required_secret_sources(service=self.service, platform=self.platform)
        if not configured <= allowed:
            raise ValueError("service received a secret reference outside its authority")
        if not required <= configured:
            raise ValueError("service is missing a required secret reference")

        for source, reference in references.items():
            if reference is not None and reference.source is not source:
                raise ValueError("secret reference source does not match its settings field")

        uses_offline_fallback = (
            self.platform.profile.mode is ExecutionMode.OFFLINE
            and SecretFileVariable.DATABASE_URL in allowed
            and self.database_url_file is None
        )
        if uses_offline_fallback != (self.offline_database_path is not None):
            raise ValueError("offline database fallback does not match the storage selection")

        if self.paper_order_enablement is PaperOrderEnablement.ACKNOWLEDGED and (
            self.service is not RuntimeService.PAPER_EXECUTION_WORKER
            or self.platform.profile.mode is not ExecutionMode.PAPER
        ):
            raise ValueError("paper-order acknowledgement is outside this service authority")
        return self


def _runtime_environment_snapshot(
    environment: Mapping[str, str],
    *,
    service: RuntimeService,
) -> dict[str, str]:
    """Copy only allowlisted values once, without inspecting unrelated or forbidden values."""

    failed = False
    keys: tuple[object, ...] = ()
    snapshot: dict[str, str] = {}
    try:
        if not isinstance(environment, Mapping):
            failed = True
        else:
            collected_keys: list[object] = []
            for index, key in enumerate(iter(environment)):
                if index >= 4_096:
                    failed = True
                    break
                collected_keys.append(key)
            keys = tuple(collected_keys)
    except Exception:
        failed = True
    if failed or any(type(key) is not str for key in keys):
        raise RuntimeSettingsError("runtime environment could not be read safely")
    string_keys = cast(tuple[str, ...], keys)
    if len(set(string_keys)) != len(string_keys):
        raise RuntimeSettingsError("runtime environment could not be read safely")

    key_set = frozenset(string_keys)
    if key_set & _FORBIDDEN_ALPACA_VARIABLES:
        raise RuntimeSettingsError("forbidden generic Alpaca environment variable is present")
    known_variables = frozenset(_NONSECRET_RUNTIME_VARIABLES) | frozenset(
        source.value for source in SecretFileVariable
    )
    if any(key.startswith("AQA_") and key not in known_variables for key in string_keys):
        raise RuntimeSettingsError("unknown AQA runtime variable is present")

    allowed_sources = _SERVICE_SECRET_SOURCES[service]
    supplied_sources = frozenset(source for source in SecretFileVariable if source.value in key_set)
    if not supplied_sources <= allowed_sources:
        raise RuntimeSettingsError("service received a secret reference outside its authority")

    relevant_names = (
        *_NONSECRET_RUNTIME_VARIABLES,
        *(source.value for source in sorted(allowed_sources, key=lambda item: item.value)),
    )
    failed = False
    try:
        for name in relevant_names:
            if name not in key_set:
                continue
            value = environment[name]
            if type(value) is not str or len(value) > 65_536:
                failed = True
                break
            snapshot[name] = value
    except Exception:
        failed = True
    if failed:
        raise RuntimeSettingsError("runtime environment could not be read safely")
    return snapshot


def _validated_application_root(application_root: Path) -> tuple[Path, int]:
    if type(application_root) is not _CONCRETE_PATH_TYPE:
        raise RuntimeSettingsError("application_root must be an exact pathlib.Path")
    rendered = os.fspath(application_root)
    if (
        application_root.anchor != os.sep
        or rendered == os.sep
        or application_root.as_posix() != rendered
        or any(component in {"", ".", ".."} for component in application_root.parts[1:])
    ):
        raise RuntimeSettingsError("application_root must be a canonical absolute POSIX path")
    _runtime_path_parts(rendered, setting="application_root")

    descriptor = -1
    failed = False
    path_byte_limit = 0
    try:
        descriptor = _open_nonsymlink_directory(application_root)
        filesystem_limit = os.fpathconf(descriptor, "PC_PATH_MAX")
        if type(filesystem_limit) is not int or filesystem_limit <= 1:
            failed = True
        else:
            path_byte_limit = min(filesystem_limit - 1, _MAX_RUNTIME_PATH_BYTES - 1)
            if len(rendered.encode("utf-8")) > path_byte_limit:
                failed = True
    except (OSError, RuntimeError, TypeError, ValueError):
        failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        raise RuntimeSettingsError("application_root could not be validated safely")
    return application_root, path_byte_limit


def _runtime_path_parts(value: str, *, setting: str) -> tuple[bool, tuple[str, ...]]:
    if not value or "\x00" in value or "\\" in value:
        raise RuntimeSettingsError(f"{setting} must be a canonical POSIX path")
    encoding_failed = False
    encoded = b""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed or len(encoded) > _MAX_RUNTIME_PATH_BYTES:
        raise RuntimeSettingsError(f"{setting} must be bounded UTF-8")
    absolute = value.startswith("/")
    components = value.split("/")
    if absolute:
        components = components[1:]
    if not components or any(component in {"", ".", ".."} for component in components):
        raise RuntimeSettingsError(f"{setting} contains a prohibited path component")
    if any(
        len(component.encode("utf-8")) > _MAX_RUNTIME_PATH_COMPONENT_BYTES
        for component in components
    ):
        raise RuntimeSettingsError(f"{setting} contains an oversized path component")
    return absolute, tuple(components)


def _validate_existing_runtime_path(
    *,
    application_root: Path,
    parts: tuple[str, ...],
    leaf_kind: Literal["directory", "file"],
) -> None:
    root_descriptor = -1
    current_descriptor = -1
    failed = False
    try:
        root_descriptor = _open_nonsymlink_directory(application_root)
        current_descriptor = root_descriptor
        for index, component in enumerate(parts):
            is_leaf = index == len(parts) - 1
            try:
                component_status = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                break
            if stat.S_ISLNK(component_status.st_mode):
                failed = True
                break
            expected_directory = not is_leaf or leaf_kind == "directory"
            if expected_directory and not stat.S_ISDIR(component_status.st_mode):
                failed = True
                break
            if is_leaf and leaf_kind == "file" and not stat.S_ISREG(component_status.st_mode):
                failed = True
                break
            if stat.S_ISDIR(component_status.st_mode):
                next_descriptor = -1
                try:
                    next_descriptor = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=current_descriptor,
                    )
                    next_status = os.fstat(next_descriptor)
                    if (component_status.st_dev, component_status.st_ino) != (
                        next_status.st_dev,
                        next_status.st_ino,
                    ):
                        failed = True
                        break
                    if current_descriptor != root_descriptor:
                        os.close(current_descriptor)
                    current_descriptor = next_descriptor
                    next_descriptor = -1
                finally:
                    if next_descriptor >= 0:
                        try:
                            os.close(next_descriptor)
                        except OSError:
                            failed = True
    except (OSError, RuntimeError, TypeError, ValueError):
        failed = True
    finally:
        if current_descriptor >= 0 and current_descriptor != root_descriptor:
            try:
                os.close(current_descriptor)
            except OSError:
                failed = True
        if root_descriptor >= 0:
            try:
                os.close(root_descriptor)
            except OSError:
                failed = True
    if failed:
        raise RuntimeSettingsError("runtime path could not be validated safely")


def _contained_runtime_path(
    value: str,
    *,
    setting: str,
    application_root: Path,
    path_byte_limit: int,
    leaf_kind: Literal["directory", "file"],
) -> Path:
    absolute, components = _runtime_path_parts(value, setting=setting)
    root_parts = application_root.parts
    if absolute:
        candidate_parts = (os.sep, *components)
        if candidate_parts[: len(root_parts)] != root_parts:
            raise RuntimeSettingsError(f"{setting} must remain beneath application_root")
        relative_parts = candidate_parts[len(root_parts) :]
    else:
        relative_parts = components
    if not relative_parts:
        raise RuntimeSettingsError(f"{setting} cannot select application_root itself")
    candidate = application_root.joinpath(*relative_parts)
    if len(candidate.as_posix().encode("utf-8")) > path_byte_limit:
        raise RuntimeSettingsError(f"{setting} must remain within the path-size limit")
    _validate_existing_runtime_path(
        application_root=application_root,
        parts=relative_parts,
        leaf_kind=leaf_kind,
    )
    return candidate


def _paper_order_enablement(value: str) -> PaperOrderEnablement:
    if value == "NO":
        return PaperOrderEnablement.DISABLED
    if value == "I_ACKNOWLEDGE_AQA_PAPER_ONLY":
        return PaperOrderEnablement.ACKNOWLEDGED
    raise RuntimeSettingsError("AQA_ENABLE_PAPER_ORDERS is not an accepted acknowledgement")


def _api_docs_enabled(value: str) -> bool:
    if value == "NO":
        return False
    if value == "YES":
        return True
    raise RuntimeSettingsError("AQA_API_DOCS_ENABLED must be exact NO or YES")


def load_runtime_settings(
    environment: Mapping[str, str],
    *,
    service: RuntimeService,
    application_root: Path,
) -> RuntimeSettings:
    """Compose one process's settings without reading process state or secret-file contents."""

    if type(service) is not RuntimeService:
        raise RuntimeSettingsError("service must be selected by trusted startup code")
    root, path_byte_limit = _validated_application_root(application_root)
    snapshot = _runtime_environment_snapshot(environment, service=service)

    config_path = _runtime_config_path(snapshot.get("AQA_CONFIG", _RUNTIME_DEFAULTS["AQA_CONFIG"]))
    platform_failed = False
    platform: PlatformConfig | None = None
    try:
        platform = load_platform_config(
            Path(config_path.removeprefix("configs/")),
            config_root=root / "configs",
        )
    except ExperimentConfigError:
        platform_failed = True
    if platform_failed or platform is None:
        raise RuntimeSettingsError("runtime platform configuration is invalid")
    if platform.profile.mode not in _SERVICE_MODES[service]:
        raise RuntimeSettingsError("service is incompatible with the selected platform mode")

    artifact_root = _contained_runtime_path(
        snapshot.get("AQA_ARTIFACT_ROOT", _RUNTIME_DEFAULTS["AQA_ARTIFACT_ROOT"]),
        setting="AQA_ARTIFACT_ROOT",
        application_root=root,
        path_byte_limit=path_byte_limit,
        leaf_kind="directory",
    )
    api_base_url = _runtime_base_url(
        snapshot.get("AQA_API_BASE_URL", _RUNTIME_DEFAULTS["AQA_API_BASE_URL"])
    )
    log_format = snapshot.get("AQA_LOG_FORMAT", _RUNTIME_DEFAULTS["AQA_LOG_FORMAT"])
    if log_format != "json":
        raise RuntimeSettingsError("AQA_LOG_FORMAT must be exact json")
    paper_enablement = _paper_order_enablement(
        snapshot.get(
            "AQA_ENABLE_PAPER_ORDERS",
            _RUNTIME_DEFAULTS["AQA_ENABLE_PAPER_ORDERS"],
        )
    )
    docs_enabled = _api_docs_enabled(
        snapshot.get("AQA_API_DOCS_ENABLED", _RUNTIME_DEFAULTS["AQA_API_DOCS_ENABLED"])
    )

    reference_values: dict[str, SecretFileReference] = {}
    reference_failed = False
    for source in sorted(_SERVICE_SECRET_SOURCES[service], key=lambda item: item.value):
        supplied_path = snapshot.get(source.value)
        if supplied_path is None:
            continue
        try:
            reference_values[_SECRET_FIELD_BY_SOURCE[source]] = SecretFileReference.from_path(
                supplied_path,
                source=source,
                application_root=root,
            )
        except (TypeError, ValueError):
            reference_failed = True
            break
    if reference_failed:
        raise RuntimeSettingsError("service secret-file reference is invalid")

    required_sources = _required_secret_sources(service=service, platform=platform)
    configured_sources = frozenset(
        source for source in _SERVICE_SECRET_SOURCES[service] if source.value in snapshot
    )
    if not required_sources <= configured_sources:
        raise RuntimeSettingsError("service is missing a required secret reference")

    offline_database_path: Path | None = None
    if (
        platform.profile.mode is ExecutionMode.OFFLINE
        and SecretFileVariable.DATABASE_URL in _SERVICE_SECRET_SOURCES[service]
        and SecretFileVariable.DATABASE_URL not in configured_sources
    ):
        offline_database_path = _contained_runtime_path(
            "runtime/aqa-offline.sqlite3",
            setting="offline database path",
            application_root=root,
            path_byte_limit=path_byte_limit,
            leaf_kind="file",
        )

    validation_failed = False
    settings: RuntimeSettings | None = None
    try:
        settings = RuntimeSettings(
            _construction_token=_RUNTIME_SETTINGS_CONSTRUCTION_TOKEN,
            platform=platform,
            service=service,
            config_path=config_path,
            artifact_root=artifact_root,
            api_base_url=api_base_url,
            log_format=cast(Literal["json"], log_format),
            paper_order_enablement=paper_enablement,
            api_docs_enabled=docs_enabled,
            offline_database_path=offline_database_path,
            **reference_values,
        )
    except ValidationError:
        validation_failed = True
    if validation_failed or settings is None:
        raise RuntimeSettingsError("runtime settings failed strict validation")
    return settings
