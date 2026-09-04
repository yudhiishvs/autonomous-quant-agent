"""Regression checks for the ordinary test process safety boundary."""

from __future__ import annotations

import os

_ALPACA_CREDENTIAL_ENV_NAMES = (
    "APA_ALPACA_DATA_API_KEY",
    "APA_ALPACA_DATA_SECRET_KEY",
    "APA_ALPACA_PAPER_API_KEY",
    "APA_ALPACA_PAPER_SECRET_KEY",
)
_CREDENTIAL_VALUES_AT_IMPORT = {
    variable_name: os.environ.get(variable_name) for variable_name in _ALPACA_CREDENTIAL_ENV_NAMES
}
_PAPER_ACKNOWLEDGEMENT_AT_IMPORT = os.environ.get("APA_ENABLE_PAPER_ORDERS")


def test_ordinary_tests_have_no_ambient_alpaca_authority() -> None:
    assert set(_CREDENTIAL_VALUES_AT_IMPORT.values()) == {None}
    assert _PAPER_ACKNOWLEDGEMENT_AT_IMPORT == "NO"

    for variable_name in _ALPACA_CREDENTIAL_ENV_NAMES:
        assert variable_name not in os.environ
    assert os.environ["APA_ENABLE_PAPER_ORDERS"] == "NO"
