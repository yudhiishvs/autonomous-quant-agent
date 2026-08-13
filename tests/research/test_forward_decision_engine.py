"""Causal integration tests for the broker-independent forward engine."""

from __future__ import annotations

from copy import copy
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.config import (
    AppConfig,
    DataConfig,
    MarketDataConfig,
    MeanReversionConfig,
    MomentumConfig,
    RegimeConfig,
    RiskConfig,
)
from adaptive_trader.decision_engine import ForwardDecisionEngine, ForwardDecisionError
from adaptive_trader.live_models import AccountState, MarketBar, PositionState

NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2025, 7, 15, 14, 5, tzinfo=UTC)


class FakeDailyProvider:
    """Provider that intentionally ignores request bounds to test engine filtering."""

    feed = "IEX"
    connected = True

    def __init__(self, bars: list[MarketBar]) -> None:
        self.bars = tuple(bars)
        self.requests: list[dict[str, Any]] = []

    def get_bars(
        self,
        symbols: tuple[str, ...],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "minute",
    ) -> tuple[MarketBar, ...]:
        self.requests.append(
            {"symbols": tuple(symbols), "start": start, "end": end, "timeframe": timeframe}
        )
        return self.bars


def _config() -> AppConfig:
    return AppConfig(
        data=DataConfig(tickers=["SPY", "QQQ"], benchmark="SPY"),
        market_data=MarketDataConfig(
            historical_calendar_days=600,
            minimum_completed_sessions=80,
        ),
        momentum=MomentumConfig(
            lookback_days=20,
            volatility_lookback_days=10,
            top_n=1,
        ),
        mean_reversion=MeanReversionConfig(
            zscore_lookback_days=10,
            long_term_trend_days=30,
            volatility_lookback_days=10,
            top_n=1,
        ),
        regime=RegimeConfig(
            benchmark="SPY",
            fast_moving_average_days=10,
            slow_moving_average_days=30,
            volatility_lookback_days=10,
            volatility_threshold_lookback_days=40,
        ),
        risk=RiskConfig(covariance_lookback_days=20),
    )


def _bars(*, current_multiplier: float = 1.0) -> list[MarketBar]:
    sessions = pd.bdate_range(end="2025-07-16", periods=130)
    result: list[MarketBar] = []
    for index, session in enumerate(sessions):
        for symbol, phase in (("SPY", 0.0), ("QQQ", 0.7)):
            trend = 100.0 + index * (0.11 if symbol == "SPY" else 0.14)
            close = trend * (1.0 + 0.012 * np.sin(index / 4.0 + phase))
            if session.date() >= NOW.astimezone(NEW_YORK).date():
                close *= current_multiplier if symbol == "SPY" else 1.0 / current_multiplier
            opened = close * 0.998
            started = datetime.combine(session.date(), time(16), tzinfo=NEW_YORK).astimezone(UTC)
            result.append(
                MarketBar(
                    symbol=symbol,
                    start=started,
                    end=started + timedelta(minutes=1),
                    open=Decimal(str(opened)),
                    high=Decimal(str(max(opened, close) * 1.002)),
                    low=Decimal(str(min(opened, close) * 0.998)),
                    close=Decimal(str(close)),
                    volume=1_000_000 + index,
                    feed="IEX",
                    received_at=NOW,
                    source="deterministic_fake",
                )
            )
    return result


def _account_and_positions() -> tuple[AccountState, tuple[PositionState, ...]]:
    account = AccountState(
        timestamp=NOW,
        account_id="paper-test",
        status="ACTIVE",
        equity=Decimal("100000"),
        cash=Decimal("70000"),
        buying_power=Decimal("70000"),
        last_equity=Decimal("101000"),
    )
    positions = (
        PositionState(
            timestamp=NOW,
            symbol="SPY",
            quantity=Decimal("100"),
            market_value=Decimal("20000"),
        ),
        PositionState(
            timestamp=NOW,
            symbol="QQQ",
            quantity=Decimal("25"),
            market_value=Decimal("10000"),
        ),
    )
    return account, positions


