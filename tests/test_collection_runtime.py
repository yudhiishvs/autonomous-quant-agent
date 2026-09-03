"""Offline tests for collector runtime configuration and read-only status."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

import adaptive_trader.collection.cli as collector_cli
from adaptive_trader.collection.postgres import normalize_postgres_url
from adaptive_trader.collection.repository import CollectorStatus
from adaptive_trader.collection.runtime import (
    CollectorEnvironment,
    migration_database_url_from_environment,
    parse_utc_boundary,
)


def test_runtime_environment_parses_hosted_postgres_and_redacts_password() -> None:
    environment = CollectorEnvironment.from_environment(
        {
            "APA_MARKET_DATA_DATABASE_URL": (
                "postgresql://collector:private-password@db.example.invalid/market_data"
                "?sslmode=verify-full"
            ),
            "APA_MARKET_DATA_HISTORY_START": "2025-01-02",
        }
    )

    assert environment.database_url.startswith("postgresql+psycopg://")
    assert environment.history_start == datetime(2025, 1, 2, tzinfo=UTC)
    assert "private-password" not in repr(environment)
    assert "private-password" not in str(environment)


def test_runtime_environment_rejects_non_postgres_and_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        CollectorEnvironment.from_environment(
            {"APA_MARKET_DATA_DATABASE_URL": "sqlite:///local.db"}
        )
    with pytest.raises(ValueError, match="UTC offset"):
        parse_utc_boundary("2026-09-03T12:00:00", field_name="start")


@pytest.mark.parametrize("sslmode", (None, "require", "verify-ca"))
def test_hosted_postgres_requires_full_tls_verification(sslmode: str | None) -> None:
    suffix = "" if sslmode is None else f"?sslmode={sslmode}"
    with pytest.raises(ValueError, match="sslmode=verify-full"):
        normalize_postgres_url(
            "postgresql://collector:password@database.example.invalid/market_data" + suffix
        )


def test_hosted_postgres_accepts_verify_full_and_loopback_without_tls() -> None:
    secured = normalize_postgres_url(
        "postgresql://collector:password@database.example.invalid/market_data?sslmode=verify-full"
    )
    loopback = normalize_postgres_url(
        "postgresql://collector:password@127.0.0.1:5432/collector_test"
    )

    assert secured.query["sslmode"] == "verify-full"
    assert loopback.host == "127.0.0.1"


@pytest.mark.parametrize(
    "override",
    (
        "host=database.example.invalid",
        "hostaddr=203.0.113.1",
        "port=6432",
        "service=external",
    ),
)
def test_loopback_postgres_rejects_query_parameter_routing_overrides(override: str) -> None:
    with pytest.raises(ValueError, match="cannot override connection routing"):
        normalize_postgres_url(
            "postgresql://collector:password@127.0.0.1:5432/collector_test?"
            f"{override}&sslmode=disable"
        )


def test_migration_database_url_is_separate_and_fully_validated() -> None:
    migration_url = migration_database_url_from_environment(
        {
            "APA_MARKET_DATA_DATABASE_URL": (
                "postgresql://runtime:runtime@database.example.invalid/market_data"
                "?sslmode=verify-full"
            ),
            "APA_MARKET_DATA_MIGRATION_DATABASE_URL": (
                "postgresql://owner:owner@database.example.invalid/market_data?sslmode=verify-full"
            ),
        }
    )

    assert migration_url.startswith("postgresql+psycopg://owner:")
    with pytest.raises(ValueError, match="APA_MARKET_DATA_MIGRATION_DATABASE_URL"):
        migration_database_url_from_environment(
            {
                "APA_MARKET_DATA_DATABASE_URL": (
                    "postgresql://runtime:runtime@database.example.invalid/market_data"
                    "?sslmode=verify-full"
                )
            }
        )


def test_verbose_logging_keeps_transport_libraries_above_debug() -> None:
    import logging

    logger_names = ("alpaca", "httpcore", "httpx", "urllib3", "websockets")
    previous_root = logging.getLogger().level
    previous_collection = logging.getLogger("adaptive_trader.collection").level
    previous_transports = {name: logging.getLogger(name).level for name in logger_names}
    try:
        collector_cli._configure_logging(verbose=True)

        assert logging.getLogger().level == logging.INFO
        assert logging.getLogger("adaptive_trader.collection").level == logging.DEBUG
        assert all(logging.getLogger(name).level >= logging.WARNING for name in logger_names)
    finally:
        logging.getLogger().setLevel(previous_root)
        logging.getLogger("adaptive_trader.collection").setLevel(previous_collection)
        for name, level in previous_transports.items():
            logging.getLogger(name).setLevel(level)


def test_parse_utc_boundary_normalizes_offsets() -> None:
    assert parse_utc_boundary("2026-09-03T10:30:00-04:00", field_name="start") == datetime(
        2026,
        9,
        3,
        14,
        30,
        tzinfo=UTC,
    )


def test_status_is_database_only_and_never_loads_alpaca_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRepository:
        def __init__(self, _database_url: str) -> None:
            self.closed = False

        def verify_schema(self) -> None:
            return None

        def status(self) -> CollectorStatus:
            return CollectorStatus(10, 12, 29, 0, 1, None, 1, 1)

        def close(self) -> None:
            self.closed = True

    environment = CollectorEnvironment(
        "postgresql+psycopg://collector:password@db.example.invalid/market_data?sslmode=verify-full"
    )
    monkeypatch.setattr(collector_cli, "_environment", lambda: environment)
    monkeypatch.setattr(collector_cli, "PostgresMarketDataRepository", FakeRepository)
    monkeypatch.setattr(collector_cli, "require_database_at_head", lambda _url: None)

    def forbid_credentials() -> None:
        raise AssertionError("status must not load Alpaca credentials")

    monkeypatch.setattr(
        collector_cli.AlpacaDataCredentials,
        "from_environment",
        forbid_credentials,
    )

    result = CliRunner().invoke(collector_cli.app, ["status"])

    assert result.exit_code == 0
    assert '"status": "ok"' in result.stdout
    assert '"symbol_count": 29' in result.stdout
    assert '"active_lease_count": 1' in result.stdout
    assert '"active_run_count": 1' in result.stdout
    assert "password" not in result.stdout


def test_migrate_uses_only_the_schema_owner_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    migration_url = (
        "postgresql+psycopg://owner:password@database.example.invalid/market_data"
        "?sslmode=verify-full"
    )
    monkeypatch.setattr(
        collector_cli,
        "migration_database_url_from_environment",
        lambda: migration_url,
    )
    monkeypatch.setattr(
        collector_cli,
        "upgrade_database",
        lambda url: calls.append(("upgrade", url)),
    )
    monkeypatch.setattr(
        collector_cli,
        "require_database_at_head",
        lambda url: calls.append(("verify", url)),
    )

    def forbid_runtime_environment() -> None:
        raise AssertionError("migrate must not load the runtime database URL")

    monkeypatch.setattr(collector_cli, "_environment", forbid_runtime_environment)

    result = CliRunner().invoke(collector_cli.app, ["migrate"])

    assert result.exit_code == 0
    assert calls == [("upgrade", migration_url), ("verify", migration_url)]


@pytest.mark.parametrize(
    ("active_leases", "running_runs", "active_runs", "expected_exit"),
    ((1, 1, 1, 0), (0, 1, 0, 1), (1, 1, 0, 1), (1, 0, 0, 1)),
)
def test_readiness_requires_an_active_lease_and_run(
    monkeypatch: pytest.MonkeyPatch,
    active_leases: int,
    running_runs: int,
    active_runs: int,
    expected_exit: int,
) -> None:
    class FakeRepository:
        def __init__(self, _database_url: str) -> None:
            pass

        def verify_schema(self) -> None:
            return None

        def is_ready(self, *, lease_name: str) -> bool:
            assert lease_name == "market-data-collector.v1"
            return active_leases == 1 and running_runs == 1 and active_runs == 1

        def status(self) -> CollectorStatus:
            raise AssertionError("readiness must not run full-table status queries")

        def close(self) -> None:
            return None

    environment = CollectorEnvironment(
        "postgresql+psycopg://collector:password@127.0.0.1/collector_test"
    )
    monkeypatch.setattr(collector_cli, "_environment", lambda: environment)
    monkeypatch.setattr(collector_cli, "PostgresMarketDataRepository", FakeRepository)
    monkeypatch.setattr(collector_cli, "require_database_at_head", lambda _url: None)

    result = CliRunner().invoke(collector_cli.app, ["ready"])

    assert result.exit_code == expected_exit
    if expected_exit == 0:
        assert "collector is ready" in result.stdout
    else:
        assert "collector is not active" in result.stderr


def test_collection_failure_does_not_echo_exception_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRepository:
        def __init__(self, _database_url: str) -> None:
            pass

        def close(self) -> None:
            return None

    environment = CollectorEnvironment(
        "postgresql+psycopg://collector:database-secret@127.0.0.1/collector_test"
    )
    monkeypatch.setattr(collector_cli, "_environment", lambda: environment)
    monkeypatch.setattr(collector_cli, "PostgresMarketDataRepository", FakeRepository)
    monkeypatch.setattr(collector_cli, "require_database_at_head", lambda _url: None)

    def fail_credentials() -> None:
        raise RuntimeError("alpaca-secret and database-secret")

    monkeypatch.setattr(
        collector_cli.AlpacaDataCredentials,
        "from_environment",
        fail_credentials,
    )

    result = CliRunner().invoke(
        collector_cli.app,
        [
            "backfill",
            "--start",
            "2026-09-01T13:30:00Z",
            "--end",
            "2026-09-01T20:00:00Z",
        ],
    )

    assert result.exit_code == 1
    assert "Historical collection failed (RuntimeError)" in result.stderr
    assert "alpaca-secret" not in result.output
    assert "database-secret" not in result.output
