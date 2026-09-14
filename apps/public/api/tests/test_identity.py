"""Provider contract tests use signed local tokens and no external credentials."""

import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from aqa_public.identity import IdentityProvider, IdentityUnavailable
from aqa_public.transport import ProviderSession, ProviderTransportError


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


def signed_provider(settings, monkeypatch, *, wrong_key=False, algorithm="RS256", **changes):
    key = RSAKey.generate_key(2048)
    claims = {
        "iss": settings.issuer,
        "sub": "synthetic-user",
        "aud": settings.client_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": "expected",
        "email": "synthetic@example.invalid",
        "email_verified": True,
    }
    claims.update(changes)
    token = jwt.encode({"alg": algorithm}, claims, key, algorithms=[algorithm])

    class Client:
        def fetch_token(self, *args, **kwargs):
            return {
                "id_token": token,
                "access_token": "synthetic-access",
                "expires_at": time.time() + 300,
            }

        def get(self, *args, **kwargs):
            return Response(
                {"keys": [(RSAKey.generate_key(2048) if wrong_key else key).as_dict(private=False)]}
            )

    @contextmanager
    def client():
        yield Client()

    provider = IdentityProvider(settings)
    monkeypatch.setattr(provider, "_client", client)
    return provider


def test_verified_subject_and_encrypted_server_token(settings, monkeypatch):
    provider = signed_provider(settings, monkeypatch)
    identity = provider.exchange(code="local", nonce="expected", verifier="local")
    assert identity.subject == "synthetic-user"
    assert "synthetic-access" not in identity.encrypted_access_token
    assert provider.cipher.decrypt(identity.encrypted_access_token.encode()) == b"synthetic-access"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "another-client"},
        {"nonce": "wrong"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"exp": 1},
        {"sub": ""},
        {"email": None},
    ],
)
def test_identity_rejects_untrusted_or_unverified_claims(settings, monkeypatch, claims):
    provider = signed_provider(settings, monkeypatch, **claims)
    with pytest.raises(IdentityUnavailable, match="could not be verified"):
        provider.exchange(code="local", nonce="expected", verifier="local")


def test_pkce_nonce_and_fixed_callback(settings):
    provider = IdentityProvider(settings)
    url = provider.authorization_url(state="state", nonce="nonce", verifier="a" * 43)
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] == ["nonce"]
    assert query["state"] == ["state"]
    assert query["redirect_uri"] == [settings.callback]
    assert query["prompt"] == ["login"]
    assert "a" * 43 not in url


def test_provider_outage_does_not_accept_session(settings, monkeypatch):
    provider = IdentityProvider(settings)
    with pytest.raises(IdentityUnavailable):
        provider.active("invalid encrypted token")


def test_identity_transport_rejects_other_hosts_and_bounds_bodies(monkeypatch):
    from requests import Response as HttpResponse

    with ProviderSession(origin="https://identity.example.invalid", client_id="test") as client:
        with pytest.raises(ProviderTransportError, match="destination"):
            client.get("https://attacker.invalid/token", withhold_token=True)
        response = HttpResponse()
        response.status_code = 200
        response._content = b"x" * 262145
        response._content_consumed = True
        monkeypatch.setattr(client, "send", lambda *args, **kwargs: response)
        with pytest.raises(ProviderTransportError, match="limit"):
            client.get("https://identity.example.invalid/certs", withhold_token=True)


@pytest.mark.parametrize("parameters", [{"wrong_key": True}, {"algorithm": "RS512"}])
def test_identity_rejects_wrong_signature_and_unapproved_algorithm(
    settings, monkeypatch, parameters
):
    provider = signed_provider(settings, monkeypatch, **parameters)
    with pytest.raises(IdentityUnavailable, match="could not be verified"):
        provider.exchange(code="local", nonce="expected", verifier="local")
