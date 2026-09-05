"""One-time PostgreSQL cluster-role bootstrap outside Alembic.

The bootstrap connection is an infrastructure authority used only to create and reconcile
cluster-global principals. Alembic subsequently connects as the non-superuser migration login
and assumes the password-free ``aqa_migrate`` authorization role.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import psycopg
from psycopg import sql

from adaptive_trader.platform.security import RedactedSecret, load_local_bootstrap_secret
from adaptive_trader.platform.storage.engine import (
    normalize_platform_postgres_url,
    platform_postgres_connect_args,
)
from adaptive_trader.platform.storage.migration_roles import (
    REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS,
)

AUTHORIZATION_ROLES: Final = (
    "aqa_migrate",
    "aqa_collector",
    "aqa_scheduler",
    "aqa_strategy",
    "aqa_execution",
    "aqa_control",
    "aqa_readonly",
)
LOGIN_ROLE_BY_AUTHORIZATION_ROLE: Final = {role: f"{role}_login" for role in AUTHORIZATION_ROLES}
ROLE_PASSWORD_FILE_BY_AUTHORIZATION_ROLE: Final = {
    role: f"{role}_password" for role in AUTHORIZATION_ROLES
}
LOGIN_ROLES: Final = tuple(LOGIN_ROLE_BY_AUTHORIZATION_ROLE.values())
ALL_PLATFORM_ROLES: Final = AUTHORIZATION_ROLES + LOGIN_ROLES
PRE_GOVERNANCE_REVISIONS: Final = frozenset({"20260903_0001", "20260905_0002", "20260905_0003"})

_ROLE_ATTRIBUTES = """
    SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
           rolreplication, rolbypassrls, rolconfig, rolconnlimit, rolvaliduntil
    FROM pg_catalog.pg_roles
    WHERE rolname = %s
"""
_ROLE_SETTINGS_EXIST = """
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_db_role_setting AS setting
        JOIN pg_catalog.pg_roles AS role ON role.oid = setting.setrole
        WHERE role.rolname = %s
    )
"""
_MANAGED_MEMBERSHIPS = """
    SELECT granted_role.rolname, member_role.rolname,
           membership.admin_option, membership.inherit_option, membership.set_option
    FROM pg_catalog.pg_auth_members AS membership
    JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
    JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
    WHERE granted_role.rolname = ANY(%s)
       OR member_role.rolname = ANY(%s)
    ORDER BY granted_role.rolname, member_role.rolname
"""
_ADMIN_AUTHORITY = """
    SELECT role.rolsuper,
           role.rolsuper OR database.datdba = role.oid,
           role.rolsuper OR pg_catalog.pg_has_role(current_user, namespace.nspowner, 'USAGE')
    FROM pg_catalog.pg_roles AS role
    JOIN pg_catalog.pg_database AS database ON database.datname = current_database()
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'
    WHERE role.rolname = current_user
"""
_CURRENT_DATABASE = "SELECT current_database()"
_CURRENT_USER = "SELECT current_user"
_VERSION_TABLE_EXISTS = "SELECT to_regclass('market_data.alembic_version') IS NOT NULL"
_CURRENT_REVISIONS = "SELECT version_num FROM market_data.alembic_version"
_MANAGED_SCHEMAS_EXIST = """
    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace
        WHERE nspname IN ('aqa', 'market_data')
    )
"""
_MANAGED_OBJECT_OWNERS = """
    SELECT array_agg(DISTINCT managed_object.owner_name ORDER BY managed_object.owner_name)
    FROM (
        SELECT pg_catalog.pg_get_userbyid(namespace.nspowner) AS owner_name
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname IN ('aqa', 'market_data')
        UNION ALL
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname IN ('aqa', 'market_data')
        UNION ALL
        SELECT pg_catalog.pg_get_userbyid(routine.proowner)
        FROM pg_catalog.pg_proc AS routine
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = routine.pronamespace
        WHERE namespace.nspname IN ('aqa', 'market_data')
    ) AS managed_object
