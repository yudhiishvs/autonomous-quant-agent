"""Status observes existing state and cannot initialize storage or claim health."""

import json
import shutil
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from adaptive_trader.platform.cli import app
from adaptive_trader.platform.durable_status import durable_status, observe_status
from tests.unit.test_operational_scheduler import ROOT, scheduler

__all__ = ["scheduler"]


def test_status_observes_current_slots_without_advancing_them(scheduler):
    cycle, clock, schedule = scheduler
    cycle.run_cycle()
    first = cycle.repository.get(schedule.strategy_slots[0].slot_id)
    result = observe_status(
        cycle.engine, experiment_hash=first.experiment_hash, kind="scheduler", now=clock[0]
    )
    assert result["observation"] == "observed"
    assert result["returned_count"] == len(schedule.slots)
    assert result["counts_within_returned_records"]["WAITING_FOR_DATA"] >= 1
    assert result["health"] == "not_evaluated"
    assert cycle.repository.get(first.slot_id) == first
    next_day = observe_status(
        cycle.engine,
        experiment_hash=first.experiment_hash,
        kind="scheduler",
        now=clock[0] + timedelta(days=1),
    )
    assert next_day["status"] == "empty"
    assert next_day["returned_count"] == 0


def test_empty_data_state_is_observed_without_claiming_readiness(scheduler):
    cycle, clock, _ = scheduler
    result = observe_status(
        cycle.engine,
        experiment_hash=cycle.settings.platform.experiment.definition.content_hash,
        kind="data",
        now=clock[0],
    )
    assert result["status"] == "empty"
    assert result["records"] == []
    assert result["health"] == "not_evaluated"


def test_missing_offline_database_is_unavailable_and_never_created(tmp_path):
    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    for kind in ("data", "scheduler"):
        result = durable_status(
            kind=kind, profile=Path("platform/offline.yaml"), application_root=tmp_path
        )
        assert result["observation"] == "unavailable"
        assert result["health"] == "unknown"
    assert not (tmp_path / "runtime").exists()


def test_cli_configuration_does_not_become_observed_health(tmp_path):
    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    runner = CliRunner()
    for kind in ("data", "scheduler"):
        result = runner.invoke(
            app,
            [
                kind,
                "status",
                "--application-root",
                str(tmp_path),
                "--config-root",
                str(tmp_path / "configs"),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["configured"]["mode"] == "offline"
        assert payload["status"] == "unavailable"
    preview = runner.invoke(app, ["scheduler", "status", "--preview", "--json"])
    assert preview.exit_code == 0, preview.output
    payload = json.loads(preview.output)
    assert payload["observation"] == "fixture_preview"
    assert payload["strategy_slots"] == 20
    assert payload["status"] == "preview"
