"""Bounded migration replay and periodic recovery of older canonical gaps."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select

from adaptive_trader.collection.postgres import PostgresMarketDataRepository
from adaptive_trader.collection.repository import CoverageAdvance, LeaseToken
from adaptive_trader.collection.schema import collector_events
from adaptive_trader.collection.service import HistoricalRepairWindow, completed_bar_cutoff
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.storage.tables import aqa_data_gaps

_REPLAY_EVENT = "canonical_projection_v2_rebuilt"
_REPAIR_EVENT = "canonical_gap_reconciliation_started"
_REPAIR_INTERVAL = timedelta(minutes=5)


def restore_canonical_projection(
    repository: PostgresMarketDataRepository,
    *,
    current_lease: Callable[[], LeaseToken],
    run_id: str,
    history_start: datetime,
    now: datetime,
) -> None:
    """Import pre-convergence current rows once, retrying safely after a crash.

    The immutable completion event follows the last committed page. An interrupted
    import restarts its keyset scan; canonical economic identity makes that replay
    idempotent. Intake starts only after this callback finishes under the live lease.
    """

    history_start = require_utc_instant(history_start, field_name="history_start")
    cutoff = completed_bar_cutoff(now, lag_minutes=2)
    current_lease()
    with repository.engine.connect() as connection:
        done = connection.scalar(
            select(collector_events.c.event_id)
            .where(collector_events.c.event_type == _REPLAY_EVENT)
            .limit(1)
        )
    if done is not None:
        return
    cursor = None
    total = 0
    while True:
        count, cursor = repository.rebuild_canonical_batch(
            lease=current_lease(), after_identity_hash=cursor, limit=750
        )
        total += count
        if count == 0:
            break
    # Empty but inspected history has no current row to replay. Enqueue it too,
    # otherwise an old IEX gap would block readiness without becoming repairable.
    from adaptive_trader.collection.canonical import mirror_current

    checkpoints = repository.checkpoints(checkpoint_name="rest_coverage")
    boundaries = [
        checkpoint.committed_through_utc
        for checkpoint in checkpoints.values()
        if checkpoint.committed_through_utc is not None
    ]
    through = max(boundaries, default=history_start)
    if through > cutoff:
        raise ValueError("stored recovery coverage exceeds completed history")
    window_start = history_start
    while window_start < through:
        window_end = min(window_start + timedelta(days=7), through)
        advances = tuple(
            CoverageAdvance(
                key=checkpoint.key,
                committed_through_utc=boundary,
                metadata={
                    "source": "canonical_coverage_rebuild",
                    "window_start": window_start.isoformat(),
                    "window_end": min(boundary, window_end).isoformat(),
                },
            )
            for checkpoint in checkpoints.values()
            if (boundary := checkpoint.committed_through_utc) is not None
            and boundary > window_start
        )
        with repository.engine.begin() as connection:
            repository.validate_ownership(connection, lease=current_lease())
            mirror_current(connection, repository.engine, (), advances)
        window_start = window_end
    repository.record_event(
        event_type=_REPLAY_EVENT,
        lease=current_lease(),
        run_id=run_id,
        details={"projection_count": total, "universe_hash": COLLECTION_UNIVERSE_V1.universe_hash},
    )


def next_gap_repair(
    repository: PostgresMarketDataRepository,
    *,
    experiment: ExperimentDefinition,
    history_start: datetime,
    now: datetime,
) -> HistoricalRepairWindow | None:
    """Rotate through persisted gaps with at most one extra session fetch per five minutes.

    The cursor and cooldown come from the durable attempt event, so a restart cannot
    create a retry storm or repeatedly starve later gaps. Missing IEX trades remain
    missing; completed REST requests do not themselves resolve a canonical gap.
    """

    now = require_utc_instant(now, field_name="repair_clock")
    history_start = require_utc_instant(history_start, field_name="history_start")
    with repository.engine.connect() as connection:
        previous = (
            connection.execute(
                select(collector_events.c.occurred_at, collector_events.c.details)
                .where(collector_events.c.event_type == _REPAIR_EVENT)
                .order_by(collector_events.c.occurred_at.desc(), collector_events.c.event_id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        cursor = None
        if previous is not None:
            if now - previous["occurred_at"] < _REPAIR_INTERVAL:
                return None
            cursor = previous["details"].get("gap_id")
            if type(cursor) is not str:
                raise ValueError("persisted gap repair cursor is invalid")
        candidates = (
            select(aqa_data_gaps.c.gap_id, aqa_data_gaps.c.gap_start_at, aqa_data_gaps.c.gap_end_at)
            .where(
                aqa_data_gaps.c.experiment_hash == experiment.content_hash,
                aqa_data_gaps.c.provider == "alpaca",
                aqa_data_gaps.c.feed == "iex",
                aqa_data_gaps.c.adjustment == "raw",
                aqa_data_gaps.c.timeframe == "1Min",
                aqa_data_gaps.c.symbol.in_(experiment.collection_allowlist),
                aqa_data_gaps.c.status.in_(("open", "repairing")),
                aqa_data_gaps.c.gap_start_at >= history_start,
                aqa_data_gaps.c.gap_end_at <= completed_bar_cutoff(now, lag_minutes=2),
            )
            .order_by(aqa_data_gaps.c.gap_id)
            .limit(1)
        )
        row = None
        if cursor is not None:
            row = (
                connection.execute(candidates.where(aqa_data_gaps.c.gap_id > cursor))
                .mappings()
                .one_or_none()
            )
        if row is None:
            row = connection.execute(candidates).mappings().one_or_none()
    if row is None:
        return None
    return HistoricalRepairWindow(row["gap_id"], row["gap_start_at"], row["gap_end_at"])
