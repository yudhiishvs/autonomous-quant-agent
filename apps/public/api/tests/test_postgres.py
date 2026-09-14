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


def test_broker_claims_and_credentials_are_owner_isolated(store):
    from test_broker import account_data

    from aqa_public.broker import PaperAccount
    from aqa_public.broker_storage import BrokerStore, ConnectionConflict

    token, _, alice = sign_in(store)
    bob_token, _, bob = sign_in(store)
    accounts = BrokerStore(store)
    account = PaperAccount.model_validate(account_data())
    accounts.begin(alice, token, token + "alice-state")
    generation = accounts.consume(alice, token, token + "alice-state")
    saved = accounts.connect(
        owner=alice,
        session=token,
        generation=generation,
        account=account,
        encrypted_token="synthetic-encrypted",
    )
    accounts.begin(bob, bob_token, bob_token + "bob-state")
    generation = accounts.consume(bob, bob_token, bob_token + "bob-state")
    with pytest.raises(ConnectionConflict, match="cannot be connected"):
        accounts.connect(
            owner=bob,
            session=bob_token,
            generation=generation,
            account=account,
            encrypted_token="different",
        )
    assert accounts.accounts(bob) == []
    assert accounts.credential(bob, saved["id"]) is None
    assert not accounts.disconnect(bob, saved["id"])
    assert "encrypted_token" not in accounts.accounts(alice)[0]
    assert accounts.accounts(alice)[0]["snapshot"]["cash"] == "10000.123456789"
    with store.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM aqa_public.broker_accounts")).scalar_one()
            == 0
        )


def test_disconnect_fences_inflight_callback_refresh_and_stale_session(store):
    from test_broker import account_data

    from aqa_public.broker import PaperAccount
    from aqa_public.broker_storage import BrokerStore, ConnectionConflict

    token, _, owner = sign_in(store)
    accounts = BrokerStore(store)
    account = PaperAccount.model_validate(account_data())
    accounts.begin(owner, token, token + "first")
    generation = accounts.consume(owner, token, token + "first")
    saved = accounts.connect(
        owner=owner,
        session=token,
        generation=generation,
        account=account,
        encrypted_token="first-token",
    )
    accounts.begin(owner, token, token + "inflight")
    old_generation = accounts.consume(owner, token, token + "inflight")
    assert accounts.disconnect(owner, saved["id"])
    assert accounts.credential(owner, saved["id"]) is None
    with pytest.raises(ConnectionConflict, match="superseded"):
        accounts.connect(
            owner=owner,
            session=token,
            generation=old_generation,
            account=account,
            encrypted_token="late-token",
        )
    accounts.begin(owner, token, token + "reconnect")
    generation = accounts.consume(owner, token, token + "reconnect")
    reconnected = accounts.connect(
        owner=owner,
        session=token,
        generation=generation,
        account=account,
        encrypted_token="new-token",
    )
    assert reconnected["id"] == saved["id"]
    assert not accounts.refresh(owner, saved["id"], saved["revision"], account)
    assert not accounts.disconnect(
        owner, saved["id"], state="reconnect_required", expected_revision=saved["revision"]
    )
    assert accounts.credential(owner, saved["id"])["encrypted_token"] == "new-token"
    accounts.begin(owner, token, token + "signout-race")
    generation = accounts.consume(owner, token, token + "signout-race")
    store.sign_out(token)
    with pytest.raises(ConnectionConflict, match="session expired"):
        accounts.connect(
            owner=owner,
            session=token,
            generation=generation,
            account=account,
            encrypted_token="stale-session-token",
        )


