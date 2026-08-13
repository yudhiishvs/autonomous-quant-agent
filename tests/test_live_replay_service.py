from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite

import pytest

from adaptive_trader.broker import AlpacaPaperBroker, FakePaperBroker
from adaptive_trader.config import AppConfig, DataConfig, MarketDataConfig, ProjectConfig
from adaptive_trader.constants import UTC
from adaptive_trader.exceptions import SafetyViolation
from adaptive_trader.live_models import LocalOrderState, ReplayEvent, ReplayEventType
from adaptive_trader.replay import generate_synthetic_replay_events, run_replay


def _config() -> AppConfig:
    return AppConfig(
        project=ProjectConfig(run_name="replay-test"),
        data=DataConfig(tickers=["SPY", "QQQ", "IWM"], benchmark="SPY"),
        market_data=MarketDataConfig(provider="replay", stale_after_seconds=180),
    )


def _explicit_target() -> dict[str, float]:
    return {"SPY": 0.20, "QQQ": 0.20, "IWM": 0.20}


def test_replay_rejects_real_broker_before_any_broker_method_call() -> None:
    broker = object.__new__(AlpacaPaperBroker)
    calls: list[str] = []

    def forbid(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("broker_method")
        raise AssertionError("replay touched a real broker")

    for name in (
        "get_account",
        "get_clock",
        "get_calendar",
        "get_asset",
        "get_positions",
        "get_orders",
        "submit_order",
        "cancel_all_orders",
        "cancel_order",
        "start_trade_updates",
        "stop_trade_updates",
    ):
        setattr(broker, name, forbid)

    with pytest.raises(
        SafetyViolation,
        match="Simulated replay orders require replay mode and FakePaperBroker",
    ):
        run_replay(_config(), (), broker=broker)  # type: ignore[arg-type]

    assert calls == []


def test_default_replay_is_deterministic_and_uses_forward_decision_engine() -> None:
    config = _config()
    first = generate_synthetic_replay_events(config)
    second = generate_synthetic_replay_events(config)

    assert first == second
    assert len(first) == len(config.data.tickers) + 1
    assert first[-1].event_type is ReplayEventType.EVALUATE
    assert first[-1].payload is None

    result = run_replay(config, first)
    cycle = result.cycles[0]
    metadata = cycle.decision_metadata

    assert cycle.status == "submitted"
    assert metadata is not None
    assert metadata["provider_feed"] == "REPLAY"
    assert metadata["cutoff"] < cycle.session_date
    assert metadata["history_observations"] >= config.market_data.minimum_completed_sessions
    assert set(metadata["strategy_outputs"]) == {"momentum", "mean_reversion"}
    assert metadata["regime"] is not None
    assert metadata["allocation"] is not None
    assert metadata["risk_decision"] is not None
    assert metadata["final_target"]
    assert all(isfinite(weight) for weight in metadata["final_target"].values())
    assert result.repository.count("strategy_signals") == 1
    assert result.repository.count("regime_states") == 1
    assert result.repository.count("allocation_results") == 1
    assert result.repository.count("risk_decisions") == 1


def test_nonterminal_restart_does_not_duplicate_submissions() -> None:
    config = _config()
    generated = list(generate_synthetic_replay_events(config))
    evaluation = generated[-1]
    generated.extend(
        [
            ReplayEvent(
                sequence=100,
                event_type=ReplayEventType.RESTART,
                timestamp=evaluation.timestamp + timedelta(seconds=1),
            ),
            ReplayEvent(
                sequence=101,
                event_type=ReplayEventType.EVALUATE,
                timestamp=evaluation.timestamp + timedelta(seconds=2),
                payload={"target_weights": _explicit_target()},
            ),
        ]
    )

    result = run_replay(
        config,
        generated,
        target_weights=_explicit_target(),
        auto_fill=False,
        use_decision_engine=False,
    )

    assert result.event_count == len(generated)
    assert result.restart_count == 1
    assert result.broker_submit_calls == 3
    assert result.cycles[0].status == "submitted"
    assert result.cycles[1].status == "duplicate"
    assert result.repository.count("decision_receipts") == 1
    assert {row["state"] for row in result.repository.list_orders(open_only=True)} == {
        LocalOrderState.ACCEPTED.value
    }


def test_disconnect_then_reconnect_recovers_without_claiming_while_stale() -> None:
    config = _config()
    now = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)
    generated = list(generate_synthetic_replay_events(config, start=now))
    events = [
        *generated[:-1],
        ReplayEvent(
            sequence=50,
            event_type=ReplayEventType.DISCONNECT,
            timestamp=now - timedelta(seconds=1),
        ),
        ReplayEvent(
            sequence=51,
            event_type=ReplayEventType.EVALUATE,
            timestamp=now,
        ),
        ReplayEvent(
            sequence=52,
            event_type=ReplayEventType.RECONNECT,
            timestamp=now + timedelta(seconds=1),
        ),
        ReplayEvent(
            sequence=53,
            event_type=ReplayEventType.EVALUATE,
            timestamp=now + timedelta(seconds=2),
        ),
    ]

    result = run_replay(config, events)

    assert result.broker_submit_calls == 3
    assert result.cycles[0].status == "waiting_for_fresh_data"
    assert result.cycles[0].claimed is False
    assert any("not fresh" in reason for reason in result.cycles[0].gate.reasons)
    assert result.cycles[1].status == "submitted"
    assert result.cycles[1].claimed is True
    assert result.repository.count("decision_receipts") == 1


