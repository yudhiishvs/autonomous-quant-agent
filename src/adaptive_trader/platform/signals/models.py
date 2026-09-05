"""Strict immutable signal proposal and default-deny paper authorization contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from adaptive_trader.platform.config import BrokerAdapter, ExecutionMode, ExperimentDefinition
from adaptive_trader.platform.domain import (
    DeterministicId,
    require_finite_decimal,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.scheduling.models import DecisionSlot, DecisionType

SIGNAL_CONTRACT_VERSION = 1

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$", flags=re.ASCII)
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", flags=re.ASCII)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_SIGNAL_ID = re.compile(r"^signal_[0-9a-f]{64}$", flags=re.ASCII)
_CONTENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}_[0-9a-f]{64}$", flags=re.ASCII)


class SignalValidationError(DomainValidationError):
    """Raised when signal data violates the closed proposal contract."""


class SignalAction(StrEnum):
    """Closed per-symbol directional proposals."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class SignalSourceMode(StrEnum):
    """Bounded provenance labels for built-in and locally registered providers."""

    BUILTIN = "builtin"
    OFFLINE_FIXTURE = "offline_fixture"
    REGISTERED_PLUGIN = "registered_plugin"


class PaperAuthorizationReason(StrEnum):
    """Closed default-deny authorization reasons available in this release."""

    MODEL_APPROVAL_NOT_IMPLEMENTED = "model_approval_not_implemented"


