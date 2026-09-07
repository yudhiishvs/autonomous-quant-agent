"""Keep PostgreSQL drift checks aligned with explicitly dialect-scoped metadata."""

from __future__ import annotations

from sqlalchemy.schema import SchemaItem


def include_postgres_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    """Exclude only metadata DDL explicitly reserved for another database dialect.

    Reflected PostgreSQL objects always participate, so unexpected deployed constraints
    cannot disappear from the drift report. SQLite's approximate NUMERIC sign check
    supplements the exact PostgreSQL multiplication constraint only in isolated tests.
    """

    del name, type_, compare_to
    return reflected or "postgresql" in obj.info.get("dialects", ("postgresql",))
