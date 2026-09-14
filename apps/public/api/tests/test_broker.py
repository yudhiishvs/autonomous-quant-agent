"""Offline Alpaca paper OAuth/account contracts; no broker credentials or network."""

from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aqa_public.broker import (
    PAPER_ACCOUNT,
    TOKEN,
    AlpacaConnection,
    BrokerRevoked,
    BrokerSettings,
    BrokerUnavailable,
    PaperAccount,
)


def account_data(**changes):
    return {
        "id": str(uuid4()),
        "account_number": "PAPER1234",
        "status": "ACTIVE",
        "currency": "USD",
        "trading_blocked": False,
        "account_blocked": False,
        "cash": "10000.123456789",
        "equity": "10000.123456789",
        "buying_power": "20000.246913578",
        **changes,
    }


@pytest.fixture
def broker(settings):
    return AlpacaConnection(
        BrokerSettings("synthetic-app", settings.client_secret, settings.encryption_key),
        settings.origin + "/broker/alpaca/callback",
    )


def test_consent_is_paper_only_and_requests_only_trading(broker):
    url = broker.authorization_url("unpredictable-state")
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "app.alpaca.markets"
    assert parts.path == "/oauth/authorize"
    query = parse_qs(parts.query)
    assert query["env"] == ["paper"]
    assert query["scope"] == ["trading"]
    assert query["state"] == ["unpredictable-state"]
    assert query["redirect_uri"] == ["http://127.0.0.1:5178/broker/alpaca/callback"]
    assert "code_challenge" not in query  # Not documented by this provider.
    assert "client_secret" not in query


def test_token_ciphertext_is_bound_to_user_and_paper_account(broker):
    owner, account = str(uuid4()), uuid4()
    encrypted = broker.encrypt(owner=owner, account_id=account, access="synthetic-broker-access")
    assert "synthetic-broker-access" not in encrypted
    assert (
        broker.decrypt(owner=owner, account_id=account, ciphertext=encrypted)
        == "synthetic-broker-access"
    )
    for wrong_owner, wrong_account, ciphertext in [
        (str(uuid4()), account, encrypted),
        (owner, uuid4(), encrypted),
        (owner, account, "invalid"),
    ]:
        with pytest.raises(BrokerUnavailable):
            broker.decrypt(owner=wrong_owner, account_id=wrong_account, ciphertext=ciphertext)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ACCOUNT_CLOSED"},
        {"trading_blocked": True},
        {"account_blocked": 0},
        {"currency": "EUR"},
        {"cash": 100},
        {"cash": "NaN"},
        {"equity": "Infinity"},
        {"id": "not-an-account"},
        {"buying_power": "-1"},
        {"cash": "1e1000"},
    ],
)
def test_account_contract_rejects_unsafe_and_ambiguous_fields(changes):
    with pytest.raises(ValidationError):
        PaperAccount.model_validate(account_data(**changes))


def test_account_snapshot_preserves_decimal_precision_and_masks_identifier():
    account = PaperAccount.model_validate(account_data())
    snapshot = account.snapshot()
    assert snapshot["cash"] == "10000.123456789"
    assert snapshot["buying_power"] == "20000.246913578"
    assert snapshot["label"] == "Alpaca · …1234"
    assert "account_number" not in snapshot and "id" not in snapshot


def client_double(broker, monkeypatch, *, token=None, status=200):
    calls = []

    class Response:
        status_code = status

        def raise_for_status(self):
            if status != 200:
                raise RuntimeError("synthetic-private-provider-response")

        def json(self):
            return account_data()

    class Client:
        def fetch_token(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return token or {
                "token_type": "bearer",
                "scope": "trading",
                "access_token": "synthetic-grant",
            }

        def get(self, url):
            calls.append(("GET", url, {}))
            return Response()

    @contextmanager
    def client(**kwargs):
        yield Client()

    monkeypatch.setattr(broker, "_client", client)
    return calls


def test_exchange_and_reads_have_distinct_fixed_endpoints(broker, monkeypatch):
    calls = client_double(broker, monkeypatch)
    access = broker.exchange("synthetic-code")
    assert broker.account(access).status == "ACTIVE"
    assert calls == [("POST", TOKEN, {"code": "synthetic-code"}), ("GET", PAPER_ACCOUNT, {})]


@pytest.mark.parametrize(
    "token",
    [
        {"access_token": "synthetic", "token_type": "bearer", "scope": "trading account:write"},
        {"access_token": "synthetic", "token_type": "bearer", "scope": "data"},
        {"access_token": "synthetic\r\nheader", "token_type": "bearer", "scope": "trading"},
        {"access_token": "synthetic", "token_type": "basic", "scope": "trading"},
    ],
)
def test_malformed_or_excessive_grant_is_not_stored(broker, monkeypatch, token):
    client_double(broker, monkeypatch, token=token)
    with pytest.raises(BrokerUnavailable, match="authorization could not be verified"):
        broker.exchange("synthetic-code")


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_errors_are_sanitized_and_revocation_distinct(broker, monkeypatch, status):
    client_double(broker, monkeypatch, status=status)
    with pytest.raises(BrokerRevoked if status in (401, 403) else BrokerUnavailable) as error:
        broker.account("synthetic-grant")
    assert "synthetic-private" not in str(error.value)


def test_partial_broker_configuration_fails_without_selecting_a_fake(monkeypatch):
    from aqa_public.settings import Settings

    for key in ("AQA_PUBLIC_ALPACA_CLIENT_SECRET_FILE", "AQA_PUBLIC_BROKER_ENCRYPTION_KEY_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AQA_PUBLIC_ALPACA_CLIENT_ID", "synthetic-client")
    with pytest.raises(ValueError, match="configuration is incomplete"):
        Settings.from_environment()


def test_actual_oauth_client_prepares_form_exchange_and_paper_bearer_read(broker, monkeypatch):
    import json

    from requests import Response

    from aqa_public.transport import ProviderSession

    calls = []

    def send(client, request, **kwargs):
        calls.append((request, kwargs))
        response = Response()
        response.status_code = 200
        response.request = request
        payload = (
            {"access_token": "synthetic-grant", "token_type": "bearer", "scope": "trading"}
            if request.url == TOKEN
            else account_data()
        )
        response._content = json.dumps(payload).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(ProviderSession, "send", send)
    access = broker.exchange("synthetic-authorization-code")
    broker.account(access)
    exchange, exchange_options = calls[0]
    account, account_options = calls[1]
    assert exchange.url == TOKEN and exchange.method == "POST"
    assert exchange.headers["Content-Type"].split(";", 1)[0] == "application/x-www-form-urlencoded"
    body = parse_qs(exchange.body)
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["synthetic-authorization-code"]
    assert body["client_id"] == ["synthetic-app"]
    assert body["client_secret"] == ["SYNTHETIC-ONLY-NO-EXTERNAL-AUTHORITY"]
    assert body["redirect_uri"] == [broker.callback]
    assert "Authorization" not in exchange.headers
    assert account.url == PAPER_ACCOUNT and account.method == "GET"
    assert account.headers["Authorization"] == "Bearer synthetic-grant"
    for options in (exchange_options, account_options):
        assert options["allow_redirects"] is False
        assert options["timeout"] == (3.05, 10)
