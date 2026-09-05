"""Fail-closed migration-role activation for governed PostgreSQL revisions."""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

MIGRATION_ROLE_REVISION = "20260905_0004"

# PostgreSQL's referential-integrity triggers perform parent lookups with ``FOR KEY SHARE`` as
# the relation owner. Once ordinary owner DML is revoked, that row lock still requires UPDATE on
# at least one column of every referenced relation. These non-key, lifecycle timestamps are the
# smallest durable exception; ``bar_observations.inserted_at`` is additionally protected by the
# table's immutable-row trigger. ``aqa_migrate`` remains a trusted deployment authority, never a
# runtime application identity.
REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS: Final = (
    ("aqa", "aqa_experiments", "registered_at"),
    ("aqa", "aqa_bar_identities", "created_at"),
    ("aqa", "aqa_bar_events", "created_at"),
    ("aqa", "aqa_decision_slots", "created_at"),
    ("aqa", "aqa_signal_envelopes", "created_at"),
    ("aqa", "aqa_risk_decisions", "decided_at"),
    ("aqa", "aqa_execution_plans", "created_at"),
    ("aqa", "aqa_order_intents", "created_at"),
    ("aqa", "aqa_broker_orders", "submitted_at"),
    ("aqa", "aqa_jobs", "created_at"),
    ("market_data", "collection_universes", "created_at"),
    ("market_data", "bar_observations", "inserted_at"),
    ("market_data", "ingestion_runs", "started_at"),
)

_VERSION_TABLE_EXISTS = text("SELECT to_regclass('market_data.alembic_version') IS NOT NULL")
_SIGNAL_CREATED_AT_EXISTS = text(
    """
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'aqa'
          AND table_name = 'aqa_signal_envelopes'
          AND column_name = 'created_at'
    )
    """
)
_CURRENT_REVISIONS = text("SELECT version_num FROM market_data.alembic_version")
_MIGRATION_ROLE_IS_SAFE = text(
    """
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = 'aqa_migrate'
          AND NOT role.rolcanlogin
          AND NOT role.rolsuper
          AND role.rolinherit
          AND NOT role.rolcreatedb
          AND NOT role.rolcreaterole
          AND NOT role.rolreplication
          AND NOT role.rolbypassrls
          AND role.rolconfig IS NULL
          AND role.rolconnlimit = -1
          AND role.rolvaliduntil IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_auth_members AS membership
              WHERE membership.member = role.oid
          )
          AND NOT EXISTS (
              SELECT member_role.rolname,
                     membership.admin_option,
                     membership.inherit_option,
                     membership.set_option
              FROM pg_catalog.pg_auth_members AS membership
              JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
              WHERE membership.roleid = role.oid
              EXCEPT (
                  SELECT 'aqa_migrate_login', false, true, true
                  UNION ALL
                  SELECT current_user, true, false, true
                  WHERE :allow_transition
              )
          )
          AND EXISTS (
              SELECT 1
              FROM pg_catalog.pg_auth_members AS membership
              JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
              WHERE membership.roleid = role.oid
                AND member_role.rolname = 'aqa_migrate_login'
                AND NOT membership.admin_option
                AND membership.inherit_option
                AND membership.set_option
          )
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_db_role_setting AS setting
              WHERE setting.setrole = role.oid
          )
    )
    """
)
_CAN_SET_MIGRATION_ROLE = text("SELECT pg_has_role(session_user, 'aqa_migrate', 'SET')")
_SET_MIGRATION_ROLE = text("SET ROLE aqa_migrate")
_MIGRATION_ROLE_IS_ACTIVE = text("SELECT current_user = 'aqa_migrate'")
_MIGRATION_SESSION_IS_DEPLOYMENT_LOGIN = text(
    "SELECT session_user = 'aqa_migrate_login' AND current_user = 'aqa_migrate'"
)
_MIGRATION_SESSION_IS_BOOTSTRAP_ADMIN = text(
    """
    SELECT current_user = 'aqa_migrate'
       AND session_user <> 'aqa_migrate_login'
       AND EXISTS (
           SELECT 1
           FROM pg_catalog.pg_roles
           WHERE rolname = session_user
             AND rolsuper
       )
    """
)
_MIGRATION_LOGIN_IS_ACTIVE = text("SELECT current_user = 'aqa_migrate_login'")
_BOOTSTRAP_SESSION_IS_ACTIVE = text(
    """
    SELECT current_user = session_user
       AND EXISTS (
           SELECT 1
           FROM pg_catalog.pg_roles
           WHERE rolname = session_user
             AND rolsuper
       )
    """
)
_RESET_ROLE = text("RESET ROLE")
_REFERENTIAL_INTEGRITY_OWNER_COLUMN_ACLS = text(
    """
    SELECT namespace.nspname,
           relation.relname,
           attribute.attname,
           privilege.privilege_type
    FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS privilege
    JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = privilege.grantee
    WHERE namespace.nspname IN ('aqa', 'market_data')
      AND relation.relkind IN ('r', 'p')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND grantee.rolname = 'aqa_migrate'
    ORDER BY namespace.nspname, relation.relname, attribute.attname,
             privilege.privilege_type
    """
)