"""
_ROLE_IS_SUPERUSER = "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = %s"
_HAS_DATABASE_CREATE = "SELECT has_database_privilege(%s, current_database(), 'CREATE')"
_TRANSITION_MEMBERSHIP_GRANTOR = """
    SELECT grantor_role.rolname
    FROM pg_catalog.pg_auth_members AS membership
    JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
    JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
    JOIN pg_catalog.pg_roles AS grantor_role ON grantor_role.oid = membership.grantor
    WHERE granted_role.rolname = 'aqa_migrate'
      AND member_role.rolname = %s
"""
_SET_MIGRATION_ROLE = "SET ROLE aqa_migrate"
_RESET_ROLE = "RESET ROLE"
_SIGNAL_CREATED_AT_EXISTS = """
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'aqa'
          AND table_name = 'aqa_signal_envelopes'
          AND column_name = 'created_at'
    )
"""
_REFERENTIAL_INTEGRITY_OWNER_COLUMN_ACLS = """
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


class PlatformRoleBootstrapError(RuntimeError):
    """Raised when cluster roles cannot be provisioned without weakening authority."""


@dataclass(frozen=True, slots=True)
class PlatformRoleBootstrapResult:
    """Safe names describing one completed bootstrap."""

    created_authorization_roles: tuple[str, ...]
    created_login_roles: tuple[str, ...]
    reconciled_login_roles: tuple[str, ...]


def load_platform_role_password(
    application_root: Path,
    authorization_role: str,
) -> RedactedSecret:
    """Load the fixed password corresponding to one authorization role."""

    if type(authorization_role) is not str or authorization_role not in AUTHORIZATION_ROLES:
        raise PlatformRoleBootstrapError("platform role password source is not supported")
    filename = ROLE_PASSWORD_FILE_BY_AUTHORIZATION_ROLE[authorization_role]
    try:
        return load_local_bootstrap_secret(application_root, filename)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PlatformRoleBootstrapError(
            "platform role password could not be loaded safely"
        ) from None


def _load_role_passwords(application_root: Path) -> dict[str, RedactedSecret]:
    return {
        role: load_platform_role_password(application_root, role) for role in AUTHORIZATION_ROLES
    }


def _role_attributes(
    connection: psycopg.Connection[Any],
    role: str,
) -> tuple[bool, bool, bool, bool, bool, bool, bool, list[str] | None, int, object] | None:
    row = connection.execute(_ROLE_ATTRIBUTES, (role,)).fetchone()
    if row is None:
        return None
    return (
        bool(row[0]),
        bool(row[1]),
        bool(row[2]),
        bool(row[3]),
        bool(row[4]),
        bool(row[5]),
        bool(row[6]),
        row[7],
        int(row[8]),
        row[9],
    )


def _require_safe_role(
    connection: psycopg.Connection[Any],
    role: str,
    *,
    login: bool,
) -> None:
    attributes = _role_attributes(connection, role)
    expected = (login, False, True, False, False, False, False, None, -1, None)
    if attributes != expected:
        raise PlatformRoleBootstrapError("an existing platform role has unsafe attributes")
    settings_row = connection.execute(_ROLE_SETTINGS_EXIST, (role,)).fetchone()
    if settings_row is None or settings_row[0] is not False:
        raise PlatformRoleBootstrapError("an existing platform role has unsafe settings")


def _create_role(
    connection: psycopg.Connection[Any],
    role: str,
    *,
    login: bool,
) -> None:
    login_clause = sql.SQL("LOGIN") if login else sql.SQL("NOLOGIN")
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} {} NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS"
        ).format(sql.Identifier(role), login_clause)
    )
    connection.execute(sql.SQL("REVOKE {} FROM CURRENT_USER").format(sql.Identifier(role)))


def _managed_memberships(
    connection: psycopg.Connection[Any],
) -> set[tuple[str, str, bool, bool, bool]]:
    rows = connection.execute(
        _MANAGED_MEMBERSHIPS,
        (list(ALL_PLATFORM_ROLES), list(ALL_PLATFORM_ROLES)),
    ).fetchall()
    return {(str(row[0]), str(row[1]), bool(row[2]), bool(row[3]), bool(row[4])) for row in rows}


