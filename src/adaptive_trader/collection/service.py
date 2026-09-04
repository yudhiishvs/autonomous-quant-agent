"""Restart-safe orchestration for historical and real-time market-data collection."""

from __future__ import annotations

import logging
import math
import os
import random
import socket
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import Protocol

import exchange_calendars

from adaptive_trader.collection.contracts import RawBarObservationV1
from adaptive_trader.collection.repository import (
    BatchResult,
    Checkpoint,
    CheckpointKey,
    CoverageAdvance,
    LeaseLostError,
    LeaseToken,
    LeaseUnavailableError,
    MarketDataRepository,
)
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1

LOGGER = logging.getLogger(__name__)


def _is_retryable(error: BaseException) -> bool:
    return getattr(error, "retryable", True) is not False


def _error_details(error: BaseException) -> dict[str, object]:
    details: dict[str, object] = {
        "error_type": type(error).__name__,
        "retryable": _is_retryable(error),
    }
    provider_status = getattr(error, "provider_status", None)
    if isinstance(provider_status, int) and not isinstance(provider_status, bool):
        details["provider_status"] = provider_status
    retry_after = getattr(error, "retry_after_seconds", None)
    if (
        isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and math.isfinite(float(retry_after))
    ):
        details["retry_after_seconds"] = round(float(retry_after), 3)
    return details


class HistoricalBarSource(Protocol):
    def fetch(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        source: str = "historical_backfill",
    ) -> tuple[RawBarObservationV1, ...]: ...


class LiveBarSource(Protocol):
    def run(
        self,
        symbols: Sequence[str],
        handler: Callable[[RawBarObservationV1], None],
        *,
        stop_requested: Callable[[], bool],
    ) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectorServiceConfig:
    """Operational controls whose defaults favor recovery over dropped data."""

    checkpoint_name: str = "rest_coverage"
    lease_name: str = "market-data-collector.v1"
    lease_ttl_seconds: int = 90
    lease_renew_interval_seconds: int = 30
    reconciliation_interval_seconds: int = 60
    reconciliation_overlap_minutes: int = 5
    completed_bar_lag_minutes: int = 2
    api_symbol_batch_size: int = 29
    database_batch_size: int = 750
    rest_max_attempts: int = 6
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 60.0
    provider_retry_max_seconds: float = 300.0
    stream_restart_initial_seconds: float = 1.0
    stream_restart_max_seconds: float = 60.0
    worker_shutdown_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        positive_integers = {
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "lease_renew_interval_seconds": self.lease_renew_interval_seconds,
            "reconciliation_interval_seconds": self.reconciliation_interval_seconds,
            "completed_bar_lag_minutes": self.completed_bar_lag_minutes,
            "api_symbol_batch_size": self.api_symbol_batch_size,
            "database_batch_size": self.database_batch_size,
            "rest_max_attempts": self.rest_max_attempts,
        }
        for field_name, value in positive_integers.items():
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.reconciliation_overlap_minutes < 0:
            raise ValueError("reconciliation_overlap_minutes cannot be negative")
        if self.lease_ttl_seconds < 10:
            raise ValueError("lease_ttl_seconds must be at least 10")
        if self.lease_renew_interval_seconds * 2 >= self.lease_ttl_seconds:
            raise ValueError("lease renewal must run at less than half the lease TTL")
        if self.api_symbol_batch_size > len(COLLECTION_UNIVERSE_V1.symbols):
            raise ValueError("api_symbol_batch_size cannot exceed the collection universe")
        retry_values = {
            "retry_initial_seconds": self.retry_initial_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "provider_retry_max_seconds": self.provider_retry_max_seconds,
            "stream_restart_initial_seconds": self.stream_restart_initial_seconds,
            "stream_restart_max_seconds": self.stream_restart_max_seconds,
            "worker_shutdown_timeout_seconds": self.worker_shutdown_timeout_seconds,
        }
        for field_name, retry_value in retry_values.items():
            if retry_value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds cannot be less than retry_initial_seconds")
        if self.stream_restart_max_seconds < self.stream_restart_initial_seconds:
            raise ValueError(
                "stream_restart_max_seconds cannot be less than stream_restart_initial_seconds"
            )


