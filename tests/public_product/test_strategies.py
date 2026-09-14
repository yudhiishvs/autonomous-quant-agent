"""Adversarial strategy contracts and deterministic current-observation outcomes."""

from datetime import UTC, datetime, timedelta
from decimal import localcontext

import pytest
from pydantic import ValidationError

from adaptive_trader.public_product.strategies import (
    MarketObservation,
    StrategyDefinition,
    evaluate,
)

NOW = datetime(2026, 9, 14, 15, 0, 10, tzinfo=UTC)


def definition(**changes):
    value = {
        "symbol": "SPY",
        "rule": {
            "kind": "moving_average_target",
            "fast_minutes": 2,
            "slow_minutes": 3,
            "target_shares": 2,
        },
        "order": {"kind": "market"},
    }
    value.update(changes)
    return StrategyDefinition.model_validate(value)


def observation(prices=("100", "101", "102"), **changes):
    value = {
        "symbol": "SPY",
        "feed": "iex",
        "observed_at": NOW,
        "regular_session_open": True,
        "asset_eligible": True,
        "bars": [
            {
                "started_at": NOW.replace(second=0) - timedelta(minutes=len(prices) - i),
                "close": price,
            }
            for i, price in enumerate(prices)
        ],
    }
    value.update(changes)
    return MarketObservation.model_validate(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"cadence_seconds": True},
        {"cadence_seconds": "60"},
        {"cadence_seconds": 59},
        {"cadence_seconds": 61},
        {"cadence_seconds": 86460},
        {"session": "extended"},
        {"asset_class": "crypto"},
        {"symbol": "BTC/USD"},
        {"symbol": "SPY\n"},
        {"symbol": "spy"},
        {"python": "import os"},
        {"rule": {"kind": "python", "code": "pass"}},
        {"rule": {"kind": "constant_target", "target_shares": -1}},
        {"rule": {"kind": "constant_target", "target_shares": 1.5}},
        {"rule": {"kind": "constant_target", "target_shares": True}},
        {"rule": {"kind": "constant_target", "target_shares": 100001}},
        {
            "rule": {
                "kind": "moving_average_target",
                "fast_minutes": 3,
                "slow_minutes": 3,
                "target_shares": 1,
            }
        },
        {
            "rule": {
                "kind": "moving_average_target",
                "fast_minutes": 2,
                "slow_minutes": 201,
                "target_shares": 1,
            }
        },
        {"order": {"kind": "market", "time_in_force": "gtc"}},
        {"order": {"kind": "limit", "offset_bps": -1}},
        {"order": {"kind": "limit", "offset_bps": 101}},
        {"order": {"kind": "market", "host": "https://example.com"}},
    ],
)
def test_closed_supported_contract(changes):
    with pytest.raises(ValidationError):
        definition(**changes)


def test_identity_is_semantic_complete_and_immutable():
    strategy = definition()
    assert (
        strategy.content_hash
        == StrategyDefinition.model_validate_json(strategy.model_dump_json()).content_hash
    )
    assert strategy.content_hash != definition(cadence_seconds=120).content_hash
    assert (
        strategy.content_hash != definition(order={"kind": "limit", "offset_bps": 0}).content_hash
    )
    assert strategy.content_hash != definition(symbol="AAPL").content_hash
    assert (
        strategy.content_hash
        != definition(rule={"kind": "constant_target", "target_shares": 2}).content_hash
    )
    with pytest.raises(ValidationError):
        strategy.rule.target_shares = 5