def _expected_memberships(
    transition_owner: str | None = None,
) -> set[tuple[str, str, bool, bool, bool]]:
    expected = {
        (role, LOGIN_ROLE_BY_AUTHORIZATION_ROLE[role], False, True, True)
        for role in AUTHORIZATION_ROLES
    }
    if transition_owner is not None:
        expected.add(("aqa_migrate", transition_owner, True, False, True))
    return expected


def _grant_missing_memberships(
    connection: psycopg.Connection[Any],
    existing: set[tuple[str, str, bool, bool, bool]],
    *,
    expected: set[tuple[str, str, bool, bool, bool]],
) -> None:
    if not existing <= expected:
        raise PlatformRoleBootstrapError("platform roles have an unexpected membership")
    fixed_memberships = _expected_memberships()
    for role, login_role, _admin, _inherit, _set in sorted(fixed_memberships - existing):
        connection.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN FALSE, INHERIT TRUE, SET TRUE").format(
                sql.Identifier(role), sql.Identifier(login_role)
            )
        )


def _current_database_revision(connection: psycopg.Connection[Any]) -> str | None:
    table_row = connection.execute(_VERSION_TABLE_EXISTS).fetchone()
    if table_row is None or len(table_row) != 1 or type(table_row[0]) is not bool:
        raise PlatformRoleBootstrapError("database migration state could not be inspected")
    if table_row[0] is False:
        schema_row = connection.execute(_MANAGED_SCHEMAS_EXIST).fetchone()
        if schema_row is None or schema_row[0] is not False:
            raise PlatformRoleBootstrapError("unversioned managed schemas prevent role bootstrap")
        return None

    rows = connection.execute(_CURRENT_REVISIONS).fetchall()
    if len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not str:
        raise PlatformRoleBootstrapError("database migration state is not a single revision")
    return str(rows[0][0])


def _current_user(connection: psycopg.Connection[Any]) -> str:
    row = connection.execute(_CURRENT_USER).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not str or not row[0]:
        raise PlatformRoleBootstrapError("database bootstrap identity could not be inspected")
    return str(row[0])


def _managed_object_owners(connection: psycopg.Connection[Any]) -> tuple[str, ...]:
    owners_row = connection.execute(_MANAGED_OBJECT_OWNERS).fetchone()
    owners = None if owners_row is None else owners_row[0]
    if not isinstance(owners, list) or any(type(owner) is not str for owner in owners):
        raise PlatformRoleBootstrapError("managed object ownership could not be validated")
    return tuple(str(owner) for owner in owners)


def _referential_integrity_owner_column_acls(
    connection: psycopg.Connection[Any],
) -> frozenset[tuple[str, str, str, str]]:
    return frozenset(
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute(_REFERENTIAL_INTEGRITY_OWNER_COLUMN_ACLS).fetchall()
    )


def _grant_referential_integrity_owner_privileges(
    connection: psycopg.Connection[Any],
    *,
    grantor: str,
) -> None:
    """Install the exact FK row-lock exception through the bootstrap administrator."""

    for schema_name, table_name, column_name in _referential_integrity_update_columns(connection):
        connection.execute(
            sql.SQL("GRANT UPDATE ({}) ON {}.{} TO aqa_migrate GRANTED BY {}").format(
                sql.Identifier(column_name),
                sql.Identifier(schema_name),
                sql.Identifier(table_name),
                sql.Identifier(grantor),
            )
        )


def _require_exact_referential_integrity_owner_privileges(
    connection: psycopg.Connection[Any],
) -> None:
    expected = frozenset(
        (*target, "UPDATE") for target in _referential_integrity_update_columns(connection)
    )
    if _referential_integrity_owner_column_acls(connection) != expected:
        raise PlatformRoleBootstrapError(
            "referential-integrity owner privileges do not match the governed inventory"
        )


def _referential_integrity_update_columns(
    connection: psycopg.Connection[Any],
) -> tuple[tuple[str, str, str], ...]:
    """Return the revision-appropriate lifecycle columns during rolling upgrades."""

    exists = connection.execute(_SIGNAL_CREATED_AT_EXISTS).fetchone()
    if exists == (True,):
        return REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
    return tuple(
        (schema_name, table_name, "emitted_at")
        if (schema_name, table_name, column_name) == ("aqa", "aqa_signal_envelopes", "created_at")
        else (schema_name, table_name, column_name)
        for schema_name, table_name, column_name in REFERENTIAL_INTEGRITY_OWNER_UPDATE_COLUMNS
    )


