"""Offline tests for the generic platform's database-engine trust boundary."""

from __future__ import annotations

import inspect
import shutil
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import URL, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

import adaptive_trader.platform.storage.engine as storage_engine
from adaptive_trader.platform import (
    RuntimeService,
    RuntimeSettings,
    RuntimeSettingsError,
    load_runtime_settings,
)
from adaptive_trader.platform.storage import (
    create_platform_engine,
    create_platform_read_only_engine,
)
from adaptive_trader.platform.storage.tables import aqa_experiments, metadata

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "TEST_DATABASE_PASSWORD_DO_NOT_LEAK"


def _application_root(tmp_path: Path) -> Path:
    root = tmp_path / "application"
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    return root


def _offline_settings(tmp_path: Path) -> RuntimeSettings:
    return _load_settings(_application_root(tmp_path), {})


def _database_settings(tmp_path: Path, database_url: str) -> RuntimeSettings:
    root = _application_root(tmp_path)
    secret_path = root / "database-url"
    secret_path.write_text(database_url, encoding="utf-8")
    secret_path.chmod(0o600)
    return _load_settings(
        root,
        {"AQA_DATABASE_URL_FILE": secret_path.as_posix()},
    )


def _load_settings(root: Path, environment: dict[str, str]) -> RuntimeSettings:
    return load_runtime_settings(
        environment,
        service=RuntimeService.MARKET_DATA_WORKER,
        application_root=root,
    )


def test_factory_accepts_only_validated_runtime_settings() -> None:
    signature = inspect.signature(create_platform_engine)

    assert tuple(signature.parameters) == ("settings", "application_name")
    with pytest.raises(RuntimeSettingsError, match="validated runtime settings") as captured:
        create_platform_engine(cast(RuntimeSettings, SENTINEL))
    assert SENTINEL not in str(captured.value)

    read_signature = inspect.signature(create_platform_read_only_engine)
    assert tuple(read_signature.parameters) == ("settings", "application_name")
    with pytest.raises(RuntimeSettingsError, match="validated runtime settings") as captured:
        create_platform_read_only_engine(cast(RuntimeSettings, SENTINEL))
    assert SENTINEL not in str(captured.value)


def test_read_only_offline_factory_does_not_create_a_missing_path(tmp_path: Path) -> None:
    settings = _offline_settings(tmp_path)
    database_path = settings.offline_database_path
    assert database_path is not None
    assert not database_path.parent.exists()

    with pytest.raises(RuntimeSettingsError, match="read-only"):
        create_platform_read_only_engine(settings)

    assert not database_path.parent.exists()
    assert not database_path.exists()


def test_read_only_offline_factory_is_observational_and_enforces_query_only(
    tmp_path: Path,
) -> None:
    settings = _offline_settings(tmp_path)
    database_path = settings.offline_database_path
    assert database_path is not None
    writer = create_platform_engine(settings)
    try:
        metadata.create_all(writer)
    finally:
        writer.dispose()
    before_bytes = database_path.read_bytes()
    before_stat = database_path.stat()
    wal_path = database_path.with_name(database_path.name + "-wal")
    before_sidecars = tuple(database_path.parent.glob(database_path.name + "-*"))
    assert before_sidecars == ()

    reader = create_platform_read_only_engine(settings)
    try:
        assert reader.url.query == {"mode": "ro", "uri": "true"}
        with reader.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5_000
            assert connection.scalar(select(aqa_experiments.c.experiment_hash)) is None
            with pytest.raises(OperationalError):
                connection.exec_driver_sql("CREATE TABLE forbidden_write (id INTEGER)")
    finally:
        reader.dispose()

    after_stat = database_path.stat()
    assert database_path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    sidecars = tuple(sorted(database_path.parent.glob(database_path.name + "-*")))
    assert sidecars == (
        database_path.with_name(database_path.name + "-shm"),
        wal_path,
    )
    assert wal_path.read_bytes() == b""


def test_read_only_offline_factory_observes_an_active_wal_without_mutating_it(
    tmp_path: Path,
) -> None:
    settings = _offline_settings(tmp_path)
    database_path = settings.offline_database_path
    assert database_path is not None
    writer = create_platform_engine(settings)
    try:
        with writer.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE observation (value TEXT NOT NULL)")
            connection.exec_driver_sql("INSERT INTO observation (value) VALUES ('committed')")
        wal_path = database_path.with_name(database_path.name + "-wal")
        assert wal_path.is_file()
        before_wal = wal_path.read_bytes()

        reader = create_platform_read_only_engine(settings)
        try:
            assert reader.url.query == {"mode": "ro", "uri": "true"}
            with reader.connect() as connection:
                assert (
                    connection.exec_driver_sql("SELECT value FROM observation").scalar_one()
                    == "committed"
                )
                assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        finally:
            reader.dispose()

        assert wal_path.read_bytes() == before_wal
    finally:
        writer.dispose()


