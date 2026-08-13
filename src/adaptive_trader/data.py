"""Historical market-data download, normalization, validation, and caching."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from adaptive_trader.config import AppConfig, DataConfig

LOGGER = logging.getLogger(__name__)


class DataValidationError(ValueError):
    """Raised when market data cannot safely be used by the backtester."""


class DataDownloadError(RuntimeError):
    """Raised when the configured market data cannot be downloaded."""


def _single_cache_text(
    frame: pd.DataFrame,
    column: str,
    *,
    normalizer: Any,
    default: str,
) -> str:
    """Return one normalized cache-metadata value or reject mixed provenance."""

    if column not in frame:
        return default
    serialized = frame[column]
    missing = serialized.isna() | serialized.astype(str).str.strip().eq("")
    if bool(missing.any()):
        raise DataValidationError(f"Cache contains missing {column} metadata and cannot be trusted")
    values = {normalizer(str(value).strip()) for value in serialized.tolist()}
    if len(values) > 1:
        raise DataValidationError(f"Cache contains mixed {column} metadata and cannot be trusted")
    return values.pop() if values else default


def _cache_boolean(value: Any) -> bool:
    """Parse a serialized cache boolean without treating ``'False'`` as true."""

    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise DataValidationError("Cache contains an invalid open_prices_are_approximated value")


@dataclass(frozen=True)
class MarketData:
    """Aligned adjusted daily prices, opens, and volumes by market session.

    ``opens`` is optional for compatibility with legacy caches. When absent it
    is conservatively approximated with the preceding completed close and the
    approximation flag is set. Production historical providers should supply
    corporate-action-consistent opens explicitly.
    """

    prices: pd.DataFrame
    volumes: pd.DataFrame
    source: str = "unknown"
    opens: pd.DataFrame | None = None
    feed: str = "unknown"
    adjustment: str = "unknown"
    open_prices_are_approximated: bool = False

    def __post_init__(self) -> None:
        prices = self.prices.copy()
        volumes = self.volumes.copy()
        supplied_opens = self.opens
        opens_supplied = supplied_opens is not None
        opens = supplied_opens.copy() if supplied_opens is not None else prices.shift(1)
        if not opens_supplied and not prices.empty:
            opens.iloc[0] = prices.iloc[0]
        prices.index = pd.to_datetime(prices.index)
        volumes.index = pd.to_datetime(volumes.index)
        opens.index = pd.to_datetime(opens.index)
        prices.columns = [str(column).upper() for column in prices.columns]
        volumes.columns = [str(column).upper() for column in volumes.columns]
        opens.columns = [str(column).upper() for column in opens.columns]
        prices = prices.rename_axis(index="date", columns="ticker")
        volumes = volumes.rename_axis(index="date", columns="ticker")
        opens = opens.rename_axis(index="date", columns="ticker")
        object.__setattr__(self, "prices", prices)
        object.__setattr__(self, "volumes", volumes)
        object.__setattr__(self, "opens", opens)
        object.__setattr__(
            self,
            "open_prices_are_approximated",
            bool(self.open_prices_are_approximated or not opens_supplied),
        )
        object.__setattr__(self, "feed", str(self.feed).upper())
        object.__setattr__(self, "adjustment", str(self.adjustment).lower())

    def to_long_frame(self) -> pd.DataFrame:
        """Return the documented cache schema in date/ticker long form."""

        price_long = self.prices.rename_axis(index="date", columns="ticker").stack(
            future_stack=True
        )
        volume_long = self.volumes.rename_axis(index="date", columns="ticker").stack(
            future_stack=True
        )
        opens = self.opens
        if opens is None:  # ``__post_init__`` always materializes this frame.
            raise DataValidationError("MarketData opens were not initialized")
        open_long = opens.rename_axis(index="date", columns="ticker").stack(future_stack=True)
        frame = pd.concat(
            [
                open_long.rename("adjusted_open"),
                price_long.rename("adjusted_close"),
                volume_long.rename("volume"),
            ],
            axis=1,
        ).reset_index()
        frame["source"] = self.source
        frame["feed"] = self.feed
        frame["adjustment"] = self.adjustment
        frame["open_prices_are_approximated"] = self.open_prices_are_approximated
        return frame.sort_values(["date", "ticker"], ignore_index=True)

    @classmethod
    def from_long_frame(cls, frame: pd.DataFrame, source: str = "cache") -> MarketData:
        """Build aligned wide frames from the documented long-form cache schema."""

        required = {"date", "ticker", "adjusted_close", "volume"}
        missing = required.difference(frame.columns)
        if missing:
            raise DataValidationError(f"Cache is missing columns: {sorted(missing)}")
        if frame.duplicated(["date", "ticker"]).any():
            examples = frame.loc[
                frame.duplicated(["date", "ticker"], keep=False), ["date", "ticker"]
            ].head()
            raise DataValidationError(
                "Cache contains duplicate date/ticker observations: "
                f"{examples.to_dict(orient='records')}"
            )
        working = frame.copy()
        working["date"] = pd.to_datetime(working["date"], errors="raise")
        working["ticker"] = working["ticker"].astype(str).str.upper()
        prices = working.pivot(index="date", columns="ticker", values="adjusted_close")
        volumes = working.pivot(index="date", columns="ticker", values="volume")
        opens = (
            working.pivot(index="date", columns="ticker", values="adjusted_open")
            if "adjusted_open" in working
            else None
        )
        feed = _single_cache_text(
            working,
            "feed",
            normalizer=str.upper,
            default="unknown",
        )
        adjustment = _single_cache_text(
            working,
            "adjustment",
            normalizer=str.lower,
            default="unknown",
        )
        if "open_prices_are_approximated" in working and bool(
            working["open_prices_are_approximated"].isna().any()
        ):
            raise DataValidationError(
                "Cache contains missing open_prices_are_approximated metadata and cannot be trusted"
            )
        approximation_values = (
            {_cache_boolean(value) for value in working["open_prices_are_approximated"].tolist()}
            if "open_prices_are_approximated" in working
            else set()
        )
        if len(approximation_values) > 1:
            raise DataValidationError(
                "Cache contains mixed open_prices_are_approximated metadata and cannot be trusted"
            )
        approximated = approximation_values.pop() if approximation_values else opens is None
        return cls(
            prices=prices.sort_index(),
            volumes=volumes.sort_index(),
            source=source,
            opens=opens.sort_index() if opens is not None else None,
            feed=feed,
            adjustment=adjustment,
            open_prices_are_approximated=approximated,
        )


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


def validate_market_data(
    data: MarketData,
    required_tickers: list[str],
    minimum_history: int = 2,
) -> None:
    """Validate prices, volumes, ordering, coverage, and required history.

    Missing observations are not filled here. Strategies receive the original history and
    must decline to select an asset when its lookback window is incomplete.
    """

    prices = data.prices
    volumes = data.volumes
    opens = data.opens
    if not isinstance(prices.index, pd.DatetimeIndex):
        raise DataValidationError("Price index must be a DatetimeIndex")
    if prices.empty:
        raise DataValidationError("No price observations were supplied")
    if prices.index.has_duplicates:
        raise DataValidationError("Price data contains duplicate dates")
    if not prices.index.is_monotonic_increasing:
        raise DataValidationError("Price dates must be strictly increasing")
    required = _ordered_unique(required_tickers)
    if prices.columns.has_duplicates:
        raise DataValidationError("Price data contains duplicate normalized ticker columns")
    missing = [ticker for ticker in required if ticker not in prices.columns]
    if missing:
        raise DataValidationError(f"Required tickers are missing: {missing}")
    if set(prices.columns) != set(volumes.columns):
        raise DataValidationError("Price and volume ticker columns do not match")
    if not prices.index.equals(volumes.index):
        raise DataValidationError("Price and volume dates are not aligned")
    if opens is None or set(prices.columns) != set(opens.columns):
        raise DataValidationError("Open and close ticker columns do not match")
    if not prices.index.equals(opens.index):
        raise DataValidationError("Open and close dates are not aligned")

    numeric_prices = prices.apply(pd.to_numeric, errors="coerce")
    present = numeric_prices.notna()
    bad_prices = present & (~np.isfinite(numeric_prices) | (numeric_prices <= 0))
    if bad_prices.to_numpy().any():
        locations = list(zip(*np.where(bad_prices.to_numpy()), strict=True))[:5]
        examples = [f"{prices.index[row].date()}/{prices.columns[col]}" for row, col in locations]
        raise DataValidationError(f"Prices must be positive and finite; invalid at {examples}")

    numeric_volumes = volumes.apply(pd.to_numeric, errors="coerce")
    bad_volumes = numeric_volumes.notna() & (~np.isfinite(numeric_volumes) | (numeric_volumes < 0))
    if bad_volumes.to_numpy().any():
        raise DataValidationError("Volumes must be nonnegative and finite when present")

    insufficient = {
        ticker: int(numeric_prices[ticker].notna().sum())
        for ticker in required
        if int(numeric_prices[ticker].notna().sum()) < minimum_history
    }
    if insufficient:
        raise DataValidationError(
            f"Insufficient history; need at least {minimum_history} observations per ticker: "
            f"{insufficient}"
        )

    # Reject lengthy internal gaps rather than silently forward-filling them.
    long_gaps: dict[str, int] = {}
    for ticker in required:
        missing_run = (~numeric_prices[ticker].notna()).astype(int)
        groups = (missing_run == 0).cumsum()
        longest = int(missing_run.groupby(groups).sum().max()) if len(missing_run) else 0
        if longest > 5:
            long_gaps[ticker] = longest
    if long_gaps:
        raise DataValidationError(
            "Price history contains missing periods longer than five trading days: "
            f"{long_gaps}. The loader does not silently forward-fill these gaps."
        )

    numeric_opens = opens.apply(pd.to_numeric, errors="coerce")
    bad_opens = numeric_opens.isna() | (~np.isfinite(numeric_opens)) | (numeric_opens <= 0)
    if bad_opens.to_numpy().any():
        raise DataValidationError("Daily opens must be present, positive, and finite")


def _extract_yfinance_field(raw: pd.DataFrame, field: str, tickers: list[str]) -> pd.DataFrame:
    """Extract one field from any common yfinance column arrangement."""

    if isinstance(raw.columns, pd.MultiIndex):
        level_zero = raw.columns.get_level_values(0)
        level_one = raw.columns.get_level_values(1)
        if field in level_zero:
            extracted = raw.xs(field, axis=1, level=0, drop_level=True)
        elif field in level_one:
            extracted = raw.xs(field, axis=1, level=1, drop_level=True)
        else:
            raise DataDownloadError(f"yfinance response has no {field!r} field")
        if isinstance(extracted, pd.Series):
            extracted = extracted.to_frame(name=tickers[0])
        extracted.columns = [str(column).upper() for column in extracted.columns]
        return extracted

    if field not in raw.columns:
        raise DataDownloadError(f"yfinance response has no {field!r} field")
    if len(tickers) != 1:
        raise DataDownloadError("Flat yfinance columns were returned for multiple tickers")
    return raw[[field]].rename(columns={field: tickers[0]})


def normalize_yfinance_download(raw: pd.DataFrame, tickers: list[str]) -> MarketData:
    """Normalize a yfinance download into adjusted-close and volume matrices."""

    if raw is None or raw.empty:
        raise DataDownloadError("yfinance returned no observations")
    normalized_tickers = _ordered_unique(tickers)
    try:
        prices = _extract_yfinance_field(raw, "Adj Close", normalized_tickers)
    except DataDownloadError:
        # New yfinance versions may return already-adjusted Close data.
        prices = _extract_yfinance_field(raw, "Close", normalized_tickers)
        try:
            opens = _extract_yfinance_field(raw, "Open", normalized_tickers)
            approximated = False
        except DataDownloadError:
            opens = None
            approximated = True
    else:
        try:
            raw_closes = _extract_yfinance_field(raw, "Close", normalized_tickers)
            raw_opens = _extract_yfinance_field(raw, "Open", normalized_tickers)
            adjustment_factor = prices / raw_closes.replace(0.0, np.nan)
            opens = raw_opens * adjustment_factor
            approximated = False
        except DataDownloadError:
            opens = None
            approximated = True
    volumes = _extract_yfinance_field(raw, "Volume", normalized_tickers)
    prices.index = pd.to_datetime(prices.index).tz_localize(None)
    volumes.index = pd.to_datetime(volumes.index).tz_localize(None)
    if opens is not None:
        opens.index = pd.to_datetime(opens.index).tz_localize(None)
    prices = prices.reindex(columns=normalized_tickers).sort_index()
    volumes = volumes.reindex(columns=normalized_tickers).sort_index()
    if opens is not None:
        opens = opens.reindex(columns=normalized_tickers).sort_index()
    return MarketData(
        prices=prices,
        volumes=volumes,
        source="yfinance",
        opens=opens,
        feed="historical_vendor",
        adjustment="all",
        open_prices_are_approximated=approximated,
    )


def save_market_data_cache(data: MarketData, path: Path) -> None:
    """Persist normalized market data using the documented long-form CSV schema."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data.to_long_frame().to_csv(path, index=False)


