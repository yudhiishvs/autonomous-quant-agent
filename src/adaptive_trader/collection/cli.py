"""Operational CLI for the data-only Alpaca market-data collector."""

from __future__ import annotations

import json
import logging
import os
import signal
import stat
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from adaptive_trader.collection.alpaca import AlpacaHistoricalBarSource, AlpacaLiveBarSource
from adaptive_trader.collection.credentials import AlpacaDataCredentials
from adaptive_trader.collection.migrations import require_database_at_head
from adaptive_trader.collection.operations import (
    collection_experiment,
    ensure_configuration,
    health_snapshot,
)
from adaptive_trader.collection.postgres import PostgresMarketDataRepository
from adaptive_trader.collection.recovery import next_gap_repair, restore_canonical_projection
from adaptive_trader.collection.runtime import (
    CollectorEnvironment,
    parse_utc_boundary,
    require_file_backed_data_environment,
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
            experiment=collection_experiment(),
        )
    except Exception as error:
        _fail(f"Database migration failed ({type(error).__name__})")
    typer.echo("Platform database is at the expected migration revision.")


@app.command("status")
def status() -> None:
    """Read collector health and storage counts without loading Alpaca credentials."""

    environment = _environment()
    repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
    try:
        require_database_at_head(environment.database_url)
        repository.verify_schema()
        snapshot = repository.status()
        health = health_snapshot(repository.engine, now=datetime.now(UTC))
    except Exception as exc:
        _fail(f"Market-data status unavailable ({type(exc).__name__})")
    finally:
        repository.close()
    typer.echo(
        json.dumps(
            {
                **health,
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
    """Check collector ownership, acknowledged subscription and recovered data processing."""

    environment = _environment()
    repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
    try:
        require_database_at_head(environment.database_url)
        repository.verify_schema()
        active = repository.is_ready(lease_name=CollectorServiceConfig().lease_name)
        if active:
            active = health_snapshot(repository.engine, now=datetime.now(UTC))["service_ready"]
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
    repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
    historical_source: AlpacaHistoricalBarSource | None = None
    try:
        require_database_at_head(environment.database_url)
        credentials = AlpacaDataCredentials.from_environment()
        experiment = collection_experiment()
        history_start = ensure_configuration(
            repository.engine,
            experiment=experiment,
            history_start=environment.history_start or start_at,
        )
        from adaptive_trader.collection.derived import DerivedDataProcessor

        processor = DerivedDataProcessor(
            repository.engine,
            experiment=experiment,
            history_start=history_start,
            clock=lambda: datetime.now(UTC),
            lease_validator=lambda: service.validate_active_lease(),
            transaction_guard=lambda connection: repository.validate_ownership(
                connection, lease=service.require_active_lease()
            ),
        )
        historical_source = AlpacaHistoricalBarSource(credentials)
        service = CollectorService(
            repository,
            historical_source,
            maintenance=processor.drain_backfill,
            prepare=lambda run_id: restore_canonical_projection(
                repository,
                current_lease=service.require_active_lease,
                run_id=run_id,
                history_start=history_start,
                now=datetime.now(UTC),
            ),
        )
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


@app.command("collect-once")
def collect_once(verbose: bool = False) -> None:
    """Catch up durable historical data once using only dedicated secret-file references."""

    _configure_logging(verbose)
    try:
        require_file_backed_data_environment()
    except ValueError as error:
        _fail(str(error))
    environment = _environment()
    repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
    historical_source: AlpacaHistoricalBarSource | None = None
    try:
        require_database_at_head(environment.database_url)
        repository.verify_schema()
        experiment = collection_experiment()
        history_start = ensure_configuration(
            repository.engine,
            experiment=experiment,
            history_start=environment.history_start,
        )
        from adaptive_trader.collection.derived import DerivedDataProcessor

        processor = DerivedDataProcessor(
            repository.engine,
            experiment=experiment,
            history_start=history_start,
            clock=lambda: datetime.now(UTC),
            lease_validator=lambda: service.validate_active_lease(),
            transaction_guard=lambda connection: repository.validate_ownership(
                connection, lease=service.require_active_lease()
            ),
        )
        historical_source = AlpacaHistoricalBarSource(AlpacaDataCredentials.from_environment())
        service = CollectorService(
            repository,
            historical_source,
            maintenance=processor.drain_backfill,
            prepare=lambda run_id: restore_canonical_projection(
                repository,
                current_lease=service.require_active_lease,
                run_id=run_id,
                history_start=history_start,
                now=datetime.now(UTC),
            ),
            repair_window=lambda: next_gap_repair(
                repository,
                experiment=experiment,
                history_start=history_start,
                now=datetime.now(UTC),
            ),
        )
        restore_signals = _with_signals(service)
        try:
            counters = service.collect_once(history_start=history_start)
        finally:
            restore_signals()
        health = health_snapshot(repository.engine, now=datetime.now(UTC))
    except Exception as error:
        _fail(f"One-shot collection failed ({type(error).__name__})")
    finally:
        if historical_source is not None:
            with suppress(Exception):
                historical_source.close()
        repository.close()
    typer.echo(
        json.dumps({"status": "completed", "counters": counters, "health": health}, sort_keys=True)
    )


@app.command("run")
def run(
    start_if_empty: str | None = typer.Option(
        None,
        "--start-if-empty",
        help="First-run ISO-8601 start; defaults to AQA_MARKET_DATA_HISTORY_START.",
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
    repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
    historical_source: AlpacaHistoricalBarSource | None = None
    try:
        require_database_at_head(environment.database_url)
        credentials = AlpacaDataCredentials.from_environment()
        experiment = collection_experiment()
        history_start = ensure_configuration(
            repository.engine,
            experiment=experiment,
            history_start=initial_start,
        )
        from adaptive_trader.collection.derived import DerivedDataProcessor

        processor = DerivedDataProcessor(
            repository.engine,
            experiment=experiment,
            history_start=history_start,
            clock=lambda: datetime.now(UTC),
            lease_validator=lambda: service.validate_active_lease(),
            transaction_guard=lambda connection: repository.validate_ownership(
                connection, lease=service.require_active_lease()
            ),
        )
        historical_source = AlpacaHistoricalBarSource(credentials)
        service = CollectorService(
            repository,
            historical_source,
            AlpacaLiveBarSource(
                credentials,
                state_handler=lambda state: service.stream_state_changed(state),
            ),
            maintenance=processor.drain,
            prepare=lambda run_id: restore_canonical_projection(
                repository,
                current_lease=service.require_active_lease,
                run_id=run_id,
                history_start=history_start,
                now=datetime.now(UTC),
            ),
            repair_window=lambda: next_gap_repair(
                repository,
                experiment=experiment,
                history_start=history_start,
                now=datetime.now(UTC),
            ),
        )
        restore_signals = _with_signals(service)
        try:
            counters = service.run(start_if_empty=history_start)
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


@app.command("snapshot")
def snapshot(
    start: Annotated[str, typer.Option(help="Inclusive UTC range start (ISO-8601).")],
    end: Annotated[str, typer.Option(help="Exclusive UTC range end, at most 31 days after start.")],
    artifact_root: Annotated[
        Path, typer.Option(help="Trusted persistent root for immutable Parquet artifacts.")
    ],
    source_git_commit: Annotated[
        str, typer.Option(help="Full 40-character Git revision used to produce this data snapshot.")
    ],
    uv_lock_sha256: Annotated[str, typer.Option(help="SHA-256 of the exact source uv.lock file.")],
    metadata_file: Annotated[
        Path | None,
        typer.Option(help="Reviewed listing/corporate-action evidence JSON, at most 64 KiB."),
    ] = None,
    diagnostic: Annotated[
        bool,
        typer.Option(help="Explicitly export non-promotable data with its unresolved conditions."),
    ] = False,
    dirty_worktree: Annotated[
        bool,
        typer.Option(
            "--dirty-worktree/--clean-worktree",
            help="Declare whether source has uncommitted changes; defaults to dirty.",
        ),
    ] = True,
) -> None:
    """Freeze canonical research/context minutes and their source evidence without provider access."""

    from adaptive_trader.collection.snapshots import (
        freeze_collection_snapshot,
        parse_snapshot_metadata,
    )

    environment = _environment()
    start_at = _boundary(start, name="--start", fallback=None)
    end_at = _boundary(end, name="--end", fallback=None)
    repository = PostgresMarketDataRepository(environment.database_url, canonical=True)
    try:
        require_database_at_head(environment.database_url)
        repository.verify_schema()
        metadata = None
        if metadata_file is not None:
            descriptor = os.open(metadata_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("snapshot metadata must be a regular file")
                payload = stream.read(65_537)
            if len(payload) > 65_536:
                raise ValueError("snapshot metadata exceeds 64 KiB")
            metadata = parse_snapshot_metadata(json.loads(payload))
        frozen, registration = freeze_collection_snapshot(
            repository.engine,
            experiment=collection_experiment(),
            artifact_root=artifact_root,
            range_start=start_at,
            range_end=end_at,
            source_git_commit=source_git_commit,
            dirty_worktree=dirty_worktree,
            uv_lock_hash=uv_lock_sha256,
            created_at=datetime.now(UTC),
            diagnostic=diagnostic,
            metadata_evidence=metadata,
        )
    except Exception as exc:
        _fail(f"Canonical snapshot failed ({type(exc).__name__})")
    finally:
        repository.close()
    typer.echo(
        json.dumps(
            {
                "dataset_id": frozen.dataset_id,
                "artifact_id": frozen.artifact_id,
                "manifest_hash": frozen.manifest_hash,
                "status": frozen.status.value,
                "promotable": frozen.promotable,
                "registered": registration.created,
                "artifact_root": str(artifact_root.absolute()),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
