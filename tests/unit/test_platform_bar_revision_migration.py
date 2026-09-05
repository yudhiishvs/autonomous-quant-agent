"""Offline contracts for correction-safe bar revision history."""

from __future__ import annotations

import importlib
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import ModuleType
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import RowMapping

from adaptive_trader.platform.hashing import sha256_hex

_REVISION_MODULE = "migrations.versions.20260905_0003_bar_revision_history"


def _migration_module() -> ModuleType:
    return importlib.import_module(_REVISION_MODULE)


def _operations(output: io.StringIO) -> Operations:
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    return Operations(context)


def test_revision_follows_platform_foundation() -> None:
    migration = _migration_module()

    assert migration.revision == "20260905_0003"
    assert migration.down_revision == "20260905_0002"


def test_offline_upgrade_requires_empty_history_and_removes_history_wide_uniqueness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    monkeypatch.setattr(migration, "op", _operations(output))

    migration.upgrade()

    sql = " ".join(output.getvalue().split())
    assert "IF EXISTS (SELECT 1 FROM aqa.aqa_bar_events LIMIT 1)" in sql
    assert "requires online migration for existing bar events" in sql
    assert "ADD COLUMN lineage_hash VARCHAR(64)" in sql
    assert "ADD COLUMN normalized_payload_hash VARCHAR(64)" in sql
    assert "ALTER COLUMN normalized_payload_hash SET NOT NULL" in sql
    assert "content_hash <> normalized_payload_hash" in sql
    assert sql.endswith("ALTER TABLE aqa.aqa_bar_events DROP CONSTRAINT bar_identity_content;")


def test_frozen_v1_hash_has_known_answer_parity_with_the_canonical_boundary() -> None:
    migration = _migration_module()
    start_at = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
    end_at = start_at + timedelta(minutes=1)
    received_at = end_at + timedelta(seconds=1)
    identity_hash = "24ca4556fc7e4c42ee6df3326e28a7c6c304e2aa8957bb50cc85f86c35c31356"
    normalized_hash = "d908f11f8e12d5fede90dcfe10610c6a969acefcb964098854374fe4c2ffb949"
    event_id = "bar_event_642d6be06924b3dd80cc22fb732519d66ba8e8541886f128eefee69079748b1e"
    cases: tuple[tuple[object, str], ...] = (
        (
            ("fixture", "iex", "raw", "NVDA", "1Min", start_at),
            identity_hash,
        ),
        (
            (
                "canonical_bar_v1",
                1,
                identity_hash,
                end_at,
                end_at,
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.25"),
                Decimal("1000"),
                25,
                Decimal("10.20"),
                ("complete",),
            ),
            normalized_hash,
        ),
        (
            (f"bar_identity_{identity_hash}", 1, normalized_hash),
            event_id.removeprefix("bar_event_"),
        ),
        (
            {
                "bar_event_id": event_id,
                "bar_identity_id": f"bar_identity_{identity_hash}",
                "correction_of_event_id": None,
                "created_at": received_at,
                "normalized_payload_hash": normalized_hash,
                "received_at": received_at,
                "revision": 1,
                "schema": "bar_event_v1",
                "source": "fixture",
                "source_event_id": "delivery-one",
                "source_payload_hash": "a" * 64,
            },
            "87ca9dd1219d5b5f7b152dd644970e75da9f9a501d3000805eb9d71bd1a565ca",
        ),
    )

    for value, expected in cases:
        assert migration._sha256_hex_v1(value) == expected
        assert sha256_hex(value) == expected


@pytest.mark.parametrize(
    "unsupported",
    [
        True,
        1.0,
        ["list-not-used-by-v1-bar-hashes"],
        datetime(2026, 9, 5, 14, 30),
        Decimal("NaN"),
        {1: "non-string-key"},
    ],
)
def test_frozen_v1_hash_rejects_values_outside_its_closed_contract(
    unsupported: object,
) -> None:
    migration = _migration_module()

    with pytest.raises(RuntimeError, match="bar hash backfill"):
        migration._sha256_hex_v1(unsupported)