def load_market_data_cache(path: Path) -> MarketData:
    """Read normalized market data from a local CSV cache."""

    if not path.exists():
        raise FileNotFoundError(f"Market-data cache does not exist: {path}")
    frame = pd.read_csv(path)
    source = _single_cache_text(
        frame,
        "source",
        normalizer=str.lower,
        default=f"cache:{path}",
    )
    return MarketData.from_long_frame(frame, source=source)


def required_history_from_config(config: AppConfig) -> int:
    """Return the largest configured rolling lookback plus one observation."""

    feature_history = (
        max(
            config.momentum.lookback_days,
            config.momentum.volatility_lookback_days,
            config.mean_reversion.zscore_lookback_days,
            config.mean_reversion.long_term_trend_days,
            config.mean_reversion.volatility_lookback_days,
            config.regime.slow_moving_average_days,
            config.regime.volatility_lookback_days
            + config.regime.volatility_threshold_lookback_days,
            config.risk.covariance_lookback_days,
        )
        + 1
    )
    minimum_completed = int(
        getattr(getattr(config, "market_data", None), "minimum_completed_sessions", 0)
    )
    return max(feature_history, minimum_completed)


def download_market_data(config: DataConfig) -> MarketData:
    """Download all configured instruments through the legacy yfinance adapter.

    This function remains for compatibility with the original research prototype.
    Canonical ``market_data.provider: alpaca`` configurations are routed through
    :func:`download_alpaca_market_data` by :func:`load_market_data` and never fall
    back to this adapter.
    """

    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - installation failure path
        raise DataDownloadError(
            "yfinance is not installed; install project dependencies before downloading data"
        ) from exc

    tickers = _ordered_unique([*config.tickers, config.benchmark])
    LOGGER.info("Downloading adjusted daily prices for %s", ", ".join(tickers))
    try:
        raw = yf.download(
            tickers=tickers,
            start=config.start_date,
            end=config.end_date,
            auto_adjust=False,
            actions=False,
            progress=False,
            group_by="column",
            threads=True,
        )
    except Exception as exc:  # pragma: no cover - depends on external service
        raise DataDownloadError(f"yfinance request failed: {exc}") from exc
    data = normalize_yfinance_download(raw, tickers)
    failed = [ticker for ticker in tickers if data.prices[ticker].notna().sum() == 0]
    if failed:
        raise DataDownloadError(
            f"No usable adjusted-price history was returned for ticker(s): {failed}"
        )
    return data


