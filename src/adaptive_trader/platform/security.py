"""POSIX secret-file loading with redacted in-memory values."""

from __future__ import annotations

import os
import stat
from enum import StrEnum
from pathlib import Path
from typing import Any, SupportsIndex

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

_MAX_SECRET_BYTES = 16 * 1024
_MAX_SECRET_PATH_BYTES = 4096
_REDACTED = "<redacted>"
_CONCRETE_PATH_TYPE = type(Path())
_SECRET_CONSTRUCTION_TOKEN = object()


class SecretFileVariable(StrEnum):
    """The complete secret-file namespace available to new platform services."""

    DATABASE_URL = "AQA_DATABASE_URL_FILE"
    OPERATOR_TOKEN = "AQA_OPERATOR_TOKEN_FILE"
    ALPACA_DATA_API_KEY = "AQA_ALPACA_DATA_API_KEY_FILE"
    ALPACA_DATA_SECRET_KEY = "AQA_ALPACA_DATA_SECRET_KEY_FILE"
    ALPACA_PAPER_API_KEY = "AQA_ALPACA_PAPER_API_KEY_FILE"
    ALPACA_PAPER_SECRET_KEY = "AQA_ALPACA_PAPER_SECRET_KEY_FILE"
    PAPER_ACCOUNT_ID_HASH = "AQA_PAPER_ACCOUNT_ID_HASH_FILE"


class SecretFileError(ValueError):
    """Raised when a secret file cannot be accepted without weakening its boundary."""


class _RejectedSecretInput:
    __slots__ = ()

    def __repr__(self) -> str:
        return _REDACTED


_REJECTED_SECRET_INPUT = _RejectedSecretInput()


