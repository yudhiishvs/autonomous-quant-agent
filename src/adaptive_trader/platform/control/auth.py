"""Constant-time bearer-token authentication from the secret-file boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum

from adaptive_trader.platform.security import RedactedSecret, SecretFileReference

_TOKEN = re.compile(r"^[\x21-\x7e]{32,4096}$", re.ASCII)
_DASHBOARD_READ_TOKEN_DOMAIN = b"autonomous-quant-agent:dashboard-read-bearer:v1"


class AuthenticationConfigurationError(RuntimeError):
    """The operator-token configuration cannot safely authenticate requests."""


class OperatorScope(StrEnum):
    """Closed control-plane authorization scopes."""

    OPERATOR = "operator"
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    """Opaque identity used for per-token rate limiting."""

    fingerprint: str
    scope: OperatorScope

    def __post_init__(self) -> None:
        if (
            type(self.fingerprint) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None
        ):
            raise ValueError("operator principal fingerprint is invalid")
        if type(self.scope) is not OperatorScope:
            raise TypeError("operator principal scope is invalid")


def derive_dashboard_read_token(operator_secret: RedactedSecret) -> str:
    """Derive the dashboard's read-only bearer without returning the operator bearer."""

    if type(operator_secret) is not RedactedSecret:
        raise TypeError("dashboard token derivation requires a loaded operator secret")
    value = operator_secret.reveal()
    if _TOKEN.fullmatch(value) is None:
        raise AuthenticationConfigurationError(
            "operator token must be 32 to 4096 visible ASCII bytes"
        )
    return hmac.new(
        value.encode("ascii"),
        _DASHBOARD_READ_TOKEN_DOMAIN,
        hashlib.sha256,
    ).hexdigest()


class OperatorTokenAuthenticator:
    """Authenticate operator and derived read-only tokens using fixed-size digests."""

    def __init__(self, expected: RedactedSecret) -> None:
        if type(expected) is not RedactedSecret:
            raise TypeError("operator authenticator requires a loaded secret")
        value = expected.reveal()
        if _TOKEN.fullmatch(value) is None:
            raise AuthenticationConfigurationError(
                "operator token must be 32 to 4096 visible ASCII bytes"
            )
        dashboard_read_token = derive_dashboard_read_token(expected)
        self._operator_fingerprint = hashlib.sha256(value.encode("ascii")).hexdigest()
        self._read_only_fingerprint = hashlib.sha256(
            dashboard_read_token.encode("ascii")
        ).hexdigest()
        if hmac.compare_digest(self._operator_fingerprint, self._read_only_fingerprint):
            raise AuthenticationConfigurationError("operator token derivation is invalid")

    @classmethod
    def from_reference(cls, reference: SecretFileReference) -> OperatorTokenAuthenticator:
        if type(reference) is not SecretFileReference:
            raise TypeError("operator authenticator requires a secret-file reference")
        return cls(reference.load())

    def authenticate(self, authorization: str | None) -> OperatorPrincipal | None:
        if type(authorization) is not str or len(authorization) > 4_103:
            return None
        scheme, separator, supplied = authorization.partition(" ")
        if separator != " " or scheme != "Bearer" or _TOKEN.fullmatch(supplied) is None:
            return None
        supplied_fingerprint = hashlib.sha256(supplied.encode("ascii")).hexdigest()
        operator_matches = hmac.compare_digest(
            self._operator_fingerprint,
            supplied_fingerprint,
        )
        read_only_matches = hmac.compare_digest(
            self._read_only_fingerprint,
            supplied_fingerprint,
        )
        if operator_matches:
            return OperatorPrincipal(
                fingerprint=self._operator_fingerprint,
                scope=OperatorScope.OPERATOR,
            )
        if read_only_matches:
            return OperatorPrincipal(
                fingerprint=self._read_only_fingerprint,
                scope=OperatorScope.READ_ONLY,
            )
        return None
