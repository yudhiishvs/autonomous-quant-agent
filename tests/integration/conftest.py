"""Session-scoped guards for disposable PostgreSQL cluster-role integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text

from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.platform.security import (
    RedactedSecret,
    SecretFileVariable,
    bootstrap_local_secrets,
    load_secret_file,
)
from adaptive_trader.platform.storage.role_bootstrap import (
    ALL_PLATFORM_ROLES,
    AUTHORIZATION_ROLES,
    LOGIN_ROLE_BY_AUTHORIZATION_ROLE,
    bootstrap_platform_database_roles,
    load_platform_role_password,
    migration_login_database_url,
)

_DATABASE_URL = os.environ.get("APA_TEST_POSTGRES_URL", "").strip()


@dataclass(frozen=True, slots=True)
class _DisposableRoleCluster:
    migration_database_url: str
    login_database_urls: Mapping[str, str]
    created_roles: tuple[str, ...]


def _validate_cluster_role_guard() -> None:
    if os.environ.get("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE") != "YES":
        raise RuntimeError(
            "PostgreSQL integration tests require APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES"
        )
    if os.environ.get("APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES") != "YES":
        raise RuntimeError(
            "PostgreSQL role tests require APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES"
        )
    url = normalize_postgres_url(_DATABASE_URL)
    if url.host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("PostgreSQL role tests require a loopback database host")
    if url.database != "collector_test":
        raise RuntimeError("PostgreSQL role tests require the collector_test database")


def _existing_platform_roles() -> tuple[str, ...]:
    engine = create_engine(
        normalize_postgres_url(_DATABASE_URL),
        hide_parameters=True,
        connect_args=postgres_connect_args("platform-role-test-preflight", migration=True),
    )
    try:
        with engine.connect() as connection:
            return tuple(
                connection.scalars(
                    text(
                        "SELECT rolname FROM pg_catalog.pg_roles "
                        "WHERE rolname = ANY(:roles) ORDER BY rolname"
                    ),
                    {"roles": list(ALL_PLATFORM_ROLES)},
                )
            )
    finally:
        engine.dispose()


def _admin_url_secret(root: Path) -> RedactedSecret:
    path = root / "admin_database_url"
    path.write_text(f"{_DATABASE_URL}\n", encoding="utf-8")
    path.chmod(0o600)
    return load_secret_file(path, source=SecretFileVariable.DATABASE_URL)


def _drop_created_platform_roles(created_roles: tuple[str, ...]) -> None:
    normalized = normalize_postgres_url(_DATABASE_URL)
    conninfo = normalized.set(drivername="postgresql").render_as_string(hide_password=False)
    connect_args = cast(
        dict[str, Any],
        postgres_connect_args("platform-role-test-cleanup", migration=True),
    )
    with psycopg.connect(conninfo, **connect_args) as connection:
        connection.execute("DROP SCHEMA IF EXISTS aqa CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS market_data CASCADE")
        for role in reversed(created_roles):
            connection.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role)))
        for role in reversed(created_roles):
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        connection.commit()


@pytest.fixture(scope="session", autouse=True)
def _platform_postgres_role_cluster(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_DisposableRoleCluster | None]:
    if not _DATABASE_URL:
        yield None
        return

    _validate_cluster_role_guard()
    existing = _existing_platform_roles()
    if existing:
        raise RuntimeError("PostgreSQL role tests refuse a cluster with pre-existing managed roles")

    root = tmp_path_factory.mktemp("platform-role-bootstrap")
    root.chmod(0o700)
    bootstrap_local_secrets(root)
    admin_database_url = _admin_url_secret(root)
    result = bootstrap_platform_database_roles(
        admin_database_url,
        application_root=root,
    )
    created = result.created_authorization_roles + result.created_login_roles
    if frozenset(created) != frozenset(ALL_PLATFORM_ROLES):
        _drop_created_platform_roles(created)
        raise RuntimeError("PostgreSQL role tests did not create the exact managed role inventory")
    cluster = _DisposableRoleCluster(
        migration_database_url=migration_login_database_url(
            admin_database_url,
            application_root=root,
        ),
        login_database_urls=MappingProxyType(
            {
                role: str(
                    normalize_postgres_url(_DATABASE_URL)
                    .set(
                        username=LOGIN_ROLE_BY_AUTHORIZATION_ROLE[role],
                        password=load_platform_role_password(root, role).reveal(),
                    )
                    .render_as_string(hide_password=False)
                )
                for role in AUTHORIZATION_ROLES
            }
        ),
        created_roles=created,
    )
    try:
        yield cluster
    finally:
        _drop_created_platform_roles(cluster.created_roles)


@pytest.fixture(scope="session")
def platform_migration_database_url(
    _platform_postgres_role_cluster: _DisposableRoleCluster | None,
) -> str:
    """Return the disposable non-superuser migration-login URL without rendering it."""

    if _platform_postgres_role_cluster is None:
        raise RuntimeError("PostgreSQL role test cluster was not provisioned")
    return _platform_postgres_role_cluster.migration_database_url


@pytest.fixture(scope="session")
def platform_login_database_urls(
    _platform_postgres_role_cluster: _DisposableRoleCluster | None,
) -> Mapping[str, str]:
    """Return disposable login URLs keyed by authorization role without rendering them."""

    if _platform_postgres_role_cluster is None:
        raise RuntimeError("PostgreSQL role test cluster was not provisioned")
    return _platform_postgres_role_cluster.login_database_urls
