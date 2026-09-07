"""Build the package with its immutable runtime configuration and migration assets."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

_PROJECT_ROOT = Path(__file__).resolve().parent
_RESOURCE_DESTINATION = Path("adaptive_trader/platform/_resources")


class BuildPyWithPlatformResources(build_py):
    """Copy non-Python runtime assets into the installed resource package."""

    def run(self) -> None:
        super().run()
        destination = Path(self.build_lib) / _RESOURCE_DESTINATION
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_PROJECT_ROOT / "alembic.ini", destination / "alembic.ini")
        for source_name in ("configs", "migrations"):
            target = destination / source_name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(
                _PROJECT_ROOT / source_name,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )


setup(cmdclass={"build_py": BuildPyWithPlatformResources})
