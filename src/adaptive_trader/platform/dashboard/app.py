"""Read-only Streamlit operations view backed exclusively by the private API."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.dashboard.client import (
    DashboardApiClient,
    DashboardApiError,
    DashboardRoute,
)


@dataclass(frozen=True, slots=True)
class DashboardSection:
    title: str
    routes: tuple[DashboardRoute, ...]


_SECTIONS = (
    DashboardSection(
        "Service health",
        (DashboardRoute.HEALTH_LIVE, DashboardRoute.HEALTH_READY, DashboardRoute.SYSTEM_STATUS),
    ),
    DashboardSection("Active experiment and roles", (DashboardRoute.EXPERIMENT,)),
    DashboardSection(
        "Data freshness, gaps, corrections, and watermarks",
        (DashboardRoute.DATA_STATUS, DashboardRoute.DATA_GAPS),
    ),
    DashboardSection("Dataset manifests", (DashboardRoute.DATASETS,)),
    DashboardSection("Scheduler slots and deadlines", (DashboardRoute.DECISION_SLOTS,)),
    DashboardSection("Signals and authorization blocks", (DashboardRoute.SIGNALS,)),
    DashboardSection(
        "Signed exposure and risk controls",
        (DashboardRoute.RISK_DECISIONS, DashboardRoute.RISK_LATCHES),
    ),
    DashboardSection("Dry-run, fake, and paper order states", (DashboardRoute.ORDERS,)),
    DashboardSection(
        "Reconciliation discrepancies and signed positions",
        (DashboardRoute.FILLS, DashboardRoute.RECONCILIATIONS),
    ),
    DashboardSection("Incidents", (DashboardRoute.INCIDENTS,)),
    DashboardSection("Audit stream status", (DashboardRoute.AUDIT_STATUS,)),
)


def render_dashboard(client: DashboardApiClient) -> None:
    """Render safe summaries; no mutation or broker controls exist in this UI."""

    if not isinstance(client, DashboardApiClient):
        raise TypeError("dashboard requires the private API client")
    st.set_page_config(page_title="Autonomous Quant Agent", layout="wide")
    st.title("Autonomous Quant Agent")
    st.caption("Read-only operator status")
    st.caption("This is a status snapshot. Refresh the page to retrieve current operational state.")
    for section in _SECTIONS:
        st.subheader(section.title)
        _render_routes(client, section.routes)
    _render_job_lookup(client)


def _render_routes(client: DashboardApiClient, routes: Sequence[DashboardRoute]) -> None:
    for route in routes:
        try:
            payload = client.get(route)
        except DashboardApiError:
            st.warning("Operational status is temporarily unavailable")
            continue
        items = payload.get("items")
        if type(items) is list:
            if not items:
                if route is DashboardRoute.SIGNALS:
                    st.info(
                        "No signal receipts are available; this view shows no approved AI output."
                    )
                else:
                    st.info("No records are available for this view.")
            else:
                st.dataframe(items, use_container_width=True, hide_index=True)
                st.caption("Showing the API's first bounded page of records.")
        else:
            st.json(payload, expanded=False)


def _render_job_lookup(client: DashboardApiClient) -> None:
    st.subheader("Jobs and deterministic demo status")
    job_id = st.text_input(
        "Job identifier",
        max_chars=128,
        help="Use the identifier returned by an operational job request. This lookup only reads status.",
    )
    if not job_id:
        st.info("Enter a job identifier to inspect its state and evidence artifact reference.")
        return
    try:
        job = client.get_job(job_id)
    except DashboardApiError:
        st.warning("Job status is unavailable or the job identifier is invalid.")
        return
    st.json(job, expanded=False)
    if job["job_type"] == "OFFLINE_DEMO":
        if job["state"] == "SUCCEEDED" and job["result_artifact_id"] is not None:
            st.success(
                "The offline demo job succeeded and recorded an evidence artifact reference."
            )
            st.caption(
                "Synthetic offline evidence does not establish AI approval or paper authorization."
            )
        elif job["state"] in {"FAILED", "DEAD", "CANCELED"}:
            st.warning("The offline demo job has not completed successfully.")
        else:
            st.info("No completed offline demo evidence is available for this job.")


def main(*, application_root: Path | None = None) -> None:
    """Load dashboard-only settings and render through the private read API."""

    root = _application_root(tuple(sys.argv[1:])) if application_root is None else application_root
    settings = load_runtime_settings(
        dict(os.environ),
        service=RuntimeService.DASHBOARD,
        application_root=root.resolve(strict=True),
    )
    render_dashboard(DashboardApiClient.from_settings(settings))


def _application_root(arguments: tuple[str, ...]) -> Path:
    if len(arguments) != 1 or not arguments[0]:
        raise RuntimeError("dashboard application root is unavailable")
    return Path(arguments[0])


if __name__ == "__main__":
    main()
