"""Read-only Streamlit dashboard over the private control API."""

from adaptive_trader.platform.dashboard.app import main, render_dashboard
from adaptive_trader.platform.dashboard.client import (
    DashboardApiClient,
    DashboardApiError,
    DashboardRoute,
)

__all__ = [
    "DashboardApiClient",
    "DashboardApiError",
    "DashboardRoute",
    "main",
    "render_dashboard",
]
