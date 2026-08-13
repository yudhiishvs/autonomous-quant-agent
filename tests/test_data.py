"""Offline tests for market-data normalization and validation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from adaptive_trader.data import (
    DataDownloadError,
    DataValidationError,
    MarketData,
    download_alpaca_market_data,
    generate_synthetic_market_data,
    load_market_data,
    load_market_data_cache,
    normalize_yfinance_download,
    save_market_data_cache,
    validate_market_data,
)
from adaptive_trader.live_models import PaperCredentials


def _valid_data() -> MarketData:
    dates = pd.bdate_range("2022-01-03", periods=8)
    prices = pd.DataFrame(
        {"AAA": np.linspace(100, 107, len(dates)), "BBB": np.linspace(50, 52, len(dates))},
        index=dates,
    )
    volumes = pd.DataFrame(1_000.0, index=dates, columns=prices.columns)
    return MarketData(prices=prices, volumes=volumes, source="test")


def test_validation_rejects_nonpositive_and_nonfinite_prices() -> None:
    data = _valid_data()
    for invalid in (0.0, -1.0, np.inf):
        prices = data.prices.copy()
        prices.iloc[2, 0] = invalid
        with pytest.raises(DataValidationError, match="positive and finite"):
            validate_market_data(MarketData(prices, data.volumes), ["AAA", "BBB"])


def test_long_cache_rejects_duplicate_observations() -> None:
    frame = _valid_data().to_long_frame()
    duplicate = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(DataValidationError, match="duplicate"):
        MarketData.from_long_frame(duplicate)


def test_cache_round_trip(tmp_path) -> None:
    path = tmp_path / "nested" / "cache.csv"
    original = _valid_data()
    save_market_data_cache(original, path)
    restored = load_market_data_cache(path)
    pd.testing.assert_frame_equal(
        restored.prices, original.prices, check_names=True, check_freq=False
    )
    pd.testing.assert_frame_equal(
        restored.volumes, original.volumes, check_names=True, check_freq=False
    )
    assert restored.source == original.source


@pytest.mark.parametrize(
    ("column", "replacement"),
    (("feed", "SIP"), ("adjustment", "raw")),
)
def test_cache_rejects_mixed_feed_or_adjustment_provenance(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    frame = _valid_data().to_long_frame()
    frame[column] = "IEX" if column == "feed" else "all"
    frame.loc[frame.index[-1], column] = replacement
    path = tmp_path / "mixed-provenance.csv"
    frame.to_csv(path, index=False)

    with pytest.raises(DataValidationError, match=f"mixed {column}"):
        load_market_data_cache(path)


def test_cache_parses_serialized_open_approximation_boolean(tmp_path: Path) -> None:
    frame = _valid_data().to_long_frame()
    frame["open_prices_are_approximated"] = "False"
    path = tmp_path / "boolean-metadata.csv"
    frame.to_csv(path, index=False)

    restored = load_market_data_cache(path)

    assert restored.open_prices_are_approximated is False


@pytest.mark.parametrize("column", ("source", "feed", "adjustment"))
def test_cache_rejects_partial_provenance_metadata(tmp_path: Path, column: str) -> None:
    frame = _valid_data().to_long_frame()
    frame.loc[frame.index[-1], column] = None
    path = tmp_path / "partial-provenance.csv"
    frame.to_csv(path, index=False)

    with pytest.raises(DataValidationError, match=f"missing {column}"):
        load_market_data_cache(path)


def test_alpaca_loader_rejects_cache_without_alpaca_source_provenance(
    default_config,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = default_config.to_dict()
    values["data"].update(
        {
            "cache_file": "data/cache/market_data.csv",
            "refresh_cache": False,
        }
    )
    values["market_data"]["provider"] = "alpaca"
    config = type(default_config).from_dict(values)
    cached = generate_synthetic_market_data(
        config.data.tickers,
        start_date="2012-01-02",
        end_date="2025-12-31",
        seed=9817,
    )
    cached = MarketData(
        prices=cached.prices,
        volumes=cached.volumes,
        source="unverified_vendor",
        opens=cached.opens,
        feed=config.market_data.feed,
        adjustment=config.market_data.adjustment,
    )
    cache_path = tmp_path / config.data.cache_file
    save_market_data_cache(cached, cache_path)

    downloaded = MarketData(
        prices=cached.prices,
        volumes=cached.volumes,
        source="alpaca_historical",
        opens=cached.opens,
        feed=config.market_data.feed,
        adjustment=config.market_data.adjustment,
    )
    calls = 0

    def official_alpaca_download(_config):
        nonlocal calls
        calls += 1
        return downloaded

    monkeypatch.setattr(
        "adaptive_trader.data.download_alpaca_market_data",
        official_alpaca_download,
    )

    loaded = load_market_data(config, project_root=tmp_path)

    assert calls == 1
    assert loaded.source == "alpaca_historical"
    assert load_market_data_cache(cache_path).source == "alpaca_historical"


def test_normalizes_yfinance_multiindex_output() -> None:
    dates = pd.bdate_range("2023-01-02", periods=3)
    columns = pd.MultiIndex.from_product([["Adj Close", "Volume"], ["AAA", "BBB"]])
    raw = pd.DataFrame(
        [[100.0, 50.0, 10.0, 20.0], [101.0, 51.0, 11.0, 21.0], [102.0, 52.0, 12.0, 22.0]],
        index=dates,
        columns=columns,
    )
    data = normalize_yfinance_download(raw, ["AAA", "BBB"])
    assert list(data.prices.columns) == ["AAA", "BBB"]
    assert data.prices.loc[dates[-1], "AAA"] == 102.0
    assert data.volumes.loc[dates[-1], "BBB"] == 22.0


def test_validation_rejects_large_missing_period() -> None:
    data = _valid_data()
    prices = data.prices.copy()
    prices.loc[prices.index[1:7], "AAA"] = np.nan
    with pytest.raises(DataValidationError, match="missing periods"):
        validate_market_data(MarketData(prices, data.volumes), ["AAA", "BBB"])


def test_alpaca_historical_download_uses_explicit_feed_adjustment_and_daily_opens(
    default_config,
) -> None:
    captured: list[object] = []

    class FakeHistoricalClient:
        def get_stock_bars(self, request):
            captured.append(request)
            return SimpleNamespace(
                data={
                    symbol: [
                        SimpleNamespace(
                            symbol=symbol,
                            timestamp=pd.Timestamp("2025-01-02", tz="UTC"),
                            open=100.0,
                            close=101.0,
                            volume=10_000,
                        )
                    ]
                    for symbol in default_config.data.tickers
                }
            )

    data = download_alpaca_market_data(
        default_config,
        credentials=PaperCredentials("explicit-paper-key", "explicit-paper-secret"),
        client=FakeHistoricalClient(),
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.feed.value == "iex"
    assert request.adjustment.value == "all"
    assert str(request.timeframe) == "1Day"
    assert data.source == "alpaca_historical"
    assert data.feed == "IEX"
    assert data.adjustment == "all"
    assert data.open_prices_are_approximated is False
    assert data.opens.loc[pd.Timestamp("2025-01-02"), "SPY"] == 100.0


def test_alpaca_historical_errors_redact_explicit_credentials(default_config) -> None:
    class FailingHistoricalClient:
        def get_stock_bars(self, request):
            del request
            raise RuntimeError("explicit-paper-key explicit-paper-secret")

    with pytest.raises(DataDownloadError) as raised:
        download_alpaca_market_data(
            default_config,
            credentials=PaperCredentials("explicit-paper-key", "explicit-paper-secret"),
            client=FailingHistoricalClient(),
        )

    assert "explicit-paper-key" not in str(raised.value)
    assert "explicit-paper-secret" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)