class MigrationRoleActivationError(RuntimeError):
    """Raised when a governed migration cannot enforce its database role."""


def _referential_integrity_owner_column_acls(
    connection: Connection,
) -> frozenset[tuple[str, str, str, str]]:
    return frozenset(
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute(_REFERENTIAL_INTEGRITY_OWNER_COLUMN_ACLS)
    )


def _referential_integrity_update_columns(
    connection: Connection,
) -> tuple[tuple[str, str, str], ...]:
    """Return the revision-appropriate lifecycle columns during rolling upgrades."""

    if connection.scalar(_SIGNAL_CREATED_AT_EXISTS) is True:
        return REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
    return tuple(
        (schema_name, table_name, "emitted_at")
        if (schema_name, table_name, column_name) == ("aqa", "aqa_signal_envelopes", "created_at")
        else (schema_name, table_name, column_name)
        for schema_name, table_name, column_name in REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
    )


def restore_referential_integrity_owner_privileges(connection: Connection) -> None:
    """Restore the exact owner column ACLs removed by PostgreSQL's table-level REVOKE.

    Governed deployments use the durable migration login. A superuser bootstrap session is also
    accepted for a fresh or compatibility upgrade because it already owns cluster-wide authority;
    this does not grant that path to an ordinary runtime login. The function always returns with
    the password-free migration authorization role active.
    """

    update_columns = _referential_integrity_update_columns(connection)
    expected = frozenset((*target, "UPDATE") for target in update_columns)
    try:
        deployment_login = connection.scalar(_MIGRATION_SESSION_IS_DEPLOYMENT_LOGIN) is True
        bootstrap_admin = connection.scalar(_MIGRATION_SESSION_IS_BOOTSTRAP_ADMIN) is True
        if not deployment_login and not bootstrap_admin:
            raise MigrationRoleActivationError(
                "Referential-integrity owner privileges require a governed migration session"
            )
        connection.execute(_RESET_ROLE)
        if deployment_login:
            principal_is_active = connection.scalar(_MIGRATION_LOGIN_IS_ACTIVE) is True
            granted_by = " GRANTED BY aqa_migrate_login"
        else:
            principal_is_active = connection.scalar(_BOOTSTRAP_SESSION_IS_ACTIVE) is True
            granted_by = ""
        if not principal_is_active:
            raise MigrationRoleActivationError(
                "The migration privilege grantor could not be activated safely"
            )
        for schema_name, table_name, column_name in update_columns:
            connection.execute(
                text(
                    f"GRANT UPDATE ({column_name}) ON {schema_name}.{table_name} "
                    f"TO aqa_migrate{granted_by}"
                )
            )
        connection.execute(_SET_MIGRATION_ROLE)
        if connection.scalar(_MIGRATION_ROLE_IS_ACTIVE) is not True:
            raise MigrationRoleActivationError(
                "The required database migration role was not restored"
            )
        if _referential_integrity_owner_column_acls(connection) != expected:
            raise MigrationRoleActivationError(
                "Referential-integrity owner privileges do not match the governed inventory"
            )
    except MigrationRoleActivationError:
        raise
    except SQLAlchemyError:
        raise MigrationRoleActivationError(
            "Referential-integrity owner privileges could not be restored"
        ) from None


