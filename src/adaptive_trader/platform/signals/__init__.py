"""Declarative signal envelopes and operator-trusted provider discovery."""

from adaptive_trader.platform.signals.models import (
    SIGNAL_CONTRACT_VERSION,
    DecisionContext,
    PaperAuthorizationDecision,
    PaperAuthorizationReason,
    SignalAction,
    SignalEnvelope,
    SignalSourceMode,
    SignalValidationError,
    verify_paper_authorization,
)
from adaptive_trader.platform.signals.providers import (
    SIGNAL_PROVIDER_ENTRY_POINT_GROUP,
    AlwaysFlatSignalProvider,
    AuditSignalPersistenceRecorder,
    FixtureSignalScenario,
    OfflineFixtureSignalProvider,
    ProviderDiscoveryError,
    SignalEnvelopeRepository,
    SignalPersistenceError,
    SignalProvider,
    SignalProviderRegistry,
    SignalSchemaError,
)

__all__ = [
    "SIGNAL_CONTRACT_VERSION",
    "SIGNAL_PROVIDER_ENTRY_POINT_GROUP",
    "AlwaysFlatSignalProvider",
    "AuditSignalPersistenceRecorder",
    "DecisionContext",
    "FixtureSignalScenario",
    "OfflineFixtureSignalProvider",
    "PaperAuthorizationDecision",
    "PaperAuthorizationReason",
    "ProviderDiscoveryError",
    "SignalAction",
    "SignalEnvelope",
    "SignalEnvelopeRepository",
    "SignalPersistenceError",
    "SignalProvider",
    "SignalProviderRegistry",
    "SignalSchemaError",
    "SignalSourceMode",
    "SignalValidationError",
    "verify_paper_authorization",
]
