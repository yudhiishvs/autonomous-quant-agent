"""Alembic environment for the collector's isolated PostgreSQL schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.schema import CreateSchema

from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME, metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url").strip()
    value = configured or os.environ.get("APA_MARKET_DATA_MIGRATION_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError(
            "APA_MARKET_DATA_MIGRATION_DATABASE_URL is required for database migrations"
        )
    return normalize_postgres_url(value).render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=SCHEMA_NAME,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args=postgres_connect_args("adaptive-market-data-migrations", migration=True),
    )
    with engine.connect() as connection:
        connection.execute(CreateSchema(SCHEMA_NAME, if_not_exists=True))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=SCHEMA_NAME,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
