"""Shared deterministic fixtures for the offline test suite."""

from __future__ import annotations

import socket
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from adaptive_trader.config import AppConfig, load_config
from adaptive_trader.data import MarketData, generate_synthetic_market_data
from adaptive_trader.persistence import Database


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test fail immediately if production code attempts a network call."""

    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("External network access is prohibited in the offline test suite")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(autouse=True)
def close_test_database_handles(monkeypatch: pytest.MonkeyPatch):
    """Close SQLite resources created by a test, including failure paths."""

    databases: list[Database] = []
    connections: list[sqlite3.Connection] = []
    original_database_init = Database.__init__
    original_connect = sqlite3.connect

    def tracked_database_init(self: Database, *args: Any, **kwargs: Any) -> None:
        original_database_init(self, *args, **kwargs)
        databases.append(self)

    def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(Database, "__init__", tracked_database_init)
    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    for database in reversed(databases):
        with suppress(Exception):
            database.close()
    for connection in reversed(connections):
        with suppress(Exception):
            connection.close()


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def default_config(project_root: Path) -> AppConfig:
    config = load_config(project_root / "configs" / "backtest.yaml")
    values = config.to_canonical_dict()
    for field_name in (
        "development_period_start",
        "development_period_end",
        "validation_period_start",
        "validation_period_end",
        "holdout_period_start",
    ):
        values["backtest"].pop(field_name, None)
    # Preserve the compact legacy test fixture's weekly schedule while the
    # retained public backtest configuration remains the daily canonical run.
    values["backtest"]["rebalance_frequency"] = "weekly"
    return AppConfig.from_dict(values)


@pytest.fixture(scope="session")
def synthetic_market_data(default_config: AppConfig) -> MarketData:
    return generate_synthetic_market_data(
        [*default_config.data.tickers, default_config.data.benchmark],
        start_date="2017-01-02",
        end_date="2023-12-29",
        seed=6174,
    )


@pytest.fixture(scope="session")
def fast_config(default_config: AppConfig) -> AppConfig:
    """Return a smaller-lookback configuration for event-driven integration tests."""

    values = default_config.to_dict()
    values["data"].update(
        {
            "tickers": ["SPY", "AAA", "BBB", "CCC"],
            "benchmark": "SPY",
            "start_date": "2018-01-01",
            "end_date": "2022-12-30",
        }
    )
    values["backtest"].update({"out_of_sample_start": "2020-01-01", "transaction_cost_bps": 5.0})
    values["momentum"].update({"lookback_days": 21, "volatility_lookback_days": 10, "top_n": 2})
    values["mean_reversion"].update(
        {
            "zscore_lookback_days": 10,
            "long_term_trend_days": 40,
            "volatility_lookback_days": 10,
            "top_n": 2,
        }
    )
    values["regime"].update(
        {
            "benchmark": "SPY",
            "fast_moving_average_days": 15,
            "slow_moving_average_days": 60,
            "volatility_lookback_days": 10,
            "volatility_threshold_lookback_days": 40,
        }
    )
    values["risk"].update(
        {
            "max_position_weight": 0.60,
            "target_annual_volatility": 0.50,
            "covariance_lookback_days": 20,
            "max_turnover_per_rebalance": 1.0,
            "drawdown_soft_limit": 0.80,
            "soft_limit_max_gross_exposure": 0.80,
            "drawdown_hard_limit": 0.90,
        }
    )
    values["project"]["output_directory"] = "outputs/test_run"
    return AppConfig.from_dict(values)


@pytest.fixture(scope="session")
def fast_market_data(fast_config: AppConfig) -> MarketData:
    return generate_synthetic_market_data(
        fast_config.data.tickers,
        start_date="2018-01-01",
        end_date="2022-12-30",
        seed=9981,
    )


@pytest.fixture(scope="session")
def fast_suite(fast_config: AppConfig, fast_market_data: MarketData):
    from adaptive_trader.backtest import run_backtest_suite

    return run_backtest_suite(fast_config, fast_market_data)
