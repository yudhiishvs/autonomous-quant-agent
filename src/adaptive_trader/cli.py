"""Operational command-line interface for research and forward paper operation.

Imports of network-facing adapters are intentionally lazy.  Configuration,
historical research, replay, local status, halt/resume, and report generation do
not construct an Alpaca client merely because this module was imported.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time as time_module
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, TypeVar

import click
import typer
from rich.console import Console
from rich.table import Table

from adaptive_trader.constants import (
    IEX_FEED_BANNER,
    PAPER_API_KEY_ENV,
    PAPER_FLATTEN_ACKNOWLEDGEMENT,
    PAPER_ORDER_ACKNOWLEDGEMENT,
    PAPER_ORDER_ENABLEMENT_ENV,
    PAPER_RESUME_ACKNOWLEDGEMENT,
    PAPER_SECRET_KEY_ENV,
    PAPER_TRADING_BANNER,
    feed_banner,
)

LOGGER = logging.getLogger(__name__)
PROGRAM_NAME = "adaptive-portfolio-agent"
HELP_TEXT = f"""{PAPER_TRADING_BANNER}

Research, deterministic replay, live observation, and Alpaca paper-account
operations. Real-money execution is structurally absent.

Configured IEX disclosure:

{IEX_FEED_BANNER}

The exact SIP disclosure is emitted only after current entitlement verification.
"""

app = typer.Typer(
    name=PROGRAM_NAME,
    help=HELP_TEXT,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Validated command paths and configuration."""

    config_path: Path
    project_root: Path
    config: Any
    database_path: Path
    output_directory: Path


@dataclass(slots=True)
class ServiceHandle:
    """Live service plus resources owned by the CLI invocation."""

    service: Any
    database: Any


