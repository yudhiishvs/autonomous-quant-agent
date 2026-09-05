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
        "adaptive_trader.platform.config",
        "adaptive_trader.platform.security",
        "json",
        "os",
        "pathlib",
        "typer",
        "typing",
    },
    CONFIG_MODULE: {
        "__future__",
        "adaptive_trader.platform.canonical",
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in prohibited_attributes
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