@dataclass(frozen=True, slots=True)
class PaperAuthorizationDecision:
    """An authorization decision which cannot represent approval in this release."""

    approved: bool
    reason: PaperAuthorizationReason

    def __post_init__(self) -> None:
        if self.approved is not False:
            raise SignalValidationError("paper signal authorization is not implemented")
        if self.reason is not PaperAuthorizationReason.MODEL_APPROVAL_NOT_IMPLEMENTED:
            raise SignalValidationError("paper authorization reason is invalid")


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Read-only strategy input with no broker, credential, or order capability."""

    slot: DecisionSlot
    active_symbols: tuple[str, ...]
    benchmark_symbols: tuple[str, ...]
    context_symbols: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    data_contract_hash: str
    policy_hash: str
    execution_mode: ExecutionMode
    broker_adapter: BrokerAdapter
    submission_enabled: bool
    strategy_slot_ordinal: int | None

    def __post_init__(self) -> None:
        if type(self.slot) is not DecisionSlot:
            raise SignalValidationError("decision context requires an immutable slot")
        symbol_groups = (
            self.active_symbols,
            self.benchmark_symbols,
            self.context_symbols,
            self.excluded_symbols,
        )
        for symbols in symbol_groups:
            _require_symbol_tuple(symbols, allow_empty=True)
        if not self.active_symbols:
            raise SignalValidationError("decision context requires active symbols")
        flattened = tuple(symbol for group in symbol_groups for symbol in group)
        if len(flattened) != len(set(flattened)):
            raise SignalValidationError("decision context symbol roles must be disjoint")
        _require_hash(self.data_contract_hash, field_name="data contract hash")
        _require_hash(self.policy_hash, field_name="policy hash")
        if type(self.execution_mode) is not ExecutionMode:
            raise SignalValidationError("execution mode must use the closed contract")
        if type(self.broker_adapter) is not BrokerAdapter:
            raise SignalValidationError("broker adapter must use the closed contract")
        if type(self.submission_enabled) is not bool:
            raise SignalValidationError("submission flag must be boolean")
        expected_broker = {
            ExecutionMode.OFFLINE: BrokerAdapter.FAKE,
            ExecutionMode.SHADOW: BrokerAdapter.NONE,
            ExecutionMode.PAPER: BrokerAdapter.ALPACA_PAPER,
        }[self.execution_mode]
        if self.broker_adapter is not expected_broker:
            raise SignalValidationError("broker adapter does not match the execution mode")
        if self.execution_mode is not ExecutionMode.PAPER and self.submission_enabled:
            raise SignalValidationError("submission must be disabled outside paper mode")
        if self.slot.decision_type is DecisionType.STRATEGY:
            if (
                type(self.strategy_slot_ordinal) is not int
                or not 0 <= self.strategy_slot_ordinal < 20
            ):
                raise SignalValidationError("strategy slot requires an ordinal from zero to 19")
        elif self.strategy_slot_ordinal is not None:
            raise SignalValidationError("forced-flat context cannot have a strategy ordinal")

    @classmethod
    def from_experiment(
        cls,
        *,
        slot: DecisionSlot,
        experiment: ExperimentDefinition,
        data_contract_hash: str,
        policy_hash: str,
        execution_mode: ExecutionMode,
        broker_adapter: BrokerAdapter,
        submission_enabled: bool,
        strategy_slot_ordinal: int | None,
    ) -> DecisionContext:
        """Bind a slot to the exact immutable role sets of its experiment."""

        if type(experiment) is not ExperimentDefinition:
            raise SignalValidationError("decision context requires a validated experiment")
        if (
            slot.experiment_id != experiment.experiment_id
            or slot.experiment_version != experiment.experiment_version
            or slot.experiment_hash != experiment.content_hash
        ):
            raise SignalValidationError("slot and experiment identities do not match")
        return cls(
            slot=slot,
            active_symbols=experiment.active_tradable,
            benchmark_symbols=experiment.benchmark_only,
            context_symbols=experiment.context_only,
            excluded_symbols=experiment.excluded,
            data_contract_hash=data_contract_hash,
            policy_hash=policy_hash,
            execution_mode=execution_mode,
            broker_adapter=broker_adapter,
            submission_enabled=submission_enabled,
            strategy_slot_ordinal=strategy_slot_ordinal,
        )


@dataclass(frozen=True, slots=True)
class SignalEnvelope:
    """A declarative, versioned, hash-bound target proposal.

    The active-symbol-indexed tuples are immutable and positionally aligned. ``signal_id`` is
    ``signal_`` plus SHA-256 of the complete semantic payload. ``content_hash`` is the same
    canonical payload digest, making retries byte-stable and preventing identity substitution.
    """

    contract_version: int
    signal_id: str
    slot_id: str
    correlation_id: str
    provider_id: str
    provider_version: str
    provider_source_mode: SignalSourceMode
    experiment_id: str
    experiment_version: int
    experiment_hash: str
    data_contract_hash: str
    policy_hash: str
    source_bar_end: datetime
    created_at: datetime
    expires_at: datetime
    active_symbols: tuple[str, ...]
    availability_mask: tuple[bool, ...]
    actions: tuple[SignalAction, ...]
    expected_edge_bps: tuple[Decimal | None, ...]
    proposed_signed_target_inputs: tuple[Decimal | None, ...]
    artifact_id: str | None
    artifact_hash: str | None
    promotable: bool
    paper_submission_eligible: bool
    content_hash: str

    def __post_init__(self) -> None:
        _validate_envelope(self)

    @classmethod
    def create(
        cls,
        *,
        context: DecisionContext,
        provider_id: str,
        provider_version: str,
        provider_source_mode: SignalSourceMode,
        created_at: datetime,
        availability_mask: tuple[bool, ...],
        actions: tuple[SignalAction, ...],
        expected_edge_bps: tuple[Decimal | None, ...],
        proposed_signed_target_inputs: tuple[Decimal | None, ...],
        artifact_id: str | None = None,
        artifact_hash: str | None = None,
        promotable: bool = False,
        paper_submission_eligible: bool = False,
    ) -> SignalEnvelope:
        """Construct and context-validate one deterministic proposal."""

        if type(context) is not DecisionContext:
            raise SignalValidationError("signal creation requires an immutable decision context")
        instant = _utc(created_at, field_name="created_at")
        values: dict[str, object] = {
            "contract_version": SIGNAL_CONTRACT_VERSION,
            "slot_id": context.slot.slot_id,
            "correlation_id": context.slot.correlation_id,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "provider_source_mode": provider_source_mode,
            "experiment_id": context.slot.experiment_id,
            "experiment_version": context.slot.experiment_version,
            "experiment_hash": context.slot.experiment_hash,
            "data_contract_hash": context.data_contract_hash,
            "policy_hash": context.policy_hash,
            "source_bar_end": context.slot.source_interval_end,
            "created_at": instant,
            "expires_at": context.slot.deadline_at,
            "active_symbols": context.active_symbols,
            "availability_mask": availability_mask,
            "actions": actions,
            "expected_edge_bps": expected_edge_bps,
            "proposed_signed_target_inputs": proposed_signed_target_inputs,
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "promotable": promotable,
            "paper_submission_eligible": paper_submission_eligible,
        }
        digest = sha256_hex(values)
        envelope = cls(
            contract_version=SIGNAL_CONTRACT_VERSION,
            signal_id=DeterministicId(prefix="signal", digest=digest).value,
            slot_id=context.slot.slot_id,
            correlation_id=context.slot.correlation_id,
            provider_id=provider_id,
            provider_version=provider_version,
            provider_source_mode=provider_source_mode,
            experiment_id=context.slot.experiment_id,
            experiment_version=context.slot.experiment_version,
            experiment_hash=context.slot.experiment_hash,
            data_contract_hash=context.data_contract_hash,
            policy_hash=context.policy_hash,
            source_bar_end=context.slot.source_interval_end,
            created_at=instant,
            expires_at=context.slot.deadline_at,
            active_symbols=context.active_symbols,
            availability_mask=availability_mask,
            actions=actions,
            expected_edge_bps=expected_edge_bps,
            proposed_signed_target_inputs=proposed_signed_target_inputs,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            promotable=promotable,
            paper_submission_eligible=paper_submission_eligible,
            content_hash=digest,
        )
        return envelope.validate_for(context)

    def validate_for(self, context: DecisionContext) -> Self:
        """Validate slot, universe, timing, and configuration hashes as one boundary."""

        if type(context) is not DecisionContext:
            raise SignalValidationError("signal validation requires an immutable decision context")
        slot = context.slot
        if self.slot_id != slot.slot_id or self.correlation_id != slot.correlation_id:
            raise SignalValidationError("signal does not match its decision slot")
        if (
            self.provider_id != slot.signal_provider_id
            or self.provider_version != slot.signal_provider_version
        ):
            raise SignalValidationError("signal provider identity does not match its slot")
        if (
            self.experiment_id != slot.experiment_id
            or self.experiment_version != slot.experiment_version
            or self.experiment_hash != slot.experiment_hash
        ):
            raise SignalValidationError("signal experiment identity does not match its slot")
        if self.data_contract_hash != context.data_contract_hash:
            raise SignalValidationError("signal data-contract hash does not match its context")
        if self.policy_hash != context.policy_hash:
            raise SignalValidationError("signal policy hash does not match its context")
        if self.active_symbols != context.active_symbols:
            raise SignalValidationError("signal must contain the exact active-symbol set")
        forbidden = set(
            (*context.benchmark_symbols, *context.context_symbols, *context.excluded_symbols)
        )
        if forbidden.intersection(self.active_symbols):
            raise SignalValidationError("signal contains a non-tradable experiment symbol")
        if self.source_bar_end != slot.source_interval_end:
            raise SignalValidationError("signal source bar does not match its slot")
        if not slot.ready_at <= self.created_at < self.expires_at <= slot.deadline_at:
            raise SignalValidationError("signal creation or expiry falls outside its slot deadline")
        return self

    def action_for(self, symbol: str) -> SignalAction:
        """Return the exact action for one active symbol."""

        return self.actions[self._symbol_index(symbol)]

    def is_available(self, symbol: str) -> bool:
        """Return whether the provider had eligible input for one active symbol."""

        return self.availability_mask[self._symbol_index(symbol)]

    def edge_for(self, symbol: str) -> Decimal | None:
        """Return the optional expected edge for one active symbol."""

        return self.expected_edge_bps[self._symbol_index(symbol)]

    def target_input_for(self, symbol: str) -> Decimal | None:
        """Return the optional signed target input for one active symbol."""

        return self.proposed_signed_target_inputs[self._symbol_index(symbol)]

    def _symbol_index(self, symbol: str) -> int:
        normalized = _require_symbol(symbol)
        try:
            return self.active_symbols.index(normalized)
        except ValueError:
            raise KeyError(normalized) from None


def verify_paper_authorization(
    envelope: SignalEnvelope,
    *,
    context: DecisionContext,
) -> PaperAuthorizationDecision:
    """Validate the proposal, then deny paper authorization unconditionally."""

    if type(envelope) is not SignalEnvelope:
        raise SignalValidationError("paper authorization requires a signal envelope")
    envelope.validate_for(context)
    return PaperAuthorizationDecision(
        approved=False,
        reason=PaperAuthorizationReason.MODEL_APPROVAL_NOT_IMPLEMENTED,
    )


def _validate_envelope(envelope: SignalEnvelope) -> None:
    if envelope.contract_version != SIGNAL_CONTRACT_VERSION:
        raise SignalValidationError("signal contract version is unsupported")
    if type(envelope.signal_id) is not str or _SIGNAL_ID.fullmatch(envelope.signal_id) is None:
        raise SignalValidationError("signal ID is invalid")
    if type(envelope.slot_id) is not str or not envelope.slot_id.startswith("slot_"):
        raise SignalValidationError("signal slot ID is invalid")
    if type(envelope.correlation_id) is not str or not envelope.correlation_id.startswith(
        "correlation_"
    ):
        raise SignalValidationError("signal correlation ID is invalid")
    _require_identifier(envelope.provider_id, field_name="provider ID")
    _require_version(envelope.provider_version, field_name="provider version")
    if type(envelope.provider_source_mode) is not SignalSourceMode:
        raise SignalValidationError("provider source mode must use the closed contract")
    _require_identifier(envelope.experiment_id, field_name="experiment ID")
    if type(envelope.experiment_version) is not int or envelope.experiment_version < 1:
        raise SignalValidationError("experiment version must be positive")
    for field_name, value in (
        ("experiment hash", envelope.experiment_hash),
        ("data contract hash", envelope.data_contract_hash),
        ("policy hash", envelope.policy_hash),
        ("content hash", envelope.content_hash),
    ):
        _require_hash(value, field_name=field_name)
    _utc(envelope.source_bar_end, field_name="source_bar_end")
    _utc(envelope.created_at, field_name="created_at")
    _utc(envelope.expires_at, field_name="expires_at")
    if not envelope.source_bar_end <= envelope.created_at < envelope.expires_at:
        raise SignalValidationError("signal timestamps are not ordered")
    _require_symbol_tuple(envelope.active_symbols, allow_empty=False)
    expected_length = len(envelope.active_symbols)
    aligned_values = (
        envelope.availability_mask,
        envelope.actions,
        envelope.expected_edge_bps,
        envelope.proposed_signed_target_inputs,
    )
    if any(
        type(values) is not tuple or len(values) != expected_length for values in aligned_values
    ):
        raise SignalValidationError("signal symbol values must align with every active symbol")
    if any(type(value) is not bool for value in envelope.availability_mask):
        raise SignalValidationError("signal availability mask must contain booleans")
    if any(type(action) is not SignalAction for action in envelope.actions):
        raise SignalValidationError("signal actions must use the closed contract")
    for index, action in enumerate(envelope.actions):
        edge = _optional_decimal(envelope.expected_edge_bps[index], field_name="expected_edge_bps")
        target = _optional_decimal(
            envelope.proposed_signed_target_inputs[index],
            field_name="proposed_signed_target_input",
        )
        if not envelope.availability_mask[index] and (
            action is not SignalAction.FLAT or edge is not None or target is not None
        ):
            raise SignalValidationError("unavailable symbols must have an empty flat proposal")
        if action is SignalAction.LONG and (
            (edge is not None and edge <= 0) or (target is not None and target <= 0)
        ):
            raise SignalValidationError("LONG action requires positive supplied values")
        if action is SignalAction.SHORT and (
            (edge is not None and edge >= 0) or (target is not None and target >= 0)
        ):
            raise SignalValidationError("SHORT action requires negative supplied values")
        if action is SignalAction.FLAT and (
            (edge is not None and edge != 0) or (target is not None and target != 0)
        ):
            raise SignalValidationError("FLAT action requires zero supplied values")
    if (envelope.artifact_id is None) is not (envelope.artifact_hash is None):
        raise SignalValidationError("signal artifact ID and hash must be supplied together")
    if envelope.artifact_id is not None and (
        type(envelope.artifact_id) is not str or _CONTENT_ID.fullmatch(envelope.artifact_id) is None
    ):
        raise SignalValidationError("signal artifact ID is invalid")
    if envelope.artifact_hash is not None:
        _require_hash(envelope.artifact_hash, field_name="artifact hash")
    if (
        type(envelope.promotable) is not bool
        or type(envelope.paper_submission_eligible) is not bool
    ):
        raise SignalValidationError("signal eligibility flags must be boolean")
    if envelope.provider_source_mode is SignalSourceMode.OFFLINE_FIXTURE and (
        envelope.promotable or envelope.paper_submission_eligible
    ):
        raise SignalValidationError("offline fixture signals are permanently ineligible")

    payload = _envelope_payload(envelope)
    expected_hash = sha256_hex(payload)
    if envelope.content_hash != expected_hash:
        raise SignalValidationError("signal content hash does not match its payload")
    expected_id = DeterministicId(prefix="signal", digest=expected_hash).value
    if envelope.signal_id != expected_id:
        raise SignalValidationError("signal ID does not match its payload")


def _envelope_payload(envelope: SignalEnvelope) -> dict[str, object]:
    return {
        field: getattr(envelope, field)
        for field in envelope.__dataclass_fields__
        if field not in {"signal_id", "content_hash"}
    }


def _require_symbol_tuple(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise SignalValidationError("symbol set must be an immutable tuple")
    symbols = value
    if (not allow_empty and not symbols) or any(type(symbol) is not str for symbol in symbols):
        raise SignalValidationError("symbol set is invalid")
    normalized = tuple(_require_symbol(symbol) for symbol in symbols)
    if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
        raise SignalValidationError("symbol set must be sorted and unique")
    return normalized


def _require_symbol(value: object) -> str:
    if type(value) is not str or _SYMBOL.fullmatch(value) is None:
        raise SignalValidationError("symbol is invalid")
    return value


def _require_identifier(value: object, *, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SignalValidationError(f"signal {field_name} is invalid")
    return value


def _require_version(value: object, *, field_name: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise SignalValidationError(f"signal {field_name} is invalid")
    return value


def _require_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise SignalValidationError(f"signal {field_name} is invalid")
    return value


def _utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise SignalValidationError(f"signal {field_name} must be a UTC instant") from None


def _optional_decimal(value: object, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise SignalValidationError(f"signal {field_name} must be finite Decimal or null") from None


__all__ = [
    "SIGNAL_CONTRACT_VERSION",
    "DecisionContext",
    "PaperAuthorizationDecision",
    "PaperAuthorizationReason",
    "SignalAction",
    "SignalEnvelope",
    "SignalSourceMode",
    "SignalValidationError",
    "verify_paper_authorization",
]
