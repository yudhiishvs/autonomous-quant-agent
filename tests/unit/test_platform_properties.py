"""Seeded bounded property and malformed-input smoke tests without extra dependencies."""

from __future__ import annotations

import json
from random import Random

import pytest

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.errors import CanonicalizationError


def _json_tree(generator: Random, depth: int = 0) -> object:
    scalars = [None, True, False, generator.randint(-(2**63), 2**63 - 1)]
    scalars.append("".join(generator.choices("abc\x00\n漢é😀", k=generator.randrange(24))))
    if depth == 4 or generator.randrange(3) == 0:
        return generator.choice(scalars)
    values = [_json_tree(generator, depth + 1) for _ in range(generator.randrange(6))]
    if generator.choice((True, False)):
        return values
    return {f"key_{index}": value for index, value in enumerate(values)}


def _reverse_mappings(value: object) -> object:
    if isinstance(value, dict):
        return {key: _reverse_mappings(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_mappings(item) for item in value]
    return value


def test_seeded_json_trees_preserve_values_and_canonical_identity() -> None:
    generator = Random(2026090601)
    for _ in range(256):
        value = _json_tree(generator)
        encoded = canonical_json_bytes(value)
        decoded = json.loads(encoded)
        assert decoded == value
        assert canonical_json_bytes(decoded) == encoded
        assert canonical_json_bytes(_reverse_mappings(value)) == encoded


def test_seeded_malformed_nested_values_fail_with_redacted_typed_errors() -> None:
    generator = Random(2026090602)
    secret_marker = "DO_NOT_DISCLOSE_FUZZ_VALUE"  # pragma: allowlist secret
    for index in range(160):
        malformed: object = generator.choice(
            [float("nan"), float("inf"), -(2**63) - 1, 2**63, b"bytes", "\ud800", {1, 2}]
        )
        for depth in range(generator.randrange(8)):
            malformed = (
                [index, malformed]
                if generator.choice((True, False))
                else {f"{secret_marker}_{depth}": malformed}
            )
        with pytest.raises(CanonicalizationError) as first:
            canonical_json_bytes(malformed)
        with pytest.raises(CanonicalizationError) as repeated:
            canonical_json_bytes(malformed)
        assert str(first.value) == str(repeated.value)
        assert secret_marker not in str(first.value)


def test_seeded_nesting_budget_rejects_before_python_recursion_limit() -> None:
    generator = Random(2026090603)
    for depth in generator.sample(range(24, 65), 30):
        value: object = 0
        for _ in range(depth):
            value = [value] if generator.choice((True, False)) else {"child": value}
        if depth <= 32:
            assert json.loads(canonical_json_bytes(value)) == value
        else:
            with pytest.raises(CanonicalizationError, match="nesting limit"):
                canonical_json_bytes(value)


def test_seeded_node_budget_is_independent_of_container_shape() -> None:
    generator = Random(2026090604)
    for count in generator.sample(range(4080, 4110), 20):
        value = [generator.randrange(100) for _ in range(count)]
        if count <= 4095:
            assert json.loads(canonical_json_bytes(value)) == value
        else:
            with pytest.raises(CanonicalizationError, match="node limit"):
                canonical_json_bytes(value)