def test_read_only_offline_factory_does_not_assume_database_is_immutable(
    tmp_path: Path,
) -> None:
    writer_settings = _offline_settings(tmp_path)
    database_path = writer_settings.offline_database_path
    assert database_path is not None
    writer = create_platform_engine(writer_settings)
    try:
        with writer.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE evidence (value INTEGER NOT NULL)")

        reader = create_platform_read_only_engine(writer_settings)
        try:
            # Constructing the reader before a writer starts must not freeze an obsolete
            # sidecar-free view of the database.
            with writer.begin() as connection:
                connection.exec_driver_sql("INSERT INTO evidence VALUES (1)")

            with reader.connect() as connection:
                assert connection.exec_driver_sql("SELECT value FROM evidence").scalar_one() == 1
        finally:
            reader.dispose()
    finally:
        writer.dispose()


def test_offline_factory_uses_only_selected_absolute_path_and_sqlite_pragmas(
    tmp_path: Path,
) -> None:
    settings = _offline_settings(tmp_path)
    expected_path = settings.offline_database_path
    assert expected_path is not None

    engine = create_platform_engine(settings)
    try:
        assert engine.url.drivername == "sqlite+pysqlite"
        assert engine.url.database == expected_path.as_posix()
        assert engine.hide_parameters is True
        assert expected_path.parent.is_dir()
        assert not expected_path.exists()

        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5_000
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                ).scalar_one()
                == 0
            )

        assert expected_path.is_file()
    finally:
        engine.dispose()


def test_offline_factory_translates_platform_schema_for_repository_sql(tmp_path: Path) -> None:
    engine = create_platform_engine(_offline_settings(tmp_path))
    registered_at = datetime(2026, 9, 5, tzinfo=UTC)
    row = {
        "experiment_hash": "1" * 64,
        "experiment_id": "offline-fixture",
        "experiment_version": 1,
        "schema_version": 1,
        "configuration": {"fixture": True},
        "content_hash": "2" * 64,
        "registered_at": registered_at,
    }
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(insert(aqa_experiments).values(row))
        with engine.connect() as connection:
            restored = connection.execute(select(aqa_experiments)).mappings().one()

        assert restored["experiment_hash"] == row["experiment_hash"]
        assert restored["registered_at"] == registered_at
    finally:
        engine.dispose()


def test_offline_factory_rejects_path_replaced_by_symlink_after_settings_load(
    tmp_path: Path,
) -> None:
    settings = _offline_settings(tmp_path)
    runtime_path = settings.offline_database_path
    assert runtime_path is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime_path.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeSettingsError, match="offline database path"):
        create_platform_engine(settings)

    assert not (outside / runtime_path.name).exists()


