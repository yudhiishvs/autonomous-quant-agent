"""Known-answer and rejection tests for canonical platform serialization."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext
from enum import Enum, IntEnum, StrEnum

import pytest

from adaptive_trader.platform import CanonicalizationError, canonical_json_bytes, sha256_hex


class Feed(StrEnum):
    IEX = "iex"


class Revision(IntEnum):
    SECOND = 2


def test_known_answer_bytes_and_hash_are_stable() -> None:
    value = {
        "price": Decimal("100.5000"),
        "levels": ("é", Revision.SECOND),
        "feed": Feed.IEX,
        "captured_at": datetime(
            2026,
            9,
            3,
            10,
            30,
            5,
            1200,
            tzinfo=timezone(timedelta(hours=-4)),
        ),
    }

    expected = (
        b'{"captured_at":"2026-09-03T14:30:05.001200Z","feed":"iex",'
        b'"levels":["\xc3\xa9",2],"price":"100.5"}'
    )
    assert canonical_json_bytes(value) == expected
    assert sha256_hex(value) == "34ab327fa7efe5ea2512e17fd38ffff70428ff1761468a4d2d2c6014bdc569a7"


def test_mapping_order_does_not_change_bytes_or_hash() -> None:
    first = {"z": 3, "a": 1, "middle": 2}
    second = {"middle": 2, "z": 3, "a": 1}

    assert canonical_json_bytes(first) == b'{"a":1,"middle":2,"z":3}'
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert sha256_hex(first) == sha256_hex(second)


def test_strings_are_utf8_and_output_has_no_insignificant_whitespace() -> None:
    encoded = canonical_json_bytes({"message": "café", "nested": [True, None]})

    assert encoded == b'{"message":"caf\xc3\xa9","nested":[true,null]}'
    assert b"\\u00e9" not in encoded
    assert b": " not in encoded
    assert b", " not in encoded


def test_control_escaping_and_unicode_key_order_are_stable() -> None:
    value = {"😀": "astral", "\ue000": 'line\n"'}

    assert canonical_json_bytes(value) == (
        b'{"\xee\x80\x80":"line\\n\\"","\xf0\x9f\x98\x80":"astral"}'
    )


def test_datetimes_are_utc_with_exactly_six_fractional_digits() -> None:
    assert canonical_json_bytes(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == (
        b'"2026-01-02T03:04:05.000000Z"'
    )
    assert (
        canonical_json_bytes(
            datetime(2026, 1, 1, 22, 4, 5, 6, tzinfo=timezone(timedelta(hours=-5)))
        )
        == b'"2026-01-02T03:04:05.000006Z"'
    )


def test_naive_datetime_is_rejected_with_structural_context() -> None:
    with pytest.raises(CanonicalizationError, match=r"naive datetime.*\$\[0\]"):
        canonical_json_bytes([datetime(2026, 1, 2, 3, 4, 5)])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.2300"), b'"1.23"'),
        (Decimal("1E+3"), b'"1000"'),
        (Decimal("1E-7"), b'"0.0000001"'),
        (Decimal("100.000"), b'"100"'),
        (Decimal("0.0100"), b'"0.01"'),
        (Decimal("-0.000"), b'"0"'),
        (Decimal("0E+1000000000"), b'"0"'),
        (Decimal("-0E-1000000000"), b'"0"'),
    ],
)
def test_decimals_are_plain_normalized_strings(value: Decimal, expected: bytes) -> None:
    assert canonical_json_bytes(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_nonfinite_numbers_are_rejected(value: float | Decimal) -> None:
    with pytest.raises(CanonicalizationError, match="nonfinite"):
        canonical_json_bytes(value)


def test_finite_float_is_encoded_as_a_json_number() -> None:
    assert canonical_json_bytes({"ratio": 0.5}) == b'{"ratio":0.5}'


def test_enum_values_and_tuple_order_are_preserved() -> None:
    class State(Enum):
        READY = "ready"

    assert canonical_json_bytes((State.READY, Revision.SECOND, "first")) == (b'["ready",2,"first"]')


@pytest.mark.parametrize("value", [{"A", "B"}, frozenset({"A", "B"})])
def test_unordered_collections_are_rejected(value: object) -> None:
    with pytest.raises(CanonicalizationError, match=r"unsupported value type at \$"):
        canonical_json_bytes(value)


def test_mapping_keys_must_be_exact_strings() -> None:
    with pytest.raises(CanonicalizationError, match=r"mapping-key type at \$<key>"):
        canonical_json_bytes({1: "value"})


@pytest.mark.parametrize("value", [b"bytes", bytearray(b"bytes"), object()])
def test_unsupported_objects_are_rejected(value: object) -> None:
    with pytest.raises(CanonicalizationError, match=r"unsupported value type at \$"):
        canonical_json_bytes(value)


def test_secret_like_wrappers_are_not_coerced_or_rendered() -> None:
    sentinel = "private-sentinel-that-must-not-appear"

    class RedactedSecret:
        def __str__(self) -> str:
            raise AssertionError("secret string conversion was called")

        def __repr__(self) -> str:
            raise AssertionError("secret representation was called")

    with pytest.raises(CanonicalizationError) as captured:
        canonical_json_bytes({sentinel: RedactedSecret()})

    message = str(captured.value)
    assert sentinel not in message
    assert "$<member:0>" in message
    _assert_safe_error(captured.value, sentinel=sentinel)


def test_primitive_and_container_subclasses_are_rejected() -> None:
    class SecretString(str):
        pass

    class CustomMapping(dict[str, object]):
        pass

    with pytest.raises(CanonicalizationError, match=r"unsupported value type at \$"):
        canonical_json_bytes(SecretString("hidden"))
    with pytest.raises(CanonicalizationError, match=r"unsupported value type at \$"):
        canonical_json_bytes(CustomMapping())


def test_direct_and_indirect_container_cycles_are_rejected() -> None:
    direct: list[object] = []
    direct.append(direct)
    with pytest.raises(CanonicalizationError, match=r"cycle.*\$\[0\]"):
        canonical_json_bytes(direct)

    indirect: dict[str, object] = {"items": []}
    items = indirect["items"]
    assert isinstance(items, list)
    items.append(indirect)
    with pytest.raises(CanonicalizationError, match=r"cycle.*\$<member:0>\[0\]"):
        canonical_json_bytes(indirect)


def test_repeated_noncyclic_references_are_allowed() -> None:
    shared = [Decimal("2.00")]

    assert canonical_json_bytes([shared, shared]) == b'[["2"],["2"]]'


def test_invalid_utf8_scalar_is_rejected_without_rendering_it() -> None:
    invalid = "\ud800"

    with pytest.raises(CanonicalizationError, match=r"invalid Unicode scalar at \$") as captured:
        canonical_json_bytes(invalid)
    assert invalid not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_decimal_normalization_is_independent_of_decimal_context() -> None:
    value = Decimal("12345678901234567890.1234500")

    with localcontext() as context:
        context.prec = 3
        encoded = canonical_json_bytes(value)

    assert encoded == b'"12345678901234567890.12345"'


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1])
def test_signed_64_bit_integer_boundaries_are_accepted(value: int) -> None:
    assert canonical_json_bytes(value) == str(value).encode("ascii")


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**63])
def test_integers_outside_the_fixed_range_are_rejected(value: int) -> None:
    with pytest.raises(CanonicalizationError, match=r"integer range limit exceeded at \$"):
        canonical_json_bytes(value)


def test_depth_limit_is_fixed_before_the_interpreter_recursion_limit() -> None:
    accepted: object = "leaf"
    for _ in range(32):
        accepted = [accepted]
    canonical_json_bytes(accepted)

    rejected: object = [accepted]
    with pytest.raises(CanonicalizationError, match="canonical nesting limit exceeded"):
        canonical_json_bytes(rejected)


def test_node_limit_is_fixed() -> None:
    canonical_json_bytes([None] * 4095)

    with pytest.raises(CanonicalizationError, match="canonical node limit exceeded"):
        canonical_json_bytes([None] * 4096)


def test_individual_and_total_text_limits_are_fixed() -> None:
    canonical_json_bytes("é" * 32768)

    with pytest.raises(CanonicalizationError, match="string length limit exceeded"):
        canonical_json_bytes("é" * 32769)
    with pytest.raises(CanonicalizationError, match="canonical text limit exceeded"):
        canonical_json_bytes(["a" * 65536] * 17)


def test_plain_decimal_length_is_bounded_before_formatting() -> None:
    assert canonical_json_bytes(Decimal("1E+1023")).startswith(b'"1000')

    with pytest.raises(CanonicalizationError, match="Decimal length limit exceeded"):
        canonical_json_bytes(Decimal("1E+1024"))


def test_float_spellings_are_an_explicit_hash_contract() -> None:
    assert canonical_json_bytes([-0.0, 1.0, 5e-324, 1e20]) == b"[-0.0,1.0,5e-324,1e+20]"


def test_enum_value_accessor_override_is_not_invoked() -> None:
    class HostileEnum(Enum):
        SAFE = "safe"

        @property
        def value(self) -> str:
            raise RuntimeError("enum-private-sentinel")

    assert canonical_json_bytes(HostileEnum.SAFE) == b'"safe"'


def test_enum_value_descriptor_override_is_not_invoked() -> None:
    sentinel = "enum-descriptor-private-sentinel"

    class HostileEnum(Enum):
        SAFE = "safe"

    def reveal_value(instance: HostileEnum) -> str:
        del instance
        raise RuntimeError(sentinel)

    type.__setattr__(HostileEnum, "_value_", property(reveal_value))

    assert canonical_json_bytes(HostileEnum.SAFE) == b'"safe"'


def test_enum_storage_descriptor_is_rejected_without_invocation() -> None:
    calls = 0

    class HostileEnum(Enum):
        SAFE = "safe"

        @property
        def __dict__(self) -> dict[str, object]:  # type: ignore[override]
            nonlocal calls
            calls += 1
            raise RuntimeError("enum-storage-private-sentinel")

    calls = 0
    with pytest.raises(CanonicalizationError, match="invalid enum storage"):
        canonical_json_bytes(HostileEnum.SAFE)
    assert calls == 0


def test_custom_timezone_is_rejected_without_invoking_it() -> None:
    sentinel = "timezone-private-sentinel"
    calls = 0

    class BrokenTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta | None:
            nonlocal calls
            del value
            calls += 1
            raise RuntimeError(sentinel)

        def dst(self, value: datetime | None) -> timedelta | None:
            del value
            return None

        def tzname(self, value: datetime | None) -> str | None:
            del value
            return "broken"

    value = datetime(2026, 1, 2, tzinfo=BrokenTimezone())
    with pytest.raises(CanonicalizationError, match="unsupported datetime timezone") as captured:
        canonical_json_bytes(value)

    assert calls == 0
    _assert_safe_error(captured.value, sentinel=sentinel)


def test_unsupported_dynamic_type_name_is_not_disclosed() -> None:
    sentinel = "type-name-private-sentinel"
    value = type(sentinel, (), {})()

    with pytest.raises(CanonicalizationError) as captured:
        canonical_json_bytes(value)

    _assert_safe_error(captured.value, sentinel=sentinel)


def _assert_safe_error(error: CanonicalizationError, *, sentinel: str) -> None:
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            "".join(traceback.format_exception(error)),
        )
    )
    assert sentinel not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None
