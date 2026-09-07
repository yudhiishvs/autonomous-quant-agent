"""Exercise actual Streamlit rendering and read-only job lookup interactions."""

import pytest
from streamlit.testing.v1 import AppTest


def _dashboard_fixture(mode: str) -> None:
    from adaptive_trader.platform.dashboard.app import render_dashboard
    from adaptive_trader.platform.dashboard.client import (
        DashboardApiClient,
        DashboardApiError,
        DashboardRoute,
    )

    class FixtureClient(DashboardApiClient):
        def __init__(self) -> None:
            self.mode = mode

        def get(self, route: DashboardRoute):
            if self.mode == "unavailable":
                raise DashboardApiError("sensitive-error-sentinel")
            if route in {DashboardRoute.HEALTH_LIVE, DashboardRoute.HEALTH_READY}:
                return {
                    "status": "live" if route is DashboardRoute.HEALTH_LIVE else "ready",
                    "checked_at": "2026-07-06T20:00:00Z",
                    "checks": {},
                }
            if route is DashboardRoute.SIGNALS and self.mode == "populated":
                return {"items": [{"provider_id": "fixture-provider", "promotable": False}]}
            return {"items": []}

        def get_job(self, job_id: str):
            if job_id != "job-1" or self.mode == "unavailable":
                raise DashboardApiError("sensitive-error-sentinel")
            return {
                "job_id": job_id,
                "job_type": "OFFLINE_DEMO",
                "state": "FAILED" if self.mode == "failed" else "SUCCEEDED",
                "result_artifact_id": None if self.mode == "failed" else "demo-evidence-1",
            }

    render_dashboard(FixtureClient())


def test_dashboard_renders_truthful_empty_state_and_audit_label() -> None:
    app = AppTest.from_function(_dashboard_fixture, args=("empty",)).run()
    assert not app.exception
    assert any("no approved AI output" in element.value for element in app.info)
    assert "Audit stream status" in [element.value for element in app.subheader]
    assert "Offline demo evidence" not in [element.value for element in app.subheader]
    assert app.text_input[0].label == "Job identifier"
    assert not app.button
    assert any("Refresh the page" in element.value for element in app.caption)


def test_dashboard_job_lookup_renders_recorded_demo_success() -> None:
    app = AppTest.from_function(_dashboard_fixture, args=("empty",)).run()
    app.text_input[0].input("job-1").run()
    assert not app.exception
    assert len(app.success) == 1
    assert "recorded an evidence artifact reference" in app.success[0].value
    assert "demo-evidence-1" in app.json[-1].value
    assert any("does not establish AI approval" in element.value for element in app.caption)


@pytest.mark.parametrize("mode,job_id", (("failed", "job-1"), ("empty", "../orders")))
def test_dashboard_failed_or_invalid_job_never_renders_success(mode: str, job_id: str) -> None:
    app = AppTest.from_function(_dashboard_fixture, args=(mode,)).run()
    app.text_input[0].input(job_id).run()
    assert not app.exception
    assert not app.success
    assert app.warning
    assert all("sensitive-error-sentinel" not in element.value for element in app.warning)


def test_dashboard_degraded_api_renders_safe_errors_and_remains_usable() -> None:
    app = AppTest.from_function(_dashboard_fixture, args=("unavailable",)).run()
    assert not app.exception
    assert all(
        element.value == "Operational status is temporarily unavailable" for element in app.warning
    )
    app.text_input[0].input("job-1").run()
    assert not app.exception
    assert app.warning[-1].value == "Job status is unavailable or the job identifier is invalid."


def test_dashboard_renders_available_records_without_empty_signal_claim() -> None:
    app = AppTest.from_function(_dashboard_fixture, args=("populated",)).run()
    assert not app.exception
    assert len(app.dataframe) == 1
    assert app.dataframe[0].value.iloc[0]["provider_id"] == "fixture-provider"
    assert all("no approved AI output" not in element.value for element in app.info)
