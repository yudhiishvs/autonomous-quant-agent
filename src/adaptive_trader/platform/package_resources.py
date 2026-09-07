"""Locate immutable platform resources in source and installed distributions."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Final, cast

_RESOURCE_PACKAGE: Final = "adaptive_trader.platform._resources"
_SOURCE_ROOT_DEPTH: Final = 3
_CONFIG_SENTINEL: Final = Path("platform/offline.yaml")
_MIGRATION_SENTINEL: Final = Path("versions/20260903_0001_market_data_foundation.py")


class PlatformResourceError(RuntimeError):
    """A required build-installed platform resource is unavailable or malformed."""


def _filesystem_resource_root() -> Path | None:
    """Return the installed resource package when it has filesystem-backed assets."""

    traversable = files(_RESOURCE_PACKAGE)
    if not isinstance(traversable, os.PathLike):
        return None
    try:
        resource_path = cast(os.PathLike[str], traversable)
        candidate = Path(os.fspath(resource_path)).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _source_checkout_root() -> Path | None:
    """Return the repository root only for an editable source checkout."""

    try:
        candidate = Path(__file__).resolve(strict=True).parents[_SOURCE_ROOT_DEPTH]
    except (IndexError, OSError, RuntimeError):
        return None
    if not (candidate / "pyproject.toml").is_file():
        return None
    return candidate


def _resource_directory(
    installed_name: str,
    *,
    source_name: str,
    sentinel: Path,
) -> Path:
    installed_root = _filesystem_resource_root()
    if installed_root is not None:
        installed = installed_root / installed_name
        if installed.is_dir() and (installed / sentinel).is_file():
            return installed.resolve(strict=True)

    source_root = _source_checkout_root()
    if source_root is not None:
        source = source_root / source_name
        if source.is_dir() and (source / sentinel).is_file():
            return source.resolve(strict=True)

    raise PlatformResourceError(f"required platform {installed_name} resources are unavailable")


def packaged_config_root() -> Path:
    """Return the trusted filesystem root containing the shipped platform profiles."""

    return _resource_directory(
        "configs",
        source_name="configs",
        sentinel=_CONFIG_SENTINEL,
    )


def packaged_migration_root() -> Path:
    """Return the trusted filesystem root containing the shipped Alembic environment."""

    return _resource_directory(
        "migrations",
        source_name="migrations",
        sentinel=_MIGRATION_SENTINEL,
    )


def packaged_alembic_ini() -> Path:
    """Return the shipped Alembic configuration from the installed resource bundle."""

    installed_root = _filesystem_resource_root()
    if installed_root is not None:
        installed = installed_root / "alembic.ini"
        if installed.is_file():
            return installed.resolve(strict=True)

    source_root = _source_checkout_root()
    if source_root is not None:
        source = source_root / "alembic.ini"
        if source.is_file():
            return source.resolve(strict=True)

    raise PlatformResourceError("required platform Alembic configuration is unavailable")
