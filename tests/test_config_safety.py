"""Strict configuration and paper-only safety invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from adaptive_trader.config import PAPER_ENABLEMENT_VALUE, AppConfig, load_config


def test_duplicate_tickers_are_rejected_case_insensitively() -> None:
    with pytest.raises(ValueError, match="duplicate tickers"):
        AppConfig.from_dict(
            {
                "universe": {
                    "tickers": ["SPY", "spy"],
                    "benchmark": "SPY",
                    "asset_class": "us_equity",
                    "require_fractionable": True,
                }
            }
        )


@pytest.mark.parametrize("value", [False, 0, "false", None])
def test_paper_only_cannot_be_disabled(value: object) -> None:
    with pytest.raises(ValueError, match="paper_only"):
        AppConfig.from_dict({"execution": {"paper_only": value}})


def test_live_switch_and_live_alpaca_endpoint_are_rejected() -> None:
    with pytest.raises(ValueError, match="Forbidden live-trading"):
        AppConfig.from_dict({"execution": {"live_trading_enabled": True}})
    with pytest.raises(ValueError, match="Live Alpaca trading endpoint"):
        AppConfig.from_dict(
            {"project": {"output_directory": "https://api.alpaca.markets/v2/orders"}}
        )


@pytest.mark.parametrize("feed", ["polygon", "OTC", "sip-fallback"])
def test_unsupported_feed_is_rejected(feed: str) -> None:
    with pytest.raises(ValueError, match="feed"):
        AppConfig.from_dict({"market_data": {"feed": feed}})


def test_configuration_hash_is_deterministic_and_sensitive_to_final_values() -> None:
    original = AppConfig.from_dict({})
    repeated = AppConfig.from_dict(original.to_dict())
    changed_values = original.to_dict()
    changed_values["project"]["run_name"] = "different-paper-run"
    changed = AppConfig.from_dict(changed_values)

    assert original.configuration_hash == repeated.configuration_hash
    assert original.configuration_hash != changed.configuration_hash
    assert len(original.configuration_hash) == 64


def test_canonical_configs_load_with_paper_safety_defaults(project_root: Path) -> None:
    for filename in ("backtest.yaml", "paper.yaml", "observer.yaml", "replay.yaml"):
        config = load_config(project_root / "configs" / filename)
        assert config.execution.paper_only is True
        assert config.execution.paper_order_submission_enabled is False
        assert config.execution.required_enablement_value == PAPER_ENABLEMENT_VALUE
        assert config.market_data.feed in {"IEX", "SIP"}
        assert config.market_data.minimum_completed_sessions >= 450
        assert config.universe.asset_class == "us_equity"


def test_backtest_config_freezes_ordered_research_windows(project_root: Path) -> None:
    config = load_config(project_root / "configs" / "backtest.yaml")

    assert config.backtest.development_period_start == "2016-01-01"
    assert config.backtest.development_period_end == "2022-12-31"
    assert config.backtest.validation_period_start == "2023-01-01"
    assert config.backtest.validation_period_end == "2024-12-31"
    assert config.backtest.holdout_period_start == "2025-01-01"
    assert config.backtest.out_of_sample_start == config.backtest.holdout_period_start
    assert config.backtest.comparison_period_start == "2020-01-01"
    assert config.to_canonical_dict()["backtest"]["out_of_sample_start"] == "2025-01-01"
    assert (
        AppConfig.from_dict(config.to_canonical_dict()).backtest.comparison_period_start
        == "2020-01-01"
    )


def test_partial_or_overlapping_research_windows_are_rejected() -> None:
    with pytest.raises(ValueError, match="configured together"):
        AppConfig.from_dict({"backtest": {"development_period_start": "2016-01-01"}})
    with pytest.raises(ValueError, match="must be ordered"):
        AppConfig.from_dict(
            {
                "backtest": {
                    "out_of_sample_start": "2025-01-01",
                    "development_period_start": "2016-01-01",
                    "development_period_end": "2023-12-31",
                    "validation_period_start": "2023-01-01",
                    "validation_period_end": "2024-12-31",
                    "holdout_period_start": "2025-01-01",
                }
            }
        )


def test_canonical_session_names_and_legacy_day_names_round_trip() -> None:
    canonical = AppConfig.from_dict(
        {
            "momentum": {"lookback_sessions": 42},
            "risk": {"covariance_lookback_sessions": 35},
        }
    )
    legacy_values = canonical.to_dict()
    legacy_values["momentum"]["lookback_days"] = 21
    legacy_values["risk"]["covariance_lookback_days"] = 20
    restored = AppConfig.from_dict(legacy_values)

    assert canonical.momentum.lookback_days == 42
    assert restored.momentum.lookback_days == 21
    assert restored.risk.covariance_lookback_days == 20


def test_cash_buffer_must_match_between_execution_and_risk() -> None:
    with pytest.raises(ValueError, match="required_cash_buffer"):
        AppConfig.from_dict(
            {
                "execution": {"required_cash_buffer": 0.02},
                "risk": {"required_cash_buffer": 0.03},
            }
        )


def test_schedule_times_require_zero_padded_24_hour_values() -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        AppConfig.from_dict({"schedule": {"evaluation_time_et": "9:30"}})
    with pytest.raises(ValueError, match="must precede"):
        AppConfig.from_dict(
            {
                "schedule": {
                    "evaluation_time_et": "14:31",
                    "catch_up_cutoff_et": "14:30",
                }
            }
        )


def test_invalid_regime_allocations_are_rejected() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        AppConfig.from_dict(
            {
                "regime": {
                    "allocations": {
                        "bull_low_vol": {
                            "momentum": 0.80,
                            "mean_reversion": 0.30,
                            "strategic_cash": 0.00,
                        }
                    }
                }
            }
        )


def test_hard_drawdown_limit_must_exceed_soft_limit() -> None:
    with pytest.raises(ValueError, match="below drawdown_hard_limit"):
        AppConfig.from_dict({"risk": {"drawdown_soft_limit": 0.15, "drawdown_hard_limit": 0.10}})


def test_unsupported_asset_class_is_rejected() -> None:
    with pytest.raises(ValueError, match="us_equity"):
        AppConfig.from_dict(
            {
                "universe": {
                    "tickers": ["BTCUSD"],
                    "benchmark": "BTCUSD",
                    "asset_class": "crypto",
                }
            }
        )
