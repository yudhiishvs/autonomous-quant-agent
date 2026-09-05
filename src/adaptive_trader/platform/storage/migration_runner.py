"""Bounded PostgreSQL migration orchestration for the platform schema."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

from adaptive_trader.platform.security import RedactedSecret
from adaptive_trader.platform.storage.engine import (
    normalize_platform_postgres_url,
    platform_postgres_connect_args,
)
from adaptive_trader.platform.storage.migration_roles import (
    MIGRATION_ROLE_REVISION,
    migration_role_revision_sets,
)
from adaptive_trader.platform.storage.role_bootstrap import (
    PRE_GOVERNANCE_REVISIONS,
    PlatformRoleBootstrapError,
    finalize_legacy_role_transition,
    migration_login_database_url,
    require_legacy_role_transition,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_VERSION_TABLE_SCHEMA = "market_data"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        normalize_platform_postgres_url(database_url)
        .render_as_string(hide_password=False)
        .replace("%", "%%"),
    )
    return config


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(
        normalize_platform_postgres_url(database_url),
        future=True,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=platform_postgres_connect_args(
            "aqa-platform-migration-state",
            read_only=True,
            migration=True,
        ),
    )
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"version_table_schema": _VERSION_TABLE_SCHEMA},
            )
            revisions = context.get_current_heads()
    finally:
        engine.dispose()
    if len(revisions) > 1:
        raise RuntimeError("Platform migrations require a single current database revision")
    return revisions[0] if revisions else None


def _migration_entry_revision(
    *,
    base_database_url: str,
    migration_database_url: str,
) -> str | None:
    """Inspect through the durable login, falling back only across the legacy ACL boundary."""

    try:
        migration_revision = _current_revision(migration_database_url)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "42501":
            raise
        return _current_revision(base_database_url)

    if migration_revision is not None:
        return migration_revision
    # An inaccessible pre-governance version table can appear absent to catalog helpers. The
    # supplied legacy owner is authoritative only while the durable login reports no revision.
    return _current_revision(base_database_url)


def _finalize_governed_transition(
    *,
    base_database_url: RedactedSecret,
    bootstrap_admin_database_url: RedactedSecret | None,
    application_root: Path,
    governed_revisions: frozenset[str],
) -> None:
    """Prefer durable authority, with one exact fallback for interrupted legacy cleanup."""

    try:
        finalize_legacy_role_transition(
            base_database_url,
            governed_revisions=governed_revisions,
            application_root=application_root,
        )
    except PlatformRoleBootstrapError as migration_error:
        if bootstrap_admin_database_url is None:
            raise
        try:
            finalize_legacy_role_transition(
                bootstrap_admin_database_url,
                governed_revisions=governed_revisions,
            )
        except PlatformRoleBootstrapError:
            raise migration_error from None


def migrate_platform_database(
    base_database_url: RedactedSecret,
    *,
    application_root: Path,
    bootstrap_admin_database_url: RedactedSecret | None = None,
) -> None:
    """Reach head without using legacy authority beyond the governance handoff.

    Empty databases and already-governed databases migrate only through
    ``aqa_migrate_login``. A recognized pre-governance database is upgraded to exactly 0004 by
    its validated legacy bootstrap owner, the temporary membership is removed, and a new
    connection then applies every governed descendant as the migration login.
    """

    if type(base_database_url) is not RedactedSecret:
        raise TypeError("platform migration requires a loaded database URL")
    if (
        bootstrap_admin_database_url is not None
        and type(bootstrap_admin_database_url) is not RedactedSecret
    ):
        raise TypeError("platform migration administrator requires a loaded database URL")
    migration_database_url = migration_login_database_url(
        base_database_url,
        application_root=application_root,
    )
    base_url = base_database_url.reveal()
    config = _alembic_config(base_url)
    script_directory = ScriptDirectory.from_config(config)
    known_revisions, governed_revisions = migration_role_revision_sets(script_directory)
    expected_head = script_directory.get_current_head()
    if expected_head is None:
        raise RuntimeError("No platform migration head exists")

    current_revision = _migration_entry_revision(
        base_database_url=base_url,
        migration_database_url=migration_database_url,
    )
    if current_revision is None:
        command.upgrade(_alembic_config(migration_database_url), "head")
    elif current_revision in PRE_GOVERNANCE_REVISIONS:
        if bootstrap_admin_database_url is None:
            raise RuntimeError(
                "Legacy role transition requires the separate bootstrap administrator URL"
            )
        require_legacy_role_transition(base_database_url)
        command.upgrade(_alembic_config(base_url), MIGRATION_ROLE_REVISION)
        finalize_legacy_role_transition(
            bootstrap_admin_database_url,
            governed_revisions=governed_revisions,
        )
        command.upgrade(_alembic_config(migration_database_url), "head")
    elif current_revision in governed_revisions:
        _finalize_governed_transition(
            base_database_url=base_database_url,
            bootstrap_admin_database_url=bootstrap_admin_database_url,
            application_root=application_root,
            governed_revisions=governed_revisions,
        )
        command.upgrade(_alembic_config(migration_database_url), "head")
    elif current_revision in known_revisions:
        raise RuntimeError("Database revision has no supported platform migration transition")
    else:
        raise RuntimeError("Database revision is not recognized by this migration graph")

    if _current_revision(migration_database_url) != expected_head:
        raise RuntimeError("Platform migrations did not reach the checked-in head")
