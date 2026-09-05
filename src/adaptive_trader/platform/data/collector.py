"""Restart-safe orchestration for validated historical and streaming bars."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.data.calendar import ExchangeCalendar, TradingInterval
from adaptive_trader.platform.data.normalization import (
    CanonicalBar,
    NormalizationPolicy,
    normalize_alpaca_bar,
    normalize_fixture_bar,
)
from adaptive_trader.platform.data.provider import (
    HistoricalRequest,
    HistoricalResult,
    HistoricalStatus,
    MarketDataProvider,
    MarketDataProviderError,
    RawBarEnvelope,
    StreamEventType,
    StreamSubscription,
)
from adaptive_trader.platform.data.watermarks import (
    DataGap,
    DataSeries,
    GapDetection,
    GapRepairCoverage,
    GapRepository,
    GapStatus,
    WatermarkRepository,
    detect_data_gaps,
)
from adaptive_trader.platform.domain import AuditPayload, AuditWriter, require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.market_data import (
    BarIdentity,
    BarWrite,
    BarWriteStatus,
    MarketDataRepository,
    SymbolWatermark,
)
from adaptive_trader.platform.storage.repositories import AuditRepository

RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16)
_MAX_HISTORICAL_PAGES = 100

Clock = Callable[[], datetime]
Sleeper = Callable[[int], None]


class MarketDataCollectorError(RuntimeError):
    """Raised when collection cannot continue without weakening data guarantees."""


class CollectorRateLimitError(MarketDataCollectorError):
    """Raised after the bounded explicit rate-limit retry schedule is exhausted."""


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Deterministic counters for one bounded collector operation."""

    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    corrected: int = 0
    out_of_order: int = 0
    rate_limits: int = 0
    gaps_recorded: int = 0

    def __add__(self, other: CollectionResult) -> CollectionResult:
        if type(other) is not CollectionResult:
            return NotImplemented
        return CollectionResult(
            received=self.received + other.received,
            inserted=self.inserted + other.inserted,
            duplicates=self.duplicates + other.duplicates,
            corrected=self.corrected + other.corrected,
            out_of_order=self.out_of_order + other.out_of_order,
            rate_limits=self.rate_limits + other.rate_limits,
            gaps_recorded=self.gaps_recorded + other.gaps_recorded,
        )


def _utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name.replace(" ", "_"))
    except DomainValidationError:
        raise MarketDataCollectorError(f"{field_name} must be a UTC instant") from None


