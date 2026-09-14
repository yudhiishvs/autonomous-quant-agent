"""Bounded declarative strategies producing targets, never execution authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from adaptive_trader.platform.hashing import sha256_hex

Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.]{0,9}$", max_length=10)]
WholeShares = Annotated[int, Field(strict=True, ge=0, le=100_000)]


class Contract(BaseModel):
    """Reject unknown fields and mutable nested containers in the closed models."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class ConstantTarget(Contract):
    kind: Literal["constant_target"]
    target_shares: WholeShares


class MovingAverageTarget(Contract):
    kind: Literal["moving_average_target"]
    fast_minutes: Annotated[int, Field(strict=True, ge=2, le=199)]
    slow_minutes: Annotated[int, Field(strict=True, ge=3, le=200)]
    target_shares: Annotated[int, Field(strict=True, ge=1, le=100_000)]

    @model_validator(mode="after")
    def ordered_windows(self) -> Self:
        if self.fast_minutes >= self.slow_minutes:
            raise ValueError("Fast window must be shorter than slow window.")
        return self


Rule = Annotated[ConstantTarget | MovingAverageTarget, Field(discriminator="kind")]


class MarketOrder(Contract):
    kind: Literal["market"]
    time_in_force: Literal["day"] = "day"


class LimitOrder(Contract):
    kind: Literal["limit"]
    time_in_force: Literal["day"] = "day"
    # Limits are relative to a fresh reference, never a user-supplied network quote.
    offset_bps: Annotated[int, Field(strict=True, ge=0, le=100)] = 0


OrderPolicy = Annotated[MarketOrder | LimitOrder, Field(discriminator="kind")]


class StrategyDefinition(Contract):
    schema_version: Literal[1] = 1
    symbol: Symbol
    cadence_seconds: Annotated[int, Field(strict=True, ge=60, le=86_400)] = 60
    session: Literal["regular"] = "regular"
    asset_class: Literal["us_equity"] = "us_equity"
    rule: Rule
    order: OrderPolicy

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema version must be an integer.")
        return value

    @field_validator("cadence_seconds")
    @classmethod
    def whole_minutes(cls, value: int) -> int:
        if value % 60:
            raise ValueError("Cadence must be a whole number of minutes.")
        return value

    @property
    def content_hash(self) -> str:
        """Identity includes defaults; changing execution configuration changes identity."""
        return sha256_hex(("public-strategy-v1", self.model_dump(mode="python")))


class MinuteClose(Contract):
    started_at: datetime
    close: Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=14, decimal_places=8)]

    @field_validator("close", mode="before")
    @classmethod
    def decimal_input(cls, value: object) -> object:
        if type(value) not in (str, Decimal):
            raise ValueError("Prices must be decimal strings.")
        return value

    @field_validator("started_at")
    @classmethod
    def utc_minute(cls, value: datetime) -> datetime:
        require_utc(value)
        if value.second or value.microsecond:
            raise ValueError("Bar timestamp must start on a UTC minute.")
        return value.astimezone(UTC)


class MarketObservation(Contract):
    """Adapter-validated provenance is still required before using this value at runtime."""

    symbol: Symbol
    feed: Literal["iex", "sip"]
    observed_at: datetime
    regular_session_open: bool = Field(strict=True)
    asset_eligible: bool = Field(strict=True)
    bars: Annotated[tuple[MinuteClose, ...], Field(min_length=1, max_length=200)]

    @field_validator("observed_at")
    @classmethod
    def utc_observed(cls, value: datetime) -> datetime:
        require_utc(value)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_completed_bars(self) -> Self:
        for previous, current in zip(self.bars, self.bars[1:], strict=False):
            if current.started_at - previous.started_at != timedelta(minutes=1):
                raise ValueError("Bars must be consecutive, ordered and unique.")
        if self.bars[-1].started_at + timedelta(minutes=1) > self.observed_at:
            raise ValueError("Only completed bars can be evaluated.")
        return self


class TargetProposal(Contract):
    strategy_hash: str
    symbol: Symbol
    evaluated_at: datetime
    expires_at: datetime
    target_shares: WholeShares | None
    reason: Literal[
        "constant_target",
        "trend_above",
        "trend_not_above",
        "market_closed",
        "asset_ineligible",
        "data_stale",
        "data_in_future",
        "insufficient_history",
    ]
    observation_hash: str


def require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must be timezone-aware UTC.")


def evaluate(
    definition: StrategyDefinition, observation: MarketObservation, *, now: datetime
) -> TargetProposal:
    """Evaluate a single current observation; no replay, positions, fills or broker calls.

    None means no proposal, including no liquidation. A zero target is an explicit
    flat proposal that still requires independent approval and execution-time risk.
    Inputs must come from the application's trusted data adapter, never a browser flag.
    """
    require_utc(now)
    definition = StrategyDefinition.model_validate(definition)
    observation = MarketObservation.model_validate(observation)
    if definition.symbol != observation.symbol:
        raise ValueError("Observation symbol differs from strategy.")
    now = now.astimezone(UTC)
    latest_end = observation.bars[-1].started_at + timedelta(minutes=1)
    target: int | None = None
    reason: Literal[
        "constant_target",
        "trend_above",
        "trend_not_above",
        "market_closed",
        "asset_ineligible",
        "data_stale",
        "data_in_future",
        "insufficient_history",
    ]
    if observation.observed_at > now:
        reason = "data_in_future"
    elif now - observation.observed_at > timedelta(seconds=30) or now - latest_end >= timedelta(
        seconds=60
    ):
        reason = "data_stale"
    elif not observation.regular_session_open:
        reason = "market_closed"
    elif not observation.asset_eligible:
        reason = "asset_ineligible"
    elif isinstance(definition.rule, ConstantTarget):
        target = definition.rule.target_shares
        reason = "constant_target"
    elif len(observation.bars) < definition.rule.slow_minutes:
        reason = "insufficient_history"
    else:
        rule = definition.rule
        # Cross multiplication avoids rounded division and ambient decimal context.
        with localcontext() as context:
            context.prec = 40
            fast = sum((bar.close for bar in observation.bars[-rule.fast_minutes :]), Decimal(0))
            slow = sum((bar.close for bar in observation.bars[-rule.slow_minutes :]), Decimal(0))
            above = fast * rule.slow_minutes > slow * rule.fast_minutes
        target = rule.target_shares if above else 0
        reason = "trend_above" if above else "trend_not_above"
    return TargetProposal(
        strategy_hash=definition.content_hash,
        symbol=definition.symbol,
        evaluated_at=now,
        expires_at=min(now + timedelta(seconds=30), latest_end + timedelta(seconds=60)),
        target_shares=target,
        reason=reason,
        observation_hash=sha256_hex(
            ("public-observation-v1", observation.model_dump(mode="python"))
        ),
    )
