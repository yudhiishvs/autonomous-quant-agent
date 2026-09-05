"""Credentials dedicated to read-only Alpaca market-data collection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from adaptive_trader.platform.security import SecretFileVariable, load_secret_file

ALPACA_DATA_API_KEY_ENV = "APA_ALPACA_DATA_API_KEY"
ALPACA_DATA_SECRET_KEY_ENV = "APA_ALPACA_DATA_SECRET_KEY"
ALPACA_DATA_API_KEY_FILE_ENV = SecretFileVariable.ALPACA_DATA_API_KEY.value
ALPACA_DATA_SECRET_KEY_FILE_ENV = SecretFileVariable.ALPACA_DATA_SECRET_KEY.value


class AlpacaDataCredentialError(ValueError):
    """Raised when the dedicated market-data credentials are unavailable."""


@dataclass(frozen=True, slots=True, repr=False)
class AlpacaDataCredentials:
    """A credential pair whose representation never contains secret material."""

    api_key: str
    secret_key: str

    def __post_init__(self) -> None:
        api_key = self.api_key.strip() if isinstance(self.api_key, str) else ""
        secret_key = self.secret_key.strip() if isinstance(self.secret_key, str) else ""
        if not api_key:
            raise AlpacaDataCredentialError(f"{ALPACA_DATA_API_KEY_ENV} must be set")
        if not secret_key:
            raise AlpacaDataCredentialError(f"{ALPACA_DATA_SECRET_KEY_ENV} must be set")
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "secret_key", secret_key)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> AlpacaDataCredentials:
        """Read the data-only pair from hardened files or legacy compatibility variables."""

        values = os.environ if environment is None else environment
        api_key_file = values.get(ALPACA_DATA_API_KEY_FILE_ENV, "").strip()
        secret_key_file = values.get(ALPACA_DATA_SECRET_KEY_FILE_ENV, "").strip()
        legacy_api_key = values.get(ALPACA_DATA_API_KEY_ENV, "").strip()
        legacy_secret_key = values.get(ALPACA_DATA_SECRET_KEY_ENV, "").strip()
        if api_key_file or secret_key_file:
            if not api_key_file or not secret_key_file:
                raise AlpacaDataCredentialError("both Alpaca data secret files are required")
            if legacy_api_key or legacy_secret_key:
                raise AlpacaDataCredentialError("Alpaca data credential sources are ambiguous")
            return cls(
                api_key=load_secret_file(
                    Path(api_key_file),
                    source=SecretFileVariable.ALPACA_DATA_API_KEY,
                ).reveal(),
                secret_key=load_secret_file(
                    Path(secret_key_file),
                    source=SecretFileVariable.ALPACA_DATA_SECRET_KEY,
                ).reveal(),
            )
        return cls(
            api_key=legacy_api_key,
            secret_key=legacy_secret_key,
        )

    def redact(self, message: object) -> str:
        """Remove both credential values from an exception or diagnostic string."""

        safe = str(message)
        for value in sorted({self.api_key, self.secret_key}, key=len, reverse=True):
            safe = safe.replace(value, "<redacted>")
        return safe

    def __repr__(self) -> str:
        return "AlpacaDataCredentials(api_key=<redacted>, secret_key=<redacted>)"

    __str__ = __repr__
