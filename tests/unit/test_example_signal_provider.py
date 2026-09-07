"""Behavior and capability tests for the educational signal-provider example."""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from adaptive_trader.platform.config import BrokerAdapter, ExecutionMode, load_experiment
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.scheduling import build_session_schedule
from adaptive_trader.platform.signals import DecisionContext, SignalAction, SignalSourceMode
from examples.always_flat_provider import EducationalAlwaysFlatProvider

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _offline_context() -> DecisionContext:
    experiment = load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=_PROJECT_ROOT / "configs",
    )
    schedule = build_session_schedule(
        experiment=experiment,
        signal_provider_id="educational_always_flat",
        signal_provider_version="1",
        session_date=date(2026, 7, 6),
        calendar=XnasExchangeCalendar(),
    )
    return DecisionContext.from_experiment(
        slot=schedule.strategy_slots[0],
        experiment=experiment,
        data_contract_hash=sha256_hex(("example-data-contract", 1)),
        policy_hash=sha256_hex(("example-policy", 1)),
        execution_mode=ExecutionMode.OFFLINE,
        broker_adapter=BrokerAdapter.FAKE,
        submission_enabled=False,
        strategy_slot_ordinal=0,
    )


def test_example_returns_exact_all_flat_non_promotable_envelope() -> None:
    context = _offline_context()

    first = EducationalAlwaysFlatProvider().signal_for(context)
    second = EducationalAlwaysFlatProvider().signal_for(context)

    assert first == second
    assert first.active_symbols == context.active_symbols
    assert first.actions == (SignalAction.FLAT,) * len(context.active_symbols)
    assert first.provider_source_mode is SignalSourceMode.REGISTERED_PLUGIN
    assert first.promotable is False
    assert first.paper_submission_eligible is False


def test_example_has_no_network_credential_storage_or_broker_imports() -> None:
    example = _PROJECT_ROOT / "examples" / "always_flat_provider.py"
    tree = ast.parse(example.read_text(encoding="utf-8"), filename=str(example))
    forbidden_prefixes = (
        "alpaca",
        "requests",
        "socket",
        "sqlalchemy",
        "adaptive_trader.collection",
        "adaptive_trader.execution",
        "adaptive_trader.platform.execution",
        "adaptive_trader.platform.security",
    )

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)

    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imported
        for prefix in forbidden_prefixes
    )
