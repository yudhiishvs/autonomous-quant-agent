"""Verify protected AI bytes, inventory and dependency selections without rewriting them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root: Path) -> tuple[str, ...]:
    """Return concrete violations of the independently recorded working-tree baseline."""

    manifest_path = root / "docs/evidence/main-ai-freeze.json"
    baseline = json.loads((root / "docs/evidence/non-ai-freeze-baseline.json").read_text())
    manifest = json.loads(manifest_path.read_text())
    changed: list[str] = []
    if _digest(manifest_path) != baseline["protected_manifest_sha256"]:
        changed.append("protected manifest changed")
    for entry in manifest["entries"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("protected manifest paths must stay within the repository")
        path = root / relative
        if not path.is_file() or path.is_symlink() or _digest(path) != entry["sha256"]:
            changed.append(str(relative))
    for name, expected in baseline["dependency_files"].items():
        path = root / name
        if path.is_symlink() or not path.is_file() or _digest(path) != expected:
            changed.append(f"dependency selection changed: {name}")
    inventory = sorted(
        str(path.relative_to(root))
        for name in baseline["protected_inventory_roots"]
        for path in (root / name).rglob("*")
        if (path.is_file() or path.is_symlink()) and "__pycache__" not in path.parts
    )
    if inventory != baseline["protected_inventory"]:
        changed.append("protected AI inventory changed")
    return tuple(changed)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    changed = verify(root)
    if changed:
        print("AI FREEZE VIOLATION: " + ", ".join(changed))
        return 1
    count = len(json.loads((root / "docs/evidence/main-ai-freeze.json").read_text())["entries"])
    print(f"AI freeze verified: {count} protected files, inventory and dependencies unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
