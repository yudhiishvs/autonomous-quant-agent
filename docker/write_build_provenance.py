"""Write immutable non-secret build identity for a runtime without Git metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def write_build_provenance(root: Path, *, revision: str) -> None:
    """Bind supplied source revision to the exact lock bytes included in this image.

    A build argument cannot prove a clean checkout. Always mark the build dirty; an
    unspecified revision remains unknown and cannot support a dataset freeze.
    """
    if revision != "unknown" and re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("build source revision must be a full lowercase Git hash")
    payload = {
        "schema_version": 1,
        "source_git_commit": None if revision == "unknown" else revision,
        "dirty_worktree": True,
        "uv_lock_hash": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
    }
    destination = root / "source-provenance.json"
    with destination.open("x", encoding="ascii") as stream:
        stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    destination.chmod(0o444)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    write_build_provenance(arguments.root, revision=arguments.revision)


if __name__ == "__main__":
    main()
