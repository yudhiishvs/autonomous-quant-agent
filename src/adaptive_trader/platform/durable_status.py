"""Bounded read-only observations; configuration never implies operational health."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError

from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.storage.engine import create_platform_read_only_engine
from adaptive_trader.platform.storage.tables import aqa_basket_watermarks, aqa_decision_slots


def observe_status(
    engine: Engine, *, experiment_hash: str, kind: Literal["data", "scheduler"], now: datetime
) -> dict[str, object]:
    """Read at most 101 rows using safe PostgreSQL views or existing offline tables."""
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("status observation requires UTC")
    if kind not in {"data", "scheduler"}:
        raise ValueError("status kind is invalid")
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    statement: Any
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            role = connection.scalar(text("SELECT current_user"))
            if role not in {
                "aqa_control",
                "aqa_control_login",
                "aqa_readonly",
                "aqa_readonly_login",
            }:
                raise ValueError("status requires control or read-only database authority")
            if kind == "data":
                statement = text(
                    "SELECT role, timeframe, status, contiguous_through, updated_at FROM aqa.aqa_basket_watermarks_v WHERE experiment_hash=:experiment ORDER BY role, timeframe LIMIT 101"
                )
            else:
                statement = text(
                    "SELECT slot_id, state, decision_type, deadline_at, lease_expires_at FROM aqa.aqa_decision_slots_v WHERE experiment_hash=:experiment AND session_date=:day ORDER BY ready_at, slot_id LIMIT 101"
                )
            rows = (
                connection.execute(statement, {"experiment": experiment_hash, "day": day})
                .mappings()
                .all()
            )
        elif engine.dialect.name == "sqlite":
            if kind == "data":
                table = aqa_basket_watermarks
                statement = (
                    select(
                        table.c.role,
                        table.c.timeframe,
                        table.c.status,
                        table.c.contiguous_through,
                        table.c.updated_at,
                    )
                    .where(table.c.experiment_hash == experiment_hash)
                    .order_by(table.c.role, table.c.timeframe)
                    .limit(101)
                )
            else:
                table = aqa_decision_slots
                statement = (
                    select(
                        table.c.slot_id,
                        table.c.state,
                        table.c.decision_type,
                        table.c.deadline_at,
                        table.c.lease_expires_at,
                    )
                    .where(table.c.experiment_hash == experiment_hash, table.c.session_date == day)
                    .order_by(table.c.ready_at, table.c.slot_id)
                    .limit(101)
                )
            rows = connection.execute(statement).mappings().all()
        else:
            raise ValueError("status storage is unsupported")
    records = [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for row in rows[:100]
    ]
    counts: dict[str, int] = {}
    for row in rows[:100]:
        state = str(row["status" if kind == "data" else "state"])
        counts[state] = counts.get(state, 0) + 1
    return {
        "observation": "observed",
        "status": "observed" if rows else "empty",
        "observed_at": now.isoformat(),
        "session_date": day.isoformat(),
        "records": records,
        "returned_count": len(records),
        "counts_within_returned_records": counts,
        "truncated": len(rows) > 100,
        "health": "not_evaluated",
        "readiness_scope": "persisted_watermarks_only"
        if kind == "data"
        else "current_session_slots_only",
    }


def durable_status(
    *,
    kind: Literal["data", "scheduler"],
    profile: Path,
    application_root: Path,
    database_url_file: Path | None = None,
) -> dict[str, object]:
    """Do not initialize storage or emit credentials when an observation is unavailable."""
    environment = {"AQA_CONFIG": (Path("configs") / profile).as_posix()}
    if database_url_file is not None:
        environment["AQA_DATABASE_URL_FILE"] = database_url_file.as_posix()
    try:
        settings = load_runtime_settings(
            environment,
            service=RuntimeService.AUDIT_VERIFIER,
            application_root=application_root.resolve(),
        )
        engine = create_platform_read_only_engine(settings, application_name="aqa-status")
        try:
            return observe_status(
                engine,
                experiment_hash=settings.platform.experiment.definition.content_hash,
                kind=kind,
                now=datetime.now(UTC),
            )
        finally:
            engine.dispose()
    except (OSError, RuntimeError, ValueError, SQLAlchemyError):
        return {
            "observation": "unavailable",
            "status": "unavailable",
            "reason": "existing_read_only_state_unavailable",
            "health": "unknown",
        }
