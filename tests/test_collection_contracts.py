"""Offline tests for the versioned market-data collection contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adaptive_trader.collection import (
    COLLECTION_UNIVERSE_V1,
    CollectionRole,
    CollectionUniverseV1,
    MarketBarV1,
    RawBarObservationV1,
    UniverseMemberV1,
)

REQUESTED_EQUITIES = {
    "TSLA",
    "UBER",
    "GOOGL",
    "NVDA",
    "AMZN",
    "AAPL",
    "META",
    "AMD",
    "CSCO",
    "NET",
    "OKTA",
    "ROKU",
    "BOX",
    "ZG",
    "RBLX",
    "SOUN",
    "PUBM",
    "HLIT",
    "PAYC",
    "WDAY",
    "SNDK",
    "RIVN",
    "LCID",
    "AAOI",
    "AXTI",
    "INSG",
}


def _bar(**overrides: object) -> MarketBarV1:
    values: dict[str, object] = {
        "provider": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "symbol": "nvda",
        "timeframe": "1m",
        "bar_timestamp_utc": datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
        "receipt_timestamp_utc": datetime(2026, 9, 3, 14, 31, 2, tzinfo=UTC),
        "open": Decimal("100.00"),
        "high": Decimal("102.00"),
        "low": Decimal("99.50"),
        "close": Decimal("101.25"),
        "volume": 1234,
        "trade_count": 87,
        "vwap": Decimal("100.75"),
        "quality_flags": frozenset({"late"}),
        "source": "alpaca_websocket",
    }
    values.update(overrides)
    return MarketBarV1(**values)  # type: ignore[arg-type]


def test_collection_universe_contains_all_29_symbols_with_explicit_roles() -> None:
    assert len(COLLECTION_UNIVERSE_V1.symbols) == 29
    assert (
        set(COLLECTION_UNIVERSE_V1.symbols_for_role(CollectionRole.COLLECTED_EQUITY))
        == REQUESTED_EQUITIES
    )
    assert COLLECTION_UNIVERSE_V1.symbols_for_role(CollectionRole.CONTEXT) == ("SPY", "QQQ")
    assert COLLECTION_UNIVERSE_V1.symbols_for_role(CollectionRole.BENCHMARK) == ("SOXX",)
    assert COLLECTION_UNIVERSE_V1.execution_symbols == ()
    assert all(not member.execution_authorized for member in COLLECTION_UNIVERSE_V1.members)


def test_sndk_is_sandisk_and_wdc_is_not_substituted() -> None:
    assert COLLECTION_UNIVERSE_V1.member("sndk").company_name == "Sandisk Corporation"
    assert "WDC" not in COLLECTION_UNIVERSE_V1.symbols
    with pytest.raises(KeyError, match="WDC"):
        COLLECTION_UNIVERSE_V1.member("WDC")


def test_universe_hash_is_deterministic_and_order_independent() -> None:
    reordered = CollectionUniverseV1(tuple(reversed(COLLECTION_UNIVERSE_V1.members)))

    assert reordered.universe_hash == COLLECTION_UNIVERSE_V1.universe_hash
    assert len(COLLECTION_UNIVERSE_V1.universe_hash) == 64
    assert (
        COLLECTION_UNIVERSE_V1.universe_hash
        == "1abbdb81bcb03e71b2a045d5ba750a40722d91c24e3a145e758b2ed9f6521a79"
    )


def test_collection_universe_rejects_duplicates_and_execution_authority() -> None:
    member = COLLECTION_UNIVERSE_V1.members[0]
    with pytest.raises(ValueError, match="duplicate symbols"):
        CollectionUniverseV1((member, member))
    with pytest.raises(ValueError, match="cannot authorize execution"):
        UniverseMemberV1("NVDA", "NVIDIA Corporation", CollectionRole.COLLECTED_EQUITY, True)


def test_market_bar_normalizes_values_and_is_immutable() -> None:
    eastern = timezone(timedelta(hours=-4))
    bar = _bar(
        bar_timestamp_utc=datetime(2026, 9, 3, 10, 30, tzinfo=eastern),
        receipt_timestamp_utc=datetime(2026, 9, 3, 10, 31, 2, tzinfo=eastern),
        quality_flags=frozenset({" Late ", "LATE"}),
    )

    assert bar.symbol == "NVDA"
    assert bar.feed == "IEX"
    assert bar.bar_timestamp_utc == datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    assert bar.quality_flags == frozenset({"late"})
    with pytest.raises(FrozenInstanceError):
        bar.close = Decimal("50")  # type: ignore[misc]


def test_identity_and_content_hashes_have_explicit_stable_semantics() -> None:
    original = _bar()
    retransmission = replace(
        original,
        receipt_timestamp_utc=original.receipt_timestamp_utc + timedelta(minutes=2),
        source="alpaca_historical_rest",
        quality_flags=frozenset({"gap_repair"}),
        open=Decimal("100.0000"),
    )
    correction = replace(original, close=Decimal("101.30"))

    assert retransmission.identity_hash == original.identity_hash
    assert retransmission.content_hash == original.content_hash
    assert correction.identity_hash == original.identity_hash
    assert correction.content_hash != original.content_hash
    assert len(original.identity_hash) == len(original.content_hash) == 64


def test_provider_event_timestamp_participates_in_content_hash() -> None:
    original = _bar()
    updated = replace(
        original,
        provider_event_timestamp_utc=datetime(2026, 9, 3, 14, 31, tzinfo=UTC),
    )

    assert updated.identity_hash == original.identity_hash
    assert updated.content_hash != original.content_hash


def test_raw_observation_has_retry_stable_id_and_explicit_correction_semantics() -> None:
    bar = _bar()
    payload_hash = "a" * 64
    original = RawBarObservationV1(
        bar=bar,
        provider_event_id="event-1",
        raw_payload_sha256=payload_hash,
    )
    retry = RawBarObservationV1(
        bar=replace(bar, receipt_timestamp_utc=bar.receipt_timestamp_utc + timedelta(seconds=5)),
        provider_event_id="event-1",
        raw_payload_sha256=payload_hash.upper(),
    )
    correction = replace(original, is_correction=True)

    assert retry.observation_id == original.observation_id
    assert correction.observation_id != original.observation_id
    assert original.identity_hash == bar.identity_hash
    assert original.content_hash == bar.content_hash
    with pytest.raises(FrozenInstanceError):
        original.is_correction = True  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        ("bar_timestamp_utc", datetime(2026, 9, 3, 14, 30), "timezone-aware"),
        ("receipt_timestamp_utc", datetime(2026, 9, 3, 14, 31), "timezone-aware"),
        ("volume", -1, "nonnegative"),
        ("volume", 1.5, "nonnegative integer"),
        ("trade_count", -1, "nonnegative"),
        ("vwap", Decimal("0"), "positive"),
    ),
)
def test_market_bar_rejects_invalid_timestamps_and_nonnegative_fields(
    field_name: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _bar(**{field_name: value})


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"high": Decimal("99")}, "OHLC"),
        ({"low": Decimal("102")}, "OHLC"),
        ({"open": Decimal("0")}, "positive"),
        ({"close": Decimal("NaN")}, "finite"),
    ),
)
def test_market_bar_rejects_invalid_ohlc(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _bar(**changes)


def test_raw_observation_rejects_invalid_payload_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        RawBarObservationV1(bar=_bar(), raw_payload_sha256="not-a-digest")
