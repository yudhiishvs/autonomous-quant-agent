"""Pure signed sizing, shrink-only constraints, and rebalance-band policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, localcontext
from enum import StrEnum
from typing import Final

from adaptive_trader.platform.config import RiskGroupSpec, RiskPolicySpec
from adaptive_trader.platform.domain import require_finite_decimal
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk.statistics import RiskStatistics

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_CONVERGENCE_TOLERANCE: Final = Decimal("0.000000000001")
_MAX_CONSTRAINT_PASSES: Final = 8
_ARITHMETIC: Final = Context(prec=50)


class SignedRiskPolicyError(DomainValidationError):
    """Raised when signed sizing or constraint inputs violate the policy contract."""


class SignalDirection(StrEnum):
    """Closed normalized directions accepted from a validated signal envelope."""

    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class RiskControl(StrEnum):
    """Closed ordered controls that may alter a signed target."""

    CLUSTER_GROSS = "cluster_gross"
    GROSS = "gross"
    NET_LONG = "net_long"
    NET_SHORT = "net_short"
    REBALANCE_BAND = "rebalance_band"
    RISK_GROUP_GROSS = "risk_group_gross"
    SYMBOL_CLIP = "symbol_clip"


@dataclass(frozen=True, slots=True)
class SymbolSignal:
    """One normalized action and optional edge from an immutable signal envelope."""

    symbol: str
    direction: SignalDirection
    expected_edge_bps: Decimal | None

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        if type(self.direction) is not SignalDirection:
            raise SignedRiskPolicyError("signal direction must use the closed contract")
        if self.expected_edge_bps is not None:
            edge = _decimal(self.expected_edge_bps, field_name="expected_edge_bps")
            if abs(edge) > Decimal("1000000"):
                raise SignedRiskPolicyError("expected edge exceeds the bounded policy range")
            if self.direction is SignalDirection.LONG and edge <= 0:
                raise SignedRiskPolicyError("long expected edge must be positive")
            if self.direction is SignalDirection.SHORT and edge >= 0:
                raise SignedRiskPolicyError("short expected edge must be negative")
            if self.direction is SignalDirection.FLAT and edge != 0:
                raise SignedRiskPolicyError("flat expected edge must be zero when supplied")
        elif self.direction is not SignalDirection.FLAT:
            raise SignedRiskPolicyError("non-flat signals require expected edge")


@dataclass(frozen=True, slots=True)
class AppliedRiskControl:
    """One ordered, replayable shrink or retained-position band operation."""

    pass_number: int
    ordinal: int
    control: RiskControl
    scope: str
    factor: Decimal
    before: tuple[tuple[str, Decimal], ...]
    after: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        if type(self.pass_number) is not int or self.pass_number < 1:
            raise SignedRiskPolicyError("control pass number must be positive")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise SignedRiskPolicyError("control ordinal must be positive")
        if type(self.control) is not RiskControl:
            raise SignedRiskPolicyError("control type is invalid")
        if type(self.scope) is not str or not self.scope or len(self.scope) > 128:
            raise SignedRiskPolicyError("control scope is invalid")
        factor = _decimal(self.factor, field_name="control_factor")
        if not _ZERO <= factor <= _ONE:
            raise SignedRiskPolicyError("control factor must be in [0, 1]")
        _weight_tuple(self.before)
        _weight_tuple(self.after)


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    """Signed exposure totals at one deterministic point in policy evaluation."""

    gross: Decimal
    net: Decimal
    positive: Decimal
    short_abs: Decimal
    group_gross: tuple[tuple[str, Decimal], ...]
    cluster_gross: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        with localcontext(_ARITHMETIC):
            gross = _decimal(self.gross, field_name="gross")
            net = _decimal(self.net, field_name="net")
            positive = _decimal(self.positive, field_name="positive")
            short_abs = _decimal(self.short_abs, field_name="short_abs")
            if min(gross, positive, short_abs) < 0:
                raise SignedRiskPolicyError("gross exposure totals cannot be negative")
            if gross != positive + short_abs or net != positive - short_abs:
                raise SignedRiskPolicyError("exposure totals are internally inconsistent")
        _named_decimal_tuple(self.group_gross, field_name="group exposure")
        _named_decimal_tuple(self.cluster_gross, field_name="cluster exposure")


@dataclass(frozen=True, slots=True)
class InitialTargetResult:
    """Raw scores and volatility-scaled targets before hard constraints."""

    raw_scores: tuple[tuple[str, Decimal], ...]
    forecast_volatility: Decimal | None
    multiplier: Decimal | None
    targets: tuple[tuple[str, Decimal], ...]
    reason_code: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _weight_tuple(self.raw_scores)
        _weight_tuple(self.targets)
        if tuple(symbol for symbol, _ in self.raw_scores) != tuple(
            symbol for symbol, _ in self.targets
        ):
            raise SignedRiskPolicyError("score and target symbols do not match")
        for optional, field_name in (
            (self.forecast_volatility, "forecast_volatility"),
            (self.multiplier, "multiplier"),
        ):
            if optional is not None:
                _decimal(optional, field_name=field_name)
        if self.reason_code is not None and (
            type(self.reason_code) is not str or not self.reason_code
        ):
            raise SignedRiskPolicyError("initial target reason is invalid")
        if self.content_hash != _initial_target_hash(
            raw_scores=self.raw_scores,
            forecast_volatility=self.forecast_volatility,
            multiplier=self.multiplier,
            targets=self.targets,
            reason_code=self.reason_code,
        ):
            raise SignedRiskPolicyError("initial target result hash is invalid")


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    """Complete ordered constraint receipt with before/after exposures."""

    original_targets: tuple[tuple[str, Decimal], ...]
    final_targets: tuple[tuple[str, Decimal], ...]
    before_exposure: ExposureSnapshot
    after_exposure: ExposureSnapshot
    controls: tuple[AppliedRiskControl, ...]
    correlation_components: tuple[tuple[str, ...], ...]
    converged: bool
    reason_code: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _weight_tuple(self.original_targets)
        _weight_tuple(self.final_targets)
        if tuple(symbol for symbol, _ in self.original_targets) != tuple(
            symbol for symbol, _ in self.final_targets
        ):
            raise SignedRiskPolicyError("constraint target symbols do not match")
        if (
            type(self.before_exposure) is not ExposureSnapshot
            or type(self.after_exposure) is not ExposureSnapshot
        ):
            raise SignedRiskPolicyError("constraint exposures are invalid")
        if type(self.controls) is not tuple or any(
            type(control) is not AppliedRiskControl for control in self.controls
        ):
            raise SignedRiskPolicyError("constraint controls must be immutable")
        if tuple(control.ordinal for control in self.controls) != tuple(
            range(1, len(self.controls) + 1)
        ):
            raise SignedRiskPolicyError("constraint control ordinals must be contiguous")
        _components(self.correlation_components, tuple(symbol for symbol, _ in self.final_targets))
        if type(self.converged) is not bool:
            raise SignedRiskPolicyError("constraint convergence flag must be boolean")
        if self.reason_code is not None and (
            type(self.reason_code) is not str or not self.reason_code
        ):
            raise SignedRiskPolicyError("constraint reason is invalid")
        if self.converged != (self.reason_code is None):
            raise SignedRiskPolicyError("constraint convergence and reason are inconsistent")
        if self.content_hash != _constraint_values_hash(
            original_targets=self.original_targets,
            final_targets=self.final_targets,
            before_exposure=self.before_exposure,
            after_exposure=self.after_exposure,
            controls=self.controls,
            correlation_components=self.correlation_components,
            converged=self.converged,
            reason_code=self.reason_code,
        ):
            raise SignedRiskPolicyError("constraint result hash is invalid")


def policy_hash(policy: RiskPolicySpec, risk_groups: Sequence[RiskGroupSpec]) -> str:
    """Hash every semantic signed-risk policy and risk-group parameter."""

    groups = _risk_groups(risk_groups)
    _policy(policy)
    return sha256_hex(
        {
            "policy": {
                "correlation_edge_threshold": policy.correlation_edge_threshold,
                "covariance_eigenvalue_floor": policy.covariance_eigenvalue_floor,
                "deployment_drawdown_trigger": policy.deployment_drawdown_trigger,
                "edge_saturation_bps": policy.edge_saturation_bps,
                "id": policy.id,
                "max_absolute_symbol_weight": policy.max_absolute_symbol_weight,
                "max_cluster_gross_weight": policy.max_cluster_gross_weight,
                "max_gross_weight": policy.max_gross_weight,
                "max_net_weight": policy.max_net_weight,
                "min_net_weight": policy.min_net_weight,
                "minimum_rebalance_equity_fraction": policy.minimum_rebalance_equity_fraction,
                "session_loss_trigger": policy.session_loss_trigger,
                "sigma_floor": policy.sigma_floor,
                "target_annualized_volatility": policy.target_annualized_volatility,
                "version": policy.version,
            },
            "risk_groups": tuple(
                {
                    "id": group.id,
                    "max_gross_weight": group.max_gross_weight,
                    "symbols": group.symbols,
                }
                for group in groups
            ),
            "schema": "signed-risk-policy-v1",
        }
    )


def calculate_initial_targets(
    *,
    signals: Sequence[SymbolSignal],
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
) -> InitialTargetResult:
    """Apply the exact edge/sigma score and 10%-volatility scaling formulas."""

    ordered = _signals(signals)
    _policy(policy)
    if type(statistics) is not RiskStatistics:
        raise SignedRiskPolicyError("initial sizing requires validated risk statistics")
    symbols = tuple(signal.symbol for signal in ordered)
    if symbols != statistics.symbols:
        raise SignedRiskPolicyError("signal and statistics symbols do not match")
    scores: list[tuple[str, Decimal]] = []
    with localcontext(_ARITHMETIC):
        for signal in ordered:
            edge = signal.expected_edge_bps or _ZERO
            edge_fraction = min(abs(edge) / policy.edge_saturation_bps, _ONE)
            denominator = max(statistics.sigma_for(signal.symbol), policy.sigma_floor)
            direction = {
                SignalDirection.FLAT: _ZERO,
                SignalDirection.LONG: _ONE,
                SignalDirection.SHORT: -_ONE,
            }[signal.direction]
            scores.append((signal.symbol, direction * edge_fraction / denominator))
    raw_scores = tuple(scores)
    zeros = tuple((symbol, _ZERO) for symbol in symbols)
    if all(score == 0 for _, score in raw_scores):
        return _initial_target_result(
            raw_scores=raw_scores,
            forecast_volatility=None,
            multiplier=None,
            targets=zeros,
            reason_code="all_scores_zero",
        )
    try:
        with localcontext(_ARITHMETIC):
            forecast_variance = sum(
                (
                    left_score * statistics.covariance_for(left_symbol, right_symbol) * right_score
                    for left_symbol, left_score in raw_scores
                    for right_symbol, right_score in raw_scores
                ),
                _ZERO,
            )
            if forecast_variance <= 0 or not forecast_variance.is_finite():
                raise SignedRiskPolicyError("forecast volatility is nonpositive or nonfinite")
            forecast_volatility = forecast_variance.sqrt()
            if forecast_volatility <= 0 or not forecast_volatility.is_finite():
                raise SignedRiskPolicyError("forecast volatility is nonpositive or nonfinite")
            multiplier = policy.target_annualized_volatility / forecast_volatility
            if multiplier <= 0 or not multiplier.is_finite():
                raise SignedRiskPolicyError("initial multiplier is nonpositive or nonfinite")
            targets = tuple((symbol, score * multiplier) for symbol, score in raw_scores)
    except DecimalException:
        raise SignedRiskPolicyError("forecast volatility could not be calculated") from None
    return _initial_target_result(
        raw_scores=raw_scores,
        forecast_volatility=forecast_volatility,
        multiplier=multiplier,
        targets=targets,
        reason_code=None,
    )


def apply_signed_constraints(
    *,
    targets: Sequence[tuple[str, Decimal]],
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
    risk_groups: Sequence[RiskGroupSpec],
) -> ConstraintResult:
    """Apply all hard constraints independently of the process Decimal context."""

    with localcontext(_ARITHMETIC):
        return _apply_signed_constraints_in_context(
            targets=targets,
            statistics=statistics,
            policy=policy,
            risk_groups=risk_groups,
        )


def _apply_signed_constraints_in_context(
    *,
    targets: Sequence[tuple[str, Decimal]],
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
    risk_groups: Sequence[RiskGroupSpec],
) -> ConstraintResult:
    """Apply every shrink-only hard constraint in the required deterministic order."""

    original = _normalized_weights(targets)
    symbols = tuple(symbol for symbol, _ in original)
    if type(statistics) is not RiskStatistics or statistics.symbols != symbols:
        raise SignedRiskPolicyError("constraint statistics must match target symbols")
    _policy(policy)
    groups = _risk_groups(risk_groups, symbols=symbols)
    components = correlation_components(
        statistics=statistics,
        threshold=policy.correlation_edge_threshold,
    )
    weights = dict(original)
    controls: list[AppliedRiskControl] = []
    converged = False

    for pass_number in range(1, _MAX_CONSTRAINT_PASSES + 1):
        start = tuple((symbol, weights[symbol]) for symbol in symbols)
        _clip_symbols(
            weights,
            symbols=symbols,
            limit=policy.max_absolute_symbol_weight,
            pass_number=pass_number,
            controls=controls,
        )
        _scale_all_gross(
            weights,
            symbols=symbols,
            limit=policy.max_gross_weight,
            pass_number=pass_number,
            controls=controls,
        )
        _scale_positive_net(
            weights,
            symbols=symbols,
            limit=policy.max_net_weight,
            pass_number=pass_number,
            controls=controls,
        )
        _scale_negative_net(
            weights,
            symbols=symbols,
            limit=policy.min_net_weight,
            pass_number=pass_number,
            controls=controls,
        )
        for group in groups:
            _scale_scope_gross(
                weights,
                all_symbols=symbols,
                scope_symbols=group.symbols,
                limit=group.max_gross_weight,
                pass_number=pass_number,
                control=RiskControl.RISK_GROUP_GROSS,
                scope=f"group:{group.id}",
                controls=controls,
            )
        for component in components:
            _scale_scope_gross(
                weights,
                all_symbols=symbols,
                scope_symbols=component,
                limit=policy.max_cluster_gross_weight,
                pass_number=pass_number,
                control=RiskControl.CLUSTER_GROSS,
                scope=f"cluster:{','.join(component)}",
                controls=controls,
            )
        end = tuple((symbol, weights[symbol]) for symbol in symbols)
        maximum_change = max(abs(dict(start)[symbol] - weights[symbol]) for symbol in symbols)
        if maximum_change <= _CONVERGENCE_TOLERANCE and constraints_satisfied(
            weights=end,
            statistics=statistics,
            policy=policy,
            risk_groups=groups,
        ):
            converged = True
            break

    before = exposure_snapshot(
        weights=original,
        risk_groups=groups,
        correlation_components_=components,
    )
    if not converged:
        final = tuple((symbol, _ZERO) for symbol in symbols)
        reason = "risk_constraints_not_converged"
    else:
        final = tuple((symbol, weights[symbol]) for symbol in symbols)
        reason = None
    after = exposure_snapshot(
        weights=final,
        risk_groups=groups,
        correlation_components_=components,
    )
    content_hash = _constraint_values_hash(
        original_targets=original,
        final_targets=final,
        before_exposure=before,
        after_exposure=after,
        controls=tuple(controls),
        correlation_components=components,
        converged=converged,
        reason_code=reason,
    )
    return ConstraintResult(
        original_targets=original,
        final_targets=final,
        before_exposure=before,
        after_exposure=after,
        controls=tuple(controls),
        correlation_components=components,
        converged=converged,
        reason_code=reason,
        content_hash=content_hash,
    )


def apply_rebalance_band(
    *,
    targets: Sequence[tuple[str, Decimal]],
    current_quantities: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    equity: Decimal,
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
    risk_groups: Sequence[RiskGroupSpec],
    force_reduction: bool,
) -> tuple[tuple[tuple[str, Decimal], ...], tuple[AppliedRiskControl, ...]]:
    """Apply the rebalance band independently of the process Decimal context."""

    with localcontext(_ARITHMETIC):
        return _apply_rebalance_band_in_context(
            targets=targets,
            current_quantities=current_quantities,
            prices=prices,
            equity=equity,
            statistics=statistics,
            policy=policy,
            risk_groups=risk_groups,
            force_reduction=force_reduction,
        )


def _apply_rebalance_band_in_context(
    *,
    targets: Sequence[tuple[str, Decimal]],
    current_quantities: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    equity: Decimal,
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
    risk_groups: Sequence[RiskGroupSpec],
    force_reduction: bool,
) -> tuple[tuple[tuple[str, Decimal], ...], tuple[AppliedRiskControl, ...]]:
    """Retain below-band current weights only when all hard constraints remain satisfied."""

    desired = _normalized_weights(targets)
    symbols = tuple(symbol for symbol, _ in desired)
    quantities = _exact_decimal_mapping(
        current_quantities,
        symbols=symbols,
        field_name="current quantity",
        positive=False,
    )
    validated_prices = _exact_decimal_mapping(
        prices,
        symbols=symbols,
        field_name="planning price",
        positive=True,
    )
    account_equity = _decimal(equity, field_name="equity")
    if account_equity <= 0:
        raise SignedRiskPolicyError("equity must be positive")
    if type(force_reduction) is not bool:
        raise SignedRiskPolicyError("force_reduction must be boolean")
    groups = _risk_groups(risk_groups, symbols=symbols)
    current = {
        symbol: quantities[symbol] * validated_prices[symbol] / account_equity for symbol in symbols
    }
    result = dict(desired)
    controls: list[AppliedRiskControl] = []
    threshold = policy.minimum_rebalance_equity_fraction * account_equity
    if force_reduction:
        return desired, ()

    for symbol, target in desired:
        if target == 0:
            continue
        change_dollars = abs((target - current[symbol]) * account_equity)
        if change_dollars >= threshold:
            continue
        candidate = dict(result)
        candidate[symbol] = current[symbol]
        candidate_tuple = tuple((item, candidate[item]) for item in symbols)
        if not constraints_satisfied(
            weights=candidate_tuple,
            statistics=statistics,
            policy=policy,
            risk_groups=groups,
        ):
            continue
        before = tuple((item, result[item]) for item in symbols)
        result = candidate
        after = tuple((item, result[item]) for item in symbols)
        controls.append(
            AppliedRiskControl(
                pass_number=1,
                ordinal=len(controls) + 1,
                control=RiskControl.REBALANCE_BAND,
                scope=f"symbol:{symbol}",
                factor=_band_factor(target=target, retained=current[symbol]),
                before=before,
                after=after,
            )
        )
    return tuple((symbol, result[symbol]) for symbol in symbols), tuple(controls)


def correlation_components(
    *,
    statistics: RiskStatistics,
    threshold: Decimal,
) -> tuple[tuple[str, ...], ...]:
    """Build deterministic components for absolute correlations strictly above threshold."""

    if type(statistics) is not RiskStatistics:
        raise SignedRiskPolicyError("correlation components require validated statistics")
    edge_threshold = _decimal(threshold, field_name="correlation_threshold")
    if not _ZERO <= edge_threshold <= _ONE:
        raise SignedRiskPolicyError("correlation threshold must be in [0, 1]")
    symbols = statistics.symbols
    parent = {symbol: symbol for symbol in symbols}

    def find(symbol: str) -> str:
        while parent[symbol] != symbol:
            parent[symbol] = parent[parent[symbol]]
            symbol = parent[symbol]
        return symbol

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        smaller, larger = sorted((left_root, right_root))
        parent[larger] = smaller

    for left_index, left in enumerate(symbols):
        for right in symbols[left_index + 1 :]:
            if abs(statistics.correlation_for(left, right)) > edge_threshold:
                union(left, right)
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(find(symbol), []).append(symbol)
    return tuple(sorted((tuple(items) for items in groups.values()), key=lambda item: item[0]))


def constraints_satisfied(
    *,
    weights: Sequence[tuple[str, Decimal]],
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
    risk_groups: Sequence[RiskGroupSpec],
) -> bool:
    """Recompute hard constraints independently of the process Decimal context."""

    with localcontext(_ARITHMETIC):
        return _constraints_satisfied_in_context(
            weights=weights,
            statistics=statistics,
            policy=policy,
            risk_groups=risk_groups,
        )


def _constraints_satisfied_in_context(
    *,
    weights: Sequence[tuple[str, Decimal]],
    statistics: RiskStatistics,
    policy: RiskPolicySpec,
    risk_groups: Sequence[RiskGroupSpec],
) -> bool:
    """Recompute every hard constraint without mutating targets."""

    ordered = _normalized_weights(weights)
    symbols = tuple(symbol for symbol, _ in ordered)
    if type(statistics) is not RiskStatistics or statistics.symbols != symbols:
        raise SignedRiskPolicyError("constraint statistics must match target symbols")
    _policy(policy)
    groups = _risk_groups(risk_groups, symbols=symbols)
    values = dict(ordered)
    positive = sum((value for value in values.values() if value > 0), _ZERO)
    short_abs = sum((-value for value in values.values() if value < 0), _ZERO)
    if any(abs(value) > policy.max_absolute_symbol_weight for value in values.values()):
        return False
    if positive + short_abs > policy.max_gross_weight:
        return False
    net = positive - short_abs
    if net > policy.max_net_weight or net < policy.min_net_weight:
        return False
    if any(
        sum((abs(values[symbol]) for symbol in group.symbols), _ZERO) > group.max_gross_weight
        for group in groups
    ):
        return False
    return not any(
        sum((abs(values[symbol]) for symbol in component), _ZERO) > policy.max_cluster_gross_weight
        for component in correlation_components(
            statistics=statistics,
            threshold=policy.correlation_edge_threshold,
        )
    )


def exposure_snapshot(
    *,
    weights: Sequence[tuple[str, Decimal]],
    risk_groups: Sequence[RiskGroupSpec],
    correlation_components_: Sequence[tuple[str, ...]],
) -> ExposureSnapshot:
    """Calculate exposures independently of the process Decimal context."""

    with localcontext(_ARITHMETIC):
        return _exposure_snapshot_in_context(
            weights=weights,
            risk_groups=risk_groups,
            correlation_components_=correlation_components_,
        )


def _exposure_snapshot_in_context(
    *,
    weights: Sequence[tuple[str, Decimal]],
    risk_groups: Sequence[RiskGroupSpec],
    correlation_components_: Sequence[tuple[str, ...]],
) -> ExposureSnapshot:
    """Calculate signed, group, and component exposures for a decision receipt."""

    ordered = _normalized_weights(weights)
    symbols = tuple(symbol for symbol, _ in ordered)
    values = dict(ordered)
    groups = _risk_groups(risk_groups, symbols=symbols)
    components = _components(correlation_components_, symbols)
    positive = sum((value for value in values.values() if value > 0), _ZERO)
    short_abs = sum((-value for value in values.values() if value < 0), _ZERO)
    return ExposureSnapshot(
        gross=positive + short_abs,
        net=positive - short_abs,
        positive=positive,
        short_abs=short_abs,
        group_gross=tuple(
            (
                group.id,
                sum((abs(values[symbol]) for symbol in group.symbols), _ZERO),
            )
            for group in groups
        ),
        cluster_gross=tuple(
            (
                ",".join(component),
                sum((abs(values[symbol]) for symbol in component), _ZERO),
            )
            for component in components
        ),
    )


def _initial_target_result(
    *,
    raw_scores: tuple[tuple[str, Decimal], ...],
    forecast_volatility: Decimal | None,
    multiplier: Decimal | None,
    targets: tuple[tuple[str, Decimal], ...],
    reason_code: str | None,
) -> InitialTargetResult:
    return InitialTargetResult(
        raw_scores=raw_scores,
        forecast_volatility=forecast_volatility,
        multiplier=multiplier,
        targets=targets,
        reason_code=reason_code,
        content_hash=_initial_target_hash(
            raw_scores=raw_scores,
            forecast_volatility=forecast_volatility,
            multiplier=multiplier,
            targets=targets,
            reason_code=reason_code,
        ),
    )


def _initial_target_hash(
    *,
    raw_scores: tuple[tuple[str, Decimal], ...],
    forecast_volatility: Decimal | None,
    multiplier: Decimal | None,
    targets: tuple[tuple[str, Decimal], ...],
    reason_code: str | None,
) -> str:
    return sha256_hex(
        {
            "forecast_volatility": forecast_volatility,
            "multiplier": multiplier,
            "raw_scores": raw_scores,
            "reason_code": reason_code,
            "schema": "signed-risk-initial-target-v1",
            "targets": targets,
        }
    )


def _constraint_values_hash(
    *,
    original_targets: tuple[tuple[str, Decimal], ...],
    final_targets: tuple[tuple[str, Decimal], ...],
    before_exposure: ExposureSnapshot,
    after_exposure: ExposureSnapshot,
    controls: tuple[AppliedRiskControl, ...],
    correlation_components: tuple[tuple[str, ...], ...],
    converged: bool,
    reason_code: str | None,
) -> str:
    return sha256_hex(
        {
            "after_exposure": _exposure_payload(after_exposure),
            "before_exposure": _exposure_payload(before_exposure),
            "controls": tuple(
                {
                    "after": control.after,
                    "before": control.before,
                    "control": control.control,
                    "factor": control.factor,
                    "ordinal": control.ordinal,
                    "pass_number": control.pass_number,
                    "scope": control.scope,
                }
                for control in controls
            ),
            "correlation_components": correlation_components,
            "converged": converged,
            "final_targets": final_targets,
            "original_targets": original_targets,
            "reason_code": reason_code,
            "schema": "signed-risk-constraint-result-v1",
        }
    )


def _exposure_payload(exposure: ExposureSnapshot) -> dict[str, object]:
    return {
        "cluster_gross": exposure.cluster_gross,
        "gross": exposure.gross,
        "group_gross": exposure.group_gross,
        "net": exposure.net,
        "positive": exposure.positive,
        "short_abs": exposure.short_abs,
    }


def _clip_symbols(
    weights: dict[str, Decimal],
    *,
    symbols: tuple[str, ...],
    limit: Decimal,
    pass_number: int,
    controls: list[AppliedRiskControl],
) -> None:
    for symbol in symbols:
        value = weights[symbol]
        clipped = max(-limit, min(limit, value))
        if clipped == value:
            continue
        before = tuple((item, weights[item]) for item in symbols)
        weights[symbol] = clipped
        _record_control(
            weights=weights,
            symbols=symbols,
            before=before,
            pass_number=pass_number,
            control=RiskControl.SYMBOL_CLIP,
            scope=f"symbol:{symbol}",
            factor=abs(clipped / value),
            controls=controls,
        )


def _scale_all_gross(
    weights: dict[str, Decimal],
    *,
    symbols: tuple[str, ...],
    limit: Decimal,
    pass_number: int,
    controls: list[AppliedRiskControl],
) -> None:
    gross = sum((abs(weights[symbol]) for symbol in symbols), _ZERO)
    if gross <= limit:
        return
    before = tuple((symbol, weights[symbol]) for symbol in symbols)
    factor = limit / gross
    for symbol in symbols:
        weights[symbol] *= factor
    _trim_gross_excess(weights, scope_symbols=symbols, limit=limit)
    _record_control(
        weights=weights,
        symbols=symbols,
        before=before,
        pass_number=pass_number,
        control=RiskControl.GROSS,
        scope="portfolio",
        factor=factor,
        controls=controls,
    )


def _scale_positive_net(
    weights: dict[str, Decimal],
    *,
    symbols: tuple[str, ...],
    limit: Decimal,
    pass_number: int,
    controls: list[AppliedRiskControl],
) -> None:
    positive = sum((weights[symbol] for symbol in symbols if weights[symbol] > 0), _ZERO)
    short_abs = sum((-weights[symbol] for symbol in symbols if weights[symbol] < 0), _ZERO)
    if positive - short_abs <= limit:
        return
    factor = (limit + short_abs) / positive
    if not _ZERO <= factor <= _ONE:
        raise SignedRiskPolicyError("positive-net shrink factor is invalid")
    before = tuple((symbol, weights[symbol]) for symbol in symbols)
    for symbol in symbols:
        if weights[symbol] > 0:
            weights[symbol] *= factor
    adjusted_positive = sum((weights[symbol] for symbol in symbols if weights[symbol] > 0), _ZERO)
    adjusted_short = sum((-weights[symbol] for symbol in symbols if weights[symbol] < 0), _ZERO)
    excess = (adjusted_positive - adjusted_short) - limit
    if excess > 0:
        first_positive = next(symbol for symbol in symbols if weights[symbol] > 0)
        weights[first_positive] -= excess
    _record_control(
        weights=weights,
        symbols=symbols,
        before=before,
        pass_number=pass_number,
        control=RiskControl.NET_LONG,
        scope="positive_weights",
        factor=factor,
        controls=controls,
    )


def _scale_negative_net(
    weights: dict[str, Decimal],
    *,
    symbols: tuple[str, ...],
    limit: Decimal,
    pass_number: int,
    controls: list[AppliedRiskControl],
) -> None:
    positive = sum((weights[symbol] for symbol in symbols if weights[symbol] > 0), _ZERO)
    short_abs = sum((-weights[symbol] for symbol in symbols if weights[symbol] < 0), _ZERO)
    if positive - short_abs >= limit:
        return
    factor = ((-limit) + positive) / short_abs
    if not _ZERO <= factor <= _ONE:
        raise SignedRiskPolicyError("negative-net shrink factor is invalid")
    before = tuple((symbol, weights[symbol]) for symbol in symbols)
    for symbol in symbols:
        if weights[symbol] < 0:
            weights[symbol] *= factor
    adjusted_positive = sum((weights[symbol] for symbol in symbols if weights[symbol] > 0), _ZERO)
    adjusted_short = sum((-weights[symbol] for symbol in symbols if weights[symbol] < 0), _ZERO)
    excess = limit - (adjusted_positive - adjusted_short)
    if excess > 0:
        first_negative = next(symbol for symbol in symbols if weights[symbol] < 0)
        weights[first_negative] += excess
    _record_control(
        weights=weights,
        symbols=symbols,
        before=before,
        pass_number=pass_number,
        control=RiskControl.NET_SHORT,
        scope="negative_weights",
        factor=factor,
        controls=controls,
    )


def _scale_scope_gross(
    weights: dict[str, Decimal],
    *,
    all_symbols: tuple[str, ...],
    scope_symbols: Sequence[str],
    limit: Decimal,
    pass_number: int,
    control: RiskControl,
    scope: str,
    controls: list[AppliedRiskControl],
) -> None:
    gross = sum((abs(weights[symbol]) for symbol in scope_symbols), _ZERO)
    if gross <= limit:
        return
    before = tuple((symbol, weights[symbol]) for symbol in all_symbols)
    factor = limit / gross
    for symbol in scope_symbols:
        weights[symbol] *= factor
    _trim_gross_excess(weights, scope_symbols=scope_symbols, limit=limit)
    _record_control(
        weights=weights,
        symbols=all_symbols,
        before=before,
        pass_number=pass_number,
        control=control,
        scope=scope,
        factor=factor,
        controls=controls,
    )


def _record_control(
    *,
    weights: dict[str, Decimal],
    symbols: tuple[str, ...],
    before: tuple[tuple[str, Decimal], ...],
    pass_number: int,
    control: RiskControl,
    scope: str,
    factor: Decimal,
    controls: list[AppliedRiskControl],
) -> None:
    after = tuple((symbol, weights[symbol]) for symbol in symbols)
    controls.append(
        AppliedRiskControl(
            pass_number=pass_number,
            ordinal=len(controls) + 1,
            control=control,
            scope=scope,
            factor=factor,
            before=before,
            after=after,
        )
    )


def _trim_gross_excess(
    weights: dict[str, Decimal],
    *,
    scope_symbols: Sequence[str],
    limit: Decimal,
) -> None:
    """Remove finite-precision overshoot from one weight without redistributing it."""

    gross = sum((abs(weights[symbol]) for symbol in scope_symbols), _ZERO)
    excess = gross - limit
    if excess <= 0:
        return
    symbol = next(symbol for symbol in scope_symbols if weights[symbol] != 0)
    value = weights[symbol]
    weights[symbol] = value - excess if value > 0 else value + excess


def _band_factor(*, target: Decimal, retained: Decimal) -> Decimal:
    if target == 0 or retained == target:
        return _ONE
    if abs(retained) <= abs(target) and retained * target >= 0:
        return abs(retained / target)
    # A rebalance hold can be larger or across zero; this field remains a bounded operation marker,
    # not a claim that the rebalance band is one of the shrink-only hard constraints.
    return _ONE


def _signals(signals: Sequence[SymbolSignal]) -> tuple[SymbolSignal, ...]:
    if type(signals) not in {list, tuple} or not signals:
        raise SignedRiskPolicyError("signals must be a nonempty bounded sequence")
    ordered = tuple(signals)
    if any(type(signal) is not SymbolSignal for signal in ordered):
        raise SignedRiskPolicyError("signals must use the validated symbol contract")
    if tuple(signal.symbol for signal in ordered) != tuple(
        sorted(signal.symbol for signal in ordered)
    ) or len({signal.symbol for signal in ordered}) != len(ordered):
        raise SignedRiskPolicyError("signals must be unique and alphabetically ordered")
    return ordered


def _normalized_weights(
    weights: Sequence[tuple[str, Decimal]],
) -> tuple[tuple[str, Decimal], ...]:
    if type(weights) not in {list, tuple} or not weights:
        raise SignedRiskPolicyError("weights must be a nonempty bounded sequence")
    normalized: list[tuple[str, Decimal]] = []
    for item in weights:
        if type(item) is not tuple or len(item) != 2:
            raise SignedRiskPolicyError("weight entries must be symbol-value pairs")
        symbol, value = item
        _symbol(symbol)
        normalized.append((symbol, _decimal(value, field_name="target_weight")))
    symbols = tuple(symbol for symbol, _ in normalized)
    if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
        raise SignedRiskPolicyError("weight symbols must be unique and alphabetically ordered")
    return tuple(normalized)


def _weight_tuple(value: object) -> tuple[tuple[str, Decimal], ...]:
    if type(value) is not tuple:
        raise SignedRiskPolicyError("weights must be immutable")
    return _normalized_weights(value)


def _named_decimal_tuple(value: object, *, field_name: str) -> tuple[tuple[str, Decimal], ...]:
    if type(value) is not tuple:
        raise SignedRiskPolicyError(f"{field_name} must be immutable")
    previous: str | None = None
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise SignedRiskPolicyError(f"{field_name} entries are invalid")
        name, amount = item
        if type(name) is not str or not name:
            raise SignedRiskPolicyError(f"{field_name} name is invalid")
        if previous is not None and name <= previous:
            raise SignedRiskPolicyError(f"{field_name} must be unique and ordered")
        if _decimal(amount, field_name="gross_exposure") < 0:
            raise SignedRiskPolicyError(f"{field_name} cannot be negative")
        previous = name
    return value


def _risk_groups(
    risk_groups: Sequence[RiskGroupSpec],
    *,
    symbols: tuple[str, ...] | None = None,
) -> tuple[RiskGroupSpec, ...]:
    if type(risk_groups) not in {list, tuple} or not risk_groups:
        raise SignedRiskPolicyError("risk groups must be a nonempty bounded sequence")
    groups = tuple(risk_groups)
    if any(type(group) is not RiskGroupSpec for group in groups):
        raise SignedRiskPolicyError("risk groups must use validated configuration")
    if tuple(group.id for group in groups) != tuple(sorted(group.id for group in groups)):
        raise SignedRiskPolicyError("risk groups must be ordered by ID")
    if len({group.id for group in groups}) != len(groups):
        raise SignedRiskPolicyError("risk group IDs must be unique")
    grouped = tuple(symbol for group in groups for symbol in group.symbols)
    if len(grouped) != len(set(grouped)):
        raise SignedRiskPolicyError("risk groups must not overlap")
    if symbols is not None and set(grouped) != set(symbols):
        raise SignedRiskPolicyError("risk groups must partition target symbols")
    return groups


def _components(
    value: Sequence[tuple[str, ...]],
    symbols: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    if type(value) not in {list, tuple}:
        raise SignedRiskPolicyError("correlation components must be a bounded sequence")
    components = tuple(value)
    flattened: list[str] = []
    for component in components:
        if type(component) is not tuple or not component:
            raise SignedRiskPolicyError("correlation component is invalid")
        if component != tuple(sorted(component)) or len(component) != len(set(component)):
            raise SignedRiskPolicyError("correlation component must be unique and ordered")
        flattened.extend(component)
    if tuple(sorted(flattened)) != symbols or len(flattened) != len(set(flattened)):
        raise SignedRiskPolicyError("correlation components must partition symbols")
    if components != tuple(sorted(components, key=lambda item: item[0])):
        raise SignedRiskPolicyError("correlation components must be deterministically ordered")
    return components


def _exact_decimal_mapping(
    value: Mapping[str, Decimal],
    *,
    symbols: tuple[str, ...],
    field_name: str,
    positive: bool,
) -> dict[str, Decimal]:
    if not isinstance(value, Mapping) or set(value) != set(symbols):
        raise SignedRiskPolicyError(f"{field_name} mapping must contain exactly active symbols")
    normalized: dict[str, Decimal] = {}
    for symbol in symbols:
        number = _decimal(value[symbol], field_name=field_name.replace(" ", "_"))
        if positive and number <= 0:
            raise SignedRiskPolicyError(f"{field_name} must be positive")
        normalized[symbol] = number
    return normalized


def _policy(policy: object) -> RiskPolicySpec:
    if type(policy) is not RiskPolicySpec:
        raise SignedRiskPolicyError("risk policy must use validated configuration")
    if not policy.min_net_weight <= 0 <= policy.max_net_weight:
        raise SignedRiskPolicyError("signed policy net bounds must contain zero")
    return policy


def _decimal(value: object, *, field_name: str) -> Decimal:
    try:
        return require_finite_decimal(value, field_name=field_name)
    except DomainValidationError:
        raise SignedRiskPolicyError(f"{field_name} must be a finite exact decimal") from None


def _symbol(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 10
        or not value.isascii()
        or not value.isupper()
        or not value.replace(".", "").isalnum()
    ):
        raise SignedRiskPolicyError("symbol is invalid")
    return value
