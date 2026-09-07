"""Closed SDK boundary validation without constructing a network client."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adaptive_trader.platform.execution.alpaca_paper import _AlpacaSdkPaperClient, _paper_order
from adaptive_trader.platform.execution.models import ExecutionValidationError, OrderState

NOW = datetime(2026, 7, 6, 14, tzinfo=UTC)


def _asset(**overrides):
    return (
        dict(
            symbol="AAA",
            status="active",
            exchange="NASDAQ",
            asset_class="us_equity",
            tradable=True,
            shortable=True,
            easy_to_borrow=True,
        )
        | overrides
    )


def test_sdk_assets_preserve_nasdaq_eligibility_and_bind_each_symbol():
    calls = []

    def get_asset(symbol):
        calls.append(symbol)
        return _asset(symbol=symbol)

    client = _AlpacaSdkPaperClient(SimpleNamespace(get_asset=get_asset))
    values = client.security_metadata(("AAA", "BBB"), observed_at=NOW)
    assert calls == ["AAA", "BBB"]
    assert all(v.primary_listing_eligible and v.broker_capability_known for v in values)
    assert [v.symbol for v in values] == calls


@pytest.mark.parametrize("overrides", [{"symbol": "BBB"}, {"tradable": 1}, {"shortable": "true"}])
def test_sdk_assets_reject_identity_and_nonboolean_eligibility(overrides):
    client = _AlpacaSdkPaperClient(SimpleNamespace(get_asset=lambda _: _asset(**overrides)))
    with pytest.raises(ExecutionValidationError):
        client.security_metadata(("AAA",), observed_at=NOW)


@pytest.mark.parametrize("symbols", [("BBB", "AAA"), ("AAA", "AAA"), (), ("aaa",), ("A/",)])
def test_sdk_asset_request_validation_precedes_io(symbols):
    client = _AlpacaSdkPaperClient(SimpleNamespace())
    with pytest.raises(ExecutionValidationError):
        client.security_metadata(symbols, observed_at=NOW)


def test_sdk_account_positions_and_open_orders_normalize_signed_values():
    sdk = SimpleNamespace(
        get_account=lambda: dict(id="account", cash="50", equity="100", buying_power="200"),
        get_all_positions=lambda: [
            dict(symbol="BBB", qty="-2"),
            dict(symbol="AAA", qty="1"),
            dict(symbol="CCC", qty="0"),
        ],
        get_orders=lambda **_: [
            dict(client_order_id="z"),
            dict(client_order_id="a"),
            dict(client_order_id="z"),
        ],
    )
    client = _AlpacaSdkPaperClient(sdk)
    assert client.account_state(NOW).cash == Decimal(50)
    assert [(v.symbol, v.quantity) for v in client.signed_positions()] == [
        ("AAA", Decimal(1)),
        ("BBB", Decimal(-2)),
    ]
    assert client.open_client_order_ids() == ("a", "z")


def test_sdk_transport_errors_are_redacted():
    def fail():
        raise RuntimeError("credential-secret upstream URL")

    client = _AlpacaSdkPaperClient(SimpleNamespace(get_account=fail))
    with pytest.raises(ExecutionValidationError, match=r"^Alpaca paper operation failed$") as error:
        client.account_state(NOW)
    assert error.value.__suppress_context__


def _order(**overrides):
    return (
        dict(
            id="broker-id",
            client_order_id="client-id",
            status="accepted",
            filled_qty="0",
            updated_at=NOW,
        )
        | overrides
    )


def test_order_observations_are_deterministic_and_never_invent_fill_ids():
    first = _paper_order(_order())
    assert first == _paper_order(_order())
    assert first.state is OrderState.ACCEPTED
    assert first.fills == ()
    with pytest.raises(ExecutionValidationError, match="independently sourced"):
        _paper_order(_order(status="filled", filled_qty="1", filled_avg_price="10"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "unexpected"},
        {"filled_qty": "NaN"},
        {"updated_at": NOW.replace(tzinfo=None)},
        {"id": None},
    ],
)
def test_order_response_malformed_fields_fail_closed(overrides):
    with pytest.raises(ExecutionValidationError):
        _paper_order(_order(**overrides))
