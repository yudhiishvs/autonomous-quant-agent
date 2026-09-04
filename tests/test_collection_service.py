"""Offline orchestration tests for the standalone market-data collector."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from adaptive_trader.collection.contracts import MarketBarV1, RawBarObservationV1
from adaptive_trader.collection.repository import (
    BatchResult,
    Checkpoint,
    CheckpointKey,
    CollectorStatus,
    CoverageAdvance,
    LeaseLostError,
    LeaseToken,
    LeaseUnavailableError,
)
from adaptive_trader.collection.service import (
    CollectorService,
    CollectorServiceConfig,
    _session_windows,
    completed_bar_cutoff,
)
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1

NEW_YORK = ZoneInfo("America/New_York")
SESSION_OPEN = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 3, 20, 5, 47, tzinfo=UTC)


class PermanentDataError(RuntimeError):
    retryable = False
    provider_status = 401


class ThrottledDataError(RuntimeError):
    retryable = True
    provider_status = 429
    retry_after_seconds = 47.0


def _checkpoint_key(symbol: str) -> CheckpointKey:
    return CheckpointKey(
        checkpoint_name="rest_coverage",
        provider="alpaca",
        feed="IEX",
        adjustment="raw",
        symbol=symbol,
        timeframe="1m",
    )


def _observation(
    symbol: str = "AAPL",
    *,
    timestamp: datetime = datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
    close: str = "101.50",
    source: str = "historical_backfill",
) -> RawBarObservationV1:
    return RawBarObservationV1(
        bar=MarketBarV1(
            provider="alpaca",
            feed="IEX",
            adjustment="raw",
            symbol=symbol,
            timeframe="1m",
            bar_timestamp_utc=timestamp,
            provider_event_timestamp_utc=timestamp,
            receipt_timestamp_utc=timestamp + timedelta(minutes=2),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal(close),
            volume=1000,
            trade_count=20,
            vwap=Decimal("100.75"),
            source=source,
        )
    )


class FakeRepository:
    """Thread-safe in-memory implementation of the collector repository protocol."""

    def __init__(self, *, lease_available: bool = True) -> None:
        self.lease_available = lease_available
        self.schema_verified = False
        self.universe_registered = False
        self.observations: dict[str, RawBarObservationV1] = {}
        self.current_content: dict[str, str] = {}
        self.checkpoint_rows: dict[str, Checkpoint] = {}
        self.events: list[dict[str, object]] = []
        self.started_runs: list[tuple[str, str, LeaseToken]] = []
        self.finished_runs: list[dict[str, object]] = []
        self.append_calls: list[
            tuple[tuple[RawBarObservationV1, ...], tuple[CoverageAdvance, ...]]
        ] = []
        self.renew_calls = 0
        self.release_calls: list[LeaseToken] = []
        self._lease: LeaseToken | None = None
        self._fencing_token = 0
        self._lock = threading.RLock()

    def verify_schema(self) -> None:
        self.schema_verified = True

    def register_universe(self) -> None:
        self.universe_registered = True

    def start_run(self, *, mode: str, lease: LeaseToken) -> str:
        self._verify_lease(lease)
        run_id = f"run-{len(self.started_runs) + 1}"
        self.started_runs.append((run_id, mode, lease))
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        lease: LeaseToken,
        status: str,
        counters: Mapping[str, int],
        error: str | None = None,
    ) -> None:
        self._verify_lease(lease)
        self.finished_runs.append(
            {
                "run_id": run_id,
                "status": status,
                "counters": dict(counters),
                "error": error,
            }
        )

    def _verify_lease(self, lease: LeaseToken) -> None:
        if lease is None or self._lease is None:
            raise LeaseLostError("missing fake lease")
        if (
            lease.lease_name,
            lease.holder_id,
            lease.fencing_token,
        ) != (
            self._lease.lease_name,
            self._lease.holder_id,
            self._lease.fencing_token,
        ):
            raise LeaseLostError("stale fake fencing token")

    def append_batch(
        self,
        observations: Sequence[RawBarObservationV1],
        *,
        lease: LeaseToken,
        coverage_advances: Sequence[CoverageAdvance] = (),
    ) -> BatchResult:
        with self._lock:
            self._verify_lease(lease)
            observation_tuple = tuple(observations)
            advance_tuple = tuple(coverage_advances)
            self.append_calls.append((observation_tuple, advance_tuple))
            inserted = 0
            duplicates = 0
            current_inserted = 0
            current_revised = 0
            for observation in observation_tuple:
                if observation.observation_id in self.observations:
                    duplicates += 1
                    continue
                self.observations[observation.observation_id] = observation
                inserted += 1
                prior_content = self.current_content.get(observation.identity_hash)
                if prior_content is None:
                    current_inserted += 1
                elif prior_content != observation.content_hash:
                    current_revised += 1
                self.current_content[observation.identity_hash] = observation.content_hash

            for advance in advance_tuple:
                prior = self.checkpoint_rows.get(advance.key.symbol)
                committed = advance.committed_through_utc
                if prior is not None and prior.committed_through_utc is not None:
                    committed = max(committed, prior.committed_through_utc)
                self.checkpoint_rows[advance.key.symbol] = Checkpoint(
                    key=advance.key,
                    committed_through_utc=committed,
                    last_bar_timestamp_utc=(
                        prior.last_bar_timestamp_utc
                        if advance.last_bar_timestamp_utc is None and prior is not None
                        else advance.last_bar_timestamp_utc
                    ),
                    last_observation_id=(
                        prior.last_observation_id
                        if advance.last_observation_id is None and prior is not None
                        else advance.last_observation_id
                    ),
                    version=1 if prior is None else prior.version + 1,
                    updated_at=NOW,
                )

            return BatchResult(
                received=len(observation_tuple),
                observations_inserted=inserted,
                duplicates=duplicates,
                current_rows_inserted=current_inserted,
                current_rows_revised=current_revised,
                checkpoints_advanced=len(advance_tuple),
            )

    def checkpoints(self, *, checkpoint_name: str) -> dict[str, Checkpoint]:
        return {
            symbol: checkpoint
            for symbol, checkpoint in self.checkpoint_rows.items()
            if checkpoint.key.checkpoint_name == checkpoint_name
        }

    def seed_coverage(self, committed_through: datetime) -> None:
        for symbol in COLLECTION_UNIVERSE_V1.symbols:
            key = _checkpoint_key(symbol)
            self.checkpoint_rows[symbol] = Checkpoint(
                key=key,
                committed_through_utc=committed_through,
                last_bar_timestamp_utc=None,
                last_observation_id=None,
                version=1,
                updated_at=NOW,
            )

    def try_acquire_lease(
        self,
        *,
        lease_name: str,
        holder_id: str,
        ttl_seconds: int,
    ) -> LeaseToken | None:
        with self._lock:
            if not self.lease_available or self._lease is not None:
                return None
            self._fencing_token += 1
            self._lease = LeaseToken(
                lease_name=lease_name,
                holder_id=holder_id,
                fencing_token=self._fencing_token,
                expires_at=NOW + timedelta(seconds=ttl_seconds),
            )
            return self._lease

    def renew_lease(self, token: LeaseToken, *, ttl_seconds: int) -> LeaseToken:
        with self._lock:
            self._verify_lease(token)
            self.renew_calls += 1
            self._lease = LeaseToken(
                lease_name=token.lease_name,
                holder_id=token.holder_id,
                fencing_token=token.fencing_token,
                expires_at=token.expires_at + timedelta(seconds=ttl_seconds),
            )
            return self._lease

    def release_lease(self, token: LeaseToken) -> bool:
        with self._lock:
            self._verify_lease(token)
            self.release_calls.append(token)
            self._lease = None
            return True

    def record_event(
        self,
        *,
        event_type: str,
        lease: LeaseToken,
        severity: str = "info",
        run_id: str | None = None,
        symbol: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        with self._lock:
            self._verify_lease(lease)
            event_id = f"event-{len(self.events) + 1}"
            self.events.append(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "severity": severity,
                    "run_id": run_id,
                    "symbol": symbol,
                    "details": dict(details or {}),
                }
            )
            return event_id

    def status(self) -> CollectorStatus:
        return CollectorStatus(
            current_bar_count=len(self.current_content),
            observation_count=len(self.observations),
            checkpoint_count=len(self.checkpoint_rows),
            open_gap_count=0,
            running_run_count=0,
            latest_receipt_timestamp_utc=None,
        )

    def is_ready(self, *, lease_name: str) -> bool:
        return self._lease is not None and self._lease.lease_name == lease_name

    def close(self) -> None:
        return None


class FakeHistoricalSource:
    def __init__(
        self,
        observations: Sequence[RawBarObservationV1] = (),
        *,
        failures: int = 0,
    ) -> None:
        self.observations = tuple(observations)
        self.failures = failures
        self.calls: list[dict[str, object]] = []

    def fetch(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        source: str = "historical_backfill",
    ) -> tuple[RawBarObservationV1, ...]:
        self.calls.append(
            {
                "symbols": tuple(symbols),
                "start": start,
                "end": end,
                "source": source,
            }
        )
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("synthetic transient historical failure")
        requested = frozenset(symbols)
        return tuple(
            observation
            for observation in self.observations
            if observation.bar.symbol in requested
            and start <= observation.bar.bar_timestamp_utc < end
        )


class FakeLiveSource:
    def __init__(
        self,
        *,
        observation: RawBarObservationV1 | None = None,
        fail_first: bool = False,
    ) -> None:
        self.observation = observation
        self.fail_first = fail_first
        self.run_calls: list[tuple[str, ...]] = []
        self.stop_calls = 0
        self.after_success: Callable[[], None] | None = None

    def run(
        self,
        symbols: Sequence[str],
        handler: Callable[[RawBarObservationV1], None],
        *,
        stop_requested: Callable[[], bool],
    ) -> None:
        self.run_calls.append(tuple(symbols))
        if self.fail_first and len(self.run_calls) == 1:
            raise RuntimeError("synthetic stream disconnect")
        if self.observation is not None:
            handler(self.observation)
        if self.after_success is not None:
            self.after_success()

    def stop(self) -> None:
        self.stop_calls += 1


class BlockingLiveSource(FakeLiveSource):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(
        self,
        symbols: Sequence[str],
        handler: Callable[[RawBarObservationV1], None],
        *,
        stop_requested: Callable[[], bool],
    ) -> None:
        del handler
        self.run_calls.append(tuple(symbols))
        self.started.set()
        while not stop_requested():
            if self.release.wait(timeout=0.01):
                break

    def stop(self) -> None:
        super().stop()
        self.release.set()


def _service_config(**overrides: object) -> CollectorServiceConfig:
    values: dict[str, object] = {
        "lease_ttl_seconds": 90,
        "lease_renew_interval_seconds": 30,
        "reconciliation_interval_seconds": 60,
        "stream_restart_initial_seconds": 0.000001,
        "stream_restart_max_seconds": 0.000001,
    }
    values.update(overrides)
    return CollectorServiceConfig(**values)  # type: ignore[arg-type]


def test_lease_renewal_loop_renews_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()
    service = CollectorService(
        repository,
        FakeHistoricalSource(),
        holder_id="collector-one",
    )
    service._acquire_lease()
    wait_calls = 0

    def stop_after_one_interval(seconds: float) -> bool:
        nonlocal wait_calls
        assert seconds == service.config.lease_renew_interval_seconds
        wait_calls += 1
        return wait_calls > 1

    monkeypatch.setattr(service, "_wait", stop_after_one_interval)

    service._renew_lease_loop("run-1")
    service._release_lease()

    assert repository.renew_calls == 1
    assert len(repository.release_calls) == 1


def test_completed_bar_cutoff_is_exclusive_minute_boundary_in_utc() -> None:
    now_et = datetime(2026, 9, 3, 16, 5, 47, 991, tzinfo=NEW_YORK)

    assert completed_bar_cutoff(now_et, lag_minutes=1) == datetime(2026, 9, 3, 20, 5, tzinfo=UTC)
    assert completed_bar_cutoff(now_et, lag_minutes=2) == datetime(2026, 9, 3, 20, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        completed_bar_cutoff(datetime(2026, 9, 3, 20, 5), lag_minutes=2)


def test_session_windows_observe_weekends_and_xnys_early_close() -> None:
    early_close = _session_windows(
        datetime(2026, 11, 27, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 28, 0, 0, tzinfo=UTC),
    )
    weekend = _session_windows(
        datetime(2026, 11, 28, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 30, 0, 0, tzinfo=UTC),
    )

    assert early_close == (
        (
            datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
            datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        ),
    )
    assert weekend == ()


def test_first_run_requires_explicit_history_start_and_releases_lease() -> None:
    repository = FakeRepository()
    historical = FakeHistoricalSource()
    live = FakeLiveSource()
    service = CollectorService(
        repository,
        historical,
        live,
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    with pytest.raises(ValueError, match="provide a first-run history start"):
        service.run()

    assert live.run_calls == []
    assert repository.finished_runs[-1]["status"] == "failed"
    assert repository.finished_runs[-1]["error"] == "ValueError"
    assert len(repository.release_calls) == 1


def test_first_run_start_must_precede_the_completed_bar_cutoff() -> None:
    repository = FakeRepository()
    historical = FakeHistoricalSource()
    live = FakeLiveSource()
    service = CollectorService(
        repository,
        historical,
        live,
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    with pytest.raises(ValueError, match="must precede the completed-bar cutoff"):
        service.run(start_if_empty=NOW + timedelta(minutes=1))

    assert historical.calls == []
    assert live.run_calls == []
    assert repository.finished_runs[-1]["status"] == "failed"
    assert len(repository.release_calls) == 1


def test_backfill_rejects_an_end_beyond_the_completed_bar_cutoff() -> None:
    repository = FakeRepository()
    historical = FakeHistoricalSource()
    service = CollectorService(
        repository,
        historical,
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    with pytest.raises(ValueError, match="end exceeds"):
        service.backfill(start=SESSION_OPEN, end=NOW + timedelta(minutes=1))

    assert historical.calls == []
    assert repository.finished_runs[-1]["status"] == "failed"
    assert len(repository.release_calls) == 1


def test_backfill_requests_all_29_symbols_and_commits_exact_rest_coverage() -> None:
    repository = FakeRepository()
    historical = FakeHistoricalSource((_observation(),))
    service = CollectorService(
        repository,
        historical,
        config=_service_config(),
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    counters = service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(COLLECTION_UNIVERSE_V1.symbols) == 29
    assert historical.calls == [
        {
            "symbols": COLLECTION_UNIVERSE_V1.symbols,
            "start": SESSION_OPEN,
            "end": SESSION_CLOSE,
            "source": "historical_backfill",
        }
    ]
    assert set(repository.checkpoint_rows) == set(COLLECTION_UNIVERSE_V1.symbols)
    assert all(
        checkpoint.key.checkpoint_name == "rest_coverage"
        and checkpoint.committed_through_utc == SESSION_CLOSE
        for checkpoint in repository.checkpoint_rows.values()
    )
    assert repository.checkpoint_rows["AAPL"].last_bar_timestamp_utc == datetime(
        2026, 9, 3, 15, 0, tzinfo=UTC
    )
    assert repository.checkpoint_rows["AAPL"].last_observation_id == next(
        iter(repository.observations)
    )
    assert repository.checkpoint_rows["TSLA"].last_bar_timestamp_utc is None
    assert counters["observations_inserted"] == 1
    assert counters["checkpoints_advanced"] == 29
    assert repository.finished_runs[-1]["status"] == "completed"
    assert len(repository.release_calls) == 1


def test_successful_empty_rest_response_advances_every_symbol() -> None:
    repository = FakeRepository()
    historical = FakeHistoricalSource()
    service = CollectorService(
        repository,
        historical,
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    counters = service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(repository.observations) == 0
    assert len(repository.checkpoint_rows) == 29
    assert counters["observations_received"] == 0
    assert counters["checkpoints_advanced"] == 29
    coverage_call = repository.append_calls[-1]
    assert coverage_call[0] == ()
    assert len(coverage_call[1]) == 29


def test_historical_retry_uses_capped_jitter_without_real_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()
    historical = FakeHistoricalSource((_observation(),), failures=2)
    service = CollectorService(
        repository,
        historical,
        config=_service_config(
            rest_max_attempts=3,
            retry_initial_seconds=2.0,
            retry_max_seconds=3.0,
        ),
        clock=lambda: NOW,
        random_value=lambda: 0.5,
        holder_id="collector-one",
    )
    waits: list[float] = []
    original_wait = service._wait

    def wait_without_retry_sleep(seconds: float) -> bool:
        if threading.current_thread() is threading.main_thread():
            waits.append(seconds)
            return False
        return original_wait(seconds)

    monkeypatch.setattr(service, "_wait", wait_without_retry_sleep)

    service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(historical.calls) == 3
    assert waits == [2.0, 3.0]
    retries = [
        event for event in repository.events if event["event_type"] == "historical_request_retry"
    ]
    assert [event["details"]["attempt"] for event in retries] == [1, 2]  # type: ignore[index]


def test_historical_retry_respects_bounded_provider_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ThrottledHistoricalSource(FakeHistoricalSource):
        def fetch(
            self,
            symbols: Sequence[str],
            *,
            start: datetime,
            end: datetime,
            source: str = "historical_backfill",
        ) -> tuple[RawBarObservationV1, ...]:
            if not self.calls:
                self.calls.append({"symbols": tuple(symbols)})
                raise ThrottledDataError("synthetic rate limit")
            return super().fetch(symbols, start=start, end=end, source=source)

    repository = FakeRepository()
    historical = ThrottledHistoricalSource()
    service = CollectorService(
        repository,
        historical,
        config=_service_config(retry_initial_seconds=1.0, retry_max_seconds=3.0),
        clock=lambda: NOW,
        random_value=lambda: 0.5,
        holder_id="collector-one",
    )
    waits: list[float] = []
    original_wait = service._wait

    def wait_without_retry_sleep(seconds: float) -> bool:
        if threading.current_thread() is threading.main_thread():
            waits.append(seconds)
            return False
        return original_wait(seconds)

    monkeypatch.setattr(service, "_wait", wait_without_retry_sleep)

    service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert waits == [47.0]
    retry = next(
        event for event in repository.events if event["event_type"] == "historical_request_retry"
    )
    assert retry["details"] == {
        "attempt": 1,
        "retry_seconds": 47.0,
        "error_type": "ThrottledDataError",
        "retryable": True,
        "provider_status": 429,
        "retry_after_seconds": 47.0,
    }


def test_permanent_historical_failure_is_not_retried() -> None:
    class PermanentHistoricalSource(FakeHistoricalSource):
        def fetch(
            self,
            symbols: Sequence[str],
            *,
            start: datetime,
            end: datetime,
            source: str = "historical_backfill",
        ) -> tuple[RawBarObservationV1, ...]:
            self.calls.append(
                {
                    "symbols": tuple(symbols),
                    "start": start,
                    "end": end,
                    "source": source,
                }
            )
            raise PermanentDataError("synthetic permanent data failure")

    repository = FakeRepository()
    historical = PermanentHistoricalSource()
    service = CollectorService(
        repository,
        historical,
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    with pytest.raises(PermanentDataError):
        service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(historical.calls) == 1
    assert not any(event["event_type"] == "historical_request_retry" for event in repository.events)
    assert repository.finished_runs[-1]["status"] == "failed"


def test_duplicate_safe_restart_replays_without_duplicate_observation() -> None:
    repository = FakeRepository()
    observation = _observation()

    first = CollectorService(
        repository,
        FakeHistoricalSource((observation,)),
        clock=lambda: NOW,
        holder_id="collector-one",
    )
    first.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)
    second = CollectorService(
        repository,
        FakeHistoricalSource((observation,)),
        clock=lambda: NOW,
        holder_id="collector-two",
    )

    second_counters = second.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(repository.observations) == 1
    assert len(repository.current_content) == 1
    assert second_counters["observations_inserted"] == 0
    assert second_counters["duplicates"] == 1
    assert len(repository.release_calls) == 2


def test_second_collector_fails_closed_when_singleton_lease_is_unavailable() -> None:
    repository = FakeRepository(lease_available=False)
    service = CollectorService(repository, FakeHistoricalSource(), holder_id="collector-two")

    with pytest.raises(LeaseUnavailableError, match="Another market-data collector"):
        service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert repository.started_runs == []
    assert repository.release_calls == []
    assert repository.observations == {}


def test_run_start_failure_releases_the_acquired_lease() -> None:
    class FailingRunRepository(FakeRepository):
        def start_run(self, *, mode: str, lease: LeaseToken) -> str:
            del mode, lease
            raise RuntimeError("synthetic run-store failure")

    repository = FailingRunRepository()
    service = CollectorService(repository, FakeHistoricalSource(), holder_id="collector-one")

    with pytest.raises(RuntimeError, match="run-store failure"):
        service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(repository.release_calls) == 1


def test_backfill_finish_failure_still_releases_the_lease() -> None:
    class FailingFinishRepository(FakeRepository):
        def finish_run(
            self,
            run_id: str,
            *,
            lease: LeaseToken,
            status: str,
            counters: Mapping[str, int],
            error: str | None = None,
        ) -> None:
            del run_id, lease, status, counters, error
            raise RuntimeError("synthetic finish-store failure")

    repository = FailingFinishRepository()
    service = CollectorService(
        repository,
        FakeHistoricalSource(),
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    with pytest.raises(RuntimeError, match="finish-store failure"):
        service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(repository.release_calls) == 1


def test_finish_failure_does_not_mask_the_primary_collection_failure() -> None:
    class FailingFinishRepository(FakeRepository):
        def finish_run(
            self,
            run_id: str,
            *,
            lease: LeaseToken,
            status: str,
            counters: Mapping[str, int],
            error: str | None = None,
        ) -> None:
            del run_id, lease, status, counters, error
            raise RuntimeError("synthetic finish-store failure")

    class FailingHistoricalSource(FakeHistoricalSource):
        def fetch(
            self,
            symbols: Sequence[str],
            *,
            start: datetime,
            end: datetime,
            source: str = "historical_backfill",
        ) -> tuple[RawBarObservationV1, ...]:
            del symbols, start, end, source
            raise PermanentDataError("primary collection failure")

    repository = FailingFinishRepository()
    service = CollectorService(
        repository,
        FailingHistoricalSource(),
        clock=lambda: NOW,
        holder_id="collector-one",
    )

    with pytest.raises(PermanentDataError, match="primary collection failure"):
        service.backfill(start=SESSION_OPEN, end=SESSION_CLOSE)

    assert len(repository.release_calls) == 1


def test_reconciliation_fails_closed_when_its_failure_event_cannot_be_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEventRepository(FakeRepository):
        def record_event(
            self,
            *,
            event_type: str,
            lease: LeaseToken,
            severity: str = "info",
            run_id: str | None = None,
            symbol: str | None = None,
            details: Mapping[str, Any] | None = None,
        ) -> str:
            del event_type, severity, run_id, symbol, details, lease
            raise RuntimeError("synthetic event-store failure")

    repository = FailingEventRepository()
    live = FakeLiveSource()
    service = CollectorService(repository, FakeHistoricalSource(), live, holder_id="collector-one")
    service._acquire_lease()

    def fail_reconciliation(**kwargs: object) -> None:
        del kwargs
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(service, "_reconcile_once", fail_reconciliation)
    monkeypatch.setattr(service, "_wait", lambda _seconds: False)

    service._reconciliation_loop("run-1")

    assert service.stop_requested is True
    assert isinstance(service._get_fatal(), RuntimeError)
    assert live.stop_calls == 0
    service._release_lease()


def test_permanent_reconciliation_failure_stops_the_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()
    live = FakeLiveSource()
    service = CollectorService(repository, FakeHistoricalSource(), live, holder_id="collector-one")
    service._acquire_lease()

    def fail_reconciliation(**kwargs: object) -> None:
        del kwargs
        raise PermanentDataError("synthetic permanent reconciliation failure")

    monkeypatch.setattr(service, "_reconcile_once", fail_reconciliation)
    monkeypatch.setattr(service, "_wait", lambda _seconds: False)

    service._reconciliation_loop("run-1")

    assert isinstance(service._get_fatal(), PermanentDataError)
    assert service.stop_requested is True
    assert live.stop_calls == 0
    event = next(
        item
        for item in repository.events
        if item["event_type"] == "historical_reconciliation_failed"
    )
    assert event["details"] == {
        "error_type": "PermanentDataError",
        "retryable": False,
        "provider_status": 401,
    }
    service._release_lease()


def test_live_observation_is_persisted_and_stream_restarts_with_overlap() -> None:
    repository = FakeRepository()
    repository.seed_coverage(datetime(2026, 9, 3, 15, 0, tzinfo=UTC))
    historical = FakeHistoricalSource()
    live_observation = _observation(
        timestamp=datetime(2026, 9, 3, 15, 8, tzinfo=UTC),
        source="iex_bar",
    )
    live = FakeLiveSource(observation=live_observation, fail_first=True)
    service = CollectorService(
        repository,
        historical,
        live,
        config=_service_config(completed_bar_lag_minutes=2),
        clock=lambda: datetime(2026, 9, 3, 15, 10, 30, tzinfo=UTC),
        random_value=lambda: 0.5,
        holder_id="collector-one",
    )
    live.after_success = service.request_stop

    counters = service.run()

    assert historical.calls[0] == {
        "symbols": COLLECTION_UNIVERSE_V1.symbols,
        "start": datetime(2026, 9, 3, 14, 55, tzinfo=UTC),
        "end": datetime(2026, 9, 3, 15, 9, tzinfo=UTC),
        "source": "historical_reconciliation",
    }
    assert live.run_calls == [COLLECTION_UNIVERSE_V1.symbols, COLLECTION_UNIVERSE_V1.symbols]
    assert live_observation.observation_id in repository.observations
    assert counters["observations_inserted"] == 1
    event_types = [str(event["event_type"]) for event in repository.events]
    assert event_types.count("market_data_stream_starting") == 2
    assert event_types.count("market_data_stream_restart") == 1
    assert repository.finished_runs[-1]["status"] == "stopped"
    assert len(repository.release_calls) == 1


def test_permanent_stream_failure_stops_without_restart() -> None:
    class PermanentLiveSource(FakeLiveSource):
        def run(
            self,
            symbols: Sequence[str],
            handler: Callable[[RawBarObservationV1], None],
            *,
            stop_requested: Callable[[], bool],
        ) -> None:
            del handler, stop_requested
            self.run_calls.append(tuple(symbols))
            raise PermanentDataError("synthetic permanent stream failure")

    repository = FakeRepository()
    repository.seed_coverage(datetime(2026, 9, 3, 15, 0, tzinfo=UTC))
    live = PermanentLiveSource()
    service = CollectorService(
        repository,
        FakeHistoricalSource(),
        live,
        config=_service_config(completed_bar_lag_minutes=2),
        clock=lambda: datetime(2026, 9, 3, 15, 10, 30, tzinfo=UTC),
        holder_id="collector-one",
    )

    with pytest.raises(PermanentDataError):
        service.run()

    assert live.run_calls == [COLLECTION_UNIVERSE_V1.symbols]
    event = next(
        item for item in repository.events if item["event_type"] == "market_data_stream_failed"
    )
    assert event["details"] == {
        "error_type": "PermanentDataError",
        "retryable": False,
        "provider_status": 401,
    }
    assert repository.finished_runs[-1]["status"] == "failed"


def test_request_stop_unblocks_live_source_finishes_run_and_releases_lease() -> None:
    repository = FakeRepository()
    repository.seed_coverage(SESSION_CLOSE)
    live = BlockingLiveSource()
    service = CollectorService(
        repository,
        FakeHistoricalSource(),
        live,
        clock=lambda: NOW,
        holder_id="collector-one",
    )
    errors: list[BaseException] = []

    def run_service() -> None:
        try:
            service.run()
        except BaseException as exc:  # pragma: no cover - asserted through the thread
            errors.append(exc)

    thread = threading.Thread(target=run_service)
    thread.start()
    assert live.started.wait(timeout=2)

    service.request_stop()
    thread.join(timeout=2)

    assert errors == []
    assert thread.is_alive() is False
    assert live.stop_calls >= 1
    assert repository.finished_runs[-1]["status"] == "stopped"
    assert len(repository.release_calls) == 1
