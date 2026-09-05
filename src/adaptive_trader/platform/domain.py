"""Pure deterministic identity, time, and numeric domain boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DecimalException,
    localcontext,
)
from enum import StrEnum

from adaptive_trader.platform.constants import (
    MAX_DETERMINISTIC_ID_PREFIX_LENGTH,
    MAX_DOMAIN_DECIMAL_DIGITS,
    MAX_DOMAIN_DECIMAL_EXPONENT,
    MIN_DOMAIN_DECIMAL_EXPONENT,
    SHA256_HEX_LENGTH,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

_ID_PREFIX_PATTERN = re.compile(
    rf"^[a-z][a-z0-9_]{{0,{MAX_DETERMINISTIC_ID_PREFIX_LENGTH - 1}}}$",
    flags=re.ASCII,
)
_SHA256_PATTERN = re.compile(rf"^[0-9a-f]{{{SHA256_HEX_LENGTH}}}$", flags=re.ASCII)
_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$", flags=re.ASCII)


class DecimalRounding(StrEnum):
    """Closed decimal rounding modes accepted by platform quantization."""

    CEILING = ROUND_CEILING
    DOWN = ROUND_DOWN
    FLOOR = ROUND_FLOOR
    HALF_DOWN = ROUND_HALF_DOWN
    HALF_EVEN = ROUND_HALF_EVEN
    HALF_UP = ROUND_HALF_UP
    UP = ROUND_UP
    ZERO_FIVE_UP = ROUND_05UP


@dataclass(frozen=True, slots=True)
class DeterministicId:
    """A bounded identifier rendered as ``<prefix>_<canonical SHA-256>``.

    ``from_hash_input`` hashes the complete supplied value with ``sha256_hex``. Public domain
    contracts must still document which of their fields form that hash input.
    """

    prefix: str
    digest: str

    def __post_init__(self) -> None:
        _validate_id_prefix(self.prefix)
        if type(self.digest) is not str or _SHA256_PATTERN.fullmatch(self.digest) is None:
            raise DomainValidationError("deterministic ID digest must be lowercase SHA-256")

    @classmethod
    def from_hash_input(cls, *, prefix: str, hash_input: object) -> DeterministicId:
        """Create an ID from the platform's canonical serialization and SHA-256 boundary."""

        _validate_id_prefix(prefix)
        return cls(prefix=prefix, digest=sha256_hex(hash_input))

    @property
    def value(self) -> str:
        """Return the stable storage and wire representation."""

        return f"{self.prefix}_{self.digest}"

    def __str__(self) -> str:
        return self.value


def require_utc_instant(value: object, *, field_name: str) -> datetime:
    """Return a UTC-normalized datetime or reject a naive/non-UTC public instant."""

    label = _validate_field_name(field_name)
    if type(value) is not datetime:
        raise DomainValidationError(f"{label} must be a datetime")
    if value.tzinfo is UTC:
        return value
    try:
        offset = value.utcoffset()
    except Exception:
        raise DomainValidationError(f"{label} must be timezone-aware UTC") from None
    if offset != timedelta(0):
        raise DomainValidationError(f"{label} must be timezone-aware UTC")
    return value.replace(tzinfo=UTC)


def require_finite_decimal(value: object, *, field_name: str) -> Decimal:
    """Return a bounded exact ``Decimal`` without coercing floats or secret-like wrappers."""

    label = _validate_field_name(field_name)
    if type(value) is not Decimal:
        raise DomainValidationError(f"{label} must be an exact Decimal")
    if not value.is_finite():
        raise DomainValidationError(f"{label} must be finite")

    _, digits, exponent = value.as_tuple()
    if (
        len(digits) > MAX_DOMAIN_DECIMAL_DIGITS
        or not isinstance(exponent, int)
        or not MIN_DOMAIN_DECIMAL_EXPONENT <= exponent <= MAX_DOMAIN_DECIMAL_EXPONENT
    ):
        raise DomainValidationError(f"{label} exceeds the decimal representation limit")
    return value


def quantize_decimal(
    value: object,
    *,
    quantum: object,
    rounding: DecimalRounding,
    field_name: str,
) -> Decimal:
    """Round to a multiple of an explicit positive quantum with an explicit mode."""

    number = require_finite_decimal(value, field_name=field_name)
    unit = require_finite_decimal(quantum, field_name="quantum")
    if unit <= 0:
        raise DomainValidationError("quantum must be positive")
    if type(rounding) is not DecimalRounding:
        raise DomainValidationError("rounding must be a DecimalRounding member")

    try:
        arithmetic_context = Context(prec=(MAX_DOMAIN_DECIMAL_DIGITS * 4) + 4)
        with localcontext(arithmetic_context):
            # The accepted representation is bounded, so a fixed local precision makes the
            # outcome independent of process-global Decimal context without unbounded work.
            unit_count = (number / unit).quantize(Decimal(1), rounding=rounding.value)
            result = unit_count * unit
    except DecimalException:
        raise DomainValidationError(f"{field_name} could not be quantized") from None
    if result.is_zero():
        result = result.copy_abs()
    return require_finite_decimal(result, field_name=field_name)


def _validate_id_prefix(prefix: object) -> str:
    if type(prefix) is not str or _ID_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise DomainValidationError("deterministic ID prefix is invalid")
    return prefix


def _validate_field_name(field_name: object) -> str:
    if type(field_name) is not str or _FIELD_NAME_PATTERN.fullmatch(field_name) is None:
        raise DomainValidationError("domain field name is invalid")
    return field_name
