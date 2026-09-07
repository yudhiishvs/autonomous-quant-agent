"""Detect protected-byte, manifest, dependency and inventory drift before delivery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_main_ai_freeze import verify


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def frozen_tree(tmp_path: Path) -> Path:
    (tmp_path / "docs/evidence").mkdir(parents=True)
    (tmp_path / "src/ai").mkdir(parents=True)
    model = tmp_path / "src/ai/model.py"
    model.write_text("immutable_model = 1\n")
    dependency = tmp_path / "uv.lock"
    dependency.write_text("version = 1\n")
    manifest = tmp_path / "docs/evidence/main-ai-freeze.json"
    manifest.write_text(
        json.dumps({"entries": [{"path": "src/ai/model.py", "sha256": _digest(model)}]})
    )
    (tmp_path / "docs/evidence/non-ai-freeze-baseline.json").write_text(
        json.dumps(
            {
                "protected_manifest_sha256": _digest(manifest),
                "dependency_files": {"uv.lock": _digest(dependency)},
                "protected_inventory_roots": ["src/ai"],
                "protected_inventory": ["src/ai/model.py"],
            }
        )
    )
    return tmp_path


def test_unchanged_baseline_passes(frozen_tree: Path) -> None:
    assert verify(frozen_tree) == ()


@pytest.mark.parametrize(
    "boundary", ["bytes", "removed", "symlink", "dependency", "inventory", "manifest"]
)
def test_freeze_rejects_drift(frozen_tree: Path, boundary: str) -> None:
    model = frozen_tree / "src/ai/model.py"
    if boundary == "bytes":
        model.write_text("changed = True\n")
    elif boundary == "removed":
        model.unlink()
    elif boundary == "symlink":
        copy = frozen_tree / "copy.py"
        copy.write_bytes(model.read_bytes())
        model.unlink()
        model.symlink_to(copy)
    elif boundary == "dependency":
        (frozen_tree / "uv.lock").write_text("version = 2\n")
    elif boundary == "inventory":
        (frozen_tree / "src/ai/new_model.py").write_text("x = 1\n")
    else:
        manifest = frozen_tree / "docs/evidence/main-ai-freeze.json"
        manifest.write_text('{"entries": []}\n')
    assert verify(frozen_tree)