def test_missing_target_is_never_interpreted_as_liquidate() -> None:
    config = _config()
    events = list(generate_synthetic_replay_events(config))
    events[-1] = ReplayEvent(
        sequence=events[-1].sequence,
        event_type=ReplayEventType.EVALUATE,
        timestamp=events[-1].timestamp,
        payload=None,
    )

    result = run_replay(
        config,
        events,
        target_weights=None,
        use_decision_engine=False,
    )

    assert result.broker_submit_calls == 0
    assert "strategy target is unavailable" in result.cycles[0].gate.reasons


def test_repeated_top_level_replay_invocations_are_independent(tmp_path) -> None:
    config = _config()
    database = tmp_path / "repeated-replay.db"
    events = generate_synthetic_replay_events(config)

    first = run_replay(config, events, database=database)
    first.database.close()
    second = run_replay(config, events, database=database)

    try:
        assert first.broker_submit_calls == second.broker_submit_calls == 3
        assert second.cycles[0].status == "submitted"
        assert second.repository.count("decision_receipts") == 2
        assert second.repository.count("strategy_signals") == 2
        assert second.repository.count("regime_states") == 2
        assert second.repository.count("allocation_results") == 2
        assert second.repository.count("risk_decisions") == 2
    finally:
        second.database.close()


def test_replay_populates_complete_execution_audit_chain() -> None:
    config = _config()
    events = generate_synthetic_replay_events(config)
    broker = FakePaperBroker(
        now=events[0].timestamp,
        initial_cash="80000",
        auto_fill=True,
    )
    broker.set_position("SPY", "200", "100")

    result = run_replay(
        config,
        events,
        broker=broker,
        target_weights={"SPY": 0.2, "QQQ": 0.2, "IWM": 0.2},
        auto_fill=True,
        use_decision_engine=False,
    )

    assert result.cycles[0].status == "submitted"
    assert result.repository.count("application_runs") == 1
    assert result.repository.count("account_snapshots") >= 1
    assert result.repository.count("position_snapshots") >= 1
    assert result.repository.count("rebalance_decisions") == 1
    assert result.repository.count("decision_receipts") == 1
    assert result.repository.count("order_intents") == 2
    assert result.repository.count("broker_orders") == 2
    assert result.repository.count("order_events") >= 2
    assert result.repository.count("fill_events") == 2
    assert result.repository.count("reconciliation_runs") >= 2
    assert result.repository.count("daily_performance") == 1
    assert result.repository.count("benchmark_performance") == 1
    assert result.repository.count("stream_events") > 0
    assert broker.get_account().equity == Decimal("100000")
