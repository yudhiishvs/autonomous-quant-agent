"""Opt-in browser contract server: real local identity/storage, injected synthetic Alpaca."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from sqlalchemy.engine import make_url

from aqa_public.app import create_app
from aqa_public.broker import AlpacaConnection, BrokerSettings, BrokerUnavailable, PaperAccount
from aqa_public.settings import Settings


class SyntheticBroker(AlpacaConnection):
    def exchange(self, code: str) -> str:
        if code != "synthetic-browser-consent":
            raise BrokerUnavailable("Synthetic browser consent rejected.")
        return "synthetic-local-broker-token"

    def account(self, access: str) -> PaperAccount:
        if access != "synthetic-local-broker-token":
            raise BrokerUnavailable("Synthetic browser access rejected.")
        return PaperAccount.model_validate(
            {
                "id": "a8c2d560-13c7-4ab6-b8cb-196085708904",
                "account_number": "SYNTHETIC1234",
                "status": "ACTIVE",
                "currency": "USD",
                "trading_blocked": False,
                "account_blocked": False,
                "cash": "10000.123456789",
                "equity": "10000.123456789",
                "buying_power": "20000.246913578",
            }
        )


def main() -> None:
    if any(
        os.environ.get(name) != "YES"
        for name in (
            "APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE",
            "APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES",
            "AQA_PUBLIC_DISPOSABLE_TESTS",
        )
    ):
        raise SystemExit(
            "Synthetic contract server requires the three disposable acknowledgements."
        )
    local = Path(__file__).resolve().parents[2] / ".local"
    settings = Settings(
        origin="http://127.0.0.1:5178",
        issuer="http://127.0.0.1:8188/realms/paper",
        client_id="paper-web",
        development=True,
        approval_signing_key=load_secret_file(
            local / "approval_signing_key", source=SecretFileVariable.PUBLIC_APPROVAL_SIGNING_KEY
        ),
        database_url=load_secret_file(
            local / "database_url", source=SecretFileVariable.DATABASE_URL
        ),
        client_secret=load_secret_file(
            local / "oidc_secret", source=SecretFileVariable.PUBLIC_OIDC_CLIENT_SECRET
        ),
        encryption_key=load_secret_file(
            local / "encryption_key", source=SecretFileVariable.PUBLIC_ENCRYPTION_KEY
        ),
    )
    url = make_url(settings.database_url.reveal())
    if (url.host, url.port, url.database, url.username) != (
        "127.0.0.1",
        55438,
        "collector_test",
        "aqa_public_runtime",
    ):
        raise SystemExit("Synthetic contract server database rejected.")
    # Synthetic grants have no provider authority; production never imports this fixture.
    broker = SyntheticBroker(
        BrokerSettings("synthetic-only", settings.client_secret, settings.encryption_key),
        settings.origin + "/broker/alpaca/callback",
    )
    uvicorn.run(
        create_app(settings, broker=broker),
        host="127.0.0.1",
        port=8018,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
