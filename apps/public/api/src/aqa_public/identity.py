"""Keycloak OIDC boundary using maintained OAuth, JOSE and authenticated encryption."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from authlib.integrations.requests_client import OAuth2Session
from authlib.oidc.core import CodeIDToken
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry
from requests import Response

from aqa_public.settings import Settings


class IdentityUnavailable(Exception):
    """Sanitized identity boundary failure; raw provider exceptions never reach clients."""


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    email: str
    encrypted_access_token: str
    expires_at: float


class IdentitySession(OAuth2Session):
    """Fixed provider origin, bounded response bodies and finite network deadlines."""

    def __init__(self, *, origin: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.origin = origin
        self.trust_env = False

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parsed = urlsplit(url)
        if (
            parsed.scheme + "://" + parsed.netloc != self.origin
            or parsed.username
            or parsed.password
        ):
            raise IdentityUnavailable("Identity destination rejected.")
        kwargs.update(timeout=(3.05, 10), allow_redirects=False, stream=True)
        started = time.monotonic()
        response: Response = super().request(method, url, **kwargs)
        try:
            content = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                content.extend(chunk)
                if len(content) > 262_144 or time.monotonic() - started > 20:
                    raise IdentityUnavailable("Identity response limit exceeded.")
            response._content = bytes(content)
            # Let requests mark its fully buffered content as consumed before closing.
            _ = response.content
            return response
        finally:
            response.close()


class IdentityProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cipher = Fernet(settings.encryption_key.reveal().encode("ascii"))
        self.base = settings.issuer + "/protocol/openid-connect"

    def _client(self) -> OAuth2Session:
        parsed = urlsplit(self.settings.issuer)
        client = IdentitySession(
            origin=parsed.scheme + "://" + parsed.netloc,
            client_id=self.settings.client_id,
            client_secret=self.settings.client_secret.reveal(),
            redirect_uri=self.settings.callback,
            scope="openid email",
            token_endpoint_auth_method="client_secret_basic",
            code_challenge_method="S256",
            default_timeout=(3.05, 10),
        )
        client.trust_env = False
        return client

    def authorization_url(self, *, state: str, nonce: str, verifier: str) -> str:
        with self._client() as client:
            url, _ = client.create_authorization_url(
                self.base + "/auth",
                state=state,
                nonce=nonce,
                code_verifier=verifier,
                prompt="login",
                max_age=0,
            )
            return str(url)

    def exchange(self, *, code: str, nonce: str, verifier: str) -> VerifiedIdentity:
        try:
            with self._client() as client:
                token = client.fetch_token(
                    self.base + "/token", code=code, code_verifier=verifier, allow_redirects=False
                )
                response = client.get(
                    self.base + "/certs", withhold_token=True, allow_redirects=False
                )
                response.raise_for_status()
                decoded = jwt.decode(
                    token["id_token"],
                    KeySet.import_key_set(response.json()),
                    registry=JWSRegistry(algorithms=["RS256"]),
                )
                claims = CodeIDToken(
                    decoded.claims,
                    decoded.header,
                    {
                        "iss": {"value": self.settings.issuer},
                        "aud": {"value": self.settings.client_id},
                    },
                    {
                        "client_id": self.settings.client_id,
                        "nonce": nonce,
                        "access_token": token["access_token"],
                    },
                )
                claims.validate(leeway=5)
                if claims.get("email_verified") is not True:
                    raise IdentityUnavailable()
                subject, email = claims["sub"], claims.get("email")
                if (
                    not isinstance(subject, str)
                    or not 1 <= len(subject) <= 255
                    or not isinstance(email, str)
                    or not 1 <= len(email) <= 320
                ):
                    raise IdentityUnavailable()
                expires = min(float(token["expires_at"]), float(claims["exp"]), time.time() + 900)
                return VerifiedIdentity(
                    subject,
                    email,
                    self.cipher.encrypt(token["access_token"].encode()).decode(),
                    expires,
                )
        except Exception:
            raise IdentityUnavailable("Sign-in could not be verified. Please try again.") from None

    def active(self, encrypted_token: str) -> bool:
        """Check revocation at the provider on every authenticated request; fail closed."""
        try:
            access = self.cipher.decrypt(encrypted_token.encode()).decode()
            with self._client() as client:
                response = client.introspect_token(
                    self.base + "/token/introspect", token=access, allow_redirects=False
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
                return (
                    data.get("active") is True and data.get("client_id") == self.settings.client_id
                )
        except Exception:
            raise IdentityUnavailable("Identity service unavailable. Try again shortly.") from None

    def revoke(self, encrypted_token: str) -> None:
        try:
            access = self.cipher.decrypt(encrypted_token.encode()).decode()
            with self._client() as client:
                response = client.revoke_token(
                    self.base + "/revoke",
                    token=access,
                    token_type_hint="access_token",
                    allow_redirects=False,
                )
                response.raise_for_status()
        except Exception:
            raise IdentityUnavailable(
                "Local sign-out completed; identity-provider revocation could not be confirmed."
            ) from None