def completed_bar_cutoff(now: datetime, *, lag_minutes: int) -> datetime:
    """Return an exclusive, minute-aligned boundary for safely completed bars."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if lag_minutes <= 0:
        raise ValueError("lag_minutes must be positive")
    normalized = now.astimezone(UTC).replace(second=0, microsecond=0)
    return normalized - timedelta(minutes=lag_minutes - 1)


def _as_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _session_windows(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    """Clip a UTC interval to actual XNYS regular sessions, including early closes."""

    start_utc = _as_utc(start, name="start")
    end_utc = _as_utc(end, name="end")
    if start_utc >= end_utc:
        return ()
    calendar_start = datetime.combine(start_utc.date() - timedelta(days=7), time.min)
    calendar_end = datetime.combine(end_utc.date() + timedelta(days=7), time.min)
    calendar = exchange_calendars.get_calendar(
        "XNYS",
        start=calendar_start,
        end=calendar_end,
    )
    sessions = calendar.sessions_in_range(start_utc.date(), end_utc.date())
    windows: list[tuple[datetime, datetime]] = []
    for session in sessions:
        session_open = calendar.session_open(session).to_pydatetime().astimezone(UTC)
        session_close = calendar.session_close(session).to_pydatetime().astimezone(UTC)
        window_start = max(start_utc, session_open)
        window_end = min(end_utc, session_close)
        if window_start < window_end:
            windows.append((window_start, window_end))
    return tuple(windows)


class CollectorService:
    """Own one collector lease, reconcile history, and persist a live IEX stream."""

    def __init__(
        self,
        repository: MarketDataRepository,
        historical_source: HistoricalBarSource,
        live_source: LiveBarSource | None = None,
        *,
        config: CollectorServiceConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        random_value: Callable[[], float] | None = None,
        holder_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.historical_source = historical_source
        self.live_source = live_source
        self.config = config or CollectorServiceConfig()
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._random = random_value or random.random
        self._holder_id = holder_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        )
        self._stop_event = threading.Event()
        self._lease_lock = threading.Lock()
        self._lease: LeaseToken | None = None
        self._fatal_lock = threading.Lock()
        self._fatal_error: BaseException | None = None
        self._counter_lock = threading.Lock()
        self._counters: defaultdict[str, int] = defaultdict(int)

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    @property
    def counters(self) -> dict[str, int]:
        with self._counter_lock:
            return dict(self._counters)

    def _count_result(self, result: BatchResult) -> None:
        with self._counter_lock:
            self._counters["observations_received"] += result.received
            self._counters["observations_inserted"] += result.observations_inserted
            self._counters["duplicates"] += result.duplicates
            self._counters["current_rows_inserted"] += result.current_rows_inserted
            self._counters["current_rows_revised"] += result.current_rows_revised
            self._counters["checkpoints_advanced"] += result.checkpoints_advanced

    def _lease_token(self) -> LeaseToken:
        with self._lease_lock:
            if self._lease is None:
                raise LeaseLostError("The collector does not hold its singleton lease")
            return self._lease

    def _acquire_lease(self) -> LeaseToken:
        token = self.repository.try_acquire_lease(
            lease_name=self.config.lease_name,
            holder_id=self._holder_id,
            ttl_seconds=self.config.lease_ttl_seconds,
        )
        if token is None:
            raise LeaseUnavailableError("Another market-data collector owns the singleton lease")
        with self._lease_lock:
            self._lease = token
        return token

    def _release_lease(self) -> None:
        with self._lease_lock:
            token = self._lease
            self._lease = None
        if token is not None:
            self.repository.release_lease(token)

    def _set_fatal(self, error: BaseException) -> None:
        with self._fatal_lock:
            if self._fatal_error is None:
                self._fatal_error = error
        self.request_stop()

    def _get_fatal(self) -> BaseException | None:
        with self._fatal_lock:
            return self._fatal_error

    def request_stop(self) -> None:
        """Request graceful shutdown; safe to call from a signal handler."""

        self._stop_event.set()

    def _finish_and_release(
        self,
        *,
        run_id: str,
        lease: LeaseToken,
        status: str,
        error: BaseException | None,
    ) -> None:
        """Finalize owned state without allowing cleanup to mask a primary failure."""

        cleanup_error: BaseException | None = None
        try:
            self.repository.finish_run(
                run_id,
                lease=lease,
                status=status,
                counters=self.counters,
                error=None if error is None else type(error).__name__,
            )
        except BaseException as exc:
            cleanup_error = exc
            LOGGER.error("Unable to finalize the ingestion run (%s)", type(exc).__name__)
        try:
            self._release_lease()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            LOGGER.error("Unable to release the collector lease (%s)", type(exc).__name__)
        if error is None and cleanup_error is not None:
            raise cleanup_error

    def _join_workers(self, workers: Sequence[threading.Thread]) -> None:
        """Wait for bounded worker I/O before terminal run state or lease release."""

        deadline = monotonic() + self.config.worker_shutdown_timeout_seconds
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - monotonic()))
        alive = sorted(worker.name for worker in workers if worker.is_alive())
        if alive:
            raise RuntimeError("Collector workers did not stop within the shutdown deadline")

    def _wait(self, seconds: float) -> bool:
        return self._stop_event.wait(max(0.0, seconds))

    def _record_event(
        self,
        event_type: str,
        *,
        run_id: str | None,
        severity: str = "info",
        details: dict[str, object] | None = None,
    ) -> None:
        self.repository.record_event(
            event_type=event_type,
            severity=severity,
            run_id=run_id,
            details=details,
            lease=self._lease_token(),
        )

    def _fetch_with_retry(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        source: str,
        run_id: str,
    ) -> tuple[RawBarObservationV1, ...]:
        delay = self.config.retry_initial_seconds
        for attempt in range(1, self.config.rest_max_attempts + 1):
            if self.stop_requested:
                raise InterruptedError("Collector shutdown requested")
            try:
                observations = self.historical_source.fetch(
                    symbols,
                    start=start,
                    end=end,
                    source=source,
                )
                if self.stop_requested:
                    raise InterruptedError("Collector shutdown requested")
                return observations
            except Exception as exc:
                if isinstance(exc, InterruptedError):
                    raise
                if not _is_retryable(exc):
                    raise
                if attempt >= self.config.rest_max_attempts:
                    raise
                jittered = min(
                    self.config.retry_max_seconds,
                    delay * (0.75 + (0.5 * self._random())),
                )
                provider_delay = getattr(exc, "retry_after_seconds", None)
                if (
                    isinstance(provider_delay, (int, float))
                    and not isinstance(provider_delay, bool)
                    and math.isfinite(float(provider_delay))
                ):
                    jittered = max(
                        jittered,
                        min(
                            self.config.provider_retry_max_seconds,
                            max(0.0, float(provider_delay)),
                        ),
                    )
                self._record_event(
                    "historical_request_retry",
                    run_id=run_id,
                    severity="warning",
                    details={
                        "attempt": attempt,
                        "retry_seconds": round(jittered, 3),
                        **_error_details(exc),
                    },
                )
                if self._wait(jittered):
                    raise InterruptedError("Collector shutdown requested") from exc
                delay = min(self.config.retry_max_seconds, delay * 2)
        raise AssertionError("unreachable")

    def _store_observations(
        self,
        observations: Sequence[RawBarObservationV1],
    ) -> None:
        size = self.config.database_batch_size
        for offset in range(0, len(observations), size):
            if self.stop_requested:
                raise InterruptedError("Collector shutdown requested")
            result = self.repository.append_batch(
                observations[offset : offset + size],
                lease=self._lease_token(),
            )
            self._count_result(result)

    def _coverage_advances(
        self,
        symbols: Sequence[str],
        *,
        committed_through: datetime,
        source: str,
        observations: Sequence[RawBarObservationV1],
    ) -> tuple[CoverageAdvance, ...]:
        latest: dict[str, RawBarObservationV1] = {}
        for observation in observations:
            current = latest.get(observation.bar.symbol)
            if current is None or observation.bar.bar_timestamp_utc > current.bar.bar_timestamp_utc:
                latest[observation.bar.symbol] = observation
        return tuple(
            CoverageAdvance(
                key=CheckpointKey(
                    checkpoint_name=self.config.checkpoint_name,
                    provider="alpaca",
                    feed="IEX",
                    adjustment="raw",
                    symbol=symbol,
                    timeframe="1m",
                ),
                committed_through_utc=committed_through,
                metadata={"source": source, "interval_semantics": "half-open"},
                last_bar_timestamp_utc=(
                    None if symbol not in latest else latest[symbol].bar.bar_timestamp_utc
                ),
                last_observation_id=(
                    None if symbol not in latest else latest[symbol].observation_id
                ),
            )
            for symbol in symbols
        )

    def _backfill_range(
        self,
        *,
        start: datetime,
        end: datetime,
        source: str,
        run_id: str,
    ) -> None:
        start = _as_utc(start, name="start")
        end = _as_utc(end, name="end")
        if start >= end:
            raise ValueError("historical collection start must precede end")
        symbols = COLLECTION_UNIVERSE_V1.symbols
        windows = _session_windows(start, end)
        for window_start, window_end in windows:
            for offset in range(0, len(symbols), self.config.api_symbol_batch_size):
                if self.stop_requested:
                    raise InterruptedError("Collector shutdown requested")
                requested = symbols[offset : offset + self.config.api_symbol_batch_size]
                observations = self._fetch_with_retry(
                    requested,
                    start=window_start,
                    end=window_end,
                    source=source,
                    run_id=run_id,
                )
                self._store_observations(observations)
                if self.stop_requested:
                    raise InterruptedError("Collector shutdown requested")
                result = self.repository.append_batch(
                    (),
                    coverage_advances=self._coverage_advances(
                        requested,
                        committed_through=window_end,
                        source=source,
                        observations=observations,
                    ),
                    lease=self._lease_token(),
                )
                self._count_result(result)
                if self.stop_requested:
                    raise InterruptedError("Collector shutdown requested")
                self._record_event(
                    "historical_window_committed",
                    run_id=run_id,
                    details={
                        "source": source,
                        "start": window_start.isoformat(),
                        "end": window_end.isoformat(),
                        "symbol_count": len(requested),
                        "observation_count": len(observations),
                    },
                )
        if not windows or windows[-1][1] < end:
            if self.stop_requested:
                raise InterruptedError("Collector shutdown requested")
            result = self.repository.append_batch(
                (),
                coverage_advances=self._coverage_advances(
                    symbols,
                    committed_through=end,
                    source="exchange_calendar_non_trading_interval",
                    observations=(),
                ),
                lease=self._lease_token(),
            )
            self._count_result(result)

    def _resume_start(
        self,
        start_if_empty: datetime | None,
        checkpoints: dict[str, Checkpoint],
    ) -> datetime:
        missing = [
            symbol
            for symbol in COLLECTION_UNIVERSE_V1.symbols
            if symbol not in checkpoints or checkpoints[symbol].committed_through_utc is None
        ]
        if missing and start_if_empty is None:
            raise ValueError(
                "The database has no complete coverage checkpoint; provide a first-run history start"
            )
        starts: list[datetime] = []
        overlap = timedelta(minutes=self.config.reconciliation_overlap_minutes)
        for symbol in COLLECTION_UNIVERSE_V1.symbols:
            checkpoint = checkpoints.get(symbol)
            if checkpoint is None or checkpoint.committed_through_utc is None:
                assert start_if_empty is not None
                starts.append(_as_utc(start_if_empty, name="start_if_empty"))
            else:
                starts.append(checkpoint.committed_through_utc - overlap)
        return min(starts)

    def _reconcile_once(self, *, run_id: str, start_if_empty: datetime | None) -> None:
        end = completed_bar_cutoff(
            self._clock(),
            lag_minutes=self.config.completed_bar_lag_minutes,
        )
        checkpoints = self.repository.checkpoints(checkpoint_name=self.config.checkpoint_name)
        future_symbols = sorted(
            symbol
            for symbol, checkpoint in checkpoints.items()
            if checkpoint.committed_through_utc is not None
            and checkpoint.committed_through_utc > end
        )
        if future_symbols:
            raise ValueError("A coverage checkpoint is ahead of the completed-bar cutoff")
        start = self._resume_start(start_if_empty, checkpoints)
        has_missing_checkpoint = any(
            symbol not in checkpoints or checkpoints[symbol].committed_through_utc is None
            for symbol in COLLECTION_UNIVERSE_V1.symbols
        )
        if has_missing_checkpoint and start >= end:
            raise ValueError("The first-run history start must precede the completed-bar cutoff")
        if start < end:
            self._backfill_range(
                start=start,
                end=end,
                source=("historical_backfill" if not checkpoints else "historical_reconciliation"),
                run_id=run_id,
            )

    def _renew_lease_loop(self, run_id: str) -> None:
        while not self._wait(self.config.lease_renew_interval_seconds):
            try:
                renewed = self.repository.renew_lease(
                    self._lease_token(),
                    ttl_seconds=self.config.lease_ttl_seconds,
                )
                with self._lease_lock:
                    self._lease = renewed
            except Exception as exc:
                with suppress(Exception):
                    self._record_event(
                        "collector_lease_lost",
                        run_id=run_id,
                        severity="critical",
                        details={"error_type": type(exc).__name__},
                    )
                self._set_fatal(exc)
                return

    def _reconciliation_loop(self, run_id: str) -> None:
        while not self._wait(self.config.reconciliation_interval_seconds):
            try:
                self._reconcile_once(run_id=run_id, start_if_empty=None)
            except InterruptedError:
                return
            except Exception as exc:
                try:
                    self._record_event(
                        "historical_reconciliation_failed",
                        run_id=run_id,
                        severity="error",
                        details=_error_details(exc),
                    )
                except Exception as event_error:
                    self._set_fatal(event_error)
                    return
                if not _is_retryable(exc):
                    self._set_fatal(exc)
                    return

    def _live_observation(self, observation: RawBarObservationV1) -> None:
        if self.stop_requested:
            return
        try:
            result = self.repository.append_batch(
                (observation,),
                lease=self._lease_token(),
            )
            self._count_result(result)
        except Exception as exc:
            self._set_fatal(exc)
            raise

    def backfill(self, *, start: datetime, end: datetime | None = None) -> dict[str, int]:
        """Run a finite, lease-protected historical backfill."""

        self.repository.verify_schema()
        self.repository.register_universe()
        lease = self._acquire_lease()
        try:
            run_id = self.repository.start_run(mode="backfill", lease=lease)
        except BaseException:
            self._release_lease()
            raise
        renew_thread = threading.Thread(
            target=self._renew_lease_loop,
            args=(run_id,),
            name="collector-lease-renewal",
            daemon=True,
        )
        renew_thread.start()
        status = "completed"
        error: BaseException | None = None
        try:
            safe_boundary = completed_bar_cutoff(
                self._clock(),
                lag_minutes=self.config.completed_bar_lag_minutes,
            )
            boundary = end or safe_boundary
            if _as_utc(boundary, name="end") > safe_boundary:
                raise ValueError("Historical collection end exceeds the completed-bar cutoff")
            self._backfill_range(
                start=_as_utc(start, name="start"),
                end=_as_utc(boundary, name="end"),
                source="historical_backfill",
                run_id=run_id,
            )
            fatal = self._get_fatal()
            if fatal is not None:
                raise fatal
            return self.counters
        except BaseException as exc:
            effective_error = self._get_fatal() or exc
            status = (
                "stopped"
                if isinstance(effective_error, (InterruptedError, KeyboardInterrupt))
                else "failed"
            )
            error = effective_error
            if effective_error is not exc:
                raise effective_error from exc
            raise
        finally:
            self._stop_event.set()
            try:
                self._join_workers((renew_thread,))
            except BaseException as exc:
                if error is None:
                    raise
                LOGGER.error("Unable to stop collector workers cleanly (%s)", type(exc).__name__)
            else:
                self._finish_and_release(
                    run_id=run_id,
                    lease=lease,
                    status=status,
                    error=error,
                )

    def run(self, *, start_if_empty: datetime | None = None) -> dict[str, int]:
        """Catch up history, then retain and reconcile the live IEX minute stream."""

        if self.live_source is None:
            raise ValueError("run mode requires a live market-data source")
        self.repository.verify_schema()
        self.repository.register_universe()
        lease = self._acquire_lease()
        try:
            run_id = self.repository.start_run(mode="run", lease=lease)
        except BaseException:
            self._release_lease()
            raise
        renew_thread = threading.Thread(
            target=self._renew_lease_loop,
            args=(run_id,),
            name="collector-lease-renewal",
            daemon=True,
        )
        renew_thread.start()
        reconciliation_thread: threading.Thread | None = None
        status = "stopped"
        error: BaseException | None = None
        try:
            self._reconcile_once(run_id=run_id, start_if_empty=start_if_empty)
            reconciliation_thread = threading.Thread(
                target=self._reconciliation_loop,
                args=(run_id,),
                name="collector-rest-reconciliation",
                daemon=True,
            )
            reconciliation_thread.start()
            delay = self.config.stream_restart_initial_seconds
            while not self.stop_requested:
                try:
                    self._record_event("market_data_stream_starting", run_id=run_id)
                    self.live_source.run(
                        COLLECTION_UNIVERSE_V1.symbols,
                        self._live_observation,
                        stop_requested=lambda: self.stop_requested,
                    )
                    if not self.stop_requested:
                        raise RuntimeError("Alpaca market-data stream stopped unexpectedly")
                except Exception as exc:
                    if self.stop_requested:
                        break
                    if not _is_retryable(exc):
                        self._record_event(
                            "market_data_stream_failed",
                            run_id=run_id,
                            severity="error",
                            details=_error_details(exc),
                        )
                        raise
                    self._record_event(
                        "market_data_stream_restart",
                        run_id=run_id,
                        severity="warning",
                        details=_error_details(exc),
                    )
                    jittered = min(
                        self.config.stream_restart_max_seconds,
                        delay * (0.75 + (0.5 * self._random())),
                    )
                    if self._wait(jittered):
                        break
                    delay = min(self.config.stream_restart_max_seconds, delay * 2)
            fatal = self._get_fatal()
            if fatal is not None:
                raise fatal
            return self.counters
        except BaseException as exc:
            status = (
                "stopped" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else "failed"
            )
            error = exc
            raise
        finally:
            self._stop_event.set()
            with suppress(Exception):
                self.live_source.stop()
            workers = [renew_thread]
            if reconciliation_thread is not None:
                workers.append(reconciliation_thread)
            try:
                self._join_workers(workers)
            except BaseException as exc:
                if error is None:
                    raise
                LOGGER.error("Unable to stop collector workers cleanly (%s)", type(exc).__name__)
            else:
                self._finish_and_release(
                    run_id=run_id,
                    lease=lease,
                    status=status,
                    error=error,
                )
