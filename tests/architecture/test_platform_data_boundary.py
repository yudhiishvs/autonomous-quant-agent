"""Static authority checks for pure canonical market-data transformations."""

from __future__ import annotations

import ast
from pathlib import Path

DATA_MODULES = (
    Path("src/adaptive_trader/platform/data/normalization.py"),
    Path("src/adaptive_trader/platform/data/aggregation.py"),
)
DATASET_MODULE = Path("src/adaptive_trader/platform/data/datasets.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "adaptive_trader.broker",
    "adaptive_trader.execution",
    "adaptive_trader.platform.storage",
    "alpaca",
    "requests",
    "sqlalchemy",
    "websockets",
)
SHIPPED_EXPERIMENT_SYMBOLS = {
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


def test_market_data_transformations_have_no_network_broker_or_storage_authority() -> None:
    for path in DATA_MODULES:
        imports = _imports(_tree(path))
        assert all(
            not imported.startswith(prefix)
            for imported in imports
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        )


def test_dataset_freeze_has_no_network_broker_or_database_authority() -> None:
    imports = _imports(_tree(DATASET_MODULE))

    assert all(
        not imported.startswith(prefix)
        for imported in imports
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def test_generic_market_data_algorithms_do_not_encode_shipped_experiment_symbols() -> None:
    for path in DATA_MODULES:
        strings = {
            node.value
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert all(
            symbol not in literal for symbol in SHIPPED_EXPERIMENT_SYMBOLS for literal in strings
        )


def test_normalizer_exposes_one_transport_independent_entry_point() -> None:
    tree = _tree(DATA_MODULES[0])
    public_normalizers = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("normalize_")
    }

    assert public_normalizers == {"normalize_alpaca_bar", "normalize_fixture_bar"}


def test_aggregation_is_a_synchronous_pure_transformation() -> None:
    tree = _tree(DATA_MODULES[1])
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    aggregate = functions["aggregate_one_minute_bars"]
    assert isinstance(aggregate, ast.FunctionDef)
    assert all(
        not isinstance(node, (ast.Await, ast.Global, ast.Nonlocal)) for node in ast.walk(aggregate)
    )
