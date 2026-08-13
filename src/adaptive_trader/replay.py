"""Deterministic, offline replay of the live orchestration path."""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from adaptive_trader.broker import FakePaperBroker
from adaptive_trader.clock import FakeClock, as_utc
from adaptive_trader.constants import NEW_YORK, UTC
from adaptive_trader.data import required_history_from_config
from adaptive_trader.decision_engine import ForwardDecisionEngine
from adaptive_trader.live import LiveCycleResult, LiveService, TargetProvider
from adaptive_trader.live_models import (
    BrokerOrderState,
    MarketBar,
    MarketClockState,
    ReplayEvent,
    ReplayEventType,
    RunMode,
    Side,
    TradeUpdate,
)
from adaptive_trader.market_data_live import ReplayMarketDataProvider
from adaptive_trader.persistence import AuditRepository, Database


@dataclass(frozen=True, slots=True)
class ReplayResult:
    cycles: tuple[LiveCycleResult, ...]
    reconciliation_ids: tuple[str, ...]
    restart_count: int
    event_count: int
    broker_submit_calls: int
    database: Database
    repository: AuditRepository


def _configured_universe(config: Any | None) -> tuple[str, ...]:
    if config is None:
        return ("SPY",)
    section = getattr(config, "universe", None) or getattr(config, "data", None)
    values = getattr(section, "tickers", ("SPY",))
    return tuple(str(symbol).strip().upper() for symbol in values)


def _completed_session_dates(
    config: Any,
    *,
    before: date,
) -> tuple[date, ...]:
    """Return enough deterministic weekday sessions inside the configured window."""

    required = required_history_from_config(config)
    calendar_days = int(config.market_data.historical_calendar_days)
    lower_bound = before - timedelta(days=calendar_days)
    cursor = before - timedelta(days=1)
    sessions: list[date] = []
    while cursor >= lower_bound and len(sessions) < required:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    if len(sessions) < required:
        raise ValueError(
            "Replay history window cannot satisfy the configured minimum history: "
            f"need {required} completed sessions inside {calendar_days} calendar days, "
            f"found {len(sessions)}"
        )
    return tuple(reversed(sessions))


def _generate_completed_daily_history(
    config: Any,
    *,
    before: datetime,
    seed: int,
) -> tuple[MarketBar, ...]:
    """Build finite provider-style daily bars strictly before the replay session."""

    current_session = as_utc(before).astimezone(NEW_YORK).date()
    sessions = _completed_session_dates(config, before=current_session)
    symbols = _configured_universe(config)
    rng = random.Random(seed)
    prices = {symbol: 70.0 + 17.0 * index for index, symbol in enumerate(symbols)}
    bars: list[MarketBar] = []
    received_at = as_utc(before)
    for session_index, session_date in enumerate(sessions):
        bar_at = datetime.combine(session_date, time(16), tzinfo=NEW_YORK).astimezone(UTC)
        for symbol_index, symbol in enumerate(symbols):
            # A positive drift guarantees a usable momentum signal; bounded
            # deterministic variation keeps volatility/covariance well-defined.
            daily_return = (
                0.00075
                + 0.00010 * symbol_index
                + 0.00020 * ((session_index + symbol_index) % 7 - 3) / 3
                + rng.uniform(-0.00015, 0.00015)
            )
            prices[symbol] *= 1.0 + daily_return
            close = Decimal(f"{prices[symbol]:.6f}")
            opened = close * Decimal("0.9995")
            bars.append(
                MarketBar(
                    symbol=symbol,
                    start=bar_at,
                    end=bar_at + timedelta(minutes=1),
                    open=opened,
                    high=close * Decimal("1.0005"),
                    low=opened * Decimal("0.9995"),
                    close=close,
                    volume=1_000_000 + session_index * 10 + symbol_index,
                    feed="REPLAY",
                    received_at=received_at,
                    source="deterministic_completed_daily_history",
                )
            )
    return tuple(bars)


