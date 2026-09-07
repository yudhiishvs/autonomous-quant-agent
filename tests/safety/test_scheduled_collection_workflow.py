"""Scheduled activation and executable secret-file bridge contracts, with synthetic values."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml


def _workflow(project_root: Path) -> dict[str, Any]:
    return yaml.safe_load((project_root / ".github/workflows/market-data.yml").read_text())


def test_collection_schedule_is_opt_in_bounded_and_has_only_data_authority(
    project_root: Path,
) -> None:
    workflow = _workflow(project_root)
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"] == [{"cron": "2-57/5 9-17 * * 1-5", "timezone": "America/New_York"}]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["activate"]["default"] is False
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "github.ref" not in workflow["concurrency"]["group"]
    job = workflow["jobs"]["collect"]
    assert "vars.AQA_ENABLE_SCHEDULED_DATA_COLLECTION == 'true'" in job["if"]
    assert "inputs.activate == true" in job["if"]
    assert "github.event.repository.default_branch" in job["if"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert 1 <= job["timeout-minutes"] <= 20
    steps = job["steps"]
    assert any(step.get("run") == "uv sync --locked" for step in steps)
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    assert not any(step.get("with", {}).get("enable-cache") for step in steps)
    authority = steps[-1]["env"]
    assert {name for name, value in authority.items() if "secrets." in str(value)} == {
        "AQA_DATABASE_URL",
        "AQA_ALPACA_DATA_API_KEY",
        "AQA_ALPACA_DATA_SECRET_KEY",
    }
    assert authority["AQA_ENABLE_PAPER_ORDERS"] == "NO"


@pytest.mark.parametrize("child_exit", [0, 7])
def test_workflow_bridge_passes_private_files_and_cleans_up_on_child_exit(
    project_root: Path, tmp_path: Path, child_exit: int
) -> None:
    command = _workflow(project_root)["jobs"]["collect"]["steps"][-1]["run"]
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    executable = commands / "uv"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:] == ['run', '--no-sync', 'aqa', 'data', 'collect-once']\n"
        "names = ('AQA_DATABASE_URL', 'AQA_ALPACA_DATA_API_KEY', 'AQA_ALPACA_DATA_SECRET_KEY')\n"
        "for name in names:\n"
        "    assert name not in os.environ\n"
        "    path = Path(os.environ[name + '_FILE'])\n"
        "    assert stat.S_IMODE(path.stat().st_mode) == 0o600\n"
        "    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700\n"
        "    assert path.stat().st_uid == os.getuid()\n"
        "    assert path.read_text() == name + ':synthetic-value'\n"
        "Path(os.environ['RUNNER_TEMP'], 'checked.json').write_text(json.dumps({'checked': 3}))\n"
        "sys.exit(int(os.environ['TEST_EXIT']))\n"
    )
    executable.chmod(0o700)
    environment = {
        "PATH": str(commands) + os.pathsep + os.defpath,
        "RUNNER_TEMP": str(runner_temp),
        "TEST_EXIT": str(child_exit),
        **{
            name: name + ":synthetic-value"
            for name in (
                "AQA_DATABASE_URL",
                "AQA_ALPACA_DATA_API_KEY",
                "AQA_ALPACA_DATA_SECRET_KEY",
            )
        },
    }
    result = subprocess.run(
        ["bash", "-c", command], env=environment, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == child_exit, result.stderr
    assert "synthetic-value" not in result.stdout + result.stderr
    assert json.loads((runner_temp / "checked.json").read_text()) == {"checked": 3}
    assert list(runner_temp.glob("aqa-market-data.*")) == []


def test_workflow_missing_secret_fails_before_materialization(
    project_root: Path, tmp_path: Path
) -> None:
    command = _workflow(project_root)["jobs"]["collect"]["steps"][-1]["run"]
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            "PATH": os.defpath,
            "RUNNER_TEMP": str(tmp_path),
            "AQA_DATABASE_URL": "",
            "AQA_ALPACA_DATA_API_KEY": "",
            "AQA_ALPACA_DATA_SECRET_KEY": "",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Required data secret is unavailable" in result.stderr
    assert list(tmp_path.iterdir()) == []
