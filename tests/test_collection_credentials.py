"""Tests for the isolated Alpaca market-data credential boundary."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from adaptive_trader.collection.credentials import (
    ALPACA_DATA_API_KEY_ENV,
    ALPACA_DATA_SECRET_KEY_ENV,
    AlpacaDataCredentialError,
    AlpacaDataCredentials,
)


def test_data_credentials_read_only_dedicated_environment_names() -> None:
    credentials = AlpacaDataCredentials.from_environment(
        {
            ALPACA_DATA_API_KEY_ENV: " data-key ",
            ALPACA_DATA_SECRET_KEY_ENV: " data-secret ",
            "APA_ALPACA_PAPER_API_KEY": "unrelated-key",
            "APA_ALPACA_PAPER_SECRET_KEY": "unrelated-secret",
        }
    )

    assert credentials.api_key == "data-key"
    assert credentials.secret_key == "data-secret"


def test_unrelated_alpaca_environment_names_cannot_supply_data_credentials() -> None:
    with pytest.raises(AlpacaDataCredentialError, match=ALPACA_DATA_API_KEY_ENV):
        AlpacaDataCredentials.from_environment(
            {
                "APCA_API_KEY_ID": "other-key",
                "APCA_API_SECRET_KEY": "other-secret",
                "APA_ALPACA_PAPER_API_KEY": "paper-key",
                "APA_ALPACA_PAPER_SECRET_KEY": "paper-secret",
            }
        )


@pytest.mark.parametrize(
    ("environment", "missing_name"),
    [
        ({ALPACA_DATA_SECRET_KEY_ENV: "secret"}, ALPACA_DATA_API_KEY_ENV),
        ({ALPACA_DATA_API_KEY_ENV: "key"}, ALPACA_DATA_SECRET_KEY_ENV),
        (
            {
                ALPACA_DATA_API_KEY_ENV: "   ",
                ALPACA_DATA_SECRET_KEY_ENV: "secret",
            },
            ALPACA_DATA_API_KEY_ENV,
        ),
    ],
)
def test_data_credentials_reject_missing_or_blank_values(
    environment: dict[str, str],
    missing_name: str,
) -> None:
    with pytest.raises(AlpacaDataCredentialError, match=missing_name):
        AlpacaDataCredentials.from_environment(environment)


def test_data_credentials_never_render_secret_material() -> None:
    credentials = AlpacaDataCredentials("key-material-123", "secret-material-456")

    for rendered in (repr(credentials), str(credentials)):
        assert "key-material-123" not in rendered
        assert "secret-material-456" not in rendered
        assert rendered == "AlpacaDataCredentials(api_key=<redacted>, secret_key=<redacted>)"

    message = credentials.redact(
        "request key-material-123 failed with secret-material-456; secret-material-456"
    )
    assert "key-material-123" not in message
    assert "secret-material-456" not in message
    assert message.count("<redacted>") == 3


def test_redaction_handles_one_credential_that_prefixes_the_other() -> None:
    credentials = AlpacaDataCredentials("shared-prefix", "shared-prefix-longer")

    rendered = credentials.redact("shared-prefix-longer and shared-prefix")

    assert "shared-prefix" not in rendered
    assert rendered == "<redacted> and <redacted>"


def test_collection_package_has_no_trading_or_legacy_broker_imports() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "adaptive_trader" / "collection"
    forbidden_modules = {
        "alpaca.trading",
        "adaptive_trader.broker",
        "adaptive_trader.execution",
        "adaptive_trader.market_data_live",
    }
    forbidden_names = {"PaperCredentials", "TradingClient", "TradingStream"}

    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    not any(alias.name.startswith(module) for module in forbidden_modules)
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                imported_module = node.module or ""
                assert not any(imported_module.startswith(module) for module in forbidden_modules)
                assert forbidden_names.isdisjoint(alias.name for alias in node.names)


def test_collection_cli_clean_import_loads_no_external_alpaca_package() -> None:
    program = """
import sys
import adaptive_trader.collection.cli

loaded = sorted(
    name for name in sys.modules
    if name == "alpaca" or name.startswith("alpaca.")
)
if loaded:
    print("Unexpected external Alpaca modules:", loaded)
    raise SystemExit(1)
"""

    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
