"""Private control-plane API and least-privilege read boundaries."""

from adaptive_trader.platform.control.api import MAX_REQUEST_BYTES, create_control_app
from adaptive_trader.platform.control.auth import (
    AuthenticationConfigurationError,
    OperatorPrincipal,
    OperatorScope,
    OperatorTokenAuthenticator,
    derive_dashboard_read_token,
)
from adaptive_trader.platform.control.models import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    OPERATOR_RESUME_ACKNOWLEDGEMENT,
)
from adaptive_trader.platform.control.operator import (
    OperatorControlConflictError,
    OperatorControlError,
    OperatorControlPort,
    OperatorControlResult,
    SQLAlchemyOperatorControls,
    require_resume_acknowledgement,
)
from adaptive_trader.platform.control.queries import (
    ControlQueryError,
    ControlQueryPort,
    ReadResource,
    SQLAlchemyControlQueryService,
)
from adaptive_trader.platform.control.rate_limit import OperatorRateLimiter, RateClass

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "MAX_REQUEST_BYTES",
    "OPERATOR_RESUME_ACKNOWLEDGEMENT",
    "AuthenticationConfigurationError",
    "ControlQueryError",
    "ControlQueryPort",
    "OperatorControlConflictError",
    "OperatorControlError",
    "OperatorControlPort",
    "OperatorControlResult",
    "OperatorPrincipal",
    "OperatorRateLimiter",
    "OperatorScope",
    "OperatorTokenAuthenticator",
    "RateClass",
    "ReadResource",
    "SQLAlchemyControlQueryService",
    "SQLAlchemyOperatorControls",
    "create_control_app",
    "derive_dashboard_read_token",
    "require_resume_acknowledgement",
]
