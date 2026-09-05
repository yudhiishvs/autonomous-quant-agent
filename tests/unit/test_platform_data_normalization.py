"""Canonical market-data normalization parity and rejection tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adaptive_trader.platform.data.normalization import (
    CanonicalBar,
    MarketDataNormalizationError,
    NormalizationPolicy,
    normalize_alpaca_bar,
    normalize_fixture_bar,
)

_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_RECEIVED = _START + timedelta(minutes=1, seconds=2)


def _policy() -> NormalizationPolicy:
    return NormalizationPolicy(
        collection_allowlist=("AAA", "BBB"),
        excluded_symbols=("CCC",),
    )


def _compact_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "T": "b",
        "S": "AAA",
        "t": "2026-07-06T13:30:00Z",
        "o": 100.0,
        "h": 101.5,
        "l": 99.75,
        "c": 101.0,
        "v": 1200,
        "n": 32,
        "vw": 100.625,
    }
    payload.update(overrides)
    return payload


def _verbose_payload() -> dict[str, object]:
    return {
        "symbol": "aaa",
        "timestamp": _START,
        "open": "100.000",
        "high": Decimal("101.5000"),
        "low": "99.750",
        "close": Decimal("101.00"),
        "volume": Decimal("1200"),
        "trade_count": Decimal("32"),
        "vwap": "100.6250",
        "provider": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "timeframe": "1Min",
        "schema_version": 1,
    }


def _canonical(**overrides: object) -> CanonicalBar:
    values: dict[str, object] = {
        "provider": "alpaca",
        "feed": "iex",
        "adjustment": "raw",
        "symbol": "AAA",
        "timeframe": "1Min",
        "source_mode": "external_provider",
        "interval_start_utc": _START,
        "interval_end_utc": _START + timedelta(minutes=1),
        "receipt_timestamp_utc": _RECEIVED,
        "provider_event_timestamp_utc": _START,
        "open": Decimal("100"),
        "high": Decimal("101.5"),
        "low": Decimal("99.75"),
        "close": Decimal("101"),
        "volume": Decimal("1200"),
        "trade_count": 32,
        "vwap": Decimal("100.625"),
        "schema_version": 1,
        "source_event_id": "provider_event_1",
        "quality_flags": ("complete",),
        "is_correction": False,
        "correction_of_source_event_id": None,
        "payload_hash": "",
    }
    values.update(overrides)
    return CanonicalBar(**values)  # type: ignore[arg-type]


def test_historical_and_realtime_shapes_share_byte_equivalent_normalization() -> None:
    realtime = normalize_alpaca_bar(
        _compact_payload(),
        policy=_policy(),
        receipt_timestamp_utc=_RECEIVED,
        quality_flags=("complete",),
    )
    historical = normalize_alpaca_bar(
        _verbose_payload(),
        policy=_policy(),
        receipt_timestamp_utc=_RECEIVED,
        quality_flags=("complete",),
    )

    assert historical == realtime
    assert historical.canonical_bytes == realtime.canonical_bytes
    assert historical.normalized_bytes == realtime.normalized_bytes
    assert historical.payload_hash == realtime.payload_hash
    assert historical.source_event_id == realtime.source_event_id


def test_canonical_bar_identity_and_payload_hash_have_independent_known_answers() -> None:
    bar = _canonical()

    assert bar.identity_key == (
        "alpaca",
        "iex",
        "raw",
        "AAA",
        "1Min",
        _START,
    )
    assert bar.identity_hash == "9a3e29bd2a6d4b59c5d2c2fe3ee46de623738c7f3a3b1c1485be502c384d1e90"
    assert bar.payload_hash == "845ef12379e23a5e0ba57236c160e2d4b87415def6e994281d1c86ac53082cb4"
    assert bar.canonical_bytes == (
        b'{"adjustment":"raw","close":"101","correction":{"correction_of_source_event_id"'
        b':null,"is_correction":false},"feed":"iex","high":"101.5","interval_end_utc":'
        b'"2026-07-06T13:31:00.000000Z","interval_start_utc":"2026-07-06T13:30:00.000000Z"'
        b',"low":"99.75","open":"100","payload_hash":"845ef12379e23a5e0ba57236c160e2d4b'
        b'87415def6e994281d1c86ac53082cb4","provider":"alpaca","provider_event_timestamp_utc"'
        b':"2026-07-06T13:30:00.000000Z","quality_flags":["complete"],"receipt_timestamp_utc"'
        b':"2026-07-06T13:31:02.000000Z","schema_version":1,"source_event_id":"provider_event_1"'
        b',"source_mode":"external_provider","symbol":"AAA","timeframe":"1Min","trade_count"'
        b':32,"volume":"1200","vwap"'
        b':"100.625"}'
    )


def test_receipt_provenance_does_not_turn_a_retry_into_a_correction() -> None:
    first = _canonical()
    retry = replace(
        first,
        receipt_timestamp_utc=first.receipt_timestamp_utc + timedelta(seconds=5),
    )

    assert retry.payload_hash == first.payload_hash
    assert retry.normalized_bytes == first.normalized_bytes
    assert retry.canonical_bytes != first.canonical_bytes


def test_canonical_contract_is_immutable_and_has_no_instance_dictionary() -> None:
    bar = _canonical()

    with pytest.raises(FrozenInstanceError):
        bar.close = Decimal("1")  # type: ignore[misc]
    assert not hasattr(bar, "__dict__")


def test_one_minute_execution_reference_requires_vwap() -> None:
    payload = _compact_payload()
    del payload["vw"]

    with pytest.raises(MarketDataNormalizationError, match="requires VWAP"):
        normalize_alpaca_bar(
            payload,
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
            execution_reference=True,
        )

    stored_only = normalize_alpaca_bar(
        payload,
        policy=_policy(),
        receipt_timestamp_utc=_RECEIVED,
    )
    assert stored_only.vwap is None


def test_offline_fixture_provenance_cannot_masquerade_as_alpaca() -> None:
    fixture_policy = NormalizationPolicy(
        collection_allowlist=("AAA", "BBB"),
        excluded_symbols=("CCC",),
        provider="fixture",
        source_mode="offline_fixture",
    )
    payload = _verbose_payload()
    payload["provider"] = "fixture"

    fixture = normalize_fixture_bar(
        payload,
        policy=fixture_policy,
        receipt_timestamp_utc=_RECEIVED,
        quality_flags=("complete",),
    )

    assert fixture.provider == "fixture"
    assert fixture.source_mode == "offline_fixture"
    assert fixture.has_promotable_provenance is False
    assert fixture.source_event_id.startswith("fixture_bar_")
    assert (
        fixture.payload_hash
        != normalize_alpaca_bar(
            _verbose_payload(),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
            quality_flags=("complete",),
        ).payload_hash
    )
    assert _canonical().has_promotable_provenance is True
    with pytest.raises(MarketDataNormalizationError, match="Alpaca normalization policy"):
        normalize_alpaca_bar(
            payload,
            policy=fixture_policy,
            receipt_timestamp_utc=_RECEIVED,
        )
    with pytest.raises(MarketDataNormalizationError, match="fixture normalization policy"):
        normalize_fixture_bar(
            _verbose_payload(),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"S": "DDD"}, "outside the collection allowlist"),
        ({"S": "CCC"}, "explicitly excluded"),
        ({"provider": "other"}, "provider does not match"),
        ({"feed": "sip"}, "feed does not match"),
        ({"adjustment": "all"}, "adjustment does not match"),
        ({"timeframe": "15Min"}, "timeframe does not match"),
        ({"schema_version": 2}, "schema version"),
    ],
)
def test_normalization_rejects_unconfigured_authority(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MarketDataNormalizationError, match=message):
        normalize_alpaca_bar(
            _compact_payload(**overrides),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("o", 0, "prices must be positive"),
        ("o", -1, "prices must be positive"),
        ("h", 99, "OHLC values are incoherent"),
        ("l", 102, "OHLC values are incoherent"),
        ("v", -1, "volume must be nonnegative"),
        ("v", "1.5", "whole number"),
        ("n", -1, "trade_count is invalid"),
        ("n", 1.5, "trade_count is invalid"),
        ("vw", -1, "VWAP must be nonnegative"),
        ("o", float("nan"), "must be finite"),
        ("c", float("inf"), "must be finite"),
        ("v", True, "must be numeric"),
    ],
)
def test_normalization_rejects_invalid_numeric_values(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(MarketDataNormalizationError, match=message):
        normalize_alpaca_bar(
            _compact_payload(**{field: value}),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 7, 6, 13, 30),
        datetime(2026, 7, 6, 9, 30, tzinfo=timezone(timedelta(hours=-4))),
        "2026-07-06T09:30:00-04:00",
        "2026-07-06T13:30:01Z",
    ],
)
def test_normalization_rejects_non_utc_or_unaligned_event_timestamps(timestamp: object) -> None:
    with pytest.raises(MarketDataNormalizationError):
        normalize_alpaca_bar(
            _compact_payload(t=timestamp),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )


@pytest.mark.parametrize(
    "receipt",
    [
        datetime(2026, 7, 6, 13, 31),
        datetime(2026, 7, 6, 9, 31, tzinfo=timezone(timedelta(hours=-4))),
        _START,
    ],
)
def test_normalization_rejects_invalid_receipt_timestamps(receipt: datetime) -> None:
    with pytest.raises(MarketDataNormalizationError):
        normalize_alpaca_bar(
            _compact_payload(),
            policy=_policy(),
            receipt_timestamp_utc=receipt,
        )


def test_normalization_rejects_alias_ambiguity_and_unknown_fields() -> None:
    with pytest.raises(MarketDataNormalizationError, match="ambiguous aliases"):
        normalize_alpaca_bar(
            _compact_payload(symbol="AAA"),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )
    with pytest.raises(MarketDataNormalizationError, match="unsupported fields"):
        normalize_alpaca_bar(
            _compact_payload(url="https://invalid.example"),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )


def test_updated_bar_requires_explicit_correction_metadata() -> None:
    with pytest.raises(MarketDataNormalizationError, match="requires correction metadata"):
        normalize_alpaca_bar(
            _compact_payload(T="u"),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
        )

    updated = normalize_alpaca_bar(
        _compact_payload(T="u"),
        policy=_policy(),
        receipt_timestamp_utc=_RECEIVED,
        is_correction=True,
        correction_of_source_event_id="provider_event_0",
    )
    historical = normalize_alpaca_bar(
        _verbose_payload(),
        policy=_policy(),
        receipt_timestamp_utc=_RECEIVED,
    )

    assert updated.source_event_id == historical.source_event_id
    assert updated.is_correction is True
    assert updated.correction_of_source_event_id == "provider_event_0"
    assert updated.payload_hash != historical.payload_hash


def test_correction_metadata_is_validated_on_the_immutable_contract() -> None:
    valid = _canonical()
    correction = replace(
        valid,
        source_event_id="provider_event_2",
        is_correction=True,
        correction_of_source_event_id="provider_event_1",
        payload_hash="",
    )

    assert correction.payload_hash != valid.payload_hash
    with pytest.raises(MarketDataNormalizationError, match="requires correction metadata"):
        replace(
            valid,
            correction_of_source_event_id="provider_event_0",
            payload_hash="",
        )
    with pytest.raises(MarketDataNormalizationError, match="cannot reference itself"):
        replace(
            valid,
            is_correction=True,
            correction_of_source_event_id=valid.source_event_id,
            payload_hash="",
        )


def test_payload_hash_mismatch_and_malformed_canonical_metadata_are_rejected() -> None:
    valid = _canonical()
    with pytest.raises(MarketDataNormalizationError, match="payload hash"):
        replace(valid, payload_hash="0" * 64)
    with pytest.raises(MarketDataNormalizationError, match="schema version"):
        replace(valid, schema_version=True, payload_hash="")
    with pytest.raises(MarketDataNormalizationError, match="quality flag"):
        replace(valid, quality_flags=("late", "complete"), payload_hash="")
    with pytest.raises(MarketDataNormalizationError, match="source event ID"):
        replace(valid, source_event_id="https://invalid.example", payload_hash="")


def test_canonical_interval_and_provider_timestamp_invariants_fail_closed() -> None:
    valid = _canonical()
    with pytest.raises(MarketDataNormalizationError, match="does not match"):
        replace(
            valid,
            interval_end_utc=valid.interval_end_utc + timedelta(minutes=1),
            payload_hash="",
        )
    with pytest.raises(MarketDataNormalizationError, match="follows its receipt"):
        replace(
            valid,
            provider_event_timestamp_utc=valid.receipt_timestamp_utc + timedelta(seconds=1),
            payload_hash="",
        )


def test_policy_requires_sorted_disjoint_exact_symbol_authority() -> None:
    with pytest.raises(MarketDataNormalizationError, match="sorted and unique"):
        NormalizationPolicy(collection_allowlist=("BBB", "AAA"), excluded_symbols=())
    with pytest.raises(MarketDataNormalizationError, match="disjoint"):
        NormalizationPolicy(collection_allowlist=("AAA",), excluded_symbols=("AAA",))
    with pytest.raises(MarketDataNormalizationError, match="unsupported"):
        NormalizationPolicy(
            collection_allowlist=("AAA",),
            excluded_symbols=(),
            feed="sip",
        )


def test_symbol_response_group_mismatch_is_rejected_without_aliasing() -> None:
    with pytest.raises(MarketDataNormalizationError, match="does not match"):
        normalize_alpaca_bar(
            _compact_payload(S="AAA"),
            policy=_policy(),
            receipt_timestamp_utc=_RECEIVED,
            symbol="BBB",
        )