def test_online_hash_backfill_separates_normalized_identity_from_full_event_content() -> None:
    migration = _migration_module()
    start_at = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
    end_at = start_at + timedelta(minutes=1)
    received_at = end_at + timedelta(seconds=1)
    identity_hash = sha256_hex(("fixture", "iex", "raw", "NVDA", "1Min", start_at))
    identity_id = f"bar_identity_{identity_hash}"
    normalized_hash = sha256_hex(
        (
            "canonical_bar_v1",
            1,
            identity_hash,
            end_at,
            end_at,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.25"),
            Decimal("1000"),
            25,
            Decimal("10.20"),
            ("complete",),
        )
    )
    event_id = f"bar_event_{sha256_hex((identity_id, 1, normalized_hash))}"
    row = cast(
        RowMapping,
        {
            "bar_event_id": event_id,
            "bar_identity_id": identity_id,
            "revision": 1,
            "schema_version": 1,
            "provider_timestamp": end_at,
            "received_at": received_at,
            "open": Decimal("10"),
            "high": Decimal("11"),
            "low": Decimal("9"),
            "close": Decimal("10.25"),
            "volume": Decimal("1000"),
            "trade_count": 25,
            "vwap": Decimal("10.20"),
            "quality_flags": ["complete"],
            "source": "fixture",
            "source_event_id": "delivery-one",
            "source_payload_hash": "a" * 64,
            "lineage_hash": None,
            "correction_of_event_id": None,
            "content_hash": normalized_hash,
            "created_at": received_at,
            "provider": "fixture",
            "feed": "iex",
            "adjustment": "raw",
            "symbol": "NVDA",
            "timeframe": "1Min",
            "start_at": start_at,
            "end_at": end_at,
            "identity_content_hash": identity_hash,
            "identity_created_at": received_at,
        },
    )

    updates = migration._prepare_hash_updates((row,))

    assert len(updates) == 1
    assert updates[0].bar_event_id == event_id
    assert updates[0].normalized_payload_hash == normalized_hash
    assert updates[0].content_hash == sha256_hex(
        {
            "bar_event_id": event_id,
            "bar_identity_id": identity_id,
            "correction_of_event_id": None,
            "created_at": received_at,
            "normalized_payload_hash": normalized_hash,
            "received_at": received_at,
            "revision": 1,
            "schema": "bar_event_v1",
            "source": "fixture",
            "source_event_id": "delivery-one",
            "source_payload_hash": "a" * 64,
        }
    )
    assert updates[0].content_hash != normalized_hash

    database_timezone = ZoneInfo("America/New_York")
    localized_row = cast(
        RowMapping,
        {
            **dict(row),
            "start_at": start_at.astimezone(database_timezone),
            "end_at": end_at.astimezone(database_timezone),
            "provider_timestamp": end_at.astimezone(database_timezone),
            "received_at": received_at.astimezone(database_timezone),
            "created_at": received_at.astimezone(database_timezone),
            "identity_created_at": received_at.astimezone(database_timezone),
        },
    )
    assert migration._prepare_hash_updates((localized_row,)) == updates

    invalid_identity_origin = cast(
        RowMapping,
        {
            **dict(row),
            "identity_created_at": received_at + timedelta(seconds=1),
        },
    )
    with pytest.raises(RuntimeError, match="malformed durable state"):
        migration._prepare_hash_updates((invalid_identity_origin,))

    terminal = migration._EventHashUpdate(
        bar_event_id="bar_event_" + ("b" * 64),
        bar_identity_id=identity_id,
        revision=2,
        received_at=received_at + timedelta(seconds=1),
        normalized_payload_hash="c" * 64,
        content_hash="d" * 64,
    )
    valid_latest = cast(
        RowMapping,
        {
            "bar_identity_id": identity_id,
            "bar_event_id": terminal.bar_event_id,
            "revision": 2,
            "version": 2,
            "projected_at": terminal.received_at,
            "content_hash": terminal.normalized_payload_hash,
        },
    )
    assert migration._validate_latest_rows((valid_latest,), (updates[0], terminal)) == {
        updates[0].bar_event_id: updates[0],
        terminal.bar_event_id: terminal,
    }

    with pytest.raises(RuntimeError, match="incomplete latest projections"):
        migration._validate_latest_rows((), updates)

    nonterminal_latest = cast(
        RowMapping,
        {
            "bar_identity_id": identity_id,
            "bar_event_id": updates[0].bar_event_id,
            "revision": 1,
            "version": 1,
            "projected_at": updates[0].received_at,
            "content_hash": updates[0].normalized_payload_hash,
        },
    )
    with pytest.raises(RuntimeError, match="malformed latest projection"):
        migration._validate_latest_rows((nonterminal_latest,), (updates[0], terminal))

    invalid_flags = ["z", "a"]
    invalid_normalized_hash = sha256_hex(
        (
            "canonical_bar_v1",
            1,
            identity_hash,
            end_at,
            end_at,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.25"),
            Decimal("1000"),
            25,
            Decimal("10.20"),
            tuple(invalid_flags),
        )
    )
    invalid_row = cast(
        RowMapping,
        {
            **dict(row),
            "bar_event_id": (f"bar_event_{sha256_hex((identity_id, 1, invalid_normalized_hash))}"),
            "quality_flags": invalid_flags,
            "content_hash": invalid_normalized_hash,
        },
    )
    with pytest.raises(RuntimeError, match="malformed durable state"):
        migration._prepare_hash_updates((invalid_row,))