def _legacy_transition_owner(connection: psycopg.Connection[Any]) -> str | None:
    revision = _current_database_revision(connection)
    if revision not in PRE_GOVERNANCE_REVISIONS:
        return None

    owners = _managed_object_owners(connection)
    if len(owners) != 1:
        raise PlatformRoleBootstrapError("legacy managed objects must have one validated owner")
    owner = owners[0]
    if owner in ALL_PLATFORM_ROLES:
        raise PlatformRoleBootstrapError("legacy migration owner conflicts with a managed role")
    return owner


def _grant_legacy_transition_membership(
    connection: psycopg.Connection[Any],
    owner: str,
) -> None:
    connection.execute(
        sql.SQL("GRANT aqa_migrate TO {} WITH ADMIN TRUE, INHERIT FALSE, SET TRUE").format(
            sql.Identifier(owner)
        )
    )


def _grant_legacy_transition_database_authority(
    connection: psycopg.Connection[Any],
    owner: str,
) -> None:
    database_row = connection.execute(_CURRENT_DATABASE).fetchone()
    if (
        database_row is None
        or len(database_row) != 1
        or type(database_row[0]) is not str
        or not database_row[0]
    ):
        raise PlatformRoleBootstrapError("target PostgreSQL database could not be identified")
    connection.execute(_SET_MIGRATION_ROLE)
    connection.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(database_row[0]),
            sql.Identifier(owner),
        )
    )
    connection.execute(_RESET_ROLE)


def _revoke_legacy_transition_database_authority(
    connection: psycopg.Connection[Any],
    owner: str,
) -> None:
    """Remove the previous bounded database grant before rebuilding bootstrap ACLs."""

    database_row = connection.execute(_CURRENT_DATABASE).fetchone()
    if (
        database_row is None
        or len(database_row) != 1
        or type(database_row[0]) is not str
        or not database_row[0]
    ):
        raise PlatformRoleBootstrapError("target PostgreSQL database could not be identified")
    connection.execute(_SET_MIGRATION_ROLE)
    connection.execute(
        sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
            sql.Identifier(database_row[0]),
            sql.Identifier(owner),
        )
    )
    connection.execute(_RESET_ROLE)


def _role_is_superuser(
    connection: psycopg.Connection[Any],
    role: str,
) -> bool:
    row = connection.execute(_ROLE_IS_SUPERUSER, (role,)).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not bool:
        raise PlatformRoleBootstrapError("legacy migration owner could not be validated")
    return bool(row[0])


def _grant_initial_migration_database_authority(
    connection: psycopg.Connection[Any],
) -> None:
    """Permit the migration authorization role to create the first managed schemas."""

    database_row = connection.execute(_CURRENT_DATABASE).fetchone()
    if (
        database_row is None
        or len(database_row) != 1
        or type(database_row[0]) is not str
        or not database_row[0]
    ):
        raise PlatformRoleBootstrapError("target PostgreSQL database could not be identified")
    managed_roles = sql.SQL(", ").join(sql.Identifier(role) for role in ALL_PLATFORM_ROLES)
    # Revision 0004 can leave a self-granted CREATE ACL beneath the administrator's grant option.
    # Remove that dependent edge as its exact grantor before rebuilding the administrator-owned
    # database ACL. This keeps repeated bootstrap deterministic without a cascading revocation.
    connection.execute(_SET_MIGRATION_ROLE)
    connection.execute(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON DATABASE {} FROM aqa_migrate GRANTED BY aqa_migrate"
        ).format(sql.Identifier(database_row[0]))
    )
    connection.execute(_RESET_ROLE)
    connection.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
            sql.Identifier(database_row[0]),
            managed_roles,
        )
    )
    connection.execute(
        sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(database_row[0])
        )
    )
    connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO aqa_migrate").format(
            sql.Identifier(database_row[0])
        )
    )
    connection.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO aqa_migrate WITH GRANT OPTION").format(
            sql.Identifier(database_row[0])
        )
    )


