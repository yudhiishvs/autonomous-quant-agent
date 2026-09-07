"""Static authority checks for the signed execution boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

EXECUTION_ROOT = Path("src/adaptive_trader/platform/execution")
BROKER_MODULE = EXECUTION_ROOT / "broker.py"
ALPACA_PAPER_MODULE = EXECUTION_ROOT / "alpaca_paper.py"
SIGNAL_ROOT = Path("src/adaptive_trader/platform/signals")
FORBIDDEN_TRANSPORT_IMPORTS = (
    "alpaca",
    "httpx",
    "requests",
    "socket",
    "urllib",
    "websocket",
    "websockets",
)
SHIPPED_SYMBOLS = {
    "AAOI",
    "AMD",
    "AXTI",
    "CSCO",
    "HLIT",
    "INSG",
    "NVDA",
    "SNDK",
    "SOXX",
    "QQQ",
    "SPY",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def test_execution_broker_boundary_has_no_concrete_network_transport() -> None:
    imports = _imports(_tree(BROKER_MODULE))
    source = BROKER_MODULE.read_text(encoding="utf-8")

    assert all(
        not imported.startswith(prefix)
        for imported in imports
        for prefix in FORBIDDEN_TRANSPORT_IMPORTS
    )
    assert "http://" not in source
    assert "https://" not in source


def test_only_fixed_paper_factory_imports_alpaca_trading_sdk() -> None:
    for path in sorted(EXECUTION_ROOT.glob("*.py")):
        imports = _imports(_tree(path))
        has_alpaca_sdk = any(imported.startswith("alpaca.trading") for imported in imports)
        assert has_alpaca_sdk is (path == ALPACA_PAPER_MODULE)

    tree = _tree(ALPACA_PAPER_MODULE)
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TradingClient"
    ]
    assert len(constructors) == 1
    keywords = {keyword.arg: keyword.value for keyword in constructors[0].keywords}
    paper_value = keywords.get("paper")
    assert isinstance(paper_value, ast.Constant)
    assert paper_value.value is True
    assert "url_override" not in keywords
    assert "paper=False" not in ALPACA_PAPER_MODULE.read_text(encoding="utf-8")


def test_signal_authority_cannot_import_execution() -> None:
    for path in sorted(SIGNAL_ROOT.glob("*.py")):
        imports = _imports(_tree(path))
        assert all(
            not imported.startswith("adaptive_trader.platform.execution") for imported in imports
        )


def test_generic_execution_code_does_not_encode_shipped_symbols() -> None:
    strings = {
        node.value
        for path in sorted(EXECUTION_ROOT.glob("*.py"))
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert SHIPPED_SYMBOLS.isdisjoint(strings)


def test_pure_planning_modules_have_no_clock_environment_or_io_authority() -> None:
    forbidden_names = {
        "environ",
        "getenv",
        "now",
        "open",
        "popen",
        "putenv",
        "random",
        "subprocess",
        "system",
        "utcnow",
    }
    for name in ("models.py", "planner.py", "state_machine.py"):
        tree = _tree(EXECUTION_ROOT / name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_names


def _execution_forbidden_imports(source: str, path: Path) -> set[str]:
    forbidden = (
        "importlib",
        "pkg_resources",
        "pickle",
        "cloudpickle",
        "dill",
        "joblib",
        "xgboost",
        "sklearn",
        "torch",
        "tensorflow",
        "openai",
        "anthropic",
        "adaptive_trader.models",
        "adaptive_trader.features",
        "adaptive_trader.strategies",
        "adaptive_trader.decision_engine",
        "adaptive_trader.platform.signals.providers",
    )
    imports: set[str] = set()
    package = path.relative_to("src").parent.parts
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join((*prefix, *((node.module or "").split(".")))).strip(".")
            imports.add(module)
            imports.update(f"{module}.{alias.name}" for alias in node.names)
    return {name for name in imports if name.startswith(forbidden)}


def test_execution_and_worker_entrypoints_cannot_load_ai_or_plugins() -> None:
    paths = (
        *EXECUTION_ROOT.rglob("*.py"),
        Path("src/adaptive_trader/platform/worker_runtime.py"),
        Path("src/adaptive_trader/platform/paper_gate_runtime.py"),
    )
    forbidden_calls = {
        "__import__",
        "eval",
        "exec",
        "compile",
        "entry_points",
        "import_module",
        "discover",
        "discover_entry_points",
        "load_model",
        "load_checkpoint",
    }
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert not _execution_forbidden_imports(source, path), str(path)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else (function.attr if isinstance(function, ast.Attribute) else "")
                )
                if isinstance(function, ast.Attribute) and name == "compile":
                    assert isinstance(function.value, ast.Name) and function.value.id == "re"
                    continue
                assert name not in forbidden_calls, str(path)


@pytest.mark.parametrize(
    "source",
    (
        "from ..signals import providers",
        "from adaptive_trader import models",
        "import importlib.metadata as registry",
        "from xgboost import Booster",
    ),
)
def test_execution_import_guard_detects_forbidden_capabilities(source: str) -> None:
    assert _execution_forbidden_imports(source, EXECUTION_ROOT / "service.py")


def test_planning_and_storage_imports_do_not_load_broker_capabilities() -> None:
    import subprocess
    import sys

    script = """
import sys
from adaptive_trader.platform.execution import plan_signed_orders, ReconciliationRequest
from adaptive_trader.platform.storage.execution import SignedExecutionRepository
assert 'adaptive_trader.platform.execution.broker' not in sys.modules
assert 'adaptive_trader.platform.execution.alpaca_paper' not in sys.modules
assert 'alpaca.trading.client' not in sys.modules
from adaptive_trader.platform.execution import DeterministicFakePaperBroker
from adaptive_trader.platform.execution.broker import DeterministicFakePaperBroker as concrete
assert DeterministicFakePaperBroker is concrete
from adaptive_trader.platform import execution
for name in execution.__all__:
    assert getattr(execution, name) is not None
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