class _ZeroMutationBroker:
    """Delegate broker reads/streams while making every mutation uncallable.

    Observer and real-data dry-run CLI paths opt into this guard before the
    service and its reconciliation/order components are constructed.  An
    implementation defect that attempts a mutation therefore fails closed and
    becomes visible in the bounded audit instead of reaching Alpaca.
    """

    def __init__(self, delegate: Any) -> None:
        object.__setattr__(self, "_ZeroMutationBroker__delegate", delegate)
        self.mutation_attempts: list[str] = []

    def __getattribute__(self, name: str) -> Any:
        if name in {"_delegate", "__delegate", "_ZeroMutationBroker__delegate"}:
            mutation_attempts = object.__getattribute__(self, "mutation_attempts")
            mutation_attempts.append(f"raw_adapter_attribute:{name}")
            from adaptive_trader.exceptions import SafetyViolation

            raise SafetyViolation("Raw broker delegate access is prohibited")
        return object.__getattribute__(self, name)

    @property
    def paper_only(self) -> bool:
        delegate = object.__getattribute__(self, "_ZeroMutationBroker__delegate")
        return bool(getattr(delegate, "paper_only", False))

    @property
    def underlying_paper_adapter_verified(self) -> bool:
        from adaptive_trader.broker import AlpacaPaperBroker

        delegate = object.__getattribute__(self, "_ZeroMutationBroker__delegate")
        return isinstance(delegate, AlpacaPaperBroker)

    def __getattr__(self, name: str) -> Any:
        normalized = name.lower().strip("_")
        forbidden_names = {
            "api",
            "client",
            "rest_client",
            "session",
            "trade_client",
            "trading_api",
            "trading_client",
        }
        mutation_prefixes = (
            "cancel",
            "close",
            "create_order",
            "delete_order",
            "liquidate",
            "replace",
            "submit",
        )
        if normalized in forbidden_names or normalized.endswith("_client"):
            self._refuse(f"raw_adapter_attribute:{name}")
        if normalized.startswith(mutation_prefixes):
            self._refuse(f"mutation_attribute:{name}")
        delegate = object.__getattribute__(self, "_ZeroMutationBroker__delegate")
        return getattr(delegate, name)

    def _refuse(self, operation: str) -> None:
        from adaptive_trader.exceptions import SafetyViolation

        self.mutation_attempts.append(operation)
        raise SafetyViolation(f"{operation} is prohibited by the zero-mutation broker guard")

    def submit_order(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self._refuse("submit_order")

    def cancel_all_orders(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._refuse("cancel_all_orders")

    def cancel_order(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._refuse("cancel_order")

    def replace_order(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self._refuse("replace_order")

    def close_position(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self._refuse("close_position")

    def close_all_positions(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self._refuse("close_all_positions")

    def liquidate(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self._refuse("liquidate")


T = TypeVar("T")


def _console(*, stderr: bool = False) -> Console:
    return Console(
        file=sys.stderr if stderr else sys.stdout,
        color_system=None,
        highlight=False,
        markup=False,
        soft_wrap=True,
    )


def _print_paper_banner() -> None:
    typer.echo(PAPER_TRADING_BANNER)


def _print_feed_disclosure(config: Any) -> None:
    configured_feed = str(config.market_data.feed).upper()
    provider = str(config.market_data.provider).lower()
    if configured_feed == "SIP":
        typer.echo(
            "SIP FEED CONFIGURED — ENTITLEMENT UNCONFIRMED; the service will fail closed "
            "and will never fall back to IEX."
        )
    else:
        typer.echo(feed_banner(configured_feed))
    if provider != "alpaca":
        typer.echo(
            f"DATA MODE: {provider.upper()} — the feed line above is the configured "
            "reference disclosure, not a claim of a live connection."
        )


def _print_banners(config: Any) -> None:
    _print_paper_banner()
    _print_feed_disclosure(config)


def _find_project_root(config_path: Path) -> Path:
    resolved = config_path.resolve()
    for candidate in (resolved.parent, *resolved.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


def _resolve_project_path(root: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else root / path


def _load_context(config_path: Path) -> CommandContext:
    _print_paper_banner()
    from adaptive_trader.config import load_config

    resolved = config_path.expanduser().resolve()
    config = load_config(resolved)
    _print_feed_disclosure(config)
    root = _find_project_root(resolved)
    return CommandContext(
        config_path=resolved,
        project_root=root,
        config=config,
        database_path=_resolve_project_path(root, config.project.database_path),
        output_directory=_resolve_project_path(root, config.project.output_directory),
    )


def _safe_error(exc: BaseException) -> str:
    secrets = (
        os.environ.get(PAPER_API_KEY_ENV, ""),
        os.environ.get(PAPER_SECRET_KEY_ENV, ""),
    )
    try:
        from adaptive_trader.logging_config import redact

        return redact(str(exc), secrets)
    except Exception:  # pragma: no cover - last-resort CLI redaction boundary
        text = str(exc)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text


def _dispatch(operation: Callable[[], int]) -> None:
    try:
        exit_code = int(operation())
    except KeyboardInterrupt:
        typer.echo("Interrupted; graceful service shutdown was requested.", err=True)
        raise typer.Exit(130) from None
    except Exception as exc:  # CLI boundary: render a redacted operational failure.
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            LOGGER.debug("Command failed", exc_info=True)
        typer.echo(f"ERROR: {_safe_error(exc)}", err=True)
        raise typer.Exit(2) from None
    if exit_code:
        raise typer.Exit(exit_code)


def _configure_console_logging(verbose: bool) -> None:
    from adaptive_trader.logging_config import configure_logging

    configure_logging(
        Path.cwd() / "runtime",
        level=logging.DEBUG if verbose else logging.INFO,
        secrets=(
            os.environ.get(PAPER_API_KEY_ENV, ""),
            os.environ.get(PAPER_SECRET_KEY_ENV, ""),
        ),
    )


@app.callback()
def _application(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Enable detailed redacted logs.")
    ] = False,
) -> None:
    """Configure non-secret console logging for the selected command."""

    # Doctor is a strict read-only diagnostic. It must not create runtime/log
    # directories merely by being invoked; its user-facing errors are redacted
    # directly at the CLI boundary.
    if ctx.invoked_subcommand not in {"doctor", "observer-readiness"}:
        _configure_console_logging(verbose)


def _portfolio_metrics(run: Any, config: Any) -> Any:
    from adaptive_trader.metrics import calculate_metrics

    hard_stops = sum(
        1
        for action in run.risk_actions
        if action.control == "hard_drawdown" and action.details.get("newly_latched", False)
    )
    return calculate_metrics(
        returns=run.daily["daily_return"],
        equity_curve=run.daily["equity"],
        gross_exposure=run.daily["gross_exposure"],
        cash_allocation=run.daily["cash_weight"],
        turnover=run.daily["turnover"],
        transaction_costs=run.daily["transaction_cost"],
        number_of_rebalances=len(run.rebalances),
        number_of_risk_interventions=len(run.risk_actions),
        number_of_hard_stop_events=hard_stops,
        annualization_factor=config.backtest.annualization_factor,
    )


def _print_backtest_summary(
    suite: Any, artifacts: Mapping[str, Path], output_directory: Path
) -> None:
    typer.echo(f"Date range: {suite.start_date.date()} to {suite.end_date.date()}")
    typer.echo(f"Assets: {', '.join(suite.config.data.tickers)}")
    typer.echo(f"Data source: {suite.market_data.source}")
    typer.echo(f"Output directory: {output_directory.resolve()}")
    table = Table(title="Historical simulation summary")
    table.add_column("Portfolio")
    table.add_column("Total return", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("Maximum drawdown", justify="right")
    total_interventions = 0
    hard_stop_triggered = False
    for name, run in suite.runs.items():
        metrics = _portfolio_metrics(run, suite.config)
        total_interventions += metrics.number_of_risk_interventions
        hard_stop_triggered = hard_stop_triggered or metrics.number_of_hard_stop_events > 0
        table.add_row(
            str(name),
            f"{metrics.total_return:.2%}",
            f"{metrics.sharpe_ratio:.3f}",
            f"{metrics.maximum_drawdown:.2%}",
        )
    _console().print(table)
    typer.echo(f"Risk interventions: {total_interventions}")
    typer.echo(f"Hard stop triggered: {'yes' if hard_stop_triggered else 'no'}")
    if "report.md" in artifacts:
        typer.echo(f"Report: {artifacts['report.md'].resolve()}")


def _synthetic_market_data(context: CommandContext, seed: int) -> Any:
    from adaptive_trader.data import generate_synthetic_market_data

    config = context.config
    configured_start = datetime.fromisoformat(config.data.start_date)
    synthetic_start = (
        (configured_start - timedelta(days=int(config.market_data.historical_calendar_days)))
        .date()
        .isoformat()
    )
    synthetic_end = config.data.end_date or "2025-12-31"
    return generate_synthetic_market_data(
        config.data.tickers,
        start_date=synthetic_start,
        end_date=synthetic_end,
        seed=seed,
    )


def _persist_historical_receipts(
    context: CommandContext,
    suite: Any,
    receipt_path: Path,
    *,
    application_run_id: str | None = None,
) -> int:
    """Persist run-scoped copies of exported receipts in the immutable SQLite audit."""

    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    repository = AuditRepository(database)
    manages_run = application_run_id is None
    run_id = application_run_id or repository.start_run(
        mode="historical_backtest",
        configuration=context.config,
        market_data_feed=str(suite.market_data.feed),
        strategy_name="adaptive_portfolio_historical_suite",
        strategy_version="historical-research-v1",
    )
    stored = 0
    shutdown_reason = "historical_backtest_persistence_failed"
    try:
        for raw_line in receipt_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            receipt = json.loads(raw_line)
            if not isinstance(receipt, Mapping):
                raise ValueError("Historical decision receipt must be a JSON object")
            source_decision_id = str(receipt["decision_id"])
            if str(receipt.get("run_id")) == run_id:
                decision_id = source_decision_id
                database_receipt = dict(receipt)
            else:
                decision_id = hashlib.sha256(f"{run_id}:{source_decision_id}".encode()).hexdigest()
                database_receipt = {
                    **receipt,
                    "source_decision_id": source_decision_id,
                    "source_run_name": receipt.get("run_id"),
                    "decision_id": decision_id,
                    "run_id": run_id,
                }
            database_receipt["application_run_id"] = run_id
            session_value = str(receipt.get("market_session_date") or receipt.get("execution_date"))
            session_date = datetime.fromisoformat(session_value).date()
            scheduled_value = receipt.get("scheduled_evaluation_timestamp")
            scheduled_at = (
                None
                if scheduled_value is None
                else datetime.fromisoformat(str(scheduled_value).replace("Z", "+00:00"))
            )
            claimed_id, _ = repository.claim_rebalance(
                run_id=run_id,
                idempotency_key=f"historical:{run_id}:{decision_id}",
                session_date=session_date,
                strategy_version="historical-research-v1",
                mode="historical_backtest",
                scheduled_at=scheduled_at,
                decision_id=decision_id,
            )
            repository.complete_rebalance(
                claimed_id,
                status=str(database_receipt.get("risk_status") or "completed"),
                payload=database_receipt,
                skip_reason=(
                    None
                    if database_receipt.get("skip_reason") is None
                    else str(database_receipt["skip_reason"])
                ),
            )
            repository.store_decision_receipt(
                run_id=run_id,
                decision_id=claimed_id,
                payload=database_receipt,
            )
            stored += 1
        shutdown_reason = "historical_backtest_complete"
        return stored
    finally:
        if manages_run:
            repository.end_run(run_id, shutdown_reason)
        database.close()


def _start_historical_audit(context: CommandContext, *, market_data_feed: str) -> str:
    """Create the application run before computing receipts so all identities agree."""

    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        run_id = repository.start_run(
            mode="historical_backtest",
            configuration=context.config,
            market_data_feed=market_data_feed,
            strategy_name="adaptive_portfolio_historical_suite",
            strategy_version="historical-research-v1",
        )
        return run_id
    finally:
        database.close()


def _end_historical_audit(context: CommandContext, run_id: str, reason: str) -> None:
    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    try:
        AuditRepository(database).end_run(run_id, reason)
    finally:
        database.close()


def _run_backtest_command(
    config_path: Path,
    *,
    synthetic: bool,
    synthetic_seed: int,
    output: Path | None,
) -> int:
    context = _load_context(config_path)
    config = context.config
    provider = str(config.market_data.provider).lower()
    if synthetic or provider in {"synthetic", "replay"}:
        reason = "--synthetic" if synthetic else f"market_data.provider={provider}"
        typer.echo(
            f"Using deterministic synthetic daily data because {reason}. "
            "This is engineering evidence, not market evidence."
        )
        seed = synthetic_seed if synthetic else int(config.replay.deterministic_seed)
        market_data = _synthetic_market_data(context, seed)
    else:
        from adaptive_trader.data import load_market_data

        market_data = load_market_data(config, project_root=context.project_root)

    from adaptive_trader.backtest import run_backtest_suite
    from adaptive_trader.reporting import generate_outputs

    audit_run_id = _start_historical_audit(
        context,
        market_data_feed=str(market_data.feed),
    )
    shutdown_reason = "historical_backtest_failed"
    try:
        suite = run_backtest_suite(config, market_data, audit_run_id=audit_run_id)
        if output is None:
            output_directory = context.output_directory
        else:
            output_directory = _resolve_project_path(context.project_root, output)
        artifacts = generate_outputs(suite, output_directory=output_directory)
        persisted_receipts = _persist_historical_receipts(
            context,
            suite,
            artifacts["decision_receipts.jsonl"],
            application_run_id=audit_run_id,
        )
        typer.echo(
            f"Historical SQLite audit receipts added: {persisted_receipts}; "
            f"database={context.database_path.resolve()}"
        )
        _print_backtest_summary(suite, artifacts, output_directory)
        shutdown_reason = "historical_backtest_complete"
        return 0
    finally:
        _end_historical_audit(context, audit_run_id, shutdown_reason)


def _run_refresh_data(config_path: Path) -> int:
    context = _load_context(config_path)
    config = context.config
    provider = str(config.market_data.provider).lower()
    if provider in {"synthetic", "replay"}:
        typer.echo(f"No remote cache refresh is needed for {provider} data.")
        return 0
    config.data.refresh_cache = True
    from adaptive_trader.data import load_market_data

    data = load_market_data(config, project_root=context.project_root)
    typer.echo(
        f"Downloaded {len(data.prices):,} rows for {len(data.prices.columns)} tickers; "
        f"cache refreshed at {config.data.cache_file}"
    )
    return 0


def _paper_credentials_or_none() -> Any | None:
    from adaptive_trader.exceptions import CredentialError
    from adaptive_trader.live_models import PaperCredentials

    try:
        return PaperCredentials.from_environment()
    except CredentialError:
        return None


def _create_alpaca_broker(credentials: Any) -> Any:
    from adaptive_trader.broker import AlpacaPaperBroker

    return AlpacaPaperBroker(credentials)


def _construct_supported(factory: Callable[..., T], **values: Any) -> T:
    """Call an evolving adapter API using only parameters it accepts."""

    signature = inspect.signature(factory)
    accepts_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supplied = (
        values
        if accepts_keywords
        else {name: value for name, value in values.items() if name in signature.parameters}
    )
    return factory(**supplied)


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _invoke_method(target: Any, name: str, **values: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        raise RuntimeError(f"{type(target).__name__} does not provide {name}()")
    signature = inspect.signature(method)
    accepts_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supplied = (
        values
        if accepts_keywords
        else {key: value for key, value in values.items() if key in signature.parameters}
    )
    return _maybe_await(method(**supplied))


@contextmanager
def _service_signal_handlers(service: Any) -> Iterator[None]:
    """Translate SIGINT/SIGTERM into the service's graceful stop lifecycle."""

    installed: dict[int, Any] = {}

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del frame
        LOGGER.info("graceful stop requested by signal %s", signum)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()

    for watched in (signal.SIGINT, signal.SIGTERM):
        try:
            installed[int(watched)] = signal.getsignal(watched)
            signal.signal(watched, request_stop)
        except (ValueError, OSError):
            # Registration is restricted to the main interpreter thread.
            # Embedded callers still retain the `finally` shutdown path.
            continue
    try:
        yield
    finally:
        for restore_signum, previous in installed.items():
            signal.signal(restore_signum, previous)


def _feed_entitlement_check(config: Any, credentials: Any) -> tuple[bool, str]:
    """Confirm the configured feed with a small, read-only historical request."""

    try:
        module = importlib.import_module("adaptive_trader.market_data_live")
        provider_class = module.AlpacaMarketDataProvider
        provider = _construct_supported(
            provider_class,
            credentials=credentials,
            feed=config.market_data.feed,
            reconnect_initial_seconds=config.market_data.reconnect_initial_seconds,
            reconnect_max_seconds=config.market_data.reconnect_max_seconds,
            config=config,
        )
    except (ImportError, AttributeError) as exc:
        return False, f"market-data adapter unavailable: {exc}"

    for name in ("check_feed_entitlement", "validate_feed_entitlement", "doctor"):
        method = getattr(provider, name, None)
        if not callable(method):
            continue
        result = _maybe_await(method())
        if isinstance(result, bool):
            return result, "entitlement confirmed" if result else "entitlement rejected"
        if isinstance(result, Mapping):
            confirmed = bool(result.get("confirmed", result.get("ok", False)))
            return confirmed, str(result.get("message", result))
        return bool(result), str(result)

    get_bars = getattr(provider, "get_bars", None)
    if not callable(get_bars):
        return False, "provider exposes no read-only feed check"
    now = datetime.now(UTC)
    symbol = str(config.data.benchmark).upper()
    bars = _maybe_await(
        get_bars(
            [symbol],
            start=now - timedelta(days=14),
            end=now,
            timeframe="day",
        )
    )
    bar_count = len(tuple(bars))
    if bar_count == 0:
        return False, f"configured {config.market_data.feed} request returned no bars for {symbol}"
    return (
        True,
        f"configured {config.market_data.feed} request returned {bar_count} bar(s) for {symbol}",
    )


def _run_doctor(config_path: Path) -> int:
    context = _load_context(config_path)
    config = context.config
    rows: list[tuple[str, str, str]] = [
        ("Configuration", "PASS", f"hash={config.configuration_hash[:12]}…"),
        ("Paper-only invariant", "PASS", "execution.paper_only is true"),
        (
            "Default submission",
            "PASS" if not config.execution.paper_order_submission_enabled else "WARN",
            (
                "disabled"
                if not config.execution.paper_order_submission_enabled
                else "configuration enables paper submission; environment and runtime gates still apply"
            ),
        ),
    ]

    runtime_directory = context.database_path.parent
    permission_target = runtime_directory
    while not permission_target.exists() and permission_target != permission_target.parent:
        permission_target = permission_target.parent
    runtime_accessible = permission_target.is_dir() and os.access(
        permission_target,
        os.R_OK | os.W_OK | os.X_OK,
    )
    rows.append(
        (
            "Runtime directory permissions",
            "PASS" if runtime_accessible else "FAIL",
            (
                f"{runtime_directory} exists and is readable/writable"
                if runtime_directory.is_dir() and runtime_accessible
                else (
                    f"{runtime_directory} was not created; parent {permission_target} is writable"
                    if runtime_accessible
                    else f"no readable/writable parent for {runtime_directory}"
                )
            ),
        )
    )

    active_halts: set[str] = set()
    last_heartbeat = "none stored"
    last_bar = "none stored"
    stored_fresh = False
    reconciliation_clean = False
    try:
        if context.database_path.is_file():
            uri = f"file:{context.database_path.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("SELECT 1").fetchone()
                tables = _sqlite_tables(connection)
                if "halt_events" in tables:
                    for event in connection.execute(
                        "SELECT action, latch_type FROM halt_events ORDER BY created_at"
                    ).fetchall():
                        latch = str(event["latch_type"])
                        if event["action"] in {"halt", "hard_stop", "daily_loss"}:
                            active_halts.add(latch)
                        elif event["action"] in {"resume", "expired"}:
                            active_halts.discard(latch)
                if "heartbeats" in tables:
                    heartbeat = connection.execute(
                        "SELECT created_at, healthy, mode, components FROM heartbeats "
                        "ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    if heartbeat is not None:
                        heartbeat_at = _parse_timestamp(heartbeat["created_at"])
                        heartbeat_age = (
                            None
                            if heartbeat_at is None
                            else max(
                                0.0,
                                (datetime.now(UTC) - heartbeat_at).total_seconds(),
                            )
                        )
                        try:
                            components = json.loads(str(heartbeat["components"]))
                        except (json.JSONDecodeError, TypeError):
                            components = {}
                        maximum_age = max(
                            90,
                            int(config.schedule.heartbeat_interval_seconds) * 3,
                        )
                        stored_fresh = (
                            bool(heartbeat["healthy"])
                            and bool(
                                isinstance(components, Mapping)
                                and components.get("fresh")
                                and components.get("trade_updates_healthy")
                            )
                            and (heartbeat_age is not None and heartbeat_age <= maximum_age)
                        )
                        last_heartbeat = (
                            f"{heartbeat['created_at']} mode={heartbeat['mode']} "
                            f"healthy={bool(heartbeat['healthy'])} "
                            f"fresh/current={stored_fresh}"
                        )
                if "market_bars" in tables:
                    bar = connection.execute(
                        "SELECT symbol, start_at, received_at, feed FROM market_bars "
                        "ORDER BY received_at DESC LIMIT 1"
                    ).fetchone()
                    if bar is not None:
                        last_bar = (
                            f"{bar['symbol']} start={bar['start_at']} "
                            f"received={bar['received_at']} feed={bar['feed']}"
                        )
                if "reconciliation_runs" in tables:
                    reconciliation = connection.execute(
                        "SELECT completed_at, clean, blocking FROM reconciliation_runs "
                        "ORDER BY completed_at DESC LIMIT 1"
                    ).fetchone()
                    if reconciliation is not None:
                        reconciliation_clean = bool(reconciliation["clean"]) and not bool(
                            reconciliation["blocking"]
                        )
            rows.append(
                ("Database accessibility", "PASS", f"read-only open: {context.database_path}")
            )
        else:
            rows.append(
                (
                    "Database accessibility",
                    "PASS" if runtime_accessible else "FAIL",
                    (
                        f"not present and not created; writable parent: {permission_target}"
                        if runtime_accessible
                        else f"not present and parent is inaccessible: {context.database_path}"
                    ),
                )
            )
    except sqlite3.Error as exc:
        rows.append(("Database accessibility", "FAIL", _safe_error(exc)))
    rows.extend(
        [
            (
                "Current halt status",
                "BLOCKED" if active_halts else "PASS",
                ", ".join(sorted(active_halts)) if active_halts else "no active stored latch",
            ),
            ("Last stored heartbeat", "INFO", last_heartbeat),
            ("Last stored market bar", "INFO", last_bar),
        ]
    )

    credentials = _paper_credentials_or_none()
    remote_failure = False
    market_open: bool | None = None
    account_ready: bool | None = None
    if credentials is None:
        rows.extend(
            [
                ("Paper credentials", "SKIPPED", "paper credential variables are absent"),
                ("Paper account", "SKIPPED", "no network request attempted"),
                ("Market clock", "SKIPPED", "no network request attempted"),
                ("Asset eligibility", "SKIPPED", "no network request attempted"),
                ("Feed entitlement", "SKIPPED", "no network request attempted"),
                ("Historical data access", "SKIPPED", "no network request attempted"),
            ]
        )
    else:
        rows.append(("Paper credentials", "PASS", "present and redacted"))
        try:
            from adaptive_trader.broker import validate_asset

            broker = _create_alpaca_broker(credentials)
            if broker.paper_only is not True:
                raise RuntimeError("broker did not assert paper-only mode")
            account = broker.get_account()
            clock = broker.get_clock()
            market_open = bool(clock.is_open)
            account_ready = account.status.upper() == "ACTIVE" and not account.trading_blocked
            rows.append(
                (
                    "Paper account",
                    "PASS" if account_ready else "FAIL",
                    f"status={account.status}; trading_blocked={account.trading_blocked}",
                )
            )
            rows.append(
                (
                    "Market clock",
                    "PASS",
                    f"open={clock.is_open}; timestamp={clock.timestamp.isoformat()}",
                )
            )
            invalid_assets: list[str] = []
            for symbol in config.data.tickers:
                asset = broker.get_asset(symbol)
                eligible, reasons = validate_asset(asset, config.data.tickers)
                if config.data.require_fractionable and not asset.fractionable:
                    eligible = False
                    reasons = (*reasons, "not fractionable")
                if not eligible:
                    invalid_assets.append(f"{symbol}: {', '.join(reasons)}")
            rows.append(
                (
                    "Asset eligibility",
                    "PASS" if not invalid_assets else "FAIL",
                    "all configured assets eligible"
                    if not invalid_assets
                    else "; ".join(invalid_assets),
                )
            )
            entitlement_ok, entitlement_message = _feed_entitlement_check(config, credentials)
            if entitlement_ok:
                typer.echo(feed_banner(config.market_data.feed))
            rows.append(
                (
                    "Feed entitlement",
                    "PASS" if entitlement_ok else "FAIL",
                    entitlement_message,
                )
            )
            rows.append(
                (
                    "Historical data access",
                    "PASS" if entitlement_ok else "FAIL",
                    (
                        "configured feed returned completed benchmark history"
                        if entitlement_ok
                        else entitlement_message
                    ),
                )
            )
            remote_failure = not account_ready or bool(invalid_assets) or not entitlement_ok
        except Exception as exc:
            rows.append(("Read-only Alpaca checks", "FAIL", _safe_error(exc)))
            remote_failure = True

    enablement_reasons = _static_submission_gate_reasons(config, dry_run=False)
    if active_halts:
        enablement_reasons.append(f"active halt latch: {', '.join(sorted(active_halts))}")
    if market_open is False:
        enablement_reasons.append("regular US equity market is closed")
    if account_ready is False:
        enablement_reasons.append("paper account is inactive or trading-blocked")
    if last_heartbeat == "none stored":
        enablement_reasons.append("no stored heartbeat/freshness evidence")
    elif not stored_fresh:
        enablement_reasons.append("stored heartbeat/data freshness is stale or unhealthy")
    if not reconciliation_clean:
        enablement_reasons.append("no latest clean nonblocking reconciliation is stored")
    rows.append(
        (
            "Paper-order enablement",
            "SATISFIED" if not enablement_reasons else "BLOCKED",
            "all static/read-only checks satisfied"
            if not enablement_reasons
            else "; ".join(dict.fromkeys(enablement_reasons)),
        )
    )
    table = Table(title="Read-only doctor checks")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for row in rows:
        table.add_row(*row)
    _console().print(table)
    typer.echo(
        "Doctor performed read-only checks only; it did not create the database and never "
        "submits, cancels, replaces, or liquidates an order."
    )
    any_failure = any(status == "FAIL" for _, status, _ in rows)
    return 2 if any_failure or remote_failure else 0


def _static_submission_gate_reasons(config: Any, *, dry_run: bool) -> list[str]:
    reasons: list[str] = []
    if dry_run:
        reasons.append("--dry-run explicitly prohibits submission")
    if config.execution.paper_only is not True:
        reasons.append("configuration is not paper-only")
    if not config.execution.paper_order_submission_enabled:
        reasons.append("execution.paper_order_submission_enabled is false")
    actual_token = os.environ.get(PAPER_ORDER_ENABLEMENT_ENV, "")
    if actual_token != PAPER_ORDER_ACKNOWLEDGEMENT:
        reasons.append(
            f"{PAPER_ORDER_ENABLEMENT_ENV} does not exactly equal the required acknowledgement"
        )
    if _paper_credentials_or_none() is None:
        reasons.append("Alpaca paper credential check cannot run because credentials are absent")
    return reasons


def _print_gate_reasons(reasons: Sequence[str], *, disposition: str) -> None:
    typer.echo(disposition)
    for reason in reasons:
        typer.echo(f"- {reason}")
    typer.echo("No order was submitted.")


def _record_degraded_cycle(context: CommandContext, *, mode: str, reasons: Sequence[str]) -> None:
    """Persist a truthful no-order lifecycle when a live adapter cannot run."""

    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        run_id = repository.start_run(
            mode=mode,
            configuration=context.config,
            market_data_feed=context.config.market_data.feed,
        )
        repository.heartbeat(
            run_id=run_id,
            mode=mode,
            healthy=False,
            components={"submission": "blocked", "reasons": list(reasons)},
        )
        repository.end_run(run_id, "safe_no_order_degraded_cycle")
    finally:
        database.close()


def _create_live_service(
    context: CommandContext,
    *,
    mode: str,
    environment: Mapping[str, str] | None = None,
    prohibit_broker_mutations: bool = False,
) -> ServiceHandle:
    """Lazily construct the provider, paper/fake broker, repository, and service."""

    from adaptive_trader.broker import FakePaperBroker
    from adaptive_trader.live_models import RunMode
    from adaptive_trader.persistence import AuditRepository, Database

    live_module = importlib.import_module("adaptive_trader.live")
    market_module = importlib.import_module("adaptive_trader.market_data_live")
    live_service_class = live_module.LiveService
    database = Database(context.database_path)
    repository = AuditRepository(database)
    provider_name = str(context.config.market_data.provider).lower()
    target_provider: Any | None = None
    try:
        if provider_name == "alpaca":
            credentials = _paper_credentials_or_none()
            if credentials is None:
                raise RuntimeError("Alpaca paper credentials are required for live market data")
            broker = _create_alpaca_broker(credentials)
            provider_class = market_module.AlpacaMarketDataProvider
            market_data = _construct_supported(
                provider_class,
                credentials=credentials,
                feed=context.config.market_data.feed,
                reconnect_initial_seconds=(context.config.market_data.reconnect_initial_seconds),
                reconnect_max_seconds=context.config.market_data.reconnect_max_seconds,
                config=context.config,
                repository=repository,
            )
            from adaptive_trader.decision_engine import ForwardDecisionEngine

            target_provider = ForwardDecisionEngine(context.config, market_data)
        elif provider_name == "replay":
            broker = FakePaperBroker(auto_fill=False)
            provider_class = market_module.ReplayMarketDataProvider
            market_data = _construct_supported(
                provider_class,
                config=context.config,
                fixture_path=_resolve_project_path(
                    context.project_root, context.config.replay.fixture_path
                ),
                repository=repository,
            )
        elif provider_name == "synthetic":
            broker = FakePaperBroker(auto_fill=False)
            provider_class = market_module.SyntheticMarketDataProvider
            market_data = _construct_supported(
                provider_class,
                config=context.config,
                seed=context.config.replay.deterministic_seed,
                repository=repository,
            )
        else:  # configuration should reject this before reaching the adapter.
            raise RuntimeError(f"Unsupported market-data provider: {provider_name}")

        if prohibit_broker_mutations:
            broker = _ZeroMutationBroker(broker)

        service = _construct_supported(
            live_service_class,
            config=context.config,
            repository=repository,
            broker=broker,
            market_data=market_data,
            mode=RunMode(mode),
            environment=dict(os.environ if environment is None else environment),
            target_provider=target_provider,
        )
        return ServiceHandle(service=service, database=database)
    except Exception:
        database.close()
        raise


def _start_service_and_confirm_feed(context: CommandContext, service: Any) -> None:
    """Start read streams, then make only an evidence-backed SIP feed claim."""

    _invoke_method(service, "start_streams")
    if not (
        str(context.config.market_data.provider).lower() == "alpaca"
        and str(context.config.market_data.feed).upper() == "SIP"
    ):
        return
    status = _invoke_method(service, "status")
    if isinstance(status, Mapping) and (
        bool(status.get("feed_entitlement_verified"))
        and str(status.get("feed", "")).upper() == "SIP"
    ):
        # The command's initial disclosure says unconfirmed.  This exact banner
        # is emitted only after the service's read-only entitlement probe.
        typer.echo(feed_banner("SIP"))


def _run_live_once(
    context: CommandContext,
    *,
    mode: str,
    dry_run: bool,
) -> int:
    environment = dict(os.environ)
    if dry_run:
        environment[PAPER_ORDER_ENABLEMENT_ENV] = "NO"
    handle = _create_live_service(
        context,
        mode=mode,
        environment=environment,
        prohibit_broker_mutations=dry_run,
    )
    try:
        _start_service_and_confirm_feed(context, handle.service)
        result = _invoke_method(handle.service, "run_once", dry_run=dry_run)
        if result is not None:
            gate = getattr(result, "gate", None)
            planning = getattr(result, "planning", None)
            safe_summary = {
                "decision_id": str(getattr(result, "decision_id", "")),
                "session_date": str(getattr(result, "session_date", "")),
                "status": str(getattr(result, "status", "unknown")),
                "claimed": bool(getattr(result, "claimed", False)),
                "gate_allowed": None if gate is None else bool(getattr(gate, "allowed", False)),
                "gate_reasons": [] if gate is None else list(getattr(gate, "reasons", ())),
                "proposed_intents": (
                    0 if planning is None else len(getattr(planning, "intents", ()))
                ),
                "submitted_orders": len(getattr(result, "submitted_client_order_ids", ())),
                "hypothetical_orders": len(getattr(result, "hypothetical_client_order_ids", ())),
            }
            typer.echo(f"Cycle result: {json.dumps(safe_summary, sort_keys=True)}")
        typer.echo(
            "Dry-run completed; no order was submitted." if dry_run else "Paper cycle completed."
        )
        return 0
    finally:
        shutdown = getattr(handle.service, "shutdown", None)
        if callable(shutdown):
            _maybe_await(shutdown())
        handle.database.close()


def _run_observer(context: CommandContext) -> int:
    if (
        str(context.config.market_data.provider).lower() == "alpaca"
        and _paper_credentials_or_none() is None
    ):
        reasons = ["Alpaca paper credentials are absent; no network request was attempted"]
        _print_gate_reasons(reasons, disposition="OBSERVER UNAVAILABLE")
        _record_degraded_cycle(context, mode="observe", reasons=reasons)
        return 2
    try:
        handle = _create_live_service(
            context,
            mode="observe",
            prohibit_broker_mutations=True,
        )
    except (ImportError, AttributeError) as exc:
        reasons = [f"live observer adapter unavailable: {_safe_error(exc)}"]
        _print_gate_reasons(reasons, disposition="OBSERVER DEGRADED")
        _record_degraded_cycle(context, mode="observe", reasons=reasons)
        return 2
    try:
        _start_service_and_confirm_feed(context, handle.service)
        typer.echo("Observer mode started. Broker mutation is disabled.")
        with _service_signal_handlers(handle.service):
            _invoke_method(handle.service, "run")
        return 0
    finally:
        shutdown = getattr(handle.service, "shutdown", None)
        if callable(shutdown):
            _maybe_await(shutdown())
        handle.database.close()


def _run_observe_command(config_path: Path) -> int:
    context = _load_context(config_path)
    return _run_observer(context)


def _observer_smoke_audit_path(context: CommandContext) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return context.project_root / "audit" / f"observer_smoke_{timestamp}.md"


def _write_observer_smoke_audit(
    report: Mapping[str, Any],
    path: Path,
) -> Path:
    from adaptive_trader.observer_evidence import write_evidence_markdown

    return write_evidence_markdown(report, path, title="Bounded Observer Smoke Audit")


def _observer_smoke_database_summary(
    context: CommandContext,
    *,
    run_id: str | None,
    runtime_duration_seconds: float,
    mutation_attempts: Sequence[str],
    error: str | None,
) -> dict[str, Any]:
    """Inspect a completed smoke run without reopening SQLite for writes."""

    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "observer_smoke",
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": str(context.config.market_data.provider).lower(),
        "feed": str(context.config.market_data.feed).upper(),
        "runtime_duration_seconds": round(runtime_duration_seconds, 3),
        "run_id": run_id,
        "bars_received_by_symbol": {symbol: 0 for symbol in context.config.data.tickers},
        "stream_health": "UNVERIFIED",
        "last_bar_time": None,
        "strategy_decisions": 0,
        "hypothetical_order_intents": 0,
        "broker_submissions": 0,
        "broker_cancellations": 0,
        "broker_fills": 0,
        "orphan_fills": 0,
        "reconciliation_result": "UNVERIFIED",
        "database_integrity": "missing",
        "incidents": [],
        "market_state": "UNKNOWN",
        "bars_expected": None,
        "startup_preflight_complete": False,
        "final_checkpoint_present": False,
        "qualifies_as_completed_observer_session": False,
        "zero_mutation_guard_attempts": list(mutation_attempts),
        "error": error,
    }
    checks: list[dict[str, Any]] = []
    report["checks"] = checks

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(
            {
                "group": "observer_smoke",
                "name": name,
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
                "evidence": {},
            }
        )

    if not context.database_path.is_file() or not run_id:
        add("database_run", False, "the bounded observer run did not create durable evidence")
        add("zero_mutation_guard", not mutation_attempts, "no broker mutation may be attempted")
        report["status"] = "INCOMPLETE" if error is None else "FAIL"
        return report

    try:
        uri = f"{context.database_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            report["database_integrity"] = None if integrity is None else str(integrity[0])
            run = connection.execute(
                "SELECT started_at, ended_at, shutdown_reason, mode, configuration_hash, "
                "market_data_feed FROM application_runs "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            started = None if run is None else _parse_timestamp(run["started_at"])
            ended = None if run is None else _parse_timestamp(run["ended_at"])
            run_identity_valid = bool(
                run is not None
                and str(run["mode"]) == "observe"
                and str(run["configuration_hash"]) == context.config.configuration_hash
                and str(run["market_data_feed"] or "").upper()
                == str(context.config.market_data.feed).upper()
            )
            stream_rows = connection.execute(
                "SELECT stream, event_type, symbol, payload, created_at FROM stream_events "
                "WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
            stream_events = {(str(row["stream"]), str(row["event_type"])) for row in stream_rows}
            event_types = {str(row["event_type"]) for row in stream_rows}
            feed_events = [
                _json_mapping(row["payload"])
                for row in stream_rows
                if row["event_type"] == "feed_entitlement_confirmed"
            ]
            account_events = [
                _json_mapping(row["payload"])
                for row in stream_rows
                if row["event_type"] == "paper_account_verified"
            ]
            feed_verified = any(
                str(payload.get("feed") or "").upper()
                == str(context.config.market_data.feed).upper()
                and payload.get("fallback_used") is False
                for payload in feed_events
            )
            account_verified = any(
                payload.get("adapter") == "AlpacaPaperBroker"
                and payload.get("paper_only") is True
                and str(payload.get("account_status") or "").upper() == "ACTIVE"
                and payload.get("trading_blocked") is False
                for payload in account_events
            )
            startup_types = {
                "paper_account_verified",
                "feed_entitlement_confirmed",
                "asset_validation_confirmed",
                "history_preflight_confirmed",
            }
            market_handshake = ("market_data", "connected") in stream_events
            trade_handshake = ("trade_updates", "connected") in stream_events
            startup_complete = bool(
                startup_types <= event_types
                and account_verified
                and feed_verified
                and market_handshake
                and trade_handshake
            )
            report["startup_preflight_complete"] = startup_complete

            bar_keys: set[tuple[str, str, str]] = set()
            for row in stream_rows:
                if row["event_type"] not in {
                    "bar_inserted",
                    "bar_duplicate",
                    "bar_corrected",
                }:
                    continue
                payload = _json_mapping(row["payload"])
                bar_keys.add(
                    (
                        str(row["symbol"] or "").upper(),
                        str(payload.get("start") or ""),
                        str(payload.get("feed") or "").upper(),
                    )
                )
            counts = {str(symbol).upper(): 0 for symbol in context.config.data.tickers}
            latest: datetime | None = None
            for row in connection.execute(
                "SELECT symbol, start_at, feed, source FROM market_bars"
            ).fetchall():
                timestamp = _parse_timestamp(row["start_at"])
                if timestamp is None:
                    continue
                key = (
                    str(row["symbol"]).upper(),
                    timestamp.isoformat(),
                    str(row["feed"]).upper(),
                )
                if key not in bar_keys:
                    continue
                if (
                    str(row["source"]) not in {"alpaca_stream", "alpaca_stream_update"}
                    or str(row["feed"]).upper() != str(context.config.market_data.feed).upper()
                ):
                    continue
                if key[0] in counts:
                    counts[key[0]] += 1
                latest = timestamp if latest is None else max(latest, timestamp)
            report["bars_received_by_symbol"] = counts
            report["last_bar_time"] = None if latest is None else latest.isoformat()

            heartbeat = connection.execute(
                "SELECT healthy, components FROM heartbeats WHERE run_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            components: dict[str, Any] = {}
            if heartbeat is not None:
                try:
                    parsed = json.loads(str(heartbeat["components"]))
                    components = dict(parsed) if isinstance(parsed, Mapping) else {}
                except (TypeError, json.JSONDecodeError):
                    components = {}
                report["stream_health"] = "HEALTHY" if bool(heartbeat["healthy"]) else "UNHEALTHY"
                report["final_checkpoint_present"] = bool(
                    components.get("final_observer_checkpoint") is True
                )
                report["market_state"] = (
                    "OPEN"
                    if components.get("market_open") is True
                    else ("CLOSED" if components.get("market_open") is False else "UNKNOWN")
                )
            market_open = components.get("market_open") is True
            market_closed = components.get("market_open") is False
            report["bars_expected"] = True if market_open else (False if market_closed else None)
            decisions = connection.execute(
                "SELECT decision_id FROM rebalance_decisions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            decision_ids = tuple(str(row["decision_id"]) for row in decisions)
            report["strategy_decisions"] = len(decision_ids)
            if decision_ids:
                placeholders = ",".join("?" for _ in decision_ids)
                report["hypothetical_order_intents"] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM order_intents WHERE decision_id IN "
                        f"({placeholders}) AND reason LIKE 'hypothetical_%'",
                        decision_ids,
                    ).fetchone()[0]
                )
            report["broker_submissions"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM broker_orders WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            report["broker_cancellations"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM stream_events WHERE run_id = ? "
                    "AND lower(event_type) LIKE '%cancel%'",
                    (run_id,),
                ).fetchone()[0]
            )
            if started is not None:
                end = ended or datetime.now(UTC)
                decision_fill_clause = (
                    "0"
                    if not decision_ids
                    else f"decision_id IN ({','.join('?' for _ in decision_ids)})"
                )
                fill_parameters: tuple[Any, ...] = (
                    *decision_ids,
                    started.isoformat(),
                    end.isoformat(),
                )
                report["broker_fills"] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM fill_events WHERE {decision_fill_clause} "
                        "OR (created_at >= ? AND created_at <= ?)",
                        fill_parameters,
                    ).fetchone()[0]
                )
                report["orphan_fills"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM fill_events f LEFT JOIN broker_orders o "
                        "ON o.client_order_id = f.client_order_id "
                        "WHERE o.client_order_id IS NULL AND f.created_at >= ? "
                        "AND f.created_at <= ?",
                        (started.isoformat(), end.isoformat()),
                    ).fetchone()[0]
                )
            reconciliation = connection.execute(
                "SELECT clean, blocking FROM reconciliation_runs WHERE run_id = ? "
                "ORDER BY completed_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if reconciliation is not None:
                report["reconciliation_result"] = (
                    f"clean={bool(reconciliation['clean'])}; "
                    f"blocking={bool(reconciliation['blocking'])}"
                )
            incident_rows = connection.execute(
                "SELECT incident_type, severity FROM system_incidents WHERE run_id = ? "
                "AND resolved_at IS NULL",
                (run_id,),
            ).fetchall()
            report["incidents"] = [
                {"type": str(row["incident_type"]), "severity": str(row["severity"])}
                for row in incident_rows
            ]
            clean_shutdown = bool(
                run is not None
                and run["ended_at"] is not None
                and str(run["shutdown_reason"] or "").strip()
            )
            zero_mutations = (
                all(
                    int(report[field]) == 0
                    for field in (
                        "broker_submissions",
                        "broker_cancellations",
                        "broker_fills",
                        "orphan_fills",
                    )
                )
                and not mutation_attempts
            )
            final_reconciliation_clean = bool(
                reconciliation is not None
                and bool(reconciliation["clean"])
                and not bool(reconciliation["blocking"])
            )
            final_checkpoint_healthy = bool(
                heartbeat is not None
                and bool(heartbeat["healthy"])
                and report["final_checkpoint_present"]
            )
            open_market_evidence = bool(
                market_open
                and all(int(count) > 0 for count in counts.values())
                and components.get("fresh") is True
                and components.get("stream_connected") is True
                and components.get("trade_updates_healthy") is True
                and not components.get("missing_symbols")
                and not components.get("stale_symbols")
                and components.get("unresolved_gap") is not True
                and len(decision_ids) <= 1
            )
            closed_market_evidence = bool(
                market_closed and startup_complete and len(decision_ids) <= 1
            )
            add("database_integrity", report["database_integrity"] == "ok", "integrity_check")
            add("run_identity", run_identity_valid, "observe/config/feed identity must match")
            add("clean_shutdown", clean_shutdown, "the service ended its run durably")
            add(
                "startup_handshakes",
                startup_complete,
                "account/feed/assets/history/market/trade handshakes are required",
            )
            add(
                "final_reconciliation",
                final_reconciliation_clean,
                "the final reconciliation must be clean and nonblocking",
            )
            add(
                "final_heartbeat",
                final_checkpoint_healthy,
                "the final marked observer checkpoint must be healthy",
            )
            add(
                "market_state_evidence",
                open_market_evidence or closed_market_evidence,
                "open markets require fresh full-universe bars; closed markets require explicit handshakes",
            )
            add(
                "no_unresolved_incidents",
                not incident_rows,
                "no unresolved smoke-run incident may remain",
            )
            add("zero_broker_mutations", zero_mutations, "all mutation counters must be zero")
    except sqlite3.Error as exc:
        report["error"] = f"read-only database inspection failed: {type(exc).__name__}"
        add("read_only_inspection", False, "SQLite evidence could not be inspected")

    report["status"] = (
        "PASS"
        if error is None and checks and all(check["status"] == "PASS" for check in checks)
        else "FAIL"
    )
    return report


def _run_observer_smoke(config_path: Path, *, duration_seconds: int) -> int:
    context = _load_context(config_path)
    audit_path = _observer_smoke_audit_path(context)
    blockers: list[str] = []
    if duration_seconds <= 0 or duration_seconds > 21_600:
        blockers.append("duration-seconds must be between 1 and 21600")
    if str(context.config.market_data.provider).lower() != "alpaca":
        blockers.append("observer-smoke requires the explicit Alpaca provider")
    if context.config.execution.paper_order_submission_enabled:
        blockers.append("paper-order submission is enabled in configuration")
    if os.environ.get(PAPER_ORDER_ENABLEMENT_ENV) == PAPER_ORDER_ACKNOWLEDGEMENT:
        blockers.append("the paper-order acknowledgement token is active")
    if _paper_credentials_or_none() is None:
        blockers.append("Alpaca paper credentials are absent; no network request was attempted")
    if blockers:
        report = {
            "schema_version": 1,
            "kind": "observer_smoke",
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "INCOMPLETE",
            "provider": str(context.config.market_data.provider).lower(),
            "feed": str(context.config.market_data.feed).upper(),
            "runtime_duration_seconds": 0,
            "broker_submissions": 0,
            "broker_cancellations": 0,
            "broker_fills": 0,
            "orphan_fills": 0,
            "qualifies_as_completed_observer_session": False,
            "checks": [
                {
                    "group": "observer_smoke",
                    "name": "preconditions",
                    "status": "INCOMPLETE",
                    "detail": "; ".join(blockers),
                    "evidence": {},
                }
            ],
        }
        _write_observer_smoke_audit(report, audit_path)
        _print_gate_reasons(blockers, disposition="OBSERVER SMOKE INCOMPLETE")
        typer.echo(f"Sanitized smoke audit: {audit_path}")
        return 1

    environment = dict(os.environ)
    environment[PAPER_ORDER_ENABLEMENT_ENV] = "NO"
    handle: ServiceHandle | None = None
    run_id: str | None = None
    mutation_attempts: tuple[str, ...] = ()
    error: str | None = None
    started = time_module.monotonic()
    timer: threading.Timer | None = None
    try:
        handle = _create_live_service(
            context,
            mode="observe",
            environment=environment,
            prohibit_broker_mutations=True,
        )
        run_id = str(getattr(handle.service, "run_id", "")) or None
        timer = threading.Timer(float(duration_seconds), handle.service.stop)
        timer.daemon = True
        timer.start()
        _start_service_and_confirm_feed(context, handle.service)
        typer.echo(
            f"Bounded observer smoke started for at most {duration_seconds} seconds; "
            "all broker mutations are guarded."
        )
        with _service_signal_handlers(handle.service):
            _invoke_method(
                handle.service,
                "run",
                duration_seconds=float(duration_seconds),
            )
        _invoke_method(handle.service, "final_observer_checkpoint")
    except Exception as exc:
        error = _safe_error(exc)
    finally:
        if timer is not None:
            timer.cancel()
        if handle is not None:
            broker = getattr(handle.service, "broker", None)
            mutation_attempts = tuple(getattr(broker, "mutation_attempts", ()))
            shutdown = getattr(handle.service, "shutdown", None)
            if callable(shutdown):
                try:
                    _invoke_method(
                        handle.service,
                        "shutdown",
                        reason="bounded_observer_smoke_complete",
                    )
                except Exception as exc:
                    if error is None:
                        error = _safe_error(exc)
            handle.database.close()
    report = _observer_smoke_database_summary(
        context,
        run_id=run_id,
        runtime_duration_seconds=time_module.monotonic() - started,
        mutation_attempts=mutation_attempts,
        error=error,
    )
    _write_observer_smoke_audit(report, audit_path)
    typer.echo(
        f"Observer smoke status: {report['status']}; submissions="
        f"{report['broker_submissions']}; cancellations={report['broker_cancellations']}; "
        f"fills={report['broker_fills']}."
    )
    typer.echo("This bounded smoke never qualifies as a completed observer session.")
    typer.echo(f"Sanitized smoke audit: {audit_path}")
    return 0 if report["status"] == "PASS" else 2


def _run_observer_readiness(config_path: Path) -> int:
    context = _load_context(config_path)
    from adaptive_trader.observer_evidence import (
        evaluate_observer_readiness,
        evidence_markdown,
        write_evidence_json,
    )

    evidence_directory = context.project_root / "outputs" / "observer_evidence"
    report = evaluate_observer_readiness(
        context.config,
        database_path=context.database_path,
        evidence_directory=evidence_directory,
        project_root=context.project_root,
    )
    json_path = context.project_root / "outputs" / "observer_readiness_report.json"
    markdown_path = context.project_root / "outputs" / "observer_readiness_report.md"
    write_evidence_json(report, json_path)
    markdown_path.write_text(
        evidence_markdown(report, title="Phase 2 Observer Readiness Report"),
        encoding="utf-8",
    )
    typer.echo(
        f"Observer readiness: {report['status']} "
        f"({report['summary']['pass']} PASS, "
        f"{report['summary']['incomplete']} INCOMPLETE, "
        f"{report['summary']['fail']} FAIL)"
    )
    typer.echo(f"JSON report: {json_path}")
    typer.echo(f"Markdown report: {markdown_path}")
    typer.echo("Readiness does not enable paper orders and performs no broker request.")
    return 0 if report["status"] == "PASS" else (2 if report["status"] == "FAIL" else 1)


def _offline_dry_run_database_path(context: CommandContext) -> Path:
    suffix = context.database_path.suffix or ".db"
    return context.database_path.with_name(f"{context.database_path.stem}.offline-dry-run{suffix}")


def _real_data_dry_run_context(context: CommandContext) -> CommandContext:
    """Route real-data dry-run facts away from the official observer record.

    The derived configuration records its real database/output destinations and
    therefore has its own honest hash.  Evidence records link that derived hash
    back to the frozen observer configuration hash.
    """

    suffix = context.database_path.suffix or ".db"
    database_path = context.database_path.with_name(
        f"{context.database_path.stem}.real-data-dry-run{suffix}"
    )
    output_directory = context.output_directory.with_name(
        f"{context.output_directory.name}.real-data-dry-run"
    )
    from adaptive_trader.observer_evidence import derive_real_data_dry_run_config

    derived_config = derive_real_data_dry_run_config(
        context.config,
        database_path=database_path,
        output_directory=output_directory,
    )
    return CommandContext(
        config_path=context.config_path,
        project_root=context.project_root,
        config=derived_config,
        database_path=database_path,
        output_directory=output_directory,
    )


def _synthetic_forward_bars(config: Any, evaluation_at: datetime) -> tuple[list[Any], list[Any]]:
    """Build completed daily history and one fresh minute bar per asset."""

    from decimal import Decimal

    from adaptive_trader.data import generate_synthetic_market_data
    from adaptive_trader.live_models import MarketBar

    history_end = (evaluation_at - timedelta(days=1)).date()
    history_start = evaluation_at.date() - timedelta(
        days=int(config.market_data.historical_calendar_days) - 1
    )
    generated = generate_synthetic_market_data(
        list(config.data.tickers),
        start_date=history_start.isoformat(),
        end_date=history_end.isoformat(),
        seed=int(config.replay.deterministic_seed),
    )
    daily_bars: list[Any] = []
    for session, row in generated.prices.iterrows():
        started_at = datetime(
            session.year,
            session.month,
            session.day,
            21,
            tzinfo=UTC,
        )
        for symbol in generated.prices.columns:
            close = Decimal(str(row[symbol]))
            daily_bars.append(
                MarketBar(
                    symbol=str(symbol),
                    start=started_at,
                    end=started_at + timedelta(days=1),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=int(generated.volumes.at[session, symbol]),
                    feed="SYNTHETIC",
                    received_at=evaluation_at,
                    source="offline_engineering_daily",
                )
            )

    minute_at = evaluation_at - timedelta(minutes=1)
    latest = generated.prices.iloc[-1]
    fresh_bars = [
        MarketBar(
            symbol=str(symbol),
            start=minute_at,
            end=evaluation_at,
            open=Decimal(str(latest[symbol])),
            high=Decimal(str(latest[symbol])),
            low=Decimal(str(latest[symbol])),
            close=Decimal(str(latest[symbol])),
            volume=10_000,
            feed="SYNTHETIC",
            received_at=evaluation_at,
            source="offline_engineering_minute",
        )
        for symbol in generated.prices.columns
    ]
    return daily_bars, fresh_bars


def _print_offline_dry_run_result(result: Any, metadata: Any, submit_calls: int) -> None:
    typer.echo("OFFLINE SYNTHETIC ENGINEERING EVIDENCE — NOT MARKET OR PAPER PERFORMANCE")
    typer.echo(
        "Decision metadata: "
        f"status={metadata.status}; completed_history_cutoff={metadata.cutoff}; "
        f"history_sessions={metadata.history_observations}; feed={metadata.provider_feed}"
    )
    regime = None if metadata.regime is None else metadata.regime.get("name")
    risk_status = None if metadata.risk_decision is None else metadata.risk_decision.get("status")
    typer.echo(f"Risk metadata: status={risk_status}; regime={regime}")
    for action in metadata.risk_actions:
        typer.echo(
            f"Risk action: {action.get('control', action.get('action', 'unspecified'))} — "
            f"{action.get('description', action)}"
        )
    target = dict(metadata.final_target or {})
    typer.echo(
        "Final synthetic target: "
        + ", ".join(f"{symbol}={weight:.6f}" for symbol, weight in sorted(target.items()))
    )

    intents = () if result.planning is None else result.planning.intents
    typer.echo(f"Proposed intents ({len(intents)}; never submitted):")
    if not intents:
        typer.echo("- none")
    for intent in intents:
        amount = (
            f"notional={intent.notional}"
            if intent.notional is not None
            else f"quantity={intent.quantity}"
        )
        typer.echo(
            f"- {intent.side.value.upper()} {intent.symbol} {amount} "
            f"reference_price={intent.reference_price}"
        )
    if result.planning is not None:
        for skipped in result.planning.skipped:
            typer.echo(f"Planning skip: {skipped}")
    typer.echo(f"Fake broker submission calls: {submit_calls}")
    typer.echo("Dry-run completed; no order was submitted.")


def _run_offline_engineering_dry_run(
    context: CommandContext,
    *,
    gate_reasons: Sequence[str],
) -> int:
    """Run the real forward decision/planning path with deterministic fake inputs."""

    from copy import deepcopy

    from adaptive_trader.broker import FakePaperBroker
    from adaptive_trader.clock import FakeClock
    from adaptive_trader.decision_engine import ForwardDecisionEngine
    from adaptive_trader.live import LiveService
    from adaptive_trader.live_models import RunMode
    from adaptive_trader.market_data_live import SyntheticMarketDataProvider
    from adaptive_trader.persistence import AuditRepository, Database

    evaluation_at = datetime(2026, 1, 5, 15, 5, tzinfo=UTC)
    offline_config = deepcopy(context.config)
    offline_database_path = _offline_dry_run_database_path(context)
    offline_config.project.run_name = f"{context.config.project.run_name}_offline_dry_run"
    offline_config.project.database_path = str(offline_database_path)
    offline_config.market_data.provider = "synthetic"
    offline_config.execution.paper_order_submission_enabled = False
    daily_bars, fresh_bars = _synthetic_forward_bars(offline_config, evaluation_at)
    provider = SyntheticMarketDataProvider(daily_bars)
    fake_broker = FakePaperBroker(now=evaluation_at, auto_fill=False)
    broker = _ZeroMutationBroker(fake_broker)
    database = Database(offline_database_path)
    repository = AuditRepository(database)
    run_id = repository.start_run(
        mode="paper_once_offline_dry_run",
        configuration=offline_config,
        market_data_feed="SYNTHETIC",
    )
    service: Any | None = None
    shutdown_reason = "offline_engineering_dry_run_failed"
    try:
        clock = FakeClock(evaluation_at)
        engine = ForwardDecisionEngine(offline_config, provider)
        service = LiveService(
            offline_config,
            repository=repository,
            broker=broker,
            market_data=provider,
            mode=RunMode.PAPER_ONCE,
            clock=clock,
            environment={PAPER_ORDER_ENABLEMENT_ENV: "DISABLED_FOR_OFFLINE_DRY_RUN"},
            target_provider=engine,
            run_id=run_id,
            strategy_version=f"offline-dry-run-engineering-v1-{run_id[:12]}",
        )
        service.start_streams()
        for bar in fresh_bars:
            provider.emit(bar)
        result = service.run_once(dry_run=True)
        metadata = engine.last_metadata
        if metadata is None:
            raise RuntimeError("offline forward engine did not retain decision metadata")
        if result.gate is None or result.gate.allowed:
            raise RuntimeError("offline dry-run submission gate did not remain closed")
        if (
            result.submitted_client_order_ids
            or fake_broker.submit_calls != 0
            or broker.mutation_attempts
        ):
            raise RuntimeError("offline dry-run violated the zero-submission invariant")

        repository.heartbeat(
            run_id=run_id,
            mode="paper_once_offline_dry_run",
            healthy=True,
            components={
                "evidence": "offline_synthetic_engineering",
                "submission": "prohibited",
                "static_gate_reasons": list(gate_reasons),
                "proposed_intents": (
                    0 if result.planning is None else len(result.planning.intents)
                ),
            },
            created_at=evaluation_at,
        )
        _print_offline_dry_run_result(result, metadata, fake_broker.submit_calls)
        typer.echo(f"Offline dry-run audit database: {offline_database_path}")
        shutdown_reason = "offline_engineering_dry_run_complete"
        return 0
    finally:
        if service is not None:
            service.shutdown(shutdown_reason)
        else:
            repository.end_run(run_id, shutdown_reason)
        database.close()


def _run_paper_once(config_path: Path, *, dry_run: bool) -> int:
    context = _load_context(config_path)
    reasons = _static_submission_gate_reasons(context.config, dry_run=dry_run)
    if reasons:
        _print_gate_reasons(reasons, disposition="DOWNGRADED TO DRY-RUN")
        if _paper_credentials_or_none() is None:
            return _run_offline_engineering_dry_run(context, gate_reasons=reasons)
        dry_run_context = _real_data_dry_run_context(context)
        try:
            return _run_live_once(
                dry_run_context,
                mode="paper_once",
                dry_run=True,
            )
        except (ImportError, AttributeError) as exc:
            unavailable = [*reasons, f"live service unavailable: {_safe_error(exc)}"]
            _record_degraded_cycle(
                dry_run_context,
                mode="paper_once_dry_run",
                reasons=unavailable,
            )
            typer.echo(
                "Dry-run evaluation adapter was unavailable; the no-order outcome was recorded."
            )
            return 0
    return _run_live_once(context, mode="paper_once", dry_run=False)


def _run_paper_service(config_path: Path) -> int:
    context = _load_context(config_path)
    reasons = _static_submission_gate_reasons(context.config, dry_run=False)
    if reasons:
        _print_gate_reasons(reasons, disposition="PAPER-RUN REFUSED; OBSERVER-SAFE STATE RETAINED")
        _record_degraded_cycle(context, mode="paper_run_refused", reasons=reasons)
        return 2
    handle = _create_live_service(context, mode="paper_run")
    try:
        _start_service_and_confirm_feed(context, handle.service)
        typer.echo("Persistent Alpaca paper service started. All runtime gates remain enforced.")
        with _service_signal_handlers(handle.service):
            _invoke_method(handle.service, "run")
        return 0
    finally:
        shutdown = getattr(handle.service, "shutdown", None)
        if callable(shutdown):
            _maybe_await(shutdown())
        handle.database.close()


def _load_replay_events(module: Any, context: CommandContext) -> list[Any]:
    fixture = _resolve_project_path(context.project_root, context.config.replay.fixture_path)
    for name in ("load_replay_events", "read_replay_events"):
        loader = getattr(module, name, None)
        if callable(loader):
            return list(
                _maybe_await(_construct_supported(loader, path=fixture, config=context.config))
            )
    if not fixture.is_file():
        generator = getattr(module, "generate_synthetic_replay_events", None)
        if callable(generator):
            return list(
                _maybe_await(
                    _construct_supported(
                        generator,
                        config=context.config,
                        seed=context.config.replay.deterministic_seed,
                    )
                )
            )
        raise FileNotFoundError(
            f"Replay fixture does not exist and no deterministic generator is available: {fixture}"
        )

    from adaptive_trader.live_models import ReplayEvent, ReplayEventType

    events: list[Any] = []
    with fixture.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError(f"Replay line {line_number} must be a JSON object")
            events.append(
                ReplayEvent(
                    sequence=int(payload.get("sequence", len(events))),
                    event_type=ReplayEventType(str(payload["event_type"])),
                    timestamp=datetime.fromisoformat(
                        str(payload["timestamp"]).replace("Z", "+00:00")
                    ),
                    payload=payload.get("payload"),
                )
            )
    return events


def _run_replay_command(config_path: Path) -> int:
    context = _load_context(config_path)
    try:
        module = importlib.import_module("adaptive_trader.replay")
        run_replay = module.run_replay
    except (ImportError, AttributeError) as exc:
        reasons = [f"deterministic replay adapter unavailable: {_safe_error(exc)}"]
        _print_gate_reasons(reasons, disposition="REPLAY UNAVAILABLE")
        _record_degraded_cycle(context, mode="replay_unavailable", reasons=reasons)
        return 2

    events = _load_replay_events(module, context)
    result = _maybe_await(
        _construct_supported(
            run_replay,
            config=context.config,
            events=events,
            database=context.database_path,
        )
    )
    event_count = int(getattr(result, "event_count", len(events)))
    cycle_count = len(getattr(result, "cycles", ()))
    reconciliation_count = len(getattr(result, "reconciliation_ids", ()))
    restart_count = int(getattr(result, "restart_count", 0))
    submit_calls = int(getattr(result, "broker_submit_calls", 0))
    typer.echo(f"Deterministic replay completed with {event_count} events.")
    typer.echo(
        f"Cycles={cycle_count}; reconciliations={reconciliation_count}; restarts={restart_count}."
    )
    typer.echo(f"Fake broker submission calls: {submit_calls}")
    typer.echo(f"Replay audit database: {context.database_path}")
    result_database = getattr(result, "database", None)
    close_database = getattr(result_database, "close", None)
    if callable(close_database):
        close_database()
    return 0


def _sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    return {str(key): item for key, item in parsed.items()}


def _read_status(context: CommandContext) -> tuple[dict[str, Any], bool]:
    if not context.database_path.is_file():
        return {
            "Database": f"absent: {context.database_path}",
            "Configured feed": str(context.config.market_data.feed).upper(),
            "Observed feed": "not recorded",
            "Feed entitlement verified": False,
            "Market status": "not recorded",
            "Running": "no",
            "Heartbeat": "unavailable",
        }, False
    uri = f"{context.database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = _sqlite_tables(connection)
        configured_feed = str(context.config.market_data.feed).upper()
        snapshot: dict[str, Any] = {
            "Database": str(context.database_path),
            "Configured feed": configured_feed,
            "Observed feed": "not recorded",
            "Feed entitlement verified": False,
            "Market status": "not recorded",
        }
        healthy = False
        current_run_id: str | None = None
        if "application_runs" in tables:
            latest_run = connection.execute(
                "SELECT run_id, mode, market_data_feed FROM application_runs "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if latest_run is not None:
                current_run_id = str(latest_run["run_id"])
                snapshot["Latest run"] = current_run_id
                snapshot["Mode"] = latest_run["mode"]
                if latest_run["market_data_feed"]:
                    snapshot["Observed feed"] = str(latest_run["market_data_feed"]).upper()
        if "heartbeats" in tables:
            if current_run_id is None:
                heartbeat = connection.execute(
                    "SELECT * FROM heartbeats ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            else:
                heartbeat = connection.execute(
                    "SELECT * FROM heartbeats WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                    (current_run_id,),
                ).fetchone()
            if heartbeat is not None:
                heartbeat_at = _parse_timestamp(heartbeat["created_at"])
                age = (
                    None
                    if heartbeat_at is None
                    else max(0.0, (datetime.now(UTC) - heartbeat_at).total_seconds())
                )
                max_age = max(90, int(context.config.schedule.heartbeat_interval_seconds) * 3)
                healthy = bool(heartbeat["healthy"]) and age is not None and age <= max_age
                snapshot.update(
                    {
                        "Running": "yes" if healthy else "no/stale",
                        "Mode": heartbeat["mode"],
                        "Heartbeat": heartbeat["created_at"],
                        "Heartbeat age": "unknown" if age is None else f"{age:.1f}s",
                        "Health": "healthy" if bool(heartbeat["healthy"]) else "unhealthy",
                    }
                )
                try:
                    components = json.loads(str(heartbeat["components"]))
                except (json.JSONDecodeError, TypeError):
                    components = {}
                if isinstance(components, Mapping):
                    snapshot["Stream health"] = components.get("stream_connected", "not recorded")
                    snapshot["Trade-update stream health"] = components.get(
                        "trade_updates_healthy", "not recorded"
                    )
                    snapshot["Trade-update stream status"] = components.get(
                        "trade_updates_status", "not recorded"
                    )
                    snapshot["Data freshness"] = components.get("fresh", "not recorded")
                    snapshot["History preflight verified"] = components.get(
                        "history_preflight_verified", "not recorded"
                    )
                    health_reasons = components.get("health_reasons")
                    snapshot["Health reasons"] = (
                        ", ".join(str(reason) for reason in health_reasons)
                        if isinstance(health_reasons, (list, tuple)) and health_reasons
                        else "none"
                    )
                    observed_feed = components.get("feed")
                    if observed_feed:
                        snapshot["Observed feed"] = str(observed_feed).upper()
                    market_open = components.get("market_open")
                    snapshot["Market status"] = (
                        "open"
                        if market_open is True
                        else "closed"
                        if market_open is False
                        else "not recorded"
                    )
                    snapshot["Feed entitlement verified"] = bool(
                        components.get("feed_entitlement_verified", False)
                    )
            else:
                snapshot.update({"Running": "no", "Heartbeat": "none"})
        else:
            snapshot.update({"Running": "no", "Heartbeat": "table unavailable"})

        if "halt_events" in tables:
            rows = connection.execute("SELECT * FROM halt_events ORDER BY created_at").fetchall()
            latches: dict[str, str] = {}
            for row in rows:
                if row["action"] in {"halt", "hard_stop", "daily_loss"}:
                    latches[str(row["latch_type"])] = str(row["reason"])
                elif row["action"] in {"resume", "expired"}:
                    latches.pop(str(row["latch_type"]), None)
            snapshot["Active latches"] = ", ".join(sorted(latches)) if latches else "none"
        if "reconciliation_runs" in tables:
            row = connection.execute(
                "SELECT * FROM reconciliation_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            snapshot["Last reconciliation"] = (
                "none"
                if row is None
                else f"{row['completed_at']} clean={bool(row['clean'])} blocking={bool(row['blocking'])}"
            )
        if "market_bars" in tables:
            row = connection.execute(
                "SELECT symbol, start_at, received_at, feed FROM market_bars "
                "ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
            snapshot["Last market bar"] = (
                "none"
                if row is None
                else f"{row['symbol']} {row['start_at']} feed={row['feed']} received={row['received_at']}"
            )
        if "account_snapshots" in tables:
            account = connection.execute(
                "SELECT * FROM account_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if account is not None:
                snapshot.update(
                    {
                        "Paper account status": account["status"],
                        "Last paper account snapshot": account["timestamp"],
                        "Simulated paper equity": account["equity"],
                        "Simulated paper cash": account["cash"],
                        "Buying power (display only)": account["buying_power"],
                    }
                )
                if "position_snapshots" in tables:
                    positions = connection.execute(
                        "SELECT symbol, quantity, market_value FROM position_snapshots "
                        "WHERE account_snapshot_id = ? ORDER BY symbol",
                        (account["snapshot_id"],),
                    ).fetchall()
                    snapshot["Current stored positions"] = (
                        "none"
                        if not positions
                        else "; ".join(
                            f"{row['symbol']} qty={row['quantity']} value={row['market_value']}"
                            for row in positions
                        )
                    )
        if "daily_performance" in tables:
            performance = connection.execute(
                "SELECT * FROM daily_performance ORDER BY session_date DESC LIMIT 1"
            ).fetchone()
            if performance is not None:
                try:
                    payload = json.loads(str(performance["payload"]))
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                if isinstance(payload, Mapping):
                    snapshot["Current drawdown"] = payload.get("drawdown", "not recorded")
                    snapshot["Daily paper P&L"] = payload.get("daily_pnl", "not recorded")
        if "rebalance_decisions" in tables:
            decision = connection.execute(
                "SELECT session_date, created_at, status, mode, skip_reason "
                "FROM rebalance_decisions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            snapshot["Last strategy decision"] = (
                "none"
                if decision is None
                else (
                    f"{decision['created_at']} session={decision['session_date']} "
                    f"mode={decision['mode']} status={decision['status']} "
                    f"reason={decision['skip_reason'] or 'none'}"
                )
            )
        if "risk_decisions" in tables:
            risk = connection.execute(
                "SELECT created_at, decision_id, payload FROM risk_decisions "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            snapshot["Last risk decision"] = (
                "none"
                if risk is None
                else f"{risk['created_at']} decision={risk['decision_id']} payload={risk['payload']}"
            )
        if "broker_orders" in tables:
            snapshot["Recorded paper orders"] = connection.execute(
                "SELECT COUNT(*) FROM broker_orders"
            ).fetchone()[0]
            snapshot["Open recorded paper orders"] = connection.execute(
                "SELECT COUNT(*) FROM broker_orders WHERE state NOT IN "
                "('filled', 'canceled', 'rejected', 'expired', 'replaced')"
            ).fetchone()[0]
        if "system_incidents" in tables:
            snapshot["Unresolved incidents"] = connection.execute(
                "SELECT COUNT(*) FROM system_incidents WHERE resolved_at IS NULL"
            ).fetchone()[0]
        return snapshot, healthy


def _run_status(config_path: Path) -> int:
    context = _load_context(config_path)
    snapshot, healthy = _read_status(context)
    table = Table(title="Local paper-system status")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in snapshot.items():
        table.add_row(str(key), str(value))
    _console().print(table)
    if (
        str(context.config.market_data.feed).upper() == "SIP"
        and str(snapshot.get("Observed feed", "")).upper() == "SIP"
        and snapshot.get("Feed entitlement verified") is True
    ):
        typer.echo(feed_banner("SIP"))
    typer.echo("Status is read-only and never mutates an order.")
    return 0 if healthy else 1


def _broker_for_reconciliation(context: CommandContext) -> Any | None:
    provider = str(context.config.market_data.provider).lower()
    if provider in {"replay", "synthetic"}:
        from adaptive_trader.broker import FakePaperBroker

        return FakePaperBroker(auto_fill=False)
    credentials = _paper_credentials_or_none()
    return None if credentials is None else _create_alpaca_broker(credentials)


def _run_reconcile(config_path: Path) -> int:
    context = _load_context(config_path)
    broker = _broker_for_reconciliation(context)
    if broker is None:
        typer.echo("RECONCILIATION SKIPPED: Alpaca paper credentials are absent.")
        typer.echo("No network request and no order mutation occurred.")
        return 2
    try:
        module = importlib.import_module("adaptive_trader.reconciliation")
        reconcile_function = module.reconcile
    except (ImportError, AttributeError) as exc:
        typer.echo(f"RECONCILIATION UNAVAILABLE: {_safe_error(exc)}")
        typer.echo("No order mutation occurred.")
        return 2

    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        result = _construct_supported(
            reconcile_function,
            repository=repository,
            broker=broker,
            run_id=None,
            universe=tuple(context.config.data.tickers),
        )
        result = _maybe_await(result)
    finally:
        database.close()
    typer.echo(
        f"Reconciliation clean={bool(result.clean)} blocking={bool(result.blocking)} "
        f"discrepancies={len(result.discrepancies)}"
    )
    for discrepancy in result.discrepancies:
        typer.echo(f"- {discrepancy.severity.value}: {discrepancy.kind}: {discrepancy.message}")
    typer.echo("Reconcile is read/check/persist only and never mutates an order.")
    return 2 if result.blocking else 0


def _record_operator_halt(
    context: CommandContext, *, action: str, reason: str, acknowledgement: str | None = None
) -> tuple[str, dict[str, Mapping[str, Any]]]:
    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        identifier = repository.record_halt(
            run_id=None,
            action=action,
            latch_type="operator",
            initiator="cli",
            reason=reason,
            acknowledgement=acknowledgement,
        )
        state = repository.active_halts()
        return identifier, state
    finally:
        database.close()


def _run_halt(
    config_path: Path,
    *,
    reason: str,
    flatten_paper_positions: bool,
    acknowledgement: str,
) -> int:
    context = _load_context(config_path)
    if not reason.strip():
        typer.echo("HALT REFUSED: --reason must be nonempty.", err=True)
        return 2

    def record_cli_halt_only() -> str:
        halt_id, _ = _record_operator_halt(
            context,
            action="halt",
            reason=reason.strip(),
        )
        typer.echo(f"Persistent operator halt recorded: {halt_id}")
        return halt_id

    if not flatten_paper_positions:
        record_cli_halt_only()
        typer.echo("No broker cancellation or liquidation was requested; no order was submitted.")
        return 0
    if acknowledgement != PAPER_FLATTEN_ACKNOWLEDGEMENT:
        record_cli_halt_only()
        typer.echo(
            "SIMULATED PAPER LIQUIDATION REFUSED: typed confirmation is missing or incorrect."
        )
        typer.echo("The halt remains active; no order was submitted.")
        return 2
    reasons = _static_submission_gate_reasons(context.config, dry_run=False)
    if reasons:
        record_cli_halt_only()
        _print_gate_reasons(reasons, disposition="SIMULATED PAPER LIQUIDATION REFUSED")
        return 2
    try:
        handle = _create_live_service(context, mode="paper_once")
    except (ImportError, AttributeError, RuntimeError) as exc:
        record_cli_halt_only()
        typer.echo(f"SIMULATED PAPER LIQUIDATION UNAVAILABLE: {_safe_error(exc)}")
        typer.echo("The halt remains active; no order was submitted.")
        return 2
    try:
        try:
            _start_service_and_confirm_feed(context, handle.service)
            result = _invoke_method(
                handle.service,
                "halt",
                reason=reason,
                flatten=True,
                acknowledgement=acknowledgement,
            )
        except Exception as exc:
            from adaptive_trader.exceptions import ReconciliationBlocked

            if not isinstance(exc, ReconciliationBlocked):
                raise
            typer.echo(f"SIMULATED PAPER LIQUIDATION NOT EXECUTED: {_safe_error(exc)}")
            typer.echo(
                "The service persisted the halt and cancellation outcome, but no paper "
                "position liquidation order was submitted."
            )
            return 2
        typer.echo(f"Persistent manual halt and paper liquidation request recorded: {result}")
        return 0
    finally:
        shutdown = getattr(handle.service, "shutdown", None)
        if callable(shutdown):
            _maybe_await(shutdown())
        handle.database.close()


def _run_resume(config_path: Path, *, acknowledgement: str) -> int:
    context = _load_context(config_path)
    if acknowledgement != PAPER_RESUME_ACKNOWLEDGEMENT:
        typer.echo("RESUME REFUSED: typed paper-account review acknowledgement is incorrect.")
        return 2
    from adaptive_trader.persistence import AuditRepository, Database

    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        active = repository.active_halts()
        resumable_latches = tuple(
            name for name in ("operator", "manual", "hard_stop") if name in active
        )
        if not resumable_latches:
            typer.echo("No active operator/manual-review halt exists; no state was changed.")
            if active:
                typer.echo(f"Other active latches remain: {', '.join(sorted(active))}")
            return 0
    finally:
        database.close()

    if str(context.config.market_data.provider).lower() != "alpaca":
        typer.echo(
            "RESUME REFUSED: a fresh Alpaca paper-account reconciliation and live-feed "
            "freshness check are required."
        )
        return 2
    if _paper_credentials_or_none() is None:
        typer.echo(
            "RESUME REFUSED: Alpaca paper credentials are absent, so reconciliation and "
            "live-feed freshness cannot be confirmed."
        )
        typer.echo("The operator halt remains active; no network request was attempted.")
        return 2

    try:
        handle = _create_live_service(context, mode="resume")
    except Exception as exc:
        typer.echo(f"RESUME REFUSED: paper-service preflight is unavailable: {_safe_error(exc)}")
        typer.echo("The operator halt remains active.")
        return 2
    try:
        _invoke_method(handle.service, "start_streams")
        reconciliation = _invoke_method(handle.service, "reconcile")
        status = _invoke_method(handle.service, "status")
        blocking = bool(getattr(reconciliation, "blocking", True))
        fresh = bool(status.get("fresh", False)) if isinstance(status, Mapping) else False
        connected = (
            bool(status.get("stream_connected", False)) if isinstance(status, Mapping) else False
        )
        if blocking or not fresh or not connected:
            reasons: list[str] = []
            if blocking:
                reasons.append("the new paper-account reconciliation is blocking")
            if not connected:
                reasons.append("the configured live feed is not connected")
            if not fresh:
                reasons.append("required market data is not fresh")
            typer.echo(f"RESUME REFUSED: {'; '.join(reasons)}.")
            typer.echo("The operator halt remains active; no order was submitted.")
            return 2
    except Exception as exc:
        typer.echo(f"RESUME REFUSED: safety preflight failed: {_safe_error(exc)}")
        typer.echo("The operator halt remains active; no order was submitted.")
        return 2
    finally:
        shutdown = getattr(handle.service, "shutdown", None)
        if callable(shutdown):
            _maybe_await(shutdown())
        handle.database.close()

    database = Database(context.database_path)
    try:
        repository = AuditRepository(database)
        resume_ids = tuple(
            repository.record_halt(
                run_id=None,
                action="resume",
                latch_type=latch_type,
                initiator="cli",
                reason=(
                    "operator reviewed paper account after clean reconciliation and fresh data"
                ),
                acknowledgement=acknowledgement,
            )
            for latch_type in resumable_latches
        )
        remaining = repository.active_halts()
    finally:
        database.close()
    typer.echo(
        "Manual-review halt resume recorded: "
        + ", ".join(
            f"{latch_type}={resume_id}"
            for latch_type, resume_id in zip(resumable_latches, resume_ids, strict=True)
        )
    )
    if remaining:
        typer.echo(f"Other active latches remain: {', '.join(sorted(remaining))}")
    typer.echo("Resume never submits an order.")
    return 0


def _run_report(config_path: Path, *, output: Path | None) -> int:
    context = _load_context(config_path)
    from adaptive_trader.forward_reporting import generate_forward_outputs

    output_directory = (
        context.output_directory
        if output is None
        else _resolve_project_path(context.project_root, output)
    )
    artifacts = generate_forward_outputs(
        context.database_path,
        output_directory,
        feed=context.config.market_data.feed,
    )
    typer.echo("Forward paper report generation is read-only with respect to the audit database.")
    for name, path in sorted(artifacts.items()):
        typer.echo(f"{name}: {path.resolve()}")
    return 0


@app.command(
    "validate-config", help="Validate strict paper-only configuration without network access."
)
def validate_config(
    config: Annotated[Path, typer.Option("--config", help="YAML configuration path.")],
) -> None:
    def operation() -> int:
        context = _load_context(config)
        typer.echo(
            f"Configuration is valid: {context.config.project.name} "
            f"({len(context.config.data.tickers)} assets, benchmark "
            f"{context.config.data.benchmark}, hash {context.config.configuration_hash})"
        )
        return 0

    _dispatch(operation)


@app.command("refresh-data", help="Explicitly refresh the configured historical-data cache.")
def refresh_data(
    config: Annotated[Path, typer.Option("--config", help="YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_refresh_data(config))


@app.command("backtest", help="Run causal historical comparisons and write static artifacts.")
def backtest(
    config: Annotated[Path, typer.Option("--config", help="YAML configuration path.")],
    synthetic: Annotated[
        bool,
        typer.Option(
            "--synthetic",
            help="Use deterministic synthetic daily data; never present it as market evidence.",
        ),
    ] = False,
    synthetic_seed: Annotated[int, typer.Option("--synthetic-seed")] = 20240311,
    output: Annotated[
        Path | None, typer.Option("--output", help="Override output directory.")
    ] = None,
) -> None:
    _dispatch(
        lambda: _run_backtest_command(
            config,
            synthetic=synthetic,
            synthetic_seed=synthetic_seed,
            output=output,
        )
    )


@app.command("doctor", help="Run read-only configuration, paper-account, asset, and feed checks.")
def doctor(
    config: Annotated[Path, typer.Option("--config", help="YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_doctor(config))


@app.command("replay", help="Run deterministic replay with fake market/broker dependencies.")
def replay(
    config: Annotated[Path, typer.Option("--config", help="Replay YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_replay_command(config))


@app.command("observe", help="Run live observation; this command can never submit an order.")
def observe(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_observe_command(config))


@app.command(
    "observer-smoke",
    help="Run a bounded, zero-mutation real-market observer smoke check.",
)
def observer_smoke(
    config: Annotated[Path, typer.Option("--config", help="Observer YAML path.")],
    duration_seconds: Annotated[
        int,
        typer.Option(
            "--duration-seconds",
            min=1,
            max=21_600,
            help="Maximum bounded runtime in seconds.",
        ),
    ] = 300,
) -> None:
    _dispatch(lambda: _run_observer_smoke(config, duration_seconds=duration_seconds))


@app.command(
    "observer-readiness",
    help="Evaluate the formal read-only Phase 2 evidence gate.",
)
def observer_readiness(
    config: Annotated[Path, typer.Option("--config", help="Observer YAML path.")],
) -> None:
    _dispatch(lambda: _run_observer_readiness(config))


@app.command("paper-once", help="Evaluate one paper cycle; failed gates downgrade to dry-run.")
def paper_once(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Structurally prohibit submission.")
    ] = False,
) -> None:
    _dispatch(lambda: _run_paper_once(config, dry_run=dry_run))


@app.command("paper-run", help="Run the persistent, explicitly multi-gated Alpaca paper service.")
def paper_run(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_paper_service(config))


@app.command("status", help="Read local heartbeat, latch, reconciliation, and audit status.")
def status(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_status(config))


@app.command("reconcile", help="Read broker state, compare it locally, and persist findings only.")
def reconcile_command(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
) -> None:
    _dispatch(lambda: _run_reconcile(config))


@app.command("halt", help="Persistently block new decisions; liquidation is separately confirmed.")
def halt(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
    reason: Annotated[str, typer.Option("--reason", help="Required operator reason.")] = "",
    flatten_paper_positions: Annotated[
        bool,
        typer.Option(
            "--flatten-paper-positions",
            help="Request simulated paper liquidation after all gates pass.",
        ),
    ] = False,
    acknowledge: Annotated[
        str,
        typer.Option(
            "--acknowledge",
            help="Typed confirmation required only for simulated paper liquidation.",
        ),
    ] = "",
) -> None:
    _dispatch(
        lambda: _run_halt(
            config,
            reason=reason,
            flatten_paper_positions=flatten_paper_positions,
            acknowledgement=acknowledge,
        )
    )


@app.command("resume", help="Clear an eligible operator halt after explicit account review.")
def resume(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
    acknowledge: Annotated[
        str, typer.Option("--acknowledge", help="Typed review acknowledgement.")
    ] = "",
) -> None:
    _dispatch(lambda: _run_resume(config, acknowledgement=acknowledge))


@app.command("report", help="Generate read-only forward-paper CSV, JSONL, Markdown, and plots.")
def report(
    config: Annotated[Path, typer.Option("--config", help="Paper YAML configuration path.")],
    output: Annotated[
        Path | None, typer.Option("--output", help="Override report directory.")
    ] = None,
) -> None:
    _dispatch(lambda: _run_report(config, output=output))


def main(argv: list[str] | None = None) -> int:
    """Run the Typer application and return a process-compatible exit code."""

    try:
        result = app(args=argv, prog_name=PROGRAM_NAME, standalone_mode=False)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    # Click returns the Exit code instead of raising when standalone_mode is
    # disabled. Preserve it so `python -m adaptive_trader.cli` cannot turn an
    # INCOMPLETE/FAIL gate into a successful shell status.
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