def _harden_public_schema(connection: psycopg.Connection[Any]) -> None:
    """Remove bootstrap defaults that permit application roles to use ``public``."""

    managed_roles = sql.SQL(", ").join(sql.Identifier(role) for role in ALL_PLATFORM_ROLES)
    connection.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC, {}").format(managed_roles)
    )


def _change_password(
    connection: psycopg.Connection[Any],
    login_role: str,
    password: RedactedSecret,
) -> None:
    # PQchangePassword performs the required escaping and hashes according to the server's
    # password-encryption policy before issuing ALTER ROLE. Plaintext is never interpolated into
    # application SQL or retained by a SQLAlchemy statement/log record.
    connection.pgconn.change_password(
        login_role.encode("utf-8"),
        password.reveal().encode("utf-8"),
    )


def _provision_roles(
    connection: psycopg.Connection[Any],
    passwords: Mapping[str, RedactedSecret],
    *,
    transition_owner: str | None = None,
) -> PlatformRoleBootstrapResult:
    authority_row = connection.execute(_ADMIN_AUTHORITY).fetchone()
    if authority_row is None or len(authority_row) != 3 or authority_row[0] is not True:
        raise PlatformRoleBootstrapError(
            "cluster bootstrap requires PostgreSQL superuser authority"
        )
    if authority_row[1] is not True:
        raise PlatformRoleBootstrapError("cluster bootstrap requires target database ownership")
    if authority_row[2] is not True:
        raise PlatformRoleBootstrapError("cluster bootstrap requires public schema ownership")
    if set(passwords) != set(AUTHORIZATION_ROLES) or any(
        type(password) is not RedactedSecret for password in passwords.values()
    ):
        raise PlatformRoleBootstrapError("platform role password inventory is incomplete")
    created_authorization: list[str] = []
    created_logins: list[str] = []
    for role in AUTHORIZATION_ROLES:
        if _role_attributes(connection, role) is None:
            _create_role(connection, role, login=False)
            created_authorization.append(role)
        _require_safe_role(connection, role, login=False)

    for role in AUTHORIZATION_ROLES:
        login_role = LOGIN_ROLE_BY_AUTHORIZATION_ROLE[role]
        if _role_attributes(connection, login_role) is None:
            _create_role(connection, login_role, login=True)
            created_logins.append(login_role)
        _require_safe_role(connection, login_role, login=True)

    transition_member = (
        transition_owner
        if transition_owner is not None and not _role_is_superuser(connection, transition_owner)
        else None
    )
    expected_memberships = _expected_memberships(transition_member)
    existing_memberships = _managed_memberships(connection)
    _grant_missing_memberships(
        connection,
        existing_memberships,
        expected=expected_memberships,
    )
    if (
        transition_member is not None
        and (
            "aqa_migrate",
            transition_member,
            True,
            False,
            True,
        )
        not in existing_memberships
    ):
        _grant_legacy_transition_membership(connection, transition_member)
    if _managed_memberships(connection) != expected_memberships:
        raise PlatformRoleBootstrapError("platform role memberships could not be reconciled")
    if transition_member is not None:
        _revoke_legacy_transition_database_authority(connection, transition_member)
    _grant_initial_migration_database_authority(connection)
    if transition_member is not None:
        _grant_legacy_transition_database_authority(connection, transition_member)
    _harden_public_schema(connection)

    for role in AUTHORIZATION_ROLES:
        _change_password(
            connection,
            LOGIN_ROLE_BY_AUTHORIZATION_ROLE[role],
            passwords[role],
        )

    return PlatformRoleBootstrapResult(
        created_authorization_roles=tuple(created_authorization),
        created_login_roles=tuple(created_logins),
        reconciled_login_roles=LOGIN_ROLES,
    )