class RedactedSecret:
    """An immutable in-memory secret with closed construction and redacted rendering."""

    __slots__ = ("__value",)
    __value: str

    def __init__(self, value: str, *, _token: object) -> None:
        if _token is not _SECRET_CONSTRUCTION_TOKEN or type(value) is not str:
            raise TypeError("redacted secrets must be loaded from a secret file")
        object.__setattr__(self, "_RedactedSecret__value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("redacted secrets are immutable")

    def __str__(self) -> str:
        return _REDACTED

    def __repr__(self) -> str:
        return _REDACTED

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __copy__(self) -> RedactedSecret:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> RedactedSecret:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("redacted secrets cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError("redacted secrets cannot be serialized")

    def reveal(self) -> str:
        """Return the value only at an explicitly authorized adapter boundary."""

        return self.__value

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        del source_type, handler

        def loaded_instance_or_redacted_marker(value: object) -> object:
            if type(value) is cls:
                return value
            return _REJECTED_SECRET_INPUT

        def serialize_redacted(value: object) -> str:
            return _REDACTED

        return core_schema.chain_schema(
            [
                core_schema.no_info_plain_validator_function(
                    loaded_instance_or_redacted_marker,
                ),
                core_schema.is_instance_schema(cls),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(
                serialize_redacted,
                return_schema=core_schema.str_schema(),
                when_used="always",
            ),
        )


def _error(source: SecretFileVariable, reason: str) -> SecretFileError:
    return SecretFileError(f"{source.value}: secret file {reason}")


def _posix_open_flags(source: SecretFileVariable) -> tuple[int, int]:
    flag_names = ("O_RDONLY", "O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    flags: dict[str, int] = {}
    for name in flag_names:
        value = getattr(os, name, None)
        if type(value) is not int:
            raise _error(source, "loading is unsupported on this platform")
        flags[name] = value

    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", ())
    if (
        not callable(getattr(os, "geteuid", None))
        or os.open not in supports_dir_fd
        or os.stat not in supports_dir_fd
        or os.stat not in supports_follow_symlinks
    ):
        raise _error(source, "loading is unsupported on this platform")

    directory_flags = (
        flags["O_RDONLY"] | flags["O_CLOEXEC"] | flags["O_DIRECTORY"] | flags["O_NOFOLLOW"]
    )
    file_flags = flags["O_RDONLY"] | flags["O_CLOEXEC"] | flags["O_NOFOLLOW"] | flags["O_NONBLOCK"]
    return directory_flags, file_flags


def _secret_path_components(
    path: str | Path,
    *,
    source: SecretFileVariable,
) -> tuple[bool, tuple[str, ...]]:
    if type(path) not in {str, _CONCRETE_PATH_TYPE}:
        raise _error(source, "path must be exact text or pathlib.Path")
    rendered = os.fspath(path)
    if type(rendered) is not str or not rendered or "\x00" in rendered or "\\" in rendered:
        raise _error(source, "path is not a canonical POSIX path")

    encoding_failed = False
    encoded = b""
    try:
        encoded = rendered.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed or len(encoded) > _MAX_SECRET_PATH_BYTES:
        raise _error(source, "path is not bounded UTF-8")

    is_absolute = rendered.startswith("/")
    components = rendered.split("/")
    if is_absolute:
        components = components[1:]
    if not components or any(component in {"", ".", ".."} for component in components):
        raise _error(source, "path contains a prohibited component")
    return is_absolute, tuple(components)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_walk_root(
    *,
    is_absolute: bool,
    components: tuple[str, ...],
    directory_flags: int,
) -> tuple[int, tuple[str, ...]]:
    if not is_absolute:
        return os.open(".", directory_flags), components

    current_directory = os.open(".", directory_flags)
    use_current_directory = False
    try:
        current_path = os.getcwd()
        current_components = tuple(part for part in current_path.split("/") if part)
        current_status = os.stat(current_path, follow_symlinks=False)
        descriptor_status = os.fstat(current_directory)
        use_current_directory = (
            len(components) > len(current_components)
            and components[: len(current_components)] == current_components
            and _same_identity(current_status, descriptor_status)
        )
        if use_current_directory:
            return current_directory, components[len(current_components) :]
    finally:
        if not use_current_directory:
            os.close(current_directory)

    return os.open("/", directory_flags), components


def load_secret_file(
    path: str | Path,
    *,
    source: SecretFileVariable,
) -> RedactedSecret:
    """Read one owner-private regular file without following symbolic links.

    Every path component is opened relative to a pinned directory descriptor. Exactly one terminal
    LF is removed; all other bytes remain part of the secret. Errors contain only the allowlisted
    source variable and a stable reason, never a path, value, or operating-system exception.
    """

    if type(source) is not SecretFileVariable:
        raise SecretFileError("secret file source is not supported")
    directory_flags, file_flags = _posix_open_flags(source)
    is_absolute, components = _secret_path_components(path, source=source)

    failure_reason: str | None = None
    directory_descriptor = -1
    file_descriptor = -1
    payload = b""
    try:
        directory_descriptor, remaining_components = _open_walk_root(
            is_absolute=is_absolute,
            components=components,
            directory_flags=directory_flags,
        )

        for component in remaining_components[:-1]:
            component_status = os.stat(
                component,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(component_status.st_mode):
                failure_reason = "path contains a symbolic link"
                break
            if not stat.S_ISDIR(component_status.st_mode):
                failure_reason = "path component is not a directory"
                break

            next_descriptor = -1
            try:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                next_status = os.fstat(next_descriptor)
                if not stat.S_ISDIR(next_status.st_mode) or not _same_identity(
                    component_status,
                    next_status,
                ):
                    failure_reason = "path changed during validation"
                else:
                    os.close(directory_descriptor)
                    directory_descriptor = next_descriptor
                    next_descriptor = -1
            finally:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
            if failure_reason is not None:
                break

        if failure_reason is None:
            leaf = remaining_components[-1]
            leaf_status = os.stat(
                leaf,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(leaf_status.st_mode):
                failure_reason = "path contains a symbolic link"
            elif not stat.S_ISREG(leaf_status.st_mode):
                failure_reason = "is not a regular file"
            else:
                file_descriptor = os.open(
                    leaf,
                    file_flags,
                    dir_fd=directory_descriptor,
                )
                file_status = os.fstat(file_descriptor)
                permissions = stat.S_IMODE(file_status.st_mode)
                if not stat.S_ISREG(file_status.st_mode):
                    failure_reason = "is not a regular file"
                elif not _same_identity(leaf_status, file_status):
                    failure_reason = "path changed during validation"
                elif file_status.st_uid != os.geteuid():
                    failure_reason = "is not owned by the current user"
                elif permissions not in {0o400, 0o600}:
                    failure_reason = "permissions are not owner-private"
                else:
                    before_read = (
                        file_status.st_dev,
                        file_status.st_ino,
                        file_status.st_uid,
                        file_status.st_mode,
                        file_status.st_size,
                        file_status.st_mtime_ns,
                    )
                    with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                        payload = stream.read(_MAX_SECRET_BYTES + 1)
                    after_status = os.fstat(file_descriptor)
                    after_read = (
                        after_status.st_dev,
                        after_status.st_ino,
                        after_status.st_uid,
                        after_status.st_mode,
                        after_status.st_size,
                        after_status.st_mtime_ns,
                    )
                    if before_read != after_read:
                        failure_reason = "changed while being read"
    except FileNotFoundError:
        failure_reason = "is missing"
    except (OSError, RuntimeError, TypeError, ValueError):
        failure_reason = "could not be read safely"
    finally:
        for descriptor in (file_descriptor, directory_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    failure_reason = "could not be closed safely"

    if failure_reason is not None:
        raise _error(source, failure_reason)
    if len(payload) > _MAX_SECRET_BYTES:
        raise _error(source, "exceeds the size limit")
    if b"\x00" in payload:
        raise _error(source, "contains a NUL byte")

    decoding_failed = False
    value = ""
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        decoding_failed = True
    if decoding_failed:
        raise _error(source, "is not valid UTF-8")
    if value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise _error(source, "is empty")
    return RedactedSecret(value, _token=_SECRET_CONSTRUCTION_TOKEN)
