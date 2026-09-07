"""Malformed authority-bearing inputs must fail before risk or repair can use them."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import permutations

import pytest

from adaptive_trader.platform.data.watermarks import (
    BarQualityPolicy,
    GapRepository,
    ReadinessIntegrityError,
    ReadinessValidationError,
)
from adaptive_trader.platform.risk.models import (
    AccountSnapshot,
    OpenOrderSnapshot,
    PlanningPrice,
    ReconciliationSnapshot,
    SignedRiskValidationError,
)
from adaptive_trader.platform.signals import SignalValidationError
from tests.unit.test_platform_signals import _context, _fixture_provider, _rehash, experiment
from tests.unit.test_platform_watermarks import (
    _detect,
    _interval,
    _series,
    calendar,
    sqlite_engine,
)

__all__ = ["calendar", "experiment", "sqlite_engine"]
NOW = datetime(2026, 7, 6, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.0, True, "10"],
)
@pytest.mark.parametrize("field", ["equity", "cash", "buying_power"])
def test_account_cannot_coerce_nonfinite_or_untyped_money(field, value):
    account = AccountSnapshot("a" * 64, Decimal("100"), Decimal("100"), Decimal("100"), NOW)
    with pytest.raises(SignedRiskValidationError):
        replace(account, **{field: value})


@pytest.mark.parametrize("field", ["equity", "buying_power"])
def test_negative_account_authority_is_rejected_but_signed_cash_is_representable(field):
    account = AccountSnapshot("a" * 64, Decimal("100"), Decimal("-10"), Decimal("100"), NOW)
    assert account.cash == Decimal("-10")
    with pytest.raises(SignedRiskValidationError):
        replace(account, **{field: Decimal("-0.01")})


@pytest.mark.parametrize(
    "change",
    [
        {"price": Decimal(0)},
        {"price": Decimal(-1)},
        {"validated": 1},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"symbol": "nvda"},
    ],
)
def test_planning_price_requires_positive_typed_timestamped_provenance(change):
    with pytest.raises(SignedRiskValidationError):
        replace(PlanningPrice("NVDA", Decimal(100), NOW, True), **change)


def test_reserved_exposure_order_is_canonical_and_tampering_cannot_reuse_authority():
    pairs = [("AMD", Decimal("-10")), ("NVDA", Decimal("20")), ("TSM", Decimal("0"))]
    records = [
        OpenOrderSnapshot.create(
            reserved_signed_notional=dict(order),
            conflicting_symbols=("NVDA",),
            ambiguous_order_exists=False,
            observed_at=NOW,
        )
        for order in permutations(pairs)
    ]
    assert len({item.content_hash for item in records}) == 1
    for change in (
        {"ambiguous_order_exists": True},
        {"observed_at": NOW + timedelta(seconds=1)},
        {
            "reserved_signed_notional": (
                ("AMD", Decimal("-11")),
                ("NVDA", Decimal("20")),
                ("TSM", Decimal(0)),
            )
        },
        {"conflicting_symbols": ("QQQ",)},
        {"ambiguous_order_exists": 0},
    ):
        with pytest.raises(SignedRiskValidationError):
            replace(records[0], **change)


@pytest.mark.parametrize(
    "change",
    [
        {"reconciled": False},
        {"ambiguous_order_exists": True},
        {"observed_at": NOW + timedelta(seconds=1)},
        {"reconciled": 1},
    ],
)
def test_reconciliation_authority_is_bound_to_flags_and_observation(change):
    snapshot = ReconciliationSnapshot.create(
        reconciled=True, ambiguous_order_exists=False, observed_at=NOW
    )
    with pytest.raises(SignedRiskValidationError):
        replace(snapshot, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"active_symbols": ()},
        {"active_symbols": ("NVDA", "AMD")},
        {"strategy_slot_ordinal": True},
        {"strategy_slot_ordinal": -1},
        {"strategy_slot_ordinal": 20},
        {"execution_mode": "offline"},
        {"broker_adapter": "fake"},
        {"submission_enabled": 0},
    ],
)
def test_strategy_context_rejects_coercion_or_ambiguous_symbol_order(experiment, change):
    with pytest.raises(SignalValidationError):
        replace(_context(experiment), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"artifact_id": "../escape", "artifact_hash": "a" * 64},
        {"artifact_id": "model"},
        {"paper_submission_eligible": True},
        {"promotable": True},
        {"experiment_version": True},
        {"provider_id": "../plugin"},
    ],
)
def test_rehashed_fixture_cannot_gain_authority_or_invalid_provenance(experiment, change):
    context = _context(experiment)
    envelope = _fixture_provider(context).signal_for(context)
    with pytest.raises(SignalValidationError):
        _rehash(envelope, **change)


@pytest.mark.parametrize("field", ["created_at", "expires_at", "source_bar_end"])
def test_rehashed_signal_cannot_reverse_observation_time(experiment, field):
    context = _context(experiment)
    envelope = _fixture_provider(context).signal_for(context)
    invalid = {
        "created_at": envelope.source_bar_end - timedelta(microseconds=1),
        "expires_at": envelope.created_at,
        "source_bar_end": envelope.created_at + timedelta(microseconds=1),
    }
    with pytest.raises(SignalValidationError):
        _rehash(envelope, **{field: invalid[field]})


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "other"},
        {"provider": "ALPACA"},
        {"feed": "sip"},
        {"adjustment": "all"},
        {"symbol": "nvda"},
        {"timeframe": "5Min"},
    ],
)
def test_watermark_series_never_falls_back_to_another_data_contract(change):
    with pytest.raises(ReadinessValidationError):
        replace(_series(), **change)


@pytest.mark.parametrize(
    "flags", [(), ("complete", "complete"), ("z", "a"), ("../complete",), ["complete"]]
)
def test_quality_authority_requires_nonempty_exact_ordered_flags(flags):
    with pytest.raises(ReadinessValidationError):
        BarQualityPolicy("strict", flags)


@pytest.mark.parametrize(
    "change",
    [
        {"attempt_count": -1},
        {"attempt_count": True},
        {"attempt_count": 2**31},
        {"version": 0},
        {"version": 2**63},
        {"last_attempt_at": NOW},
        {"resolved_at": NOW},
        {"status": "resolved"},
        {"gap_id": "gap_" + "0" * 64},
    ],
)
def test_persisted_gap_corruption_never_becomes_repair_authority(sqlite_engine, calendar, change):
    repository = GapRepository(sqlite_engine, calendar=calendar)
    gap = repository.record(_detect(calendar, _series(), observed=(_interval(0), _interval(2)))[0])
    with pytest.raises(ReadinessIntegrityError):
        replace(gap, **change)
    assert repository.get(gap.gap_id) == gap
