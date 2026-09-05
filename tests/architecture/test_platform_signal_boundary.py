"""Static checks for the scheduler and strategy extension authority boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

SCHEDULING_MODULES = tuple(Path("src/adaptive_trader/platform/scheduling").glob("*.py"))
SIGNAL_MODULES = tuple(Path("src/adaptive_trader/platform/signals").glob("*.py"))
FORBIDDEN_IMPORTS = (
    "adaptive_trader.broker",
    "adaptive_trader.execution",
    "adaptive_trader.platform.data.credentials",
    "alpaca",
    "requests",
    "websockets",
)
EXPERIMENT_SYMBOLS = {
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


def test_scheduler_and_signal_modules_have_no_broker_network_or_credential_authority() -> None:
    for path in (*SCHEDULING_MODULES, *SIGNAL_MODULES):
        imports = _imports(_tree(path))
        assert all(
            not imported.startswith(prefix) for imported in imports for prefix in FORBIDDEN_IMPORTS
        )


def test_generic_provider_implementation_does_not_hard_code_experiment_symbols() -> None:
    tree = _tree(Path("src/adaptive_trader/platform/signals/providers.py"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert all(symbol not in literal for symbol in EXPERIMENT_SYMBOLS for literal in literals)


def test_provider_discovery_uses_only_the_fixed_entry_point_group() -> None:
    source = Path("src/adaptive_trader/platform/signals/providers.py").read_text(encoding="utf-8")

    assert 'SIGNAL_PROVIDER_ENTRY_POINT_GROUP = "autonomous_quant_agent.signal_providers"' in source
    assert "import_module" not in source
    assert "module_path" not in source
    assert "class_path" not in source
    tree = ast.parse(source)
    assert all(not isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(tree))
