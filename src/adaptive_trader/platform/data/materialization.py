"""Transactional persistence service for deterministic fifteen-minute aggregates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from adaptive_trader.platform.data.aggregation import (
    AggregatedBar,
    EffectiveBar,
    SessionWindow,
    aggregate_one_minute_bars,
)
from adaptive_trader.platform.domain import DecimalRounding, quantize_decimal
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteResult,
    EligibleWatermark,
    MarketDataRepository,
)


@runtime_checkable
class AggregateStore(Protocol):
    """Narrow persistence authority required by the materializer."""

    def append(
        self,
        bar: BarWrite,
        *,
        eligible_watermark: EligibleWatermark | None = None,
    ) -> BarWriteResult:
        """Append one aggregate revision and its optional readiness projection."""


@dataclass(frozen=True, slots=True)
class MaterializationReceipt:
    """Pure aggregate and authoritative persistence result from one attempt."""

    aggregate: AggregatedBar
    write_result: BarWriteResult

    def __post_init__(self) -> None:
        if type(self.aggregate) is not AggregatedBar:
            raise TypeError("materialization receipt requires an aggregate")
        if type(self.write_result) is not BarWriteResult:
            raise TypeError("materialization receipt requires a bar write result")
        event = self.write_result.event
        if (
            event.identity.timeframe != "15Min"
            or event.bar.lineage_hash != self.aggregate.lineage_hash
            or event.bar.source_payload_hash != self.aggregate.result_hash
        ):
            raise ValueError("materialization receipt does not match its aggregate")


class FifteenMinuteMaterializer:
    """Apply the pure aggregator and append its immutable result.

    The service receives no provider client, credentials, clock, or broker authority. The ordered
    effective event IDs and revisions determine lineage; retrying identical constituents is a
    duplicate, while changing an effective constituent appends a new aggregate revision through
    the market-data repository's serialized transaction.
    """

    def __init__(self, store: AggregateStore) -> None:
        if not isinstance(store, AggregateStore):
            raise TypeError("materializer requires aggregate persistence")
        self._store = store

    @classmethod
    def from_repository(cls, repository: MarketDataRepository) -> FifteenMinuteMaterializer:
        """Construct the operational service from the canonical repository."""

        if type(repository) is not MarketDataRepository:
            raise TypeError("materializer requires a market-data repository")
        return cls(repository)

    def materialize(
        self,
        constituents: tuple[EffectiveBar, ...],
        *,
        session: SessionWindow,
        eligible_watermark: EligibleWatermark | None = None,
    ) -> MaterializationReceipt:
        """Persist exactly one complete, session-aligned fifteen-minute bucket."""

        aggregate = aggregate_one_minute_bars(constituents, session=session)
        bar = aggregate.bar
        stored_vwap = (
            None
            if bar.vwap is None
            else quantize_decimal(
                bar.vwap,
                quantum=Decimal("0.000000000000000001"),
                rounding=DecimalRounding.HALF_EVEN,
                field_name="aggregate.vwap",
            )
        )
        write = BarWrite(
            identity=BarIdentity(
                provider=bar.provider,
                feed=bar.feed,
                adjustment=bar.adjustment,
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                start_at=bar.interval_start_utc,
                end_at=bar.interval_end_utc,
            ),
            received_at=bar.receipt_timestamp_utc,
            provider_timestamp=bar.provider_event_timestamp_utc,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            trade_count=bar.trade_count,
            vwap=stored_vwap,
            quality_flags=bar.quality_flags,
            source="aggregate",
            source_mode=bar.source_mode,
            source_event_id=bar.source_event_id,
            source_payload_hash=aggregate.result_hash,
            lineage_hash=aggregate.lineage_hash,
            is_correction=bar.is_correction,
            correction_of_source_event_id=bar.correction_of_source_event_id,
            schema_version=bar.schema_version,
        )
        result = self._store.append(write, eligible_watermark=eligible_watermark)
        return MaterializationReceipt(aggregate=aggregate, write_result=result)


__all__ = [
    "AggregateStore",
    "FifteenMinuteMaterializer",
    "MaterializationReceipt",
]
