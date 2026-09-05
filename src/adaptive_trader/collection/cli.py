"""Operational CLI for the data-only Alpaca market-data collector."""

from __future__ import annotations

import json
import logging
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from adaptive_trader.collection.alpaca import AlpacaHistoricalBarSource, AlpacaLiveBarSource
from adaptive_trader.collection.credentials import AlpacaDataCredentials
from adaptive_trader.collection.migrations import require_database_at_head
from adaptive_trader.collection.postgres import PostgresMarketDataRepository
from adaptive_trader.collection.runtime import (
    CollectorEnvironment,
    parse_utc_boundary,
)
from adaptive_trader.collection.service import CollectorService, CollectorServiceConfig
from adaptive_trader.collection.universe import COLLECTION_UNIVERSE_V1
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from adaptive_trader.platform.storage.migration_runner import migrate_platform_database

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Collect and persist Alpaca IEX minute bars without trading access.",
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("adaptive_trader.collection").setLevel(
        logging.DEBUG if verbose else logging.INFO
    )
    for logger_name in ("alpaca", "httpcore", "httpx", "urllib3", "websockets"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _environment() -> CollectorEnvironment:
    try:
        return CollectorEnvironment.from_environment()
    except ValueError as exc:
        _fail(str(exc))


def _boundary(value: str | None, *, name: str, fallback: datetime | None) -> datetime:
    if value is None:
        if fallback is None:
            _fail(f"{name} is required because this database has no initial coverage checkpoint")
        return fallback
    try:
        return parse_utc_boundary(value, field_name=name)
    except ValueError as exc:
        _fail(str(exc))


def _with_signals(service: CollectorService) -> Callable[[], None]:
    previous: dict[int, Any] = {}

    def stop(_signum: int, _frame: object) -> None:
        service.request_stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, stop)

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


@app.command("migrate")
def migrate(
    database_url_file: Annotated[
        Path,
        typer.Option(
            "--database-url-file",
            help="Owner-private file containing the target PostgreSQL URL.",
        ),
    ],
    application_root: Annotated[
        Path | None,
        typer.Option(
            "--application-root",
            help="Trusted root containing role passwords; defaults to the current directory.",
        ),
    ] = None,
    bootstrap_admin_database_url_file: Annotated[
        Path | None,
        typer.Option(
            "--bootstrap-admin-database-url-file",
            help=(
                "Owner-private cluster-administrator URL file, required only to finalize a "
                "legacy role handoff."
            ),
        ),
    ] = None,
) -> None:
    """Apply migrations through the bounded platform-role handoff."""

    try:
        database_url = load_secret_file(
            database_url_file,
            source=SecretFileVariable.DATABASE_URL,
        )
        selected_root = Path(
            os.path.abspath(os.fspath(Path.cwd() if application_root is None else application_root))
        )
        bootstrap_admin_database_url = (
            None
            if bootstrap_admin_database_url_file is None
            else load_secret_file(
                bootstrap_admin_database_url_file,
                source=SecretFileVariable.DATABASE_URL,
            )
        )
        migrate_platform_database(
            database_url,
            application_root=selected_root,
            bootstrap_admin_database_url=bootstrap_admin_database_url,
        )
    except Exception as error:
        _fail(f"Database migration failed ({type(error).__name__})")
    typer.echo("Platform database is at the expected migration revision.")


@app.command("status")
def status() -> None:
    """Read collector health and storage counts without loading Alpaca credentials."""

    environment = _environment()
    repository = PostgresMarketDataRepository(environment.database_url)
    try:
        require_database_at_head(environment.database_url)
        repository.verify_schema()
        snapshot = repository.status()
    except Exception as exc:
        _fail(f"Market-data status unavailable ({type(exc).__name__})")
    finally:
        repository.close()
    typer.echo(
        json.dumps(
            {
                "status": "ok",
                "universe_version": COLLECTION_UNIVERSE_V1.SCHEMA_VERSION,
                "universe_hash": COLLECTION_UNIVERSE_V1.universe_hash,
                "symbol_count": len(COLLECTION_UNIVERSE_V1.symbols),
                "current_bar_count": snapshot.current_bar_count,
                "observation_count": snapshot.observation_count,
                "checkpoint_count": snapshot.checkpoint_count,
                "open_gap_count": snapshot.open_gap_count,
                "running_run_count": snapshot.running_run_count,
                "active_lease_count": snapshot.active_lease_count,
                "active_run_count": snapshot.active_run_count,
                "latest_receipt_timestamp_utc": (
                    None
                    if snapshot.latest_receipt_timestamp_utc is None
                    else snapshot.latest_receipt_timestamp_utc.isoformat()
                ),
            },
            sort_keys=True,
        )
    )


