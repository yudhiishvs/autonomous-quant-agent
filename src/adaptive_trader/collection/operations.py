"""Canonical collection configuration and read-only operational evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, and_, case, func, select
from sqlalchemy.dialects.postgresql import insert

from adaptive_trader.collection.schema import (
    canonical_work,
    collector_checkpoints,
    collector_configuration,
    collector_events,
    collector_leases,
    ingestion_runs,
)
from adaptive_trader.collection.service import CollectorServiceConfig, completed_bar_cutoff
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.config import ExperimentDefinition, load_experiment
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.package_resources import packaged_config_root
from adaptive_trader.platform.storage.tables import (
    aqa_bar_identities,
    aqa_bar_latest,
    aqa_basket_watermarks,
    aqa_data_gaps,
)


def collection_experiment(config_root: Path | None = None) -> ExperimentDefinition:
    """Read the approved research identity without extending its research membership."""

    experiment = load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=packaged_config_root() if config_root is None else config_root,
    )
    if set(experiment.collection_allowlist) | set(experiment.excluded) != set(
        COLLECTION_UNIVERSE_V1.symbols
    ):
        raise ValueError("research and collection universe contracts disagree")
    return experiment


def ensure_configuration(
    engine: Engine,
    *,
    experiment: ExperimentDefinition,
    history_start: datetime | None,
    now: datetime | None = None,
) -> datetime:
    """Persist initial collection scope once; never silently move the history boundary."""

    with engine.begin() as connection:
        if history_start is not None:
            if history_start.tzinfo is None or history_start.utcoffset() is None:
                raise ValueError("collection history start must be timezone-aware")
            history_start = history_start.astimezone(UTC)
            existing = connection.scalar(
                select(collector_configuration.c.history_start).where(
                    collector_configuration.c.name == "canonical"
                )
            )
            if existing is None:
                cutoff = completed_bar_cutoff(now or datetime.now(UTC), lag_minutes=2)
                if history_start.second or history_start.microsecond:
                    raise ValueError("collection history start must align to a minute")
                if history_start >= cutoff:
                    raise ValueError("collection history start must precede completed history")
            connection.execute(
                insert(collector_configuration)
                .values(
                    name="canonical",
                    universe_hash=COLLECTION_UNIVERSE_V1.universe_hash,
                    experiment_hash=experiment.content_hash,
                    history_start=history_start,
                )
                .on_conflict_do_nothing()
            )
        row = (
            connection.execute(
                select(collector_configuration).where(collector_configuration.c.name == "canonical")
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("AQA_MARKET_DATA_HISTORY_START is required for first activation")
        if (
            row["universe_hash"] != COLLECTION_UNIVERSE_V1.universe_hash
            or row["experiment_hash"] != experiment.content_hash
            or (history_start is not None and row["history_start"] != history_start)
        ):
            raise ValueError("canonical collection configuration differs from durable history")
        return datetime.fromisoformat(row["history_start"].isoformat()).astimezone(UTC)


def expected_completed_bar(now: datetime) -> tuple[datetime, bool]:
    """Find the latest expected regular-session minute, including weekends/early closes."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("freshness clock must be timezone-aware")
    now = now.astimezone(UTC)
    cutoff = completed_bar_cutoff(now, lag_minutes=2)
    calendar = XnasExchangeCalendar()
    today = calendar.session(now.date())
    market_closed = today is None or not (today.open_at <= now < today.close_at)
    for offset in range(370):
        session = calendar.session(now.date() - timedelta(days=offset))
        if session is not None and cutoff > session.open_at:
            return min(cutoff, session.close_at), market_closed
    raise ValueError("no completed session exists within the supported calendar window")


