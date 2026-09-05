"""Static dependency checks for pure generic platform domain modules."""

from __future__ import annotations

import ast
from pathlib import Path

MODULE_IMPORTS = {
    Path("src/adaptive_trader/platform/constants.py"): {"__future__", "typing"},
    Path("src/adaptive_trader/platform/domain.py"): {
        "__future__",
        "adaptive_trader.platform.constants",
        "adaptive_trader.platform.errors",
        "adaptive_trader.platform.hashing",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "re",
    },
    Path("src/adaptive_trader/platform/errors.py"): {"__future__"},
}
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


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def test_domain_modules_have_only_pure_dependency_authority() -> None:
    for module, expected_imports in MODULE_IMPORTS.items():
        assert module.is_file()
        assert _imports(module) == expected_imports


def test_domain_modules_do_not_encode_the_shipped_experiment() -> None:
    for module in MODULE_IMPORTS:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert all(symbol not in literal for symbol in EXPERIMENT_SYMBOLS for literal in strings)