def generate_synthetic_replay_events(
    config: Any | None = None,
    *,
    start: datetime | None = None,
    sessions: int = 1,
    seed: int | None = None,
) -> tuple[ReplayEvent, ...]:
    """Generate a small, deterministic offline session for the bare replay CLI."""

    if sessions <= 0:
        raise ValueError("sessions must be positive")
    if seed is None:
        seed = int(getattr(getattr(config, "replay", None), "deterministic_seed", 20260808))
    rng = random.Random(seed)
    symbols = _configured_universe(config)
    if not symbols:
        raise ValueError("Synthetic replay requires a nonempty universe")
    cursor = as_utc(start or datetime(2026, 1, 2, 15, 5, tzinfo=UTC))
    sequence = 0
    events: list[ReplayEvent] = []
    for _ in range(sessions):
        session_at = cursor
        while session_at.weekday() >= 5:
            session_at += timedelta(days=1)
        bar_at = session_at - timedelta(minutes=1)
        for symbol_index, symbol in enumerate(symbols):
            close = (
                Decimal("50")
                + Decimal(symbol_index * 10)
                + Decimal(str(round(rng.uniform(-1.0, 1.0), 4)))
            )
            events.append(
                ReplayEvent(
                    sequence=sequence,
                    event_type=ReplayEventType.BAR,
                    timestamp=bar_at,
                    payload={
                        "symbol": symbol,
                        "start": bar_at,
                        "open": str(close),
                        "high": str(close + Decimal("0.10")),
                        "low": str(close - Decimal("0.10")),
                        "close": str(close),
                        "volume": 1_000 + symbol_index,
                        "feed": "REPLAY",
                    },
                )
            )
            sequence += 1
        events.append(
            ReplayEvent(
                sequence=sequence,
                event_type=ReplayEventType.EVALUATE,
                timestamp=session_at,
                payload=None,
            )
        )
        sequence += 1
        cursor = session_at + timedelta(days=1)
    return tuple(events)


