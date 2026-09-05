"""Alembic environment for the additive operational PostgreSQL schemas."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool, text
from sqlalchemy.schema import CreateSchema

from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as VERSION_TABLE_SCHEMA
from adaptive_trader.collection.schema import metadata as collection_metadata
from adaptive_trader.platform.storage.migration_roles import (
    activate_platform_migration_role,
    migration_role_revision_sets,
    restore_referential_integrity_owner_privileges,
)
from adaptive_trader.platform.storage.tables import metadata as platform_metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = (collection_metadata, platform_metadata)

_GOVERNED_VERSION_TABLE_DDL = text(
    """
    CREATE TABLE IF NOT EXISTS market_data.alembic_version (
        version_num VARCHAR(32) NOT NULL,
        CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
    )
    """
)
_GOVERNED_VERSION_TABLE_GRANT = text(
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE market_data.alembic_version TO aqa_migrate
    """
)
_CURRENT_DATABASE_REVISION = text("SELECT version_num FROM market_data.alembic_version")
_GOVERNED_BUSINESS_DML_REVOKE = text(
    """
    DO $aqa_migration_dml$
    BEGIN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE
        ON ALL TABLES IN SCHEMA market_data FROM aqa_migrate;
        IF to_regnamespace('aqa') IS NOT NULL THEN
            REVOKE INSERT, UPDATE, DELETE, TRUNCATE
            ON ALL TABLES IN SCHEMA aqa FROM aqa_migrate;
        END IF;
    END
    $aqa_migration_dml$
    """
)
_PRE_GOVERNANCE_MIGRATION_DML_GRANT = text(
    """
    DO $aqa_pre_governance_migration_dml$
    BEGIN
        IF to_regnamespace('market_data') IS NOT NULL THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA market_data TO aqa_migrate;
        END IF;
        IF to_regnamespace('aqa') IS NOT NULL THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA aqa TO aqa_migrate;
        END IF;
    END
    $aqa_pre_governance_migration_dml$
    """
)


def _database_url() -> str:
    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    value = configured or os.environ.get("APA_MARKET_DATA_MIGRATION_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError(
            "APA_MARKET_DATA_MIGRATION_DATABASE_URL is required for database migrations"
        )
    return str(normalize_postgres_url(value).render_as_string(hide_password=False))


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=VERSION_TABLE_SCHEMA,
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
        known_revisions, governed_revisions = migration_role_revision_sets(
            ScriptDirectory.from_config(config)
        )
        migration_role_active = activate_platform_migration_role(
            connection,
            known_revisions=known_revisions,
            governed_revisions=governed_revisions,
        )
        if migration_role_active:
            connection.execute(CreateSchema(VERSION_TABLE_SCHEMA, if_not_exists=True))
            # Alembic mutates its own version table. Re-establish this narrow exception when a
            # previously governed database is rebuilt after owner DML defaults were hardened.
            connection.execute(_GOVERNED_VERSION_TABLE_DDL)
            connection.execute(_GOVERNED_VERSION_TABLE_GRANT)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=VERSION_TABLE_SCHEMA,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            if migration_role_active:
                current_revision = connection.scalar(_CURRENT_DATABASE_REVISION)
                if current_revision in governed_revisions:
                    # Newly created tables give their owner implicit DML. Remove that accidental
                    # runtime surface only after the governance revision exists. Historical
                    # migration tests and interrupted upgrades must retain the privileges needed
                    # to seed or transform their pre-governance state.
                    connection.execute(_GOVERNED_BUSINESS_DML_REVOKE)
                    restore_referential_integrity_owner_privileges(connection)
                    connection.execute(_GOVERNED_VERSION_TABLE_GRANT)
                else:
                    # Revisions before the role-governance boundary include data backfills and
                    # compatibility tests. Keep their deployment owner able to transform state;
                    # revision 0004 replaces this temporary surface with the final narrow grants.
                    connection.execute(_PRE_GOVERNANCE_MIGRATION_DML_GRANT)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
