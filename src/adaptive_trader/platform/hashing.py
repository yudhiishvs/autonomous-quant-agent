"""Content hashing over the platform's canonical serialization boundary."""

from __future__ import annotations

import hashlib

from adaptive_trader.platform.canonical import canonical_json_bytes


def sha256_hex(value: object) -> str:
    """Return lowercase hexadecimal SHA-256 of canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
