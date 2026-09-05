"""Canonical JSON encoding for replayable identities and content hashes."""

from __future__ import annotations

import json
import math
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import TypeAlias, cast

from adaptive_trader.platform.constants import (
    MAX_SIGNED_64_BIT_INTEGER,
    MIN_SIGNED_64_BIT_INTEGER,
)
from adaptive_trader.platform.errors import CanonicalizationError

JsonScalar: TypeAlias = bool | int | float | str | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_MAX_DEPTH = 32
_MAX_NODES = 4096
_MAX_STRING_BYTES = 65_536
_MAX_TEXT_BYTES = 1_048_576
_MAX_DECIMAL_CHARACTERS = 1024
_MAX_OUTPUT_BYTES = 8_388_608


@dataclass(slots=True)
class _Budget:
    nodes: int = 0
    text_bytes: int = 0

    def consume_node(self, *, depth: int, path: str) -> None:
        if depth > _MAX_DEPTH:
            raise CanonicalizationError(f"canonical nesting limit exceeded at {path}")
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            raise CanonicalizationError(f"canonical node limit exceeded at {path}")

    def consume_text(self, size: int, *, path: str) -> None:
        self.text_bytes += size
        if self.text_bytes > _MAX_TEXT_BYTES:
            raise CanonicalizationError(f"canonical text limit exceeded at {path}")


def canonical_json_bytes(value: object) -> bytes:
    """Return bounded canonical UTF-8 JSON for a closed set of safe types.

    Error paths use structural positions rather than mapping-key contents so malformed input
    cannot disclose a value through an exception message. Callers must not mutate accepted lists
    or dictionaries while this function is encoding them. Integers are signed 64-bit. Inputs are
    limited to 32 levels, 4,096 value nodes, 65,536 UTF-8 bytes per string, 1 MiB total text, 1,024
    plain-decimal characters, and 8 MiB of encoded output.
    """

    normalization_failed = False
    try:
        normalized = _normalize(
            value,
            path="$",
            depth=0,
            active_containers=set(),
            budget=_Budget(),
        )
    except CanonicalizationError:
        raise
    except (RecursionError, RuntimeError, TypeError, UnicodeError, ValueError):
        normalization_failed = True
        normalized = None
    if normalization_failed:
        raise CanonicalizationError("canonical normalization failed at $")

    rendering_failed = False
    try:
        rendered = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = rendered.encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError):
        rendering_failed = True
        encoded = b""
    if rendering_failed:
        raise CanonicalizationError("canonical rendering failed at $")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        raise CanonicalizationError("canonical output limit exceeded at $")
    return encoded


def _normalize(
    value: object,
    *,
    path: str,
    depth: int,
    active_containers: set[int],
    budget: _Budget,
) -> JsonValue:
    budget.consume_node(depth=depth, path=path)

    if isinstance(value, Enum):
        enum_value = _enum_value(value, path=path)
        return _normalize(
            enum_value,
            path=f"{path}<enum-value>",
            depth=depth + 1,
            active_containers=active_containers,
            budget=budget,
        )

    value_type = type(value)
    if value is None or value_type is bool:
        return cast(JsonScalar, value)

    if value_type is int:
        integer = cast(int, value)
        if not MIN_SIGNED_64_BIT_INTEGER <= integer <= MAX_SIGNED_64_BIT_INTEGER:
            raise CanonicalizationError(f"integer range limit exceeded at {path}")
        return integer

    if value_type is str:
        text = cast(str, value)
        _consume_string(text, path=path, budget=budget)
        return text

    if value_type is float:
        float_number = cast(float, value)
        if not math.isfinite(float_number):
            raise CanonicalizationError(f"nonfinite float is prohibited at {path}")
        return float_number

    if value_type is Decimal:
        decimal_number = cast(Decimal, value)
        if not decimal_number.is_finite():
            raise CanonicalizationError(f"nonfinite Decimal is prohibited at {path}")
        rendered_decimal = _decimal_text(decimal_number, path=path)
        budget.consume_text(len(rendered_decimal), path=path)
        return rendered_decimal

    if value_type is datetime:
        instant = cast(datetime, value)
        rendered_instant = _datetime_text(instant, path=path)
        budget.consume_text(len(rendered_instant), path=path)
        return rendered_instant

    if value_type is list or value_type is tuple:
        sequence = cast(list[object] | tuple[object, ...], value)
        return _normalize_sequence(
            sequence,
            path=path,
            depth=depth,
            active_containers=active_containers,
            budget=budget,
        )

    if value_type is dict:
        mapping = cast(dict[object, object], value)
        return _normalize_mapping(
            mapping,
            path=path,
            depth=depth,
            active_containers=active_containers,
            budget=budget,
        )

    raise CanonicalizationError(f"unsupported value type at {path}")


