"""Smoke-test the installed wheel's public imports and embedded runtime resources."""

from __future__ import annotations

import json
from pathlib import Path

from adaptive_trader.platform.api import create_control_app
from adaptive_trader.platform.api.app import create_control_app as app_factory
from adaptive_trader.platform.api.auth import OperatorTokenAuthenticator
from adaptive_trader.platform.api.schemas import StrictApiModel
from adaptive_trader.platform.config import load_platform_config
from adaptive_trader.platform.control.auth import OperatorTokenAuthenticator as ControlAuthenticator
from adaptive_trader.platform.control.models import StrictApiModel as ControlApiModel
from adaptive_trader.platform.package_resources import packaged_config_root
from adaptive_trader.platform.storage.migration_runner import platform_migration_head


def main() -> None:
    """Require the installed package to be operational without repository-relative files."""

    config_root = packaged_config_root()
    config = load_platform_config(Path("platform/offline.yaml"), config_root=config_root)
    if create_control_app is not app_factory:
        raise RuntimeError("public API application factory identity changed")
    if (
        OperatorTokenAuthenticator is not ControlAuthenticator
        or StrictApiModel is not ControlApiModel
    ):
        raise RuntimeError("public API compatibility surface is unavailable")
    payload = {
        "config_hash": config.content_hash,
        "experiment_hash": config.experiment.definition_hash,
        "migration_head": platform_migration_head(),
        "status": "ok",
    }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
