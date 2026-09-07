"""Injected Trading API activities prove fills without provider calls or invented IDs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from requests import Response, Session

from adaptive_trader.platform.execution.alpaca_paper import (
    _AlpacaSdkPaperClient,
    _FixedPaperSession,
)
from adaptive_trader.platform.execution.models import ExecutionValidationError, OrderState
from tests.unit.test_platform_execution_broker import _long_plan

NOW = datetime(2026, 7, 6, 14, tzinfo=UTC)
CLIENT = _long_plan().intents[0].client_order_id


def order(**changes):
    return (
        dict(
            id="order-id",
            client_order_id=CLIENT,
            symbol="AAA",
            side="buy",
            asset_class="us_equity",
            qty="2",
            status="filled",
            filled_qty="2",
            filled_avg_price="101",
            submitted_at=NOW,
            updated_at=NOW + timedelta(seconds=2),
        )
        | changes
    )


def activity(index=1, **changes):
    return (
        dict(
            id=f"2026070614000000{index}::execution-{index}",
            activity_type="FILL",
            order_id="order-id",
            symbol="AAA",
            side="buy",
            type="partial_fill" if index == 1 else "fill",
            qty="1",
            price="100" if index == 1 else "102",
            cum_qty=str(index),
            leaves_qty=str(2 - index),
            transaction_time=(NOW + timedelta(seconds=index)).isoformat(),
        )
        | changes
    )


def client(raw, pages):
    calls = []

    def get(path, **kwargs):
        calls.append((path, kwargs))
        return pages[min(len(calls) - 1, len(pages) - 1)]

    return _AlpacaSdkPaperClient(
        SimpleNamespace(get_order_by_client_id=lambda _: raw, get=get)
    ), calls


def test_multiple_out_of_order_duplicate_activities_produce_unique_real_execution_ids():
    facade, calls = client(order(), [[activity(2), activity(), activity()]])
    result = facade.lookup_by_client_order_id(CLIENT)
    assert result.state is OrderState.FILLED
    assert [f.broker_execution_id for f in result.fills] == [activity()["id"], activity(2)["id"]]
    assert sum(f.quantity for f in result.fills) == Decimal(2)
    assert result.average_fill_price == Decimal(101)
    assert all(f.fee == 0 for f in result.fills)
    assert calls == [
        (
            "/account/activities/FILL",
            dict(
                data=dict(order_id="order-id", direction="asc", page_size=100),
                base_url="https://paper-api.alpaca.markets",
                api_version="v2",
            ),
        )
    ]
    assert facade.lookup_by_client_order_id(CLIENT) == result


def test_partial_fill_uses_current_cumulative_without_inventing_remaining_execution():
    facade, _ = client(
        order(status="partially_filled", filled_qty="1", filled_avg_price="100"), [[activity()]]
    )
    result = facade.lookup_by_client_order_id(CLIENT)
    assert result.state is OrderState.PARTIALLY_FILLED
    assert len(result.fills) == 1 and result.fills[0].quantity == 1


@pytest.mark.parametrize(
    "change",
    [
        dict(order_id="other-order"),
        dict(symbol="BBB"),
        dict(side="sell"),
        dict(cum_qty="2"),
        dict(leaves_qty="0"),
        dict(qty="0"),
        dict(price="NaN"),
        dict(activity_type="FEE"),
        dict(type="correction"),
        dict(transaction_time="bad"),
        dict(transaction_time=NOW.replace(tzinfo=None).isoformat()),
        dict(fee="0.01"),
        dict(commission="1"),
    ],
)
def test_wrong_or_unsupported_fill_activity_fails_closed(change):
    facade, _ = client(order(), [[activity(**change), activity(2)]])
    with pytest.raises(ExecutionValidationError):
        facade.lookup_by_client_order_id(CLIENT)


@pytest.mark.parametrize(
    "pages",
    [
        [[]],
        [[activity()]],
        [[activity(), activity(price="99"), activity(2)]],
        [dict(not_a_page=True)],
    ],
)
def test_incomplete_or_conflicting_execution_evidence_is_not_a_fill(pages):
    facade, _ = client(order(), pages)
    with pytest.raises(ExecutionValidationError):
        facade.lookup_by_client_order_id(CLIENT)


@pytest.mark.parametrize(
    "change",
    [
        dict(filled_avg_price="102"),
        dict(asset_class="crypto"),
        dict(client_order_id="wrong-client"),
        dict(updated_at=NOW),
        dict(qty="1"),
    ],
)
def test_order_observation_must_match_activity_evidence(change):
    facade, _ = client(order(**change), [[activity(), activity(2)]])
    with pytest.raises(ExecutionValidationError):
        facade.lookup_by_client_order_id(CLIENT)


def test_pagination_uses_last_real_activity_id_and_requires_terminal_page():
    rows = [
        activity(
            index=i,
            qty="1",
            price="100",
            cum_qty=str(i),
            leaves_qty=str(101 - i),
            transaction_time=NOW.isoformat(),
        )
        for i in range(1, 102)
    ]
    facade, calls = client(
        order(qty="101", filled_qty="101", filled_avg_price="100"), [rows[:100], rows[100:]]
    )
    result = facade.lookup_by_client_order_id(CLIENT)
    assert len(result.fills) == 101
    assert calls[1][1]["data"]["page_token"] == rows[99]["id"]


def test_repeated_pagination_and_maximum_pages_are_bounded():
    facade, calls = client(order(), [[activity()] * 100])
    with pytest.raises(ExecutionValidationError, match="pagination repeated"):
        facade.lookup_by_client_order_id(CLIENT)
    assert len(calls) == 2
    pages = [
        [activity(id=f"activity-{page}-{index}") for index in range(100)] for page in range(10)
    ]
    facade, calls = client(order(), pages)
    with pytest.raises(ExecutionValidationError, match="bound exceeded"):
        facade.lookup_by_client_order_id(CLIENT)
    assert len(calls) == 10


@pytest.mark.parametrize("status", [404, 401, 429, 500, None])
def test_only_http404_proves_lookup_absence(status):
    def fail(_):
        raise APIError(
            '{"code":40410000,"message":"private upstream detail"}',
            SimpleNamespace(response=SimpleNamespace(status_code=status)),
        )

    facade = _AlpacaSdkPaperClient(SimpleNamespace(get_order_by_client_id=fail))
    if status == 404:
        assert facade.lookup_by_client_order_id(CLIENT) is None
    else:
        with pytest.raises(ExecutionValidationError, match=r"^Alpaca paper operation failed$"):
            facade.lookup_by_client_order_id(CLIENT)


def test_network_lookup_ambiguity_is_not_absence():
    def fail(_):
        raise TimeoutError("private upstream detail")

    with pytest.raises(ExecutionValidationError, match=r"^Alpaca paper operation failed$"):
        _AlpacaSdkPaperClient(
            SimpleNamespace(get_order_by_client_id=fail)
        ).lookup_by_client_order_id(CLIENT)


def test_fixed_paper_transport_enforces_timeout_and_disables_redirects(monkeypatch):
    calls = []

    def request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr(Session, "request", request)
    transport = _FixedPaperSession()
    assert transport.trust_env is False
    transport.request(
        "GET", "https://paper-api.alpaca.markets/v2/account", timeout=None, allow_redirects=True
    )
    assert calls[0][2] == dict(timeout=(3.05, 10), allow_redirects=False)
    for url in (
        "http://paper-api.alpaca.markets/v2/account",
        "https://api.alpaca.markets/v2/account",
        "https://paper-api.alpaca.markets.evil/v2/account",
    ):
        with pytest.raises(ExecutionValidationError, match="host"):
            transport.request("GET", url)
    assert len(calls) == 1


def test_same_timestamp_partial_observations_bind_distinct_fill_evidence():
    first, _ = client(
        order(qty="3", status="partially_filled", filled_qty="1", filled_avg_price="100"),
        [[activity(leaves_qty="2")]],
    )
    second, _ = client(
        order(qty="3", status="partially_filled"),
        [[activity(leaves_qty="2"), activity(2, leaves_qty="1", type="partial_fill")]],
    )
    first_result = first.lookup_by_client_order_id(CLIENT)
    second_result = second.lookup_by_client_order_id(CLIENT)
    assert first_result.occurred_at == second_result.occurred_at
    assert first_result.state == second_result.state
    assert first_result.broker_event_id != second_result.broker_event_id
    assert first.lookup_by_client_order_id(CLIENT) == first_result
    assert second.lookup_by_client_order_id(CLIENT) == second_result


def test_cancel_identity_mismatch_is_rejected_before_side_effect():
    calls = []
    facade = _AlpacaSdkPaperClient(
        SimpleNamespace(
            get_order_by_client_id=lambda _: order(client_order_id="other-order"),
            cancel_order_by_id=lambda order_id: calls.append(order_id),
        )
    )
    with pytest.raises(ExecutionValidationError, match="identity differs"):
        facade.cancel_by_client_order_id(CLIENT)
    assert calls == []


def test_cancel_confirmation_cannot_substitute_another_client_order():
    replies = iter([order(), order(client_order_id="other-order")])
    calls = []
    facade = _AlpacaSdkPaperClient(
        SimpleNamespace(
            get_order_by_client_id=lambda _: next(replies),
            cancel_order_by_id=lambda order_id: calls.append(order_id),
        )
    )
    with pytest.raises(ExecutionValidationError, match="identity differs"):
        facade.cancel_by_client_order_id(CLIENT)
    assert calls == ["order-id"]


@pytest.mark.parametrize("count", [499, 500, 501])
def test_open_order_snapshot_never_treats_a_capped_page_as_complete(count):
    calls = []

    def get_orders(*, filter):
        calls.append(filter)
        return [{"client_order_id": f"order-{index:04d}"} for index in range(count)]

    facade = _AlpacaSdkPaperClient(SimpleNamespace(get_orders=get_orders))
    if count < 500:
        assert len(facade.open_client_order_ids()) == count
    else:
        with pytest.raises(ExecutionValidationError, match="may be truncated"):
            facade.open_client_order_ids()
    assert len(calls) == 1 and calls[0].limit == 500
