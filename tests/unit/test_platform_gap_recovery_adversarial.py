"""Restart and rejected transitions preserve durable gap ownership and evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update

from adaptive_trader.platform.data.watermarks import (
    GapRepairCoverage,
    GapRepository,
    GapStatus,
    ReadinessIntegrityError,
    ReadinessValidationError,
)
from adaptive_trader.platform.storage.tables import aqa_data_gaps
from tests.unit.test_platform_watermarks import (
    _detect,
    _interval,
    _series,
    calendar,
    sqlite_engine,
)

__all__ = ["calendar", "sqlite_engine"]


@pytest.fixture
def gap_state(sqlite_engine, calendar):
    repository = GapRepository(sqlite_engine, calendar=calendar)
    gap = repository.record(_detect(calendar, _series(), observed=(_interval(0), _interval(2)))[0])
    coverage = GapRepairCoverage(
        series=gap.series,
        start_at=gap.start_at,
        end_at=gap.end_at,
        observed_intervals=(_interval(1),),
        completed_at=gap.detected_at + timedelta(seconds=3),
    )
    return repository, gap, coverage


@pytest.mark.parametrize(
    "attack",
    [
        "complete_unclaimed",
        "claim_before_detection",
        "reclaim_other_time",
        "complete_before_attempt",
        "reopen_before_attempt",
        "wrong_range",
        "wrong_series",
    ],
)
def test_rejected_gap_transition_leaves_restart_state_unchanged(
    gap_state, sqlite_engine, calendar, attack
):
    repository, gap, coverage = gap_state
    if attack in {
        "reclaim_other_time",
        "complete_before_attempt",
        "reopen_before_attempt",
        "wrong_range",
        "wrong_series",
    }:
        gap = repository.begin_repair(
            gap.gap_id, attempted_at=gap.detected_at + timedelta(seconds=2)
        )
    with pytest.raises(ReadinessValidationError):
        if attack == "complete_unclaimed":
            repository.complete_repair(gap.gap_id, coverage=coverage)
        elif attack == "claim_before_detection":
            repository.begin_repair(gap.gap_id, attempted_at=gap.detected_at - timedelta(seconds=1))
        elif attack == "reclaim_other_time":
            repository.begin_repair(gap.gap_id, attempted_at=coverage.completed_at)
        elif attack == "reopen_before_attempt":
            repository.reopen_interrupted(gap.gap_id, reopened_at=gap.detected_at)
        else:
            changes = {
                "complete_before_attempt": {"completed_at": gap.detected_at},
                "wrong_range": {"end_at": gap.end_at + timedelta(minutes=1)},
                "wrong_series": {"series": _series("AMD")},
            }[attack]
            repository.complete_repair(gap.gap_id, coverage=replace(coverage, **changes))
    assert GapRepository(sqlite_engine, calendar=calendar).get(gap.gap_id) == gap


def test_completed_repair_retries_are_idempotent_and_cannot_regress(
    gap_state, sqlite_engine, calendar
):
    repository, gap, coverage = gap_state
    assert repository.reopen_interrupted(gap.gap_id, reopened_at=coverage.completed_at) == gap
    claimed = repository.begin_repair(gap.gap_id, attempted_at=gap.detected_at)
    assert repository.begin_repair(gap.gap_id, attempted_at=gap.detected_at) == claimed
    resolved = repository.complete_repair(gap.gap_id, coverage=coverage)
    restarted = GapRepository(sqlite_engine, calendar=calendar)
    assert restarted.begin_repair(gap.gap_id, attempted_at=coverage.completed_at) == resolved
    assert restarted.complete_repair(gap.gap_id, coverage=coverage) == resolved
    assert resolved.status is GapStatus.RESOLVED
    with pytest.raises(ReadinessValidationError):
        restarted.complete_repair(gap.gap_id, coverage=replace(coverage, observed_intervals=()))
    assert restarted.get(gap.gap_id) == resolved


@pytest.mark.parametrize(
    "change",
    [
        {"content_hash": "f" * 64},
        {"reason_code": "changed_reason"},
        {"attempt_count": 1},
        {"last_attempt_at": _interval(0).start_at},
        {"detected_at": _interval(0).start_at},
        {"resolved_at": _interval(2).end_at},
        {"version": 2},
        {"symbol": "AMD"},
        {"feed": "sip"},
        {"adjustment": "all"},
        {"timeframe": "5Min"},
        {"provider": "other"},
    ],
)
def test_persisted_gap_corruption_is_never_repairable_after_restart(
    gap_state, sqlite_engine, calendar, change
):
    _, gap, _ = gap_state
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_data_gaps).where(aqa_data_gaps.c.gap_id == gap.gap_id).values(**change)
        )
    restarted = GapRepository(sqlite_engine, calendar=calendar)
    with pytest.raises((ReadinessIntegrityError, ReadinessValidationError)):
        restarted.get(gap.gap_id)
    with pytest.raises((ReadinessIntegrityError, ReadinessValidationError)):
        restarted.begin_repair(gap.gap_id, attempted_at=gap.detected_at + timedelta(seconds=5))
