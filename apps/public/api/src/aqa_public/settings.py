"""Explicit local versus HTTPS deployment configuration; no implicit identity fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from adaptive_trader.platform.security import RedactedSecret, SecretFileVariable, load_secret_file

from aqa_public.broker import BrokerSettings


@dataclass(frozen=True)
class Settings:
    origin: str
    issuer: str
    client_id: str
    database_url: RedactedSecret = field(repr=False)
    client_secret: RedactedSecret = field(repr=False)
    encryption_key: RedactedSecret = field(repr=False)
    development: bool = False
    broker: BrokerSettings | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value in (self.origin, self.issuer):
            parsed = urlsplit(value)
            if (
                parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or value.endswith("/")
            ):
                raise ValueError("Invalid application or identity origin.")
            local = self.development and parsed.hostname in {"localhost", "127.0.0.1"}
            if not parsed.netloc or (
                parsed.scheme != "https" and not (local and parsed.scheme == "http")
            ):
                raise ValueError("HTTPS is required outside explicit loopback development.")
        if urlsplit(self.origin).path or not self.client_id:
            raise ValueError("Invalid application configuration.")

    @property
    def callback(self) -> str:
        return self.origin + "/auth/callback"

    @classmethod
    def from_environment(cls) -> Settings:
        def secret(source: SecretFileVariable) -> RedactedSecret:
            return load_secret_file(os.environ[source.value], source=source)

        broker = None
        broker_names = (
            "AQA_PUBLIC_ALPACA_CLIENT_ID",
            SecretFileVariable.PUBLIC_ALPACA_CLIENT_SECRET.value,
            SecretFileVariable.PUBLIC_BROKER_ENCRYPTION_KEY.value,
        )
        if any(os.environ.get(name) for name in broker_names):
            if not all(os.environ.get(name) for name in broker_names):
                raise ValueError("Alpaca connection configuration is incomplete.")
            broker = BrokerSettings(
                client_id=os.environ[broker_names[0]],
                client_secret=secret(SecretFileVariable.PUBLIC_ALPACA_CLIENT_SECRET),
                encryption_key=secret(SecretFileVariable.PUBLIC_BROKER_ENCRYPTION_KEY),
            )
        return cls(
            broker=broker,
            origin=os.environ["AQA_PUBLIC_ORIGIN"],
            issuer=os.environ["AQA_PUBLIC_OIDC_ISSUER"],
            client_id=os.environ["AQA_PUBLIC_OIDC_CLIENT_ID"],
            database_url=secret(SecretFileVariable.DATABASE_URL),
            client_secret=secret(SecretFileVariable.PUBLIC_OIDC_CLIENT_SECRET),
            encryption_key=secret(SecretFileVariable.PUBLIC_ENCRYPTION_KEY),
            development=os.environ.get("AQA_PUBLIC_DEVELOPMENT") == "YES",
        )