def bootstrap_platform_database_roles(
    admin_database_url: RedactedSecret,
    *,
    application_root: Path,
) -> PlatformRoleBootstrapResult:
    """Provision fixed authorization/login principals through a cluster administrator.

    The function creates no schemas and performs no application migration. Passwords are loaded
    from the owner-private files created by the local infrastructure bootstrap and are updated via
    libpq's dedicated password-change boundary.
    """

    if type(admin_database_url) is not RedactedSecret:
        raise PlatformRoleBootstrapError("cluster bootstrap requires a loaded database URL")
    passwords = _load_role_passwords(application_root)
    try:
        normalized = normalize_platform_postgres_url(admin_database_url.reveal())
        if not normalized.username or not normalized.password:
            raise PlatformRoleBootstrapError(
                "cluster bootstrap database URL requires a user and password"
            )
        conninfo = normalized.set(drivername="postgresql").render_as_string(hide_password=False)
        connect_args = cast(
            dict[str, Any],
            platform_postgres_connect_args(
                "aqa-platform-role-bootstrap",
                read_only=False,
                migration=True,
            ),
        )
        with psycopg.connect(conninfo, **connect_args) as connection:
            transition_owner = _legacy_transition_owner(connection)
            result = _provision_roles(
                connection,
                passwords,
                transition_owner=transition_owner,
            )
            connection.commit()
            return result
    except PlatformRoleBootstrapError:
        raise
    except (psycopg.Error, OSError, RuntimeError, TypeError, ValueError):
        raise PlatformRoleBootstrapError(
            "PostgreSQL platform roles could not be bootstrapped safely"
        ) from None


def migration_login_database_url(
    base_database_url: RedactedSecret,
    *,
    application_root: Path,
) -> str:
    """Build the deployable migration-login URL at an explicit adapter boundary."""

    if type(base_database_url) is not RedactedSecret:
        raise PlatformRoleBootstrapError("migration login requires a loaded database URL")
    try:
        normalized = normalize_platform_postgres_url(base_database_url.reveal())
        password = load_platform_role_password(application_root, "aqa_migrate")
        return str(
            normalized.set(
                username=LOGIN_ROLE_BY_AUTHORIZATION_ROLE["aqa_migrate"],
                password=password.reveal(),
            ).render_as_string(hide_password=False)
        )
    except PlatformRoleBootstrapError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise PlatformRoleBootstrapError(
            "migration login database URL could not be composed safely"
        ) from None


def require_legacy_role_transition(
    admin_database_url: RedactedSecret,
) -> None:
    """Validate the exact temporary authority required to execute revision 0004.

    The supplied identity must be the sole owner of every existing managed object, and the
    bootstrap must have granted it only the one bounded migration-role membership. No database
    mutation occurs in this validation step.
    """

    if type(admin_database_url) is not RedactedSecret:
        raise PlatformRoleBootstrapError("legacy role transition requires a loaded database URL")
    try:
        normalized = normalize_platform_postgres_url(admin_database_url.reveal())
        if not normalized.username or not normalized.password:
            raise PlatformRoleBootstrapError(
                "legacy role transition database URL requires a user and password"
            )
        conninfo = normalized.set(drivername="postgresql").render_as_string(hide_password=False)
        connect_args = cast(
            dict[str, Any],
            platform_postgres_connect_args(
                "aqa-platform-role-transition-validation",
                read_only=True,
                migration=True,
            ),
        )
        with psycopg.connect(conninfo, **connect_args) as connection:
            transition_owner = _legacy_transition_owner(connection)
            if transition_owner is None:
                raise PlatformRoleBootstrapError(
                    "legacy role transition requires a recognized pre-governance revision"
                )
            if _current_user(connection) != transition_owner:
                raise PlatformRoleBootstrapError(
                    "legacy role transition requires the validated legacy owner"
                )
            transition_member = (
                None if _role_is_superuser(connection, transition_owner) else transition_owner
            )
            if _managed_memberships(connection) != _expected_memberships(transition_member):
                raise PlatformRoleBootstrapError(
                    "legacy role transition membership is absent or unsafe"
                )
    except PlatformRoleBootstrapError:
        raise
    except (psycopg.Error, OSError, RuntimeError, TypeError, ValueError):
        raise PlatformRoleBootstrapError(
            "legacy role transition could not be validated safely"
        ) from None