@pytest.mark.parametrize(
    "prices,expected,reason",
    [
        (("100", "101", "102"), 2, "trend_above"),
        (("102", "101", "100"), 0, "trend_not_above"),
        (("100", "100", "100"), 0, "trend_not_above"),
        (("100", "100.00000001", "100.00000002"), 2, "trend_above"),
    ],
)
def test_mean_comparison_uses_exact_prices(prices, expected, reason):
    with localcontext() as context:
        context.prec = 2
        result = evaluate(definition(), observation(prices), now=NOW)
    assert result.target_shares == expected
    assert result.reason == reason
    assert result.expires_at == NOW + timedelta(seconds=30)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"regular_session_open": False}, "market_closed"),
        ({"asset_eligible": False}, "asset_ineligible"),
        ({"observed_at": NOW + timedelta(seconds=1)}, "data_in_future"),
    ],
)
def test_rejected_context_does_not_propose_liquidation(changes, reason):
    result = evaluate(definition(), observation(**changes), now=NOW)
    assert result.target_shares is None
    assert result.reason == reason


def test_stale_and_insufficient_history_have_no_target():
    assert (
        evaluate(definition(), observation(), now=NOW + timedelta(seconds=31)).reason
        == "data_stale"
    )
    result = evaluate(definition(), observation(("100", "101")), now=NOW)
    assert result.reason == "insufficient_history"
    assert result.target_shares is None


def test_constant_and_zero_targets_still_require_fresh_eligible_data():
    strategy = definition(rule={"kind": "constant_target", "target_shares": 0})
    assert evaluate(strategy, observation(), now=NOW).target_shares == 0
    assert evaluate(strategy, observation(asset_eligible=False), now=NOW).target_shares is None


@pytest.mark.parametrize(
    "price", ["NaN", "Infinity", "-1", "0", "1000001", "0.000000001", 100.0, True]
)
def test_prices_reject_nonfinite_or_imprecise_input(price):
    with pytest.raises(ValidationError):
        observation(("100", "101", price))


def test_time_and_symbol_boundaries():
    with pytest.raises(ValueError, match="symbol"):
        evaluate(definition(), observation(symbol="AAPL"), now=NOW)
    with pytest.raises(ValueError, match="UTC"):
        evaluate(definition(), observation(), now=NOW.replace(tzinfo=None))
    bars = observation().model_dump()["bars"]
    with pytest.raises(ValidationError, match="consecutive"):
        observation(bars=(bars[0], bars[2]))
    with pytest.raises(ValidationError, match="consecutive"):
        observation(bars=(bars[0], bars[0], bars[1]))
    with pytest.raises(ValidationError, match="completed"):
        observation(bars=({"started_at": NOW.replace(second=0), "close": "100"},))


def test_provenance_changes_evidence_hash_and_expiry_is_bounded():
    first = evaluate(definition(), observation(), now=NOW)
    second = evaluate(definition(), observation(feed="sip"), now=NOW)
    assert first.observation_hash != second.observation_hash
    later = NOW.replace(second=55)
    result = evaluate(definition(), observation(observed_at=later), now=later)
    assert result.expires_at == NOW.replace(second=0) + timedelta(minutes=1)


def test_json_observations_normalize_utc_without_changing_evidence():
    original = observation()
    parsed = MarketObservation.model_validate_json(original.model_dump_json())
    assert evaluate(definition(), parsed, now=NOW) == evaluate(definition(), original, now=NOW)


def test_copied_models_are_revalidated_before_evaluation():
    strategy = definition()
    forged = strategy.model_copy(
        update={"rule": strategy.rule.model_copy(update={"target_shares": -1})}
    )
    with pytest.raises(ValidationError):
        evaluate(forged, observation(), now=NOW)


def test_validator_cli_bounds_and_redacts_input(tmp_path, monkeypatch, capsys):
    from adaptive_trader.public_product.__main__ import main

    path = tmp_path / "definition.json"
    path.write_text(definition().model_dump_json())
    monkeypatch.setattr("sys.argv", ["validate", str(path)])
    assert main() == 0
    assert definition().content_hash in capsys.readouterr().out
    for payload in ('{"unknown":"PRIVATE-INPUT-SENTINEL"}', "x" * 16385):
        path.write_text(payload)
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
        captured = capsys.readouterr()
        assert "PRIVATE-INPUT-SENTINEL" not in captured.err
        assert not captured.out
