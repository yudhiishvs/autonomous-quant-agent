"""Paper-only forward service built from the durable execution primitives.

The one-shot path is deliberately the canonical path: the daemon merely calls
it on a schedule.  This keeps replay, CLI ``paper-once`` and long-running paper
operation on the same state machine and persistence model.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from adaptive_trader.broker import AlpacaPaperBroker, Broker, FakePaperBroker, validate_asset
from adaptive_trader.clock import Clock, SystemClock, as_utc
from adaptive_trader.constants import (
    NEW_YORK,
    PAPER_API_KEY_ENV,
    PAPER_FLATTEN_ACKNOWLEDGEMENT,
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    PAPER_RESUME_ACKNOWLEDGEMENT,
    PAPER_SECRET_KEY_ENV,
    UTC,
)
from adaptive_trader.exceptions import (
    BrokerConnectionError,
    ReconciliationBlocked,
    SafetyViolation,
)
from adaptive_trader.execution import (
    TERMINAL_STATES,
    OrderManager,
    OrderPlanner,
    evaluate_order_enablement,
    state_for_broker_status,
)
from adaptive_trader.live_models import (
    AccountState,
    DataFreshnessState,
    LocalOrderState,
    MarketBar,
    MarketSession,
    OrderIntent,
    PlanningResult,
    ReconciliationResult,
    RunMode,
    Side,
    SubmissionGateResult,
    TradeUpdate,
)
from adaptive_trader.logging_config import redact
from adaptive_trader.market_data_live import (
    AlpacaMarketDataProvider,
    BarStore,
    MarketDataProvider,
)
from adaptive_trader.persistence import AuditRepository, configuration_hash
from adaptive_trader.reconciliation import Reconciler

TargetProvider = Callable[[datetime, AccountState, Sequence[Any]], Mapping[str, float]]


class _LiveRiskEngineProxy:
    """Inject durable account-risk context into legacy decision providers.

    ``ForwardDecisionEngine`` predates the live risk-context protocol and owns
    its risk engine as ``_risk``.  This narrow proxy keeps that provider API
    stable while ensuring its single evaluation sees the durable high-water
    drawdown and active latches.  Providers with ``set_live_risk_context`` use
    that public extension point instead.
    """

    def __init__(self, delegate: Any, context: Mapping[str, Any]) -> None:
        self._delegate = delegate
        self._context = context

    def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.update(self._context)
        return self._delegate.evaluate(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


@dataclass(frozen=True, slots=True)
class LiveCycleResult:
    """Stable result returned by ``LiveService.run_once`` and replay."""

    decision_id: str
    session_date: date
    claimed: bool
    status: str
    gate: SubmissionGateResult | None
    planning: PlanningResult | None
    reconciliation: ReconciliationResult | None
    decision_metadata: Mapping[str, Any] | None = None
    decision_context: Mapping[str, Any] | None = None
    submitted_client_order_ids: tuple[str, ...] = ()
    hypothetical_client_order_ids: tuple[str, ...] = ()
    skip_reason: str | None = None


def _section_value(config: Any, section: str, name: str, default: Any) -> Any:
    target = getattr(config, section, config)
    return getattr(target, name, default)


def _universe(config: Any) -> tuple[str, ...]:
    section = getattr(config, "universe", None) or getattr(config, "data", None)
    values = getattr(section, "tickers", ()) if section is not None else ()
    return tuple(
        dict.fromkeys(str(symbol).strip().upper() for symbol in values if str(symbol).strip())
    )


def _parse_time(raw: Any, default: time) -> time:
    if isinstance(raw, time):
        return raw
    try:
        hour, minute = (int(part) for part in str(raw).split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        return default


def _payload(result: LiveCycleResult) -> dict[str, Any]:
    gate = result.gate
    planning = result.planning
    reconciliation = result.reconciliation
    metadata = result.decision_metadata
    context = result.decision_context or {}
    strategy_outputs = metadata.get("strategy_outputs", {}) if metadata else {}
    risk_decision = metadata.get("risk_decision") if metadata else None
    allocation = metadata.get("allocation") if metadata else None
    proposed_target = None
    if isinstance(risk_decision, Mapping):
        proposed_target = risk_decision.get("proposed_weights")
    if proposed_target is None and isinstance(allocation, Mapping):
        proposed_target = allocation.get("pre_risk_weights")
    payload = {
        "decision_id": result.decision_id,
        "session_date": result.session_date.isoformat(),
        "claimed": result.claimed,
        "status": result.status,
        "execution_status": result.status,
        "skip_reason": result.skip_reason,
        "run_id": context.get("run_id"),
        "configuration_hash": context.get("configuration_hash"),
        "strategy_version": context.get("strategy_version"),
        "scheduled_at": context.get("scheduled_at"),
        "actual_at": context.get("actual_at"),
        "mode": context.get("mode"),
        "market": context.get("market"),
        "feed": context.get("feed"),
        "freshness": context.get("freshness"),
        "account": context.get("account"),
        "positions": context.get("positions", ()),
        "current_drawdown": context.get("current_drawdown"),
        "current_daily_loss": context.get("current_daily_loss"),
        "current_target": context.get("current_target"),
        "current_cash_weight": context.get("current_cash_weight"),
        "target_cash_weight": context.get("target_cash_weight"),
        "turnover": context.get("turnover"),
        "estimated_volatility": context.get("estimated_volatility"),
        "warnings": context.get("warnings", ()),
        "incidents": context.get("incidents", ()),
        "non_evaluated_fields": context.get("non_evaluated_fields", {}),
        "order_outcomes": context.get("order_outcomes", ()),
        "decision_metadata": None if metadata is None else dict(metadata),
        "signal_cutoff": None if metadata is None else metadata.get("cutoff"),
        "momentum": (
            strategy_outputs.get("momentum") if isinstance(strategy_outputs, Mapping) else None
        ),
        "mean_reversion": (
            strategy_outputs.get("mean_reversion")
            if isinstance(strategy_outputs, Mapping)
            else None
        ),
        "regime": None if metadata is None else metadata.get("regime"),
        "allocation": allocation,
        "proposed_target": proposed_target,
        "final_target": context.get(
            "final_target",
            None if metadata is None else metadata.get("final_target"),
        ),
        "risk_actions": context.get(
            "risk_actions",
            () if metadata is None else metadata.get("risk_actions", ()),
        ),
        "risk_decision": risk_decision,
        "operational_risk_state": context.get("operational_risk_state"),
        "gate": None
        if gate is None
        else {
            "allowed": gate.allowed,
            "reasons": list(gate.reasons),
            "effective_mode": gate.effective_mode,
        },
        "planning": None
        if planning is None
        else {
            "intents": [
                {
                    "client_order_id": intent.client_order_id,
                    "decision_id": intent.decision_id,
                    "session_date": intent.session_date.isoformat(),
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "sequence": intent.sequence,
                    "quantity": None if intent.quantity is None else str(intent.quantity),
                    "notional": None if intent.notional is None else str(intent.notional),
                    "reference_price": str(intent.reference_price),
                    "created_at": intent.created_at,
                    "reason": intent.reason,
                }
                for intent in planning.intents
            ],
            "skipped": list(planning.skipped),
        },
        "reconciliation": None
        if reconciliation is None
        else {
            "id": reconciliation.reconciliation_id,
            "blocking": reconciliation.blocking,
            "discrepancies": [
                {
                    "kind": item.kind,
                    "severity": item.severity.value,
                    "message": item.message,
                    "symbol": item.symbol,
                    "client_order_id": item.client_order_id,
                }
                for item in reconciliation.discrepancies
            ],
        },
        "submitted_client_order_ids": list(result.submitted_client_order_ids),
        "hypothetical_client_order_ids": list(result.hypothetical_client_order_ids),
    }
    sanitized = _sanitize_audit_value(payload)
    if not isinstance(sanitized, dict):
        raise TypeError("Sanitized decision receipt must remain a mapping")
    return sanitized


def _account_id_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _sanitize_audit_value(value: Any) -> Any:
    """Remove raw broker-account identifiers at every durable audit boundary."""

    if isinstance(value, AccountState):
        return {
            "timestamp": value.timestamp,
            "account_id_hash": _account_id_hash(value.account_id),
            "status": value.status,
            "equity": value.equity,
            "cash": value.cash,
            "buying_power": value.buying_power,
            "last_equity": value.last_equity,
            "trading_blocked": value.trading_blocked,
        }
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in {"account_id", "account_number", "paper_account_id"}:
                sanitized[f"{key}_hash"] = _account_id_hash(item)
            else:
                sanitized[key] = _sanitize_audit_value(item)
        return sanitized
    if isinstance(value, tuple):
        return tuple(_sanitize_audit_value(item) for item in value)
    if isinstance(value, list):
        return [_sanitize_audit_value(item) for item in value]
    if isinstance(value, set):
        return tuple(_sanitize_audit_value(item) for item in value)
    return value


class LiveService:
    """Orchestrate observation, one-shot paper decisions and scheduled operation.

    ``broker`` and ``market_data`` are mandatory dependencies.  Constructing the
    service never reads Alpaca credentials and never establishes a connection.
    """

    def __init__(
        self,
        config: Any,
        *,
        repository: AuditRepository,
        broker: Broker,
        market_data: MarketDataProvider,
        mode: RunMode | str = RunMode.OBSERVE,
        clock: Clock | None = None,
        environment: Mapping[str, str] | None = None,
        target_provider: TargetProvider | None = None,
        run_id: str | None = None,
        strategy_version: str = "configured-strategy-v1",
        idempotency_namespace: str | None = None,
        allow_simulated_replay_orders: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.repository = repository
        self.broker = broker
        self.market_data = market_data
        self.mode = RunMode(str(getattr(mode, "value", mode)).replace("-", "_"))
        self.clock = clock or SystemClock()
        self.environment = environment
        self.target_provider = target_provider
        self.universe = _universe(config)
        if not self.universe:
            raise ValueError("LiveService requires a nonempty configured universe")
        if allow_simulated_replay_orders and not (
            self.mode is RunMode.REPLAY and isinstance(broker, FakePaperBroker)
        ):
            raise SafetyViolation("Simulated replay orders require replay mode and FakePaperBroker")
        self.allow_simulated_replay_orders = allow_simulated_replay_orders
        self.dry_run = bool(dry_run)
        self.strategy_version = str(strategy_version)
        self.run_name = str(_section_value(config, "project", "run_name", "primary"))
        self.idempotency_namespace = str(idempotency_namespace or self.run_name)
        self.run_id = run_id or repository.start_run(
            mode=self.mode.value,
            configuration=config,
            market_data_feed=getattr(market_data, "feed", None),
            strategy_version=self.strategy_version,
        )
        self.bar_store = BarStore(
            repository,
            universe=self.universe,
            stale_after_seconds=int(
                _section_value(config, "market_data", "stale_after_seconds", 180)
            ),
            run_id=self.run_id,
            clock=self.clock,
        )
        self.planner = OrderPlanner.from_config(config)
        self.order_manager = OrderManager(
            repository=repository,
            broker=broker,
            run_id=self.run_id,
        )
        self.reconciler = Reconciler(
            repository=repository,
            broker=broker,
            universe=self.universe,
            run_id=self.run_id,
            clock=self.clock,
        )
        self._stop = threading.Event()
        self._streams_started = False
        self._closed = False
        self._active_cycle_context: dict[str, Any] | None = None
        durable_incidents = repository.active_incidents(
            configuration_hash=configuration_hash(config)
        )
        self._incident_ids = [str(row["incident_id"]) for row in durable_incidents]
        self._incidents_by_type: dict[str, list[str]] = {}
        for row in durable_incidents:
            self._incidents_by_type.setdefault(str(row["incident_type"]), []).append(
                str(row["incident_id"])
            )
        self._last_heartbeat_at: datetime | None = None
        self._last_risk_snapshot_at: datetime | None = None
        self._last_reconciliation_at: datetime | None = None
        self._last_open_order_monitor_at: datetime | None = None
        self._last_risk_metrics: dict[str, Any] = {}
        self._timed_out_decisions: set[str] = set()
        self._cash_blocked_decisions: set[str] = set()
        self._feed_entitlement_verified = False
        self._paper_account_verified = False
        self._assets_verified = False
        self._history_preflight_verified = False
        self._last_trade_stream_status: str | None = None
        self._trade_stream_incident_active = False
        self._calendar_cache_date: date | None = None
        self._calendar_cache_session: MarketSession | None = None
        self._calendar_last_attempt_at: datetime | None = None
        self._calendar_last_error: str | None = None

    def _paper_authority_gate(self) -> dict[str, bool]:
        """Return non-secret, service-owned paper execution authority facts.

        Fully specified production configurations can submit only through the
        concrete Alpaca paper broker and Alpaca data provider after all startup
        preflights.  Missing provider configuration is never authority: callers
        that need deterministic execution tests must use the isolated replay
        mode with a ``FakePaperBroker``.
        """

        provider = _section_value(self.config, "market_data", "provider", None)
        values = os.environ if self.environment is None else self.environment
        credentials_verified = bool(values.get(PAPER_API_KEY_ENV)) and bool(
            values.get(PAPER_SECRET_KEY_ENV)
        )
        adapter_verified = bool(
            isinstance(self.broker, AlpacaPaperBroker)
            or getattr(self.broker, "underlying_paper_adapter_verified", False)
        )
        provider_authority_verified = bool(
            str(provider).lower() == "alpaca"
            and adapter_verified
            and isinstance(self.market_data, AlpacaMarketDataProvider)
        )
        startup_preflight_verified = bool(
            self._streams_started
            and self._paper_account_verified
            and self._feed_entitlement_verified
            and self._assets_verified
            and self._history_preflight_verified
        )
        return {
            "credentials_verified": credentials_verified,
            "provider_authority_verified": provider_authority_verified,
            "startup_preflight_verified": startup_preflight_verified,
        }

    def start_streams(self) -> None:
        if self._streams_started:
            return
        self._verify_paper_account()
        self._verify_feed_entitlement()
        self._verify_assets()
        self._verify_target_history()
        self.market_data.start_stream(
            self.universe,
            self.process_bar,
            self.bar_store.mark_health,
        )
        self.broker.start_trade_updates(self.process_trade_update)
        self._streams_started = True
        self.recover_durable_market_data_gaps()

    def _verify_paper_account(self) -> None:
        """Persist sanitized evidence that production uses the paper adapter."""

        provider = _section_value(self.config, "market_data", "provider", None)
        if provider is None or str(provider).lower() in {"replay", "synthetic"}:
            self._paper_account_verified = True
            return
        adapter_verified = bool(
            isinstance(self.broker, AlpacaPaperBroker)
            or getattr(self.broker, "underlying_paper_adapter_verified", False)
        )
        if not (
            str(provider).lower() == "alpaca"
            and isinstance(self.market_data, AlpacaMarketDataProvider)
            and adapter_verified
            and self.broker.paper_only
        ):
            raise SafetyViolation(
                "Production paper-account verification requires the concrete Alpaca "
                "paper broker and market-data adapters"
            )
        account = self.broker.get_account()
        if account.status.upper() != "ACTIVE" or account.trading_blocked:
            raise SafetyViolation("The dedicated Alpaca paper account is not active and unblocked")
        snapshot_id = self.repository.record_account_state(
            account,
            self.broker.get_positions(),
            run_id=self.run_id,
        )
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="paper_broker",
            event_type="paper_account_verified",
            created_at=self.clock.now(),
            payload={
                "adapter": "AlpacaPaperBroker",
                "paper_only": True,
                "account_status": account.status,
                "trading_blocked": account.trading_blocked,
                "account_snapshot_id": snapshot_id,
            },
        )
        self._paper_account_verified = True

    def _verify_feed_entitlement(self) -> None:
        """Fail closed before live startup when an adapter exposes a feed probe."""

        if self._feed_entitlement_verified:
            return
        probe = getattr(self.market_data, "check_feed_entitlement", None)
        if not callable(probe):
            # Replay/synthetic providers have no external entitlement to prove.
            self._feed_entitlement_verified = True
            return
        symbol = self.universe[0]
        now = self.clock.now()
        try:
            confirmed = bool(probe(symbol, now=now))
            if not confirmed:
                raise SafetyViolation(
                    f"{self.market_data.feed} entitlement probe was not confirmed"
                )
        except Exception as exc:
            reason = redact(str(exc) or type(exc).__name__)
            self.bar_store.mark_health(False, "feed_entitlement_failed")
            self._record_incident(
                "market_data_feed_entitlement_failed",
                reason,
                details={"feed": self.market_data.feed, "symbol": symbol},
            )
            raise SafetyViolation(
                f"Configured {self.market_data.feed} feed entitlement was not verified: {reason}"
            ) from None
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="market_data",
            event_type="feed_entitlement_confirmed",
            symbol=symbol,
            payload={"feed": self.market_data.feed, "fallback_used": False},
            created_at=now,
        )
        self._feed_entitlement_verified = True
        self._resolve_incidents("market_data_feed_entitlement_failed")

    def _verify_assets(self) -> None:
        """Validate the complete configured universe before daemon readiness."""

        if self._assets_verified:
            return
        failures: dict[str, tuple[str, ...]] = {}
        for symbol in self.universe:
            try:
                asset = self.broker.get_asset(symbol)
                valid, reasons = validate_asset(asset, self.universe)
            except Exception as exc:
                valid = False
                reasons = (redact(str(exc) or type(exc).__name__),)
            if not valid:
                failures[symbol] = reasons
        now = self.clock.now()
        if failures:
            details = {symbol: list(reasons) for symbol, reasons in sorted(failures.items())}
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="system",
                event_type="asset_validation_failed",
                created_at=now,
                payload={"failures": details},
            )
            reason = "; ".join(
                f"{symbol}: {', '.join(reasons)}" for symbol, reasons in sorted(failures.items())
            )
            if not self._incidents_by_type.get("startup_asset_validation_failed"):
                self._record_incident(
                    "startup_asset_validation_failed",
                    reason,
                    details={"failures": details},
                )
            raise SafetyViolation(f"Configured universe failed startup asset validation: {reason}")
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="system",
            event_type="asset_validation_confirmed",
            created_at=now,
            payload={"symbols": list(self.universe)},
        )
        self._assets_verified = True
        self._resolve_incidents("startup_asset_validation_failed")

    def _verify_target_history(self) -> None:
        """Validate sufficient completed history before real-time connections."""

        if self._history_preflight_verified:
            return
        now = self.clock.now()
        target = self.target_provider
        public_probe = getattr(target, "preflight_history", None)
        compatibility_probe = getattr(target, "_completed_history", None)
        requires_history = (
            target is not None and getattr(target, "minimum_history", None) is not None
        )
        if target is None or (
            not callable(public_probe)
            and not callable(compatibility_probe)
            and not requires_history
        ):
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="strategy",
                event_type="history_preflight_not_required",
                created_at=now,
                payload={"target_provider_configured": target is not None},
            )
            self._history_preflight_verified = True
            return
        if not callable(public_probe) and not callable(compatibility_probe):
            reason = "Configured historical target provider exposes no read-only history preflight"
            self._record_incident("strategy_history_preflight_failed", reason)
            raise SafetyViolation(reason)
        try:
            if callable(public_probe):
                evidence = public_probe(now)
            elif callable(compatibility_probe):
                current_session = as_utc(now).astimezone(NEW_YORK).date()
                evidence = compatibility_probe(as_utc(now), current_session)
            else:  # Guarded above; retained for static exhaustiveness.
                raise SafetyViolation("strategy history preflight is unavailable")
            prices = getattr(evidence, "prices", None)
            observations = None if prices is None else len(prices)
            minimum = getattr(target, "minimum_history", None)
            if observations is not None and minimum is not None and observations < int(minimum):
                raise SafetyViolation(
                    f"completed history has {observations} observations; {minimum} are required"
                )
        except Exception as exc:
            reason = redact(str(exc) or type(exc).__name__)
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="strategy",
                event_type="history_preflight_failed",
                created_at=now,
                payload={"reason": reason},
            )
            if not self._incidents_by_type.get("strategy_history_preflight_failed"):
                self._record_incident("strategy_history_preflight_failed", reason)
            raise SafetyViolation(
                f"Completed strategy history was not verified before stream startup: {reason}"
            ) from None
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="strategy",
            event_type="history_preflight_confirmed",
            created_at=now,
            payload={
                "observations": observations,
                "minimum_required": getattr(target, "minimum_history", None),
                "cutoff": getattr(evidence, "cutoff", None),
            },
        )
        self._history_preflight_verified = True
        self._resolve_incidents("strategy_history_preflight_failed")

    def process_bar(self, bar: MarketBar) -> str:
        return self.bar_store.ingest(bar)

    def process_trade_update(self, update: TradeUpdate) -> bool:
        return self.order_manager.process_trade_update(update)

    def recover_durable_market_data_gaps(self) -> tuple[str, ...]:
        """Backfill exact persisted gap intervals without changing data feeds."""

        gaps = self.repository.unresolved_gaps(self.universe)
        if not gaps:
            self.bar_store.refresh_gap_state()
            self._resolve_incidents("durable_market_data_gap_recovery_incomplete")
            return ()
        max_intervals = max(
            1,
            int(
                _section_value(
                    self.config,
                    "market_data",
                    "gap_recovery_max_intervals",
                    50,
                )
            ),
        )
        max_minutes = max(
            1,
            int(
                _section_value(
                    self.config,
                    "market_data",
                    "gap_recovery_max_minutes_per_interval",
                    600,
                )
            ),
        )
        configured_feed = str(self.market_data.feed).upper()
        failures: list[dict[str, Any]] = []
        attempted_ids: list[str] = []
        for gap in gaps[:max_intervals]:
            gap_id = str(gap["gap_id"])
            symbol = str(gap["symbol"]).upper()
            gap_feed = str(gap["feed"]).upper()
            start = as_utc(gap["gap_start"])
            end = as_utc(gap["gap_end"])
            minutes = int((end - start).total_seconds() // 60) + 1
            attempted_ids.append(gap_id)
            if gap_feed != configured_feed:
                failures.append(
                    {
                        "gap_id": gap_id,
                        "reason": "persisted gap feed differs from configured feed",
                        "gap_feed": gap_feed,
                        "configured_feed": configured_feed,
                    }
                )
                continue
            if minutes <= 0 or minutes > max_minutes:
                failures.append(
                    {
                        "gap_id": gap_id,
                        "reason": "gap interval exceeds bounded recovery window",
                        "minutes": minutes,
                        "maximum_minutes": max_minutes,
                    }
                )
                continue
            try:
                supplied = tuple(
                    self.market_data.get_bars(
                        (symbol,),
                        start=start,
                        end=end,
                        timeframe="minute",
                    )
                )
            except Exception as exc:
                failures.append(
                    {
                        "gap_id": gap_id,
                        "reason": redact(str(exc) or type(exc).__name__),
                    }
                )
                continue
            accepted = 0
            for bar in supplied:
                if (
                    bar.symbol != symbol
                    or bar.feed.upper() != gap_feed
                    or bar.start < start
                    or bar.start > end
                ):
                    continue
                self.bar_store.ingest(bar)
                accepted += 1
            if not accepted:
                failures.append(
                    {
                        "gap_id": gap_id,
                        "reason": "configured provider returned no in-range bars for exact gap",
                    }
                )
        if len(gaps) > max_intervals:
            failures.append(
                {
                    "reason": "unresolved gap count exceeds bounded startup batch",
                    "remaining_unattempted": len(gaps) - max_intervals,
                }
            )
        self.bar_store.resolve_gaps_after_backfill()
        remaining = self.repository.unresolved_gaps(self.universe)
        remaining_ids = tuple(str(gap["gap_id"]) for gap in remaining)
        resolved_ids = tuple(gap_id for gap_id in attempted_ids if gap_id not in set(remaining_ids))
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="market_data",
            event_type=(
                "durable_gap_recovery_complete"
                if not remaining_ids
                else "durable_gap_recovery_incomplete"
            ),
            created_at=self.clock.now(),
            payload={
                "attempted_gap_ids": attempted_ids,
                "resolved_gap_ids": resolved_ids,
                "remaining_gap_ids": remaining_ids,
                "failures": failures,
                "feed": configured_feed,
                "fallback_used": False,
            },
        )
        if remaining_ids:
            if not self._incidents_by_type.get("durable_market_data_gap_recovery_incomplete"):
                self._record_incident(
                    "durable_market_data_gap_recovery_incomplete",
                    "Persisted market-data gaps remain unresolved after exact backfill",
                    severity="warning",
                    details={
                        "remaining_gap_ids": remaining_ids,
                        "failures": failures,
                        "feed": configured_feed,
                    },
                )
        else:
            self._resolve_incidents("durable_market_data_gap_recovery_incomplete")
        return remaining_ids

    def _trade_updates_healthy(self) -> bool:
        return bool(getattr(self.broker, "trade_updates_healthy", False))

    def _trade_updates_status(self) -> str:
        return str(getattr(self.broker, "trade_updates_status", "unavailable"))

    def _apply_trade_stream_gate(
        self,
        gate: SubmissionGateResult,
        *,
        submission_expected: bool,
    ) -> SubmissionGateResult:
        if not submission_expected or self._trade_updates_healthy():
            return gate
        reason = "paper trade-update stream is not authenticated and healthy"
        return SubmissionGateResult(
            allowed=False,
            reasons=tuple(dict.fromkeys((*gate.reasons, reason))),
            effective_mode="observer",
        )

    def reconcile(self) -> ReconciliationResult:
        result = self.reconciler.run()
        self._last_reconciliation_at = result.completed_at
        return result

    def _record_incident(
        self,
        incident_type: str,
        message: str,
        *,
        severity: str = "error",
        details: Mapping[str, Any] | None = None,
    ) -> str:
        identifier = self.repository.record_incident(
            run_id=self.run_id,
            incident_type=incident_type,
            severity=severity,
            message=redact(message),
            details=details,
        )
        self._incident_ids.append(identifier)
        self._incidents_by_type.setdefault(incident_type, []).append(identifier)
        return identifier

    def _resolve_incidents(self, incident_type: str) -> None:
        identifiers = self._incidents_by_type.pop(incident_type, [])
        if not identifiers:
            return
        resolved = set(identifiers)
        for identifier in identifiers:
            self.repository.resolve_incident(identifier, resolved_at=self.clock.now())
        self._incident_ids = [
            identifier for identifier in self._incident_ids if identifier not in resolved
        ]

    def _market_session(self, session_date: date) -> MarketSession | None:
        sessions = self.broker.get_calendar(session_date, session_date)
        return next(
            (session for session in sessions if session.session_date == session_date),
            None,
        )

    def _scheduled_market_session(
        self,
        session_date: date,
        now: datetime,
    ) -> MarketSession | None:
        """Return a date-scoped calendar result with bounded failure retries."""

        if self._calendar_cache_date != session_date:
            self._calendar_cache_date = session_date
            self._calendar_cache_session = None
            self._calendar_last_attempt_at = None
            self._calendar_last_error = None
        if self._calendar_last_attempt_at is not None and self._calendar_last_error is None:
            return self._calendar_cache_session
        retry_seconds = max(
            0.0,
            float(
                _section_value(
                    self.config,
                    "schedule",
                    "calendar_retry_interval_seconds",
                    30,
                )
            ),
        )
        if (
            self._calendar_last_error is not None
            and self._calendar_last_attempt_at is not None
            and (now - self._calendar_last_attempt_at).total_seconds() < retry_seconds
        ):
            raise BrokerConnectionError(
                "Paper calendar remains unavailable; bounded retry is pending: "
                f"{self._calendar_last_error}"
            )
        self._calendar_last_attempt_at = now
        try:
            session = self._market_session(session_date)
        except Exception as exc:
            self._calendar_last_error = redact(str(exc) or type(exc).__name__)
            raise
        self._calendar_cache_session = session
        self._calendar_last_error = None
        return session

    def ensure_ready(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_seconds: float = 0.25,
        require_trade_updates: bool = False,
    ) -> DataFreshnessState:
        """Start read-only streams and boundedly backfill one-shot minute readiness."""

        if timeout_seconds is None:
            timeout_seconds = float(
                _section_value(
                    self.config,
                    "schedule",
                    "one_shot_readiness_timeout_seconds",
                    5.0,
                )
            )
        timeout_seconds = max(0.0, float(timeout_seconds))
        poll_seconds = max(0.05, float(poll_seconds))
        self.start_streams()
        attempts = max(1, int(timeout_seconds / poll_seconds) + 1)
        latest = self.bar_store.freshness(self.clock.now())
        for attempt in range(attempts):
            now = self.clock.now()
            if attempt and latest.unresolved_gap:
                self.recover_durable_market_data_gaps()
            try:
                bars = self.market_data.get_bars(
                    self.universe,
                    start=now
                    - timedelta(seconds=max(3600, self.bar_store.stale_after_seconds * 4)),
                    end=now,
                    timeframe="minute",
                )
                for bar in bars:
                    self.bar_store.ingest(bar)
                if self.bar_store.freshness(now).unresolved_gap:
                    self.bar_store.resolve_gaps_after_backfill()
                    if not self.bar_store.freshness(now).unresolved_gap:
                        self._resolve_incidents("durable_market_data_gap_recovery_incomplete")
            except Exception as exc:
                if attempt == attempts - 1:
                    self._record_incident(
                        "one_shot_readiness_failure",
                        str(exc) or type(exc).__name__,
                        severity="warning",
                    )
            latest = self.bar_store.freshness(now)
            if latest.fresh and (not require_trade_updates or self._trade_updates_healthy()):
                self._resolve_incidents("one_shot_readiness_failure")
                return latest
            if attempt + 1 < attempts:
                asyncio.run(self.clock.sleep(poll_seconds))
        return latest

    def _session_date(self, value: datetime) -> date:
        return as_utc(value).astimezone(NEW_YORK).date()

    def _performance_series_id(self) -> str:
        """Stable logical forward series across process/run-id restarts."""

        return f"{self.run_name}:{self.strategy_version}:{configuration_hash(self.config)}"

    def _before_cutoff(self, now: datetime) -> bool:
        cutoff = _parse_time(
            _section_value(self.config, "schedule", "catch_up_cutoff_et", "14:30"),
            time(14, 30),
        )
        return as_utc(now).astimezone(NEW_YORK).time().replace(tzinfo=None) <= cutoff

    @staticmethod
    def _blocking_halt_active(active: Mapping[str, Any]) -> bool:
        """Risk latches permit reductions; every other active latch blocks."""

        return any(name not in {"daily_loss", "hard_stop"} for name in active)

    def _latest_prices(self) -> dict[str, tuple[Decimal, datetime]]:
        result: dict[str, tuple[Decimal, datetime]] = {}
        now = self.clock.now()
        lookback = timedelta(seconds=max(3600, self.bar_store.stale_after_seconds * 4))
        bars = self.market_data.get_bars(
            self.universe,
            start=now - lookback,
            end=now,
            timeframe="minute",
        )
        for bar in bars:
            current = result.get(bar.symbol)
            if current is None or bar.start >= current[1]:
                result[bar.symbol] = (bar.close, bar.start)
        return result

    def _apply_account_risk_latches(
        self,
        account: AccountState,
        session_date: date,
        *,
        cancellation_authorized: bool | None = None,
        mutations_authorized: bool | None = None,
    ) -> dict[str, Any]:
        """Persist risk state and independently cancel orders when authorized."""

        requested_cancellation = bool(
            mutations_authorized if cancellation_authorized is None else cancellation_authorized
        )
        # A caller boolean may only further restrict the central authority; it
        # can never grant mutation authority to an observer/dry-run service.
        cancellation_authorized = bool(
            requested_cancellation and self._paper_cancellation_authorized(dry_run=self.dry_run)
        )

        active = self.repository.active_halts(self.clock.now())
        daily_limit = Decimal(str(_section_value(self.config, "risk", "daily_loss_limit", "0.03")))
        soft_limit = Decimal(
            str(_section_value(self.config, "risk", "drawdown_soft_limit", "0.10"))
        )
        hard_limit = Decimal(
            str(_section_value(self.config, "risk", "drawdown_hard_limit", "0.15"))
        )
        triggers: list[tuple[str, Decimal, Decimal]] = []
        daily_loss = Decimal("0")
        if account.last_equity is not None and account.last_equity > 0:
            daily_loss = max(
                Decimal("0"),
                (account.last_equity - account.equity) / account.last_equity,
            )
            if daily_loss >= daily_limit and "daily_loss" not in active:
                triggers.append(("daily_loss", daily_loss, daily_limit))
        prior_high = self.repository.account_equity_high_water()
        high_water = max(account.equity, prior_high or account.equity)
        drawdown = Decimal("0")
        if high_water > 0:
            drawdown = max(Decimal("0"), (high_water - account.equity) / high_water)
            if drawdown >= hard_limit and "hard_stop" not in active:
                triggers.append(("hard_stop", drawdown, hard_limit))
        actions: list[dict[str, Any]] = []
        for latch_type, observed, limit in triggers:
            self.repository.record_halt(
                run_id=self.run_id,
                action=latch_type,
                latch_type=latch_type,
                initiator="risk_monitor",
                reason=f"{latch_type} threshold reached",
                session_date=session_date,
                created_at=self.clock.now(),
                details={"observed": str(observed), "limit": str(limit)},
            )
            active[latch_type] = {
                "latch_type": latch_type,
                "session_date": session_date.isoformat(),
            }
            actions.append(
                {
                    "control": latch_type,
                    "description": f"{latch_type} threshold reached",
                    "observed": str(observed),
                    "limit": str(limit),
                }
            )
        controls = {str(action["control"]) for action in actions}
        if drawdown >= soft_limit and "soft_drawdown_cap" not in controls:
            actions.append(
                {
                    "control": "soft_drawdown_cap",
                    "description": "Cap gross exposure while soft drawdown is active",
                    "observed": str(drawdown),
                    "limit": str(soft_limit),
                }
            )
        if "daily_loss" in active and "daily_loss" not in controls:
            actions.append(
                {
                    "control": "daily_loss",
                    "description": "Permit reductions only while daily-loss latch is active",
                    "observed": str(daily_loss),
                    "limit": str(daily_limit),
                }
            )
        if "hard_stop" in active and "hard_stop" not in controls:
            actions.append(
                {
                    "control": "hard_stop",
                    "description": "Replace strategy target with an all-cash reduction target",
                    "observed": str(drawdown),
                    "limit": str(hard_limit),
                }
            )
        risk_latch_active = "daily_loss" in active or "hard_stop" in active
        if cancellation_authorized and risk_latch_active:
            # Emergency cancellation is intentionally independent of data,
            # market-hours, cutoff, reconciliation and stream-health gates.
            # Reduction submissions still pass the complete normal gate.
            self.broker.cancel_all_orders()
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="orders",
                event_type="risk_cancel_all_requested",
                created_at=self.clock.now(),
                payload={
                    "daily_loss_active": "daily_loss" in active,
                    "hard_stop_active": "hard_stop" in active,
                },
            )
        result = {
            "daily_loss": daily_loss,
            "drawdown": drawdown,
            "high_water_equity": high_water,
            "daily_loss_active": "daily_loss" in active,
            "hard_stop_active": "hard_stop" in active,
            "soft_drawdown_active": drawdown >= soft_limit,
            "manual_halt_active": self._blocking_halt_active(active),
            "actions": tuple(actions),
        }
        self._last_risk_metrics = result
        return result

    def _paper_cancellation_authorized(
        self,
        *,
        dry_run: bool,
    ) -> bool:
        """Authorize only emergency cancellations in an explicit paper mode."""

        if (
            self.mode is RunMode.REPLAY
            and self.allow_simulated_replay_orders
            and isinstance(self.broker, FakePaperBroker)
        ):
            return not dry_run
        values = os.environ if self.environment is None else self.environment
        authority = self._paper_authority_gate()
        return bool(
            self.mode in {RunMode.PAPER_ONCE, RunMode.PAPER_RUN}
            and not dry_run
            and self.broker.paper_only
            and bool(_section_value(self.config, "execution", "paper_only", True))
            and bool(
                _section_value(
                    self.config,
                    "execution",
                    "paper_order_submission_enabled",
                    False,
                )
            )
            and values.get(PAPER_ORDER_ENABLEMENT_ENV) == PAPER_ORDER_ACKNOWLEDGEMENT
            and all(authority.values())
        )

    def _expire_daily_loss_after_clean_reconciliation(
        self,
        *,
        session_date: date,
        now: datetime,
        reconciliation: ReconciliationResult,
    ) -> str | None:
        session = self._market_session(session_date)
        if (
            session is None
            or now < session.open_at
            or not reconciliation.clean
            or reconciliation.blocking
        ):
            return None
        return self.repository.expire_daily_loss_latch(
            run_id=self.run_id,
            next_session=session_date,
            created_at=now,
            reconciliation_id=reconciliation.reconciliation_id,
        )

    def _risk_adjusted_target(
        self,
        target: Mapping[str, float],
        risk_state: Mapping[str, Any],
    ) -> tuple[dict[str, float], tuple[str, ...]]:
        if bool(risk_state.get("hard_stop_active")):
            return {}, ("hard-stop all-cash reduction target",)
        adjusted = {str(symbol): float(weight) for symbol, weight in target.items()}
        warnings: list[str] = []
        if bool(risk_state.get("soft_drawdown_active")):
            cap = float(
                _section_value(
                    self.config,
                    "risk",
                    "soft_limit_max_gross_exposure",
                    0.50,
                )
            )
            gross = sum(adjusted.values())
            if gross > cap and gross > 0:
                scale = cap / gross
                adjusted = {symbol: weight * scale for symbol, weight in adjusted.items()}
                warnings.append("soft-drawdown exposure cap applied")
        return adjusted, tuple(warnings)

    def _operational_risk_context(
        self,
        *,
        risk_state: Mapping[str, Any],
        account: AccountState,
        positions: Sequence[Any],
        open_orders: Sequence[Any],
        freshness: DataFreshnessState,
        market_clock: Any,
        reconciliation: ReconciliationResult,
        asset_validation: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Return the complete live evidence used by the operational risk overlay."""

        active_halts = self.repository.active_halts(now)
        return {
            **risk_state,
            "account": account,
            "positions": tuple(positions),
            "open_orders": tuple(
                {
                    "client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "status": order.status,
                    "submitted_at": order.submitted_at,
                    "updated_at": order.updated_at,
                }
                for order in open_orders
            ),
            "freshness": freshness,
            "market_clock": market_clock,
            "market_open": bool(market_clock.is_open),
            "before_strategy_cutoff": self._before_cutoff(now),
            "asset_validation": dict(asset_validation),
            "reconciliation": {
                "reconciliation_id": reconciliation.reconciliation_id,
                "started_at": reconciliation.started_at,
                "completed_at": reconciliation.completed_at,
                "clean": reconciliation.clean,
                "blocking": reconciliation.blocking,
                "discrepancies": reconciliation.discrepancies,
            },
            "active_halts": tuple(sorted(active_halts)),
            "halt_state": {
                "manual_or_operator_active": self._blocking_halt_active(active_halts),
                "daily_loss_active": "daily_loss" in active_halts,
                "hard_stop_active": "hard_stop" in active_halts,
            },
            "limits": {
                "daily_loss": _section_value(self.config, "risk", "daily_loss_limit", "0.03"),
                "soft_drawdown": _section_value(self.config, "risk", "drawdown_soft_limit", "0.10"),
                "hard_drawdown": _section_value(self.config, "risk", "drawdown_hard_limit", "0.15"),
                "soft_max_gross_exposure": _section_value(
                    self.config,
                    "risk",
                    "soft_limit_max_gross_exposure",
                    "0.50",
                ),
            },
        }

    def _call_target_provider_with_risk_context(
        self,
        *,
        now: datetime,
        account: AccountState,
        positions: Sequence[Any],
        risk_state: Mapping[str, Any],
        open_orders: Sequence[Any],
        latest_prices: Mapping[str, tuple[Decimal, datetime]],
        freshness: DataFreshnessState,
        market_clock: Any,
        asset_validation: Mapping[str, Any],
    ) -> Mapping[str, float]:
        """Invoke a target provider with the actual durable account-risk state."""

        if self.target_provider is None:
            raise RuntimeError("target provider is unavailable")
        # RiskEngine represents losses/drawdowns as negative returns, whereas
        # LiveService exposes positive loss magnitudes to operations.
        engine_context = {
            "current_drawdown": -float(risk_state.get("drawdown", 0)),
            "current_daily_loss": -float(risk_state.get("daily_loss", 0)),
            "hard_stop_latched": bool(risk_state.get("hard_stop_active")),
            "daily_loss_latched": bool(risk_state.get("daily_loss_active")),
            "halt_latched": bool(risk_state.get("manual_halt_active")),
        }
        if freshness.fresh:
            freshness_state = "fresh"
        elif freshness.unresolved_gap:
            freshness_state = "unresolved_gap"
        elif freshness.missing_symbols:
            freshness_state = "missing_symbols"
        elif freshness.stale_symbols:
            freshness_state = "stale"
        else:
            freshness_state = "stream_unhealthy"
        asset_metadata: dict[str, dict[str, Any]] = {}
        for symbol, details in sorted(asset_validation.items()):
            detail_values = details if isinstance(details, Mapping) else {}
            asset = detail_values.get("asset")
            asset_metadata[symbol] = {
                "valid": bool(detail_values.get("valid", False)),
                "reasons": tuple(str(reason) for reason in detail_values.get("reasons", ())),
                "asset_class": getattr(asset, "asset_class", None),
                "exchange": getattr(asset, "exchange", None),
                "active": getattr(asset, "active", None),
                "tradable": getattr(asset, "tradable", None),
                "fractionable": getattr(asset, "fractionable", None),
            }
        cutoff = _parse_time(
            _section_value(self.config, "schedule", "catch_up_cutoff_et", "14:30"),
            time(14, 30),
        )
        evaluation_cutoff = datetime.combine(
            as_utc(now).astimezone(NEW_YORK).date(),
            cutoff,
            tzinfo=NEW_YORK,
        ).astimezone(UTC)
        provider_context = {
            **engine_context,
            "account_timestamp": account.timestamp.isoformat(),
            "positions": tuple(
                {
                    "symbol": position.symbol,
                    "quantity": float(position.quantity),
                    "market_value": float(position.market_value),
                    "current_price": (
                        None
                        if getattr(position, "current_price", None) is None
                        else float(position.current_price)
                    ),
                    "timestamp": position.timestamp.isoformat(),
                }
                for position in positions
            ),
            "open_order_symbols": tuple(sorted({str(order.symbol) for order in open_orders})),
            "open_orders": tuple(
                {
                    "client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "status": order.status,
                    "requested_notional": (
                        None
                        if order.requested_notional is None
                        else float(order.requested_notional)
                    ),
                    "requested_quantity": (
                        None
                        if order.requested_quantity is None
                        else float(order.requested_quantity)
                    ),
                    "filled_quantity": float(order.filled_quantity),
                    "updated_at": order.updated_at.isoformat(),
                }
                for order in open_orders
            ),
            "current_prices": {
                symbol: float(price) for symbol, (price, _) in sorted(latest_prices.items())
            },
            "current_price_timestamps": {
                symbol: timestamp.isoformat()
                for symbol, (_, timestamp) in sorted(latest_prices.items())
            },
            "asset_eligibility": {
                symbol: bool(
                    details.get("valid", False) if isinstance(details, Mapping) else details
                )
                for symbol, details in sorted(asset_validation.items())
            },
            "asset_metadata": asset_metadata,
            "market_timestamp": market_clock.timestamp.isoformat(),
            "next_market_open": market_clock.next_open.isoformat(),
            "next_market_close": market_clock.next_close.isoformat(),
            "evaluation_cutoff": evaluation_cutoff.isoformat(),
            "data_freshness_state": freshness_state,
            "market_state": "open" if market_clock.is_open else "closed",
            "halt_state": (
                "manual_or_operator" if bool(risk_state.get("manual_halt_active")) else "clear"
            ),
        }
        setter = getattr(self.target_provider, "set_live_risk_context", None)
        if callable(setter):
            setter(**provider_context)
            return self.target_provider(now, account, positions)

        provider: Any = self.target_provider
        risk_engine = getattr(provider, "_risk", None)
        if risk_engine is None or not callable(getattr(risk_engine, "evaluate", None)):
            return self.target_provider(now, account, positions)
        proxy = _LiveRiskEngineProxy(risk_engine, engine_context)
        try:
            provider._risk = proxy
        except (AttributeError, TypeError):
            return self.target_provider(now, account, positions)
        try:
            return self.target_provider(now, account, positions)
        finally:
            provider._risk = risk_engine

    @staticmethod
    def _risk_metadata_with_actual_context(
        metadata: Mapping[str, Any] | None,
        risk_state: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if metadata is None:
            return None
        result = dict(metadata)
        risk_decision = result.get("risk_decision")
        if isinstance(risk_decision, Mapping):
            result["risk_decision"] = {
                **risk_decision,
                "current_drawdown": -float(risk_state.get("drawdown", 0)),
                "operational_drawdown_magnitude": risk_state.get("drawdown"),
                "operational_daily_loss_magnitude": risk_state.get("daily_loss"),
                "durable_high_water_equity": risk_state.get("high_water_equity"),
            }
        return result

    @staticmethod
    def _deduplicate_operational_risk_actions(
        metadata: Mapping[str, Any] | None,
        actions: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        engine_actions = () if metadata is None else metadata.get("risk_actions", ())
        engine_controls = {
            str(action.get("control")) for action in engine_actions if isinstance(action, Mapping)
        }
        equivalent = {
            "soft_drawdown_cap": "soft_drawdown",
            "hard_stop": "hard_drawdown",
            "daily_loss": "daily_loss",
        }
        return tuple(
            action
            for action in actions
            if equivalent.get(str(action.get("control"))) not in engine_controls
        )

    @staticmethod
    def _reductions_only(planning: PlanningResult) -> PlanningResult:
        skipped = list(planning.skipped)
        for intent in planning.buys:
            skipped.append(
                {
                    "symbol": intent.symbol,
                    "reason": "daily_loss_reductions_only",
                }
            )
        return PlanningResult(intents=planning.sells, skipped=tuple(skipped))

    def _benchmark_performance(
        self,
        *,
        session: MarketSession | None,
        now: datetime,
    ) -> dict[str, Any]:
        benchmark_section = getattr(self.config, "universe", None) or getattr(
            self.config, "data", None
        )
        symbol = str(getattr(benchmark_section, "benchmark", "SPY")).upper()
        series_id = self._performance_series_id()
        base = {
            "symbol": symbol,
            "session_date": self._session_date(now).isoformat(),
            "series_id": series_id,
            "benchmark_return": None,
            "benchmark_cumulative_return": None,
        }
        if session is None:
            return {**base, "availability_reason": "not_a_market_session"}
        if now < session.close_at:
            return {**base, "availability_reason": "session_not_closed"}
        try:
            bars = sorted(
                self.market_data.get_bars(
                    (symbol,),
                    start=session.open_at - timedelta(days=10),
                    end=now,
                    timeframe="day",
                ),
                key=lambda bar: bar.start,
            )
        except Exception as exc:
            return {
                **base,
                "availability_reason": f"provider_error:{redact(str(exc))}",
            }
        completed = [
            bar
            for bar in bars
            if bar.symbol == symbol
            and bar.start.astimezone(NEW_YORK).date() <= session.session_date
        ]
        if len(completed) < 2:
            return {**base, "availability_reason": "insufficient_completed_daily_bars"}
        previous, current = completed[-2:]
        existing = self.repository.get_performance(
            run_id=self.run_id,
            session_date=session.session_date,
            table_name="benchmark_performance",
        )
        if existing is not None:
            existing_payload = dict(existing.get("payload") or {})
            if existing_payload.get("benchmark_close") == float(current.close):
                return existing_payload
        benchmark_return = float(current.close / previous.close - Decimal("1"))
        prior = self.repository.latest_performance(
            "benchmark_performance",
            series_id=series_id,
        )
        prior_payload = {} if prior is None else dict(prior.get("payload") or {})
        if prior_payload.get(
            "session_date"
        ) == session.session_date.isoformat() and prior_payload.get("benchmark_close") == float(
            current.close
        ):
            return prior_payload
        previous_cumulative = float(prior_payload.get("benchmark_cumulative_return") or 0.0)
        cumulative = (1.0 + previous_cumulative) * (1.0 + benchmark_return) - 1.0
        return {
            **base,
            "benchmark_return": benchmark_return,
            "benchmark_cumulative_return": cumulative,
            "benchmark_close": float(current.close),
            "availability_reason": None,
        }

    def _record_forward_performance(
        self,
        *,
        account: AccountState,
        positions: Sequence[Any],
        session_date: date,
        now: datetime,
    ) -> Mapping[str, Any]:
        series_id = self._performance_series_id()
        existing = self.repository.get_performance(
            run_id=self.run_id,
            session_date=session_date,
        )
        existing_payload = {} if existing is None else dict(existing.get("payload") or {})
        previous_performance = self.repository.latest_performance(
            "daily_performance",
            series_id=series_id,
        )
        previous_payload = (
            {} if previous_performance is None else dict(previous_performance.get("payload") or {})
        )
        if (
            not existing_payload
            and previous_payload.get("session_date") == session_date.isoformat()
        ):
            # A process restart creates a new application run_id, but not a new
            # logical forward series or trading session.
            existing_payload = dict(previous_payload)
        last_snapshot_raw = existing_payload.get("account_snapshot_time")
        if last_snapshot_raw is not None:
            last_snapshot = as_utc(datetime.fromisoformat(str(last_snapshot_raw)))
            if account.timestamp <= last_snapshot:
                benchmark = self._benchmark_performance(
                    session=self._market_session(session_date),
                    now=now,
                )
                metrics = {
                    **existing_payload,
                    "series_id": series_id,
                    "benchmark_return": benchmark.get("benchmark_return"),
                    "benchmark_cumulative_return": benchmark.get("benchmark_cumulative_return"),
                }
                self.repository.record_daily_performance(
                    run_id=self.run_id,
                    session_date=session_date,
                    metrics=metrics,
                    created_at=now,
                )
                self.repository.record_benchmark_performance(
                    run_id=self.run_id,
                    session_date=session_date,
                    metrics=benchmark,
                    created_at=now,
                )
                return metrics
        prior_account = self.repository.latest_account_state(before=account.timestamp)
        external_flow = Decimal("0")
        interval_pnl = Decimal("0")
        interval_return: Decimal | None = Decimal("0")
        continuity = "baseline"
        return_unavailable_reason: str | None = None
        start_equity = account.equity
        if prior_account is not None:
            prior_equity = Decimal(str(prior_account["equity"]))
            prior_cash = Decimal(str(prior_account["cash"]))
            fill_effect = self.repository.fill_cash_effect(
                after=prior_account["timestamp"],
                through=account.timestamp,
            )
            external_flow = account.cash - prior_cash - fill_effect
            if abs(external_flow) <= Decimal("0.01"):
                external_flow = Decimal("0")
                continuity = "continuous"
            else:
                continuity = "external_cash_flow_discontinuity"
            interval_pnl = account.equity - prior_equity - external_flow
            if external_flow:
                # Deposit/withdrawal timing inside the interval is unknown, so
                # a denominator-based return would be fabricated.  Begin a new
                # segment at this observed post-flow equity instead.
                interval_return = None
                return_unavailable_reason = "external_cash_flow_timing_unknown"
                start_equity = account.equity
            elif prior_equity > 0:
                interval_return = interval_pnl / prior_equity
                start_equity = Decimal(str(existing_payload.get("start_equity", prior_equity)))
        prior_daily_raw = existing_payload.get("daily_return", "0")
        prior_daily_return = (
            Decimal("0") if prior_daily_raw is None else Decimal(str(prior_daily_raw))
        )
        daily_return = (
            None
            if interval_return is None
            else (Decimal("1") + prior_daily_return) * (Decimal("1") + interval_return)
            - Decimal("1")
        )
        if external_flow:
            # The post-flow snapshot begins a new segment.  Return and P&L
            # accumulators from the prior segment must not leak into it.
            daily_pnl = interval_pnl
            total_external_flow = external_flow
        else:
            daily_pnl = Decimal(str(existing_payload.get("daily_pnl", "0"))) + interval_pnl
            total_external_flow = Decimal(str(existing_payload.get("external_cash_flow", "0")))
        prior_cumulative_raw = existing_payload.get(
            "cumulative_return",
            previous_payload.get("cumulative_return", "0"),
        )
        prior_cumulative = (
            Decimal("0") if prior_cumulative_raw is None else Decimal(str(prior_cumulative_raw))
        )
        cumulative_return = (
            Decimal("0")
            if interval_return is None
            else (Decimal("1") + prior_cumulative) * (Decimal("1") + interval_return) - Decimal("1")
        )
        prior_segment = int(
            existing_payload.get("segment_id", previous_payload.get("segment_id", 1))
        )
        segment_id = prior_segment + (1 if external_flow else 0)
        high_water = max(
            account.equity,
            self.repository.account_equity_high_water() or account.equity,
        )
        drawdown = Decimal("0") if high_water <= 0 else (high_water - account.equity) / high_water
        gross_value = sum(
            (abs(Decimal(str(position.market_value))) for position in positions),
            Decimal("0"),
        )
        gross_exposure = Decimal("0") if account.equity <= 0 else gross_value / account.equity
        cash_allocation = Decimal("0") if account.equity <= 0 else account.cash / account.equity
        session = self._market_session(session_date)
        benchmark = self._benchmark_performance(session=session, now=now)
        metrics = {
            "series_id": series_id,
            "session_date": session_date.isoformat(),
            "account_snapshot_time": account.timestamp,
            "period_start": (
                account.timestamp if prior_account is None else prior_account["timestamp"]
            ),
            "period_end": account.timestamp,
            "start_equity": start_equity,
            "end_equity": account.equity,
            "external_cash_flow": total_external_flow,
            "daily_pnl": daily_pnl,
            "daily_return": daily_return,
            "cumulative_return": cumulative_return,
            "drawdown": drawdown,
            "gross_exposure": gross_exposure,
            "cash_allocation": cash_allocation,
            "turnover": None,
            "regime": None,
            "continuity_flag": continuity,
            "return_unavailable_reason": return_unavailable_reason,
            "segment_id": segment_id,
            "benchmark_return": benchmark.get("benchmark_return"),
            "benchmark_cumulative_return": benchmark.get("benchmark_cumulative_return"),
        }
        self.repository.record_daily_performance(
            run_id=self.run_id,
            session_date=session_date,
            metrics=metrics,
            created_at=now,
        )
        self.repository.record_benchmark_performance(
            run_id=self.run_id,
            session_date=session_date,
            metrics=benchmark,
            created_at=now,
        )
        return metrics

    def _replay_gate(
        self,
        *,
        freshness: DataFreshnessState,
        reconciliation: ReconciliationResult,
        market_open: bool,
        before_cutoff: bool,
        halt_active: bool,
        dry_run: bool,
    ) -> SubmissionGateResult:
        reasons: list[str] = []
        if dry_run:
            reasons.append("dry-run mode never submits orders")
        if not market_open:
            reasons.append("regular US equity market is closed")
        if not before_cutoff:
            reasons.append("the configured strategy-order cutoff has passed")
        if not freshness.fresh:
            reasons.append("required market data is not fresh and healthy")
        if reconciliation.blocking:
            reasons.append("the latest reconciliation is blocking")
        if halt_active:
            reasons.append("a persistent halt latch is active")
        return SubmissionGateResult(
            allowed=not reasons,
            reasons=tuple(reasons),
            effective_mode="replay_simulation" if not reasons else "observer",
        )

    def _persist_decision_metadata(
        self,
        *,
        decision_id: str,
        metadata: Mapping[str, Any] | None,
        as_of_at: datetime,
        operational_final_target: Mapping[str, float] | None = None,
        operational_actions: Sequence[Mapping[str, Any]] = (),
        operational_risk_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Project opaque decision-engine metadata into immutable audit facts."""

        if metadata is None and operational_final_target is None and not operational_actions:
            return
        metadata = {} if metadata is None else metadata
        common = {
            "status": metadata.get("status"),
            "signal_cutoff": metadata.get("cutoff"),
            "evaluated_at": metadata.get("evaluated_at"),
            "error": metadata.get("error"),
        }
        fact_payloads = {
            "strategy_signals": {
                **common,
                "strategy_outputs": metadata.get("strategy_outputs", {}),
            },
            "regime_states": {
                **common,
                "regime": metadata.get("regime"),
            },
            "allocation_results": {
                **common,
                "allocation": metadata.get("allocation"),
                "proposed_target": (
                    metadata.get("risk_decision", {}).get("proposed_weights")
                    if isinstance(metadata.get("risk_decision"), Mapping)
                    else None
                ),
                "final_target": (
                    operational_final_target
                    if operational_final_target is not None
                    else metadata.get("final_target")
                ),
            },
            "risk_decisions": {
                **common,
                "risk_decision": metadata.get("risk_decision"),
                "engine_risk_actions": metadata.get("risk_actions", ()),
                "operational_risk_actions": tuple(operational_actions),
                "operational_risk_state": operational_risk_state,
                "operational_final_target": operational_final_target,
                "final_target": (
                    operational_final_target
                    if operational_final_target is not None
                    else metadata.get("final_target")
                ),
            },
        }
        for table_name, payload in fact_payloads.items():
            sanitized_payload = _sanitize_audit_value(payload)
            if not isinstance(sanitized_payload, Mapping):
                raise TypeError("Sanitized decision fact must remain a mapping")
            self.repository.append_fact(
                table_name,
                run_id=self.run_id,
                decision_id=decision_id,
                payload=sanitized_payload,
                as_of_at=as_of_at,
            )
        strategy_outputs = metadata.get("strategy_outputs")
        if isinstance(strategy_outputs, Mapping):
            for name, raw in strategy_outputs.items():
                if not isinstance(raw, Mapping):
                    continue
                self.repository.record_strategy_version(
                    run_id=self.run_id,
                    strategy_name=str(raw.get("name") or name),
                    version=str(raw.get("version") or "unknown"),
                    metadata_values={"as_of_date": raw.get("as_of_date")},
                )
        actions = metadata.get("risk_actions")
        normalized_actions = (
            tuple(action for action in actions if isinstance(action, Mapping))
            if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes))
            else ()
        )
        normalized_actions = (*normalized_actions, *operational_actions)
        if normalized_actions:
            self.repository.append_risk_actions(
                run_id=self.run_id,
                decision_id=decision_id,
                actions=normalized_actions,
                created_at=as_of_at,
            )

    def run_once(
        self,
        target_weights: Mapping[str, float] | None = None,
        *,
        dry_run: bool = False,
        force: bool = False,
        session_date: date | None = None,
        scheduled_at: datetime | None = None,
    ) -> LiveCycleResult:
        """Run one cycle and durably close every decision claimed by this process."""

        self._active_cycle_context = None
        try:
            return self._run_once_impl(
                target_weights,
                dry_run=dry_run,
                force=force,
                session_date=session_date,
                scheduled_at=scheduled_at,
            )
        except Exception as exc:
            context = self._active_cycle_context
            if context is None:
                raise
            reason = f"decision cycle failed safely: {redact(str(exc) or type(exc).__name__)}"
            incident_id = self._record_incident(
                "decision_cycle_failure",
                reason,
                details={"decision_id": context["decision_id"]},
            )
            decision_context = {
                **context,
                "warnings": (reason,),
                "incidents": (incident_id,),
            }
            result = LiveCycleResult(
                decision_id=str(context["decision_id"]),
                session_date=context["session_date"],
                claimed=True,
                status="rejected",
                gate=SubmissionGateResult(
                    allowed=False,
                    reasons=(reason,),
                    effective_mode="observer",
                ),
                planning=None,
                reconciliation=None,
                decision_context=decision_context,
                skip_reason=reason,
            )
            payload = _payload(result)
            if not self.repository.has_decision_receipt(result.decision_id):
                self.repository.store_decision_receipt(
                    run_id=self.run_id,
                    decision_id=result.decision_id,
                    payload=payload,
                )
            self.repository.complete_rebalance(
                result.decision_id,
                status=result.status,
                payload=payload,
                skip_reason=reason,
            )
            return result
        finally:
            self._active_cycle_context = None

    def _run_once_impl(
        self,
        target_weights: Mapping[str, float] | None = None,
        *,
        dry_run: bool = False,
        force: bool = False,
        session_date: date | None = None,
        scheduled_at: datetime | None = None,
    ) -> LiveCycleResult:
        """Run one restart-safe decision cycle; normal paths submit at most once."""

        del force  # Idempotency remains mandatory even for manual catch-up.
        dry_run = bool(dry_run or self.dry_run)
        now = self.clock.now()
        decision_scheduled_at = now if scheduled_at is None else as_utc(scheduled_at)
        broker_clock = self.broker.get_clock()
        effective_date = session_date or self._session_date(broker_clock.timestamp)
        requires_trade_updates = not dry_run and (
            self.mode in {RunMode.PAPER_ONCE, RunMode.PAPER_RUN}
            or self.allow_simulated_replay_orders
        )
        freshness = self.ensure_ready(require_trade_updates=requires_trade_updates)
        trade_updates_ready = not requires_trade_updates or self._trade_updates_healthy()
        if not freshness.fresh or not trade_updates_ready:
            # Account-risk cancellation remains available even when market data
            # or the trade stream is unfit for any new reduction submission.
            early_account = self.broker.get_account()
            self._apply_account_risk_latches(
                early_account,
                effective_date,
                cancellation_authorized=self._paper_cancellation_authorized(dry_run=dry_run),
            )
            reason = (
                "paper trade-update stream is not ready; no decision was claimed"
                if freshness.fresh and not trade_updates_ready
                else "market streams/history are not fresh; no decision was claimed"
            )
            return LiveCycleResult(
                decision_id="",
                session_date=effective_date,
                claimed=False,
                status="waiting_for_fresh_data",
                gate=SubmissionGateResult(
                    allowed=False,
                    reasons=(reason,),
                    effective_mode="observer",
                ),
                planning=None,
                reconciliation=None,
                decision_context={
                    "run_id": self.run_id,
                    "configuration_hash": configuration_hash(self.config),
                    "strategy_version": self.strategy_version,
                    "scheduled_at": decision_scheduled_at,
                    "actual_at": self.clock.now(),
                    "mode": self.mode.value,
                    "market": broker_clock,
                    "feed": self.market_data.feed,
                    "freshness": freshness,
                    "warnings": (reason,),
                    "incidents": tuple(self._incident_ids),
                },
                skip_reason=reason,
            )
        account = self.broker.get_account()
        risk_state = self._apply_account_risk_latches(
            account,
            effective_date,
            cancellation_authorized=self._paper_cancellation_authorized(dry_run=dry_run),
        )
        idempotency_key = (
            f"{self.idempotency_namespace}:{self.strategy_version}:{effective_date.isoformat()}"
        )
        decision_id, claimed = self.repository.claim_rebalance(
            run_id=self.run_id,
            idempotency_key=idempotency_key,
            session_date=effective_date,
            strategy_version=self.strategy_version,
            mode=self.mode.value,
            scheduled_at=decision_scheduled_at,
        )
        if not claimed:
            prior = self.repository.get_rebalance(decision_id)
            return LiveCycleResult(
                decision_id=decision_id,
                session_date=effective_date,
                claimed=False,
                status="duplicate",
                gate=None,
                planning=None,
                reconciliation=None,
                skip_reason=None if prior is None else str(prior.get("skip_reason") or "") or None,
            )

        self._active_cycle_context = {
            "decision_id": decision_id,
            "session_date": effective_date,
            "run_id": self.run_id,
            "configuration_hash": configuration_hash(self.config),
            "strategy_version": self.strategy_version,
            "scheduled_at": decision_scheduled_at,
            "actual_at": self.clock.now(),
            "mode": self.mode.value,
            "market": broker_clock,
            "feed": self.market_data.feed,
            "freshness": freshness,
        }
        positions = list(self.broker.get_positions())
        validation_errors: list[str] = []
        asset_validation: dict[str, Any] = {}
        for symbol in self.universe:
            try:
                asset = self.broker.get_asset(symbol)
                valid, reasons = validate_asset(asset, self.universe)
                asset_validation[symbol] = {
                    "valid": valid,
                    "reasons": tuple(reasons),
                    "asset": asset,
                }
            except Exception as exc:
                valid = False
                reasons = (redact(str(exc) or type(exc).__name__),)
                asset_validation[symbol] = {
                    "valid": False,
                    "reasons": reasons,
                    "asset": None,
                }
            if not valid:
                validation_errors.extend(f"{symbol}: {reason}" for reason in reasons)

        risk_was_active = bool(risk_state["daily_loss_active"] or risk_state["hard_stop_active"])
        reconciliation = self.reconcile()
        self._expire_daily_loss_after_clean_reconciliation(
            session_date=effective_date,
            now=now,
            reconciliation=reconciliation,
        )
        # Reconciliation and emergency cancellation can change holdings, cash,
        # and open orders.  Never plan from the pre-reconciliation snapshots.
        account = self.broker.get_account()
        positions = list(self.broker.get_positions())
        open_orders = list(self.broker.get_orders(include_closed=False))
        risk_state = self._apply_account_risk_latches(
            account,
            effective_date,
            cancellation_authorized=False,
        )
        risk_now_active = bool(risk_state["daily_loss_active"] or risk_state["hard_stop_active"])
        if risk_now_active and not risk_was_active:
            # A breach first visible in the refreshed account gets the same
            # immediate paper-order cancellation, followed by fresh evidence.
            risk_state = self._apply_account_risk_latches(
                account,
                effective_date,
                cancellation_authorized=self._paper_cancellation_authorized(dry_run=dry_run),
            )
            reconciliation = self.reconcile()
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
            open_orders = list(self.broker.get_orders(include_closed=False))
            risk_state = self._apply_account_risk_latches(
                account,
                effective_date,
                cancellation_authorized=False,
            )
        performance = self._record_forward_performance(
            account=account,
            positions=positions,
            session_date=effective_date,
            now=now,
        )
        active_before_risk = self.repository.active_halts(now)
        manual_halt_active = self._blocking_halt_active(active_before_risk)
        prices = self._latest_prices()
        selected_weights = target_weights
        decision_metadata: Mapping[str, Any] | None = None
        target_error: str | None = None
        if selected_weights is None and self.target_provider is not None:
            try:
                selected_weights = self._call_target_provider_with_risk_context(
                    now=now,
                    account=account,
                    positions=positions,
                    risk_state=risk_state,
                    open_orders=open_orders,
                    latest_prices=prices,
                    freshness=freshness,
                    market_clock=broker_clock,
                    asset_validation=asset_validation,
                )
            except Exception as exc:
                target_error = redact(str(exc) or type(exc).__name__)
                selected_weights = None
            raw_metadata = getattr(self.target_provider, "last_metadata", None)
            if isinstance(raw_metadata, Mapping):
                decision_metadata = self._risk_metadata_with_actual_context(
                    raw_metadata,
                    risk_state,
                )
        # Refresh once more immediately before planning.  A target provider may
        # perform nontrivial work, while fills/cancels continue asynchronously.
        risk_active_before_planning = bool(
            risk_state["daily_loss_active"] or risk_state["hard_stop_active"]
        )
        account = self.broker.get_account()
        positions = list(self.broker.get_positions())
        open_orders = list(self.broker.get_orders(include_closed=False))
        broker_clock = self.broker.get_clock()
        freshness = self.bar_store.freshness(now)
        risk_state = self._apply_account_risk_latches(
            account,
            effective_date,
            cancellation_authorized=False,
        )
        risk_active_at_planning = bool(
            risk_state["daily_loss_active"] or risk_state["hard_stop_active"]
        )
        if risk_active_at_planning and not risk_active_before_planning:
            self._apply_account_risk_latches(
                account,
                effective_date,
                cancellation_authorized=self._paper_cancellation_authorized(dry_run=dry_run),
            )
            reconciliation = self.reconcile()
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
            open_orders = list(self.broker.get_orders(include_closed=False))
            broker_clock = self.broker.get_clock()
            freshness = self.bar_store.freshness(now)
            risk_state = self._apply_account_risk_latches(
                account,
                effective_date,
                cancellation_authorized=False,
            )
        operational_actions = self._deduplicate_operational_risk_actions(
            decision_metadata,
            tuple(risk_state.get("actions", ())),
        )
        if bool(risk_state["hard_stop_active"]):
            selected_weights = {}
        target_missing = selected_weights is None
        risk_warnings: tuple[str, ...] = ()
        if selected_weights is not None:
            selected_weights, risk_warnings = self._risk_adjusted_target(
                selected_weights,
                risk_state,
            )
        # Refresh prices along with the final account/position snapshot used
        # by the planner; no timestamp from strategy evaluation is assumed.
        prices = self._latest_prices()
        operational_risk_state = self._operational_risk_context(
            risk_state=risk_state,
            account=account,
            positions=positions,
            open_orders=open_orders,
            freshness=freshness,
            market_clock=broker_clock,
            reconciliation=reconciliation,
            asset_validation=asset_validation,
            now=now,
        )
        self._persist_decision_metadata(
            decision_id=decision_id,
            metadata=decision_metadata,
            as_of_at=now,
            operational_final_target=selected_weights,
            operational_actions=operational_actions,
            operational_risk_state=operational_risk_state,
        )

        planning: PlanningResult | None = None
        if not validation_errors and selected_weights is not None:
            planning = self.planner.plan(
                decision_id=decision_id,
                session_date=effective_date,
                created_at=now,
                target_weights=selected_weights,
                account=account,
                positions=positions,
                latest_prices=prices,
                universe=self.universe,
                open_orders=open_orders,
            )
            if bool(risk_state["daily_loss_active"]) and not bool(risk_state["hard_stop_active"]):
                planning = self._reductions_only(planning)

        if self.allow_simulated_replay_orders:
            gate = self._replay_gate(
                freshness=freshness,
                reconciliation=reconciliation,
                market_open=broker_clock.is_open,
                before_cutoff=self._before_cutoff(now),
                halt_active=manual_halt_active,
                dry_run=dry_run,
            )
        else:
            gate = evaluate_order_enablement(
                self.config,
                mode=self.mode,
                environment=self.environment,
                broker_paper_only=self.broker.paper_only,
                **self._paper_authority_gate(),
                account=account,
                market_clock=broker_clock,
                freshness=freshness,
                reconciliation_clean=not reconciliation.blocking,
                halt_active=manual_halt_active,
                before_cutoff=self._before_cutoff(now),
                dry_run=dry_run,
            )
        gate = self._apply_trade_stream_gate(
            gate,
            submission_expected=planning is not None and bool(planning.intents),
        )
        if validation_errors:
            gate = SubmissionGateResult(
                allowed=False,
                reasons=tuple((*gate.reasons, *validation_errors)),
                effective_mode="observer",
            )
        if target_missing:
            target_reason = (
                f"strategy target rejected: {target_error}"
                if target_error is not None
                else "strategy target is unavailable"
            )
            gate = SubmissionGateResult(
                allowed=False,
                reasons=tuple((*gate.reasons, target_reason)),
                effective_mode="observer",
            )

        receipt_before_intents = bool(
            planning is not None
            and planning.intents
            and (gate.allowed or self.mode is RunMode.OBSERVE or dry_run)
        )
        if receipt_before_intents and planning is not None:
            submission_authorized = bool(
                gate.allowed and self.mode is not RunMode.OBSERVE and not dry_run
            )
            pre_submission_status = (
                "submission_authorized" if submission_authorized else "observation_planned"
            )
            current_target_before_submission = {
                position.symbol: (
                    Decimal("0") if account.equity <= 0 else position.market_value / account.equity
                )
                for position in positions
            }
            pre_submission_context = {
                **(self._active_cycle_context or {}),
                "actual_at": self.clock.now(),
                "account": account,
                "account_snapshot_time": account.timestamp,
                "positions": tuple(positions),
                "current_drawdown": risk_state["drawdown"],
                "current_daily_loss": risk_state["daily_loss"],
                "operational_risk_state": operational_risk_state,
                "current_target": current_target_before_submission,
                "current_cash_weight": (
                    Decimal("0") if account.equity <= 0 else account.cash / account.equity
                ),
                "target_cash_weight": (
                    None
                    if selected_weights is None
                    else Decimal("1")
                    - sum(
                        (Decimal(str(value)) for value in selected_weights.values()),
                        Decimal("0"),
                    )
                ),
                "final_target": selected_weights,
                "risk_actions": (
                    *(
                        tuple(decision_metadata.get("risk_actions") or ())
                        if decision_metadata is not None
                        else ()
                    ),
                    *operational_actions,
                ),
                "warnings": ("broker outcomes not yet known",),
                "incidents": tuple(self._incident_ids),
                "order_outcomes": self._order_outcomes(planning),
            }
            pre_submission_result = LiveCycleResult(
                decision_id=decision_id,
                session_date=effective_date,
                claimed=True,
                status=pre_submission_status,
                gate=gate,
                planning=planning,
                reconciliation=reconciliation,
                decision_metadata=decision_metadata,
                decision_context=pre_submission_context,
            )
            pre_submission_payload = _payload(pre_submission_result)
            pre_submission_payload["execution_phase"] = (
                "authorized_not_yet_submitted"
                if submission_authorized
                else "hypothetical_not_submitted"
            )
            self.repository.store_decision_receipt(
                run_id=self.run_id,
                decision_id=decision_id,
                payload=pre_submission_payload,
            )
            self.repository.complete_rebalance(
                decision_id,
                status=pre_submission_status,
                payload=pre_submission_payload,
            )

        hypothetical: tuple[str, ...] = ()
        if planning is not None and (self.mode is RunMode.OBSERVE or dry_run):
            hypothetical = self.repository.record_hypothetical_order_intents(
                run_id=self.run_id,
                intents=planning.intents,
                mode="dry_run" if dry_run else "observe",
            )

        submitted: tuple[str, ...] = ()
        ambiguous = False
        if gate.allowed and planning is not None:
            results = self.order_manager.execute(
                planning.intents,
                reconcile_after_sells=self.reconcile,
            )
            submitted = tuple(order.client_order_id for order in results if order is not None)
            ambiguous = any(order is None for order in results)
            pending_buys = tuple(
                intent.client_order_id
                for intent in planning.buys
                if self.repository.get_order(intent.client_order_id) is None
            )
            if ambiguous:
                reconciliation = self.reconcile()
                status = "submission_unknown"
            elif pending_buys:
                status = "execution_pending"
            else:
                status = "submitted" if submitted else "no_orders"
            skip_reason = None
        else:
            status = "rejected" if target_error is not None else "observed"
            skip_reason = "; ".join(gate.reasons) or "submission disabled"
        current_target = {
            position.symbol: (
                Decimal("0") if account.equity <= 0 else position.market_value / account.equity
            )
            for position in positions
        }
        risk_decision = (
            decision_metadata.get("risk_decision") if decision_metadata is not None else None
        )
        turnover = (
            risk_decision.get("final_turnover") if isinstance(risk_decision, Mapping) else None
        )
        estimated_volatility = (
            risk_decision.get("final_estimated_volatility")
            if isinstance(risk_decision, Mapping)
            else None
        )
        regime = decision_metadata.get("regime") if decision_metadata is not None else None
        performance = {
            **performance,
            "turnover": turnover,
            "regime": regime,
        }
        self.repository.record_daily_performance(
            run_id=self.run_id,
            session_date=effective_date,
            metrics=performance,
            created_at=now,
        )
        warnings = tuple(
            dict.fromkeys(
                (
                    *risk_warnings,
                    *gate.reasons,
                    *(
                        str(item.get("reason"))
                        for item in (() if planning is None else planning.skipped)
                    ),
                    *("broker submission outcome unknown" if ambiguous else "",),
                )
            )
        )
        warnings = tuple(warning for warning in warnings if warning)
        decision_context = {
            **(self._active_cycle_context or {}),
            "actual_at": self.clock.now(),
            "account": account,
            "account_snapshot_time": account.timestamp,
            "positions": tuple(positions),
            "current_drawdown": risk_state["drawdown"],
            "current_daily_loss": risk_state["daily_loss"],
            "operational_risk_state": operational_risk_state,
            "current_target": current_target,
            "current_cash_weight": (
                Decimal("0") if account.equity <= 0 else account.cash / account.equity
            ),
            "target_cash_weight": (
                None
                if selected_weights is None
                else Decimal("1")
                - sum((Decimal(str(value)) for value in selected_weights.values()), Decimal("0"))
            ),
            "final_target": selected_weights,
            "risk_actions": (
                *(
                    tuple(decision_metadata.get("risk_actions") or ())
                    if decision_metadata is not None
                    else ()
                ),
                *operational_actions,
            ),
            "turnover": turnover,
            "estimated_volatility": estimated_volatility,
            "warnings": warnings,
            "incidents": tuple(self._incident_ids),
            "order_outcomes": self._order_outcomes(planning),
            "pending_buy_client_order_ids": (
                pending_buys if gate.allowed and planning is not None else ()
            ),
            "monitor_timestamps": {
                "heartbeat": self._last_heartbeat_at,
                "risk_snapshot": self._last_risk_snapshot_at,
                "reconciliation": self._last_reconciliation_at,
                "open_orders": self._last_open_order_monitor_at,
            },
        }
        result = LiveCycleResult(
            decision_id=decision_id,
            session_date=effective_date,
            claimed=True,
            status=status,
            gate=gate,
            planning=planning,
            reconciliation=reconciliation,
            decision_metadata=decision_metadata,
            decision_context=decision_context,
            submitted_client_order_ids=submitted,
            hypothetical_client_order_ids=hypothetical,
            skip_reason=skip_reason,
        )
        payload = _payload(result)
        if not self.repository.has_decision_receipt(decision_id):
            self.repository.store_decision_receipt(
                run_id=self.run_id,
                decision_id=decision_id,
                payload=payload,
            )
        self.repository.complete_rebalance(
            decision_id,
            status=status,
            payload=payload,
            skip_reason=skip_reason,
        )
        return result

    @staticmethod
    def _monitor_due(
        now: datetime,
        previous: datetime | None,
        interval_seconds: float,
    ) -> bool:
        return previous is None or (now - previous).total_seconds() >= interval_seconds

    def _execute_hard_stop_reduction(
        self,
        *,
        account: AccountState,
        positions: Sequence[Any],
        session_date: date,
        now: datetime,
        gate: SubmissionGateResult,
        reconciliation: ReconciliationResult,
        risk_state: Mapping[str, Any],
        operator_flatten_request_id: str | None = None,
    ) -> LiveCycleResult | None:
        """Submit one audited, idempotent all-cash reduction attempt.

        The default namespace is the durable operational hard-stop series for
        a market session.  ``operator_flatten_request_id`` selects the same
        state machine for an explicitly acknowledged operator liquidation,
        with retries tied to the immutable halt event that requested it.
        """

        if not gate.allowed:
            return None
        operator_flatten = operator_flatten_request_id is not None
        strategy_version = (
            "paper-liquidation-v1" if operator_flatten else "operational-hard-stop-v1"
        )
        decision_mode = "paper_liquidation" if operator_flatten else self.mode.value
        submitted_status = "liquidation_submitted" if operator_flatten else "hard_stop_submitted"
        deferred_status = "liquidation_deferred" if operator_flatten else "hard_stop_deferred"
        no_positions_status = (
            "liquidation_no_positions" if operator_flatten else "hard_stop_no_positions"
        )
        warning = (
            "explicitly acknowledged paper liquidation"
            if operator_flatten
            else "operational hard-stop reduction"
        )
        failure_incident_type = (
            "operator_flatten_failure" if operator_flatten else "hard_stop_reduction_failure"
        )
        # The cancel/reconcile sequence can change holdings.  Always refresh
        # from the broker before deciding whether a residual reduction exists.
        account = self.broker.get_account()
        positions = list(self.broker.get_positions())
        open_orders = list(self.broker.get_orders(include_closed=False))
        base_key = (
            f"{self.idempotency_namespace}:paper-liquidation:{operator_flatten_request_id}"
            if operator_flatten
            else (f"{self.idempotency_namespace}:operational-hard-stop:{session_date.isoformat()}")
        )
        prior_attempts = sorted(
            (
                row
                for row in self.repository.list_rebalances()
                if str(row.get("idempotency_key", "")).startswith(base_key)
            ),
            key=lambda row: row["created_at"],
        )
        broker_by_client = {
            order.client_order_id: order for order in self.broker.get_orders(include_closed=True)
        }
        for prior in prior_attempts:
            prior_payload = dict(prior.get("payload") or {})
            prior_planning = prior_payload.get("planning")
            raw_intents = (
                prior_planning.get("intents", ()) if isinstance(prior_planning, Mapping) else ()
            )
            client_ids = {
                str(raw["client_order_id"])
                for raw in raw_intents
                if isinstance(raw, Mapping) and raw.get("client_order_id")
            }
            # Unknown or nonterminal prior submissions can still fill, so they
            # block a retry.  Demonstrably terminal orders are safe to replan
            # against the broker's current residual position.
            for client_id in client_ids:
                local = self.repository.get_order(client_id)
                if local is not None:
                    try:
                        local_state = LocalOrderState(str(local["state"]))
                    except (KeyError, ValueError):
                        return None
                    if local_state not in TERMINAL_STATES:
                        return None
                broker_order = broker_by_client.get(client_id)
                if (
                    broker_order is not None
                    and state_for_broker_status(broker_order.status) not in TERMINAL_STATES
                ):
                    return None
            if str(prior.get("status")) == no_positions_status and not positions:
                return None
        if prior_attempts and not positions:
            return None
        attempt = len(prior_attempts)
        idempotency_key = base_key if attempt == 0 else f"{base_key}:retry:{attempt}"
        decision_id, claimed = self.repository.claim_rebalance(
            run_id=self.run_id,
            idempotency_key=idempotency_key,
            session_date=session_date,
            strategy_version=strategy_version,
            mode=decision_mode,
            scheduled_at=now,
            decision_id=uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex,
        )
        if not claimed:
            return None
        try:
            # Final just-in-time snapshots prevent a fill observed during
            # reconciliation from turning the reduction into an oversell.
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
            open_orders = list(self.broker.get_orders(include_closed=False))
            planning = self.planner.plan(
                decision_id=decision_id,
                session_date=session_date,
                created_at=now,
                target_weights={},
                account=account,
                positions=positions,
                latest_prices=self._latest_prices(),
                universe=self.universe,
                open_orders=open_orders,
            )
            hard_freshness = self.bar_store.freshness(now)
            hard_market_clock = self.broker.get_clock()
            hard_asset_validation = {
                symbol: {
                    "valid": self._assets_verified,
                    "source": "startup_validation",
                }
                for symbol in self.universe
            }
            operational_risk_state = self._operational_risk_context(
                risk_state=risk_state,
                account=account,
                positions=positions,
                open_orders=open_orders,
                freshness=hard_freshness,
                market_clock=hard_market_clock,
                reconciliation=reconciliation,
                asset_validation=hard_asset_validation,
                now=now,
            )
            reduction_actions: tuple[Any, ...] = tuple(risk_state.get("actions", ()))
            if operator_flatten:
                reduction_actions = (
                    *reduction_actions,
                    {
                        "control": "operator_flatten",
                        "description": "Explicitly acknowledged all-cash paper reduction",
                        "halt_event_id": operator_flatten_request_id,
                    },
                )
            context: dict[str, Any] = {
                "run_id": self.run_id,
                "configuration_hash": configuration_hash(self.config),
                "strategy_version": strategy_version,
                "scheduled_at": now,
                "actual_at": self.clock.now(),
                "mode": decision_mode,
                "market": hard_market_clock,
                "feed": self.market_data.feed,
                "freshness": hard_freshness,
                "account": account,
                "positions": tuple(positions),
                "current_drawdown": risk_state.get("drawdown"),
                "current_daily_loss": risk_state.get("daily_loss"),
                "operational_risk_state": operational_risk_state,
                "final_target": {},
                "target_cash_weight": Decimal("1"),
                "risk_actions": reduction_actions,
                "operator_flatten_request_id": operator_flatten_request_id,
                "warnings": ("broker outcomes not yet known",),
                "incidents": tuple(self._incident_ids),
                "order_outcomes": self._order_outcomes(planning),
            }
            if planning.sells:
                pre_submission_result = LiveCycleResult(
                    decision_id=decision_id,
                    session_date=session_date,
                    claimed=True,
                    status="submission_authorized",
                    gate=gate,
                    planning=planning,
                    reconciliation=reconciliation,
                    decision_context=context,
                )
                pre_submission_payload = _payload(pre_submission_result)
                pre_submission_payload["execution_phase"] = "authorized_not_yet_submitted"
                self.repository.store_decision_receipt(
                    run_id=self.run_id,
                    decision_id=decision_id,
                    payload=pre_submission_payload,
                )
                self.repository.complete_rebalance(
                    decision_id,
                    status="submission_authorized",
                    payload=pre_submission_payload,
                )
            results = self.order_manager.execute(planning.sells)
            ambiguous = any(order is None for order in results)
            if ambiguous:
                reconciliation = self.reconcile()
            submitted = tuple(order.client_order_id for order in results if order is not None)
            if ambiguous:
                status = "submission_unknown"
            elif submitted:
                status = submitted_status
            elif positions:
                status = deferred_status
            else:
                status = no_positions_status
            context = {
                **context,
                "actual_at": self.clock.now(),
                "warnings": (warning,),
                "order_outcomes": self._order_outcomes(planning),
            }
            result = LiveCycleResult(
                decision_id=decision_id,
                session_date=session_date,
                claimed=True,
                status=status,
                gate=gate,
                planning=planning,
                reconciliation=reconciliation,
                decision_context=context,
                submitted_client_order_ids=submitted,
            )
            payload = _payload(result)
            self._persist_decision_metadata(
                decision_id=decision_id,
                metadata=None,
                as_of_at=now,
                operational_final_target={},
                operational_actions=reduction_actions,
                operational_risk_state=operational_risk_state,
            )
            if not self.repository.has_decision_receipt(decision_id):
                self.repository.store_decision_receipt(
                    run_id=self.run_id,
                    decision_id=decision_id,
                    payload=payload,
                )
            self.repository.complete_rebalance(
                decision_id,
                status=status,
                payload=payload,
            )
            self._resolve_incidents(failure_incident_type)
            return result
        except Exception as exc:
            reason = f"{warning} failed safely: {redact(str(exc))}"
            incident_id = self._record_incident(
                failure_incident_type,
                reason,
                details={"decision_id": decision_id},
            )
            result = LiveCycleResult(
                decision_id=decision_id,
                session_date=session_date,
                claimed=True,
                status="rejected",
                gate=SubmissionGateResult(
                    allowed=False,
                    reasons=(reason,),
                    effective_mode="observer",
                ),
                planning=None,
                reconciliation=reconciliation,
                decision_context={
                    "run_id": self.run_id,
                    "configuration_hash": configuration_hash(self.config),
                    "strategy_version": strategy_version,
                    "scheduled_at": now,
                    "actual_at": self.clock.now(),
                    "mode": decision_mode,
                    "operator_flatten_request_id": operator_flatten_request_id,
                    "final_target": {},
                    "warnings": (reason,),
                    "incidents": (incident_id,),
                },
                skip_reason=reason,
            )
            payload = _payload(result)
            if not self.repository.has_decision_receipt(decision_id):
                self.repository.store_decision_receipt(
                    run_id=self.run_id,
                    decision_id=decision_id,
                    payload=payload,
                )
            self.repository.complete_rebalance(
                decision_id,
                status=result.status,
                payload=payload,
                skip_reason=reason,
            )
            return result

    def _active_operator_flatten_request(self, now: datetime) -> str | None:
        """Return the newest flatten request with durable cancel-all evidence."""

        candidates = self._operator_flatten_request_candidates(now)
        candidates = [
            row
            for row in candidates
            if self.repository.has_operator_halt_cancel_success(str(row["halt_event_id"]))
        ]
        if not candidates:
            return None
        selected = max(candidates, key=lambda row: as_utc(row["created_at"]))
        return str(selected["halt_event_id"])

    def _pending_operator_flatten_request(self, now: datetime) -> str | None:
        """Return the newest authorized request whose cancel stage is incomplete."""

        candidates = [
            row
            for row in self._operator_flatten_request_candidates(now)
            if not self.repository.has_operator_halt_cancel_success(str(row["halt_event_id"]))
        ]
        if not candidates:
            return None
        selected = max(candidates, key=lambda row: as_utc(row["created_at"]))
        return str(selected["halt_event_id"])

    def _operator_flatten_request_candidates(
        self,
        now: datetime,
    ) -> list[Mapping[str, Any]]:
        """Select active, explicitly acknowledged durable flatten requests."""

        candidates: list[Mapping[str, Any]] = []
        for latch_type, row in self.repository.active_halts(now).items():
            if latch_type not in {"manual", "operator"}:
                continue
            details = row.get("details")
            if not isinstance(details, Mapping) or not bool(details.get("flatten_requested")):
                continue
            if row.get("acknowledgement") != PAPER_FLATTEN_ACKNOWLEDGEMENT:
                continue
            candidates.append(row)
        return candidates

    def _request_operator_halt_cancellation(
        self,
        *,
        halt_event_id: str,
        flatten_requested: bool,
    ) -> None:
        """Attempt cancel-all and persist request-scoped outcome evidence."""

        if not self._paper_cancellation_authorized(dry_run=self.dry_run):
            raise SafetyViolation(
                "Operator halt cancellation requires complete paper/replay authority"
            )
        try:
            self.broker.cancel_all_orders()
        except Exception as exc:
            reason = redact(str(exc) or type(exc).__name__)
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="orders",
                event_type="operator_halt_cancel_all_failed",
                created_at=self.clock.now(),
                payload={
                    "halt_event_id": halt_event_id,
                    "flatten_requested": flatten_requested,
                    "reason": reason,
                },
            )
            if flatten_requested:
                raise ReconciliationBlocked(
                    f"Halt was recorded, but paper-order cancellation failed: {reason}"
                ) from None
            raise
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="orders",
            event_type="operator_halt_cancel_all_requested",
            created_at=self.clock.now(),
            payload={
                "halt_event_id": halt_event_id,
                "flatten_requested": flatten_requested,
                "outcome": "paper broker accepted the cancel-all request",
            },
        )

    def _operator_flatten_attempt(
        self,
        *,
        request_id: str,
        now: datetime,
    ) -> tuple[LiveCycleResult | None, SubmissionGateResult]:
        """Reconcile, refresh and make at most one durable flatten attempt."""

        reconciliation = self.reconcile()
        if reconciliation.blocking:
            # The first pass may legitimately project a just-observed fill or
            # cancellation and refresh the account snapshot.  A second pass
            # distinguishes that recovered state from a persistent blocker.
            verification = self.reconcile()
            if not verification.blocking:
                reconciliation = verification
        account = self.broker.get_account()
        positions = list(self.broker.get_positions())
        open_orders = list(self.broker.get_orders(include_closed=False))
        broker_clock = self.broker.get_clock()
        freshness = self.bar_store.freshness(now)
        risk_state = self._apply_account_risk_latches(
            account,
            self._session_date(broker_clock.timestamp),
            cancellation_authorized=False,
        )
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="orders",
            event_type="operator_flatten_reconciled",
            created_at=self.clock.now(),
            payload={
                "halt_event_id": request_id,
                "reconciliation_id": reconciliation.reconciliation_id,
                "clean": reconciliation.clean,
                "blocking": reconciliation.blocking,
                "remaining_positions": {
                    position.symbol: str(position.quantity) for position in positions
                },
                "open_order_ids": [order.client_order_id for order in open_orders],
            },
        )
        if not positions:
            gate = SubmissionGateResult(
                allowed=True,
                reasons=("paper account already has no positions",),
                effective_mode="paper_liquidation_noop",
            )
        elif self.allow_simulated_replay_orders:
            gate = self._replay_gate(
                freshness=freshness,
                reconciliation=reconciliation,
                market_open=broker_clock.is_open,
                before_cutoff=True,
                halt_active=False,
                dry_run=False,
            )
        else:
            gate = evaluate_order_enablement(
                self.config,
                mode=RunMode.PAPER_ONCE,
                environment=self.environment,
                broker_paper_only=self.broker.paper_only,
                **self._paper_authority_gate(),
                account=account,
                market_clock=broker_clock,
                freshness=freshness,
                reconciliation_clean=not reconciliation.blocking,
                # The exact acknowledgement authorizes only reductions and
                # deliberately bypasses the strategy/manual-halt cutoff.
                halt_active=False,
                before_cutoff=True,
                dry_run=False,
            )
        gate = self._apply_trade_stream_gate(
            gate,
            submission_expected=bool(positions),
        )
        result = self._execute_hard_stop_reduction(
            account=account,
            positions=positions,
            session_date=self._session_date(broker_clock.timestamp),
            now=now,
            gate=gate,
            reconciliation=reconciliation,
            risk_state=risk_state,
            operator_flatten_request_id=request_id,
        )
        return result, gate

    def _monitor_risk_snapshot(self, now: datetime) -> None:
        account = self.broker.get_account()
        broker_clock = self.broker.get_clock()
        session_date = self._session_date(broker_clock.timestamp)
        freshness = self.bar_store.freshness(now)
        risk_state = self._apply_account_risk_latches(
            account,
            session_date,
            cancellation_authorized=self._paper_cancellation_authorized(dry_run=self.dry_run),
        )
        risk_was_active = bool(risk_state["daily_loss_active"] or risk_state["hard_stop_active"])
        if freshness.unresolved_gap:
            self.recover_durable_market_data_gaps()
            freshness = self.bar_store.freshness(now)
        reconciliation = self.reconcile()
        self._expire_daily_loss_after_clean_reconciliation(
            session_date=session_date,
            now=now,
            reconciliation=reconciliation,
        )
        account = self.broker.get_account()
        positions = list(self.broker.get_positions())
        broker_clock = self.broker.get_clock()
        freshness = self.bar_store.freshness(now)
        risk_state = self._apply_account_risk_latches(
            account,
            session_date,
            cancellation_authorized=False,
        )
        risk_now_active = bool(risk_state["daily_loss_active"] or risk_state["hard_stop_active"])
        if risk_now_active and not risk_was_active:
            self._apply_account_risk_latches(
                account,
                session_date,
                cancellation_authorized=self._paper_cancellation_authorized(dry_run=self.dry_run),
            )
            reconciliation = self.reconcile()
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
            broker_clock = self.broker.get_clock()
            freshness = self.bar_store.freshness(now)
            risk_state = self._apply_account_risk_latches(
                account,
                session_date,
                cancellation_authorized=False,
            )
        actions = tuple(risk_state.get("actions", ()))
        if actions:
            self.repository.append_risk_actions(
                run_id=self.run_id,
                decision_id=f"risk-monitor:{session_date.isoformat()}",
                actions=actions,
                created_at=now,
            )
        flatten_request_id = self._active_operator_flatten_request(now)
        pending_flatten_request_id = self._pending_operator_flatten_request(now)
        if (
            flatten_request_id is None
            and pending_flatten_request_id is not None
            and self._paper_cancellation_authorized(dry_run=self.dry_run)
        ):
            # A process may have stopped after committing the halt but before
            # durably recording cancel-all success.  Retrying cancel-all is
            # idempotent; no reduction is authorized until this exact request
            # has success evidence, after which the normal reconcile/refresh
            # path runs below.
            try:
                self._request_operator_halt_cancellation(
                    halt_event_id=pending_flatten_request_id,
                    flatten_requested=True,
                )
            except Exception:
                pass
            else:
                flatten_request_id = self._active_operator_flatten_request(now)
        if bool(risk_state.get("hard_stop_active")) and flatten_request_id is None:
            # A first reconciliation may legitimately project fills/cancels or
            # a changed account snapshot.  Require a subsequent clean view and
            # refresh again before authorizing the reduction plan.
            reconciliation = self.reconcile()
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
            broker_clock = self.broker.get_clock()
            freshness = self.bar_store.freshness(now)
            risk_state = self._apply_account_risk_latches(
                account,
                session_date,
                cancellation_authorized=False,
            )
            if self.allow_simulated_replay_orders:
                reduction_gate = self._replay_gate(
                    freshness=self.bar_store.freshness(now),
                    reconciliation=reconciliation,
                    market_open=broker_clock.is_open,
                    before_cutoff=True,
                    halt_active=self._blocking_halt_active(self.repository.active_halts(now)),
                    dry_run=self.dry_run,
                )
            else:
                reduction_gate = evaluate_order_enablement(
                    self.config,
                    mode=self.mode,
                    environment=self.environment,
                    broker_paper_only=self.broker.paper_only,
                    **self._paper_authority_gate(),
                    account=account,
                    market_clock=broker_clock,
                    freshness=self.bar_store.freshness(now),
                    reconciliation_clean=not reconciliation.blocking,
                    halt_active=self._blocking_halt_active(self.repository.active_halts(now)),
                    before_cutoff=True,
                    dry_run=self.dry_run,
                )
            reduction_gate = self._apply_trade_stream_gate(
                reduction_gate,
                submission_expected=bool(positions),
            )
            self._execute_hard_stop_reduction(
                account=account,
                positions=positions,
                session_date=session_date,
                now=now,
                gate=reduction_gate,
                reconciliation=reconciliation,
                risk_state=risk_state,
            )
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
        elif (
            flatten_request_id is not None
            and not self.dry_run
            and (
                self.mode in {RunMode.PAPER_ONCE, RunMode.PAPER_RUN}
                or (self.mode is RunMode.REPLAY and self.allow_simulated_replay_orders)
            )
        ):
            self._operator_flatten_attempt(
                request_id=flatten_request_id,
                now=now,
            )
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
        self._record_forward_performance(
            account=account,
            positions=positions,
            session_date=session_date,
            now=now,
        )
        self.repository.record_account_state(account, positions, run_id=self.run_id)
        self._last_risk_snapshot_at = now

    @staticmethod
    def _intent_from_receipt(raw: Mapping[str, Any]) -> OrderIntent:
        return OrderIntent(
            decision_id=str(raw["decision_id"]),
            client_order_id=str(raw["client_order_id"]),
            session_date=date.fromisoformat(str(raw["session_date"])),
            symbol=str(raw["symbol"]),
            side=Side(str(raw["side"])),
            sequence=int(raw["sequence"]),
            reference_price=Decimal(str(raw["reference_price"])),
            created_at=as_utc(datetime.fromisoformat(str(raw["created_at"])))
            if raw.get("created_at")
            else datetime.now(tz=UTC),
            notional=(None if raw.get("notional") is None else Decimal(str(raw["notional"]))),
            quantity=(None if raw.get("quantity") is None else Decimal(str(raw["quantity"]))),
            reason=str(raw.get("reason", "rebalance")),
        )

    def _order_outcomes(
        self,
        planning: PlanningResult | None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Snapshot broker IDs and final-known local/broker states for a receipt."""

        if planning is None:
            return ()
        outcomes: list[Mapping[str, Any]] = []
        for intent in planning.intents:
            row = self.repository.get_order(intent.client_order_id)
            outcomes.append(
                {
                    "client_order_id": intent.client_order_id,
                    "broker_order_id": None if row is None else row.get("broker_order_id"),
                    "local_state": "not_submitted" if row is None else row.get("state"),
                    "broker_status": None if row is None else row.get("raw_status"),
                    "filled_quantity": None if row is None else row.get("filled_quantity"),
                    "average_fill_price": (None if row is None else row.get("average_fill_price")),
                    "known_at": self.clock.now(),
                }
            )
        return tuple(outcomes)

    def _resume_pending_executions(self, now: datetime) -> None:
        pending_rows = self.repository.list_rebalances(status="execution_pending")
        if not pending_rows:
            return
        timeout_seconds = float(
            _section_value(self.config, "schedule", "order_fill_timeout_seconds", 120)
        )
        for decision in pending_rows:
            decision_id = str(decision["decision_id"])
            payload = dict(decision.get("payload") or {})
            planning_payload = payload.get("planning")
            if not isinstance(planning_payload, Mapping):
                continue
            raw_intents = planning_payload.get("intents", ())
            intents = tuple(
                self._intent_from_receipt(raw) for raw in raw_intents if isinstance(raw, Mapping)
            )
            original_session = date.fromisoformat(str(decision["session_date"]))
            current_session = self._session_date(now)
            stale_session = original_session != current_session
            inconsistent_intents = any(
                intent.session_date != original_session for intent in intents
            )
            after_original_cutoff = not self._before_cutoff(now)
            if stale_session or inconsistent_intents or after_original_cutoff:
                if stale_session:
                    reason = "pending buy leg abandoned after its original market session"
                elif inconsistent_intents:
                    reason = "pending buy leg abandoned because intent session metadata conflicts"
                else:
                    reason = "pending buy leg abandoned after its original session cutoff"
                outcomes = self._order_outcomes(PlanningResult(intents=intents))
                updated_payload = {
                    **payload,
                    "execution_status": "buy_leg_abandoned",
                    "buy_leg_abandoned_at": now,
                    "buy_leg_abandonment_reason": reason,
                    "order_outcomes": outcomes,
                }
                self.repository.complete_rebalance(
                    decision_id,
                    status="buy_leg_abandoned",
                    payload=updated_payload,
                    skip_reason=reason,
                )
                self.repository.record_stream_event(
                    run_id=self.run_id,
                    stream="orders",
                    event_type="pending_buy_leg_abandoned",
                    created_at=now,
                    payload={
                        "decision_id": decision_id,
                        "original_session": original_session,
                        "current_session": current_session,
                        "reason": reason,
                        "order_outcomes": outcomes,
                    },
                )
                continue
            sells = tuple(intent for intent in intents if intent.side is Side.SELL)
            unresolved = False
            for intent in sells:
                local = self.repository.get_order(intent.client_order_id)
                if local is None or LocalOrderState(str(local["state"])) not in TERMINAL_STATES:
                    unresolved = True
                    break
            age = (now - decision["created_at"]).total_seconds()
            if unresolved:
                if age >= timeout_seconds and decision_id not in self._timed_out_decisions:
                    self._record_incident(
                        "sell_fill_timeout",
                        "Sell leg remained nonterminal beyond the configured timeout",
                        severity="warning",
                        details={"decision_id": decision_id, "age_seconds": age},
                    )
                    self._timed_out_decisions.add(decision_id)
                continue
            self._resolve_incidents("sell_fill_timeout")
            active = self.repository.active_halts(now)
            if active or not intents:
                # Daily-loss and hard-stop latches also prohibit resuming buys.
                continue
            reconciliation = self.reconcile()
            account = self.broker.get_account()
            if self.allow_simulated_replay_orders:
                gate = self._replay_gate(
                    freshness=self.bar_store.freshness(now),
                    reconciliation=reconciliation,
                    market_open=self.broker.get_clock().is_open,
                    before_cutoff=True,
                    halt_active=False,
                    dry_run=self.dry_run,
                )
            else:
                gate = evaluate_order_enablement(
                    self.config,
                    mode=self.mode,
                    environment=self.environment,
                    broker_paper_only=self.broker.paper_only,
                    **self._paper_authority_gate(),
                    account=account,
                    market_clock=self.broker.get_clock(),
                    freshness=self.bar_store.freshness(now),
                    reconciliation_clean=not reconciliation.blocking,
                    halt_active=False,
                    before_cutoff=True,
                    dry_run=self.dry_run,
                )
            gate = self._apply_trade_stream_gate(
                gate,
                submission_expected=bool(intents),
            )
            if not gate.allowed:
                continue
            buys = tuple(
                intent
                for intent in intents
                if intent.side is Side.BUY
                and self.repository.get_order(intent.client_order_id) is None
            )
            reserved_open_buys = sum(
                (
                    order.requested_notional or Decimal("0")
                    for order in self.broker.get_orders(include_closed=False)
                    if order.side is Side.BUY
                ),
                Decimal("0"),
            )
            cash_buffer = Decimal(
                str(
                    _section_value(
                        self.config,
                        "execution",
                        "required_cash_buffer",
                        "0.02",
                    )
                )
            )
            available_cash = max(
                Decimal("0"),
                account.cash * (Decimal("1") - cash_buffer) - reserved_open_buys,
            )
            required_cash = sum(
                (intent.estimated_notional for intent in buys),
                Decimal("0"),
            )
            if required_cash > available_cash:
                if decision_id not in self._cash_blocked_decisions:
                    self._record_incident(
                        "pending_buy_cash_recheck_failed",
                        "Pending buy leg exceeds currently available broker cash",
                        severity="warning",
                        details={
                            "decision_id": decision_id,
                            "required_cash": str(required_cash),
                            "available_cash": str(available_cash),
                        },
                    )
                    self.repository.record_stream_event(
                        run_id=self.run_id,
                        stream="orders",
                        event_type="pending_execution_cash_blocked",
                        created_at=now,
                        payload={
                            "decision_id": decision_id,
                            "required_cash": required_cash,
                            "available_cash": available_cash,
                        },
                    )
                    self._cash_blocked_decisions.add(decision_id)
                continue
            self._resolve_incidents("pending_buy_cash_recheck_failed")
            results = self.order_manager.execute(buys)
            ambiguous = any(order is None for order in results)
            if ambiguous:
                self.reconcile()
            status = "submission_unknown" if ambiguous else "submitted"
            continued_outcomes = self._order_outcomes(PlanningResult(intents=intents))
            updated_payload = {
                **payload,
                "execution_status": status,
                "continued_at": now,
                "continued_buy_client_order_ids": [
                    order.client_order_id for order in results if order is not None
                ],
                "order_outcomes": continued_outcomes,
            }
            self.repository.complete_rebalance(
                decision_id,
                status=status,
                payload=updated_payload,
            )
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="orders",
                event_type="pending_execution_continued",
                created_at=now,
                payload={
                    "decision_id": decision_id,
                    "status": status,
                    "order_outcomes": continued_outcomes,
                },
            )

    def _monitor_open_orders(self, now: datetime) -> None:
        broker_orders = list(self.broker.get_orders(include_closed=False))
        local_orders = self.repository.list_orders(open_only=True)
        trade_stream_status = self._trade_updates_status()
        trade_stream_healthy = self._trade_updates_healthy()
        if trade_stream_status != self._last_trade_stream_status:
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="trade_updates",
                event_type=("connected" if trade_stream_healthy else "unhealthy"),
                created_at=now,
                payload={"status": trade_stream_status},
            )
            self._last_trade_stream_status = trade_stream_status
        if (broker_orders or local_orders) and not trade_stream_healthy:
            # REST reconciliation is the safe observation fallback.  It does
            # not make the stream healthy and the submission gate stays shut.
            self.reconcile()
            if not self._trade_stream_incident_active:
                self._record_incident(
                    "trade_update_stream_unhealthy_with_open_orders",
                    "Trade-update stream is unhealthy while paper orders are open",
                    severity="warning",
                    details={"status": trade_stream_status},
                )
                self._trade_stream_incident_active = True
        elif trade_stream_healthy:
            self._resolve_incidents("trade_update_stream_unhealthy_with_open_orders")
            self._trade_stream_incident_active = False
        self.repository.record_stream_event(
            run_id=self.run_id,
            stream="orders",
            event_type="open_order_monitor",
            created_at=now,
            payload={
                "broker_open_count": len(broker_orders),
                "local_open_count": len(local_orders),
                "broker_client_order_ids": [order.client_order_id for order in broker_orders],
                "local_client_order_ids": [str(order["client_order_id"]) for order in local_orders],
                "trade_updates_healthy": trade_stream_healthy,
                "trade_updates_status": trade_stream_status,
            },
        )
        self._resume_pending_executions(now)
        self._last_open_order_monitor_at = now

    def _heartbeat_components(self, now: datetime) -> dict[str, Any]:
        market_clock_error: str | None = None
        try:
            market_clock = self.broker.get_clock()
        except Exception as exc:
            market_clock = None
            market_clock_error = redact(str(exc) or type(exc).__name__)
        freshness = self.bar_store.freshness(now)
        account = self.repository.latest_account_state()
        equity = None if account is None else Decimal(str(account["equity"]))
        last_equity = (
            None
            if account is None or account.get("last_equity") is None
            else Decimal(str(account["last_equity"]))
        )
        high_water = self.repository.account_equity_high_water()
        drawdown = (
            None
            if equity is None or high_water is None or high_water <= 0
            else max(Decimal("0"), (high_water - equity) / high_water)
        )
        daily_loss = (
            None
            if equity is None or last_equity is None or last_equity <= 0
            else max(Decimal("0"), (last_equity - equity) / last_equity)
        )
        open_local_orders = len(self.repository.list_orders(open_only=True))
        return {
            "market_open": None if market_clock is None else market_clock.is_open,
            "market_timestamp": None if market_clock is None else market_clock.timestamp,
            "next_open": None if market_clock is None else market_clock.next_open,
            "next_close": None if market_clock is None else market_clock.next_close,
            "market_clock_error": market_clock_error,
            "feed": self.market_data.feed,
            "feed_entitlement_verified": self._feed_entitlement_verified,
            "assets_verified": self._assets_verified,
            "history_preflight_verified": self._history_preflight_verified,
            "stream_connected": freshness.stream_healthy,
            "trade_updates_healthy": self._trade_updates_healthy(),
            "trade_updates_status": self._trade_updates_status(),
            "fresh": freshness.fresh,
            "latest_bar_timestamps": freshness.last_bar_by_symbol,
            "missing_symbols": freshness.missing_symbols,
            "stale_symbols": freshness.stale_symbols,
            "unresolved_gap": freshness.unresolved_gap,
            "current_drawdown": drawdown,
            "current_daily_loss": daily_loss,
            "account_snapshot_time": None if account is None else account["timestamp"],
            "active_halts": sorted(self.repository.active_halts(now)),
            "active_incidents": tuple(self._incident_ids),
            "open_local_orders": open_local_orders,
            "monitor_timestamps": {
                "heartbeat": self._last_heartbeat_at,
                "risk_snapshot": self._last_risk_snapshot_at,
                "reconciliation": self._last_reconciliation_at,
                "open_orders": self._last_open_order_monitor_at,
            },
        }

    @staticmethod
    def _heartbeat_health(components: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
        """Derive health from live dependencies, with closed-market semantics."""

        reasons: list[str] = []
        if components.get("active_incidents"):
            reasons.append("unresolved system incident")
        if components.get("market_clock_error"):
            reasons.append("paper market clock is unavailable")
        if not bool(components.get("feed_entitlement_verified")):
            reasons.append("market-data feed entitlement is unverified")
        if not bool(components.get("assets_verified")):
            reasons.append("configured asset eligibility is unverified")
        if not bool(components.get("history_preflight_verified")):
            reasons.append("completed strategy history is unverified")
        if bool(components.get("unresolved_gap")):
            reasons.append("market-data gap is unresolved")
        market_open = bool(components.get("market_open"))
        if market_open and not bool(components.get("stream_connected")):
            reasons.append("market-data stream is disconnected during market hours")
        if market_open and not bool(components.get("fresh")):
            reasons.append("market data is not fresh during market hours")
        trade_updates_required = market_open or int(components.get("open_local_orders") or 0) > 0
        if trade_updates_required and not bool(components.get("trade_updates_healthy")):
            reasons.append("trade-update stream is unhealthy while required")
        return not reasons, tuple(dict.fromkeys(reasons))

    def _record_missed_after_cutoff(
        self,
        *,
        session: MarketSession,
        scheduled_at: datetime,
        now: datetime,
    ) -> LiveCycleResult | None:
        reason = "scheduled evaluation was missed after the configured catch-up cutoff"
        snapshot_errors: dict[str, str] = {}
        account: AccountState | None = None
        positions: list[Any] = []
        open_orders: list[Any] = []
        market_evidence: Any
        try:
            account = self.broker.get_account()
        except Exception as exc:
            snapshot_errors["account"] = redact(str(exc) or type(exc).__name__)
        try:
            positions = list(self.broker.get_positions())
        except Exception as exc:
            snapshot_errors["positions"] = redact(str(exc) or type(exc).__name__)
        try:
            open_orders = list(self.broker.get_orders(include_closed=False))
        except Exception as exc:
            snapshot_errors["open_orders"] = redact(str(exc) or type(exc).__name__)
        try:
            market_evidence = self.broker.get_clock()
        except Exception as exc:
            snapshot_errors["market_clock"] = redact(str(exc) or type(exc).__name__)
            market_evidence = {
                "timestamp": now,
                "is_open": session.open_at <= now < session.close_at,
                "session_open": session.open_at,
                "session_close": session.close_at,
                "source": "cached_market_calendar",
            }
        freshness = self.bar_store.freshness(now)
        risk_state: dict[str, Any] = {
            "daily_loss": None,
            "drawdown": None,
            "high_water_equity": self.repository.account_equity_high_water(),
            "daily_loss_active": "daily_loss" in self.repository.active_halts(now),
            "hard_stop_active": "hard_stop" in self.repository.active_halts(now),
            "soft_drawdown_active": None,
            "manual_halt_active": self._blocking_halt_active(self.repository.active_halts(now)),
            "actions": (),
        }
        if account is not None:
            try:
                risk_state = self._apply_account_risk_latches(
                    account,
                    session.session_date,
                    cancellation_authorized=False,
                )
            except Exception as exc:
                snapshot_errors["risk_state"] = redact(str(exc) or type(exc).__name__)
        current_target = (
            None
            if account is None or account.equity <= 0
            else {position.symbol: position.market_value / account.equity for position in positions}
        )
        current_cash_weight = (
            None if account is None or account.equity <= 0 else account.cash / account.equity
        )
        latest_reconciliation = self.repository.latest_reconciliation()
        active_halts = self.repository.active_halts(now)
        risk_actions = tuple(risk_state.get("actions", ()))
        non_evaluated_fields = {
            "signals": reason,
            "momentum": reason,
            "mean_reversion": reason,
            "regime": reason,
            "allocation": reason,
            "proposed_target": reason,
            "final_target": reason,
            "target_cash_weight": reason,
            "turnover": reason,
            "estimated_volatility": reason,
            "order_intents": reason,
        }
        operational_risk_state = {
            **risk_state,
            "account": account,
            "positions": tuple(positions),
            "open_orders": tuple(
                {
                    "client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "status": order.status,
                    "updated_at": order.updated_at,
                }
                for order in open_orders
            ),
            "freshness": freshness,
            "market_clock": market_evidence,
            "market_open": (
                bool(market_evidence.is_open)
                if hasattr(market_evidence, "is_open")
                else bool(market_evidence["is_open"])
            ),
            "before_strategy_cutoff": False,
            "asset_validation": {
                symbol: {
                    "valid": self._assets_verified,
                    "source": "startup_validation",
                }
                for symbol in self.universe
            },
            "reconciliation": latest_reconciliation,
            "active_halts": tuple(sorted(active_halts)),
            "halt_state": {
                "manual_or_operator_active": self._blocking_halt_active(active_halts),
                "daily_loss_active": "daily_loss" in active_halts,
                "hard_stop_active": "hard_stop" in active_halts,
            },
            "limits": {
                "daily_loss": _section_value(
                    self.config,
                    "risk",
                    "daily_loss_limit",
                    "0.03",
                ),
                "soft_drawdown": _section_value(
                    self.config,
                    "risk",
                    "drawdown_soft_limit",
                    "0.10",
                ),
                "hard_drawdown": _section_value(
                    self.config,
                    "risk",
                    "drawdown_hard_limit",
                    "0.15",
                ),
            },
            "snapshot_errors": snapshot_errors,
            "evaluation_status": "missed_after_cutoff",
        }
        skipped_metadata = {
            "cutoff": scheduled_at,
            "strategy_outputs": {
                "momentum": {"value": None, "not_evaluated_reason": reason},
                "mean_reversion": {"value": None, "not_evaluated_reason": reason},
            },
            "regime": {"value": None, "not_evaluated_reason": reason},
            "allocation": {
                "pre_risk_weights": None,
                "final_weights": None,
                "not_evaluated_reason": reason,
            },
            "risk_decision": {
                "proposed_weights": None,
                "final_weights": None,
                "final_turnover": None,
                "final_estimated_volatility": None,
                "actions": risk_actions,
                "not_evaluated_reason": reason,
            },
            "final_target": None,
            "risk_actions": risk_actions,
        }
        planning = PlanningResult(
            intents=(),
            skipped=(
                {
                    "reason": "missed_after_cutoff",
                    "detail": reason,
                    "order_intents": "not_evaluated",
                },
            ),
        )
        idempotency_key = (
            f"{self.idempotency_namespace}:{self.strategy_version}:"
            f"{session.session_date.isoformat()}"
        )
        decision_id, claimed = self.repository.claim_rebalance(
            run_id=self.run_id,
            idempotency_key=idempotency_key,
            session_date=session.session_date,
            strategy_version=self.strategy_version,
            mode=self.mode.value,
            scheduled_at=scheduled_at,
        )
        if not claimed:
            return None
        result = LiveCycleResult(
            decision_id=decision_id,
            session_date=session.session_date,
            claimed=True,
            status="missed_after_cutoff",
            gate=SubmissionGateResult(
                allowed=False,
                reasons=(reason,),
                effective_mode="observer",
            ),
            planning=planning,
            reconciliation=None,
            decision_metadata=skipped_metadata,
            decision_context={
                "run_id": self.run_id,
                "configuration_hash": configuration_hash(self.config),
                "strategy_version": self.strategy_version,
                "scheduled_at": scheduled_at,
                "actual_at": now,
                "mode": self.mode.value,
                "market": market_evidence,
                "feed": self.market_data.feed,
                "freshness": freshness,
                "account": account,
                "account_snapshot_time": None if account is None else account.timestamp,
                "positions": tuple(positions),
                "current_drawdown": risk_state.get("drawdown"),
                "current_daily_loss": risk_state.get("daily_loss"),
                "current_target": current_target,
                "current_cash_weight": current_cash_weight,
                "target_cash_weight": None,
                "turnover": None,
                "estimated_volatility": None,
                "final_target": None,
                "risk_actions": risk_actions,
                "operational_risk_state": operational_risk_state,
                "non_evaluated_fields": non_evaluated_fields,
                "warnings": (
                    reason,
                    *(
                        f"{field} snapshot unavailable: {message}"
                        for field, message in sorted(snapshot_errors.items())
                    ),
                ),
                "incidents": tuple(self._incident_ids),
                "order_outcomes": (),
            },
            skip_reason=reason,
        )
        payload = _payload(result)
        self.repository.store_decision_receipt(
            run_id=self.run_id,
            decision_id=decision_id,
            payload=payload,
        )
        self.repository.complete_rebalance(
            decision_id,
            status=result.status,
            payload=payload,
            skip_reason=reason,
        )
        return result

    def _session_decided(self, session_date: date) -> bool:
        key = f"{self.idempotency_namespace}:{self.strategy_version}:{session_date.isoformat()}"
        return self.repository.get_rebalance_by_idempotency_key(key) is not None

    def _finalize_session_close(self, session: MarketSession) -> bool:
        """Reconcile and persist the explicit EOD snapshot before reporting."""

        marker_type = f"session_close_finalization:{session.session_date.isoformat()}"
        if self.repository.has_generated_report(marker_type):
            return True
        try:
            reconciliation = self.reconcile()
            if reconciliation.blocking:
                raise ReconciliationBlocked("market-close reconciliation is blocking")
            account = self.broker.get_account()
            positions = list(self.broker.get_positions())
            performance = self._record_forward_performance(
                account=account,
                positions=positions,
                session_date=session.session_date,
                now=self.clock.now(),
            )
            snapshot_id = self.repository.record_account_state(
                account,
                positions,
                run_id=self.run_id,
            )
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="system",
                event_type="market_close_snapshot_complete",
                created_at=self.clock.now(),
                payload={
                    "session_date": session.session_date,
                    "session_open_at": session.open_at,
                    "session_close_at": session.close_at,
                    "account_snapshot_id": snapshot_id,
                    "account_snapshot_time": account.timestamp,
                    "end_equity": account.equity,
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "performance": performance,
                },
            )
            self.repository.record_generated_report(
                run_id=self.run_id,
                report_type=marker_type,
                path=str(self.repository.database.path),
                metadata_values={
                    "session_date": session.session_date,
                    "session_open_at": session.open_at,
                    "session_close_at": session.close_at,
                    "finalized": True,
                    "account_snapshot_id": snapshot_id,
                    "reconciliation_id": reconciliation.reconciliation_id,
                },
            )
            self._resolve_incidents("market_close_finalization_failure")
            return True
        except Exception as exc:
            if not self._incidents_by_type.get("market_close_finalization_failure"):
                self._record_incident(
                    "market_close_finalization_failure",
                    str(exc) or type(exc).__name__,
                    severity="warning",
                    details={"session_date": session.session_date.isoformat()},
                )
            return False

    def _generate_post_close_report(self, session: MarketSession) -> None:
        report_type = f"daily_forward_session:{session.session_date.isoformat()}"
        if self.repository.has_generated_report(report_type):
            return
        output_directory = str(
            _section_value(
                self.config,
                "project",
                "output_directory",
                "outputs/primary_forward_paper",
            )
        )
        feed = str(self.market_data.feed).upper()
        database_path = self.repository.database.path
        if feed not in {"IEX", "SIP"} or str(database_path) == ":memory:":
            self.repository.record_generated_report(
                run_id=self.run_id,
                report_type=report_type,
                path=output_directory,
                metadata_values={
                    "session_date": session.session_date,
                    "generated": False,
                    "reason": "report requires a durable database and IEX/SIP feed",
                },
            )
            return
        try:
            from adaptive_trader.forward_reporting import generate_forward_outputs

            artifacts = generate_forward_outputs(
                database_path,
                output_directory,
                feed=feed,
            )
            self.repository.record_generated_report(
                run_id=self.run_id,
                report_type=report_type,
                path=output_directory,
                metadata_values={
                    "session_date": session.session_date,
                    "generated": True,
                    "artifacts": {name: str(path) for name, path in artifacts.items()},
                },
            )
        except Exception as exc:
            incident_id = self._record_incident(
                "post_close_report_failure",
                str(exc) or type(exc).__name__,
                severity="warning",
                details={"session_date": session.session_date.isoformat()},
            )
            # Mark the attempt durably so the daemon does not repeat the same
            # failing generation on every post-close poll.
            self.repository.record_generated_report(
                run_id=self.run_id,
                report_type=report_type,
                path=output_directory,
                metadata_values={
                    "session_date": session.session_date,
                    "generated": False,
                    "reason": "generation_failed",
                    "incident_id": incident_id,
                },
            )

    def run(
        self,
        *,
        poll_seconds: float = 1.0,
        max_iterations: int | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """Run monitors and one decision per session, optionally for a bounded time."""

        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        deadline = (
            None
            if duration_seconds is None
            else self.clock.now() + timedelta(seconds=float(duration_seconds))
        )

        self.start_streams()
        iterations = 0
        evaluation = _parse_time(
            _section_value(self.config, "schedule", "evaluation_time_et", "10:05"),
            time(10, 5),
        )
        cutoff = _parse_time(
            _section_value(self.config, "schedule", "catch_up_cutoff_et", "14:30"),
            time(14, 30),
        )
        heartbeat_interval = float(
            _section_value(self.config, "schedule", "heartbeat_interval_seconds", 30)
        )
        risk_interval = float(
            _section_value(self.config, "schedule", "risk_monitor_interval_seconds", 60)
        )
        reconciliation_interval = float(
            _section_value(self.config, "schedule", "reconciliation_interval_seconds", 300)
        )
        open_order_interval = float(
            _section_value(
                self.config,
                "schedule",
                "open_order_monitor_interval_seconds",
                30,
            )
        )
        while (
            not self._stop.is_set()
            and (max_iterations is None or iterations < max_iterations)
            and (deadline is None or self.clock.now() < deadline)
        ):
            now = self.clock.now()
            now_et = now.astimezone(NEW_YORK)
            if self._monitor_due(now, self._last_reconciliation_at, reconciliation_interval):
                try:
                    self.reconcile()
                    self._resolve_incidents("reconciliation_monitor_failure")
                except Exception as exc:
                    self._record_incident("reconciliation_monitor_failure", str(exc))
                    self._last_reconciliation_at = now
            if self._monitor_due(now, self._last_risk_snapshot_at, risk_interval):
                try:
                    self._monitor_risk_snapshot(now)
                    self._resolve_incidents("risk_monitor_failure")
                except Exception as exc:
                    self._record_incident("risk_monitor_failure", str(exc))
                    self._last_risk_snapshot_at = now
            if self._monitor_due(now, self._last_open_order_monitor_at, open_order_interval):
                try:
                    self._monitor_open_orders(now)
                    self._resolve_incidents("open_order_monitor_failure")
                except Exception as exc:
                    self._record_incident("open_order_monitor_failure", str(exc))
                    self._last_open_order_monitor_at = now
            session_date = now_et.date()
            scheduler_stage = "calendar"
            scheduler_incident_active = bool(
                self._incidents_by_type.get("scheduled_session_iteration_failure")
            )
            try:
                session = self._scheduled_market_session(session_date, now)
                if session is not None:
                    wall_time = now_et.time().replace(tzinfo=None)
                    scheduled_at = datetime.combine(
                        session_date,
                        evaluation,
                        tzinfo=NEW_YORK,
                    ).astimezone(UTC)
                    session_decided = self._session_decided(session_date)
                    if wall_time > cutoff and not session_decided:
                        scheduler_stage = "missed_after_cutoff_receipt"
                        self._record_missed_after_cutoff(
                            session=session,
                            scheduled_at=scheduled_at,
                            now=now,
                        )
                    elif wall_time >= evaluation and not session_decided:
                        scheduler_stage = "scheduled_decision"
                        self.run_once(
                            session_date=session_date,
                            scheduled_at=scheduled_at,
                        )
                    if now >= session.close_at:
                        scheduler_stage = "market_close_finalization"
                        if self._finalize_session_close(session):
                            scheduler_stage = "post_close_report"
                            self._generate_post_close_report(session)
            except Exception as exc:
                if not scheduler_incident_active:
                    self._record_incident(
                        "scheduled_session_iteration_failure",
                        str(exc) or type(exc).__name__,
                        severity="warning",
                        details={
                            "stage": scheduler_stage,
                            "session_date": session_date.isoformat(),
                        },
                    )
                    self._last_heartbeat_at = None
            else:
                if scheduler_incident_active:
                    self._resolve_incidents("scheduled_session_iteration_failure")
                    self._last_heartbeat_at = None
            if self._monitor_due(now, self._last_heartbeat_at, heartbeat_interval):
                try:
                    components = self._heartbeat_components(now)
                    heartbeat_healthy, health_reasons = self._heartbeat_health(components)
                    components["health_reasons"] = health_reasons
                    self.repository.heartbeat(
                        run_id=self.run_id,
                        mode=self.mode.value,
                        healthy=heartbeat_healthy,
                        components=components,
                        created_at=now,
                    )
                    self._last_heartbeat_at = now
                    self._resolve_incidents("heartbeat_monitor_failure")
                except Exception as exc:
                    if not self._incidents_by_type.get("heartbeat_monitor_failure"):
                        self._record_incident(
                            "heartbeat_monitor_failure",
                            str(exc) or type(exc).__name__,
                            severity="warning",
                        )
                    self._last_heartbeat_at = now
            iterations += 1
            if self._stop.is_set():
                break
            if max_iterations is not None and iterations >= max_iterations:
                break
            asyncio.run(self.clock.sleep(poll_seconds))

    def final_observer_checkpoint(self) -> dict[str, Any]:
        """Persist the bounded observer's final read-only reconciliation/heartbeat."""

        if self.mode is not RunMode.OBSERVE:
            raise SafetyViolation("Final observer checkpoints require observe mode")
        reconciliation = self.reconcile()
        now = self.clock.now()
        components = self._heartbeat_components(now)
        healthy, health_reasons = self._heartbeat_health(components)
        components["health_reasons"] = health_reasons
        components["final_observer_checkpoint"] = True
        self.repository.heartbeat(
            run_id=self.run_id,
            mode=self.mode.value,
            healthy=healthy,
            components=components,
            created_at=now,
        )
        self._last_heartbeat_at = now
        return {
            "reconciliation_id": reconciliation.reconciliation_id,
            "reconciliation_clean": reconciliation.clean,
            "reconciliation_blocking": reconciliation.blocking,
            "healthy": healthy,
            "health_reasons": health_reasons,
            "created_at": now,
        }

    def status(self) -> dict[str, Any]:
        latest_reconciliation = self.repository.latest_reconciliation()
        now = self.clock.now()
        freshness = self.bar_store.freshness(now)
        components = self._heartbeat_components(now)
        healthy, health_reasons = self._heartbeat_health(components)
        return {
            "run_id": self.run_id,
            "mode": self.mode.value,
            "paper_only": self.broker.paper_only,
            "feed": self.market_data.feed,
            "feed_entitlement_verified": self._feed_entitlement_verified,
            "assets_verified": self._assets_verified,
            "history_preflight_verified": self._history_preflight_verified,
            "universe": list(self.universe),
            "stream_connected": freshness.stream_healthy,
            "trade_updates_healthy": self._trade_updates_healthy(),
            "trade_updates_status": self._trade_updates_status(),
            "fresh": freshness.fresh,
            "healthy": healthy,
            "health_reasons": health_reasons,
            "active_incidents": tuple(self._incident_ids),
            "active_halts": sorted(self.repository.active_halts()),
            "latest_reconciliation": latest_reconciliation,
            "open_local_orders": len(self.repository.list_orders(open_only=True)),
        }

    def halt(
        self,
        reason: str,
        *,
        initiator: str = "cli",
        flatten: bool = False,
        acknowledgement: str | None = None,
    ) -> str:
        if not str(reason).strip():
            raise ValueError("A nonempty halt reason is required")
        if flatten and acknowledgement != PAPER_FLATTEN_ACKNOWLEDGEMENT:
            raise SafetyViolation("Paper flatten requires the exact liquidation acknowledgement")
        cancellation_authorized = self._paper_cancellation_authorized(dry_run=self.dry_run)
        executable_flatten_requested = bool(flatten and cancellation_authorized)
        event_id = self.repository.record_halt(
            run_id=self.run_id,
            action="halt",
            latch_type="manual",
            initiator=initiator,
            reason=reason,
            acknowledgement=acknowledgement,
            session_date=self._session_date(self.clock.now()),
            created_at=self.clock.now(),
            details={
                "flatten_requested": executable_flatten_requested,
                "flatten_refused_by_service_mode": bool(flatten and not cancellation_authorized),
            },
        )
        if not cancellation_authorized:
            if flatten:
                raise SafetyViolation(
                    "Paper cancellation authority is incomplete; no cancel or "
                    "liquidation request was sent"
                )
            return event_id
        self._request_operator_halt_cancellation(
            halt_event_id=event_id,
            flatten_requested=flatten,
        )
        if flatten:
            now = self.clock.now()
            # Cancellation is emergency-authorized, while liquidation orders
            # require bounded market/trade-stream readiness and the complete
            # submission gate.  Reconciliation remains useful even when the
            # account is already flat, so the durable cancel outcome is clear.
            cancellation_reconciliation = self.reconcile()
            remaining_positions = list(self.broker.get_positions())
            if remaining_positions:
                try:
                    self.ensure_ready(require_trade_updates=True)
                except Exception as exc:
                    reason = redact(str(exc) or type(exc).__name__)
                    self.repository.record_stream_event(
                        run_id=self.run_id,
                        stream="orders",
                        event_type="operator_flatten_readiness_failed",
                        created_at=self.clock.now(),
                        payload={
                            "halt_event_id": event_id,
                            "reconciliation_id": (cancellation_reconciliation.reconciliation_id),
                            "reason": reason,
                        },
                    )
                    raise ReconciliationBlocked(
                        "Halt was recorded and orders canceled, but flatten readiness "
                        f"failed: {reason}"
                    ) from None

            max_immediate_attempts = max(
                1,
                min(
                    100,
                    int(
                        _section_value(
                            self.config,
                            "execution",
                            "operator_flatten_max_immediate_attempts",
                            25,
                        )
                    ),
                ),
            )
            latest_result: LiveCycleResult | None = None
            for _ in range(max_immediate_attempts):
                latest_result, gate = self._operator_flatten_attempt(
                    request_id=event_id,
                    now=now,
                )
                if latest_result is None:
                    if not gate.allowed:
                        raise ReconciliationBlocked(
                            "Halt was recorded and orders canceled, but flatten was blocked: "
                            + "; ".join(gate.reasons)
                        )
                    break
                remaining_positions = list(self.broker.get_positions())
                if not remaining_positions:
                    break
                submitted = set(latest_result.submitted_client_order_ids)
                broker_orders = {
                    order.client_order_id: order
                    for order in self.broker.get_orders(include_closed=True)
                    if order.client_order_id in submitted
                }
                # Continue synchronously only after an immediately completed
                # fill.  Accepted, ambiguous, rejected, canceled or partial
                # attempts are retried by the durable monitor after broker
                # terminal evidence; they are never duplicated here.
                if (
                    not submitted
                    or any(
                        order.status.lower() not in {"fill", "filled"}
                        for order in broker_orders.values()
                    )
                    or len(broker_orders) != len(submitted)
                ):
                    break
            self.repository.record_stream_event(
                run_id=self.run_id,
                stream="orders",
                event_type="operator_flatten_progress",
                created_at=self.clock.now(),
                payload={
                    "halt_event_id": event_id,
                    "latest_decision_id": (
                        None if latest_result is None else latest_result.decision_id
                    ),
                    "latest_status": None if latest_result is None else latest_result.status,
                    "remaining_positions": {
                        position.symbol: str(position.quantity)
                        for position in self.broker.get_positions()
                    },
                },
            )
        return event_id

    def resume(
        self,
        *,
        acknowledgement: str,
        reason: str = "operator reviewed paper account",
        initiator: str = "cli",
    ) -> tuple[str, ...]:
        if acknowledgement != PAPER_RESUME_ACKNOWLEDGEMENT:
            raise SafetyViolation("Resume requires the exact paper-account acknowledgement")
        reconciliation = self.reconcile()
        freshness = self.bar_store.freshness(self.clock.now())
        if reconciliation.blocking or not freshness.fresh:
            raise ReconciliationBlocked(
                "Resume requires clean reconciliation and fresh market data"
            )
        identifiers: list[str] = []
        resumable_latches = {"operator", "manual", "hard_stop"}
        for latch_type in self.repository.active_halts(self.clock.now()):
            if latch_type not in resumable_latches:
                continue
            identifiers.append(
                self.repository.record_halt(
                    run_id=self.run_id,
                    action="resume",
                    latch_type=latch_type,
                    initiator=initiator,
                    reason=reason,
                    acknowledgement=acknowledgement,
                    session_date=self._session_date(self.clock.now()),
                    created_at=self.clock.now(),
                )
            )
        return tuple(identifiers)

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self, reason: str = "normal_shutdown") -> None:
        if self._closed:
            return
        self._stop.set()
        now = self.clock.now()
        try:
            with suppress(Exception):
                self.repository.record_stream_event(
                    run_id=self.run_id,
                    stream="system",
                    event_type="shutdown_started",
                    payload={"reason": reason},
                    created_at=now,
                )
            stop_errors: list[str] = []
            if self._streams_started:
                try:
                    self.market_data.stop_stream()
                except Exception as exc:
                    stop_errors.append(f"market_data:{redact(str(exc))}")
                try:
                    self.broker.stop_trade_updates()
                except Exception as exc:
                    stop_errors.append(f"trade_updates:{redact(str(exc))}")
                self._streams_started = False
            with suppress(Exception):
                self.repository.record_stream_event(
                    run_id=self.run_id,
                    stream="system",
                    event_type="shutdown_streams_stopped",
                    payload={"errors": stop_errors},
                    created_at=self.clock.now(),
                )
            if stop_errors:
                with suppress(Exception):
                    self._record_incident(
                        "shutdown_stream_stop_failure",
                        "; ".join(stop_errors),
                        severity="warning",
                    )
            try:
                account = self.broker.get_account()
                positions = list(self.broker.get_positions())
                self._record_forward_performance(
                    account=account,
                    positions=positions,
                    session_date=self._session_date(account.timestamp),
                    now=now,
                )
                final_reconciliation = self.reconcile()
                self.repository.record_stream_event(
                    run_id=self.run_id,
                    stream="system",
                    event_type="shutdown_reconciliation_complete",
                    payload={
                        "reconciliation_id": final_reconciliation.reconciliation_id,
                        "blocking": final_reconciliation.blocking,
                    },
                    created_at=self.clock.now(),
                )
            except Exception as exc:
                self._record_incident(
                    "shutdown_reconciliation_failure",
                    str(exc) or type(exc).__name__,
                    severity="warning",
                )
        finally:
            try:
                self.repository.end_run(self.run_id, reason)
            finally:
                self._closed = True


def one_shot(
    config: Any,
    *,
    repository: AuditRepository,
    broker: Broker,
    market_data: MarketDataProvider,
    target_weights: Mapping[str, float] | None = None,
    mode: RunMode | str = RunMode.OBSERVE,
    dry_run: bool = False,
    clock: Clock | None = None,
    environment: Mapping[str, str] | None = None,
) -> LiveCycleResult:
    """Convenience entry point for CLI integration."""

    service = LiveService(
        config,
        repository=repository,
        broker=broker,
        market_data=market_data,
        mode=mode,
        clock=clock,
        environment=environment,
        dry_run=dry_run,
    )
    try:
        service.start_streams()
        return service.run_once(target_weights, dry_run=dry_run)
    finally:
        service.shutdown()
