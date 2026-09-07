"""Build and exercise a locked wheel installation outside the source checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="aqa-package-proof-") as directory:
        temporary = Path(directory)
        distribution = temporary / "dist"
        requirements = temporary / "requirements.txt"
        environment = temporary / "venv"
        run_directory = temporary / "outside-checkout"
        run_directory.mkdir()
        commands = (
            ["uv", "build", "--out-dir", str(distribution)],
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-editable",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            ["uv", "venv", str(environment), "--python", "3.11"],
            ["uv", "pip", "sync", "--python", str(environment / "bin/python"), str(requirements)],
        )
        safe_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AQA_", "APA_", "APCA_", "ALPACA_", "PYTHONPATH"))
        }
        for command in commands:
            subprocess.run(command, cwd=root, env=safe_environment, check=True, timeout=600)
        wheels = tuple(distribution.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("build did not produce exactly one wheel")
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(environment / "bin/python"),
                "--no-deps",
                str(wheels[0]),
            ],
            cwd=root,
            env=safe_environment,
            check=True,
            timeout=120,
        )
        for command in (
            [str(environment / "bin/python"), str(root / "scripts/verify_installed_package.py")],
            [str(environment / "bin/aqa"), "doctor", "--json"],
            [
                str(environment / "bin/python"),
                str(root / "scripts/verify_no_network.py"),
                "adaptive_trader.platform.cli",
                "demo",
                "--json",
                "--output",
                "evidence.json",
            ],
        ):
            subprocess.run(
                command, cwd=run_directory, env=safe_environment, check=True, timeout=120
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
