from __future__ import annotations

import socket

import pytest

from scripts.verify_no_network import NetworkAccessDenied, deny_outbound_network


def test_network_guard_blocks_dns_and_socket_connections() -> None:
    with deny_outbound_network():
        with pytest.raises(NetworkAccessDenied, match="outbound network access is disabled"):
            socket.getaddrinfo("example.invalid", 443)
        with (
            socket.socket() as candidate,
            pytest.raises(NetworkAccessDenied, match="outbound network access is disabled"),
        ):
            candidate.connect(("127.0.0.1", 443))


def test_network_guard_restores_socket_functions() -> None:
    original = socket.getaddrinfo
    with deny_outbound_network():
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original
