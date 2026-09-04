"""Strict, immutable experiment configuration loading."""

from __future__ import annotations

import hmac
import os
import re
import stat
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Self, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
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
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.universe import UniverseSpec, _normalize_symbol_tuple

_MAX_CONFIG_BYTES = 65_536
_MAX_YAML_DEPTH = 32
_MAX_YAML_NODES = 2_048
_MAX_VERSION = 2**63 - 1
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", flags=re.ASCII)
_TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$", flags=re.ASCII)
_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", flags=re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_YAML_INTEGER_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$", flags=re.ASCII)


class ExperimentConfigError(ValueError):
    """Raised when an experiment file cannot be safely loaded and validated."""


class ExperimentHashMismatchError(ExperimentConfigError):
    """Raised when a validated experiment does not match its pinned digest."""


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
    version: int = Field(ge=1, le=_MAX_VERSION)
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
    experiment_version: int = Field(ge=1, le=_MAX_VERSION)
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
        root_fd = os.open(
            safe_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
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
