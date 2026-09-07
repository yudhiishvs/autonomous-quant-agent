"""Broker-free command line for validating and operating the platform."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from adaptive_trader.platform.config import (
    ExperimentConfigError,
    PlatformConfig,
    RuntimeService,
    load_platform_config,
    load_runtime_settings,
)
from adaptive_trader.platform.domain import AuditVerificationReport
from adaptive_trader.platform.errors import (
    AuditIntegrityError,
    AuditPersistenceError,
    AuditValidationError,
    RuntimeSettingsError,
)
from adaptive_trader.platform.package_resources import packaged_config_root
from adaptive_trader.platform.security import (
    LocalSecretBootstrapError,
    LocalSecretBootstrapResult,
    bootstrap_local_secrets,
)
from adaptive_trader.platform.storage import AuditRepository, create_platform_read_only_engine

DEFAULT_CONFIG_ROOT = packaged_config_root()
DEFAULT_PROFILE = Path("platform/offline.yaml")
DEFAULT_RUNTIME_CONFIG = Path("configs") / DEFAULT_PROFILE

app = typer.Typer(
    name="aqa",
    help="Validate and operate the autonomous quant platform.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(
    name="config",
    help="Inspect static platform configuration.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
secrets_app = typer.Typer(
    name="secrets",
    help="Manage local infrastructure secret files.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
audit_app = typer.Typer(
    name="audit",
    help="Verify immutable platform audit evidence.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(
    name="db",
    help="Apply the governed PostgreSQL schema lifecycle.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
data_app = typer.Typer(
    name="data",
    help="Inspect and verify market-data workflows.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
scheduler_app = typer.Typer(
    name="scheduler",
    help="Inspect deterministic decision scheduling.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
shadow_app = typer.Typer(
    name="shadow",
    help="Validate the no-submission shadow boundary.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
api_app = typer.Typer(
    name="api",
    help="Run the authenticated private control API.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
dashboard_app = typer.Typer(
    name="dashboard",
    help="Run the read-only operations dashboard.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
service_app = typer.Typer(
    name="service",
    help="Run and inspect bounded background services.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
app.add_typer(config_app, name="config")
app.add_typer(secrets_app, name="secrets")
app.add_typer(audit_app, name="audit")
app.add_typer(db_app, name="db")
app.add_typer(data_app, name="data")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(shadow_app, name="shadow")
app.add_typer(api_app, name="api")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(service_app, name="service")


def _config_root(path: Path) -> Path:
    rendered = os.fspath(path)
    if type(rendered) is not str or not rendered or "\x00" in rendered:
        raise ExperimentConfigError("config root must be a nonempty text path")
    return Path(os.path.abspath(rendered))


def _validated_config(profile: Path, config_root: Path) -> PlatformConfig:
    return load_platform_config(profile, config_root=_config_root(config_root))


def _success_payload(config: PlatformConfig, *, check: str) -> dict[str, object]:
    return {
        "check": check,
        "config_hash": config.content_hash,
        "experiment_hash": config.experiment.definition_hash,
        "experiment_id": config.experiment.experiment_id,
        "mode": config.profile.mode.value,
        "profile": config.profile.profile_id,
        "status": "ok",
        "submission_enabled": config.profile.execution.submission_enabled,
    }


def _emit_success(config: PlatformConfig, *, check: str, json_output: bool) -> None:
    payload = _success_payload(config, check=check)
    if json_output:
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    typer.echo(
        f"{check}: ok; profile={payload['profile']}; mode={payload['mode']}; "
        f"submission_enabled={str(payload['submission_enabled']).lower()}"
    )


def _validate_or_exit(
    *,
    profile: Path,
    config_root: Path,
    check: str,
    json_output: bool,
) -> None:
    try:
        config = _validated_config(profile, config_root)
    except ExperimentConfigError as error:
        if json_output:
            typer.echo(
                json.dumps(
                    {"check": check, "error": str(error), "status": "error"},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                err=True,
            )
        else:
            typer.echo(f"{check}: error: {error}", err=True)
        raise typer.Exit(code=2) from None
    _emit_success(config, check=check, json_output=json_output)


def _emit_bootstrap_result(result: LocalSecretBootstrapResult, *, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"created": result.created, "skipped": result.skipped, "status": "ok"},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    for path in result.created:
        typer.echo(f"created: {path}")
    for path in result.skipped:
        typer.echo(f"skipped: {path}")


def _audit_environment(
    *,
    profile: Path,
    database_url_file: Path | None,
) -> dict[str, str]:
    environment = {"AQA_CONFIG": profile.as_posix()}
    if database_url_file is not None:
        environment["AQA_DATABASE_URL_FILE"] = database_url_file.as_posix()
    return environment


def _audit_success_payload(report: AuditVerificationReport) -> dict[str, object]:
    if type(report) is not AuditVerificationReport:
        raise AuditIntegrityError("audit verification returned an invalid report")
    heads = report.stream_heads
    return {
        "check": "audit",
        "event_count": report.event_count,
        "status": "ok",
        "stream_count": len(heads),
        "stream_heads": [
            {
                "event_hash": head.event_hash,
                "sequence": head.sequence,
                "stream_id": head.stream_id,
            }
            for head in heads
        ],
    }


def _emit_audit_success(report: AuditVerificationReport, *, json_output: bool) -> None:
    payload = _audit_success_payload(report)
    if json_output:
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    typer.echo(
        f"audit verify: ok; streams={payload['stream_count']}; events={payload['event_count']}"
    )


def _emit_audit_failure(*, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"check": "audit", "error": "audit verification failed", "status": "error"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            err=True,
        )
        return
    typer.echo("audit verify: error: audit verification failed", err=True)


def _offline_demo_result(
    *,
    command: str,
    config_root: Path,
    output: Path,
    json_output: bool,
) -> None:
    from adaptive_trader.platform.demo import (
        DemoError,
        publish_demo_evidence,
        run_demo_twice,
    )

    try:
        comparison = run_demo_twice(config_root=_config_root(config_root))
        evidence_path = publish_demo_evidence(
            comparison.first,
            output,
            application_root=Path.cwd(),
        )
    except (DemoError, ExperimentConfigError, OSError):
        error_payload = {
            "check": command,
            "error": "offline verification failed",
            "status": "error",
        }
        if json_output:
            typer.echo(
                json.dumps(error_payload, separators=(",", ":"), sort_keys=True),
                err=True,
            )
        else:
            typer.echo(f"{command}: error: offline verification failed", err=True)
        raise typer.Exit(code=2) from None
    payload: dict[str, object] = {
        "check": command,
        "deterministic": True,
        "evidence_label": "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE",
        "evidence_manifest_hash": comparison.manifest_hash,
        "evidence_path": evidence_path.relative_to(Path.cwd().resolve()).as_posix(),
        "status": "ok",
    }
    if json_output:
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        typer.echo(
            f"{command}: ok; deterministic=true; "
            f"evidence_manifest_hash={comparison.manifest_hash}; "
            f"evidence={payload['evidence_path']}"
        )


@app.command("doctor")
def doctor(
    profile: Annotated[
        Path,
        typer.Option("--config", help="Profile path relative to --config-root."),
    ] = DEFAULT_PROFILE,
    config_root: Annotated[
        Path,
        typer.Option(help="Trusted directory containing platform and experiment configuration."),
    ] = DEFAULT_CONFIG_ROOT,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Validate static configuration without reading secrets or constructing clients."""

    _validate_or_exit(
        profile=profile,
        config_root=config_root,
        check="doctor",
        json_output=json_output,
    )


