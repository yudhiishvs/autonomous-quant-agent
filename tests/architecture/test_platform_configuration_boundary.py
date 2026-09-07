"""Static authority boundaries for generic experiment configuration."""

from __future__ import annotations

import ast
from pathlib import Path

CONFIG_MODULE = Path("src/adaptive_trader/platform/config.py")
CLI_MODULE = Path("src/adaptive_trader/platform/cli.py")
SECURITY_MODULE = Path("src/adaptive_trader/platform/security.py")
UNIVERSE_MODULE = Path("src/adaptive_trader/platform/universe.py")
ALLOWED_IMPORTS = {
    CLI_MODULE: {
        "__future__",
        "adaptive_trader.collection.cli",
        "adaptive_trader.platform.config",
        "adaptive_trader.platform.data.calendar",
        "adaptive_trader.platform.data_cli",
        "adaptive_trader.platform.demo",
        "adaptive_trader.platform.domain",
        "adaptive_trader.platform.durable_status",
        "adaptive_trader.platform.errors",
        "adaptive_trader.platform.package_resources",
        "adaptive_trader.platform.operational_strategy",
        "adaptive_trader.platform.runtime",
        "adaptive_trader.platform.scheduling",
        "adaptive_trader.platform.shadow",
        "adaptive_trader.platform.shadow_settings",
        "adaptive_trader.platform.security",
        "adaptive_trader.platform.storage",
        "adaptive_trader.platform.storage.engine",
        "adaptive_trader.platform.storage.migration_runner",
        "datetime",
        "json",
        "os",
        "pathlib",
        "typer",
        "typing",
    },
    CONFIG_MODULE: {
        "__future__",
        "adaptive_trader.platform.canonical",
        "adaptive_trader.platform.constants",
        "adaptive_trader.platform.errors",
        "adaptive_trader.platform.hashing",
        "adaptive_trader.platform.security",
        "adaptive_trader.platform.universe",
        "collections.abc",
        "decimal",
        "enum",
        "hmac",
        "os",
        "pathlib",
        "pydantic",
        "pydantic.json_schema",
        "re",
        "stat",
        "typing",
        "urllib.parse",
        "yaml",
        "yaml.constructor",
        "yaml.events",
        "yaml.nodes",
    },
    SECURITY_MODULE: {
        "__future__",
        "adaptive_trader.platform.errors",
        "dataclasses",
        "enum",
        "fcntl",
        "os",
        "pathlib",
        "pydantic",
        "pydantic.json_schema",
        "pydantic_core",
        "secrets",
        "stat",
        "threading",
        "typing",
    },
    UNIVERSE_MODULE: {"__future__", "enum", "pydantic", "re", "typing"},
}
SHIPPED_SYMBOLS = {
    "AAPL",
    "AAOI",
    "AMD",
    "AMZN",
    "AXTI",
    "BOX",
    "CSCO",
    "GOOGL",
    "HLIT",
    "INSG",
    "LCID",
    "META",
    "NET",
    "NVDA",
    "OKTA",
    "PAYC",
    "PUBM",
    "QQQ",
    "RBLX",
    "RIVN",
    "ROKU",
    "SNDK",
    "SOUN",
    "SOXX",
    "SPY",
    "TSLA",
    "UBER",
    "WDAY",
    "ZG",
}


def _source(project_root: Path, relative_path: Path) -> str:
    return (project_root / relative_path).read_text(encoding="utf-8")


def test_generic_configuration_has_no_provider_or_broker_network_authority(
    project_root: Path,
) -> None:
    for relative_path, allowed_imports in ALLOWED_IMPORTS.items():
        tree = ast.parse(_source(project_root, relative_path), filename=str(relative_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = (node.module or "",)
            else:
                continue
            assert set(imported) <= allowed_imports


def test_generic_configuration_has_no_environment_or_process_authority(project_root: Path) -> None:
    prohibited_names = {
        "__import__",
        "compile",
        "environ",
        "eval",
        "exec",
        "getenv",
        "import_module",
        "popen",
        "putenv",
        "spawn",
        "subprocess",
        "system",
    }
    prohibited_attributes = prohibited_names - {"__import__", "compile", "eval", "exec"}

    for relative_path in ALLOWED_IMPORTS:
        tree = ast.parse(_source(project_root, relative_path), filename=str(relative_path))
        # Only the two operational CLI entrypoints snapshot ambient values for the
        # strict service-scoped settings loaders. Configuration/domain modules retain
        # no environment authority, and direct getenv/process access stays prohibited.
        environment_snapshots = set()
        if relative_path == CLI_MODULE:
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name in {
                    "shadow_propose_once",
                    "shadow_run_once",
                }:
                    for call in ast.walk(function):
                        if (
                            isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "dict"
                            and len(call.args) == 1
                            and not call.keywords
                            and isinstance(call.args[0], ast.Attribute)
                            and isinstance(call.args[0].value, ast.Name)
                            and call.args[0].value.id == "os"
                            and call.args[0].attr == "environ"
                        ):
                            environment_snapshots.add(id(call.args[0]))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in prohibited_attributes or id(node) in environment_snapshots
            elif isinstance(node, ast.Name):
                assert node.id not in prohibited_names
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                assert all(alias.name not in prohibited_names for alias in node.names)


def test_flagship_symbols_live_outside_generic_platform(
    project_root: Path,
) -> None:
    platform_root = project_root / "src" / "adaptive_trader" / "platform"
    generic_source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(platform_root.rglob("*.py"))
    )
    identifiers = {
        node.id for node in ast.walk(ast.parse(generic_source)) if isinstance(node, ast.Name)
    }
    string_literals = {
        node.value
        for node in ast.walk(ast.parse(generic_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert SHIPPED_SYMBOLS.isdisjoint(identifiers)
    assert SHIPPED_SYMBOLS.isdisjoint(string_literals)
