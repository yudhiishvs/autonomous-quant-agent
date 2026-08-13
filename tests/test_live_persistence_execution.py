from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from adaptive_trader.broker import AlpacaPaperBroker, FakePaperBroker
from adaptive_trader.constants import UTC
from adaptive_trader.execution import OrderManager
from adaptive_trader.live_models import (
    LocalOrderState,
    OrderIntent,
    PaperCredentials,
    Side,
)
from adaptive_trader.persistence import AuditRepository, Database


def _intent(now: datetime, client_order_id: str = "apa-20260102-test-spy-b-00") -> OrderIntent:
    return OrderIntent(
        decision_id="decision-1",
        client_order_id=client_order_id,
        session_date=date(2026, 1, 2),
        symbol="SPY",
        side=Side.BUY,
        sequence=0,
        reference_price=Decimal("100"),
        created_at=now,
        notional=Decimal("1000"),
    )


def test_schema_and_rebalance_claim_are_durable_and_idempotent() -> None:
    repository = AuditRepository(Database(":memory:"))
    run_id = repository.start_run(mode="observe", configuration={"safe": True})
    first, claimed = repository.claim_rebalance(
        run_id=run_id,
        idempotency_key="primary:v1:2026-01-02",
        session_date=date(2026, 1, 2),
        strategy_version="v1",
        mode="observe",
    )
    second, claimed_again = repository.claim_rebalance(
        run_id=run_id,
        idempotency_key="primary:v1:2026-01-02",
        session_date=date(2026, 1, 2),
        strategy_version="v1",
        mode="observe",
    )

    assert (first, claimed) == (second, True)
    assert claimed_again is False
    required = {
        "application_runs",
        "configuration_snapshots",
        "strategy_versions",
        "market_bars",
        "market_data_gaps",
        "stream_events",
        "account_snapshots",
        "position_snapshots",
        "strategy_signals",
        "regime_states",
        "allocation_results",
        "risk_decisions",
        "risk_actions",
        "rebalance_decisions",
        "order_intents",
        "broker_orders",
        "order_events",
        "fill_events",
        "reconciliation_runs",
        "reconciliation_discrepancies",
        "halt_events",
        "system_incidents",
        "heartbeats",
        "daily_performance",
        "benchmark_performance",
        "generated_reports",
    }
    assert required <= repository.table_names()


def test_ambiguous_submission_is_not_retried() -> None:
    now = datetime(2026, 1, 2, 15, tzinfo=UTC)
    repository = AuditRepository(Database(":memory:"))
    run_id = repository.start_run(mode="paper_once", configuration={"safe": True})
    broker = FakePaperBroker(now=now)
    manager = OrderManager(repository=repository, broker=broker, run_id=run_id)
    intent = _intent(now)
    broker.timeout_after_accept.add(intent.client_order_id)

    assert manager.submit_intent(intent) is None
    assert broker.submit_calls == 1
    assert (
        repository.get_order(intent.client_order_id)["state"] == LocalOrderState.SUBMISSION_UNKNOWN
    )

    recovered = manager.submit_intent(intent)
    assert recovered is not None
    assert broker.submit_calls == 1


def test_alpaca_broker_constructor_hard_codes_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class DummyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(("client", kwargs))

    class DummyStream:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(("stream", kwargs))

    monkeypatch.setattr("adaptive_trader.broker.TradingClient", DummyClient)
    monkeypatch.setattr("adaptive_trader.broker.TradingStream", DummyStream)
    credentials = PaperCredentials("paper-key", "paper-secret")

    broker = AlpacaPaperBroker(credentials)

    assert broker.paper_only is True
    assert calls == [("client", {"paper": True}), ("stream", {"paper": True})]
    assert "paper-key" not in repr(credentials)
