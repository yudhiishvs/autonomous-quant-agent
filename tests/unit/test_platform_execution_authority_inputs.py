"""Malformed observation and hash-bound authority inputs cannot reach execution."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adaptive_trader.platform.execution import (
    AlpacaPaperBrokerAdapter,
    ExecutionValidationError,
    PaperClientOrder,
    Position,
)
from tests.unit.test_platform_execution_broker import (
    NOW,
    _persisted_service,
    _safety,
    _submission_authority,
)
from tests.unit.test_platform_execution_persistence import _reconciliation_request


@pytest.mark.parametrize(
    "change",
    [
        {"session_open": 1},
        {"data_complete": "true"},
        {"reconciliation_clean": None},
        {"account_observed_at": NOW + timedelta(microseconds=1)},
        {"active_symbols": ["AAA"]},
        {"active_symbols": ("BBB", "AAA")},
        {"shortable_symbols": ("BBB",)},
        {"evaluated_at": NOW.replace(tzinfo=None)},
    ],
)
def test_submission_safety_rejects_untyped_future_and_inconsistent_observations(change):
    with pytest.raises(ExecutionValidationError):
        replace(_safety(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"client_order_id": ""},
        {"execution_plan_id": ""},
        {"intent_hash": "invalid"},
        {"active_order_hashes": ("z" * 64,)},
        {"plan_fill_hashes": ["a" * 64]},
        {"plan_fill_hashes": ("b" * 64, "a" * 64)},
        {"active_latches": ("RECONCILIATION",)},
        {"order_hash": "f" * 64},
    ],
)
def test_ledger_authority_requires_canonical_evidence_and_matching_digest(change):
    plan, repo, _, _ = _persisted_service()
    ledger = repo.submission_ledger_snapshot(plan.intents[0].client_order_id)
    with pytest.raises(ExecutionValidationError):
        replace(ledger, **change)
    assert repo.order_events() == ()


@pytest.mark.parametrize(
    "change",
    [
        {"client_order_id": ""},
        {"account_id_hash": "invalid"},
        {"account_cash": float("inf")},
        {"account_equity": Decimal("NaN")},
        {"account_observed_at": NOW + timedelta(seconds=1)},
        {"positions": [Position("AAA", Decimal(1))]},
        {"positions": (Position("AAA", Decimal(1)), Position("AAA", Decimal(2)))},
        {"open_client_order_ids": ("z", "a")},
        {"account_cash": Decimal(999)},
    ],
)
def test_submission_authority_cannot_coerce_or_rebind_broker_evidence(change):
    plan, repo, broker, _ = _persisted_service()
    authority = _submission_authority(repo, broker, plan.intents[0].client_order_id)
    with pytest.raises(ExecutionValidationError):
        replace(authority, **change)
    assert repo.order_events() == ()


@pytest.mark.parametrize(
    "change",
    [
        {"active_symbols": ("BBB", "AAA")},
        {"short_eligible_symbols": ["AAA"]},
        {"short_eligible_symbols": ("BBB",)},
        {"broker_account": None},
        {"baseline_cash": True},
        {"mark_prices": [("AAA", Decimal(100))]},
        {"mark_prices": (("AAA",),)},
        {"mark_prices": (("AAA", Decimal(100)), ("AAA", Decimal(100)))},
        {"mark_prices": (("AAA", Decimal(0)),)},
        {"mark_prices": ()},
        {"expected_account_id_hash": "invalid"},
        {"started_at": NOW.replace(tzinfo=None)},
        {"completed_at": NOW - timedelta(minutes=1)},
        {"live_endpoint_detected": 1},
        {"require_flat": "true"},
        {"require_flat": True},
        {"fills": []},
    ],
)
def test_reconciliation_rejects_malformed_evidence_before_producing_a_receipt(change):
    plan, repo, _, _ = _persisted_service()
    request = _reconciliation_request(repo, plan)
    with pytest.raises(ExecutionValidationError):
        replace(request, **change)
    assert repo.reconciliations() == ()


def test_paper_protocol_adapter_preserves_execution_evidence_and_absent_lookup():
    plan, _, broker, _ = _persisted_service()
    intent = plan.intents[0]
    update = broker.submit(intent, submitted_at=NOW)
    result = PaperClientOrder(
        **{name: getattr(update, name) for name in PaperClientOrder.__dataclass_fields__}
    )
    account = broker.account(observed_at=NOW)
    client = SimpleNamespace(
        submit_market_order=lambda _: result,
        lookup_by_client_order_id=lambda key: None if key == "missing" else result,
        cancel_by_client_order_id=lambda _: result,
        account_state=lambda _: account,
        signed_positions=lambda: broker.positions(),
        open_client_order_ids=lambda: (),
        security_metadata=lambda symbols, **kwargs: (symbols, kwargs["observed_at"]),
    )
    adapter = AlpacaPaperBrokerAdapter(client)
    assert adapter.submit(intent, submitted_at=NOW) == update
    assert adapter.lookup(intent.client_order_id, observed_at=NOW) == update
    assert adapter.lookup("missing", observed_at=NOW) is None
    assert adapter.cancel(intent.client_order_id, canceled_at=NOW) == update
    assert adapter.account(observed_at=NOW) == account
    assert adapter.positions() == broker.positions()
    assert adapter.open_client_order_ids() == ()
    assert adapter.security_metadata(("AAA",), observed_at=NOW) == (("AAA",), NOW)


@pytest.mark.parametrize("method", ["submit", "lookup", "cancel"])
def test_paper_protocol_adapter_rejects_raw_unvalidated_order_objects(method):
    plan, _, _, _ = _persisted_service()
    client = SimpleNamespace(
        submit_market_order=lambda _: {},
        lookup_by_client_order_id=lambda _: {},
        cancel_by_client_order_id=lambda _: {},
    )
    adapter = AlpacaPaperBrokerAdapter(client)
    argument = plan.intents[0] if method == "submit" else plan.intents[0].client_order_id
    time_key = {"submit": "submitted_at", "lookup": "observed_at", "cancel": "canceled_at"}[method]
    with pytest.raises(ExecutionValidationError, match="invalid response"):
        getattr(adapter, method)(argument, **{time_key: NOW})
