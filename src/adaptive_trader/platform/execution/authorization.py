"""Fail-closed execution-time freshness and paper submission gates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from adaptive_trader.platform.config import ExecutionMode
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.execution.models import (
    ExecutionValidationError,
    OrderIntent,
    PositionEffect,
)
from adaptive_trader.platform.signals.models import (
    PaperAuthorizationDecision,
    SignalEnvelope,
    SignalSourceMode,
)

PAPER_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_AQA_PAPER_ONLY"
ACCOUNT_FRESHNESS = timedelta(seconds=30)
SECURITY_FRESHNESS = timedelta(seconds=300)
RECONCILIATION_FRESHNESS = timedelta(seconds=60)
PLANNING_PRICE_FRESHNESS = timedelta(seconds=120)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)


@dataclass(frozen=True, slots=True)
class SubmissionSafetySnapshot:
    """Current state that must be revalidated immediately before any side effect."""

    evaluated_at: datetime
    session_open: bool
    data_complete: bool
    account_observed_at: datetime
    security_observed_at: datetime
    reconciliation_observed_at: datetime
    price_observed_at: datetime
    reconciliation_clean: bool
    ambiguous_order_exists: bool
    blocking_latch_exists: bool
    entry_disabled: bool
    active_symbols: tuple[str, ...]
    shortable_symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        now = _instant(self.evaluated_at)
        for value in (
            self.session_open,
            self.data_complete,
            self.reconciliation_clean,
            self.ambiguous_order_exists,
            self.blocking_latch_exists,
            self.entry_disabled,
        ):
            if type(value) is not bool:
                raise ExecutionValidationError("submission safety flags must be boolean")
        for observed in (
            self.account_observed_at,
            self.security_observed_at,
            self.reconciliation_observed_at,
            self.price_observed_at,
        ):
            instant = _instant(observed)
            if instant > now:
                raise ExecutionValidationError("submission state cannot be observed in the future")
        for symbols in (self.active_symbols, self.shortable_symbols):
            if type(symbols) is not tuple or symbols != tuple(sorted(set(symbols))):
                raise ExecutionValidationError("submission symbols must be unique and ordered")
        if not set(self.shortable_symbols).issubset(self.active_symbols):
            raise ExecutionValidationError("shortable symbols must be active")


@dataclass(frozen=True, slots=True)
class SubmissionAuthorization:
    """Deterministically ordered authorization reasons."""

    approved: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise ExecutionValidationError("submission decision flag must be boolean")
        if type(self.reasons) is not tuple or self.reasons != tuple(sorted(set(self.reasons))):
            raise ExecutionValidationError("submission reasons must be unique and ordered")
        if self.approved == bool(self.reasons):
            raise ExecutionValidationError("submission decision and reasons disagree")


@dataclass(frozen=True, slots=True)
class PaperGateContext:
    """All independent gates required before an Alpaca paper adapter can be invoked."""

    runtime_mode: ExecutionMode
    submission_enabled: bool
    acknowledgement: str | None
    adapter_paper_only: bool
    secret_files_valid: bool
    configured_account_id_hash: str
    observed_account_id_hash: str
    signal: SignalEnvelope
    model_authorization: PaperAuthorizationDecision
    safety: SubmissionSafetySnapshot

    def __post_init__(self) -> None:
        if type(self.runtime_mode) is not ExecutionMode:
            raise ExecutionValidationError("paper runtime mode is invalid")
        for value in (
            self.submission_enabled,
            self.adapter_paper_only,
            self.secret_files_valid,
        ):
            if type(value) is not bool:
                raise ExecutionValidationError("paper gate flags must be boolean")
        if (
            type(self.configured_account_id_hash) is not str
            or _SHA256.fullmatch(self.configured_account_id_hash) is None
        ):
            raise ExecutionValidationError("configured account hash is invalid")
        if (
            type(self.observed_account_id_hash) is not str
            or _SHA256.fullmatch(self.observed_account_id_hash) is None
        ):
            raise ExecutionValidationError("observed account hash is invalid")
        if type(self.signal) is not SignalEnvelope:
            raise ExecutionValidationError("paper gate signal is invalid")
        if type(self.model_authorization) is not PaperAuthorizationDecision:
            raise ExecutionValidationError("paper model authorization is invalid")
        if type(self.safety) is not SubmissionSafetySnapshot:
            raise ExecutionValidationError("paper safety snapshot is invalid")


def authorize_intent(
    intent: OrderIntent,
    *,
    safety: SubmissionSafetySnapshot,
) -> SubmissionAuthorization:
    """Revalidate one intent immediately before broker submission."""

    if type(intent) is not OrderIntent or type(safety) is not SubmissionSafetySnapshot:
        raise ExecutionValidationError("intent authorization requires validated inputs")
    reasons = _safety_reasons(intent, safety=safety)
    return _decision(reasons)


def authorize_paper_intent(
    intent: OrderIntent,
    *,
    context: PaperGateContext,
) -> SubmissionAuthorization:
    """Evaluate all ten paper gates; tracked behavior remains default-deny."""

    if type(intent) is not OrderIntent or type(context) is not PaperGateContext:
        raise ExecutionValidationError("paper authorization requires validated inputs")
    reasons = list(_safety_reasons(intent, safety=context.safety))
    if context.runtime_mode is not ExecutionMode.PAPER:
        reasons.append("runtime_not_paper")
    if not context.submission_enabled:
        reasons.append("submission_disabled")
    if context.acknowledgement != PAPER_ACKNOWLEDGEMENT:
        reasons.append("paper_acknowledgement_missing")
    if not context.adapter_paper_only:
        reasons.append("adapter_not_paper_only")
    if not context.secret_files_valid:
        reasons.append("paper_secret_files_invalid")
    if context.configured_account_id_hash != context.observed_account_id_hash:
        reasons.append("account_id_mismatch")
    if (
        context.signal.provider_source_mode is SignalSourceMode.OFFLINE_FIXTURE
        or not context.signal.promotable
        or not context.signal.paper_submission_eligible
    ):
        reasons.append("signal_not_paper_eligible")
    if (
        context.signal.experiment_hash != intent.experiment_hash
        or context.signal.correlation_id != intent.correlation_id
        or intent.symbol not in context.signal.active_symbols
    ):
        reasons.append("signal_intent_mismatch")
    if not context.model_authorization.approved:
        reasons.append(context.model_authorization.reason.value)
    return _decision(tuple(reasons))


def _safety_reasons(
    intent: OrderIntent,
    *,
    safety: SubmissionSafetySnapshot,
) -> tuple[str, ...]:
    now = safety.evaluated_at
    reasons: list[str] = []
    if now >= intent.deadline_at:
        reasons.append("intent_expired")
    if intent.symbol not in safety.active_symbols:
        reasons.append("symbol_not_active")
    if not safety.session_open:
        reasons.append("session_closed")
    if not safety.data_complete:
        reasons.append("market_data_incomplete")
    if now - safety.account_observed_at > ACCOUNT_FRESHNESS:
        reasons.append("account_stale")
    if now - safety.security_observed_at > SECURITY_FRESHNESS:
        reasons.append("security_metadata_stale")
    if now - safety.reconciliation_observed_at > RECONCILIATION_FRESHNESS:
        reasons.append("reconciliation_stale")
    if now - safety.price_observed_at > PLANNING_PRICE_FRESHNESS:
        reasons.append("planning_price_stale")
    if not safety.reconciliation_clean:
        reasons.append("reconciliation_blocking")
    if safety.ambiguous_order_exists:
        reasons.append("ambiguous_order_exists")
    if intent.position_effect.opens_exposure:
        if safety.blocking_latch_exists:
            reasons.append("blocking_latch_active")
        if safety.entry_disabled:
            reasons.append("entry_disabled")
        if (
            intent.position_effect in {PositionEffect.OPEN_SHORT, PositionEffect.INCREASE_SHORT}
            and intent.symbol not in safety.shortable_symbols
        ):
            reasons.append("short_not_eligible")
    return tuple(reasons)


def _decision(reasons: tuple[str, ...] | list[str]) -> SubmissionAuthorization:
    ordered = tuple(sorted(set(reasons)))
    return SubmissionAuthorization(approved=not ordered, reasons=ordered)


def _instant(value: object) -> datetime:
    try:
        return require_utc_instant(value, field_name="submission_timestamp")
    except DomainValidationError:
        raise ExecutionValidationError("submission timestamp must be timezone-aware UTC") from None
