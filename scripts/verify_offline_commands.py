"""Exercise public offline commands in disposable state without replacing existing evidence."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("regression", "demo"))
    selected = parser.parse_args().mode
    root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="aqa-offline-verification-") as directory:
        isolated = Path(directory)
        shutil.copytree(root / "configs", isolated / "configs")
        shutil.copyfile(root / "pyproject.toml", isolated / "pyproject.toml")
        commands = (
            (
                (
                    "adaptive_trader.cli",
                    "backtest",
                    "--config",
                    "configs/backtest.yaml",
                    "--synthetic",
                ),
                ("adaptive_trader.cli", "replay", "--config", "configs/replay.yaml"),
            )
            if selected == "regression"
            else (("adaptive_trader.platform.cli", "demo", "--config-root", "configs", "--json"),)
        )
        for command in commands:
            result = subprocess.run(
                [sys.executable, str(root / "scripts/verify_no_network.py"), *command],
                cwd=isolated,
                check=False,
                timeout=300,
            )
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