@app.command("ready")
def ready() -> None:
    """Run a lightweight check for the canonical active collector lease and run."""

    environment = _environment()
    repository = PostgresMarketDataRepository(environment.database_url)
    try:
        require_database_at_head(environment.database_url)
        repository.verify_schema()
        active = repository.is_ready(lease_name=CollectorServiceConfig().lease_name)
    except Exception as exc:
        _fail(f"Market-data readiness unavailable ({type(exc).__name__})")
    finally:
        repository.close()
    if not active:
        _fail("Market-data collector is not active")
    typer.echo("Market-data collector is ready.")


@app.command("backfill")
def backfill(
    start: str | None = typer.Option(
        None,
        "--start",
        help="ISO-8601 start; defaults to APA_MARKET_DATA_HISTORY_START.",
    ),
    end: str | None = typer.Option(
        None,
        "--end",
        help="Optional exclusive ISO-8601 end; defaults to the safe completed-bar cutoff.",
    ),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Fetch and persist a finite historical interval for all 29 symbols."""

    _configure_logging(verbose)
    environment = _environment()
    start_at = _boundary(start, name="--start", fallback=environment.history_start)
    end_at = None if end is None else _boundary(end, name="--end", fallback=None)
    repository = PostgresMarketDataRepository(environment.database_url)
    historical_source: AlpacaHistoricalBarSource | None = None
    try:
        require_database_at_head(environment.database_url)
        credentials = AlpacaDataCredentials.from_environment()
        historical_source = AlpacaHistoricalBarSource(credentials)
        service = CollectorService(repository, historical_source)
        restore_signals = _with_signals(service)
        try:
            counters = service.backfill(start=start_at, end=end_at)
        finally:
            restore_signals()
    except Exception as exc:
        _fail(f"Historical collection failed ({type(exc).__name__})")
    finally:
        if historical_source is not None:
            with suppress(Exception):
                historical_source.close()
        repository.close()
    typer.echo(json.dumps({"status": "completed", "counters": counters}, sort_keys=True))


@app.command("run")
def run(
    start_if_empty: str | None = typer.Option(
        None,
        "--start-if-empty",
        help="First-run ISO-8601 start; defaults to APA_MARKET_DATA_HISTORY_START.",
    ),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Catch up history and continuously persist real-time bars and corrections."""

    _configure_logging(verbose)
    environment = _environment()
    initial_start = (
        environment.history_start
        if start_if_empty is None
        else _boundary(start_if_empty, name="--start-if-empty", fallback=None)
    )
    repository = PostgresMarketDataRepository(environment.database_url)
    historical_source: AlpacaHistoricalBarSource | None = None
    try:
        require_database_at_head(environment.database_url)
        credentials = AlpacaDataCredentials.from_environment()
        historical_source = AlpacaHistoricalBarSource(credentials)
        service = CollectorService(
            repository,
            historical_source,
            AlpacaLiveBarSource(credentials),
        )
        restore_signals = _with_signals(service)
        try:
            counters = service.run(start_if_empty=initial_start)
        finally:
            restore_signals()
    except Exception as exc:
        _fail(f"Continuous collection failed ({type(exc).__name__})")
    finally:
        if historical_source is not None:
            with suppress(Exception):
                historical_source.close()
        repository.close()
    typer.echo(json.dumps({"status": "stopped", "counters": counters}, sort_keys=True))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