class MarketDataCollector:
    """Collect only an experiment's exact allowlist into append-only storage.

    All clocks, sleeps, provider transports, and persistence boundaries are injected. The class
    has no mechanism for constructing a network client and no broker or order authority.
    """

    __slots__ = (
        "_audit",
        "_calendar",
        "_clock",
        "_experiment",
        "_gaps",
        "_highest_seen",
        "_market_data",
        "_policy",
        "_provider",
        "_readiness_start_at",
        "_series_provider",
        "_sleep",
        "_watermarks",
    )

    def __init__(
        self,
        *,
        experiment: ExperimentDefinition,
        provider: MarketDataProvider,
        calendar: ExchangeCalendar,
        market_data_repository: MarketDataRepository,
        gap_repository: GapRepository,
        watermark_repository: WatermarkRepository,
        readiness_start_at: datetime,
        clock: Clock,
        sleep: Sleeper,
        audit_repository: AuditRepository | None = None,
    ) -> None:
        if type(experiment) is not ExperimentDefinition:
            raise TypeError("collector requires an immutable experiment definition")
        if not callable(getattr(provider, "fetch_historical", None)) or not callable(
            getattr(provider, "open_stream", None)
        ):
            raise TypeError("collector requires a market-data provider")
        if provider.name not in {"alpaca", "fixture"}:
            raise MarketDataCollectorError("collector provider is unsupported")
        if not isinstance(calendar, ExchangeCalendar):
            raise TypeError("collector requires an exchange calendar")
        if calendar.name != experiment.market_data.exchange_calendar:
            raise MarketDataCollectorError("collector calendar does not match the experiment")
        if type(market_data_repository) is not MarketDataRepository:
            raise TypeError("collector requires a market-data repository")
        if type(gap_repository) is not GapRepository:
            raise TypeError("collector requires a gap repository")
        if type(watermark_repository) is not WatermarkRepository:
            raise TypeError("collector requires a watermark repository")
        if not callable(clock) or not callable(sleep):
            raise TypeError("collector requires injected clock and sleep functions")
        if audit_repository is not None and type(audit_repository) is not AuditRepository:
            raise TypeError("collector audit repository is invalid")
        self._experiment = experiment
        self._provider = provider
        self._calendar = calendar
        self._market_data = market_data_repository
        self._gaps = gap_repository
        self._watermarks = watermark_repository
        self._readiness_start_at = _utc(readiness_start_at, field_name="readiness start")
        self._clock = clock
        self._sleep = sleep
        self._audit = audit_repository or AuditRepository(
            market_data_repository.engine,
            writer=AuditWriter.COLLECTOR,
        )
        if provider.name == "alpaca":
            self._policy = NormalizationPolicy.for_external_experiment(experiment)
            self._series_provider = "alpaca"
        else:
            self._policy = NormalizationPolicy.for_offline_fixture(experiment)
            self._series_provider = "fixture"
        self._highest_seen: dict[str, datetime] = {}

    @property
    def collection_allowlist(self) -> tuple[str, ...]:
        """Return the exact immutable subscription authority."""

        return self._experiment.collection_allowlist

    def collect_historical(self, *, start_at: datetime, end_at: datetime) -> CollectionResult:
        """Resume every allowed symbol from durable contiguous readiness and detect gaps."""

        start_at = _utc(start_at, field_name="collection start")
        end_at = _utc(end_at, field_name="collection end")
        if start_at >= end_at:
            raise MarketDataCollectorError("collection start must precede end")
        result = CollectionResult()
        for symbol in self.collection_allowlist:
            resume_at = self._resume_at(symbol=symbol, requested_start=start_at)
            if resume_at >= end_at:
                continue
            page_token: str | None = None
            seen_tokens: set[str] = set()
            for _page in range(_MAX_HISTORICAL_PAGES):
                request = HistoricalRequest(
                    symbols=(symbol,),
                    start_at=resume_at,
                    end_at=end_at,
                    page_token=page_token,
                )
                page, rate_limits = self._fetch_with_retry(request)
                result += CollectionResult(rate_limits=rate_limits)
                for envelope in page.bars:
                    result += self._persist_envelope(
                        envelope,
                        range_start_at=resume_at,
                        range_end_at=end_at,
                    )
                page_token = page.next_page_token
                if page_token is None:
                    break
                if page_token in seen_tokens:
                    raise MarketDataCollectorError("historical pagination token repeated")
                seen_tokens.add(page_token)
            else:
                raise MarketDataCollectorError("historical pagination limit exceeded")
            gaps = self._detect_and_record_gaps(
                symbol=symbol,
                start_at=resume_at,
                end_at=end_at,
                detected_at=_utc(self._clock(), field_name="clock"),
            )
            result += CollectionResult(gaps_recorded=len(gaps))
            self._recompute(symbol=symbol, end_at=end_at)
        return result

    def repair_gap(self, detection: GapDetection) -> DataGap:
        """Persist and claim a gap before requesting its exact-series repair.

        Re-entering this operation after process termination deterministically reopens the prior
        durable claim before creating a new attempt. Normal exceptions are already reopened by the
        repository; a lingering ``REPAIRING`` state therefore identifies an interrupted call.
        """

        if type(detection) is not GapDetection:
            raise MarketDataCollectorError("gap repair requires a validated detection")
        if detection.experiment_hash != self._experiment.content_hash:
            raise MarketDataCollectorError("gap repair experiment does not match the collector")
        expected_series = self._series(detection.series.symbol)
        if detection.series != expected_series:
            raise MarketDataCollectorError("gap repair series does not match the collector")
        attempted_at = _utc(self._clock(), field_name="clock")
        persisted = self._gaps.record(detection)
        if persisted.status is GapStatus.REPAIRING:
            self._gaps.reopen_interrupted(persisted.gap_id, reopened_at=attempted_at)

        def repairer(claimed: DataGap) -> GapRepairCoverage:
            page_token: str | None = None
            seen_tokens: set[str] = set()
            try:
                for _page_number in range(_MAX_HISTORICAL_PAGES):
                    request = HistoricalRequest(
                        symbols=(claimed.series.symbol,),
                        start_at=claimed.start_at,
                        end_at=claimed.end_at,
                        page_token=page_token,
                    )
                    page, _rate_limits = self._fetch_with_retry(request)
                    for envelope in page.bars:
                        self._persist_envelope(
                            envelope,
                            range_start_at=claimed.start_at,
                            range_end_at=claimed.end_at,
                        )
                    page_token = page.next_page_token
                    if page_token is None:
                        break
                    if page_token in seen_tokens:
                        raise MarketDataCollectorError("gap repair pagination token repeated")
                    seen_tokens.add(page_token)
                else:
                    raise MarketDataCollectorError("gap repair pagination limit exceeded")
            except CollectorRateLimitError:
                return GapRepairCoverage(
                    series=claimed.series,
                    start_at=claimed.start_at,
                    end_at=claimed.end_at,
                    observed_intervals=self._observed_intervals(
                        series=claimed.series,
                        start_at=claimed.start_at,
                        end_at=claimed.end_at,
                    ),
                    completed_at=_utc(self._clock(), field_name="clock"),
                    unambiguous=False,
                )
            return GapRepairCoverage(
                series=claimed.series,
                start_at=claimed.start_at,
                end_at=claimed.end_at,
                observed_intervals=self._observed_intervals(
                    series=claimed.series,
                    start_at=claimed.start_at,
                    end_at=claimed.end_at,
                ),
                completed_at=_utc(self._clock(), field_name="clock"),
                unambiguous=True,
            )

        repaired = self._gaps.repair(
            detection,
            attempted_at=attempted_at,
            repairer=repairer,
        )
        self._recompute(symbol=detection.series.symbol, end_at=detection.end_at)
        return repaired

    def collect_stream(self, *, max_events: int) -> CollectionResult:
        """Process a bounded live stream and reconnect with the exact retry schedule."""

        if type(max_events) is not int or max_events < 1:
            raise MarketDataCollectorError("stream event bound must be a positive integer")
        subscription = StreamSubscription(symbols=self.collection_allowlist)
        result = CollectionResult()
        handled = 0
        retry_index = 0
        while handled < max_events:
            stream = None
            try:
                stream = self._provider.open_stream(subscription)
                while handled < max_events:
                    event = stream.receive()
                    handled += 1
                    if event.event_type is StreamEventType.CONNECTED:
                        self._audit_control(
                            event_type=(
                                "collector.connected"
                                if retry_index == 0
                                else "collector.reconnected"
                            ),
                            occurred_at=event.occurred_at,
                            status="connected",
                            attempt=retry_index,
                        )
                    elif event.event_type is StreamEventType.BAR:
                        if event.bar is None:  # pragma: no cover - protected by StreamEvent
                            raise MarketDataCollectorError(
                                "stream bar event is missing its payload"
                            )
                        result += self._persist_envelope(event.bar, detect_stream_gaps=True)
                        retry_index = 0
                    elif event.event_type is StreamEventType.RATE_LIMITED:
                        result += CollectionResult(rate_limits=1)
                        self._audit_control(
                            event_type="collector.rate_limited",
                            occurred_at=event.occurred_at,
                            status="rate_limited",
                            attempt=retry_index,
                        )
                        break
                    elif event.event_type is StreamEventType.DISCONNECTED:
                        self._audit_control(
                            event_type="collector.disconnected",
                            occurred_at=event.occurred_at,
                            status="disconnected",
                            attempt=retry_index,
                        )
                        break
            except StopIteration:
                return result
            except MarketDataProviderError as exc:
                if not exc.retryable:
                    raise
                self._audit_control(
                    event_type="collector.disconnected",
                    occurred_at=_utc(self._clock(), field_name="clock"),
                    status="transport_failure",
                    attempt=retry_index,
                )
            finally:
                if stream is not None:
                    stream.close()
            if handled >= max_events:
                break
            if retry_index >= len(RETRY_DELAYS_SECONDS):
                raise MarketDataCollectorError("stream reconnect retry schedule exhausted")
            self._sleep(RETRY_DELAYS_SECONDS[retry_index])
            retry_index += 1
        return result

    def _fetch_with_retry(self, request: HistoricalRequest) -> tuple[HistoricalResult, int]:
        rate_limits = 0
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                result = self._provider.fetch_historical(request)
            except MarketDataProviderError as exc:
                if not exc.retryable:
                    raise
                if attempt >= len(RETRY_DELAYS_SECONDS):
                    raise MarketDataCollectorError(
                        "historical transport retry schedule exhausted"
                    ) from None
            else:
                if result.status is HistoricalStatus.OK:
                    return result, rate_limits
                if result.status is not HistoricalStatus.RATE_LIMITED:
                    raise MarketDataCollectorError("provider returned an unsupported status")
                rate_limits += 1
                self._audit_control(
                    event_type="collector.rate_limited",
                    occurred_at=_utc(self._clock(), field_name="clock"),
                    status="rate_limited",
                    attempt=attempt,
                )
                if attempt >= len(RETRY_DELAYS_SECONDS):
                    raise CollectorRateLimitError("historical rate-limit retry schedule exhausted")
            self._sleep(RETRY_DELAYS_SECONDS[attempt])
        raise AssertionError("unreachable")

    def _persist_envelope(
        self,
        envelope: RawBarEnvelope,
        *,
        range_start_at: datetime | None = None,
        range_end_at: datetime | None = None,
        detect_stream_gaps: bool = False,
    ) -> CollectionResult:
        if type(envelope) is not RawBarEnvelope:
            raise MarketDataCollectorError("provider returned an invalid bar envelope")
        if self._provider.name == "alpaca":
            canonical = normalize_alpaca_bar(
                envelope.payload_copy(),
                policy=self._policy,
                receipt_timestamp_utc=envelope.received_at,
                quality_flags=("complete",),
                is_correction=envelope.is_correction,
            )
        else:
            if envelope.is_correction:
                raise MarketDataCollectorError("fixture bars cannot claim provider corrections")
            canonical = normalize_fixture_bar(
                envelope.payload_copy(),
                policy=self._policy,
                receipt_timestamp_utc=envelope.received_at,
                quality_flags=("complete",),
            )
        if range_start_at is not None and canonical.interval_start_utc < range_start_at:
            raise MarketDataCollectorError("provider bar precedes the requested range")
        if range_end_at is not None and canonical.interval_end_utc > range_end_at:
            raise MarketDataCollectorError("provider bar exceeds the requested range")
        prior = self._durable_or_seen_highest(canonical)
        out_of_order = prior is not None and canonical.interval_end_utc < prior
        write_result = self._market_data.append(self._bar_write(canonical))
        gaps = (
            self._record_stream_gaps(canonical)
            if detect_stream_gaps and (prior is None or canonical.interval_end_utc > prior)
            else ()
        )
        if prior is None or canonical.interval_end_utc > prior:
            self._highest_seen[canonical.symbol] = canonical.interval_end_utc
        self._recompute(
            symbol=canonical.symbol,
            end_at=max(canonical.interval_end_utc, prior or canonical.interval_end_utc),
        )
        return CollectionResult(
            received=1,
            inserted=int(write_result.status is BarWriteStatus.INSERTED),
            duplicates=int(write_result.status is BarWriteStatus.DUPLICATE),
            corrected=int(write_result.status is BarWriteStatus.CORRECTED),
            out_of_order=int(out_of_order),
            gaps_recorded=len(gaps),
        )

    def _bar_write(self, canonical: CanonicalBar) -> BarWrite:
        write = BarWrite(
            identity=BarIdentity(
                provider=canonical.provider,
                feed=canonical.feed,
                adjustment=canonical.adjustment,
                symbol=canonical.symbol,
                timeframe=canonical.timeframe,
                start_at=canonical.interval_start_utc,
                end_at=canonical.interval_end_utc,
            ),
            received_at=canonical.receipt_timestamp_utc,
            open=canonical.open,
            high=canonical.high,
            low=canonical.low,
            close=canonical.close,
            volume=canonical.volume,
            trade_count=canonical.trade_count,
            vwap=canonical.vwap,
            quality_flags=canonical.quality_flags,
            source=canonical.provider,
            source_payload_hash=canonical.payload_hash,
            source_mode=canonical.source_mode,
            provider_timestamp=canonical.provider_event_timestamp_utc,
            source_event_id=canonical.source_event_id,
            is_correction=canonical.is_correction,
            correction_of_source_event_id=canonical.correction_of_source_event_id,
        )
        if write.normalized_payload_hash != canonical.payload_hash:
            raise MarketDataCollectorError("canonical bar hash changed at the storage boundary")
        return write

    def _record_stream_gaps(self, canonical: CanonicalBar) -> tuple[DataGap, ...]:
        watermark = self._watermark(canonical.symbol)
        scan_start = self._readiness_start_at
        if watermark is not None:
            scan_start = max(scan_start, watermark.contiguous_through)
        if canonical.interval_end_utc <= scan_start:
            return ()
        return self._detect_and_record_gaps(
            symbol=canonical.symbol,
            start_at=scan_start,
            end_at=canonical.interval_end_utc,
            detected_at=canonical.receipt_timestamp_utc,
        )

    def _series(self, symbol: str) -> DataSeries:
        if symbol not in self.collection_allowlist:
            raise MarketDataCollectorError("symbol is outside the collection allowlist")
        return DataSeries(
            provider=self._series_provider,
            feed="iex",
            adjustment="raw",
            symbol=symbol,
            timeframe="1Min",
        )

    def _anchor_identity(self, symbol: str) -> BarIdentity:
        return BarIdentity(
            provider=self._series_provider,
            feed="iex",
            adjustment="raw",
            symbol=symbol,
            timeframe="1Min",
            start_at=self._readiness_start_at,
            end_at=self._readiness_start_at + timedelta(minutes=1),
        )

    def _watermark(self, symbol: str) -> SymbolWatermark | None:
        return self._market_data.watermark(
            experiment_hash=self._experiment.content_hash,
            identity=self._anchor_identity(symbol),
        )

    def _resume_at(self, *, symbol: str, requested_start: datetime) -> datetime:
        watermark = self._watermark(symbol)
        if watermark is None:
            return requested_start
        self._highest_seen[symbol] = watermark.contiguous_through
        return max(requested_start, watermark.contiguous_through)

    def _durable_or_seen_highest(self, canonical: CanonicalBar) -> datetime | None:
        seen = self._highest_seen.get(canonical.symbol)
        if seen is not None:
            return seen
        watermark = self._watermark(canonical.symbol)
        if watermark is not None:
            self._highest_seen[canonical.symbol] = watermark.contiguous_through
            return watermark.contiguous_through
        return None

    def _recompute(self, *, symbol: str, end_at: datetime) -> None:
        if end_at <= self._readiness_start_at:
            return
        expected = self._calendar.expected_intervals(
            start_at=self._readiness_start_at,
            end_at=end_at,
            timeframe="1Min",
        )
        if not expected:
            return
        now = _utc(self._clock(), field_name="clock")
        inspected_end = expected[-1].end_at
        if now < inspected_end:
            raise MarketDataCollectorError("clock precedes persisted market-data readiness")
        self._watermarks.recompute_symbol(
            experiment_hash=self._experiment.content_hash,
            series=self._series(symbol),
            start_at=expected[0].start_at,
            end_at=inspected_end,
            updated_at=now,
        )

    def _observed_intervals(
        self,
        *,
        series: DataSeries,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[TradingInterval, ...]:
        expected = self._calendar.expected_intervals(
            start_at=start_at,
            end_at=end_at,
            timeframe=series.timeframe,
        )
        observed: list[TradingInterval] = []
        for interval in expected:
            identity = BarIdentity(
                provider=series.provider,
                feed=series.feed,
                adjustment=series.adjustment,
                symbol=series.symbol,
                timeframe=series.timeframe,
                start_at=interval.start_at,
                end_at=interval.end_at,
            )
            if self._market_data.latest(identity) is not None:
                observed.append(interval)
        return tuple(observed)

    def _detect_and_record_gaps(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        detected_at: datetime,
    ) -> tuple[DataGap, ...]:
        series = self._series(symbol)
        observed = self._observed_intervals(
            series=series,
            start_at=start_at,
            end_at=end_at,
        )
        detections = detect_data_gaps(
            calendar=self._calendar,
            experiment_hash=self._experiment.content_hash,
            series=series,
            start_at=start_at,
            end_at=end_at,
            observed_intervals=observed,
            detected_at=detected_at,
        )
        return tuple(self._gaps.record(detection) for detection in detections)

    def _audit_control(
        self,
        *,
        event_type: str,
        occurred_at: datetime,
        status: str,
        attempt: int,
    ) -> None:
        occurred_at = _utc(occurred_at, field_name="audit occurrence")
        event_hash = sha256_hex(
            (
                self._experiment.content_hash,
                event_type,
                occurred_at,
                status,
                attempt,
            )
        )
        self._audit.append(
            stream_id=f"aqa_collector:market_data:{self._experiment.content_hash}",
            event_type=event_type,
            occurred_at=occurred_at,
            payload=AuditPayload.from_mapping(
                {
                    "attempt": attempt,
                    "experiment_hash": self._experiment.content_hash,
                    "idempotency_key": f"collector_event_{event_hash}",
                    "provider": self._series_provider,
                    "status": status,
                    "symbols": self.collection_allowlist,
                }
            ),
        )


__all__ = [
    "RETRY_DELAYS_SECONDS",
    "CollectionResult",
    "CollectorRateLimitError",
    "MarketDataCollector",
    "MarketDataCollectorError",
]
