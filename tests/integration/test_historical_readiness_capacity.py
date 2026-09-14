"""Opt-in populated readiness capacity proof on an explicitly disposable cluster."""

import json
import os

import pytest
from sqlalchemy import Engine

from scripts.benchmark_historical_readiness import run
from tests.integration.test_platform_postgres_roles import provisioned_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.skipif(not os.environ.get("APA_CAPACITY_YEARS"), reason="capacity benchmark is opt-in")
def test_populated_readiness_capacity(provisioned_engine: Engine) -> None:  # noqa: F811
    report = run(int(os.environ["APA_CAPACITY_YEARS"]))
    assert report["passed"] is True
    assert report["bars"] > 90_000
    print(json.dumps(report, sort_keys=True))
