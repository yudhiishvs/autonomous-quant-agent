"""Static authority and separation checks for the signed platform risk boundary."""

from __future__ import annotations

import ast
from pathlib import Path

RISK_ROOT = Path("src/adaptive_trader/platform/risk")
PURE_MODULES = (
    RISK_ROOT / "statistics.py",
    RISK_ROOT / "policy.py",
    RISK_ROOT / "latches.py",
    RISK_ROOT / "models.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "adaptive_trader.broker",
    "adaptive_trader.execution",
    "adaptive_trader.risk",
    "adaptive_trader.platform.execution",
    "adaptive_trader.platform.storage",
    "alpaca",
    "requests",
    "sqlalchemy",
    "urllib",
    "websockets",
)
SHIPPED_SYMBOLS = {"AAOI", "AMD", "AXTI", "CSCO", "HLIT", "INSG", "NVDA", "SNDK"}


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


def test_signed_risk_has_no_broker_network_database_or_legacy_risk_authority() -> None:
    for path in sorted(RISK_ROOT.glob("*.py")):
        imports = _imports(_tree(path))
        assert all(
            not imported.startswith(prefix)
            for imported in imports
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        )


def test_pure_risk_modules_have_no_environment_clock_or_process_authority() -> None:
    forbidden_names = {
        "__import__",
        "compile",
        "environ",
        "eval",
        "exec",
        "getenv",
        "import_module",
        "now",
        "popen",
        "putenv",
        "random",
        "subprocess",
        "system",
        "utcnow",
    }
    forbidden_attributes = forbidden_names - {"__import__", "compile", "eval", "exec"}
    for path in PURE_MODULES:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_attributes


def test_generic_risk_code_does_not_encode_flagship_symbols() -> None:
    strings = {
        node.value
        for path in sorted(RISK_ROOT.glob("*.py"))
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    identifiers = {
        node.id
        for path in sorted(RISK_ROOT.glob("*.py"))
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Name)
    }

    assert SHIPPED_SYMBOLS.isdisjoint(strings)
    assert SHIPPED_SYMBOLS.isdisjoint(identifiers)


def test_risk_domain_records_are_frozen_slot_dataclasses() -> None:
    modules = (RISK_ROOT / "statistics.py", RISK_ROOT / "latches.py", RISK_ROOT / "models.py")
    domain_classes = {
        node.name: node
        for path in modules
        for node in _tree(path).body
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
            for decorator in node.decorator_list
        )
    }

    assert domain_classes
    for class_node in domain_classes.values():
        dataclass_decorator = next(
            decorator
            for decorator in class_node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
        )
        keywords = {
            keyword.arg: keyword.value for keyword in dataclass_decorator.keywords if keyword.arg
        }
        frozen = keywords.get("frozen")
        slots = keywords.get("slots")
        assert isinstance(frozen, ast.Constant)
        assert frozen.value is True
        assert isinstance(slots, ast.Constant)
        assert slots.value is True


def test_only_statistics_eigendecomposition_crosses_the_decimal_float_boundary() -> None:
    modules_using_float = []
    for path in sorted(RISK_ROOT.glob("*.py")):
        if any(isinstance(node, ast.Name) and node.id == "float" for node in ast.walk(_tree(path))):
            modules_using_float.append(path)

    assert modules_using_float == [RISK_ROOT / "statistics.py"]
