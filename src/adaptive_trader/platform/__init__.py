"""Deterministic, broker-independent platform primitives."""

from adaptive_trader.platform.canonical import CanonicalizationError, canonical_json_bytes
from adaptive_trader.platform.config import (
    ExperimentConfigError,
    ExperimentDefinition,
    ExperimentHashMismatchError,
    MarketDataSpec,
    RiskGroupSpec,
    RiskPolicySpec,
    SessionSpec,
    load_experiment,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.universe import SymbolRole, UniverseSpec

__all__ = [
    "CanonicalizationError",
    "ExperimentConfigError",
    "ExperimentDefinition",
    "ExperimentHashMismatchError",
    "MarketDataSpec",
    "RiskGroupSpec",
    "RiskPolicySpec",
    "SessionSpec",
    "SymbolRole",
    "UniverseSpec",
    "canonical_json_bytes",
    "load_experiment",
    "sha256_hex",
]
