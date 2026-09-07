"""Materialize process-scoped credentials, then replace this launcher."""

from __future__ import annotations

import os
import re
import stat
import sys
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

from adaptive_trader.platform.security import SecretFileVariable, load_secret_file

_APPLICATION_ROOT = Path("/app")
_DATABASE_NAME = "aqa"
_DATABASE_HOST = "postgres"
_DATABASE_PORT = 5432
# Compose provides a private tmpfs; the child is 0700 and the file is exclusive/no-follow.
_DATABASE_URL_PATH = Path("/tmp/aqa/database-url")  # nosec B108
_DASHBOARD_TOKEN_DIRECTORY = _APPLICATION_ROOT / "runtime" / "dashboard-auth"
_DASHBOARD_TOKEN_NAME = "read-token"
_DASHBOARD_TOKEN_TEMPORARY_NAME = ".read-token.pending"
_OPERATOR_TOKEN_PATH = Path("/run/secrets/operator_token")
_SECRET_ROOT = _APPLICATION_ROOT / "secrets"
_DASHBOARD_TOKEN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ROLE_CONFIGURATION = {
    "postgres": ("postgres", "postgres_password"),
    "aqa_migrate": ("aqa_migrate_login", "aqa_migrate_password"),
    "aqa_collector": ("aqa_collector_login", "aqa_collector_password"),
    "aqa_scheduler": ("aqa_scheduler_login", "aqa_scheduler_password"),
    "aqa_strategy": ("aqa_strategy_login", "aqa_strategy_password"),
    "aqa_execution": ("aqa_execution_login", "aqa_execution_password"),
    "aqa_control": ("aqa_control_login", "aqa_control_password"),
    "aqa_readonly": ("aqa_readonly_login", "aqa_readonly_password"),
}


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("private credential write did not progress")
        offset += written


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o700
        ):
            raise OSError("private credential directory has unsafe ownership or permissions")
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SystemExit("container credential directory is not private") from None
    return descriptor


def _validate_existing_dashboard_token(directory_descriptor: int) -> None:
    try:
        status = os.stat(
            _DASHBOARD_TOKEN_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_size != 64
    ):
        raise SystemExit("existing dashboard credential is not private")


def _materialize_dashboard_read_token() -> None:
    if os.environ.get("AQA_OPERATOR_TOKEN_FILE") != _OPERATOR_TOKEN_PATH.as_posix():
        raise SystemExit("control API operator credential source is invalid")

    # Import only for the platform image. The data-only image intentionally omits API dependencies.
    from adaptive_trader.platform.control.auth import derive_dashboard_read_token

    operator_secret = load_secret_file(
        _OPERATOR_TOKEN_PATH,
        source=SecretFileVariable.OPERATOR_TOKEN,
    )
    token = derive_dashboard_read_token(operator_secret)
    if _DASHBOARD_TOKEN.fullmatch(token) is None:
        raise SystemExit("derived dashboard credential is invalid")

    directory_descriptor = _open_private_directory(_DASHBOARD_TOKEN_DIRECTORY)
    temporary_descriptor = -1
    temporary_created = False
    try:
        _validate_existing_dashboard_token(directory_descriptor)
        try:
            os.stat(
                _DASHBOARD_TOKEN_TEMPORARY_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SystemExit("stale dashboard credential materialization was detected")

        temporary_descriptor = os.open(
            _DASHBOARD_TOKEN_TEMPORARY_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        payload = token.encode("ascii")
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        created_status = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISREG(created_status.st_mode)
            or created_status.st_uid != os.geteuid()
            or stat.S_IMODE(created_status.st_mode) != 0o600
            or created_status.st_size != len(payload)
        ):
            raise SystemExit("derived dashboard credential was not created privately")
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        os.replace(
            _DASHBOARD_TOKEN_TEMPORARY_NAME,
            _DASHBOARD_TOKEN_NAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_created = False
        os.fsync(directory_descriptor)
    except SystemExit:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SystemExit("dashboard credential could not be materialized safely") from None
    finally:
        if temporary_descriptor >= 0:
            with suppress(OSError):
                os.close(temporary_descriptor)
        if temporary_created:
            with suppress(OSError):
                os.unlink(_DASHBOARD_TOKEN_TEMPORARY_NAME, dir_fd=directory_descriptor)
        with suppress(OSError):
            os.close(directory_descriptor)


def _write_database_url(role: str) -> None:
    try:
        username, filename = _ROLE_CONFIGURATION[role]
    except KeyError:
        raise SystemExit("container startup rejected an unsupported database role") from None

    password = load_secret_file(
        _SECRET_ROOT / filename,
        source=SecretFileVariable.DATABASE_URL,
    ).reveal()
    _DATABASE_URL_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        _DATABASE_URL_PATH,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        rendered = (
            "postgresql+psycopg://"
            f"{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{_DATABASE_HOST}:{_DATABASE_PORT}/{_DATABASE_NAME}"
        )
        _write_all(descriptor, rendered.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.environ["AQA_DATABASE_URL_FILE"] = _DATABASE_URL_PATH.as_posix()


def main() -> None:
    """Prepare only the requested database identity and execute the service command."""

    role = os.environ.pop("CONTAINER_DATABASE_ROLE", "")
    if role:
        _write_database_url(role)
    derive_dashboard_token = os.environ.pop("CONTAINER_DERIVE_DASHBOARD_READ_TOKEN", "")
    if derive_dashboard_token:
        if derive_dashboard_token != "YES":
            raise SystemExit("dashboard credential derivation flag is invalid")
        _materialize_dashboard_read_token()
    if len(sys.argv) < 2:
        raise SystemExit("container startup requires an explicit service command")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