@config_app.command("validate")
def validate_config(
    profile: Annotated[
        Path,
        typer.Option("--config", help="Profile path relative to --config-root."),
    ] = DEFAULT_PROFILE,
    config_root: Annotated[
        Path,
        typer.Option(help="Trusted directory containing platform and experiment configuration."),
    ] = DEFAULT_CONFIG_ROOT,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Validate and hash one static platform profile."""

    _validate_or_exit(
        profile=profile,
        config_root=config_root,
        check="config",
        json_output=json_output,
    )


@secrets_app.command("bootstrap-local")
def bootstrap_local(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Create missing local database passwords and an operator token."""

    try:
        result = bootstrap_local_secrets(Path.cwd())
    except (LocalSecretBootstrapError, OSError):
        if json_output:
            typer.echo(
                json.dumps(
                    {"error": "local secret bootstrap failed", "status": "error"},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                err=True,
            )
        else:
            typer.echo("secrets bootstrap-local: error: local secret bootstrap failed", err=True)
        raise typer.Exit(code=2) from None
    _emit_bootstrap_result(result, json_output=json_output)


@db_app.command("migrate")
def migrate_database(
    database_url_file: Annotated[
        Path,
        typer.Option(
            "--database-url-file",
            help="Owner-private file containing the target PostgreSQL URL.",
        ),
    ],
    bootstrap_admin_database_url_file: Annotated[
        Path | None,
        typer.Option(
            "--bootstrap-admin-database-url-file",
            help="Separate administrator URL file used only for a governed role transition.",
        ),
    ] = None,
    profile: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Profile path beneath the trusted application root.",
        ),
    ] = DEFAULT_RUNTIME_CONFIG,
    application_root: Annotated[
        Path,
        typer.Option(help="Trusted deployment root containing configuration and secrets."),
    ] = Path("."),
) -> None:
    """Apply packaged migrations through the bounded migration role."""

    from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
    from adaptive_trader.platform.storage.migration_runner import migrate_platform_database

    try:
        root = _config_root(application_root)
        settings = load_runtime_settings(
            _audit_environment(
                profile=profile,
                database_url_file=database_url_file,
            ),
            service=RuntimeService.MIGRATE,
            application_root=root,
        )
        database_reference = settings.database_url_file
        if database_reference is None:
            raise RuntimeSettingsError("migration database URL is unavailable")
        database_url = database_reference.load()
        bootstrap_url = (
            None
            if bootstrap_admin_database_url_file is None
            else load_secret_file(
                bootstrap_admin_database_url_file,
                source=SecretFileVariable.DATABASE_URL,
            )
        )
        migrate_platform_database(
            database_url,
            application_root=root,
            bootstrap_admin_database_url=bootstrap_url,
            experiment=settings.platform.experiment.definition,
        )
    except (OSError, RuntimeError, ValueError):
        typer.echo("db migrate: error: migration failed", err=True)
        raise typer.Exit(code=2) from None
    typer.echo("db migrate: ok; revision=head")