def finalize_legacy_role_transition(
    admin_database_url: RedactedSecret,
    *,
    governed_revisions: Collection[str],
    application_root: Path | None = None,
) -> bool:
    """Remove the bounded legacy-owner membership after governance is durable.

    Returns whether a transition membership was removed. Exact fixed memberships are accepted so
    the operation is restart-safe after a successful cleanup; all other membership drift fails
    closed.
    """

    if type(admin_database_url) is not RedactedSecret:
        raise PlatformRoleBootstrapError("role transition cleanup requires a loaded database URL")
    governed = frozenset(governed_revisions)
    if not governed:
        raise PlatformRoleBootstrapError("governed migration revision inventory is empty")
    try:
        database_url = (
            admin_database_url.reveal()
            if application_root is None
            else migration_login_database_url(
                admin_database_url,
                application_root=application_root,
            )
        )
        normalized = normalize_platform_postgres_url(database_url)
        if not normalized.username or not normalized.password:
            raise PlatformRoleBootstrapError(
                "role transition cleanup database URL requires a user and password"
            )
        conninfo = normalized.set(drivername="postgresql").render_as_string(hide_password=False)
        connect_args = cast(
            dict[str, Any],
            platform_postgres_connect_args(
                "aqa-platform-role-transition-cleanup",
                read_only=False,
                migration=True,
            ),
        )
        with psycopg.connect(conninfo, **connect_args) as connection:
            cleanup_identity = _current_user(connection)
            fixed_memberships = _expected_memberships()
            memberships = _managed_memberships(connection)
            transition_candidates = memberships - fixed_memberships
            if not transition_candidates:
                transition_owner = None
            elif len(transition_candidates) == 1:
                role, member, admin, inherit, can_set = next(iter(transition_candidates))
                transition_owner = (
                    member
                    if role == "aqa_migrate"
                    and member not in ALL_PLATFORM_ROLES
                    and admin is True
                    and inherit is False
                    and can_set is True
                    else None
                )
            else:
                transition_owner = None
            if memberships != (
                fixed_memberships
                if transition_owner is None
                else _expected_memberships(transition_owner)
            ):
                raise PlatformRoleBootstrapError(
                    "role transition cleanup found unsafe membership drift"
                )

            connection.execute(_SET_MIGRATION_ROLE)
            revision = _current_database_revision(connection)
            if revision not in governed:
                raise PlatformRoleBootstrapError(
                    "role transition cleanup requires a governed database revision"
                )
            if _managed_object_owners(connection) != ("aqa_migrate",):
                raise PlatformRoleBootstrapError(
                    "role transition cleanup requires durable migration ownership"
                )
            connection.execute(_RESET_ROLE)
            if memberships == fixed_memberships:
                if _role_is_superuser(connection, cleanup_identity):
                    _grant_referential_integrity_owner_privileges(
                        connection,
                        grantor=cleanup_identity,
                    )
                    connection.commit()
                _require_exact_referential_integrity_owner_privileges(connection)
                return False
            if transition_owner is None or not _role_is_superuser(connection, cleanup_identity):
                raise PlatformRoleBootstrapError(
                    "role transition cleanup requires the bootstrap administrator"
                )
            grantor_row = connection.execute(
                _TRANSITION_MEMBERSHIP_GRANTOR,
                (transition_owner,),
            ).fetchone()
            if grantor_row != (cleanup_identity,):
                raise PlatformRoleBootstrapError(
                    "role transition cleanup requires the original membership grantor"
                )
            database_create_row = connection.execute(
                _HAS_DATABASE_CREATE,
                (transition_owner,),
            ).fetchone()
            if database_create_row is None or database_create_row != (False,):
                raise PlatformRoleBootstrapError(
                    "role transition cleanup found residual database authority"
                )

            _grant_referential_integrity_owner_privileges(
                connection,
                grantor=cleanup_identity,
            )
            _require_exact_referential_integrity_owner_privileges(connection)
            connection.execute(
                sql.SQL("REVOKE aqa_migrate FROM {}").format(sql.Identifier(transition_owner))
            )
            if _managed_memberships(connection) != fixed_memberships:
                raise PlatformRoleBootstrapError(
                    "role transition membership could not be removed safely"
                )
            _require_exact_referential_integrity_owner_privileges(connection)
            connection.commit()
            return True
    except PlatformRoleBootstrapError:
        raise
    except (psycopg.Error, OSError, RuntimeError, TypeError, ValueError):
        raise PlatformRoleBootstrapError(
            "role transition membership could not be finalized safely"
        ) from None
