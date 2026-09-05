"""Fixed bounds shared by deterministic platform domain contracts."""

from __future__ import annotations

from typing import Final

MIN_SIGNED_64_BIT_INTEGER: Final = -(2**63)
MAX_SIGNED_64_BIT_INTEGER: Final = 2**63 - 1
SHA256_HEX_LENGTH: Final = 64

MAX_DETERMINISTIC_ID_PREFIX_LENGTH: Final = 32
MAX_DOMAIN_DECIMAL_DIGITS: Final = 64
MIN_DOMAIN_DECIMAL_EXPONENT: Final = -64
MAX_DOMAIN_DECIMAL_EXPONENT: Final = 64