def health_snapshot(engine: Engine, *, now: datetime) -> dict[str, Any]:
    """Distinguish durable data readiness, one-shot completion, and daemon liveness."""

    expected, market_closed = expected_completed_bar(now)
    experiment = collection_experiment()
    with engine.connect() as connection:
        configuration = (
            connection.execute(
                select(collector_configuration).where(collector_configuration.c.name == "canonical")
            )
            .mappings()
            .one_or_none()
        )
        configured = bool(
            configuration is not None
            and configuration["universe_hash"] == COLLECTION_UNIVERSE_V1.universe_hash
            and configuration["experiment_hash"] == experiment.content_hash
            and configuration["history_start"] < expected
        )
        failures: dict[str, int] = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                select(collector_events.c.event_type, func.count())
                .where(
                    collector_events.c.occurred_at >= now - timedelta(hours=1),
                    collector_events.c.occurred_at <= now,
                    collector_events.c.event_type.in_(
                        (
                            "historical_request_retry",
                            "historical_reconciliation_failed",
                            "market_data_stream_failed",
                            "market_data_stream_restart",
                        )
                    ),
                )
                .group_by(collector_events.c.event_type)
            ).all()
        }
        last_reconciled = connection.scalar(
            select(func.max(collector_events.c.occurred_at)).where(
                collector_events.c.event_type == "historical_window_committed",
                collector_events.c.occurred_at <= now,
            )
        )
        last_invocation = (
            connection.execute(
                select(ingestion_runs.c.status, ingestion_runs.c.completed_at)
                .where(
                    ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                    ingestion_runs.c.lease_name == CollectorServiceConfig().lease_name,
                    ingestion_runs.c.mode == "backfill",
                    ingestion_runs.c.started_at <= now,
                )
                .order_by(ingestion_runs.c.started_at.desc(), ingestion_runs.c.run_id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        runs = connection.execute(
            select(ingestion_runs.c.run_id, ingestion_runs.c.lease_name)
            .join(
                collector_leases,
                and_(
                    ingestion_runs.c.lease_name == collector_leases.c.lease_name,
                    ingestion_runs.c.holder_id == collector_leases.c.holder_id,
                    ingestion_runs.c.fencing_token == collector_leases.c.fencing_token,
                ),
            )
            .where(
                ingestion_runs.c.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash,
                ingestion_runs.c.status == "running",
                collector_leases.c.expires_at > now,
            )
        ).all()
        stream_state = None
        owned = len(runs) == 1 and runs[0].lease_name == CollectorServiceConfig().lease_name
        if owned:
            stream_state = connection.scalar(
                select(collector_events.c.event_type)
                .where(
                    collector_events.c.run_id == runs[0].run_id,
                    collector_events.c.event_type.in_(
                        (
                            "market_data_stream_subscribed",
                            "market_data_stream_disconnected",
                            "market_data_stream_starting",
                            "market_data_stream_authenticated",
                            "market_data_stream_restart",
                            "market_data_stream_failed",
                        )
                    ),
                )
                .order_by(
                    collector_events.c.occurred_at.desc(),
                    case(
                        (collector_events.c.event_type == "market_data_stream_subscribed", 0),
                        else_=1,
                    ).desc(),
                )
                .limit(1)
            )
        checkpoints: dict[str, datetime | None] = {
            str(row[0]): row[1]
            for row in connection.execute(
                select(
                    collector_checkpoints.c.symbol,
                    collector_checkpoints.c.committed_through_utc,
                ).where(
                    collector_checkpoints.c.checkpoint_name == "rest_coverage",
                    collector_checkpoints.c.provider == "alpaca",
                    collector_checkpoints.c.feed == "IEX",
                    collector_checkpoints.c.adjustment == "raw",
                    collector_checkpoints.c.timeframe == "1m",
                )
            ).all()
        }
        latest: dict[str, datetime | None] = {
            str(row[0]): row[1]
            for row in connection.execute(
                select(
                    aqa_bar_identities.c.symbol,
                    func.max(aqa_bar_identities.c.end_at),
                )
                .join(
                    aqa_bar_latest,
                    aqa_bar_latest.c.bar_identity_id == aqa_bar_identities.c.bar_identity_id,
                )
                .where(
                    aqa_bar_identities.c.provider == "alpaca",
                    aqa_bar_identities.c.feed == "iex",
                    aqa_bar_identities.c.adjustment == "raw",
                    aqa_bar_identities.c.timeframe == "1Min",
                    aqa_bar_identities.c.end_at <= expected,
                )
                .group_by(aqa_bar_identities.c.symbol)
            ).all()
        }
        pending = connection.scalar(select(func.count()).select_from(canonical_work)) or 0
        # Every unresolved required-symbol gap blocks research; collection-only names
        # remain visible without acquiring research authority.
        gap_count = (
            connection.scalar(
                select(func.count())
                .select_from(aqa_data_gaps)
                .where(
                    aqa_data_gaps.c.experiment_hash == experiment.content_hash,
                    aqa_data_gaps.c.provider == "alpaca",
                    aqa_data_gaps.c.feed == "iex",
                    aqa_data_gaps.c.adjustment == "raw",
                    aqa_data_gaps.c.symbol.in_(experiment.active_tradable),
                    aqa_data_gaps.c.status.in_(("open", "repairing")),
                )
            )
            or 0
        )
        basket = (
            connection.execute(
                select(aqa_basket_watermarks).where(
                    aqa_basket_watermarks.c.experiment_hash == experiment.content_hash,
                    aqa_basket_watermarks.c.timeframe == "15Min",
                    aqa_basket_watermarks.c.role == "active",
                )
            )
            .mappings()
            .one_or_none()
        )
    lagging_checkpoints = sorted(
        symbol
        for symbol in COLLECTION_UNIVERSE_V1.symbols
        if (boundary := checkpoints.get(symbol)) is None or boundary < expected
    )
    stale = sorted(
        symbol
        for symbol in experiment.active_tradable
        if (boundary := latest.get(symbol)) is None or boundary < expected
    )
    subscribed = stream_state == "market_data_stream_subscribed"
    coverage_ready = configured and not lagging_checkpoints and not pending
    ready = coverage_ready and owned and subscribed
    aggregate_session = XnasExchangeCalendar().session(expected.date())
    assert aggregate_session is not None
    aggregate_end = aggregate_session.open_at + timedelta(
        minutes=int((expected - aggregate_session.open_at).total_seconds() // 900) * 15
    )
    basket_ready = bool(
        basket is not None
        and basket["status"] == "ready"
        and basket["contiguous_through"] is not None
        and basket["contiguous_through"] >= aggregate_end
    )
    return {
        "provider": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "calendar": "XNAS",
        "universe_count": len(COLLECTION_UNIVERSE_V1.symbols),
        "universe_hash": COLLECTION_UNIVERSE_V1.universe_hash,
        "experiment_hash": experiment.content_hash,
        "database_healthy": True,
        "configuration_valid": configured,
        "stream_state": stream_state,
        "provider_errors_last_hour": sum(failures.values()),
        "reconciliation_failures_last_hour": failures.get("historical_reconciliation_failed", 0),
        "last_historical_commit_at": None
        if last_reconciled is None
        else last_reconciled.isoformat(),
        "active_runs": len(runs),
        "last_invocation_status": None if last_invocation is None else last_invocation["status"],
        "last_invocation_completed_at": (
            None
            if last_invocation is None or last_invocation["completed_at"] is None
            else last_invocation["completed_at"].isoformat()
        ),
        "subscribed": subscribed,
        "market_closed": market_closed,
        "expected_completed_through": expected.isoformat(),
        "checkpoint_lag_symbols": lagging_checkpoints,
        "stale_research_symbols": stale,
        "pending_derived_sessions": int(pending),
        "unresolved_research_gaps": int(gap_count),
        "service_ready": ready,
        "coverage_ready": coverage_ready,
        "research_ready": coverage_ready and not stale and not gap_count and basket_ready,
        "aggregate_basket_ready": basket_ready,
    }
