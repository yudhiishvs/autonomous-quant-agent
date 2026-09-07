"""Rehashed proposals cannot acquire authority for a different decision context."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from adaptive_trader.platform.signals import SignalValidationError, verify_paper_authorization
from tests.unit.test_platform_signals import (
    _context,
    _fixture_provider,
    _rehash,
    experiment,
)

__all__ = ["experiment"]


@pytest.fixture
def proposal(experiment):
    context = _context(experiment)
    return context, _fixture_provider(context).signal_for(context)


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": 999},
        {"signal_id": "foreign"},
        {"slot_id": "foreign"},
        {"correlation_id": "foreign"},
        {"provider_id": "../provider"},
        {"provider_version": ""},
        {"provider_source_mode": "builtin"},
        {"experiment_version": True},
        {"experiment_version": 0},
        {"experiment_hash": "A" * 64},
        {"active_symbols": []},
        {"active_symbols": ()},
        {"active_symbols": ("AMD", "AMD")},
        {"active_symbols": ("NVDA", "AMD")},
        {"active_symbols": (1,)},
        {"created_at": datetime(2026, 7, 6)},
        {"availability_mask": (1,) * 8},
        {"actions": ("FLAT",) * 8},
        {"artifact_id": "artifact", "artifact_hash": "a" * 64},
        {"artifact_id": None, "artifact_hash": "a" * 64},
        {"promotable": 1},
        {"paper_submission_eligible": 1},
        {"promotable": True},
        {"paper_submission_eligible": True},
        {"content_hash": "0" * 64},
        {"signal_id": "signal_" + "0" * 64},
    ],
)
def test_malformed_or_promoted_fixture_receipt_never_reaches_authorization(proposal, change):
    _, envelope = proposal
    with pytest.raises(SignalValidationError):
        replace(envelope, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"slot_id": "slot_" + "0" * 64},
        {"correlation_id": "correlation_" + "0" * 64},
        {"provider_id": "always_flat"},
        {"provider_version": "2"},
        {"experiment_id": "other_experiment"},
        {"experiment_version": 2},
        {"experiment_hash": "b" * 64},
        {"data_contract_hash": "b" * 64},
        {"policy_hash": "b" * 64},
    ],
)
def test_rehashing_foreign_identity_does_not_make_it_valid_for_slot(proposal, change):
    context, envelope = proposal
    changed = _rehash(envelope, **change)
    assert changed.content_hash != envelope.content_hash
    with pytest.raises(SignalValidationError):
        verify_paper_authorization(changed, context=context)


@pytest.mark.parametrize(
    "attack", ["source", "before_ready", "past_deadline", "unavailable", "nondecimal"]
)
def test_proposal_causality_and_availability_cannot_be_overridden_by_rehash(proposal, attack):
    context, envelope = proposal
    changes = {}
    if attack == "source":
        changes["source_bar_end"] = envelope.source_bar_end - timedelta(minutes=15)
    elif attack == "before_ready":
        changes["created_at"] = context.slot.ready_at - timedelta(microseconds=1)
    elif attack == "past_deadline":
        changes["expires_at"] = context.slot.deadline_at + timedelta(microseconds=1)
    elif attack == "unavailable":
        changes["availability_mask"] = (False,) * len(envelope.active_symbols)
    else:
        changes["expected_edge_bps"] = (1.0,) * len(envelope.active_symbols)
    with pytest.raises(SignalValidationError):
        _rehash(envelope, **changes).validate_for(context)


def test_symbol_accessors_never_fall_back_to_another_active_symbol(proposal):
    context, envelope = proposal
    for method in (
        envelope.action_for,
        envelope.edge_for,
        envelope.target_input_for,
        envelope.is_available,
    ):
        with pytest.raises(KeyError):
            method("QQQ")
        with pytest.raises(SignalValidationError):
            method("amd")
    with pytest.raises(SignalValidationError):
        envelope.validate_for(None)
    with pytest.raises(SignalValidationError):
        verify_paper_authorization(None, context=context)
    assert not verify_paper_authorization(envelope, context=context).approved
