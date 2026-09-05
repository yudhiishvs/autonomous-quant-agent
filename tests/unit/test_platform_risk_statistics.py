"""Exact-history, numeric known-answer, and tamper tests for signed-risk statistics."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    ANNUALIZATION_FACTOR,
    BARS_PER_SESSION,
    RETURNS_PER_SYMBOL,
    SESSION_COUNT,
    FullSessionCloses,
    RiskStatistics,
    RiskStatisticsError,
    compute_risk_statistics,
)

_SYMBOLS = ("AMD", "NVDA")
_AS_OF = date(2026, 7, 6)


def _history(*, constant: bool = False):
    dates = tuple(_AS_OF - timedelta(days=SESSION_COUNT - index) for index in range(SESSION_COUNT))
    result: dict[str, tuple[FullSessionCloses, ...]] = {}
    for symbol_index, symbol in enumerate(_SYMBOLS):
        sessions = []
        for session_index, session_date in enumerate(dates):
            base = Decimal(100 + (20 * symbol_index) + session_index)
            closes = tuple(
                base
                if constant
                else base
                + Decimal(bar_index * (symbol_index + 1)) / Decimal(100)
                + Decimal((session_index + bar_index) % 3) / Decimal(1000)
                for bar_index in range(BARS_PER_SESSION)
            )
            sessions.append(FullSessionCloses(session_date=session_date, closes=closes))
        result[symbol] = tuple(sessions)
    return result


def test_statistics_match_independent_numpy_known_answer_and_are_replayable() -> None:
    history = _history()
    first = compute_risk_statistics(
        active_symbols=("NVDA", "AMD"),
        history=history,
        as_of_date=_AS_OF,
    )
    replay = compute_risk_statistics(
        active_symbols=_SYMBOLS,
        history=history,
        as_of_date=_AS_OF,
    )

    independent_returns = []
    for symbol in _SYMBOLS:
        values = []
        for session in history[symbol]:
            closes = np.array([float(value) for value in session.closes])
            values.extend(np.log(closes[1:] / closes[:-1]))
        independent_returns.append(values)
    raw = np.cov(np.array(independent_returns), ddof=1) * float(ANNUALIZATION_FACTOR)
    eigenvalues, eigenvectors = np.linalg.eigh((raw + raw.T) / 2)
    expected = (eigenvectors * np.maximum(eigenvalues, 1e-8)) @ eigenvectors.T
    expected = (expected + expected.T) / 2

    assert first == replay
    assert first.symbols == _SYMBOLS
    assert first.observation_count == RETURNS_PER_SYMBOL == 500
    assert first.annualization_factor == Decimal(6552)
    assert np.allclose(
        np.array([[float(value) for value in row] for row in first.annualized_covariance]),
        expected,
        rtol=1e-11,
        atol=1e-14,
    )
    assert first.prior_correlation[0][1] == first.prior_correlation[1][0]
    assert first.input_hash != first.output_hash


def test_eigenvalue_floor_makes_degenerate_covariance_positive_and_symmetric() -> None:
    result = compute_risk_statistics(
        active_symbols=_SYMBOLS,
        history=_history(constant=True),
        as_of_date=_AS_OF,
    )

    assert result.annualized_covariance == (
        (Decimal("1E-8"), Decimal(0)),
        (Decimal(0), Decimal("1E-8")),
    )
    assert result.annualized_sigma == (Decimal("0.0001"), Decimal("0.0001"))
    assert result.prior_correlation == ((Decimal(1), Decimal(0)), (Decimal(0), Decimal(1)))


@pytest.mark.parametrize("session_count", [0, 19, 21])
def test_statistics_reject_wrong_session_count(session_count: int) -> None:
    history = _history()
    sessions = history["AMD"]
    history["AMD"] = (
        sessions[:session_count]
        if session_count <= len(sessions)
        else (
            *sessions,
            replace(sessions[-1], session_date=sessions[-1].session_date + timedelta(days=1)),
        )
    )
    with pytest.raises(RiskStatisticsError, match="exactly 20"):
        compute_risk_statistics(
            active_symbols=_SYMBOLS,
            history=history,
            as_of_date=_AS_OF,
        )


def test_statistics_reject_incomplete_bars_date_mismatch_and_nonpositive_close() -> None:
    history = _history()
    session = history["AMD"][0]
    with pytest.raises(RiskStatisticsError, match="exactly 26"):
        FullSessionCloses(session_date=session.session_date, closes=session.closes[:-1])
    with pytest.raises(RiskStatisticsError, match="positive"):
        FullSessionCloses(
            session_date=session.session_date,
            closes=(Decimal(0), *session.closes[1:]),
        )

    shifted = tuple(
        replace(item, session_date=item.session_date - timedelta(days=1))
        for item in history["NVDA"]
    )
    history["NVDA"] = shifted
    with pytest.raises(RiskStatisticsError, match="same 20 sessions"):
        compute_risk_statistics(
            active_symbols=_SYMBOLS,
            history=history,
            as_of_date=_AS_OF,
        )


def test_statistics_reject_current_session_unknown_symbols_and_tampering() -> None:
    history = _history()
    last = history["AMD"][-1]
    history["AMD"] = (*history["AMD"][:-1], replace(last, session_date=_AS_OF))
    with pytest.raises(RiskStatisticsError, match="before the as-of"):
        compute_risk_statistics(
            active_symbols=_SYMBOLS,
            history=history,
            as_of_date=_AS_OF,
        )

    clean = compute_risk_statistics(
        active_symbols=_SYMBOLS,
        history=_history(),
        as_of_date=_AS_OF,
    )
    with pytest.raises(RiskStatisticsError, match="unknown symbol"):
        clean.sigma_for("AAPL")
    with pytest.raises(RiskStatisticsError, match=r"sigma|output hash"):
        replace(clean, annualized_sigma=(Decimal("0.2"), Decimal("0.2")))


def test_statistics_reject_hash_consistent_non_positive_semidefinite_covariance() -> None:
    covariance = (
        (Decimal("0.04"), Decimal("0.08")),
        (Decimal("0.08"), Decimal("0.04")),
    )
    correlation = ((Decimal(1), Decimal(1)), (Decimal(1), Decimal(1)))
    sigma = (Decimal("0.2"), Decimal("0.2"))
    input_hash = "4" * 64
    payload = {
        "annualization_factor": ANNUALIZATION_FACTOR,
        "annualized_covariance": covariance,
        "annualized_sigma": sigma,
        "eigenvalue_floor": Decimal("1E-8"),
        "input_hash": input_hash,
        "observation_count": RETURNS_PER_SYMBOL,
        "prior_correlation": correlation,
        "schema": "signed-risk-statistics-output-v1",
        "symbols": _SYMBOLS,
    }

    with pytest.raises(RiskStatisticsError, match="eigenvalue floor"):
        RiskStatistics(
            symbols=_SYMBOLS,
            observation_count=RETURNS_PER_SYMBOL,
            annualization_factor=ANNUALIZATION_FACTOR,
            eigenvalue_floor=Decimal("1E-8"),
            annualized_covariance=covariance,
            prior_correlation=correlation,
            annualized_sigma=sigma,
            input_hash=input_hash,
            output_hash=sha256_hex(payload),
        )
