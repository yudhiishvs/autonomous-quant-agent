"""Broker-free shadow evaluation of durable data using a declared virtual cash account.

Shadow is diagnostic: its account is explicitly simulated, never a claim about a broker account.
The ordinary provider is the registered always-flat baseline, not an inferred approved AI.
Fixture signals require explicit offline/fake selection. Neither path constructs a broker.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, column, select, table

from adaptive_trader.platform.config import BrokerAdapter, ExecutionMode
from adaptive_trader.platform.domain import AuditPayload, AuditWriter, require_utc_instant
from adaptive_trader.platform.execution.models import Position
from adaptive_trader.platform.execution.planner import ExecutionPlanningRequest, plan_signed_orders
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    AccountSnapshot,
    MarketIntegritySnapshot,
    OpenOrderSnapshot,
    PlanningPrice,
    ReconciliationSnapshot,
    RiskEvaluationRequest,
    SecurityMetadataSnapshot,
    SignedPosition,
    compute_risk_statistics,
    evaluate_signed_risk,
    policy_hash,
)
from adaptive_trader.platform.scheduling.models import DecisionType, SlotState
from adaptive_trader.platform.service_cycles import (
    _complete_history,
    _effective_aggregate_rows,
    _read_slot,
)
from adaptive_trader.platform.shadow_settings import (
    ShadowExecutionSettings,
    require_shadow_database_role,
    shadow_readiness_is_current,
)
from adaptive_trader.platform.signals.models import (
    DecisionContext,
    SignalAction,
    SignalEnvelope,
    SignalSourceMode,
)
from adaptive_trader.platform.storage.execution import SignedExecutionRepository
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.risk import SignedRiskRepository
from adaptive_trader.platform.storage.tables import aqa_data_gaps, aqa_signal_envelopes


def run_shadow_once(
    *,
    engine: Engine,
    settings: ShadowExecutionSettings,
    slot_id: str | None,
    now: datetime,
    fixture: bool = False,
) -> dict[str, object]:
    """Validate durable inputs, evaluate independent risk and persist an unsubmitted plan.

    Missing input yields durable blocked evidence. Existing persisted signals are validated again;
    changed canonical revisions or unavailable history cannot be silently substituted.
    """
    now = require_utc_instant(now, field_name="shadow_clock")
    if type(settings) is not ShadowExecutionSettings or settings.fixture is not fixture:
        raise ValueError("shadow requires disabled submission and an exact diagnostic mode")
    require_shadow_database_role(engine, role="aqa_execution", fixture=fixture)
    profile = settings.platform.profile
    if (
        profile.execution.submission_enabled
        or (
            fixture
            and (
                profile.mode is not ExecutionMode.OFFLINE
                or profile.execution.broker is not BrokerAdapter.FAKE
            )
        )
        or (
            not fixture
            and (
                profile.mode is not ExecutionMode.SHADOW
                or profile.execution.broker is not BrokerAdapter.NONE
            )
        )
    ):
        raise ValueError("shadow requires disabled submission and an exact diagnostic mode")
    experiment = settings.platform.experiment.definition
    audit = AuditRepository(engine, writer=AuditWriter.EXECUTION)

    def blocked(reason: str) -> dict[str, object]:
        audit.append(
            stream_id=f"aqa_execution:shadow:{experiment.content_hash}",
            event_type="execution.shadow_blocked",
            occurred_at=now,
            payload=AuditPayload.from_mapping(
                {
                    "reason_code": reason,
                    "mode": "shadow",
                    "idempotency_key": "shadow_" + sha256_hex((slot_id, now, reason)),
                }
            ),
        )
        return {"status": "blocked", "reason_code": reason, "broker_constructed": False}

    if slot_id is None:
        return blocked("decision_slot_required")
    slot = _read_slot(engine, slot_id)
    if slot is None or slot.experiment_hash != experiment.content_hash:
        return blocked("decision_slot_unavailable")
    if not fixture and (
        slot.state is not SlotState.CLAIMED
        or slot.claim_owner != "strategy_worker"
        or slot.lease_expires_at is None
        or slot.lease_expires_at <= now
    ):
        return blocked("decision_slot_unavailable")
    if not fixture and slot.signal_provider_id != profile.signal_provider.id:
        return blocked("approved_signal_unavailable")
    if not slot.ready_at <= now < slot.deadline_at - timedelta(seconds=1):
        return blocked("decision_slot_not_current")
    if not fixture and not shadow_readiness_is_current(
        engine,
        experiment_hash=experiment.content_hash,
        timeframe="1Min" if slot.decision_type is DecisionType.FORCED_FLAT else "15Min",
        interval_end=slot.source_interval_end,
        now=now,
    ):
        return blocked("canonical_readiness_unavailable")
    provider = "fixture" if fixture else "alpaca"
    with engine.connect() as connection:
        gap = connection.scalar(
            select(aqa_data_gaps.c.gap_id)
            .where(
                aqa_data_gaps.c.experiment_hash == experiment.content_hash,
                aqa_data_gaps.c.provider == provider,
                aqa_data_gaps.c.symbol.in_(experiment.active_tradable),
                aqa_data_gaps.c.status.in_(("open", "repairing")),
                aqa_data_gaps.c.gap_start_at < slot.source_interval_end,
            )
            .limit(1)
        )
    if gap is not None:
        return blocked("unresolved_data_gap")
    rows = tuple(
        row
        for row in _effective_aggregate_rows(
            engine,
            provider=provider,
            start_at=slot.source_interval_start,
            end_at=slot.source_interval_end,
            timeframe="1Min" if slot.decision_type is DecisionType.FORCED_FLAT else "15Min",
        )
        if row.symbol in experiment.active_tradable
    )
    if (
        len(rows) != len(experiment.active_tradable)
        or {row.symbol for row in rows} != set(experiment.active_tradable)
        or any(
            row.received_at > now
            or row.quality_flags != ("complete",)
            or row.source_mode != ("offline_fixture" if fixture else "external_provider")
            for row in rows
        )
    ):
        return blocked("canonical_data_unavailable")
    history = _complete_history(
        engine,
        active_symbols=experiment.active_tradable,
        before_session=slot.session_date,
        provider=provider,
        observed_at=now,
    )
    if history is None:
        return blocked("risk_history_unavailable")
    data_hash = sha256_hex(tuple(sorted(row.source_payload_hash for row in rows)))
    context = DecisionContext.from_experiment(
        slot=slot,
        experiment=experiment,
        data_contract_hash=data_hash,
        policy_hash=policy_hash(experiment.risk_policy, experiment.risk_groups),
        execution_mode=profile.mode,
        broker_adapter=profile.execution.broker,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )
    signal = _read_shadow_signal(engine, slot.slot_id)
    if signal is None:
        return blocked("approved_signal_unavailable")
    signal.validate_for(context)
    if (
        signal.created_at > now
        or signal.expires_at <= now
        or (signal.provider_source_mode is SignalSourceMode.OFFLINE_FIXTURE and not fixture)
    ):
        return blocked("signal_unavailable")
    risk = SignedRiskRepository(engine)
    decision = risk.decision_for_signal(signal.signal_id)
    virtual_account_hash = sha256_hex("shadow_virtual_cash_account_v1")
    if decision is not None and decision.account_snapshot.account_id_hash != virtual_account_hash:
        return blocked("existing_risk_is_not_shadow")
    if decision is None:
        equity = Decimal("100000")
        decision = evaluate_signed_risk(
            request=RiskEvaluationRequest(
                signal=signal,
                decision_context=context,
                positions=tuple(
                    SignedPosition(symbol, Decimal(0)) for symbol in experiment.active_tradable
                ),
                open_orders=OpenOrderSnapshot.create(
                    reserved_signed_notional={
                        symbol: Decimal(0) for symbol in experiment.active_tradable
                    },
                    conflicting_symbols=(),
                    ambiguous_order_exists=False,
                    observed_at=now,
                ),
                account=AccountSnapshot(virtual_account_hash, equity, equity, equity, now),
                prices=tuple(
                    PlanningPrice(row.symbol, row.close, now, True)
                    for row in sorted(rows, key=lambda item: item.symbol)
                ),
                security_metadata=tuple(
                    SecurityMetadataSnapshot(
                        symbol=symbol,
                        asset_active=True,
                        tradable=True,
                        shortable=False,
                        easy_to_borrow=False,
                        primary_listing_eligible=True,
                        broker_capability_known=False,
                        observed_at=now,
                    )
                    for symbol in experiment.active_tradable
                ),
                reconciliation=ReconciliationSnapshot.create(
                    reconciled=True, ambiguous_order_exists=False, observed_at=now
                ),
                market_integrity=MarketIntegritySnapshot.create(
                    active_basket_complete=True,
                    unresolved_gap=False,
                    correction_uncertainty=False,
                    supported_session=True,
                ),
                session_start_equity=equity,
                deployment_high_water_equity=equity,
                statistics=compute_risk_statistics(
                    active_symbols=experiment.active_tradable,
                    history=history,
                    as_of_date=slot.session_date,
                    eigenvalue_floor=experiment.risk_policy.covariance_eigenvalue_floor,
                ),
                latch_state=risk.latch_state(experiment.content_hash),
                operator_halt=False,
                evaluated_at=now,
            ),
            experiment=experiment,
        )
        risk.persist(decision)
    # Use the original deterministic creation time on retries, preserving immutable plan identity.
    planning = plan_signed_orders(
        ExecutionPlanningRequest(
            risk_decision=decision,
            current_positions=tuple(
                Position(item.symbol, item.quantity) for item in decision.planning_positions
            ),
            reference_prices=tuple((item.symbol, item.price) for item in decision.planning_prices),
            equity=decision.account_snapshot.equity,
            target_version=1,
            created_at=decision.decided_at + timedelta(microseconds=1),
            deadline_at=slot.deadline_at,
            forced_flat=slot.decision_type is DecisionType.FORCED_FLAT,
        )
    )
    SignedExecutionRepository(engine).persist_plan_and_intents(
        planning.plan, planning.intents, risk_decision=decision
    )
    return {
        "status": "dry_run_persisted",
        "execution_plan_id": planning.plan.execution_plan_id,
        "intent_count": len(planning.intents),
        "broker_constructed": False,
        "account_model": "virtual_cash_only",
        "risk_block_reasons": list(decision.block_reasons),
    }


def _read_shadow_signal(engine: Engine, slot_id: str) -> SignalEnvelope | None:
    """Consume declarative persisted output without constructing a signal provider."""
    relation = aqa_signal_envelopes
    if engine.dialect.name == "postgresql":
        relation = table(  # type: ignore[assignment]
            "aqa_signals_v",
            *(column(item.name, item.type) for item in aqa_signal_envelopes.columns),
            schema="aqa",
        )
    with engine.connect() as connection:
        row = (
            connection.execute(select(relation).where(relation.c.slot_id == slot_id))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    values = dict(row)
    values["provider_source_mode"] = SignalSourceMode(values["provider_source_mode"])
    values["actions"] = tuple(SignalAction(value) for value in values["actions"])
    for name in ("active_symbols", "availability_mask"):
        values[name] = tuple(values[name])
    for name in ("expected_edge_bps", "proposed_signed_target_inputs"):
        values[name] = tuple(None if value is None else Decimal(value) for value in values[name])
    return SignalEnvelope(**values)
