"""Default-deny outbound networking; opt-in storage tests permit one loopback port."""

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def network_boundary(monkeypatch):
    allowed = all(
        os.environ.get(key) == "YES"
        for key in (
            "APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE",
            "APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES",
            "AQA_PUBLIC_DISPOSABLE_TESTS",
        )
    )
    connect = socket.socket.connect
    resolve = socket.getaddrinfo

    def guarded_connect(sock, address):
        if not allowed or not isinstance(address, tuple) or address[:2] != ("127.0.0.1", 55438):
            raise AssertionError("Test network destination denied")
        return connect(sock, address)

    def guarded_resolve(host, port, *args, **kwargs):
        if not allowed or host not in {"127.0.0.1", b"127.0.0.1"} or str(port) != "55438":
            raise AssertionError("Test network destination denied")
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