@data_app.command("status")
def data_status(
    profile: Annotated[Path, typer.Option("--config")] = DEFAULT_PROFILE,
    config_root: Annotated[Path, typer.Option()] = DEFAULT_CONFIG_ROOT,
    application_root: Annotated[
        Path, typer.Option(help="Trusted root containing existing runtime storage.")
    ] = Path("."),
    database_url_file: Annotated[
        Path | None, typer.Option(help="Owner-private control/read-only PostgreSQL URL file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Observe persisted watermarks without creating storage or contacting a provider."""
    from adaptive_trader.platform.durable_status import durable_status

    try:
        config = _validated_config(profile, config_root)
    except ExperimentConfigError:
        typer.echo("data status: error: configuration invalid", err=True)
        raise typer.Exit(code=2) from None
    payload = {
        "check": "data status",
        "configured": {
            "mode": config.profile.mode.value,
            "adapter": config.profile.market_data_adapter.value,
        },
        **durable_status(
            kind="data",
            profile=profile,
            application_root=application_root,
            database_url_file=database_url_file,
        ),
    }
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"data status: {payload['status']}; observation={payload['observation']}; health={payload['health']}"
    )


@data_app.command("collect-once")
def collect_once(
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Enable safe collector diagnostics.")
    ] = False,
) -> None:
    """Resume canonical PostgreSQL coverage with finite REST collection and clean exit."""

    from adaptive_trader.collection.cli import collect_once as collect_canonical_once

    collect_canonical_once(verbose=verbose)


def _data_verification_command(
    *,
    command: str,
    config_root: Path,
    output: Path,
    json_output: bool,
) -> None:
    from adaptive_trader.platform.data_cli import (
        DataOperation,
        DataOperationError,
        run_data_operation,
    )

    operation: DataOperation
    if command == "data ingest-fixture":
        operation = "ingest-fixture"
    elif command == "data aggregate":
        operation = "aggregate"
    else:
        operation = "freeze"
    try:
        payload = run_data_operation(
            operation, config_root=_config_root(config_root), output=output
        )
    except (DataOperationError, OSError):
        if json_output:
            typer.echo(
                json.dumps(
                    {"check": command, "status": "error", "error": "offline data operation failed"},
                    sort_keys=True,
                ),
                err=True,
            )
        else:
            typer.echo(f"{command}: error: offline data operation failed", err=True)
        raise typer.Exit(code=2) from None
    if json_output:
        typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        typer.echo(
            f"{command}: ok; rows={payload['row_count']}; evidence={payload['evidence_path']}"
        )


@data_app.command("ingest-fixture")
def ingest_fixture(
    config_root: Annotated[
        Path,
        typer.Option(help="Trusted directory containing platform configuration."),
    ] = DEFAULT_CONFIG_ROOT,
    output: Annotated[
        Path,
        typer.Option(help="Relative path for the immutable offline evidence manifest."),
    ] = Path("outputs/data/ingest-fixture.json"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Persist deterministic fixture minutes through the canonical collector; retries are idempotent."""

    _data_verification_command(
        command="data ingest-fixture",
        config_root=config_root,
        output=output,
        json_output=json_output,
    )


@data_app.command("aggregate")
def aggregate_fixture(
    config_root: Annotated[
        Path,
        typer.Option(help="Trusted directory containing platform configuration."),
    ] = DEFAULT_CONFIG_ROOT,
    output: Annotated[
        Path,
        typer.Option(help="Relative path for the immutable offline evidence manifest."),
    ] = Path("outputs/data/aggregate.json"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Materialize 15-minute aggregates from already-persisted fixture minutes."""

    _data_verification_command(
        command="data aggregate",
        config_root=config_root,
        output=output,
        json_output=json_output,
    )


@data_app.command("freeze")
def freeze_fixture(
    config_root: Annotated[
        Path,
        typer.Option(help="Trusted directory containing platform configuration."),
    ] = DEFAULT_CONFIG_ROOT,
    output: Annotated[
        Path,
        typer.Option(help="Relative path for the immutable offline evidence manifest."),
    ] = Path("outputs/data/freeze.json"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Freeze persisted fixture aggregates to an immutable, non-promotable Parquet dataset."""

    _data_verification_command(
        command="data freeze",
        config_root=config_root,
        output=output,
        json_output=json_output,
    )


@scheduler_app.command("status")
def scheduler_status(
    config_root: Annotated[Path, typer.Option()] = DEFAULT_CONFIG_ROOT,
    profile: Annotated[Path, typer.Option("--config")] = DEFAULT_PROFILE,
    application_root: Annotated[
        Path, typer.Option(help="Trusted root containing existing runtime storage.")
    ] = Path("."),
    database_url_file: Annotated[
        Path | None, typer.Option(help="Owner-private control/read-only PostgreSQL URL file.")
    ] = None,
    preview: Annotated[
        bool, typer.Option(help="Show the fixed offline fixture schedule, not observed state.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Observe bounded current-session durable slots, or explicitly preview the fixture."""
    from adaptive_trader.platform.durable_status import durable_status

    try:
        config = _validated_config(profile, config_root)
    except ExperimentConfigError:
        typer.echo("scheduler status: error: configuration invalid", err=True)
        raise typer.Exit(code=2) from None
    payload: dict[str, object] = {
        "check": "scheduler status",
        "configured": {"mode": config.profile.mode.value},
    }
    if preview:
        from datetime import date

        from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
        from adaptive_trader.platform.scheduling import build_session_schedule

        schedule = build_session_schedule(
            experiment=config.experiment.definition,
            signal_provider_id="deterministic_fixture",
            signal_provider_version="1",
            session_date=date(2026, 7, 6),
            calendar=XnasExchangeCalendar(),
        )
        payload.update(
            {
                "status": "preview",
                "observation": "fixture_preview",
                "health": "not_evaluated",
                "session_date": schedule.session_date.isoformat(),
                "strategy_slots": len(schedule.strategy_slots),
                "forced_flat_slots": int(schedule.forced_flat_slot is not None),
            }
        )
    else:
        payload.update(
            durable_status(
                kind="scheduler",
                profile=profile,
                application_root=application_root,
                database_url_file=database_url_file,
            )
        )
    typer.echo(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if json_output
        else f"scheduler status: {payload['status']}; observation={payload['observation']}; health={payload['health']}"
    )


@shadow_app.command("propose-once")
def shadow_propose_once(
    slot_id: Annotated[str, typer.Option(help="Durable decision slot to propose for.")],
    config_root: Annotated[
        Path, typer.Option(help="Trusted configuration directory.")
    ] = DEFAULT_CONFIG_ROOT,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable outcome.")
    ] = False,
) -> None:
    """Persist an always-flat proposal using only the strategy database credential."""
    from datetime import UTC, datetime

    from adaptive_trader.platform.operational_strategy import propose_once
    from adaptive_trader.platform.storage.engine import create_platform_engine

    engine = None
    try:
        root = _config_root(config_root)
        environment = dict(os.environ)
        environment["AQA_CONFIG"] = "configs/platform/shadow.yaml"
        settings = load_runtime_settings(
            environment,
            service=RuntimeService.STRATEGY_WORKER,
            application_root=root.parent,
        )
        engine = create_platform_engine(settings, application_name="aqa-shadow-strategy")
        outcome = propose_once(
            engine=engine, settings=settings, slot_id=slot_id, now=datetime.now(UTC)
        )
    except Exception:
        typer.echo("shadow propose-once: error: proposal persistence failed", err=True)
        raise typer.Exit(code=2) from None
    finally:
        if engine is not None:
            engine.dispose()
    typer.echo(
        json.dumps(outcome, sort_keys=True)
        if json_output
        else f"shadow propose-once: {outcome['status']}"
    )
    if outcome["status"] == "blocked":
        raise typer.Exit(code=2)


@shadow_app.command("run-once")
def shadow_run_once(
    config_root: Annotated[
        Path, typer.Option(help="Trusted configuration directory.")
    ] = DEFAULT_CONFIG_ROOT,
    slot_id: Annotated[str | None, typer.Option(help="Durable decision slot to evaluate.")] = None,
    fixture: Annotated[bool, typer.Option(help="Explicit offline/fake diagnostic mode.")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable outcome.")
    ] = False,
) -> None:
    """Evaluate durable data/signals and persist a broker-free diagnostic risk/order plan."""
    from datetime import UTC, datetime

    from adaptive_trader.platform.shadow import run_shadow_once
    from adaptive_trader.platform.shadow_settings import (
        create_shadow_execution_engine,
        load_shadow_execution_settings,
    )

    engine = None
    try:
        root = _config_root(config_root)
        environment = dict(os.environ)
        environment["AQA_CONFIG"] = (
            "configs/platform/offline.yaml" if fixture else "configs/platform/shadow.yaml"
        )
        settings = load_shadow_execution_settings(
            environment,
            application_root=root.parent,
            fixture=fixture,
        )
        engine = create_shadow_execution_engine(settings)
        outcome = run_shadow_once(
            engine=engine,
            settings=settings,
            slot_id=slot_id,
            now=datetime.now(UTC),
            fixture=fixture,
        )
    except Exception:
        typer.echo("shadow run-once: error: diagnostic evaluation failed", err=True)
        raise typer.Exit(code=2) from None
    finally:
        if engine is not None:
            engine.dispose()
    if json_output:
        typer.echo(json.dumps(outcome, sort_keys=True))
    else:
        typer.echo(f"shadow run-once: {outcome['status']}")
    if outcome["status"] == "blocked":
        raise typer.Exit(code=2)


@app.command("demo")
def demo(
    config_root: Annotated[
        Path,
        typer.Option(help="Trusted directory containing platform configuration."),
    ] = DEFAULT_CONFIG_ROOT,
    output: Annotated[
        Path,
        typer.Option(help="Relative path for the immutable offline evidence manifest."),
    ] = Path("outputs/demo/evidence.json"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Run the deterministic credential-free vertical slice twice."""

    _offline_demo_result(
        command="demo",
        config_root=config_root,
        output=output,
        json_output=json_output,
    )


def _runtime_failure(command: str) -> None:
    typer.echo(f"{command}: error: service startup failed", err=True)
    raise typer.Exit(code=2)


@api_app.command("serve")
def api_serve(
    host: Annotated[
        str,
        typer.Option(help="Validated listen address for the private API."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(help="Fixed private API port."),
    ] = 8000,
    application_root: Annotated[
        Path,
        typer.Option(help="Trusted repository/application root."),
    ] = Path("."),
) -> None:
    """Run the authenticated control API without broker authority."""

    from adaptive_trader.platform.runtime import serve_control_api

    try:
        serve_control_api(
            host=host,
            port=port,
            application_root=application_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        _runtime_failure("api serve")


@dashboard_app.command("serve")
def dashboard_serve(
    host: Annotated[
        str,
        typer.Option(help="Validated listen address for the read-only dashboard."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(help="Fixed dashboard port."),
    ] = 8501,
    application_root: Annotated[
        Path,
        typer.Option(help="Trusted repository/application root."),
    ] = Path("."),
) -> None:
    """Run the read-only dashboard over the private API."""

    from adaptive_trader.platform.runtime import serve_dashboard

    try:
        serve_dashboard(
            host=host,
            port=port,
            application_root=application_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        _runtime_failure("dashboard serve")


def _implemented_worker_service(service: str) -> RuntimeService:
    implemented = frozenset(
        {
            RuntimeService.JOB_WORKER,
            RuntimeService.MARKET_DATA_WORKER,
            RuntimeService.SCHEDULER_WORKER,
            RuntimeService.STRATEGY_WORKER,
            RuntimeService.EXECUTION_WORKER,
            RuntimeService.MARKET_DATA_LIVE,
        }
    )
    try:
        selected = RuntimeService(service)
    except ValueError:
        typer.echo("service: error: service is not an implemented runtime", err=True)
        raise typer.Exit(code=2) from None
    if selected not in implemented:
        typer.echo("service: error: service is not an implemented runtime", err=True)
        raise typer.Exit(code=2)
    return selected


@service_app.command("run")
def service_run(
    service: Annotated[str, typer.Argument(help="Exact bounded service name.")],
    application_root: Annotated[
        Path,
        typer.Option(help="Trusted repository/application root."),
    ] = Path("."),
    once: Annotated[
        bool,
        typer.Option(help="Run one bounded queue and outbox cycle, then exit."),
    ] = False,
) -> None:
    """Run one exact bounded background worker."""

    from adaptive_trader.platform.runtime import run_platform_worker

    selected = _implemented_worker_service(service)
    try:
        run_platform_worker(
            service=selected,
            application_root=application_root,
            once=once,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        _runtime_failure("service run")


@service_app.command("health")
def service_health(
    service: Annotated[str, typer.Argument(help="Exact bounded service name.")],
) -> None:
    """Require a recent heartbeat from the selected live worker process."""

    from adaptive_trader.platform.runtime import platform_worker_is_healthy

    selected = _implemented_worker_service(service)
    if not platform_worker_is_healthy(service=selected):
        typer.echo("service health: not ready", err=True)
        raise typer.Exit(code=1)
    typer.echo("service health: ready")


@audit_app.command("verify")
def verify_audit(
    profile: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Profile path beneath the application root (default: offline).",
        ),
    ] = DEFAULT_RUNTIME_CONFIG,
    database_url_file: Annotated[
        Path | None,
        typer.Option(
            "--database-url-file",
            help="Opaque database URL secret-file reference beneath the application root.",
        ),
    ] = None,
    application_root: Annotated[
        Path | None,
        typer.Option(
            "--application-root",
            help="Trusted application root containing configuration and runtime storage.",
        ),
    ] = None,
    stream_id: Annotated[
        str | None,
        typer.Option("--stream", help="Verify only one complete audit stream."),
    ] = None,
    expected_sequence: Annotated[
        int | None,
        typer.Option(
            "--expected-sequence",
            help="Expected terminal sequence for the requested stream.",
        ),
    ] = None,
    expected_hash: Annotated[
        str | None,
        typer.Option(
            "--expected-hash",
            help="Expected terminal event hash for the requested stream.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Verify audit payloads, IDs, hashes, sequence continuity, and stream heads."""

    try:
        selected_root = _config_root(Path.cwd() if application_root is None else application_root)
        settings = load_runtime_settings(
            _audit_environment(profile=profile, database_url_file=database_url_file),
            service=RuntimeService.AUDIT_VERIFIER,
            application_root=selected_root,
        )
        engine = create_platform_read_only_engine(
            settings,
            application_name="aqa-audit-verify",
        )
        try:
            report = AuditRepository(engine).verify(
                stream_id=stream_id,
                expected_sequence=expected_sequence,
                expected_hash=expected_hash,
            )
        finally:
            engine.dispose()
    except (
        AuditIntegrityError,
        AuditPersistenceError,
        AuditValidationError,
        ExperimentConfigError,
        RuntimeSettingsError,
    ):
        _emit_audit_failure(json_output=json_output)
        raise typer.Exit(code=2) from None
    _emit_audit_success(report, json_output=json_output)


def main() -> None:
    """Run the platform command group."""

    app()


if __name__ == "__main__":
    main()
