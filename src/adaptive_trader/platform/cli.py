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
    load_platform_config,
)
from adaptive_trader.platform.security import (
    LocalSecretBootstrapError,
    LocalSecretBootstrapResult,
    bootstrap_local_secrets,
)

DEFAULT_CONFIG_ROOT = Path("configs")
DEFAULT_PROFILE = Path("platform/offline.yaml")

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
app.add_typer(config_app, name="config")
app.add_typer(secrets_app, name="secrets")


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


def main() -> None:
    """Run the platform command group."""

    app()


if __name__ == "__main__":
    main()