def test_online_backfill_locks_all_snapshot_tables_before_reading() -> None:
    migration = _migration_module()
    calls: list[str] = []

    class _Connection:
        def execute(self, statement: Any, *_args: object) -> object:
            calls.append(" ".join(str(statement).split()))
            return object()

    migration._lock_backfill_tables(cast(Any, _Connection()))

    assert calls == [
        "LOCK TABLE aqa.aqa_bar_identities, aqa.aqa_bar_events, "
        "aqa.aqa_bar_latest IN ACCESS EXCLUSIVE MODE"
    ]


def test_online_backfill_brackets_exact_migration_role_authority() -> None:
    migration = _migration_module()
    calls: list[str] = []

    class _Connection:
        def scalar(
            self,
            statement: Any,
            parameters: dict[str, str] | None = None,
        ) -> bool:
            calls.append(" ".join(str(statement).split()))
            return parameters is None

        def execute(self, statement: Any) -> object:
            calls.append(" ".join(str(statement).split()))
            return object()

    connection = cast(Any, _Connection())

    absent = migration._grant_backfill_authority(connection)
    assert absent == migration._BACKFILL_PRIVILEGES
    assert absent is not None
    migration._restore_backfill_authority(connection, absent)

    assert calls[:9] == [
        "SELECT current_user = 'aqa_migrate'",
        *(
            "SELECT has_table_privilege('aqa_migrate', :relation, :privilege)"
            for _privilege, _relation in migration._BACKFILL_PRIVILEGES
        ),
        "GRANT SELECT ON TABLE aqa.aqa_bar_identities, aqa.aqa_bar_events, "
        "aqa.aqa_bar_latest TO aqa_migrate",
        "GRANT UPDATE ON TABLE aqa.aqa_bar_identities, aqa.aqa_bar_events, "
        "aqa.aqa_bar_latest TO aqa_migrate",
    ]
    assert calls[9:] == [
        f"REVOKE {privilege} ON TABLE {relation} FROM aqa_migrate"
        for privilege, relation in migration._BACKFILL_PRIVILEGES
    ]


def test_online_backfill_preserves_preexisting_migration_role_authority() -> None:
    migration = _migration_module()
    executed: list[str] = []

    class _Connection:
        def scalar(
            self,
            _statement: Any,
            _parameters: dict[str, str] | None = None,
        ) -> bool:
            return True

        def execute(self, statement: Any) -> object:
            executed.append(" ".join(str(statement).split()))
            return object()

    connection = cast(Any, _Connection())
    absent = migration._grant_backfill_authority(connection)
    assert absent == ()
    migration._restore_backfill_authority(connection, absent)

    assert executed == [
        "GRANT SELECT ON TABLE aqa.aqa_bar_identities, aqa.aqa_bar_events, "
        "aqa.aqa_bar_latest TO aqa_migrate",
        "GRANT UPDATE ON TABLE aqa.aqa_bar_identities, aqa.aqa_bar_events, "
        "aqa.aqa_bar_latest TO aqa_migrate",
    ]


