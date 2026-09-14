"""Default-deny outbound networking; opt-in storage tests permit one loopback port."""

import os
import socket

import pytest
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from cryptography.fernet import Fernet

from aqa_public.settings import Settings


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


@pytest.fixture
def settings(tmp_path):
    def secret(name, value, source):
        path = tmp_path / name
        path.write_text(value)
        path.chmod(0o600)
        return load_secret_file(path, source=source)

    return Settings(
        origin="http://127.0.0.1:5178",
        issuer="http://127.0.0.1:8188/realms/paper",
        client_id="paper-web",
        development=True,
        database_url=secret("db", "postgresql+psycopg://unused", SecretFileVariable.DATABASE_URL),
        client_secret=secret(
            "oidc",
            "SYNTHETIC-ONLY-NO-EXTERNAL-AUTHORITY",
            SecretFileVariable.PUBLIC_OIDC_CLIENT_SECRET,
        ),
        encryption_key=secret(
            "encryption", Fernet.generate_key().decode(), SecretFileVariable.PUBLIC_ENCRYPTION_KEY
        ),
    )
