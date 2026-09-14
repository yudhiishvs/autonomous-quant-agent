"""Non-AI approval cannot bypass ownership, changed configuration or independent risk."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from adaptive_trader.public_product.approvals import (
    ApprovalBinding,
    RiskLimits,
    approval_block_reason,
    validate_approval_limits,
)
from adaptive_trader.public_product.strategies import StrategyDefinition


def limits(**changes):
    return RiskLimits.model_validate(
        {
            "max_order_notional": "1000.00",
            "max_account_exposure": "5000.00",
            "max_position_shares": 10,
            "max_daily_loss": "100.00",
            "max_daily_turnover": "10000.00",
            **changes,
        }
    )


def binding():
    return ApprovalBinding(
        approval_id=uuid4(),
        owner_id=uuid4(),
        account_id=uuid4(),
        version_id=uuid4(),
        strategy_hash="a" * 64,
        broker_account_hash="c" * 64,
        connection_generation=0,
        limits=limits(),
        expires_at=datetime(2026, 9, 15, tzinfo=UTC),
    )


def inputs(value):
    return dict(
        stored_hash=value.content_hash,
        state="approved",
        owner_id=value.owner_id,
        account_id=value.account_id,
        version_id=value.version_id,
        strategy_hash=value.strategy_hash,
        broker_account_hash=value.broker_account_hash,
        connection_generation=0,
        account_connected=True,
        now=value.expires_at - timedelta(seconds=1),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_order_notional", 1.2),
        ("max_order_notional", "NaN"),
        ("max_order_notional", "Infinity"),
        ("max_order_notional", "0"),
        ("max_order_notional", "1.001"),
        ("max_order_notional", "6000"),
        ("max_daily_loss", "6000"),
        ("max_daily_turnover", "10"),
        ("max_account_exposure", "1000001"),
        ("max_position_shares", True),
        ("max_position_shares", 0),
        ("max_orders_per_minute", 11),
        ("validity_days", True),
        ("validity_days", 2),
        ("validity_days", "1"),
        ("unknown", True),
    ],
)
def test_invalid_risk_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError):
        limits(**{field: value})


def test_equivalent_decimal_limits_have_one_identity():
    assert limits().content_hash == limits(max_order_notional="1000").content_hash
    assert limits().content_hash != limits(max_daily_loss="99").content_hash


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"state": "draft"}, "approval_draft"),
        ({"state": "revoked"}, "approval_revoked"),
        ({"state": "other"}, "approval_invalid"),
        ({"owner_id": uuid4()}, "approval_binding_changed"),
        ({"account_id": uuid4()}, "approval_binding_changed"),
        ({"version_id": uuid4()}, "approval_binding_changed"),
        ({"strategy_hash": "b" * 64}, "approval_binding_changed"),
        ({"broker_account_hash": "b" * 64}, "approval_binding_changed"),
        ({"stored_hash": "b" * 64}, "approval_integrity_failed"),
        ({"connection_generation": 1}, "connection_changed"),
        ({"connection_generation": False}, "connection_changed"),
        ({"account_connected": False}, "account_disconnected"),
        ({"account_connected": "true"}, "account_disconnected"),
    ],
)
def test_each_changed_authority_boundary_blocks(changes, reason):
    value = binding()
    assert approval_block_reason(value, **{**inputs(value), **changes}) == reason


def test_exact_expiry_and_copied_model_revalidation():
    value = binding()
    assert approval_block_reason(value, **inputs(value)) is None
    assert (
        approval_block_reason(value, **{**inputs(value), "now": value.expires_at})
        == "approval_expired"
    )
    with pytest.raises(ValueError):
        approval_block_reason(value, **{**inputs(value), "now": datetime(2026, 9, 14)})
    with pytest.raises(ValidationError):
        approval_block_reason(
            value.model_copy(update={"connection_generation": False}), **inputs(value)
        )
    assert (
        ApprovalBinding.model_validate_json(value.model_dump_json()).content_hash
        == value.content_hash
    )


@pytest.mark.parametrize(
    "equity", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("4999.99"), 10000.0]
)
def test_limits_cannot_approve_leverage_or_unknown_equity(equity):
    definition = StrategyDefinition.model_validate(
        {
            "symbol": "SPY",
            "rule": {"kind": "constant_target", "target_shares": 1},
            "order": {"kind": "market"},
        }
    )
    with pytest.raises(ValueError):
        validate_approval_limits(definition, limits(), equity)


def test_target_must_fit_limits_and_material_changes_change_binding():
    definition = StrategyDefinition.model_validate(
        {
            "symbol": "SPY",
            "rule": {"kind": "constant_target", "target_shares": 11},
            "order": {"kind": "market"},
        }
    )
    with pytest.raises(ValueError, match="position limit"):
        validate_approval_limits(definition, limits(), Decimal(10000))
    validate_approval_limits(definition, limits(max_position_shares=11), Decimal(10000))
    value = binding()
    assert (
        value.model_copy(update={"limits": limits(max_daily_loss="99")}).content_hash
        != value.content_hash
    )
