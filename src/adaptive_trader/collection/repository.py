"""Storage contracts for the standalone market-data collector."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from adaptive_trader.collection.contracts import RawBarObservationV1


class CollectionPersistenceError(RuntimeError):
    """Base class for collector persistence failures."""


class SchemaNotReadyError(CollectionPersistenceError):
    """Raised when the database has not been migrated to the expected revision."""


class LeaseUnavailableError(CollectionPersistenceError):
    """Raised when another collector owns the singleton lease."""


class LeaseLostError(CollectionPersistenceError):
    """Raised when a stale collector attempts a fenced write."""


class CheckpointRegressionError(CollectionPersistenceError):
    """Raised when a coverage watermark would move backward."""


@dataclass(frozen=True, slots=True)
class CheckpointKey:
    checkpoint_name: str
    provider: str
    feed: str
    adjustment: str
    symbol: str
    timeframe: str


@dataclass(frozen=True, slots=True)
class CoverageAdvance:
    """A successfully queried half-open interval ending at ``committed_through_utc``.

    The optional last-bar fields carry lineage when observations were committed
    in an earlier database batch. They are deliberately an all-or-nothing pair.
    """

    key: CheckpointKey
    committed_through_utc: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    last_bar_timestamp_utc: datetime | None = None
    last_observation_id: str | None = None

    def __post_init__(self) -> None:
        if (self.last_bar_timestamp_utc is None) != (self.last_observation_id is None):
            raise ValueError(
                "CoverageAdvance last_bar_timestamp_utc and last_observation_id "
                "must be supplied together"
            )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    key: CheckpointKey
    committed_through_utc: datetime | None
    last_bar_timestamp_utc: datetime | None
    last_observation_id: str | None
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseToken:
    lease_name: str
    holder_id: str
    fencing_token: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BatchResult:
    received: int
    observations_inserted: int
    duplicates: int
    current_rows_inserted: int
    current_rows_revised: int
    checkpoints_advanced: int


@dataclass(frozen=True, slots=True)
class CollectorStatus:
    current_bar_count: int
    observation_count: int
    checkpoint_count: int
    open_gap_count: int
    running_run_count: int
    latest_receipt_timestamp_utc: datetime | None
    active_lease_count: int = 0
    active_run_count: int = 0


class MarketDataRepository(Protocol):
    """Transactional persistence boundary used by collection orchestration."""

    def verify_schema(self) -> None: ...

    def register_universe(self) -> None: ...

    def start_run(self, *, mode: str, lease: LeaseToken) -> str: ...

    def finish_run(
        self,
        run_id: str,
        *,
        lease: LeaseToken,
        status: str,
        counters: Mapping[str, int],
        error: str | None = None,
    ) -> None: ...

    def append_batch(
        self,
        observations: Sequence[RawBarObservationV1],
        *,
        lease: LeaseToken,
        coverage_advances: Sequence[CoverageAdvance] = (),
    ) -> BatchResult: ...

    def checkpoints(self, *, checkpoint_name: str) -> dict[str, Checkpoint]: ...

    def try_acquire_lease(
        self,
        *,
        lease_name: str,
        holder_id: str,
        ttl_seconds: int,
    ) -> LeaseToken | None: ...

    def renew_lease(self, token: LeaseToken, *, ttl_seconds: int) -> LeaseToken: ...

    def release_lease(self, token: LeaseToken) -> bool: ...

    def record_event(
        self,
        *,
        event_type: str,
        lease: LeaseToken,
        severity: str = "info",
        run_id: str | None = None,
        symbol: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> str: ...

    def status(self) -> CollectorStatus: ...

    def is_ready(self, *, lease_name: str) -> bool: ...

    def close(self) -> None: ...
