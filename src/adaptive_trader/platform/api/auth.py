"""Public authentication and rate-limit contracts for the private control plane."""

from adaptive_trader.platform.control.auth import (
    AuthenticationConfigurationError,
    OperatorPrincipal,
    OperatorScope,
    OperatorTokenAuthenticator,
    derive_dashboard_read_token,
)
from adaptive_trader.platform.control.rate_limit import OperatorRateLimiter, RateClass

__all__ = [
    "AuthenticationConfigurationError",
    "OperatorPrincipal",
    "OperatorRateLimiter",
    "OperatorScope",
    "OperatorTokenAuthenticator",
    "RateClass",
    "derive_dashboard_read_token",
]