def _historical_request_bounds(config: AppConfig) -> tuple[datetime, datetime]:
    """Return an inclusive research/warm-up interval as aware UTC timestamps."""

    research_start = date.fromisoformat(config.data.start_date)
    request_start = research_start - timedelta(days=config.market_data.historical_calendar_days)
    configured_end = (
        date.fromisoformat(config.data.end_date) + timedelta(days=1)
        if config.data.end_date is not None
        else datetime.now(tz=ZoneInfo("America/New_York")).date()
    )
    return (
        datetime.combine(request_start, datetime.min.time(), tzinfo=UTC),
        datetime.combine(configured_end, datetime.min.time(), tzinfo=UTC),
    )


def _bar_attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def download_alpaca_market_data(
    config: AppConfig,
    *,
    credentials: Any | None = None,
    client: Any | None = None,
) -> MarketData:
    """Download adjusted daily OHLCV with Alpaca's official historical client.

    Credentials are read only from the two explicit ``APA_`` paper environment
    variables when they are not injected by a test. The configured IEX/SIP feed
    and corporate-action adjustment are passed to Alpaca and recorded in the
    resulting cache; there is no feed or provider fallback.
    """

    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from adaptive_trader.live_models import PaperCredentials
    from adaptive_trader.logging_config import redact

    explicit_credentials = credentials
    if explicit_credentials is None:
        try:
            explicit_credentials = PaperCredentials.from_environment()
        except Exception as exc:
            raise DataDownloadError(
                "Alpaca historical data requires explicit APA paper credentials: "
                f"{redact(str(exc))}"
            ) from None
    historical_client = client
    if historical_client is None:
        try:
            historical_client = StockHistoricalDataClient(
                explicit_credentials.api_key,
                explicit_credentials.secret_key,
            )
        except Exception as exc:
            safe = redact(
                str(exc),
                (explicit_credentials.api_key, explicit_credentials.secret_key),
            )
            raise DataDownloadError(f"Alpaca historical client setup failed: {safe}") from None

    feed = config.market_data.feed.upper()
    adjustment_name = config.market_data.adjustment.lower()
    adjustment_by_name = {
        "all": Adjustment.ALL,
        "split": Adjustment.SPLIT,
        "raw": Adjustment.RAW,
    }
    feed_value = DataFeed.IEX if feed == "IEX" else DataFeed.SIP
    start, end = _historical_request_bounds(config)
    request = StockBarsRequest(
        symbol_or_symbols=_ordered_unique([*config.data.tickers, config.data.benchmark]),
        start=start,
        end=end,
        timeframe=TimeFrame.Day,
        adjustment=adjustment_by_name[adjustment_name],
        feed=feed_value,
    )
    LOGGER.info(
        "Downloading Alpaca %s adjusted daily bars from %s through %s",
        feed,
        start.date(),
        (end - timedelta(days=1)).date(),
    )
    try:
        response = historical_client.get_stock_bars(request)
    except Exception as exc:
        safe = redact(
            str(exc),
            (explicit_credentials.api_key, explicit_credentials.secret_key),
        )
        raise DataDownloadError(f"Alpaca {feed} historical bar request failed: {safe}") from None

    raw_data = _bar_attribute(response, "data", response)
    rows: list[dict[str, Any]] = []
    symbol_groups: Any = raw_data.items() if isinstance(raw_data, dict) else (("", raw_data),)
    completed_before = (
        date.fromisoformat(config.data.end_date) + timedelta(days=1)
        if config.data.end_date is not None
        else datetime.now(tz=ZoneInfo("America/New_York")).date()
    )
    for response_symbol, bars in symbol_groups:
        for bar in bars:
            symbol = str(_bar_attribute(bar, "symbol", response_symbol)).upper()
            timestamp = pd.Timestamp(_bar_attribute(bar, "timestamp"))
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("UTC").tz_localize(None)
            if timestamp.date() >= completed_before:
                continue
            rows.append(
                {
                    "date": timestamp.normalize(),
                    "ticker": symbol,
                    "adjusted_open": float(_bar_attribute(bar, "open")),
                    "adjusted_close": float(_bar_attribute(bar, "close")),
                    "volume": float(_bar_attribute(bar, "volume", 0)),
                    "source": "alpaca_historical",
                    "feed": feed,
                    "adjustment": adjustment_name,
                    "open_prices_are_approximated": False,
                }
            )
    if not rows:
        raise DataDownloadError(
            f"Alpaca returned no {feed} daily bars for the configured historical interval"
        )
    data = MarketData.from_long_frame(pd.DataFrame(rows), source="alpaca_historical")
    required = _ordered_unique([*config.data.tickers, config.data.benchmark])
    missing = [symbol for symbol in required if symbol not in data.prices]
    if missing:
        raise DataDownloadError(f"Alpaca returned no daily history for ticker(s): {missing}")
    return data


