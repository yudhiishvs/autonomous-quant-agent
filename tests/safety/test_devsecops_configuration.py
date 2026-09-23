"""Declarative security contracts for containers and supply-chain automation."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

_RUNTIME_SERVICES = {
    "control-api",
    "job-worker",
    "market-data-worker",
    "scheduler-worker",
    "strategy-worker",
    "execution-worker",
    "dashboard",
    "market-data-live",
    "paper-execution-worker",
    "db-debug",
}
_REQUIRED_SERVICES = _RUNTIME_SERVICES | {"postgres", "database-bootstrap", "migrate"}
_ALPACA_DATA_SECRETS = {"alpaca_data_api_key", "alpaca_data_secret_key"}
_ALPACA_PAPER_SECRETS = {
    "alpaca_paper_api_key",
    "alpaca_paper_secret_key",
    "paper_account_id_hash",
}
_ACTION_REFERENCE = re.compile(r"^[^\s@]+@[0-9a-f]{40}$")


def _yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _compose(project_root: Path) -> dict[str, Any]:
    return _yaml(project_root / "docker-compose.yml")


def _secret_sources(service: dict[str, Any]) -> set[str]:
    return {
        item if isinstance(item, str) else item["source"] for item in service.get("secrets", [])
    }


def test_compose_declares_exact_runtime_topology_and_profile_gates(project_root: Path) -> None:
    compose = _compose(project_root)
    services = compose["services"]
    assert set(services) == _REQUIRED_SERVICES
    assert services["market-data-live"]["profiles"] == ["market-data"]
    assert services["paper-execution-worker"]["profiles"] == ["paper"]
    assert services["db-debug"]["profiles"] == ["db-debug"]
    assert "profiles" not in services["job-worker"]
    for name in ("market-data-worker", "scheduler-worker", "strategy-worker", "execution-worker"):
        assert "profiles" not in services[name]
        assert services[name]["command"] == ["aqa", "service", "run", name]


def test_compose_launches_only_implemented_default_runtime_commands(
    project_root: Path,
) -> None:
    services = _compose(project_root)["services"]
    assert services["migrate"]["command"] == [
        "aqa",
        "db",
        "migrate",
        "--database-url-file",
        "/tmp/aqa/database-url",
    ]
    assert services["control-api"]["command"] == [
        "aqa",
        "api",
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert services["job-worker"]["command"] == ["aqa", "service", "run", "job-worker"]
    assert services["job-worker"]["healthcheck"]["test"] == [
        "CMD",
        "aqa",
        "service",
        "health",
        "job-worker",
    ]
    assert services["dashboard"]["command"] == [
        "aqa",
        "dashboard",
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "8501",
    ]


def test_compose_hardens_every_runtime_service(project_root: Path) -> None:
    services = _compose(project_root)["services"]
    for name in _RUNTIME_SERVICES:
        service = services[name]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert any(str(value).startswith("/tmp:") for value in service["tmpfs"])
        if name == "market-data-live":
            assert service["restart"] == "unless-stopped"
        else:
            assert service["restart"] in {"on-failure:5", "no"}
        assert service["healthcheck"]
        limits = service["deploy"]["resources"]["limits"]
        assert set(limits) == {"cpus", "memory", "pids"}
        assert limits["pids"] == 256
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        assert all("docker.sock" not in str(volume) for volume in service.get("volumes", []))


def test_compose_publishes_only_loopback_control_and_debug_ports(project_root: Path) -> None:
    services = _compose(project_root)["services"]
    published = {
        name: service["ports"] for name, service in services.items() if service.get("ports")
    }
    assert published == {
        "control-api": ["127.0.0.1:8000:8000"],
        "dashboard": ["127.0.0.1:8501:8501"],
        "db-debug": ["127.0.0.1:5432:5432"],
    }
    assert "ports" not in services["postgres"]
    assert services["db-debug"]["profiles"] == ["db-debug"]


def test_compose_enforces_secret_and_network_trust_zones(project_root: Path) -> None:
    compose = _compose(project_root)
    services = compose["services"]
    assert compose["networks"]["control"]["internal"] is True
    assert compose["networks"]["database"]["internal"] is True

    assert set(services["dashboard"]["networks"]) == {"control"}
    assert _secret_sources(services["dashboard"]) == set()
    assert _secret_sources(services["control-api"]) == {
        "aqa_control_password",
        "operator_token",
    }
    assert services["control-api"]["environment"]["CONTAINER_DERIVE_DASHBOARD_READ_TOKEN"] == "YES"
    assert services["control-api"]["environment"]["AQA_OPERATOR_TOKEN_FILE"] == (
        "/run/secrets/operator_token"
    )
    assert services["dashboard"]["environment"]["AQA_OPERATOR_TOKEN_FILE"] == (
        "/app/runtime/dashboard-auth/read-token"
    )
    assert "dashboard-auth:/app/runtime/dashboard-auth" in services["control-api"]["volumes"]
    assert services["dashboard"]["volumes"] == ["dashboard-auth:/app/runtime/dashboard-auth:ro"]
    assert _secret_sources(services["job-worker"]) == {"aqa_control_password"}
    assert services["job-worker"]["environment"]["CONTAINER_DATABASE_ROLE"] == "aqa_control"
    assert set(services["job-worker"]["networks"]) == {"database"}
    assert services["job-worker"]["volumes"] == ["platform-artifacts:/app/outputs/artifacts"]
    assert "AQA_OPERATOR_TOKEN_FILE" not in services["job-worker"]["environment"]
    assert not (
        _secret_sources(services["job-worker"]) & (_ALPACA_DATA_SECRETS | _ALPACA_PAPER_SECRETS)
    )
    assert "dashboard-auth" in compose["volumes"]
    assert not (_secret_sources(services["control-api"]) & _ALPACA_PAPER_SECRETS)
    assert not (_secret_sources(services["market-data-live"]) & _ALPACA_PAPER_SECRETS)
    assert _secret_sources(services["market-data-live"]) >= _ALPACA_DATA_SECRETS
    assert not (_secret_sources(services["paper-execution-worker"]) & _ALPACA_DATA_SECRETS)
    assert _secret_sources(services["paper-execution-worker"]) >= _ALPACA_PAPER_SECRETS
    assert "paper-egress" not in services["market-data-live"]["networks"]
    assert "market-data-egress" not in services["paper-execution-worker"]["networks"]

    bootstrap = services["database-bootstrap"]
    assert bootstrap["restart"] == "no"
    assert not (_secret_sources(bootstrap) & (_ALPACA_DATA_SECRETS | _ALPACA_PAPER_SECRETS))
    assert _secret_sources(services["migrate"]) == {"aqa_migrate_password"}


def test_compose_keeps_paper_submission_default_denied(project_root: Path) -> None:
    services = _compose(project_root)["services"]
    paper_environment = services["paper-execution-worker"]["environment"]
    assert paper_environment["AQA_ENABLE_PAPER_ORDERS"] == "NO"
    paper_profile = _yaml(project_root / "configs" / "platform" / "paper.yaml")
    assert paper_profile["execution"] == {
        "broker": "alpaca_paper",
        "submission_enabled": False,
        "paper_only": True,
    }


def test_dockerfile_is_locked_multistage_and_nonroot(project_root: Path) -> None:
    text = (project_root / "Dockerfile").read_text(encoding="utf-8")
    assert "ghcr.io/astral-sh/uv:0.11.7@sha256:" in text
    assert "cgr.dev/chainguard/wolfi-base@sha256:" in text
    assert "python-3.11=3.11.16-r7 sqlite-libs=3.53.4-r2" in text
    assert text.count("FROM python-base AS") == 4
    assert "uv sync --locked --no-dev --extra dashboard --no-editable" in text
    assert "uv sync --locked --only-group market-data-runtime --no-install-project" in text
    assert "pip install" not in text
    assert "COPY . " not in text
    assert text.count("USER 10001:10001") == 3
    assert "chmod 0555 /app" in text
    assert "-m 0700 \\" in text
    assert "/app/runtime/dashboard-auth /app/secrets" in text
    assert "--chown=10001:10001" not in text
    assert "AQA_VCS_REF" in text and "org.opencontainers.image.revision" in text
    assert "ENTRYPOINT" in text


def test_precommit_hooks_are_immutable_and_complete(project_root: Path) -> None:
    config = _yaml(project_root / ".pre-commit-config.yaml")
    hooks = {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}
    assert hooks >= {
        "ruff-check",
        "ruff-format",
        "trailing-whitespace",
        "end-of-file-fixer",
        "check-yaml",
        "check-toml",
        "detect-private-key",
        "detect-secrets",
        "check-added-large-files",
    }
    for repository in config["repos"]:
        assert re.fullmatch(r"[0-9a-f]{40}", repository["rev"])


def test_dependabot_covers_uv_actions_and_docker(project_root: Path) -> None:
    config = _yaml(project_root / ".github" / "dependabot.yml")
    assert {entry["package-ecosystem"] for entry in config["updates"]} == {
        "uv",
        "github-actions",
        "docker",
    }
    assert all(entry["directory"] == "/" for entry in config["updates"])


def test_dependency_updates_respect_frozen_graph_and_supported_python(project_root: Path) -> None:
    entries = {
        entry["package-ecosystem"]: entry
        for entry in _yaml(project_root / ".github" / "dependabot.yml")["updates"]
    }
    assert entries["uv"]["open-pull-requests-limit"] == 0
    assert "ignore" not in entries["uv"]  # Security updates must remain eligible.
    assert entries["docker"]["open-pull-requests-limit"] > 0
    assert entries["docker"]["ignore"] == [
        {
            "dependency-name": "python",
            "update-types": ["version-update:semver-major", "version-update:semver-minor"],
        }
    ]


def test_verification_configuration_is_publishable_but_private_state_is_ignored(
    project_root: Path,
) -> None:
    public = {".coveragerc", ".gitleaksignore", ".secrets.baseline"}
    private = {".env", ".env.local", ".coverage", "secrets/example.key", "runtime/example.db"}
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(sorted(public | private)) + "\n",
        capture_output=True,
        text=True,
        cwd=project_root,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == private
    assert all((project_root / name).is_file() for name in public)


def test_codeql_initialization_and_analysis_use_compatible_revisions(project_root: Path) -> None:
    workflow = _yaml(project_root / ".github" / "workflows" / "codeql.yml")
    references = {
        step["uses"].split("@", 1)[0]: step["uses"].split("@", 1)[1]
        for step in workflow["jobs"]["codeql"]["steps"]
        if step.get("uses", "").startswith("github/codeql-action/")
    }
    assert references["github/codeql-action/init"] == references["github/codeql-action/analyze"]


def test_security_container_and_codeql_gates_are_wired(project_root: Path) -> None:
    workflow_paths = sorted((project_root / ".github" / "workflows").glob("*.yml"))
    workflows = {path.name: _yaml(path) for path in workflow_paths}
    assert set(workflows) >= {
        "ci.yml",
        "security.yml",
        "container.yml",
        "codeql.yml",
        "offline-demo.yml",
    }

    for workflow in workflows.values():
        assert workflow["permissions"]["contents"] == "read"
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is not None:
                    assert _ACTION_REFERENCE.fullmatch(action), action

    security = " ".join(yaml.safe_dump(workflows["security.yml"], sort_keys=True).split())
    assert "ghcr.io/gitleaks/gitleaks@sha256:" in security
    assert "git /repo --log-opts=--all" in security
    assert '--volume "${{ github.workspace }}:/repo:ro"' in security
    assert "--redact --exit-code=1" in security
    assert "pip-audit" in security
    assert "--no-editable" in security
    assert "--require-hashes" in security
    assert "bandit" in security
    assert "src docker" in security
    assert "tests/architecture" in security
    assert "test_offline_environment.py" in security
    assert "test_platform_control_api.py" in security
    assert "test_platform_security.py" in security

    container = " ".join(yaml.safe_dump(workflows["container.yml"], sort_keys=True).split())
    assert "anchore/sbom-action@" in container
    assert "aquasecurity/trivy-action@" in container
    assert "cyclonedx-py environment" in container
    assert "--network none" in container
    assert 'Path("/app/src").stat().st_uid == 0' in container
    assert 'Path("/app/.venv").stat().st_uid == 0' in container
    assert 'find_spec("alpaca") is None' in container
    assert 'find_spec("fastapi") is None' in container
    assert 'find_spec("yfinance") is None' in container
    assert "docker push" not in container

    codeql = workflows["codeql.yml"]
    assert codeql["permissions"] == {"contents": "read", "security-events": "write"}
    codeql_rendered = yaml.safe_dump(codeql, sort_keys=True)
    assert "github/codeql-action/init@" in codeql_rendered
    assert "github/codeql-action/analyze@" in codeql_rendered

    offline_demo = workflows["offline-demo.yml"]
    assert offline_demo["env"] == {
        "APCA_API_BASE_URL": "",
        "APCA_API_KEY_ID": "",
        "APCA_API_SECRET_KEY": "",
        "ALPACA_API_KEY": "",
        "ALPACA_SECRET_KEY": "",
        "APA_ALPACA_PAPER_API_KEY": "",
        "APA_ALPACA_PAPER_SECRET_KEY": "",
        "APA_ALPACA_DATA_API_KEY": "",
        "APA_ALPACA_DATA_SECRET_KEY": "",
        "APA_MARKET_DATA_DATABASE_URL": "",
        "APA_MARKET_DATA_MIGRATION_DATABASE_URL": "",
        "APA_ENABLE_PAPER_ORDERS": "NO",
        "AQA_ALPACA_DATA_API_KEY_FILE": "",
        "AQA_ALPACA_DATA_SECRET_KEY_FILE": "",
        "AQA_ALPACA_PAPER_API_KEY_FILE": "",
        "AQA_ALPACA_PAPER_SECRET_KEY_FILE": "",
        "AQA_PAPER_ACCOUNT_ID_HASH_FILE": "",
        "AQA_OPERATOR_TOKEN_FILE": "",
        "AQA_DATABASE_URL_FILE": "",
        "AQA_ENABLE_PAPER_ORDERS": "NO",
    }
    offline_steps = " ".join(
        str(step.get("run", "")) for step in offline_demo["jobs"]["offline-demo"]["steps"]
    )
    assert "tests/unit/test_platform_demo.py" in offline_steps
    assert "demo-first.json" in offline_steps
    assert "demo-second.json" in offline_steps
    assert "first == second" in offline_steps
    assert "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE" in offline_steps
