"""Inspect a declarative strategy without credentials or execution capability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from adaptive_trader.public_product.strategies import StrategyDefinition


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a paper strategy; never submit orders.")
    parser.add_argument("definition", type=Path)
    args = parser.parse_args()
    try:
        with args.definition.open("rb") as stream:
            payload = stream.read(16_385)
        if len(payload) > 16_384:
            raise ValueError("definition exceeds size limit")
        definition = StrategyDefinition.model_validate_json(payload)
    except (OSError, ValueError):
        # Validation errors can include entire caller payloads; keep this boundary redacted.
        parser.exit(2, "Strategy rejected. Use the documented v1 schema and a file under 16 KiB.\n")
    print(
        json.dumps(
            {
                "content_hash": definition.content_hash,
                "definition": definition.model_dump(mode="json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
