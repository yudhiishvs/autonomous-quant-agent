"""Static authority checks for jobs, the control API, and the dashboard."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from adaptive_trader.platform.control.models import (
    DataQualityAuditRequest,
    DatasetFreezeRequest,
    GapRepairRequest,
    OfflineDemoRequest,
    OperatorHaltRequest,
    OperatorResumeRequest,
)
from adaptive_trader.platform.jobs import JobState, JobType

JOBS_ROOT = Path("src/adaptive_trader/platform/jobs")
CONTROL_ROOT = Path("src/adaptive_trader/platform/control")
DASHBOARD_ROOT = Path("src/adaptive_trader/platform/dashboard")
OBSERVABILITY_ROOT = Path("src/adaptive_trader/platform/observability")
API_ROOT = Path("src/adaptive_trader/platform/api")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.Module, path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            package = path.relative_to("src").parent.parts
            prefix = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join((*prefix, *((node.module or "").split(".")))).strip(".")
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def test_control_runtime_has_no_dynamic_code_or_process_authority() -> None:
    forbidden_imports = {
        "cloudpickle",
        "dill",
        "importlib",
        "joblib",
        "marshal",
        "pickle",
        "subprocess",
    }
    forbidden_calls = {"__import__", "compile", "eval", "exec"}
    roots = (JOBS_ROOT, CONTROL_ROOT, API_ROOT, DASHBOARD_ROOT, OBSERVABILITY_ROOT)

    for path in (path for root in roots for path in sorted(root.rglob("*.py"))):
        tree = _tree(path)
        assert all(
            not imported.startswith(prefix)
            for imported in _imports(tree, path)
            for prefix in forbidden_imports
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls


def test_dashboard_has_no_database_broker_or_mutation_authority() -> None:
    forbidden_prefixes = (
        "alpaca",
        "sqlalchemy",
        "psycopg",
        "adaptive_trader.broker",
        "adaptive_trader.execution",
        "adaptive_trader.platform.execution",
        "adaptive_trader.platform.storage",
    )
    for path in sorted(DASHBOARD_ROOT.rglob("*.py")):
        imports = _imports(_tree(path), path)
        assert all(
            not imported.startswith(prefix) for imported in imports for prefix in forbidden_prefixes
        )
        source = path.read_text(encoding="utf-8")
        assert ".button(" not in source
        assert ".form(" not in source
        # Read-only selectors are allowed; only the client may construct HTTP requests.
        requests = [
            node
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Request"
        ]
        for request in requests:
            method = next((item.value for item in request.keywords if item.arg == "method"), None)
            assert isinstance(method, ast.Constant) and method.value == "GET"


def test_operator_controls_have_no_broker_or_order_authority() -> None:
    tree = _tree(CONTROL_ROOT / "operator.py")
    forbidden_prefixes = (
        "alpaca",
        "adaptive_trader.broker",
        "adaptive_trader.execution",
        "adaptive_trader.platform.execution",
    )

    assert all(
        not imported.startswith(prefix)
        for imported in _imports(tree, CONTROL_ROOT / "operator.py")
        for prefix in forbidden_prefixes
    )
    public_methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    assert {"submit_order", "cancel_order", "replace_order"}.isdisjoint(public_methods)


def test_api_has_no_broker_authority() -> None:
    forbidden = (
        "alpaca",
        "adaptive_trader.broker",
        "adaptive_trader.execution",
        "adaptive_trader.platform.execution",
    )
    for root in (API_ROOT, CONTROL_ROOT):
        for path in root.rglob("*.py"):
            assert all(
                not imported.startswith(prefix)
                for imported in _imports(_tree(path), path)
                for prefix in forbidden
            ), str(path)


@pytest.mark.parametrize(
    "source",
    (
        "from ..execution import broker",
        "from adaptive_trader.platform import execution",
        "import adaptive_trader.platform.execution.broker as hidden",
    ),
)
def test_api_import_scan_detects_relative_aliased_and_parent_imports(source: str) -> None:
    imports = _imports(ast.parse(source), CONTROL_ROOT / "api.py")
    assert any(name.startswith("adaptive_trader.platform.execution") for name in imports)


def test_public_mutation_models_cannot_carry_executable_or_remote_input() -> None:
    forbidden_fields = {
        "broker_credential",
        "code",
        "command",
        "environment",
        "function",
        "module",
        "path",
        "sql",
        "url",
    }
    request_models = (
        DataQualityAuditRequest,
        DatasetFreezeRequest,
        GapRepairRequest,
        OfflineDemoRequest,
        OperatorHaltRequest,
        OperatorResumeRequest,
    )

    for model in request_models:
        assert forbidden_fields.isdisjoint(model.model_fields)


def test_job_contract_is_closed_to_the_declared_states_and_types() -> None:
    assert tuple(JobType) == (
        JobType.DATA_QUALITY_AUDIT,
        JobType.GAP_REPAIR,
        JobType.DATASET_FREEZE,
        JobType.OFFLINE_DEMO,
    )
    assert tuple(JobState) == (
        JobState.PENDING,
        JobState.CLAIMED,
        JobState.RUNNING,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.DEAD,
        JobState.CANCELED,
    )
