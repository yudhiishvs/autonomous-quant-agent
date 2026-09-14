"""Closed HTTP response contracts exclude credentials and broker-private identifiers."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from adaptive_trader.public_product.approvals import RiskLimits
from adaptive_trader.public_product.strategies import StrategyDefinition
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionResponse(PublicResponse):
    id: UUID
    name: str = Field(min_length=1, max_length=80)
    definition: StrategyDefinition
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime


class AccountSnapshot(PublicResponse):
    label: str = Field(max_length=80)
    currency: Literal["USD"]
    cash: str = Field(max_length=40)
    equity: str = Field(max_length=40)
    buying_power: str = Field(max_length=40)
    checked_at: AwareDatetime


class AccountResponse(PublicResponse):
    id: UUID
    state: Literal["connected", "disconnected", "reconnect_required"]
    snapshot: AccountSnapshot
    revision: int = Field(ge=0)
    updated_at: datetime


class AccountsResponse(PublicResponse):
    connection_available: bool
    accounts: Annotated[list[AccountResponse], Field(max_length=3)]


class RefreshResponse(PublicResponse):
    accounts: Annotated[list[AccountResponse], Field(max_length=3)]


class DisconnectResponse(PublicResponse):
    disconnected: Literal[True]
    broker_revocation_confirmed: Literal[False]
    orders_cancelled: Literal[False]


class ApprovalResponse(PublicResponse):
    id: UUID
    account_id: UUID
    version_id: UUID
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: RiskLimits
    expires_at: AwareDatetime
    review_until: AwareDatetime
    state: Literal["draft", "approved", "revoked"]
    block_reason: str | None = Field(max_length=80)
    execution_available: Literal[False]


class ApprovalsResponse(PublicResponse):
    approval_available: bool
    approvals: Annotated[list[ApprovalResponse], Field(max_length=100)]
