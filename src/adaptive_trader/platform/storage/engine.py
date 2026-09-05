"""Redacted SQLAlchemy engine construction from validated platform settings."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from sqlalchemy import URL, event
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.engine import Engine, make_url

from adaptive_trader.platform.config import RuntimeSettings
from adaptive_trader.platform.errors import RuntimeSettingsError
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA

_CONCRETE_PATH_TYPE = type(Path())
_LOOPBACK_DATABASE_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_POSTGRES_DRIVER_ALIASES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})
_ALLOWED_POSTGRES_QUERY_KEYS = frozenset({"sslmode"})
_APPLICATION_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$", flags=re.ASCII)

_POOL_SIZE = 5
_MAX_OVERFLOW = 5
_POOL_TIMEOUT_SECONDS = 5
_POOL_RECYCLE_SECONDS = 1_800
_CONNECT_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT_MILLISECONDS = 20_000
_LOCK_TIMEOUT_MILLISECONDS = 5_000
_IDLE_TRANSACTION_TIMEOUT_MILLISECONDS = 30_000
_SQLITE_BUSY_TIMEOUT_SECONDS = 5


def _configuration_error(reason: str) -> RuntimeSettingsError:
    return RuntimeSettingsError(f"platform database configuration {reason}")


def _validated_application_name(value: object) -> str:
    if type(value) is not str or _APPLICATION_NAME_PATTERN.fullmatch(value) is None:
        raise _configuration_error("has an invalid application name")
    return value


def _normalize_postgres_url(value: str) -> URL:
    parse_failed = False
    parsed: URL | None = None
    try:
        parsed = make_url(value)
    except Exception:
        parse_failed = True
    if parse_failed or parsed is None:
        raise _configuration_error("must contain a valid PostgreSQL URL")

    if parsed.drivername not in _POSTGRES_DRIVER_ALIASES:
        raise _configuration_error("must use PostgreSQL with psycopg")
    parsed = parsed.set(drivername="postgresql+psycopg")

    component_failed = False
    try:
        port = parsed.port
    except (TypeError, ValueError):
        component_failed = True
        port = None
    if (
        component_failed
        or not parsed.username
        or not parsed.password
        or not parsed.host
        or not parsed.database
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise _configuration_error("must include a user, password, host, and database")

    query_keys = frozenset(parsed.query)
    if not query_keys <= _ALLOWED_POSTGRES_QUERY_KEYS:
        raise _configuration_error("cannot override connection routing or client settings")

    sslmode = parsed.query.get("sslmode")
    if sslmode is not None and type(sslmode) is not str:
        raise _configuration_error("has an invalid sslmode")
    normalized_host = parsed.host.lower().rstrip(".")
    if normalized_host not in _LOOPBACK_DATABASE_HOSTS and sslmode != "verify-full":
        raise _configuration_error(
            "requires sslmode=verify-full for a non-loopback PostgreSQL host"
        )
    return parsed


def _load_postgres_url(settings: RuntimeSettings) -> URL:
    reference = settings.database_url_file
    if reference is None:
        raise _configuration_error("is missing a database URL secret reference")

    load_failed = False
    value = ""
    try:
        value = reference.load().reveal()
    except Exception:
        load_failed = True
    if load_failed:
        raise _configuration_error("could not load the database URL secret")
    return _normalize_postgres_url(value)


def _postgres_connect_args(application_name: str) -> dict[str, str | int]:
    return {
        "application_name": application_name,
        "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
        "options": (
            "-c timezone=UTC "
            f"-c statement_timeout={_STATEMENT_TIMEOUT_MILLISECONDS} "
            f"-c lock_timeout={_LOCK_TIMEOUT_MILLISECONDS} "
            f"-c idle_in_transaction_session_timeout="
            f"{_IDLE_TRANSACTION_TIMEOUT_MILLISECONDS}"
        ),
    }


def _create_postgres_engine(url: URL, *, application_name: str) -> Engine:
    creation_failed = False
    engine: Engine | None = None
    try:
        engine = sqlalchemy_create_engine(
            url,
            pool_pre_ping=True,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_timeout=_POOL_TIMEOUT_SECONDS,
            pool_recycle=_POOL_RECYCLE_SECONDS,
            pool_use_lifo=True,
            hide_parameters=True,
            connect_args=_postgres_connect_args(application_name),
        )
    except Exception:
        creation_failed = True
    if creation_failed or engine is None:
        raise _configuration_error("could not initialize the PostgreSQL engine")
    return engine


def _validated_offline_database_path(settings: RuntimeSettings) -> Path:
    path = settings.offline_database_path
    if (
        type(path) is not _CONCRETE_PATH_TYPE
        or not path.is_absolute()
        or path.anchor != os.sep
        or path.as_posix() != os.fspath(path)
        or any(component in {"", ".", ".."} for component in path.parts[1:])
    ):
        raise _configuration_error("is missing a validated absolute offline database path")

    preparation_failed = False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            path.parent.resolve(strict=True) != path.parent
            or path.is_symlink()
            or (path.exists() and not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode))
        ):
            preparation_failed = True
    except (OSError, RuntimeError, TypeError, ValueError):
        preparation_failed = True
    if preparation_failed:
        raise _configuration_error("could not prepare the offline database path")
    return path


def _configure_sqlite_connection(
    dbapi_connection: Any,
    connection_record: Any,
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
    finally:
        cursor.close()


def _create_sqlite_engine(path: Path) -> Engine:
    url = URL.create("sqlite+pysqlite", database=os.fspath(path))
    creation_failed = False
    engine: Engine | None = None
    try:
        engine = sqlalchemy_create_engine(
            url,
            pool_pre_ping=True,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_timeout=_POOL_TIMEOUT_SECONDS,
            pool_recycle=_POOL_RECYCLE_SECONDS,
            pool_use_lifo=True,
            hide_parameters=True,
            connect_args={
                "check_same_thread": False,
                "timeout": _SQLITE_BUSY_TIMEOUT_SECONDS,
            },
            execution_options={"schema_translate_map": {PLATFORM_SCHEMA: None}},
        )
        event.listen(engine, "connect", _configure_sqlite_connection)
    except Exception:
        if engine is not None:
            engine.dispose()
        creation_failed = True
    if creation_failed or engine is None:
        raise _configuration_error("could not initialize the offline SQLite engine")
    return engine


def create_platform_engine(
    settings: RuntimeSettings,
    *,
    application_name: str | None = None,
) -> Engine:
    """Create a bounded engine without accepting a caller-supplied connection URL.

    Operational PostgreSQL credentials are loaded exactly once through the database secret-file
    reference. Offline SQLite receives only the absolute path already selected by
    ``load_runtime_settings``. This factory constructs no schema and opens no connection.
    """

    if type(settings) is not RuntimeSettings:
        raise _configuration_error("requires validated runtime settings")
    selected_name = _validated_application_name(
        f"aqa-{settings.service.value}" if application_name is None else application_name
    )

    if settings.database_url_file is not None:
        if settings.offline_database_path is not None:
            raise _configuration_error("cannot select PostgreSQL and SQLite together")
        return _create_postgres_engine(
            _load_postgres_url(settings),
            application_name=selected_name,
        )
    return _create_sqlite_engine(_validated_offline_database_path(settings))