def load_replay_events(
    path: str | Path,
    *,
    config: Any | None = None,
) -> tuple[ReplayEvent, ...]:
    """Load JSONL events, falling back to deterministic synthetic data if absent."""

    fixture = Path(path)
    if not fixture.exists():
        return generate_synthetic_replay_events(config)
    events: list[ReplayEvent] = []
    for line_number, line in enumerate(fixture.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            raw = json.loads(line)
            raw["timestamp"] = datetime.fromisoformat(str(raw["timestamp"]).replace("Z", "+00:00"))
            payload = raw.get("payload")
            if isinstance(payload, dict):
                for key in ("start", "end", "received_at", "submitted_at", "updated_at"):
                    if isinstance(payload.get(key), str):
                        payload[key] = datetime.fromisoformat(payload[key].replace("Z", "+00:00"))
            events.append(_event(raw, len(events)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid replay fixture line {line_number}: {exc}") from exc
    return tuple(events)


def _bar(payload: Any, timestamp: datetime) -> MarketBar:
    if isinstance(payload, MarketBar):
        return payload
    if not isinstance(payload, Mapping):
        raise TypeError("Replay bar payload must be MarketBar or a mapping")
    start = payload.get("start", timestamp)
    close = Decimal(str(payload["close"]))
    return MarketBar(
        symbol=str(payload["symbol"]),
        start=as_utc(start),
        end=as_utc(payload["end"]) if payload.get("end") else None,
        open=Decimal(str(payload.get("open", close))),
        high=Decimal(str(payload.get("high", close))),
        low=Decimal(str(payload.get("low", close))),
        close=close,
        volume=int(payload.get("volume", 0)),
        trade_count=(None if payload.get("trade_count") is None else int(payload["trade_count"])),
        vwap=(None if payload.get("vwap") is None else Decimal(str(payload["vwap"]))),
        feed=str(payload.get("feed", "REPLAY")),
        received_at=as_utc(payload.get("received_at", timestamp)),
        source=str(payload.get("source", "replay")),
        is_correction=bool(payload.get("is_correction", False)),
    )


def _trade_update(payload: Any, timestamp: datetime) -> TradeUpdate:
    if isinstance(payload, TradeUpdate):
        return payload
    if not isinstance(payload, Mapping):
        raise TypeError("Replay trade-update payload must be TradeUpdate or a mapping")
    raw_order = payload.get("order")
    if isinstance(raw_order, BrokerOrderState):
        order = raw_order
    elif isinstance(raw_order, Mapping):
        order = BrokerOrderState(
            client_order_id=str(raw_order["client_order_id"]),
            broker_order_id=str(raw_order["broker_order_id"]),
            symbol=str(raw_order["symbol"]),
            side=Side(str(raw_order["side"])),
            status=str(raw_order.get("status", payload.get("event", "accepted"))),
            submitted_at=as_utc(raw_order.get("submitted_at", timestamp)),
            updated_at=as_utc(raw_order.get("updated_at", timestamp)),
            requested_notional=(
                None
                if raw_order.get("requested_notional") is None
                else Decimal(str(raw_order["requested_notional"]))
            ),
            requested_quantity=(
                None
                if raw_order.get("requested_quantity") is None
                else Decimal(str(raw_order["requested_quantity"]))
            ),
            filled_quantity=Decimal(str(raw_order.get("filled_quantity", 0))),
            average_fill_price=(
                None
                if raw_order.get("average_fill_price") is None
                else Decimal(str(raw_order["average_fill_price"]))
            ),
        )
    else:
        raise TypeError("Replay trade update requires an order")
    return TradeUpdate(
        event=str(payload.get("event", order.status)),
        order=order,
        timestamp=timestamp,
        event_id=None if payload.get("event_id") is None else str(payload["event_id"]),
        execution_id=(
            None if payload.get("execution_id") is None else str(payload["execution_id"])
        ),
        fill_quantity=(
            None if payload.get("fill_quantity") is None else Decimal(str(payload["fill_quantity"]))
        ),
        fill_price=(
            None if payload.get("fill_price") is None else Decimal(str(payload["fill_price"]))
        ),
    )


def _event(raw: ReplayEvent | Mapping[str, Any], index: int) -> ReplayEvent:
    if isinstance(raw, ReplayEvent):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("Replay events must be ReplayEvent instances or mappings")
    return ReplayEvent(
        sequence=int(raw.get("sequence", index)),
        event_type=ReplayEventType(str(raw["event_type"])),
        timestamp=as_utc(raw["timestamp"]),
        payload=raw.get("payload"),
    )


def _advance_broker_clock(broker: FakePaperBroker, timestamp: datetime) -> None:
    previous = broker.get_clock()
    broker.set_clock(
        MarketClockState(
            timestamp=timestamp,
            is_open=previous.is_open,
            next_open=max(previous.next_open, timestamp + timedelta(days=1)),
            next_close=max(previous.next_close, timestamp + timedelta(hours=6)),
        )
    )


def run_replay(
    config: Any,
    events: Sequence[ReplayEvent | Mapping[str, Any]],
    *,
    database: Database | str | Path | None = None,
    broker: FakePaperBroker | None = None,
    provider: ReplayMarketDataProvider | None = None,
    target_weights: Mapping[str, float] | None = None,
    auto_fill: bool = True,
    use_decision_engine: bool = True,
) -> ReplayResult:
    """Replay events exactly in supplied sequence order without network access.

    If ``database`` is omitted, an isolated in-memory database is always used;
    replay therefore cannot contaminate the primary forward database by default.
    By default an ``EVALUATE`` event without explicit ``target_weights`` invokes
    the real forward decision stack against deterministic completed daily bars.
    Set ``use_decision_engine=False`` for focused execution tests that supply an
    explicit event or function-level target.
    """

    normalized = tuple(_event(raw, index) for index, raw in enumerate(events))
    sequences = [event.sequence for event in normalized]
    if len(sequences) != len(set(sequences)):
        raise ValueError("Replay event sequences must be unique")
    start = normalized[0].timestamp if normalized else datetime(2026, 1, 2, 15, tzinfo=UTC)
    db = database if isinstance(database, Database) else Database(database or ":memory:")
    repository = AuditRepository(db)
    replay_broker = broker or FakePaperBroker(now=start, auto_fill=auto_fill)
    first_evaluation = next(
        (event.timestamp for event in normalized if event.event_type is ReplayEventType.EVALUATE),
        start,
    )
    seed = int(getattr(getattr(config, "replay", None), "deterministic_seed", 20260808))
    completed_history = (
        _generate_completed_daily_history(config, before=first_evaluation, seed=seed)
        if use_decision_engine and provider is None
        else ()
    )
    replay_provider = provider or ReplayMarketDataProvider(completed_history)
    # Keep the forward engine's daily view separate from emitted minute bars.
    # This matters for multi-session replay: a prior intraday freshness bar is
    # not silently promoted into an official completed daily close.
    decision_market_data = (
        ReplayMarketDataProvider(completed_history) if provider is None else replay_provider
    )
    clock = FakeClock(start)
    configured_run_name = str(getattr(getattr(config, "project", None), "run_name", "replay"))
    invocation_namespace = f"{configured_run_name}:invocation:{uuid.uuid4().hex}"

    def configured_target_provider(
        now: datetime,
        account: Any,
        positions: Sequence[Any],
    ) -> Mapping[str, float]:
        del now, account, positions
        if target_weights is None:  # Defensive: this callable is installed only with a target.
            raise ValueError("Explicit replay target is unavailable")
        return target_weights

    def build_service(*, establish_baseline: bool = False) -> LiveService:
        target_provider: TargetProvider | None
        if use_decision_engine:
            target_provider = cast(
                TargetProvider,
                ForwardDecisionEngine(config, decision_market_data),
            )
        elif target_weights is not None:
            target_provider = configured_target_provider
        else:
            target_provider = None
        service = LiveService(
            config,
            repository=repository,
            broker=replay_broker,
            market_data=replay_provider,
            mode=RunMode.REPLAY,
            clock=clock,
            target_provider=target_provider,
            idempotency_namespace=invocation_namespace,
            allow_simulated_replay_orders=True,
        )
        if establish_baseline:
            # Separate top-level replay invocations may append to one audit DB
            # while each starts from a fresh fake broker.  In-sequence RESTART
            # events deliberately do not reset this comparison baseline.
            _advance_broker_clock(replay_broker, first_evaluation)
            repository.record_account_state(
                replay_broker.get_account(),
                replay_broker.get_positions(),
                run_id=service.run_id,
            )
        service.start_streams()
        return service

    service = build_service(establish_baseline=True)
    cycles: list[LiveCycleResult] = []
    reconciliation_ids: list[str] = []
    restarts = 0
    try:
        for event in normalized:
            # Preserve input event order.  Timestamps may be intentionally out
            # of order to exercise bar handling; the injected clock never moves
            # backwards.
            if event.timestamp >= clock.now():
                clock.set(event.timestamp)
                _advance_broker_clock(replay_broker, event.timestamp)
            event_type = event.event_type
            if event_type is ReplayEventType.BAR:
                replay_provider.emit(_bar(event.payload, event.timestamp))
            elif event_type is ReplayEventType.TRADE_UPDATE:
                service.process_trade_update(_trade_update(event.payload, event.timestamp))
            elif event_type is ReplayEventType.DISCONNECT:
                replay_provider.set_connected(False, "replay_disconnect")
            elif event_type is ReplayEventType.RECONNECT:
                replay_provider.set_connected(True, "replay_reconnect")
            elif event_type is ReplayEventType.ADVANCE:
                seconds = (
                    float(event.payload.get("seconds", 0))
                    if isinstance(event.payload, Mapping)
                    else float(event.payload or 0)
                )
                if seconds:
                    advanced = clock.advance(seconds)
                    _advance_broker_clock(replay_broker, advanced)
            elif event_type is ReplayEventType.RESTART:
                service.shutdown("replay_restart")
                service = build_service()
                restarts += 1
            elif event_type is ReplayEventType.RECONCILE:
                reconciliation_ids.append(service.reconcile().reconciliation_id)
            elif event_type is ReplayEventType.EVALUATE:
                weights = (
                    event.payload.get("target_weights", target_weights)
                    if isinstance(event.payload, Mapping)
                    else target_weights
                )
                cycles.append(service.run_once(weights))
            elif event_type is ReplayEventType.BROKER_MUTATION:
                if not callable(event.payload):
                    raise TypeError("broker_mutation payload must be callable")
                event.payload(replay_broker)
    finally:
        service.shutdown("replay_complete")

    return ReplayResult(
        cycles=tuple(cycles),
        reconciliation_ids=tuple(reconciliation_ids),
        restart_count=restarts,
        event_count=len(normalized),
        broker_submit_calls=replay_broker.submit_calls,
        database=db,
        repository=repository,
    )
