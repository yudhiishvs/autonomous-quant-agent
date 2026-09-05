"""Static authority checks for the generic platform persistence boundary."""

from __future__ import annotations

import ast
from pathlib import Path

STORAGE_ROOT = Path("src/adaptive_trader/platform/storage")
MIGRATION = Path("migrations/versions/20260905_0002_platform_foundation.py")
PROHIBITED_IMPORT_PREFIXES = (
    "adaptive_trader.broker",
    "adaptive_trader.collection",
    "adaptive_trader.execution",
    "adaptive_trader.market_data_live",
    "alpaca",
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "urllib",
    "websockets",
)


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return tuple(imported)


def test_storage_has_no_provider_broker_or_arbitrary_network_authority() -> None:
    modules = tuple(sorted(STORAGE_ROOT.glob("*.py")))
    assert modules

    for module in modules:
        for imported in _imports(module):
            assert not imported.startswith(PROHIBITED_IMPORT_PREFIXES), (
                f"{module} imports prohibited authority {imported}"
            )


def test_operational_schema_is_owned_only_by_alembic() -> None:
    production_sources = (*sorted(STORAGE_ROOT.glob("*.py")), MIGRATION)

    for path in production_sources:
        source = path.read_text(encoding="utf-8")
        assert ".create_all(" not in source, path
        assert ".drop_all(" not in source, path

    migration_tree = ast.parse(MIGRATION.read_text(encoding="utf-8"), filename=str(MIGRATION))
    downgrade = next(
        node
        for node in migration_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
    )
    assert any(isinstance(node, ast.Raise) for node in ast.walk(downgrade))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("drop_")
        for node in ast.walk(downgrade)
    )