def load_market_data(config: AppConfig, project_root: Path | None = None) -> MarketData:
    """Load a valid cache or download, validate, and cache historical data."""

    root = Path.cwd() if project_root is None else Path(project_root)
    provider = str(getattr(getattr(config, "market_data", None), "provider", "alpaca"))
    if provider in {"synthetic", "replay"}:
        configured_start = pd.Timestamp(config.data.start_date)
        history_days = int(config.market_data.historical_calendar_days)
        synthetic_start = (configured_start - pd.Timedelta(days=history_days)).date().isoformat()
        synthetic_end = config.data.end_date or "2025-12-31"
        seed = int(getattr(getattr(config, "replay", None), "deterministic_seed", 20260808))
        LOGGER.warning(
            "Using deterministic synthetic historical data because market_data.provider is %s. "
            "Results are engineering evidence only, not real-market performance.",
            provider,
        )
        synthetic_data = generate_synthetic_market_data(
            config.data.tickers,
            start_date=synthetic_start,
            end_date=synthetic_end,
            seed=seed,
        )
        validate_market_data(
            synthetic_data,
            _ordered_unique([*config.data.tickers, config.data.benchmark]),
            required_history_from_config(config),
        )
        return synthetic_data
    cache_path = Path(config.data.cache_file)
    if not cache_path.is_absolute():
        cache_path = root / cache_path
    required = _ordered_unique([*config.data.tickers, config.data.benchmark])
    data: MarketData | None = None
    if cache_path.exists() and not config.data.refresh_cache:
        LOGGER.info("Using cached market data from %s", cache_path)
        cached = load_market_data_cache(cache_path)
        if (
            cached.source == "alpaca_historical"
            and cached.feed == config.market_data.feed.upper()
            and cached.adjustment == config.market_data.adjustment.lower()
        ):
            data = cached
        else:
            LOGGER.warning(
                "Ignoring historical cache whose source/feed/adjustment (%s/%s/%s) does not "
                "match the configured Alpaca request (alpaca_historical/%s/%s)",
                cached.source,
                cached.feed,
                cached.adjustment,
                config.market_data.feed,
                config.market_data.adjustment,
            )
    if data is None:
        if provider != "alpaca":
            raise DataDownloadError(f"Unsupported historical market-data provider: {provider}")
        data = download_alpaca_market_data(config)
        save_market_data_cache(data, cache_path)
        LOGGER.info("Cached market data at %s", cache_path)
    validate_market_data(data, required, required_history_from_config(config))
    return data


