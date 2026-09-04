"""Explicit Alembic lifecycle helpers for the market-data database."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME


def _alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        normalize_postgres_url(database_url)
        .render_as_string(hide_password=False)
        .replace("%", "%%"),
    )
    return config


def upgrade_database(database_url: str) -> None:
    """Apply every checked-in migration to ``database_url``."""

    command.upgrade(_alembic_config(database_url), "head")


def database_revision(database_url: str) -> tuple[str | None, str]:
    """Return the current and expected migration revisions without changing schema."""

    config = _alembic_config(database_url)
    script = ScriptDirectory.from_config(config)
    expected = script.get_current_head()
    if expected is None:
        raise RuntimeError("No market-data migration head exists")
    engine = create_engine(
        normalize_postgres_url(database_url),
        future=True,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=postgres_connect_args("adaptive-market-data-schema-check"),
    )
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"version_table_schema": SCHEMA_NAME},
            )
            current = context.get_current_revision()
    finally:
        engine.dispose()
    return current, expected


def require_database_at_head(database_url: str) -> None:
    """Fail closed when deployment migrations do not match the application."""

    current, expected = database_revision(database_url)
    if current != expected:
        raise RuntimeError(
            f"Market-data database revision is {current or 'unversioned'}; expected {expected}"
        )