def test_legacy_owner_backfill_does_not_change_migration_role_authority() -> None:
    migration = _migration_module()
    calls: list[str] = []

    class _Connection:
        def scalar(
            self,
            statement: Any,
            _parameters: dict[str, str] | None = None,
        ) -> bool:
            calls.append(" ".join(str(statement).split()))
            return False

        def execute(self, statement: Any) -> object:
            raise AssertionError(f"unexpected authority change: {statement}")

    assert migration._grant_backfill_authority(cast(Any, _Connection())) is None
    assert calls == ["SELECT current_user = 'aqa_migrate'"]


def test_online_backfill_fails_closed_for_an_unknown_migration_role_result() -> None:
    migration = _migration_module()

    class _Connection:
        def scalar(
            self,
            _statement: Any,
            _parameters: dict[str, str] | None = None,
        ) -> None:
            return None

        def execute(self, statement: Any) -> object:
            raise AssertionError(f"unexpected authority change: {statement}")

    with pytest.raises(RuntimeError, match="could not determine its migration role"):
        migration._grant_backfill_authority(cast(Any, _Connection()))


@pytest.mark.parametrize("absent_backfill_privileges", [(), None])
def test_online_upgrade_orders_and_conditionally_restores_backfill_authority(
    monkeypatch: pytest.MonkeyPatch,
    absent_backfill_privileges: tuple[tuple[str, str], ...] | None,
) -> None:
    migration = _migration_module()
    calls: list[str] = []
    connection = object()

    class _Context:
        as_sql = False

    class _Operations:
        def get_context(self) -> _Context:
            return _Context()

        def get_bind(self) -> object:
            calls.append("bind")
            return connection

        def add_column(self, *_args: object, **_kwargs: object) -> None:
            calls.append("add-column")

        def alter_column(self, *_args: object, **_kwargs: object) -> None:
            calls.append("alter-column")

        def create_check_constraint(self, *_args: object, **_kwargs: object) -> None:
            calls.append("check")

        def drop_constraint(self, *_args: object, **_kwargs: object) -> None:
            calls.append("drop-constraint")

        def f(self, name: str) -> str:
            return name

    monkeypatch.setattr(migration, "op", _Operations())
    monkeypatch.setattr(
        migration,
        "_grant_backfill_authority",
        lambda bound: (
            calls.append("grant") or absent_backfill_privileges if bound is connection else None
        ),
    )
    monkeypatch.setattr(
        migration,
        "_lock_backfill_tables",
        lambda bound: calls.append("lock") if bound is connection else None,
    )
    monkeypatch.setattr(
        migration,
        "_backfill_event_hashes",
        lambda bound: calls.append("backfill") if bound is connection else None,
    )
    monkeypatch.setattr(
        migration,
        "_restore_backfill_authority",
        lambda bound, absent: (
            calls.append("restore")
            if bound is connection and absent is absent_backfill_privileges
            else None
        ),
    )

    migration.upgrade()

    expected = [
        "bind",
        "grant",
        "lock",
        "add-column",
        "add-column",
        "backfill",
        "alter-column",
        "check",
        "check",
        "check",
        "drop-constraint",
    ]
    if absent_backfill_privileges is not None:
        expected.append("restore")
    assert calls == expected


def test_online_backfill_rejects_an_identity_without_revision_history() -> None:
    migration = _migration_module()

    class _Connection:
        def scalar(self, _statement: Any) -> bool:
            return True

    with pytest.raises(RuntimeError, match="identity without revision history"):
        migration._reject_orphan_identities(cast(Any, _Connection()))


def test_downgrade_refuses_before_rendering_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    output = io.StringIO()
    monkeypatch.setattr(migration, "op", _operations(output))

    with pytest.raises(RuntimeError, match="Destructive downgrade"):
        migration.downgrade()

    assert output.getvalue() == ""
