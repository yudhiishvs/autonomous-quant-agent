"""Explicit non-AI approval bindings; independent risk and execution remain mandatory."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.public_product.strategies import Contract, StrategyDefinition, require_utc

Money = Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=12, decimal_places=2)]


class RiskLimits(Contract):
    """User ceilings, never permission to ignore current account-wide risk or available cash."""

    max_order_notional: Money
    max_account_exposure: Money
    max_position_shares: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    max_daily_loss: Money
    max_daily_turnover: Annotated[
        Decimal, Field(gt=0, le=10_000_000, max_digits=12, decimal_places=2)
    ]
    max_orders_per_minute: Annotated[int, Field(strict=True, ge=1, le=10)] = 2
    validity_days: Literal[1, 7, 30] = 1

    @field_validator(
        "max_order_notional",
        "max_account_exposure",
        "max_daily_loss",
        "max_daily_turnover",
        mode="before",
    )
    @classmethod
    def decimal_values(cls, value: object) -> object:
        if type(value) not in (str, Decimal) or len(str(value)) > 32:
            raise ValueError("Risk amounts must be bounded decimal strings.")
        return value

    @field_validator("validity_days", mode="before")
    @classmethod
    def integer_validity(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Validity must be a supported integer day count.")
        return value

    @model_validator(mode="after")
    def ordered_limits(self) -> Self:
        if self.max_order_notional > self.max_account_exposure:
            raise ValueError("Order limit cannot exceed account exposure limit.")
        if self.max_daily_loss > self.max_account_exposure:
            raise ValueError("Daily loss limit cannot exceed account exposure limit.")
        if self.max_daily_turnover < self.max_order_notional:
            raise ValueError("Daily turnover limit must cover one allowed order.")
        return self

    @property
    def content_hash(self) -> str:
        return sha256_hex(("public-risk-limits-v1", self.model_dump(mode="python")))


def validate_approval_limits(
    definition: StrategyDefinition, limits: RiskLimits, equity: Decimal
) -> None:
    definition = StrategyDefinition.model_validate(definition)
    limits = RiskLimits.model_validate(limits)
    if type(equity) is not Decimal or not equity.is_finite() or equity <= 0:
        raise ValueError("Positive verified paper account equity is required.")
    if limits.max_account_exposure > equity:
        raise ValueError(
            "Exposure limit exceeds verified account equity. Leverage is not supported."
        )
    if definition.rule.target_shares > limits.max_position_shares:
        raise ValueError("Strategy target exceeds the approved position limit.")


class ApprovalBinding(Contract):
    approval_id: UUID
    owner_id: UUID
    account_id: UUID
    version_id: UUID
    strategy_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    broker_account_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    connection_generation: Annotated[int, Field(strict=True, ge=0)]
    limits: RiskLimits
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        require_utc(value)
        return value.astimezone(UTC)

    @property
    def content_hash(self) -> str:
        return sha256_hex(
            {
                "schema": "public-approval-v1",
                "approval_id": str(self.approval_id),
                "owner_id": str(self.owner_id),
                "account_id": str(self.account_id),
                "version_id": str(self.version_id),
                "strategy_hash": self.strategy_hash,
                "broker_account_hash": self.broker_account_hash,
                "connection_generation": self.connection_generation,
                "risk_hash": self.limits.content_hash,
                "expires_at": self.expires_at,
            }
        )


def approval_block_reason(
    binding: ApprovalBinding,
    *,
    stored_hash: str,
    state: str,
    owner_id: UUID,
    account_id: UUID,
    version_id: UUID,
    strategy_hash: str,
    broker_account_hash: str,
    connection_generation: int,
    account_connected: bool,
    now: datetime,
) -> str | None:
    """Check approval only. A match never substitutes for risk, reconciliation or execution gates."""
    binding = ApprovalBinding.model_validate(binding)
    require_utc(now)
    if binding.content_hash != stored_hash:
        return "approval_integrity_failed"
    if (
        binding.owner_id,
        binding.account_id,
        binding.version_id,
        binding.strategy_hash,
        binding.broker_account_hash,
    ) != (
        owner_id,
        account_id,
        version_id,
        strategy_hash,
        broker_account_hash,
    ):
        return "approval_binding_changed"
    if type(account_connected) is not bool or not account_connected:
        return "account_disconnected"
    if (
        type(connection_generation) is not int
        or binding.connection_generation != connection_generation
    ):
        return "connection_changed"
    if state != "approved":
        return "approval_" + (state if state in {"draft", "revoked"} else "invalid")
    if now >= binding.expires_at:
        return "approval_expired"
    return None
