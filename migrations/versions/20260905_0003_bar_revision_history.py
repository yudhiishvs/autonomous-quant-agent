"""Separate normalized bar idempotency from immutable event integrity.

Revision ID: 20260905_0003
Revises: 20260905_0002
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, NamedTuple, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Connection
from sqlalchemy.engine import RowMapping

revision: str = "20260905_0003"
down_revision: str | None = "20260905_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HASH = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]*$", flags=re.ASCII)
_QUALITY_FLAG = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", flags=re.ASCII)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$", flags=re.ASCII)
_TIMEFRAME_DURATIONS = {"1Min": timedelta(minutes=1), "15Min": timedelta(minutes=15)}
_MAX_SIGNED_64_BIT_INTEGER = (1 << 63) - 1
_MIN_SIGNED_64_BIT_INTEGER = -(1 << 63)
_MAX_HASH_DEPTH = 32
_MAX_HASH_NODES = 4096
_MAX_HASH_STRING_BYTES = 65_536
_MAX_HASH_TEXT_BYTES = 1_048_576
_MAX_HASH_DECIMAL_CHARACTERS = 1024
_MAX_HASH_OUTPUT_BYTES = 8_388_608
_OFFLINE_EMPTY_GUARD = sa.text(
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM aqa.aqa_bar_events LIMIT 1) THEN
            RAISE EXCEPTION
                'revision 20260905_0003 requires online migration for existing bar events';
        END IF;
    END
    $$
    """
)
_ONLINE_BACKFILL_LOCK = sa.text(
    """
    LOCK TABLE
        aqa.aqa_bar_identities,
        aqa.aqa_bar_events,
        aqa.aqa_bar_latest
    IN ACCESS EXCLUSIVE MODE
    """
)
_MIGRATION_ROLE_IS_ACTIVE = sa.text("SELECT current_user = 'aqa_migrate'")
_HAS_BACKFILL_PRIVILEGE = sa.text(
    "SELECT has_table_privilege('aqa_migrate', :relation, :privilege)"
)
_BACKFILL_RELATIONS = (
    "aqa.aqa_bar_identities",
    "aqa.aqa_bar_events",
    "aqa.aqa_bar_latest",
)
_BACKFILL_PRIVILEGES = tuple(
    (privilege, relation) for privilege in ("SELECT", "UPDATE") for relation in _BACKFILL_RELATIONS
)
_BACKFILL_GRANTS = (
    sa.text(
        """
        GRANT SELECT ON TABLE
            aqa.aqa_bar_identities,
            aqa.aqa_bar_events,
            aqa.aqa_bar_latest
        TO aqa_migrate
        """
    ),
    sa.text(
        """
        GRANT UPDATE ON TABLE
            aqa.aqa_bar_identities,
            aqa.aqa_bar_events,
            aqa.aqa_bar_latest
        TO aqa_migrate
        """
    ),
)


class _EventHashUpdate(NamedTuple):
    bar_event_id: str
    bar_identity_id: str
    revision: int
    received_at: datetime
    normalized_payload_hash: str
    content_hash: str


class _CanonicalTimes(NamedTuple):
    start_at: datetime
    end_at: datetime
    received_at: datetime
    provider_timestamp: datetime | None


class _FrozenHashBudget:
    __slots__ = ("nodes", "text_bytes")

    def __init__(self) -> None:
        self.nodes = 0
        self.text_bytes = 0

    def consume_node(self, *, depth: int) -> None:
        if depth > _MAX_HASH_DEPTH:
            raise RuntimeError("bar hash backfill exceeded its canonical nesting limit")
        self.nodes += 1
        if self.nodes > _MAX_HASH_NODES:
            raise RuntimeError("bar hash backfill exceeded its canonical node limit")

    def consume_text(self, size: int) -> None:
        self.text_bytes += size
        if self.text_bytes > _MAX_HASH_TEXT_BYTES:
            raise RuntimeError("bar hash backfill exceeded its canonical text limit")


