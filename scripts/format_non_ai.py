"""Format repository-owned Python while leaving every protected AI file untouched."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "docs/evidence/main-ai-freeze.json").read_text())
    protected = {entry["path"] for entry in manifest["entries"]}
    paths = [
        str(path.relative_to(root))
        for directory in ("src", "tests", "scripts", "docker", "migrations")
        for path in (root / directory).rglob("*.py")
        if str(path.relative_to(root)) not in protected and not path.is_symlink()
    ]
    paths.extend(str(path.relative_to(root)) for path in root.glob("*.py"))
    for command in (
        [sys.executable, "scripts/verify_main_ai_freeze.py"],
        [sys.executable, "-m", "ruff", "format", *sorted(paths)],
        [sys.executable, "scripts/verify_main_ai_freeze.py"],
    ):
        result = subprocess.run(command, cwd=root, check=False, timeout=120)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
