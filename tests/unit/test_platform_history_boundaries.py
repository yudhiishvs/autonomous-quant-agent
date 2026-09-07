"""Bounded causal risk history and fail-closed shadow lease regressions."""

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import delete, select

from adaptive_trader.platform.scheduling import DecisionSlotRepository
from adaptive_trader.platform.service_cycles import _complete_history
from adaptive_trader.platform.shadow import run_shadow_once
from adaptive_trader.platform.shadow_settings import load_shadow_execution_settings
from adaptive_trader.platform.storage.tables import aqa_bar_identities, aqa_bar_latest
from tests.unit.test_platform_shadow import ROOT, engine, seed, settings

__all__ = ["engine"]


def test_latest_twenty_sessions_are_required_without_older_gap_substitution(engine):
    slot = seed(engine)
    symbols = settings().platform.experiment.definition.active_tradable
    history = _complete_history(engine, active_symbols=symbols, before_session=slot.session_date)
    assert history is not None
    assert all(len(sessions) == 20 for sessions in history.values())
    latest_date = datetime.combine(history[symbols[0]][-1].session_date, time(), tzinfo=UTC)
    with engine.begin() as connection:
        identity = connection.scalar(
            select(aqa_bar_identities.c.bar_identity_id)
            .where(
                aqa_bar_identities.c.symbol == symbols[0],
                aqa_bar_identities.c.timeframe == "15Min",
                aqa_bar_identities.c.start_at >= latest_date,
                aqa_bar_identities.c.start_at < latest_date + timedelta(days=1),
            )
            .limit(1)
        )
        assert identity is not None
        connection.execute(
            delete(aqa_bar_latest).where(aqa_bar_latest.c.bar_identity_id == identity)
        )
    assert (
        _complete_history(engine, active_symbols=symbols, before_session=slot.session_date) is None
    )
    assert _complete_history(engine, active_symbols=(), before_session=slot.session_date) is None


def test_shadow_expired_claim_is_rejected_before_reading_operational_inputs(engine, monkeypatch):
    slot = seed(engine)
    repository = DecisionSlotRepository(engine)
    claimed_at = slot.ready_at + timedelta(seconds=1)
    repository.evaluate_readiness(
        slot.slot_id, active_basket_watermark=slot.source_interval_end, now=claimed_at
    )
    claimed = repository.claim(slot.slot_id, owner="strategy_worker", now=claimed_at).slot
    assert claimed.lease_expires_at is not None
    assert claimed.lease_expires_at < slot.deadline_at
    operational = load_shadow_execution_settings(
        {
            "AQA_CONFIG": "configs/platform/shadow.yaml",
            "AQA_DATABASE_URL_FILE": "/run/secrets/database_url",
        },
        application_root=ROOT,
    )
    # SQLite exercises durable lease state; PostgreSQL role enforcement has separate real-login tests.
    monkeypatch.setattr(
        "adaptive_trader.platform.shadow.require_shadow_database_role", lambda *args, **kwargs: None
    )
    result = run_shadow_once(
        engine=engine, settings=operational, slot_id=slot.slot_id, now=claimed.lease_expires_at
    )
    assert result["status"] == "blocked"
    assert result["reason_code"] == "decision_slot_unavailable"