@pytest.mark.parametrize("driver", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgres_driver_aliases_normalize_to_exact_psycopg(
    tmp_path: Path,
    driver: str,
) -> None:
    settings = _database_settings(
        tmp_path,
        f"{driver}://service:{SENTINEL}@127.0.0.1:5432/platform",
    )

    engine = create_platform_engine(settings, application_name="aqa-engine-test")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.host == "127.0.0.1"
        assert engine.url.database == "platform"
        assert engine.hide_parameters is True
        assert SENTINEL not in str(engine.url)
        assert SENTINEL not in repr(engine.url)
        assert SENTINEL not in repr(engine)
    finally:
        engine.dispose()


def test_postgres_engine_has_bounded_pool_and_connection_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _database_settings(
        tmp_path,
        f"postgresql://service:{SENTINEL}@localhost/platform",
    )
    captured: dict[str, Any] = {}
    returned = cast(Engine, object())

    def capture_create_engine(url: URL, **kwargs: Any) -> Engine:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return returned

    monkeypatch.setattr(storage_engine, "sqlalchemy_create_engine", capture_create_engine)

    selected = create_platform_engine(settings, application_name="aqa.worker-1")

    assert selected is returned
    assert cast(URL, captured["url"]).drivername == "postgresql+psycopg"
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs == {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 5,
        "pool_recycle": 1_800,
        "pool_use_lifo": True,
        "hide_parameters": True,
        "connect_args": {
            "application_name": "aqa.worker-1",
            "connect_timeout": 5,
            "options": (
                "-c timezone=UTC -c statement_timeout=20000 -c lock_timeout=5000 "
                "-c idle_in_transaction_session_timeout=30000"
            ),
        },
    }


def test_postgres_read_only_engine_enforces_server_side_read_only_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _database_settings(
        tmp_path,
        f"postgresql://service:{SENTINEL}@localhost/platform",
    )
    captured: dict[str, Any] = {}
    returned = cast(Engine, object())

    def capture_create_engine(url: URL, **kwargs: Any) -> Engine:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return returned

    monkeypatch.setattr(storage_engine, "sqlalchemy_create_engine", capture_create_engine)

    selected = create_platform_read_only_engine(
        settings,
        application_name="aqa.audit-verifier",
    )

    assert selected is returned
    kwargs = cast(dict[str, Any], captured["kwargs"])
    connect_args = cast(dict[str, object], kwargs["connect_args"])
    assert connect_args["options"] == (
        "-c timezone=UTC -c statement_timeout=20000 -c lock_timeout=5000 "
        "-c idle_in_transaction_session_timeout=30000 "
        "-c default_transaction_read_only=on"
    )


@pytest.mark.parametrize(
    "override",
    [
        "application_name=untrusted",
        "dbname=other",
        "host=other.invalid",
        "hostaddr=203.0.113.1",
        "options=-c%20search_path%3Dpublic",
        "passfile=/tmp/pass",
        "password=other",
        "port=6432",
        "service=other",
        "servicefile=/tmp/service",
        "sslkey=/tmp/key",
        "target_session_attrs=read-write",
        "user=other",
    ],
)
def test_postgres_url_rejects_query_routing_and_client_overrides(
    tmp_path: Path,
    override: str,
) -> None:
    settings = _database_settings(
        tmp_path,
        f"postgresql://service:{SENTINEL}@localhost/platform?{override}",
    )

    with pytest.raises(RuntimeSettingsError, match="cannot override") as captured:
        create_platform_engine(settings)

    assert SENTINEL not in str(captured.value)


@pytest.mark.parametrize("sslmode", [None, "disable", "require", "verify-ca"])
def test_non_loopback_postgres_requires_full_tls_verification(
    tmp_path: Path,
    sslmode: str | None,
) -> None:
    query = "" if sslmode is None else f"?sslmode={sslmode}"
    settings = _database_settings(
        tmp_path,
        f"postgresql://service:{SENTINEL}@database.example.invalid/platform{query}",
    )

    with pytest.raises(RuntimeSettingsError, match="sslmode=verify-full"):
        create_platform_engine(settings)


def test_non_loopback_postgres_accepts_verify_full_without_connecting(tmp_path: Path) -> None:
    settings = _database_settings(
        tmp_path,
        (f"postgresql://service:{SENTINEL}@database.example.invalid/platform?sslmode=verify-full"),
    )

    engine = create_platform_engine(settings)
    try:
        assert engine.url.query == {"sslmode": "verify-full"}
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+pysqlite:////tmp/platform.sqlite3",
        "postgresql+psycopg2://service:password@localhost/platform",
        "postgresql://service@localhost/platform",
        "postgresql://:password@localhost/platform",
        "postgresql://service:password@localhost",
        "postgresql:///platform",
    ],
)
def test_postgres_url_rejects_wrong_dialect_or_missing_authority(
    tmp_path: Path,
    database_url: str,
) -> None:
    settings = _database_settings(tmp_path, database_url)

    with pytest.raises(RuntimeSettingsError):
        create_platform_engine(settings)


def test_malformed_database_url_is_rejected_without_secret_or_exception_context(
    tmp_path: Path,
) -> None:
    settings = _database_settings(
        tmp_path,
        f"postgresql://service:{SENTINEL}@localhost:not-a-port/platform",
    )

    with pytest.raises(RuntimeSettingsError) as captured:
        create_platform_engine(settings)

    rendered = "\n".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.args),
            "".join(traceback.format_exception(captured.value)),
        )
    )
    assert SENTINEL not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_load_failure_is_redacted_and_has_no_exception_context(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    missing_path = root / f"missing-{SENTINEL}"
    settings = _load_settings(
        root,
        {"AQA_DATABASE_URL_FILE": missing_path.as_posix()},
    )

    with pytest.raises(RuntimeSettingsError, match="could not load") as captured:
        create_platform_engine(settings)

    rendered = "".join(traceback.format_exception(captured.value))
    assert SENTINEL not in rendered
    assert missing_path.as_posix() not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_engine_initialization_failure_is_redacted_and_has_no_exception_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _database_settings(
        tmp_path,
        f"postgresql://service:{SENTINEL}@localhost/platform",
    )

    def fail_create_engine(*args: object, **kwargs: object) -> Engine:
        del args, kwargs
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(storage_engine, "sqlalchemy_create_engine", fail_create_engine)

    with pytest.raises(RuntimeSettingsError, match="could not initialize") as captured:
        create_platform_engine(settings)

    rendered = "".join(traceback.format_exception(captured.value))
    assert SENTINEL not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "application_name",
    ["", "1worker", "worker name", "worker\nname", "é-worker", "x" * 65],
)
def test_application_name_is_bounded_safe_ascii(
    tmp_path: Path,
    application_name: str,
) -> None:
    root = _application_root(tmp_path)
    settings = _load_settings(
        root,
        {"AQA_DATABASE_URL_FILE": (root / f"missing-{SENTINEL}").as_posix()},
    )

    with pytest.raises(RuntimeSettingsError, match="application name") as captured:
        create_platform_engine(settings, application_name=application_name)

    assert SENTINEL not in str(captured.value)
