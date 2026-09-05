"""Durable submission, fake broker, ambiguity, idempotency, and restart tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from adaptive_trader.platform.config import BrokerAdapter, ExecutionMode, load_experiment
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.execution import (
    PAPER_ACKNOWLEDGEMENT,
    AlpacaPaperBrokerAdapter,
    DeterministicFakePaperBroker,
    ExecutionService,
    ExecutionValidationError,
    FakeBrokerScenario,
    MemoryExecutionRepository,
    OrderState,
    PaperClientOrder,
    PaperGateContext,
    Position,
    SubmissionSafetySnapshot,
    authorize_paper_intent,
    transition_is_permitted,
)
from adaptive_trader.platform.execution.planner import plan_signed_orders
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.scheduling import build_session_schedule
from adaptive_trader.platform.signals import (
    AlwaysFlatSignalProvider,
    DecisionContext,
    verify_paper_authorization,
)
from tests.unit.test_platform_execution_planner import planning_request

NOW = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)


def _safety(
    *,
    now: datetime = NOW,
    account_age: int = 0,
    ambiguous: bool = False,
    entry_disabled: bool = False,
) -> SubmissionSafetySnapshot:
    return SubmissionSafetySnapshot(
        evaluated_at=now,
        session_open=True,
        data_complete=True,
        account_observed_at=now - timedelta(seconds=account_age),
        security_observed_at=now,
        reconciliation_observed_at=now,
        price_observed_at=now,
        reconciliation_clean=True,
        ambiguous_order_exists=ambiguous,
        blocking_latch_exists=False,
        entry_disabled=entry_disabled,
        active_symbols=("AAA",),
        shortable_symbols=("AAA",),
    )


def _long_plan():
    return plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal("0.10")),),
        )
    )


def _persisted_service(
    *,
    scenario: FakeBrokerScenario = FakeBrokerScenario.FULL_FILL,
):
    result = _long_plan()
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW, default_scenario=scenario)
    broker.set_mark_prices((("AAA", Decimal("100")),))
    service = ExecutionService(repository=repository, broker=broker)
    service.persist(result)
    return result, repository, broker, service


def test_intent_and_order_state_exist_before_broker_submission() -> None:
    result, repository, _broker, service = _persisted_service()
    intent = result.intents[0]

    assert repository.get_intent(intent.client_order_id) == intent
    assert repository.get_order(intent.client_order_id).state is OrderState.INTENT_COMMITTED
    outcome = service.submit_one(intent.client_order_id, safety=_safety())

    assert outcome.submitted
    assert outcome.state is OrderState.FILLED
    assert tuple(event.to_state for event in repository.order_events()) == (
        OrderState.SUBMISSION_STARTED,
        OrderState.FILLED,
    )
    assert len(repository.fills()) == 1


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (FakeBrokerScenario.FULL_FILL, OrderState.FILLED),
        (FakeBrokerScenario.PARTIAL_FILL, OrderState.PARTIALLY_FILLED),
        (FakeBrokerScenario.REJECTION, OrderState.REJECTED),
        (FakeBrokerScenario.CANCELLATION, OrderState.CANCELED),
        (FakeBrokerScenario.EXPIRATION, OrderState.EXPIRED),
        (FakeBrokerScenario.DELAYED_UPDATE, OrderState.PENDING),
    ],
)
def test_fake_broker_failure_scenarios_are_deterministic(
    scenario: FakeBrokerScenario,
    expected: OrderState,
) -> None:
    result, repository, broker, service = _persisted_service(scenario=scenario)
    intent = result.intents[0]

    outcome = service.submit_one(intent.client_order_id, safety=_safety())

    assert outcome.state is expected
    assert (
        DeterministicFakePaperBroker.from_snapshot(broker.snapshot()).snapshot()
        == broker.snapshot()
    )
    assert MemoryExecutionRepository.from_state(repository.export_state()).export_state() == (
        repository.export_state()
    )


def test_delayed_update_becomes_a_deterministic_fill_on_refresh() -> None:
    result, repository, _broker, service = _persisted_service(
        scenario=FakeBrokerScenario.DELAYED_UPDATE
    )
    client_id = result.intents[0].client_order_id

    first = service.submit_one(client_id, safety=_safety())
    refreshed = service.refresh_nonterminal(
        client_id,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert first.state is OrderState.PENDING
    assert refreshed.state is OrderState.FILLED
    assert len(repository.fills()) == 1


def test_duplicate_execution_update_is_idempotent() -> None:
    result, repository, _broker, service = _persisted_service(
        scenario=FakeBrokerScenario.DUPLICATE_EXECUTION_UPDATE
    )

    outcome = service.submit_one(result.intents[0].client_order_id, safety=_safety())

    assert outcome.state is OrderState.FILLED
    assert len(repository.fills()) == 1
    retry = service.submit_one(result.intents[0].client_order_id, safety=_safety())
    assert not retry.submitted
    assert len(repository.fills()) == 1


def test_timeout_after_acceptance_recovers_by_lookup_without_resubmission() -> None:
    result, repository, broker, service = _persisted_service(
        scenario=FakeBrokerScenario.TIMEOUT_AFTER_ACCEPTANCE
    )
    client_id = result.intents[0].client_order_id

    first = service.submit_one(client_id, safety=_safety())
    assert first.state is OrderState.SUBMISSION_UNKNOWN
    assert repository.has_ambiguous_order()

    restarted_repository = MemoryExecutionRepository.from_state(repository.export_state())
    restarted_broker = DeterministicFakePaperBroker.from_snapshot(broker.snapshot())
    restarted = ExecutionService(repository=restarted_repository, broker=restarted_broker)
    resolved = restarted.resolve_ambiguous(client_id, observed_at=NOW + timedelta(seconds=1))

    assert resolved.state is OrderState.ACCEPTED
    assert not restarted_repository.has_ambiguous_order()
    assert restarted_broker.positions() == ()


def test_timeout_before_acceptance_remains_reconciliation_required() -> None:
    result, repository, broker, service = _persisted_service(
        scenario=FakeBrokerScenario.TIMEOUT_BEFORE_ACCEPTANCE
    )
    client_id = result.intents[0].client_order_id

    assert service.submit_one(client_id, safety=_safety()).state is OrderState.SUBMISSION_UNKNOWN
    assert broker.lookup(client_id, observed_at=NOW) is None
    resolved = service.resolve_ambiguous(client_id, observed_at=NOW + timedelta(seconds=1))

    assert resolved.state is OrderState.RECONCILIATION_REQUIRED
    assert repository.has_ambiguous_order()


def test_restart_after_submission_marker_never_resubmits_unknown_intent() -> None:
    result, repository, broker, _service = _persisted_service()
    client_id = result.intents[0].client_order_id
    repository.record_submission_started(client_id, started_at=NOW)
    assert repository.has_ambiguous_order()

    restarted_repository = MemoryExecutionRepository.from_state(repository.export_state())
    restarted_broker = DeterministicFakePaperBroker.from_snapshot(broker.snapshot())
    restarted = ExecutionService(repository=restarted_repository, broker=restarted_broker)

    refused = restarted.submit_one(client_id, safety=_safety())
    resolved = restarted.resolve_ambiguous(
        client_id,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert not refused.submitted
    assert refused.reason_codes == ("reconciliation_required",)
    assert resolved.state is OrderState.RECONCILIATION_REQUIRED
    assert restarted_broker.positions() == ()


def test_restart_after_broker_side_effect_recovers_fills_by_client_id() -> None:
    result, repository, broker, _service = _persisted_service()
    intent = result.intents[0]
    repository.record_submission_started(intent.client_order_id, started_at=NOW)
    broker.submit(intent, submitted_at=NOW)

    restarted_repository = MemoryExecutionRepository.from_state(repository.export_state())
    restarted_broker = DeterministicFakePaperBroker.from_snapshot(broker.snapshot())
    restarted = ExecutionService(repository=restarted_repository, broker=restarted_broker)

    resolved = restarted.resolve_ambiguous(
        intent.client_order_id,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert resolved.state is OrderState.FILLED
    assert len(restarted_repository.fills()) == 1
    assert restarted_broker.positions() == (Position("AAA", Decimal(1)),)


def test_stale_intent_inputs_block_before_broker_side_effect() -> None:
    result, repository, broker, service = _persisted_service()
    client_id = result.intents[0].client_order_id

    outcome = service.submit_one(client_id, safety=_safety(account_age=31))

    assert not outcome.submitted
    assert outcome.reason_codes == ("account_stale",)
    assert repository.get_order(client_id).state is OrderState.INTENT_COMMITTED
    assert broker.positions() == ()


def test_entry_disabled_blocks_opening_but_not_forced_reduction() -> None:
    result, repository, broker, service = _persisted_service()
    outcome = service.submit_one(
        result.intents[0].client_order_id,
        safety=_safety(entry_disabled=True),
    )
    assert not outcome.submitted
    assert outcome.reason_codes == ("entry_disabled",)
    assert broker.positions() == ()
    assert repository.fills() == ()


def test_short_proceeds_are_restricted_and_cover_releases_them() -> None:
    open_result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(0)),),
            target_weights=(("AAA", Decimal("-0.10")),),
        )
    )
    repository = MemoryExecutionRepository()
    broker = DeterministicFakePaperBroker(initial_time=NOW)
    broker.set_mark_prices((("AAA", Decimal("100")),))
    service = ExecutionService(repository=repository, broker=broker)
    service.persist(open_result)
    service.submit_one(open_result.intents[0].client_order_id, safety=_safety())

    short_account = broker.account(observed_at=NOW)
    assert broker.positions() == (Position("AAA", Decimal(-1)),)
    assert short_account.cash == Decimal("100100.00")
    assert short_account.restricted_short_proceeds == Decimal("100")
    assert short_account.buying_power == Decimal("100000.00")
    assert short_account.equity == Decimal("100000.00")

    close_result = plan_signed_orders(
        planning_request(
            current=(("AAA", Decimal(-1)),),
            target_weights=(("AAA", Decimal(0)),),
        )
    )
    service.persist(close_result)
    service.submit_one(close_result.intents[0].client_order_id, safety=_safety())
    flat_account = broker.account(observed_at=NOW)
    assert broker.positions() == ()
    assert flat_account.cash == Decimal("100000.00")
    assert flat_account.restricted_short_proceeds == 0


def test_repository_rejects_tampered_restart_projection() -> None:
    result, repository, _broker, service = _persisted_service()
    service.submit_one(result.intents[0].client_order_id, safety=_safety())
    state = repository.export_state()
    tampered = replace(state, order_events=state.order_events[:-1])

    with pytest.raises(ExecutionValidationError):
        MemoryExecutionRepository.from_state(tampered)


def test_state_machine_rejects_post_terminal_and_backward_transitions() -> None:
    assert transition_is_permitted(OrderState.SUBMISSION_STARTED, OrderState.FILLED)
    assert not transition_is_permitted(OrderState.FILLED, OrderState.PENDING)
    assert not transition_is_permitted(OrderState.ACCEPTED, OrderState.SUBMISSION_STARTED)


class _PaperClientProbe:
    def __init__(self) -> None:
        self.calls = 0

    def submit_market_order(self, intent) -> PaperClientOrder:
        del intent
        self.calls += 1
        raise AssertionError("paper client must remain unreachable by default")

    def lookup_by_client_order_id(self, client_order_id: str) -> PaperClientOrder | None:
        del client_order_id
        self.calls += 1
        return None

    def cancel_by_client_order_id(self, client_order_id: str) -> PaperClientOrder:
        del client_order_id
        self.calls += 1
        raise AssertionError("paper client must remain unreachable by default")

    def account_state(self, observed_at: datetime):
        del observed_at
        self.calls += 1
        raise AssertionError("paper client must remain unreachable by default")

    def signed_positions(self):
        self.calls += 1
        return ()

    def open_client_order_ids(self):
        self.calls += 1
        return ()


def test_paper_adapter_is_inert_without_independent_gate_context() -> None:
    result = _long_plan()
    repository = MemoryExecutionRepository()
    probe = _PaperClientProbe()
    service = ExecutionService(
        repository=repository,
        broker=AlpacaPaperBrokerAdapter(probe),
    )
    service.persist(result)

    outcome = service.submit_one(result.intents[0].client_order_id, safety=_safety())

    assert not outcome.submitted
    assert outcome.reason_codes == ("paper_gate_context_missing",)
    assert probe.calls == 0


def _paper_signal():
    config_root = Path(__file__).resolve().parents[2] / "configs"
    experiment = load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=config_root,
    )
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="always_flat",
        signal_provider_version="1",
        session_date=date(2026, 7, 6),
        calendar=XnasExchangeCalendar(),
    )
    slot = schedule.strategy_slots[0]
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=sha256_hex(("data-contract", 1)),
        policy_hash=sha256_hex(("policy", 1)),
        execution_mode=ExecutionMode.PAPER,
        broker_adapter=BrokerAdapter.ALPACA_PAPER,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )
    signal = AlwaysFlatSignalProvider(clock=lambda: slot.ready_at).signal_for(context)
    return signal, verify_paper_authorization(signal, context=context)


def test_paper_gates_report_every_independent_default_denial() -> None:
    result = _long_plan()
    signal, model_authorization = _paper_signal()
    stale = replace(
        _safety(entry_disabled=True),
        session_open=False,
        data_complete=False,
        account_observed_at=NOW - timedelta(seconds=31),
        security_observed_at=NOW - timedelta(seconds=301),
        reconciliation_observed_at=NOW - timedelta(seconds=61),
        price_observed_at=NOW - timedelta(seconds=121),
        reconciliation_clean=False,
        ambiguous_order_exists=True,
        blocking_latch_exists=True,
    )
    context = PaperGateContext(
        runtime_mode=ExecutionMode.OFFLINE,
        submission_enabled=False,
        acknowledgement=None,
        adapter_paper_only=False,
        secret_files_valid=False,
        configured_account_id_hash="1" * 64,
        observed_account_id_hash="2" * 64,
        signal=signal,
        model_authorization=model_authorization,
        safety=stale,
    )

    decision = authorize_paper_intent(result.intents[0], context=context)

    assert not decision.approved
    assert set(decision.reasons) >= {
        "account_id_mismatch",
        "account_stale",
        "adapter_not_paper_only",
        "ambiguous_order_exists",
        "blocking_latch_active",
        "entry_disabled",
        "market_data_incomplete",
        "model_approval_not_implemented",
        "paper_acknowledgement_missing",
        "paper_secret_files_invalid",
        "planning_price_stale",
        "reconciliation_blocking",
        "reconciliation_stale",
        "runtime_not_paper",
        "security_metadata_stale",
        "session_closed",
        "signal_not_paper_eligible",
        "submission_disabled",
    }


def test_model_authorization_keeps_best_case_paper_context_denied() -> None:
    result = _long_plan()
    signal, model_authorization = _paper_signal()
    context = PaperGateContext(
        runtime_mode=ExecutionMode.PAPER,
        submission_enabled=True,
        acknowledgement=PAPER_ACKNOWLEDGEMENT,
        adapter_paper_only=True,
        secret_files_valid=True,
        configured_account_id_hash="1" * 64,
        observed_account_id_hash="1" * 64,
        signal=signal,
        model_authorization=model_authorization,
        safety=_safety(),
    )

    decision = authorize_paper_intent(result.intents[0], context=context)

    assert not decision.approved
    assert "model_approval_not_implemented" in decision.reasons
    assert "signal_not_paper_eligible" in decision.reasons


def test_fake_snapshot_rejects_noncanonical_or_malformed_input() -> None:
    with pytest.raises(ExecutionValidationError):
        DeterministicFakePaperBroker.from_snapshot(b"{}")
    with pytest.raises(ExecutionValidationError):
        DeterministicFakePaperBroker.from_snapshot(b"not-json")