def test_broker_state_is_session_bound_one_use_expiring_and_bounded(store):
    from aqa_public.broker_storage import BrokerStore, ConnectionConflict
    from aqa_public.storage import verifier

    token, _, owner = sign_in(store)
    accounts = BrokerStore(store)
    accounts.begin(owner, token, token + "bound")
    with pytest.raises(ConnectionConflict):
        accounts.consume(owner, "different-session", token + "bound")
    assert accounts.consume(owner, token, token + "bound") == 0
    with pytest.raises(ConnectionConflict):
        accounts.consume(owner, token, token + "bound")
    with store.tenant(owner) as connection:
        connection.execute(
            text(
                "INSERT INTO aqa_public.broker_oauth_attempts VALUES (:state,:owner,:session,0,now()-interval '1 second')"
            ),
            {"state": verifier(token + "expired"), "owner": owner, "session": verifier(token)},
        )
    with pytest.raises(ConnectionConflict):
        accounts.consume(owner, token, token + "expired")
    for number in range(5):
        accounts.begin(owner, token, token + str(number))
    with pytest.raises(ConnectionConflict, match="Five"):
        accounts.begin(owner, token, token + "sixth")


def test_concurrent_broker_claims_cannot_link_one_account_to_two_users(store, settings):
    from test_broker import account_data

    from aqa_public.broker import PaperAccount
    from aqa_public.broker_storage import BrokerStore, ConnectionConflict

    first_token, _, first_owner = sign_in(store)
    second_token, _, second_owner = sign_in(store)
    other = Store(settings.database_url.reveal())
    account = PaperAccount.model_validate(account_data())

    def claim(arguments):
        database, owner, token = arguments
        connection = BrokerStore(database)
        connection.begin(owner, token, "claim-" + owner)
        generation = connection.consume(owner, token, "claim-" + owner)
        try:
            connection.connect(
                owner=owner,
                session=token,
                generation=generation,
                account=account,
                encrypted_token="synthetic-token",
            )
            return "connected"
        except ConnectionConflict:
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    claim, [(store, first_owner, first_token), (other, second_owner, second_token)]
                )
            )
        assert sorted(results) == ["connected", "rejected"]
    finally:
        other.engine.dispose()


