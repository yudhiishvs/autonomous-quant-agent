"""Known-answer and rejection tests for generic platform domain primitives."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, Inexact, localcontext
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter

from adaptive_trader.platform.canonical import CanonicalizationError as CanonicalCompatibilityError
from adaptive_trader.platform.config import (
    ExperimentConfigError as ExperimentCompatibilityError,
)
from adaptive_trader.platform.config import (
    ExperimentHashMismatchError as HashCompatibilityError,
)
from adaptive_trader.platform.config import RuntimeSettingsError as RuntimeCompatibilityError
from adaptive_trader.platform.domain import (
    DecimalRounding,
    DeterministicId,
    quantize_decimal,
    require_finite_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import (
    CanonicalizationError,
    DomainValidationError,
    ExperimentConfigError,
    ExperimentHashMismatchError,
    LocalSecretBootstrapError,
    RuntimeSettingsError,
    SecretFileError,
)
from adaptive_trader.platform.security import (
    LocalSecretBootstrapError as BootstrapCompatibilityError,
)
from adaptive_trader.platform.security import SecretFileError as SecretCompatibilityError


def test_deterministic_id_has_known_canonical_value() -> None:
    identity = DeterministicId.from_hash_input(
        prefix="decision_slot",
        hash_input={"instrument": "ABC", "slot": 1},
    )

    assert identity.digest == "46aea361284149c11442b83748632892f9967b90cc707cac10d777ac277bbc91"
    assert identity.value == f"decision_slot_{identity.digest}"
    assert str(identity) == identity.value
    assert identity == DeterministicId.from_hash_input(
        prefix="decision_slot",
        hash_input={"slot": 1, "instrument": "ABC"},
    )


@pytest.mark.parametrize(
    ("prefix", "digest"),
    [
        ("Decision", "0" * 64),
        ("1decision", "0" * 64),
        ("decision-slot", "0" * 64),
        ("d" * 33, "0" * 64),
        ("decision", "A" * 64),
        ("decision", "0" * 63),
        ("decision", "g" * 64),
    ],
)
def test_deterministic_id_rejects_noncanonical_parts(prefix: str, digest: str) -> None:
    with pytest.raises(DomainValidationError):
        DeterministicId(prefix=prefix, digest=digest)


def test_deterministic_id_is_frozen_and_slotted() -> None:
    identity = DeterministicId(prefix="bar", digest="0" * 64)

    with pytest.raises(FrozenInstanceError):
        identity.prefix = "event"
    with pytest.raises(AttributeError):
        _ = identity.__dict__


def test_deterministic_id_preserves_canonical_input_rejection() -> None:
    with pytest.raises(CanonicalizationError, match="unsupported value type"):
        DeterministicId.from_hash_input(prefix="bar", hash_input={"instruments": {"ABC"}})


def test_deterministic_id_validates_prefix_before_hash_input() -> None:
    with pytest.raises(DomainValidationError, match="prefix"):
        DeterministicId.from_hash_input(prefix="Invalid", hash_input={"unsupported": {"ABC"}})


def test_utc_instant_accepts_exact_utc() -> None:
    instant = datetime(2026, 9, 5, 12, 30, 45, 123456, tzinfo=UTC)

    normalized = require_utc_instant(instant, field_name="observed_at")

    assert normalized is instant


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 9, 5, 12, 30, tzinfo=ZoneInfo("UTC")),
        TypeAdapter(datetime).validate_python("2026-09-05T12:30:00Z"),
        datetime(2026, 9, 5, 12, 30, tzinfo=timezone(timedelta(0), "GMT")),
    ],
)
def test_utc_instant_normalizes_semantic_utc_values(instant: datetime) -> None:
    normalized = require_utc_instant(instant, field_name="observed_at")

    assert normalized == datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
    assert normalized.tzinfo is UTC


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-05T12:30:45Z",
        datetime(2026, 9, 5, 12, 30, 45),
        datetime(2026, 9, 5, 8, 30, 45, tzinfo=timezone(timedelta(hours=-4))),
    ],
)
def test_utc_instant_rejects_non_datetime_naive_and_non_utc_values(value: object) -> None:
    with pytest.raises(DomainValidationError, match="observed_at"):
        require_utc_instant(value, field_name="observed_at")


def test_utc_instant_fails_closed_for_hostile_timezone() -> None:
    class HostileTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> timedelta | None:
            del value
            raise RuntimeError("private-provider-detail")

        def dst(self, value: datetime | None) -> timedelta | None:
            del value
            return None

    value = datetime(2026, 9, 5, 12, 30, tzinfo=HostileTimezone())

    with pytest.raises(DomainValidationError, match="captured_at") as captured:
        require_utc_instant(value, field_name="captured_at")

    assert "private-provider-detail" not in str(captured.value)


@pytest.mark.parametrize("value", [0, 1.5, "1.5", True, object()])
def test_decimal_boundary_does_not_coerce_other_types(value: object) -> None:
    with pytest.raises(DomainValidationError, match="price must be an exact Decimal"):
        require_finite_decimal(value, field_name="price")


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1" * 65),
        Decimal("1E+65"),
        Decimal("1E-65"),
    ],
)
def test_decimal_boundary_rejects_nonfinite_or_unbounded_values(value: Decimal) -> None:
    with pytest.raises(DomainValidationError):
        require_finite_decimal(value, field_name="notional")


def test_decimal_boundary_preserves_exact_finite_value() -> None:
    value = Decimal("-123456789.012345")

    assert require_finite_decimal(value, field_name="notional") is value


@pytest.mark.parametrize("field_name", ["", "has space", "field\nname", "x" * 65])
def test_domain_boundaries_reject_unsafe_field_labels(field_name: str) -> None:
    with pytest.raises(DomainValidationError, match="field name is invalid"):
        require_finite_decimal(Decimal("1"), field_name=field_name)


@pytest.mark.parametrize(
    ("rounding", "expected"),
    [
        (DecimalRounding.DOWN, Decimal("1.23")),
        (DecimalRounding.UP, Decimal("1.24")),
        (DecimalRounding.HALF_EVEN, Decimal("1.24")),
    ],
)
def test_quantization_requires_and_applies_explicit_rounding(
    rounding: DecimalRounding,
    expected: Decimal,
) -> None:
    assert (
        quantize_decimal(
            Decimal("1.235"),
            quantum=Decimal("0.01"),
            rounding=rounding,
            field_name="price",
        )
        == expected
    )


@pytest.mark.parametrize(
    ("rounding", "expected"),
    [
        (DecimalRounding.DOWN, Decimal("1.00")),
        (DecimalRounding.UP, Decimal("1.05")),
        (DecimalRounding.HALF_EVEN, Decimal("1.05")),
    ],
)
def test_quantization_rounds_to_true_quantum_multiples(
    rounding: DecimalRounding,
    expected: Decimal,
) -> None:
    result = quantize_decimal(
        Decimal("1.03"),
        quantum=Decimal("0.05"),
        rounding=rounding,
        field_name="price",
    )

    assert result == expected
    assert result % Decimal("0.05") == 0


def test_quantization_is_independent_of_process_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.traps[Inexact] = True
        result = quantize_decimal(
            Decimal("123456789.125"),
            quantum=Decimal("0.01"),
            rounding=DecimalRounding.HALF_EVEN,
            field_name="price",
        )

    assert result == Decimal("123456789.12")


def test_quantization_normalizes_signed_zero() -> None:
    result = quantize_decimal(
        Decimal("-0.001"),
        quantum=Decimal("0.01"),
        rounding=DecimalRounding.DOWN,
        field_name="quantity",
    )

    assert result == Decimal("0.00")
    assert not result.is_signed()


@pytest.mark.parametrize("quantum", [Decimal("0"), Decimal("-0.01")])
def test_quantization_rejects_nonpositive_quantum(quantum: Decimal) -> None:
    with pytest.raises(DomainValidationError, match="quantum must be positive"):
        quantize_decimal(
            Decimal("1.23"),
            quantum=quantum,
            rounding=DecimalRounding.DOWN,
            field_name="price",
        )


def test_quantization_rejects_untyped_rounding_mode() -> None:
    with pytest.raises(DomainValidationError, match="rounding"):
        quantize_decimal(
            Decimal("1.23"),
            quantum=Decimal("0.01"),
            rounding="ROUND_DOWN",  # type: ignore[arg-type]
            field_name="price",
        )


def test_quantization_rejects_result_outside_the_domain_representation_bound() -> None:
    with pytest.raises(DomainValidationError, match="representation limit"):
        quantize_decimal(
            Decimal(f"{'9' * 64}E+64"),
            quantum=Decimal("1"),
            rounding=DecimalRounding.DOWN,
            field_name="notional",
        )


def test_central_error_imports_preserve_existing_public_imports() -> None:
    assert CanonicalCompatibilityError is CanonicalizationError
    assert ExperimentCompatibilityError is ExperimentConfigError
    assert HashCompatibilityError is ExperimentHashMismatchError
    assert RuntimeCompatibilityError is RuntimeSettingsError
    assert SecretCompatibilityError is SecretFileError
    assert BootstrapCompatibilityError is LocalSecretBootstrapError