def test_current_and_future_daily_bars_cannot_change_forward_target() -> None:
    account, positions = _account_and_positions()
    ordinary_provider = FakeDailyProvider(_bars(current_multiplier=1.0))
    shocked_provider = FakeDailyProvider(_bars(current_multiplier=100.0))

    ordinary = ForwardDecisionEngine(_config(), ordinary_provider)
    shocked = ForwardDecisionEngine(_config(), shocked_provider)
    ordinary_target = ordinary(NOW, account, positions)
    shocked_target = shocked(NOW, account, positions)

    assert ordinary_target == shocked_target
    assert ordinary.last_metadata is not None
    assert shocked.last_metadata is not None
    assert ordinary.last_metadata.cutoff == date(2025, 7, 14)
    assert shocked.last_metadata.cutoff == date(2025, 7, 14)
    assert ordinary.last_metadata.bars_excluded_as_incomplete == 4
    assert ordinary_provider.requests[0]["timeframe"] == "day"
    assert ordinary_provider.requests[0]["end"].astimezone(NEW_YORK).date() == date(2025, 7, 15)
    for output in ordinary.last_metadata.strategy_outputs.values():
        assert pd.Timestamp(output["as_of_date"]).date() == date(2025, 7, 14)


def test_target_and_structured_metadata_are_finite_and_use_actual_position_values() -> None:
    account, positions = _account_and_positions()
    engine = ForwardDecisionEngine(_config(), FakeDailyProvider(_bars()))

    target = engine(NOW, account, positions)
    metadata = engine.last_metadata

    assert metadata is not None
    assert target
    assert all(np.isfinite(weight) and weight >= 0.0 for weight in target.values())
    assert sum(target.values()) <= 1.0 + 1e-9
    assert dict(metadata.current_weights or {}) == {"QQQ": 0.1, "SPY": 0.2}
    assert dict(metadata.final_target or {}) == target
    assert metadata.regime is not None
    assert metadata.regime["name"] in {
        "bull_low_vol",
        "bull_high_vol",
        "bear_low_vol",
        "bear_high_vol",
    }
    assert metadata.allocation is not None
    assert "pre_risk_weights" in metadata.allocation
    assert metadata.risk_decision is not None
    assert metadata.risk_decision["liquidation_authorized"] is False
    assert metadata.risk_decision["proposed_gross_exposure"] is not None
    assert metadata.risk_decision["final_gross_exposure"] == pytest.approx(sum(target.values()))
    assert metadata.risk_decision["data_freshness_state"] == "completed_daily_history"
    assert metadata.risk_decision["market_state"] == "submission_gate_pending"
    assert metadata.risk_decision["evaluation_context"]["account_equity"] == 100_000
    assert metadata.risk_decision["evaluation_context"]["position_weights"] == {
        "QQQ": 0.1,
        "SPY": 0.2,
    }
    with pytest.raises(TypeError):
        metadata.final_target["SPY"] = 1.0  # type: ignore[index]


@pytest.mark.parametrize("failure", ["missing", "insufficient", "nonfinite"])
def test_unusable_history_rejects_without_implying_liquidation(failure: str) -> None:
    account, positions = _account_and_positions()
    bars = _bars()
    if failure == "missing":
        bars = [
            bar
            for bar in bars
            if not (
                bar.symbol == "QQQ" and bar.start.astimezone(NEW_YORK).date() == date(2025, 7, 14)
            )
        ]
    elif failure == "insufficient":
        bars = bars[:80]
    else:
        bad = copy(next(bar for bar in bars if bar.symbol == "SPY"))
        object.__setattr__(bad, "close", Decimal("NaN"))
        bars[bars.index(next(bar for bar in bars if bar.symbol == "SPY"))] = bad
    engine = ForwardDecisionEngine(_config(), FakeDailyProvider(bars))

    with pytest.raises(ForwardDecisionError):
        engine(NOW, account, positions)

    metadata = engine.last_metadata
    assert metadata is not None
    assert metadata.status == "rejected"
    assert dict(metadata.final_target or {}) == {"QQQ": 0.1, "SPY": 0.2}
    assert metadata.risk_decision is None
    assert metadata.error


def test_missing_required_symbol_blocks_forward_decision() -> None:
    account, positions = _account_and_positions()
    only_spy = [bar for bar in _bars() if bar.symbol == "SPY"]
    engine = ForwardDecisionEngine(_config(), FakeDailyProvider(only_spy))

    with pytest.raises(ForwardDecisionError, match="Missing required completed daily bars"):
        engine(NOW, account, positions)

    assert engine.last_metadata is not None
    assert engine.last_metadata.status == "rejected"