def test_broker_api_consent_callback_ownership_revocation_and_configuration(store, settings):
    from urllib.parse import parse_qs, urlsplit

    from test_broker import account_data

    from aqa_public.broker import AlpacaConnection, BrokerRevoked, BrokerSettings, PaperAccount

    token, csrf, _owner = sign_in(store)
    other_token, other_csrf, _ = sign_in(store)

    class LocalBroker(AlpacaConnection):
        exchanges = 0
        revoked = False
        account_value = PaperAccount.model_validate(account_data())

        def exchange(self, code):
            self.exchanges += 1
            return "synthetic-grant"

        def account(self, access):
            if self.revoked:
                raise BrokerRevoked("Access revoked. Reconnect your paper account.")
            return self.account_value

    broker = LocalBroker(
        BrokerSettings("local", settings.client_secret, settings.encryption_key),
        settings.origin + "/broker/alpaca/callback",
    )
    app = create_app(settings, store=store, identity=LocalIdentity(settings), broker=broker)
    headers = {"Origin": settings.origin, "X-CSRF-Token": csrf}
    with TestClient(app, base_url=settings.origin) as client:
        client.cookies.set("aqa_session", token)
        assert (
            client.post("/api/v1/accounts/connect", json={"consent": "paper_only"}).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/accounts/connect", json={"consent": "live"}, headers=headers
            ).status_code
            == 422
        )
        response = client.post(
            "/api/v1/accounts/connect", json={"consent": "paper_only"}, headers=headers
        )
        assert response.status_code == 200
        query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        state = query["state"][0]
        assert query["env"] == ["paper"]
        client.cookies.set("aqa_session", other_token)
        assert (
            client.get(
                "/broker/alpaca/callback",
                params={"state": state, "code": "local"},
                follow_redirects=False,
            ).status_code
            == 409
        )
        assert broker.exchanges == 0
        client.cookies.set("aqa_session", token)
        assert (
            client.get(
                "/broker/alpaca/callback",
                params={"state": state, "code": "local"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.get(
                "/broker/alpaca/callback",
                params={"state": state, "code": "local"},
                follow_redirects=False,
            ).status_code
            == 409
        )
        assert broker.exchanges == 1
        denied = client.post(
            "/api/v1/accounts/connect", json={"consent": "paper_only"}, headers=headers
        )
        denied_state = parse_qs(urlsplit(denied.json()["authorization_url"]).query)["state"][0]
        assert (
            client.get(
                "/broker/alpaca/callback", params={"state": denied_state, "error": "access_denied"}
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/broker/alpaca/callback", params={"state": denied_state, "code": "late"}
            ).status_code
            == 409
        )
        assert broker.exchanges == 1
        response = client.get("/api/v1/accounts")
        account = response.json()["accounts"][0]
        assert "synthetic-grant" not in response.text and "encrypted_token" not in response.text
        client.cookies.set("aqa_session", other_token)
        assert client.get("/api/v1/accounts").json()["accounts"] == []
        assert (
            client.post(
                "/api/v1/accounts/" + account["id"] + "/disconnect",
                json={"confirm": "disconnect_without_cancelling_orders"},
                headers={"Origin": settings.origin, "X-CSRF-Token": other_csrf},
            ).status_code
            == 404
        )
        client.cookies.set("aqa_session", token)
        broker.revoked = True
        assert (
            client.post(
                "/api/v1/accounts/" + account["id"] + "/refresh", headers=headers
            ).status_code
            == 503
        )
        assert client.get("/api/v1/accounts").json()["accounts"][0]["state"] == "reconnect_required"
        response = client.post(
            "/api/v1/accounts/" + account["id"] + "/disconnect",
            json={"confirm": "disconnect_without_cancelling_orders"},
            headers=headers,
        )
        assert response.json() == {
            "disconnected": True,
            "broker_revocation_confirmed": False,
            "orders_cancelled": False,
        }
    with TestClient(
        create_app(settings, store=store, identity=LocalIdentity(settings)),
        base_url=settings.origin,
    ) as client:
        client.cookies.set("aqa_session", token)
        assert client.get("/api/v1/accounts").json()["connection_available"] is False
        assert (
            client.post(
                "/api/v1/accounts/connect", json={"consent": "paper_only"}, headers=headers
            ).status_code
            == 503
        )


def test_account_quota_preserves_existing_claims_and_permits_reconnect(store):
    from test_broker import account_data

    from aqa_public.broker import PaperAccount
    from aqa_public.broker_storage import BrokerStore, ConnectionConflict

    token, _, owner = sign_in(store)
    accounts = BrokerStore(store)
    original = PaperAccount.model_validate(account_data())
    for number in range(4):
        account = original if number == 0 else PaperAccount.model_validate(account_data())
        accounts.begin(owner, token, token + str(number))
        generation = accounts.consume(owner, token, token + str(number))
        if number == 3:
            with pytest.raises(ConnectionConflict, match="three paper accounts"):
                accounts.connect(
                    owner=owner,
                    session=token,
                    generation=generation,
                    account=account,
                    encrypted_token="quota-test",
                )
        else:
            accounts.connect(
                owner=owner,
                session=token,
                generation=generation,
                account=account,
                encrypted_token="quota-test",
            )
    accounts.begin(owner, token, token + "reconnect-at-quota")
    generation = accounts.consume(owner, token, token + "reconnect-at-quota")
    accounts.connect(
        owner=owner,
        session=token,
        generation=generation,
        account=original,
        encrypted_token="new-quota-token",
    )
    assert len(accounts.accounts(owner)) == 3


def test_unexpected_internal_response_fields_are_redacted(store, settings, monkeypatch, caplog):
    token, _, _owner = sign_in(store)
    monkeypatch.setattr(
        store, "versions", lambda owner: [{"encrypted_token": "synthetic-private-response"}]
    )
    with TestClient(
        create_app(settings, store=store, identity=LocalIdentity(settings)),
        base_url=settings.origin,
    ) as client:
        client.cookies.set("aqa_session", token)
        response = client.get("/api/v1/strategy-versions")
        assert response.status_code == 503
        assert "synthetic-private-response" not in response.text
        assert "synthetic-private-response" not in caplog.text
