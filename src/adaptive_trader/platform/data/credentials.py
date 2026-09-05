"""File-backed, data-only Alpaca credential boundary."""

from __future__ import annotations

from typing import Never, SupportsIndex

from adaptive_trader.platform.errors import SecretFileError
from adaptive_trader.platform.security import (
    RedactedSecret,
    SecretFileReference,
    SecretFileVariable,
)

_CONSTRUCTION_TOKEN = object()


class AlpacaDataCredentialError(SecretFileError):
    """Raised when a data adapter receives missing or wrong-purpose credentials."""


class AlpacaDataCredentials:
    """Immutable Alpaca market-data credentials loaded only from owner-private files.

    Raw values remain wrapped in :class:`RedactedSecret` and are passed only to an injected
    transport boundary. This object cannot be created from strings, rendered with values, or
    serialized. Paper-trading credential sources are never accepted.
    """

    __slots__ = ("__api_key", "__secret_key")
    __api_key: RedactedSecret
    __secret_key: RedactedSecret

    def __init__(
        self,
        api_key: RedactedSecret,
        secret_key: RedactedSecret,
        *,
        _token: object,
    ) -> None:
        if (
            _token is not _CONSTRUCTION_TOKEN
            or type(api_key) is not RedactedSecret
            or type(secret_key) is not RedactedSecret
        ):
            raise TypeError("Alpaca data credentials must be loaded from secret-file references")
        object.__setattr__(self, "_AlpacaDataCredentials__api_key", api_key)
        object.__setattr__(self, "_AlpacaDataCredentials__secret_key", secret_key)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Alpaca data credentials are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Alpaca data credentials are immutable")

    def __repr__(self) -> str:
        return "<alpaca-data-credentials configured=true values=<redacted>>"

    def __str__(self) -> str:
        return repr(self)

    def __copy__(self) -> AlpacaDataCredentials:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> AlpacaDataCredentials:
        del memo
        return self

    def __reduce__(self) -> Never:
        raise TypeError("Alpaca data credentials cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Never:
        del protocol
        raise TypeError("Alpaca data credentials cannot be serialized")

    @classmethod
    def load(
        cls,
        *,
        api_key_file: SecretFileReference,
        secret_key_file: SecretFileReference,
    ) -> AlpacaDataCredentials:
        """Load the exact AQA data-key pair through hardened file references."""

        if cls is not AlpacaDataCredentials:
            raise TypeError("Alpaca data credential factories do not support subclasses")
        if type(api_key_file) is not SecretFileReference or (
            api_key_file.source is not SecretFileVariable.ALPACA_DATA_API_KEY
        ):
            raise AlpacaDataCredentialError(
                "AQA_ALPACA_DATA_API_KEY_FILE: data API key reference is required"
            )
        if type(secret_key_file) is not SecretFileReference or (
            secret_key_file.source is not SecretFileVariable.ALPACA_DATA_SECRET_KEY
        ):
            raise AlpacaDataCredentialError(
                "AQA_ALPACA_DATA_SECRET_KEY_FILE: data secret key reference is required"
            )
        return cls(
            api_key_file.load(),
            secret_key_file.load(),
            _token=_CONSTRUCTION_TOKEN,
        )

    def transport_material(self) -> tuple[RedactedSecret, RedactedSecret]:
        """Return wrapped values solely for an injected Alpaca data transport."""

        return self.__api_key, self.__secret_key


__all__ = ["AlpacaDataCredentialError", "AlpacaDataCredentials"]