def migration_role_revision_sets(
    script_directory: ScriptDirectory,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return all known revisions and the descendants governed by the role boundary."""

    known_revisions = frozenset(revision.revision for revision in script_directory.walk_revisions())
    if MIGRATION_ROLE_REVISION not in known_revisions:
        raise RuntimeError("The migration-role governance revision is not available")

    governed_revisions = frozenset(
        revision
        for revision in known_revisions
        if MIGRATION_ROLE_REVISION
        in {ancestor.revision for ancestor in script_directory.iterate_revisions(revision, "base")}
    )
    if MIGRATION_ROLE_REVISION not in governed_revisions:
        raise RuntimeError("The migration-role revision graph is invalid")
    return known_revisions, governed_revisions


def activate_platform_migration_role(
    connection: Connection,
    *,
    known_revisions: Collection[str],
    governed_revisions: Collection[str],
) -> bool:
    """Require the bootstrap boundary and activate ``aqa_migrate`` when safe.

    Fresh databases activate the pre-provisioned role before creating either managed schema.
    Databases at a known pre-governance revision retain their existing owner identity just long
    enough for the governance migration to transfer legacy ownership. Governed revisions always
    activate the role. Every path requires a safe role that the session can assume.
    """

    if connection.dialect.name != "postgresql":
        return False

    known = frozenset(known_revisions)
    governed = frozenset(governed_revisions)
    if MIGRATION_ROLE_REVISION not in governed or not known >= governed:
        raise ValueError("Invalid migration-role revision policy")

    try:
        version_table_exists = connection.scalar(_VERSION_TABLE_EXISTS)
        current_revisions = (
            tuple(connection.scalars(_CURRENT_REVISIONS)) if version_table_exists is True else ()
        )
    except MigrationRoleActivationError:
        raise
    except SQLAlchemyError:
        raise MigrationRoleActivationError(
            "Unable to inspect the database migration-role boundary"
        ) from None

    if not current_revisions:
        current_revision: str | None = None
    elif len(current_revisions) != 1:
        raise MigrationRoleActivationError(
            "The database migration-role boundary requires one current revision"
        )
    else:
        current_revision = current_revisions[0]
    if current_revision is not None and current_revision not in known:
        raise MigrationRoleActivationError(
            "The database revision is not recognized by this migration graph"
        )
    allow_transition = current_revision is not None and current_revision not in governed

    try:
        migration_role_is_safe = connection.scalar(
            _MIGRATION_ROLE_IS_SAFE,
            {"allow_transition": allow_transition},
        )
        if migration_role_is_safe is not True:
            raise MigrationRoleActivationError(
                "The required database migration role has not been safely bootstrapped"
            )
        can_set_role = connection.scalar(_CAN_SET_MIGRATION_ROLE)
        if can_set_role is not True:
            raise MigrationRoleActivationError(
                "The migration connection cannot assume the required database role"
            )
    except MigrationRoleActivationError:
        raise
    except SQLAlchemyError:
        raise MigrationRoleActivationError(
            "Unable to inspect the database migration-role boundary"
        ) from None

    if current_revision is not None and current_revision not in governed:
        return False

    try:
        connection.execute(_SET_MIGRATION_ROLE)
        migration_role_is_active = connection.scalar(_MIGRATION_ROLE_IS_ACTIVE)
    except MigrationRoleActivationError:
        raise
    except SQLAlchemyError:
        raise MigrationRoleActivationError(
            "Unable to activate the required database migration role"
        ) from None

    if migration_role_is_active is not True:
        raise MigrationRoleActivationError("The required database migration role was not activated")
    return True
