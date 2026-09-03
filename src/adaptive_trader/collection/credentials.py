"""Credentials dedicated to read-only Alpaca market-data collection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ALPACA_DATA_API_KEY_ENV = "APA_ALPACA_DATA_API_KEY"
ALPACA_DATA_SECRET_KEY_ENV = "APA_ALPACA_DATA_SECRET_KEY"


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
        """Read only the two environment variables reserved for market data."""

        values = os.environ if environment is None else environment
        return cls(
            api_key=values.get(ALPACA_DATA_API_KEY_ENV, ""),
            secret_key=values.get(ALPACA_DATA_SECRET_KEY_ENV, ""),
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
