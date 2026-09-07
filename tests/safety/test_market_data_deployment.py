"""Data-only deployment capabilities and the actual collector image import closure."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def _deployment(project_root: Path) -> dict[str, Any]:
    result = yaml.safe_load(
        (project_root / "docker-compose.market-data.yml").read_text(encoding="utf-8")
    )
    assert isinstance(result, dict)
    return result


def _collector_stage(project_root: Path) -> str:
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    return dockerfile.split("FROM runtime-base AS market-data\n", 1)[1].split("\nFROM ", 1)[0]


def test_data_deployment_cannot_launch_research_or_execution(project_root: Path) -> None:
    deployment = _deployment(project_root)
    assert set(deployment["services"]) == {"market-data-collector"}
    service = deployment["services"]["market-data-collector"]
    assert service["command"] == ["python", "-m", "adaptive_trader.collection.cli", "run"]
    assert service["build"]["target"] == "market-data"
    assert not service.get("depends_on")
    assert not service.get("ports")
    assert not service.get("volumes")
    assert "CONTAINER_DATABASE_ROLE" not in service["environment"]
    assert service["environment"]["AQA_ENABLE_PAPER_ORDERS"] == "NO"


def test_data_deployment_mounts_only_collector_authority(project_root: Path) -> None:
    deployment = _deployment(project_root)
    service = deployment["services"]["market-data-collector"]
    expected_sources = {
        "collector_database_url": "AQA_DATABASE_URL_FILE",
        "alpaca_data_api_key": "AQA_ALPACA_DATA_API_KEY_FILE",  # pragma: allowlist secret
        "alpaca_data_secret_key": "AQA_ALPACA_DATA_SECRET_KEY_FILE",  # pragma: allowlist secret
    }
    assert set(deployment["secrets"]) == set(expected_sources)
    assert {secret["source"] for secret in service["secrets"]} == set(expected_sources)
    for secret in service["secrets"]:
        source = secret["source"]
        assert secret["target"] == source
        assert (secret["uid"], secret["gid"], secret["mode"]) == ("10001", "10001", 0o400)
        variable = expected_sources[source]
        assert service["environment"][variable] == f"/run/secrets/{source}"
        assert deployment["secrets"][source]["file"].startswith("${" + variable + ":-")
    assert set(service["environment"]) == set(expected_sources.values()) | {
        "AQA_MARKET_DATA_HISTORY_START",
        "AQA_ENABLE_PAPER_ORDERS",
    }


def test_data_deployment_survives_process_exit_without_writable_root(project_root: Path) -> None:
    service = _deployment(project_root)["services"]["market-data-collector"]
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "90s"
    assert service["init"] is True
    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["tmpfs"] == ["/tmp:size=128m,mode=1777"]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "adaptive_trader.collection.cli",
        "ready",
    ]
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "5"}
    assert set(service["deploy"]["resources"]["limits"]) == {"cpus", "memory", "pids"}


def _copy_image_application(project_root: Path, target_root: Path) -> None:
    """Reproduce source COPY instructions, excluding the separately locked dependency venv."""

    stage = _collector_stage(project_root).replace("\\\n", " ")
    for line in stage.splitlines():
        if not line.startswith("COPY ") or line.startswith("COPY --from="):
            continue
        fields = shlex.split(line)[1:]
        destination = target_root / fields[-1].removeprefix("./")
        for source_name in fields[:-1]:
            source = project_root / source_name
            if source.is_dir():
                shutil.copytree(
                    source,
                    destination,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                output = destination / source.name if fields[-1].endswith("/") else destination
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)


def test_collector_image_can_import_cli_without_application_or_ai_modules(
    project_root: Path,
    tmp_path: Path,
) -> None:
    """Exercise the image's selected sources, rather than the complete editable checkout."""

    _copy_image_application(project_root, tmp_path)
    source_root = tmp_path / "src"
    program = """
import importlib.util
import socket
import sys

def forbidden_network(*args, **kwargs):
    raise AssertionError("image import check must remain offline")

socket.getaddrinfo = forbidden_network
socket.create_connection = forbidden_network
socket.socket.connect = forbidden_network
sys.path.insert(0, sys.argv[1])
from adaptive_trader.collection.cli import app
from adaptive_trader.platform.package_resources import (
    packaged_alembic_ini, packaged_config_root, packaged_migration_root,
)
assert packaged_alembic_ini().is_file()
assert packaged_config_root().is_dir()
assert packaged_migration_root().is_dir()
for name in (
    "adaptive_trader.broker", "adaptive_trader.strategies",
    "adaptive_trader.platform.execution", "adaptive_trader.platform.signals",
    "adaptive_trader.platform.risk", "adaptive_trader.platform.scheduling",
    "adaptive_trader.platform.service_cycles", "adaptive_trader.platform.worker_runtime",
):
    assert importlib.util.find_spec(name) is None, name
sys.argv = ["adaptive-market-data", "--help"]
app()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(source_root)],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "backfill" in result.stdout
    assert "ready" in result.stdout
    stage = _collector_stage(project_root)
    assert "ENV PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt" in stage
    assert "RUN test -r /etc/ssl/certs/ca-certificates.crt" in stage
    command = next(line for line in stage.splitlines() if line.startswith("CMD "))
    assert json.loads(command.removeprefix("CMD ")) == [
        "python",
        "-m",
        "adaptive_trader.collection.cli",
        "run",
    ]
