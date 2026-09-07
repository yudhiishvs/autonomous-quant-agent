"""Causal return alignment and restarted risk receipts reject adversarial inputs."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from adaptive_trader.platform.risk import (
    FullSessionCloses,
    RiskStatisticsError,
    compute_risk_statistics,
)
from adaptive_trader.platform.risk.models import SignedRiskValidationError
from adaptive_trader.platform.storage.risk import RiskPersistenceError, SignedRiskRepository
from adaptive_trader.platform.storage.tables import aqa_risk_decisions
from tests.unit.test_platform_risk_repository import (
    _SIGNAL_ID,
    _decision,
    repository,
    sqlite_engine,
)
from tests.unit.test_platform_risk_statistics import _AS_OF, _SYMBOLS, _history

__all__ = ["repository", "sqlite_engine"]


@pytest.mark.parametrize(
    "price",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal(0),
        Decimal(-1),
        100.0,
        True,
        "100",
    ],
)
def test_full_session_refuses_unusable_close_even_at_last_bar(price):
    closes = (Decimal(100),) * 25 + (price,)
    with pytest.raises(RiskStatisticsError):
        FullSessionCloses(session_date=_AS_OF, closes=closes)


@pytest.mark.parametrize(
    "attack",
    [
        "same_day",
        "future",
        "misaligned",
        "duplicate",
        "reversed",
        "short",
        "unvalidated",
        "missing_symbol",
        "extra_symbol",
    ],
)
def test_return_history_has_exact_common_strictly_prior_sessions(attack):
    history = _history()
    sessions = history["AMD"]
    if attack in {"same_day", "future", "misaligned"}:
        day = {
            "same_day": _AS_OF,
            "future": _AS_OF + timedelta(days=1),
            "misaligned": sessions[0].session_date - timedelta(days=1),
        }[attack]
        index = 0 if attack == "misaligned" else -1
        mutable = list(sessions)
        mutable[index] = replace(mutable[index], session_date=day)
        history["AMD"] = tuple(mutable)
    elif attack == "duplicate":
        history["AMD"] = (sessions[1], *sessions[1:])
    elif attack == "reversed":
        history["AMD"] = sessions[::-1]
    elif attack == "short":
        history["AMD"] = sessions[:-1]
    elif attack == "unvalidated":
        history["AMD"] = (object(), *sessions[1:])
    elif attack == "missing_symbol":
        history.pop("NVDA")
    else:
        history["QQQ"] = sessions
    with pytest.raises(RiskStatisticsError):
        compute_risk_statistics(active_symbols=_SYMBOLS, history=history, as_of_date=_AS_OF)


@pytest.mark.parametrize(
    "change",
    [
        {"symbols": ("NVDA", "AMD")},
        {"observation_count": 499},
        {"annualization_factor": Decimal(252)},
        {"eigenvalue_floor": Decimal(0)},
        {"annualized_covariance": ()},
        {"prior_correlation": ((Decimal(1),),)},
        {"annualized_sigma": ()},
        {"annualized_sigma": (Decimal(0), Decimal(0))},
        {"input_hash": "G" * 64},
        {"output_hash": "A" * 64},
    ],
)
def test_statistics_receipt_rejects_unbound_dimensions_and_numeric_contract(change):
    statistics = compute_risk_statistics(
        active_symbols=_SYMBOLS, history=_history(), as_of_date=_AS_OF
    )
    with pytest.raises(RiskStatisticsError):
        replace(statistics, **change)


@pytest.mark.parametrize(
    "change",
    [
        {"proposed_targets": {}},
        {"proposed_targets": [["AMD"]]},
        {"proposed_targets": [[1, "0"]]},
        {"proposed_targets": [["AMD", 1]]},
        {"proposed_targets": [["AMD", "NaN"]]},
        {"proposed_targets": [["AMD", "Infinity"]]},
        {"source_timestamps": {}},
        {"source_timestamps": [["account"]]},
        {"source_timestamps": [["account", "2026-07-06T14:00:00+00:00"]]},
        {"source_timestamps": [["account", "2026-07-06T14:00:00Z"]]},
        {"reason_codes": {}},
        {"reason_codes": [1]},
    ],
)
def test_restart_refuses_corrupt_signed_risk_json(repository, sqlite_engine, change):
    decision = _decision()
    repository.persist(decision)
    assert SignedRiskRepository(sqlite_engine).decision_for_signal(_SIGNAL_ID) == decision
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_risk_decisions)
            .where(aqa_risk_decisions.c.signal_id == _SIGNAL_ID)
            .values(**change)
        )
    restarted = SignedRiskRepository(sqlite_engine)
    with pytest.raises((RiskPersistenceError, SignedRiskValidationError)):
        restarted.decision_for_signal(_SIGNAL_ID)
