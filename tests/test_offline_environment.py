"""Regression checks for the ordinary test process safety boundary."""

from __future__ import annotations

import os
import socket

import pytest

_AMBIENT_AUTHORITY_ENV_NAMES = (
    "APCA_API_BASE_URL",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "APA_ALPACA_DATA_API_KEY",
    "APA_ALPACA_DATA_SECRET_KEY",
    "APA_ALPACA_PAPER_API_KEY",
    "APA_ALPACA_PAPER_SECRET_KEY",
    "APA_MARKET_DATA_DATABASE_URL",
    "APA_MARKET_DATA_MIGRATION_DATABASE_URL",
    "AQA_ALPACA_DATA_API_KEY_FILE",
    "AQA_ALPACA_DATA_SECRET_KEY_FILE",
    "AQA_ALPACA_PAPER_API_KEY_FILE",
    "AQA_ALPACA_PAPER_SECRET_KEY_FILE",
    "AQA_DATABASE_URL_FILE",
    "AQA_OPERATOR_TOKEN_FILE",
    "AQA_PAPER_ACCOUNT_ID_HASH_FILE",
)
_AUTHORITY_VALUES_AT_IMPORT = {
    variable_name: os.environ.get(variable_name) for variable_name in _AMBIENT_AUTHORITY_ENV_NAMES
}
_PAPER_ACKNOWLEDGEMENTS_AT_IMPORT = {
    "APA_ENABLE_PAPER_ORDERS": os.environ.get("APA_ENABLE_PAPER_ORDERS"),
    "AQA_ENABLE_PAPER_ORDERS": os.environ.get("AQA_ENABLE_PAPER_ORDERS"),
}


def test_ordinary_tests_have_no_ambient_alpaca_authority() -> None:
    assert set(_AUTHORITY_VALUES_AT_IMPORT.values()) == {None}
    assert set(_PAPER_ACKNOWLEDGEMENTS_AT_IMPORT.values()) == {"NO"}

    for variable_name in _AMBIENT_AUTHORITY_ENV_NAMES:
        assert variable_name not in os.environ
    assert os.environ["APA_ENABLE_PAPER_ORDERS"] == "NO"
    assert os.environ["AQA_ENABLE_PAPER_ORDERS"] == "NO"


def test_ordinary_tests_deny_dns_and_socket_network_paths() -> None:
    with pytest.raises(AssertionError, match="External network access is prohibited"):
        socket.getaddrinfo("example.invalid", 443)
    with socket.socket() as candidate:
        with pytest.raises(AssertionError, match="External network access is prohibited"):
            candidate.connect_ex(("127.0.0.1", 443))
        with pytest.raises(AssertionError, match="External network access is prohibited"):
            candidate.sendto(b"probe", ("127.0.0.1", 443))
