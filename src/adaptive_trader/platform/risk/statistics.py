"""Deterministic signed-risk statistics over complete intraday sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, DecimalException, localcontext
from typing import Final

import numpy as np

from adaptive_trader.platform.domain import require_finite_decimal
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex

SESSION_COUNT: Final = 20
BARS_PER_SESSION: Final = 26
RETURNS_PER_SESSION: Final = 25
RETURNS_PER_SYMBOL: Final = SESSION_COUNT * RETURNS_PER_SESSION
ANNUALIZATION_FACTOR: Final = Decimal(6552)
DEFAULT_EIGENVALUE_FLOOR: Final = Decimal("0.00000001")
_CALCULATION_CONTEXT: Final = Context(prec=50)


class RiskStatisticsError(DomainValidationError):
    """Raised when return history cannot produce the exact risk-statistics contract."""


@dataclass(frozen=True, slots=True)
class FullSessionCloses:
    """Exactly 26 fifteen-minute closes from one complete full exchange session."""

    session_date: date
    closes: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise RiskStatisticsError("session date must be a date")
        if type(self.closes) is not tuple or len(self.closes) != BARS_PER_SESSION:
            raise RiskStatisticsError("a full session must contain exactly 26 closes")
        normalized: list[Decimal] = []
        for close in self.closes:
            try:
                value = require_finite_decimal(close, field_name="session_close")
            except DomainValidationError:
                raise RiskStatisticsError("session closes must be finite exact decimals") from None
            if value <= 0:
                raise RiskStatisticsError("session closes must be positive")
            normalized.append(value)
        object.__setattr__(self, "closes", tuple(normalized))


@dataclass(frozen=True, slots=True)
class RiskStatistics:
    """Hash-bound annualized covariance, correlation, and sigma in symbol order."""

    symbols: tuple[str, ...]
    observation_count: int
    annualization_factor: Decimal
    eigenvalue_floor: Decimal
    annualized_covariance: tuple[tuple[Decimal, ...], ...]
    prior_correlation: tuple[tuple[Decimal, ...], ...]
    annualized_sigma: tuple[Decimal, ...]
    input_hash: str
    output_hash: str

    def __post_init__(self) -> None:
        _validate_statistics(self)

    def sigma_for(self, symbol: str) -> Decimal:
        """Return annualized sigma for an exact active symbol."""

        try:
            return self.annualized_sigma[self.symbols.index(symbol)]
        except ValueError:
            raise RiskStatisticsError("sigma requested for an unknown symbol") from None

    def covariance_for(self, left: str, right: str) -> Decimal:
        """Return annualized covariance for an exact active-symbol pair."""

        try:
            left_index = self.symbols.index(left)
            right_index = self.symbols.index(right)
        except ValueError:
            raise RiskStatisticsError("covariance requested for an unknown symbol") from None
        return self.annualized_covariance[left_index][right_index]

    def correlation_for(self, left: str, right: str) -> Decimal:
        """Return prior correlation for an exact active-symbol pair."""

        try:
            left_index = self.symbols.index(left)
            right_index = self.symbols.index(right)
        except ValueError:
            raise RiskStatisticsError("correlation requested for an unknown symbol") from None
        return self.prior_correlation[left_index][right_index]


def compute_risk_statistics(
    *,
    active_symbols: Sequence[str],
    history: Mapping[str, Sequence[FullSessionCloses]],
    as_of_date: date,
    eigenvalue_floor: Decimal = DEFAULT_EIGENVALUE_FLOOR,
) -> RiskStatistics:
    """Calculate the exact 20-session, within-session signed-risk statistics.

    NumPy is confined to the symmetric eigendecomposition. All externally visible numeric values
    are converted to canonical finite ``Decimal`` instances before hashing or policy use.
    """

    symbols = _normalize_symbols(active_symbols)
    if type(as_of_date) is not date:
        raise RiskStatisticsError("statistics as-of date must be a date")
    floor = _positive_decimal(eigenvalue_floor, field_name="eigenvalue_floor")
    sessions_by_symbol = _normalize_history(
        symbols=symbols,
        history=history,
        as_of_date=as_of_date,
    )
    returns_by_symbol = tuple(
        _within_session_returns(sessions_by_symbol[symbol]) for symbol in symbols
    )
    input_hash = sha256_hex(
        {
            "as_of_date": as_of_date.isoformat(),
            "history": tuple(
                {
                    "sessions": tuple(
                        {
                            "closes": session.closes,
                            "session_date": session.session_date.isoformat(),
                        }
                        for session in sessions_by_symbol[symbol]
                    ),
                    "symbol": symbol,
                }
                for symbol in symbols
            ),
            "schema": "signed-risk-statistics-input-v1",
            "symbols": symbols,
        }
    )

    raw_covariance = _sample_annualized_covariance(returns_by_symbol)
    covariance = _floor_covariance_eigenvalues(raw_covariance, eigenvalue_floor=floor)
    sigma = tuple(_decimal_sqrt(covariance[index][index]) for index in range(len(symbols)))
    correlation = _correlation_matrix(covariance, sigma)
    payload = {
        "annualization_factor": ANNUALIZATION_FACTOR,
        "annualized_covariance": covariance,
        "annualized_sigma": sigma,
        "eigenvalue_floor": floor,
        "input_hash": input_hash,
        "observation_count": RETURNS_PER_SYMBOL,
        "prior_correlation": correlation,
        "schema": "signed-risk-statistics-output-v1",
        "symbols": symbols,
    }
    return RiskStatistics(
        symbols=symbols,
        observation_count=RETURNS_PER_SYMBOL,
        annualization_factor=ANNUALIZATION_FACTOR,
        eigenvalue_floor=floor,
        annualized_covariance=covariance,
        prior_correlation=correlation,
        annualized_sigma=sigma,
        input_hash=input_hash,
        output_hash=sha256_hex(payload),
    )


def _normalize_symbols(active_symbols: Sequence[str]) -> tuple[str, ...]:
    if type(active_symbols) not in {list, tuple}:
        raise RiskStatisticsError("active symbols must be a bounded sequence")
    symbols = tuple(active_symbols)
    if not symbols or any(
        type(symbol) is not str
        or not symbol
        or len(symbol) > 10
        or not symbol.isascii()
        or not symbol.isupper()
        or not symbol.replace(".", "").isalnum()
        for symbol in symbols
    ):
        raise RiskStatisticsError("active symbols are invalid")
    if len(symbols) != len(set(symbols)):
        raise RiskStatisticsError("active symbols must be unique")
    return tuple(sorted(symbols))


def _normalize_history(
    *,
    symbols: tuple[str, ...],
    history: Mapping[str, Sequence[FullSessionCloses]],
    as_of_date: date,
) -> dict[str, tuple[FullSessionCloses, ...]]:
    if not isinstance(history, Mapping) or set(history) != set(symbols):
        raise RiskStatisticsError("history must contain exactly the active symbols")
    normalized: dict[str, tuple[FullSessionCloses, ...]] = {}
    expected_dates: tuple[date, ...] | None = None
    for symbol in symbols:
        raw_sessions = history[symbol]
        if type(raw_sessions) not in {list, tuple} or len(raw_sessions) != SESSION_COUNT:
            raise RiskStatisticsError("history must contain exactly 20 sessions per symbol")
        sessions = tuple(raw_sessions)
        if any(type(session) is not FullSessionCloses for session in sessions):
            raise RiskStatisticsError("history must contain validated full sessions")
        dates = tuple(session.session_date for session in sessions)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise RiskStatisticsError("history session dates must be unique and increasing")
        if dates[-1] >= as_of_date:
            raise RiskStatisticsError("statistics may use only sessions before the as-of date")
        if expected_dates is None:
            expected_dates = dates
        elif dates != expected_dates:
            raise RiskStatisticsError("every active symbol must use the same 20 sessions")
        normalized[symbol] = sessions
    return normalized


def _within_session_returns(sessions: tuple[FullSessionCloses, ...]) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    try:
        with localcontext(_CALCULATION_CONTEXT):
            for session in sessions:
                values.extend(
                    (session.closes[index] / session.closes[index - 1]).ln()
                    for index in range(1, BARS_PER_SESSION)
                )
    except DecimalException:
        raise RiskStatisticsError("session returns could not be calculated") from None
    if len(values) != RETURNS_PER_SYMBOL or any(not value.is_finite() for value in values):
        raise RiskStatisticsError("history did not produce exactly 500 finite returns")
    return tuple(values)


def _sample_annualized_covariance(
    returns_by_symbol: tuple[tuple[Decimal, ...], ...],
) -> tuple[tuple[Decimal, ...], ...]:
    with localcontext(_CALCULATION_CONTEXT):
        means = tuple(
            sum(returns_, Decimal(0)) / Decimal(RETURNS_PER_SYMBOL)
            for returns_ in returns_by_symbol
        )
        denominator = Decimal(RETURNS_PER_SYMBOL - 1)
        matrix: list[tuple[Decimal, ...]] = []
        for left_index, left_values in enumerate(returns_by_symbol):
            row: list[Decimal] = []
            for right_index, right_values in enumerate(returns_by_symbol):
                covariance = (
                    sum(
                        (
                            (left_values[index] - means[left_index])
                            * (right_values[index] - means[right_index])
                        )
                        for index in range(RETURNS_PER_SYMBOL)
                    )
                    / denominator
                ) * ANNUALIZATION_FACTOR
                row.append(covariance)
            matrix.append(tuple(row))
    return tuple(matrix)


def _floor_covariance_eigenvalues(
    covariance: tuple[tuple[Decimal, ...], ...],
    *,
    eigenvalue_floor: Decimal,
) -> tuple[tuple[Decimal, ...], ...]:
    try:
        numeric = np.array(
            [[float(value) for value in row] for row in covariance],
            dtype=np.float64,
        )
        numeric = (numeric + numeric.T) / 2.0
        eigenvalues, eigenvectors = np.linalg.eigh(numeric)
        floored = np.maximum(eigenvalues, float(eigenvalue_floor))
        reconstructed = (eigenvectors * floored) @ eigenvectors.T
        reconstructed = (reconstructed + reconstructed.T) / 2.0
    except (FloatingPointError, OverflowError, TypeError, ValueError):
        raise RiskStatisticsError("covariance eigendecomposition failed") from None
    if not np.isfinite(reconstructed).all():
        raise RiskStatisticsError("covariance reconstruction was nonfinite")
    return tuple(tuple(_decimal_from_float(float(value)) for value in row) for row in reconstructed)


def _correlation_matrix(
    covariance: tuple[tuple[Decimal, ...], ...],
    sigma: tuple[Decimal, ...],
) -> tuple[tuple[Decimal, ...], ...]:
    matrix: list[tuple[Decimal, ...]] = []
    with localcontext(_CALCULATION_CONTEXT):
        for left_index, row in enumerate(covariance):
            correlations: list[Decimal] = []
            for right_index, value in enumerate(row):
                if left_index == right_index:
                    correlations.append(Decimal(1))
                    continue
                correlation = value / (sigma[left_index] * sigma[right_index])
                correlations.append(max(Decimal(-1), min(Decimal(1), correlation)))
            matrix.append(tuple(correlations))
    symmetric = tuple(
        tuple(
            Decimal(1)
            if row == column
            else (matrix[row][column] + matrix[column][row]) / Decimal(2)
            for column in range(len(matrix))
        )
        for row in range(len(matrix))
    )
    return symmetric


def _decimal_from_float(value: float) -> Decimal:
    if not np.isfinite(value):
        raise RiskStatisticsError("numeric output must be finite")
    try:
        result = Decimal(format(value, ".17g"))
    except DecimalException:
        raise RiskStatisticsError("numeric output could not be canonicalized") from None
    return require_finite_decimal(result, field_name="statistics_value")


def _decimal_sqrt(value: Decimal) -> Decimal:
    if value <= 0:
        raise RiskStatisticsError("floored covariance diagonal must be positive")
    try:
        with localcontext(_CALCULATION_CONTEXT):
            result = value.sqrt()
    except DecimalException:
        raise RiskStatisticsError("annualized sigma could not be calculated") from None
    return require_finite_decimal(result, field_name="annualized_sigma")


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    try:
        normalized = require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise RiskStatisticsError(f"{field_name} must be a finite exact decimal") from None
    if normalized <= 0:
        raise RiskStatisticsError(f"{field_name} must be positive")
    return normalized


def _validate_statistics(statistics: RiskStatistics) -> None:
    symbols = _normalize_symbols(statistics.symbols)
    if symbols != statistics.symbols:
        raise RiskStatisticsError("statistics symbols must be alphabetically ordered")
    size = len(symbols)
    if statistics.observation_count != RETURNS_PER_SYMBOL:
        raise RiskStatisticsError("statistics must contain exactly 500 observations per symbol")
    if statistics.annualization_factor != ANNUALIZATION_FACTOR:
        raise RiskStatisticsError("statistics annualization factor is invalid")
    _positive_decimal(statistics.eigenvalue_floor, field_name="eigenvalue_floor")
    matrices = (statistics.annualized_covariance, statistics.prior_correlation)
    if any(
        type(matrix) is not tuple
        or len(matrix) != size
        or any(type(row) is not tuple or len(row) != size for row in matrix)
        for matrix in matrices
    ):
        raise RiskStatisticsError("statistics matrix dimensions are invalid")
    if type(statistics.annualized_sigma) is not tuple or len(statistics.annualized_sigma) != size:
        raise RiskStatisticsError("statistics sigma dimensions are invalid")
    expected_sigma: list[Decimal] = []
    for index in range(size):
        _positive_decimal(statistics.annualized_sigma[index], field_name="annualized_sigma")
        expected_sigma.append(_decimal_sqrt(statistics.annualized_covariance[index][index]))
        for other in range(size):
            covariance = require_finite_decimal(
                statistics.annualized_covariance[index][other],
                field_name="annualized_covariance",
            )
            correlation = require_finite_decimal(
                statistics.prior_correlation[index][other],
                field_name="prior_correlation",
            )
            if covariance != statistics.annualized_covariance[other][index]:
                raise RiskStatisticsError("annualized covariance must be symmetric")
            if correlation != statistics.prior_correlation[other][index]:
                raise RiskStatisticsError("prior correlation must be symmetric")
            if not Decimal(-1) <= correlation <= Decimal(1):
                raise RiskStatisticsError("prior correlation must be in [-1, 1]")
        if statistics.prior_correlation[index][index] != Decimal(1):
            raise RiskStatisticsError("prior correlation diagonal must equal one")
    if statistics.annualized_sigma != tuple(expected_sigma):
        raise RiskStatisticsError("annualized sigma does not match covariance")
    expected_correlation = _correlation_matrix(
        statistics.annualized_covariance,
        statistics.annualized_sigma,
    )
    if statistics.prior_correlation != expected_correlation:
        raise RiskStatisticsError("prior correlation does not match covariance")
    try:
        numeric_covariance = np.array(
            [[float(value) for value in row] for row in statistics.annualized_covariance],
            dtype=np.float64,
        )
        eigenvalues = np.linalg.eigvalsh(numeric_covariance)
    except (FloatingPointError, OverflowError, TypeError, ValueError):
        raise RiskStatisticsError("annualized covariance is not numerically valid") from None
    floor = float(statistics.eigenvalue_floor)
    tolerance = max(abs(floor) * 1e-9, 1e-15)
    if not np.isfinite(eigenvalues).all() or float(eigenvalues.min()) < floor - tolerance:
        raise RiskStatisticsError("annualized covariance violates the eigenvalue floor")
    for value in (statistics.input_hash, statistics.output_hash):
        if type(value) is not str or len(value) != 64:
            raise RiskStatisticsError("statistics hashes must be lowercase SHA-256")
        try:
            int(value, 16)
        except ValueError:
            raise RiskStatisticsError("statistics hashes must be lowercase SHA-256") from None
        if value != value.lower():
            raise RiskStatisticsError("statistics hashes must be lowercase SHA-256")
    expected_output_hash = sha256_hex(
        {
            "annualization_factor": statistics.annualization_factor,
            "annualized_covariance": statistics.annualized_covariance,
            "annualized_sigma": statistics.annualized_sigma,
            "eigenvalue_floor": statistics.eigenvalue_floor,
            "input_hash": statistics.input_hash,
            "observation_count": statistics.observation_count,
            "prior_correlation": statistics.prior_correlation,
            "schema": "signed-risk-statistics-output-v1",
            "symbols": statistics.symbols,
        }
    )
    if statistics.output_hash != expected_output_hash:
        raise RiskStatisticsError("statistics output hash is invalid")
