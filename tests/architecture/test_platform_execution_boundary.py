"""Static authority checks for the signed execution boundary."""

from __future__ import annotations

import ast
from pathlib import Path

EXECUTION_ROOT = Path("src/adaptive_trader/platform/execution")
BROKER_MODULE = EXECUTION_ROOT / "broker.py"
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
