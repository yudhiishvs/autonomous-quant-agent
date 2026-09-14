"""Alpaca OAuth and verified paper account reads; no order-submission capability."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from adaptive_trader.platform.security import RedactedSecret
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aqa_public.transport import ProviderSession

AUTHORIZATION = "https://app.alpaca.markets/oauth/authorize"
TOKEN = "https://api.alpaca.markets/oauth/token"
PAPER_ACCOUNT = "https://paper-api.alpaca.markets/v2/account"


class BrokerUnavailable(Exception):
    """Sanitized broker/provider failure with no token or raw response attached."""


class BrokerRevoked(BrokerUnavailable):
    """The paper provider rejected the saved token."""


@dataclass(frozen=True)
class BrokerSettings:
    client_id: str
    client_secret: RedactedSecret = field(repr=False)
    encryption_key: RedactedSecret = field(repr=False)


class PaperAccount(BaseModel):
    """Only documented, bounded account fields needed for connection and display."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    id: UUID
    account_number: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    status: Literal["ACTIVE"]
    currency: Literal["USD"]
    trading_blocked: Literal[False]
    account_blocked: Literal[False]
    cash: Annotated[Decimal, Field(ge=-1_000_000_000_000, le=1_000_000_000_000)]
    equity: Annotated[Decimal, Field(ge=-1_000_000_000_000, le=1_000_000_000_000)]
    buying_power: Annotated[Decimal, Field(ge=0, le=1_000_000_000_000)]

    @field_validator("cash", "equity", "buying_power", mode="before")
    @classmethod
    def decimal_strings(cls, value: object) -> object:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("Account amounts must be bounded decimal strings.")
        return value

    @field_validator("trading_blocked", "account_blocked", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Paper account is blocked or ambiguous.")
        return value

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": "Alpaca · …" + self.account_number[-4:],
            "currency": self.currency,
            "cash": str(self.cash),
            "equity": str(self.equity),
            "buying_power": str(self.buying_power),
            "checked_at": datetime.now(UTC).isoformat(),
        }


class AlpacaConnection:
    """OAuth consent/exchange and account verification against fixed paper-only routes."""

    def __init__(self, settings: BrokerSettings, callback: str) -> None:
        self.settings = settings
        self.callback = callback
        self.cipher = Fernet(settings.encryption_key.reveal().encode("ascii"))

    def _client(self, *, access_token: str | None = None) -> ProviderSession:
        return ProviderSession(
            origin="https://paper-api.alpaca.markets"
            if access_token
            else "https://api.alpaca.markets",
            client_id=self.settings.client_id,
            client_secret=None if access_token else self.settings.client_secret.reveal(),
            token_endpoint_auth_method="client_secret_post",
            redirect_uri=self.callback,
            scope="trading",
            token={"access_token": access_token, "token_type": "Bearer"} if access_token else None,
        )

    def authorization_url(self, state: str) -> str:
        with self._client() as client:
            url, _ = client.create_authorization_url(AUTHORIZATION, state=state, env="paper")
            return str(url)

    def exchange(self, code: str) -> str:
        try:
            with self._client() as client:
                token = client.fetch_token(TOKEN, code=code)
                access = token["access_token"]
                if (
                    str(token.get("token_type", "")).lower() != "bearer"
                    or set(str(token.get("scope", "")).split()) != {"trading"}
                    or not isinstance(access, str)
                    or not 1 <= len(access) <= 4096
                    or any(ord(c) < 33 or ord(c) > 126 for c in access)
                ):
                    raise ValueError("Invalid broker grant")
                return access
        except Exception:
            raise BrokerUnavailable(
                "Paper-account authorization could not be verified. Start again."
            ) from None

    def account(self, access: str) -> PaperAccount:
        try:
            with self._client(access_token=access) as client:
                response = client.get(PAPER_ACCOUNT)
                if response.status_code in (401, 403):
                    raise BrokerRevoked(
                        "Alpaca access was revoked or its permissions changed. Reconnect your paper account."
                    )
                response.raise_for_status()
                return PaperAccount.model_validate(response.json())
        except BrokerRevoked:
            raise
        except Exception:
            raise BrokerUnavailable(
                "The paper account could not be verified as active and available. Try again or check Alpaca."
            ) from None

    def encrypt(self, *, owner: str, account_id: UUID, access: str) -> str:
        # Bind decrypted contents to owner/account; copied ciphertext cannot relink a token.
        return self.cipher.encrypt(
            json.dumps(
                {"owner": str(UUID(owner)), "account": str(account_id), "access": access}
            ).encode()
        ).decode()

    def decrypt(self, *, owner: str, account_id: UUID, ciphertext: str) -> str:
        try:
            payload = json.loads(self.cipher.decrypt(ciphertext.encode()))
            if payload["owner"] != str(UUID(owner)) or payload["account"] != str(account_id):
                raise ValueError("Token binding mismatch")
            access = payload["access"]
            if not isinstance(access, str) or not access:
                raise ValueError("Token missing")
            return access
        except Exception:
            raise BrokerUnavailable(
                "Saved access could not be verified. Reconnect your paper account."
            ) from None