def generate_synthetic_market_data(
    tickers: list[str],
    start_date: str = "2014-01-01",
    end_date: str = "2024-12-31",
    seed: int = 20240311,
) -> MarketData:
    """Create deterministic regime-changing data for tests and offline demonstrations.

    The result is explicitly synthetic and must never be described as real-market evidence.
    """

    names = _ordered_unique(tickers)
    dates = pd.bdate_range(start_date, end_date)
    if len(dates) < 3:
        raise ValueError("Synthetic data range must contain at least three business days")
    rng = np.random.default_rng(seed)
    day = np.arange(len(dates))
    cycle = np.sin(2 * np.pi * day / 420.0)
    high_vol = (np.sin(2 * np.pi * day / 750.0 + 0.8) < -0.35).astype(float)
    common = 0.00015 + 0.00045 * cycle
    common_noise = rng.normal(0.0, 0.006 + 0.008 * high_vol, size=len(dates))
    returns: dict[str, np.ndarray] = {}
    for index, ticker in enumerate(names):
        beta = 0.45 + 0.10 * (index % 5)
        drift = 0.00005 + 0.000025 * (index % 4)
        idiosyncratic = rng.normal(0.0, 0.004 + index * 0.00035, size=len(dates))
        reversion = 0.0015 * np.sin(2 * np.pi * day / (16.0 + index))
        daily = drift + beta * (common + common_noise) + idiosyncratic + reversion
        returns[ticker] = np.clip(daily, -0.18, 0.18)
    return_frame = pd.DataFrame(returns, index=dates)
    prices = 100.0 * (1.0 + return_frame).cumprod()
    volumes = pd.DataFrame(
        rng.integers(500_000, 8_000_000, size=prices.shape),
        index=dates,
        columns=names,
        dtype=float,
    )
    return MarketData(
        prices=prices,
        volumes=volumes,
        source=f"synthetic(seed={seed})",
        feed="synthetic",
        adjustment="synthetic",
    )
