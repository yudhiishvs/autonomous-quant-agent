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
from adaptive_trader.platform.security import (
    LocalSecretBootstrapError,
    LocalSecretBootstrapResult,
    bootstrap_local_secrets,
)
from adaptive_trader.platform.storage import AuditRepository, create_platform_read_only_engine

DEFAULT_CONFIG_ROOT = Path("configs")
DEFAULT_PROFILE = Path("platform/offline.yaml")
DEFAULT_RUNTIME_CONFIG = DEFAULT_CONFIG_ROOT / DEFAULT_PROFILE

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
app.add_typer(config_app, name="config")
app.add_typer(secrets_app, name="secrets")
app.add_typer(audit_app, name="audit")


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
