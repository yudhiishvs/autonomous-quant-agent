"""Opt-in real runtime-role isolation tests in the named disposable local cluster."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from adaptive_trader.public_product.strategies import StrategyDefinition
from fastapi.testclient import TestClient
from sqlalchemy import text

from aqa_public.app import create_app
from aqa_public.identity import IdentityProvider, IdentityUnavailable
from aqa_public.settings import Settings
from aqa_public.storage import Store

pytestmark = pytest.mark.skipif(
    any(
        os.environ.get(key) != "YES"
        for key in (
            "APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE",
            "APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES",
            "AQA_PUBLIC_DISPOSABLE_TESTS",
        )
    ),
    reason="Explicit disposable-cluster acknowledgements required",
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def settings():
    def secret(name, source):
        return load_secret_file(ROOT / ".local" / name, source=source)

    config = Settings(
        origin="http://127.0.0.1:5178",
        issuer="http://127.0.0.1:8188/realms/paper",
        client_id="paper-web",
        development=True,
        database_url=secret("database_url", SecretFileVariable.DATABASE_URL),
        client_secret=secret("oidc_secret", SecretFileVariable.PUBLIC_OIDC_CLIENT_SECRET),
        encryption_key=secret("encryption_key", SecretFileVariable.PUBLIC_ENCRYPTION_KEY),
    )
    from sqlalchemy.engine import make_url

    url = make_url(config.database_url.reveal())
    assert (url.host, url.port, url.database, url.username) == (
        "127.0.0.1",
        55438,
        "collector_test",
        "aqa_public_runtime",
    )
    return config


@pytest.fixture
def store(settings):
    result = Store(settings.database_url.reveal())
    result.readiness()
    yield result
    result.engine.dispose()


def sign_in(store):
    token, csrf = store.sign_in(
        "https://synthetic.invalid",
        str(uuid4()),
        "test@example.invalid",
        "synthetic-token",
        time.time() + 300,
    )
    session = store.session(token)
    assert session
    return token, csrf, session.owner


def definition():
    return StrategyDefinition.model_validate(
        {
            "symbol": "SPY",
            "rule": {"kind": "constant_target", "target_shares": 1},
            "order": {"kind": "market"},
        }
    )


def test_two_users_runtime_rls_pool_reuse_and_immutability(store):
    _, _, alice = sign_in(store)
    _, _, bob = sign_in(store)
    version = store.save_version(alice, "Alice only", definition())
    assert store.version(alice, version["id"])["name"] == "Alice only"
    assert store.version(bob, version["id"]) is None
    assert store.versions(bob) == []
    for owner, expected in ((alice, 1), (bob, 0), (alice, 1), (bob, 0)):
        with store.tenant(owner) as connection:
            # Intentionally omit the application predicate to exercise the DB policy.
            assert (
                connection.execute(
                    text("SELECT count(*) FROM aqa_public.strategy_versions")
                ).scalar_one()
                == expected
            )
    with store.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM aqa_public.strategy_versions")
            ).scalar_one()
            == 0
        )
    with pytest.raises(Exception, match="row-level security"), store.tenant(bob) as connection:
        connection.execute(
            text(
                "INSERT INTO aqa_public.strategy_versions (id,owner_id,name,definition,content_hash,created_at) VALUES (:id,:owner,'cross-user','{}',:hash,now())"
            ),
            {"id": str(uuid4()), "owner": alice, "hash": "a" * 64},
        )
    with pytest.raises(Exception, match="permission denied"), store.tenant(alice) as connection:
        connection.execute(text("UPDATE aqa_public.strategy_versions SET name='changed'"))


def test_login_one_time_state_browser_binding_and_expiry(store):
    state = str(uuid4())
    store.begin_login("browser", state, "nonce", "encrypted-verifier")
    assert store.consume_login("other", state) is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: store.consume_login("browser", state), range(2)))
    assert outcomes.count(("nonce", "encrypted-verifier")) == 1
    assert outcomes.count(None) == 1
    expired, _ = store.sign_in(
        "https://synthetic.invalid", str(uuid4()), "test@example.invalid", "fake", time.time() - 1
    )
    assert store.session(expired) is None
    token, _, _ = sign_in(store)
    store.sign_out(token)
    assert store.session(token) is None


def test_concurrent_quota_enforcement_across_instances(store, settings):
    _, _, owner = sign_in(store)
    for i in range(99):
        store.save_version(owner, f"Version {i}", definition())
    other = Store(settings.database_url.reveal())

    def save(database):
        try:
            database.save_version(owner, "Last slot", definition())
            return "saved"
        except ValueError:
            return "limited"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(save, (store, other))) == ["limited", "saved"]
        assert len(store.versions(owner)) == 100
    finally:
        other.engine.dispose()


def test_distributed_rate_limit(store, settings):
    key = str(uuid4())
    other = Store(settings.database_url.reveal())
    try:
        assert store.rate_limit(key, limit=2, window=3600)
        assert other.rate_limit(key, limit=2, window=3600)
        assert not store.rate_limit(key, limit=2, window=3600)
    finally:
        other.engine.dispose()


class LocalIdentity(IdentityProvider):
    revoked = False
    outage = False

    def active(self, encrypted_token):
        if self.outage:
            raise IdentityUnavailable("Identity service unavailable.")
        return not self.revoked

    def revoke(self, encrypted_token):
        if self.outage:
            raise IdentityUnavailable("Provider unavailable.")


def test_api_ownership_csrf_validation_and_logout_during_outage(store, settings):
    token, csrf, alice = sign_in(store)
    bob_token, _, _ = sign_in(store)
    identity = LocalIdentity(settings)
    app = create_app(settings, store=store, identity=identity)
    with TestClient(app, base_url=settings.origin) as client:
        assert client.get("/api/v1/me").status_code == 401
        client.cookies.set("aqa_session", token)
        assert client.get("/api/v1/me").json()["user_id"] == alice
        payload = {
            "request_id": str(uuid4()),
            "name": "My strategy",
            "definition": definition().model_dump(mode="json"),
        }
        assert client.post("/api/v1/strategy-versions", json=payload).status_code == 403
        assert (
            client.post(
                "/api/v1/strategy-versions", json=payload, headers={"Origin": settings.origin}
            ).status_code
            == 403
        )
        headers = {"Origin": settings.origin, "X-CSRF-Token": csrf}
        response = client.post("/api/v1/strategy-versions", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        version = response.json()
        assert version["definition"]["rule"]["target_shares"] == 1
        assert (
            client.post(
                "/api/v1/strategy-versions",
                json={**payload, "owner_id": str(uuid4())},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/strategy-versions", content=b"x" * 16385, headers=headers
            ).status_code
            == 413
        )
        client.cookies.set("aqa_session", bob_token)
        assert client.get("/api/v1/strategy-versions/" + version["id"]).status_code == 404
        assert client.get("/api/v1/strategy-versions").json() == []
        client.cookies.set("aqa_session", token)
        identity.revoked = True
        assert client.get("/api/v1/me").status_code == 401
        assert store.session(token) is None

        token, csrf, _ = sign_in(store)
        client.cookies.set("aqa_session", token)
        identity.outage = True
        assert client.get("/api/v1/me").status_code == 503
        response = client.post(
            "/auth/logout", headers={"Origin": settings.origin, "X-CSRF-Token": csrf}
        )
        assert response.json() == {"signed_out": True, "provider_revocation_confirmed": False}
        assert store.session(token) is None


def test_same_save_request_is_durable_idempotent_and_owner_scoped(store, settings):
    _, _, alice = sign_in(store)
    _, _, bob = sign_in(store)
    request_id = uuid4()
    other = Store(settings.database_url.reveal())
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda database: database.save_version(
                        alice, "Stable version", definition(), request_id=request_id
                    ),
                    (store, other),
                )
            )
        assert results[0]["id"] == results[1]["id"]
        assert len(store.versions(alice)) == 1
        with pytest.raises(ValueError, match="different configuration"):
            store.save_version(alice, "Changed name", definition(), request_id=request_id)
        assert (
            store.save_version(bob, "Stable version", definition(), request_id=request_id)["id"]
            != results[0]["id"]
        )
    finally:
        other.engine.dispose()