def _sha256_hex_v1(value: object) -> str:
    """Hash the exact closed canonical value set used by the legacy bar schema."""

    normalized = _normalize_hash_value_v1(
        value,
        depth=0,
        active_containers=set(),
        budget=_FrozenHashBudget(),
    )
    try:
        encoded = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError):
        raise RuntimeError("bar hash backfill could not render canonical input") from None
    if len(encoded) > _MAX_HASH_OUTPUT_BYTES:
        raise RuntimeError("bar hash backfill exceeded its canonical output limit")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_hash_value_v1(
    value: object,
    *,
    depth: int,
    active_containers: set[int],
    budget: _FrozenHashBudget,
) -> object:
    budget.consume_node(depth=depth)
    value_type = type(value)
    if value is None:
        return None
    if value_type is int:
        integer = cast(int, value)
        if not _MIN_SIGNED_64_BIT_INTEGER <= integer <= _MAX_SIGNED_64_BIT_INTEGER:
            raise RuntimeError("bar hash backfill found an out-of-range canonical integer")
        return integer
    if value_type is str:
        text = cast(str, value)
        _consume_hash_text_v1(text, budget=budget)
        return text
    if value_type is Decimal:
        rendered = _decimal_hash_text_v1(cast(Decimal, value))
        budget.consume_text(len(rendered))
        return rendered
    if value_type is datetime:
        instant = cast(datetime, value)
        if instant.tzinfo is None or type(instant.tzinfo) is not timezone:
            raise RuntimeError("bar hash backfill found an unsupported canonical datetime")
        try:
            rendered = (
                instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
        except (OverflowError, ValueError):
            raise RuntimeError("bar hash backfill found an invalid canonical datetime") from None
        budget.consume_text(len(rendered))
        return rendered
    if value_type is tuple:
        identity = id(value)
        if identity in active_containers:
            raise RuntimeError("bar hash backfill found a canonical container cycle")
        active_containers.add(identity)
        try:
            return [
                _normalize_hash_value_v1(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                    budget=budget,
                )
                for item in cast(tuple[object, ...], value)
            ]
        finally:
            active_containers.remove(identity)
    if value_type is dict:
        identity = id(value)
        if identity in active_containers:
            raise RuntimeError("bar hash backfill found a canonical container cycle")
        active_containers.add(identity)
        try:
            mapping = cast(dict[object, object], value)
            if any(type(key) is not str for key in mapping):
                raise RuntimeError("bar hash backfill found an unsupported canonical mapping key")
            string_mapping = cast(dict[str, object], mapping)
            for key in string_mapping:
                _consume_hash_text_v1(key, budget=budget)
            return {
                key: _normalize_hash_value_v1(
                    string_mapping[key],
                    depth=depth + 1,
                    active_containers=active_containers,
                    budget=budget,
                )
                for key in sorted(string_mapping)
            }
        finally:
            active_containers.remove(identity)
    raise RuntimeError("bar hash backfill found an unsupported canonical value")


def _consume_hash_text_v1(value: str, *, budget: _FrozenHashBudget) -> None:
    if len(value) > _MAX_HASH_STRING_BYTES:
        raise RuntimeError("bar hash backfill exceeded its canonical string limit")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise RuntimeError("bar hash backfill found invalid canonical text") from None
    if len(encoded) > _MAX_HASH_STRING_BYTES:
        raise RuntimeError("bar hash backfill exceeded its canonical string limit")
    budget.consume_text(len(encoded))


def _decimal_hash_text_v1(value: Decimal) -> str:
    if not value.is_finite():
        raise RuntimeError("bar hash backfill found a nonfinite canonical decimal")
    if value.is_zero():
        return "0"
    sign, digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise RuntimeError("bar hash backfill found an invalid canonical decimal")
    digit_count = max(1, len(digits))
    if raw_exponent >= 0:
        maximum_characters = digit_count + raw_exponent
    elif digit_count + raw_exponent > 0:
        maximum_characters = digit_count + 1
    else:
        maximum_characters = 2 - raw_exponent
    maximum_characters += sign
    if maximum_characters > _MAX_HASH_DECIMAL_CHARACTERS:
        raise RuntimeError("bar hash backfill exceeded its canonical decimal limit")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered


def _event_rows(connection: Connection) -> Sequence[RowMapping]:
    return (
        connection.execute(
            sa.text(
                """
            SELECT
                event.bar_event_id,
                event.bar_identity_id,
                event.revision,
                event.schema_version,
                event.provider_timestamp,
                event.received_at,
                event.open,
                event.high,
                event.low,
                event.close,
                event.volume,
                event.trade_count,
                event.vwap,
                event.quality_flags,
                event.source,
                event.source_event_id,
                event.source_payload_hash,
                event.lineage_hash,
                event.correction_of_event_id,
                event.content_hash,
                event.created_at,
                identity.provider,
                identity.feed,
                identity.adjustment,
                identity.symbol,
                identity.timeframe,
                identity.start_at,
                identity.end_at,
                identity.content_hash AS identity_content_hash,
                identity.created_at AS identity_created_at
            FROM aqa.aqa_bar_events AS event
            JOIN aqa.aqa_bar_identities AS identity
              ON identity.bar_identity_id = event.bar_identity_id
            ORDER BY event.bar_identity_id, event.revision
            """
            )
        )
        .mappings()
        .all()
    )


def _reject_orphan_identities(connection: Connection) -> None:
    orphan_exists = connection.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM aqa.aqa_bar_identities AS identity
                LEFT JOIN aqa.aqa_bar_events AS event
                  ON event.bar_identity_id = identity.bar_identity_id
                WHERE event.bar_event_id IS NULL
            )
            """
        )
    )
    if orphan_exists is not False:
        raise RuntimeError("bar hash backfill found an identity without revision history")


def _require_hash(value: object) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise RuntimeError("bar hash backfill found malformed durable state")
    return value


def _require_row_value(row: RowMapping, name: str, expected_type: type[Any]) -> Any:
    value = row[name]
    if type(value) is not expected_type:
        raise RuntimeError("bar hash backfill found malformed durable state")
    return value


def _require_utc(value: object) -> datetime:
    if type(value) is not datetime:
        raise RuntimeError("bar hash backfill found malformed durable state")
    try:
        offset = value.utcoffset()
    except Exception:
        raise RuntimeError("bar hash backfill found malformed durable state") from None
    if offset is None:
        raise RuntimeError("bar hash backfill found malformed durable state")
    return value.astimezone(UTC)


def _require_decimal(value: object, *, scale: int) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise RuntimeError("bar hash backfill found malformed durable state")
    _, raw_digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise RuntimeError("bar hash backfill found malformed durable state")
    digits = list(raw_digits)
    exponent = raw_exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    fractional_places = max(-exponent, 0) if digits else 0
    integral_places = max(len(digits) + exponent, 0) if digits else 0
    if fractional_places > scale or integral_places + fractional_places > 38:
        raise RuntimeError("bar hash backfill found malformed durable state")
    return value


def _validate_canonical_v1_row(row: RowMapping) -> _CanonicalTimes:
    for name, maximum_length in (("provider", 32), ("feed", 16), ("adjustment", 16)):
        value = row[name]
        if (
            type(value) is not str
            or len(value) > maximum_length
            or _LOWER_TOKEN.fullmatch(value) is None
        ):
            raise RuntimeError("bar hash backfill found malformed durable state")
    symbol = row["symbol"]
    timeframe = row["timeframe"]
    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        raise RuntimeError("bar hash backfill found malformed durable state")
    duration = _TIMEFRAME_DURATIONS.get(timeframe)
    if duration is None:
        raise RuntimeError("bar hash backfill found malformed durable state")
    start_at = _require_utc(row["start_at"])
    end_at = _require_utc(row["end_at"])
    received_at = _require_utc(row["received_at"])
    provider_timestamp = row["provider_timestamp"]
    if end_at - start_at != duration or received_at < end_at:
        raise RuntimeError("bar hash backfill found malformed durable state")
    normalized_provider_timestamp = (
        None if provider_timestamp is None else _require_utc(provider_timestamp)
    )

    prices = tuple(
        _require_decimal(row[name], scale=18) for name in ("open", "high", "low", "close")
    )
    volume = _require_decimal(row["volume"], scale=0)
    vwap = row["vwap"]
    if (
        any(value <= 0 for value in prices)
        or prices[1] < max(prices)
        or prices[2] > min(prices)
        or volume < 0
    ):
        raise RuntimeError("bar hash backfill found malformed durable state")
    if vwap is not None and _require_decimal(vwap, scale=18) <= 0:
        raise RuntimeError("bar hash backfill found malformed durable state")
    trade_count = row["trade_count"]
    if trade_count is not None and (
        type(trade_count) is not int or not 0 <= trade_count <= _MAX_SIGNED_64_BIT_INTEGER
    ):
        raise RuntimeError("bar hash backfill found malformed durable state")
    if row["schema_version"] != 1:
        raise RuntimeError("bar hash backfill found malformed durable state")

    raw_flags = row["quality_flags"]
    if (
        type(raw_flags) is not list
        or len(raw_flags) > 64
        or any(type(flag) is not str or _QUALITY_FLAG.fullmatch(flag) is None for flag in raw_flags)
        or sorted(set(raw_flags)) != raw_flags
    ):
        raise RuntimeError("bar hash backfill found malformed durable state")
    source = row["source"]
    if type(source) is not str or len(source) > 32 or _LOWER_TOKEN.fullmatch(source) is None:
        raise RuntimeError("bar hash backfill found malformed durable state")
    _require_hash(row["source_payload_hash"])
    lineage_hash = row["lineage_hash"]
    if lineage_hash is not None:
        _require_hash(lineage_hash)
    source_event_id = row["source_event_id"]
    if source_event_id is not None and (
        type(source_event_id) is not str
        or not source_event_id
        or len(source_event_id) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in source_event_id)
    ):
        raise RuntimeError("bar hash backfill found malformed durable state")
    return _CanonicalTimes(
        start_at=start_at,
        end_at=end_at,
        received_at=received_at,
        provider_timestamp=normalized_provider_timestamp,
    )


def _normalized_payload_hash(row: RowMapping) -> str:
    times = _validate_canonical_v1_row(row)
    identity_hash = _sha256_hex_v1(
        (
            row["provider"],
            row["feed"],
            row["adjustment"],
            row["symbol"],
            row["timeframe"],
            times.start_at,
        )
    )
    if (
        row["bar_identity_id"] != f"bar_identity_{identity_hash}"
        or row["identity_content_hash"] != identity_hash
    ):
        raise RuntimeError("bar hash backfill found malformed durable state")
    raw_flags = row["quality_flags"]
    lineage_hash = row["lineage_hash"]
    hash_input: tuple[Any, ...] = (
        "canonical_bar_v1",
        row["schema_version"],
        identity_hash,
        times.end_at,
        times.provider_timestamp,
        row["open"],
        row["high"],
        row["low"],
        row["close"],
        row["volume"],
        row["trade_count"],
        row["vwap"],
        tuple(raw_flags),
    )
    if lineage_hash is not None:
        hash_input = (*hash_input, "lineage", lineage_hash)
    return _require_hash(_sha256_hex_v1(hash_input))


def _prepare_hash_updates(rows: Sequence[RowMapping]) -> tuple[_EventHashUpdate, ...]:
    updates: list[_EventHashUpdate] = []
    prior_identity_id: str | None = None
    prior_event_id: str | None = None
    prior_normalized_hash: str | None = None
    prior_received_at: datetime | None = None
    expected_revision = 0
    for row in rows:
        identity_id = _require_row_value(row, "bar_identity_id", str)
        event_id = _require_row_value(row, "bar_event_id", str)
        revision_number = _require_row_value(row, "revision", int)
        received_at = _require_utc(row["received_at"])
        created_at = _require_utc(row["created_at"])
        identity_created_at = _require_utc(row["identity_created_at"])
        prior_hash = _require_hash(row["content_hash"])
        source = _require_row_value(row, "source", str)
        source_payload_hash = _require_hash(row["source_payload_hash"])
        source_event_id = row["source_event_id"]
        correction_of_event_id = row["correction_of_event_id"]
        if source_event_id is not None and type(source_event_id) is not str:
            raise RuntimeError("bar hash backfill found malformed durable state")
        if correction_of_event_id is not None and type(correction_of_event_id) is not str:
            raise RuntimeError("bar hash backfill found malformed durable state")
        if identity_id != prior_identity_id:
            expected_revision = 1
            prior_event_id = None
            prior_normalized_hash = None
            prior_received_at = None
        else:
            expected_revision += 1
        normalized_hash = _normalized_payload_hash(row)
        expected_event_id = (
            f"bar_event_{_sha256_hex_v1((identity_id, revision_number, normalized_hash))}"
        )
        if (
            revision_number != expected_revision
            or event_id != expected_event_id
            or prior_hash != normalized_hash
            or correction_of_event_id != prior_event_id
            or created_at != received_at
            or (expected_revision == 1 and identity_created_at != received_at)
            or normalized_hash == prior_normalized_hash
            or (prior_received_at is not None and received_at < prior_received_at)
        ):
            raise RuntimeError("bar hash backfill found malformed durable state")
        content_hash = _sha256_hex_v1(
            {
                "bar_event_id": event_id,
                "bar_identity_id": identity_id,
                "correction_of_event_id": correction_of_event_id,
                "created_at": received_at,
                "normalized_payload_hash": normalized_hash,
                "received_at": received_at,
                "revision": revision_number,
                "schema": "bar_event_v1",
                "source": source,
                "source_event_id": source_event_id,
                "source_payload_hash": source_payload_hash,
            }
        )
        updates.append(
            _EventHashUpdate(
                bar_event_id=event_id,
                bar_identity_id=identity_id,
                revision=revision_number,
                received_at=received_at,
                normalized_payload_hash=prior_hash,
                content_hash=content_hash,
            )
        )
        prior_identity_id = identity_id
        prior_event_id = event_id
        prior_normalized_hash = normalized_hash
        prior_received_at = received_at
    return tuple(updates)


def _validate_latest_rows(
    rows: Sequence[RowMapping],
    updates: Sequence[_EventHashUpdate],
) -> dict[str, _EventHashUpdate]:
    terminal_by_identity = {update.bar_identity_id: update for update in updates}
    updates_by_event = {update.bar_event_id: update for update in updates}
    observed_identities: set[str] = set()
    for row in rows:
        identity_id = row["bar_identity_id"]
        event_id = row["bar_event_id"]
        update = updates_by_event.get(event_id)
        if (
            type(identity_id) is not str
            or identity_id in observed_identities
            or update is None
            or terminal_by_identity.get(identity_id) != update
            or row["revision"] != update.revision
            or row["version"] != update.revision
            or _require_utc(row["projected_at"]) != update.received_at
            or row["content_hash"] != update.normalized_payload_hash
        ):
            raise RuntimeError("bar hash backfill found malformed latest projection")
        observed_identities.add(identity_id)
    if observed_identities != set(terminal_by_identity):
        raise RuntimeError("bar hash backfill found incomplete latest projections")
    return updates_by_event


def _lock_backfill_tables(connection: Connection) -> None:
    # Acquire all participating tables in writer-compatible order before the first
    # DDL or read. This prevents both snapshot races and cross-table DDL deadlocks.
    connection.execute(_ONLINE_BACKFILL_LOCK)


def _grant_backfill_authority(connection: Connection) -> tuple[tuple[str, str], ...] | None:
    migration_role_is_active = connection.scalar(_MIGRATION_ROLE_IS_ACTIVE)
    if migration_role_is_active is False:
        return None
    if migration_role_is_active is not True:
        raise RuntimeError("bar hash backfill could not determine its migration role")
    absent_privileges: list[tuple[str, str]] = []
    for privilege, relation in _BACKFILL_PRIVILEGES:
        present = connection.scalar(
            _HAS_BACKFILL_PRIVILEGE,
            {"relation": relation, "privilege": privilege},
        )
        if present is False:
            absent_privileges.append((privilege, relation))
        elif present is not True:
            raise RuntimeError("bar hash backfill could not inspect its prior authority")
    for statement in _BACKFILL_GRANTS:
        connection.execute(statement)
    return tuple(absent_privileges)


def _restore_backfill_authority(
    connection: Connection,
    absent_privileges: tuple[tuple[str, str], ...],
) -> None:
    for privilege, relation in absent_privileges:
        if (privilege, relation) not in _BACKFILL_PRIVILEGES:
            raise RuntimeError("bar hash backfill prior authority is invalid")
        connection.execute(sa.text(f"REVOKE {privilege} ON TABLE {relation} FROM aqa_migrate"))


def _backfill_event_hashes(connection: Connection) -> None:
    _reject_orphan_identities(connection)
    updates = _prepare_hash_updates(_event_rows(connection))
    latest_rows = (
        connection.execute(
            sa.text(
                """
            SELECT
                bar_identity_id,
                bar_event_id,
                revision,
                content_hash,
                version,
                projected_at
            FROM aqa.aqa_bar_latest
            """
            )
        )
        .mappings()
        .all()
    )
    updates_by_event = _validate_latest_rows(latest_rows, updates)
    for update in updates:
        result = connection.execute(
            sa.text(
                """
                UPDATE aqa.aqa_bar_events
                SET normalized_payload_hash = :normalized_payload_hash,
                    content_hash = :content_hash
                WHERE bar_event_id = :bar_event_id
                  AND content_hash = :normalized_payload_hash
                  AND normalized_payload_hash IS NULL
                """
            ),
            {
                "bar_event_id": update.bar_event_id,
                "normalized_payload_hash": update.normalized_payload_hash,
                "content_hash": update.content_hash,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError("bar hash backfill lost its event concurrency fence")
    for row in latest_rows:
        update = updates_by_event[row["bar_event_id"]]
        result = connection.execute(
            sa.text(
                """
                UPDATE aqa.aqa_bar_latest
                SET content_hash = :content_hash
                WHERE bar_identity_id = :bar_identity_id
                  AND bar_event_id = :bar_event_id
                  AND revision = :revision
                  AND content_hash = :prior_hash
                """
            ),
            {
                "bar_identity_id": update.bar_identity_id,
                "bar_event_id": update.bar_event_id,
                "revision": update.revision,
                "prior_hash": update.normalized_payload_hash,
                "content_hash": update.content_hash,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError("bar hash backfill lost its projection concurrency fence")


def upgrade() -> None:
    """Separate normalized idempotency from full immutable event hashing."""

    context = op.get_context()
    if context.as_sql:
        op.execute(_OFFLINE_EMPTY_GUARD)
        connection = None
        absent_backfill_privileges = None
    else:
        connection = op.get_bind()
        absent_backfill_privileges = _grant_backfill_authority(connection)
        _lock_backfill_tables(connection)

    op.add_column(
        "aqa_bar_events",
        sa.Column("lineage_hash", sa.String(length=64), nullable=True),
        schema="aqa",
    )
    op.add_column(
        "aqa_bar_events",
        sa.Column("normalized_payload_hash", sa.String(length=64), nullable=True),
        schema="aqa",
    )

    if connection is not None:
        _backfill_event_hashes(connection)

    op.alter_column(
        "aqa_bar_events",
        "normalized_payload_hash",
        existing_type=sa.String(length=64),
        nullable=False,
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_normalized_payload_hash_sha256_length"),
        "aqa_bar_events",
        "length(normalized_payload_hash) = 64",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_lineage_hash_sha256_length"),
        "aqa_bar_events",
        "lineage_hash IS NULL OR length(lineage_hash) = 64",
        schema="aqa",
    )
    op.create_check_constraint(
        op.f("ck_aqa_bar_events_event_hash_domain_separation"),
        "aqa_bar_events",
        "content_hash <> normalized_payload_hash",
        schema="aqa",
    )

    op.drop_constraint(
        "bar_identity_content",
        "aqa_bar_events",
        schema="aqa",
        type_="unique",
    )
    if connection is not None and absent_backfill_privileges is not None:
        _restore_backfill_authority(connection, absent_backfill_privileges)


def downgrade() -> None:
    """Refuse a downgrade that could reject already valid correction history."""

    raise RuntimeError("Destructive downgrade of bar revision history is not supported")