def _normalize_sequence(
    value: list[object] | tuple[object, ...],
    *,
    path: str,
    depth: int,
    active_containers: set[int],
    budget: _Budget,
) -> list[JsonValue]:
    identity = id(value)
    _enter_container(identity, path=path, active_containers=active_containers)
    try:
        return [
            _normalize(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                active_containers=active_containers,
                budget=budget,
            )
            for index, item in enumerate(value)
        ]
    finally:
        active_containers.remove(identity)


def _normalize_mapping(
    value: dict[object, object],
    *,
    path: str,
    depth: int,
    active_containers: set[int],
    budget: _Budget,
) -> dict[str, JsonValue]:
    identity = id(value)
    _enter_container(identity, path=path, active_containers=active_containers)
    try:
        for key in value:
            if type(key) is not str:
                raise CanonicalizationError(f"unsupported mapping-key type at {path}<key>")
            _consume_string(key, path=f"{path}<key>", budget=budget)

        string_mapping = cast(dict[str, object], value)
        return {
            key: _normalize(
                string_mapping[key],
                path=f"{path}<member:{index}>",
                depth=depth + 1,
                active_containers=active_containers,
                budget=budget,
            )
            for index, key in enumerate(sorted(string_mapping))
        }
    finally:
        active_containers.remove(identity)


def _enter_container(identity: int, *, path: str, active_containers: set[int]) -> None:
    if identity in active_containers:
        raise CanonicalizationError(f"container cycle is prohibited at {path}")
    active_containers.add(identity)


def _decimal_text(value: Decimal, *, path: str) -> str:
    if value.is_zero():
        return "0"

    sign, digits, exponent = value.as_tuple()
    decimal_exponent = cast(int, exponent)
    digit_count = max(1, len(digits))
    if decimal_exponent >= 0:
        maximum_characters = digit_count + decimal_exponent
    elif digit_count + decimal_exponent > 0:
        maximum_characters = digit_count + 1
    else:
        maximum_characters = 2 - decimal_exponent
    maximum_characters += sign
    if maximum_characters > _MAX_DECIMAL_CHARACTERS:
        raise CanonicalizationError(f"Decimal length limit exceeded at {path}")

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered == "-0":
        return "0"
    return rendered


def _consume_string(value: str, *, path: str, budget: _Budget) -> None:
    string_failed = False
    if len(value) > _MAX_STRING_BYTES:
        raise CanonicalizationError(f"string length limit exceeded at {path}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        string_failed = True
        encoded = b""
    if string_failed:
        raise CanonicalizationError(f"invalid Unicode scalar at {path}")
    if len(encoded) > _MAX_STRING_BYTES:
        raise CanonicalizationError(f"string length limit exceeded at {path}")
    budget.consume_text(len(encoded), path=path)


def _enum_value(value: Enum, *, path: str) -> object:
    member_type = type(value)
    for member_class in type.__getattribute__(member_type, "__mro__"):
        if member_class is Enum:
            break
        namespace = type.__getattribute__(member_class, "__dict__")
        storage_descriptor = namespace.get("__dict__")
        if (
            storage_descriptor is not None
            and type(storage_descriptor) is not types.GetSetDescriptorType
        ):
            raise CanonicalizationError(f"invalid enum storage at {path}")

    value_failed = False
    try:
        member_storage = object.__getattribute__(value, "__dict__")
        if type(member_storage) is not dict:
            value_failed = True
            enum_value = None
        else:
            enum_value = dict.__getitem__(member_storage, "_value_")
    except Exception:
        value_failed = True
        enum_value = None
    if value_failed:
        raise CanonicalizationError(f"invalid enum value at {path}")
    return enum_value


def _datetime_text(value: datetime, *, path: str) -> str:
    timezone_info = value.tzinfo
    if timezone_info is None:
        raise CanonicalizationError(f"naive datetime is prohibited at {path}")
    if type(timezone_info) is not timezone:
        raise CanonicalizationError(f"unsupported datetime timezone at {path}")

    conversion_failed = False
    try:
        utc_value = value.astimezone(UTC)
    except Exception:
        conversion_failed = True
        utc_value = None
    if conversion_failed or utc_value is None:
        raise CanonicalizationError(f"invalid datetime timezone at {path}")
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
