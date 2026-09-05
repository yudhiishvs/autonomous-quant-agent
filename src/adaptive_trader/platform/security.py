"""POSIX secret-file loading with redacted in-memory values."""

from __future__ import annotations

import fcntl
import os
import secrets as secrets_module
import stat
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, SupportsIndex

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

from adaptive_trader.platform.errors import LocalSecretBootstrapError, SecretFileError

_MAX_SECRET_BYTES = 16 * 1024
_MAX_SECRET_PATH_BYTES = 4096
_REDACTED = "<redacted>"
_CONCRETE_PATH_TYPE = type(Path())
_SECRET_CONSTRUCTION_TOKEN = object()
_SECRET_REFERENCE_CONSTRUCTION_TOKEN = object()
_LOCAL_SECRET_DIRECTORY = "secrets"
_LOCAL_SECRET_TOKEN_BYTES = 48
_LOCAL_BOOTSTRAP_TEMPORARY_PREFIX = ".bootstrap-"
_LOCAL_BOOTSTRAP_LOCK = threading.Lock()

LOCAL_BOOTSTRAP_FILENAMES = (
    "postgres_password",
    "aqa_migrate_password",
    "aqa_collector_password",
    "aqa_scheduler_password",
    "aqa_strategy_password",
    "aqa_execution_password",
    "aqa_control_password",
    "aqa_readonly_password",
    "operator_token",
)


class SecretFileVariable(StrEnum):
    """The complete secret-file namespace available to new platform services."""

    DATABASE_URL = "AQA_DATABASE_URL_FILE"
    OPERATOR_TOKEN = "AQA_OPERATOR_TOKEN_FILE"
    ALPACA_DATA_API_KEY = "AQA_ALPACA_DATA_API_KEY_FILE"
    ALPACA_DATA_SECRET_KEY = "AQA_ALPACA_DATA_SECRET_KEY_FILE"
    ALPACA_PAPER_API_KEY = "AQA_ALPACA_PAPER_API_KEY_FILE"
    ALPACA_PAPER_SECRET_KEY = "AQA_ALPACA_PAPER_SECRET_KEY_FILE"
    PAPER_ACCOUNT_ID_HASH = "AQA_PAPER_ACCOUNT_ID_HASH_FILE"


@dataclass(frozen=True, slots=True)
class LocalSecretBootstrapResult:
    """Names created or preserved by one local bootstrap invocation."""

    created: tuple[str, ...]
    skipped: tuple[str, ...]


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

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_: core_schema.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Describe only the redacted output surface, never accepted secret input."""

        del core_schema_
        if handler.mode == "validation":
            return {
                "not": {},
                "description": "Python RedactedSecret instance only; JSON input is rejected.",
            }
        return {
            "type": "string",
            "const": _REDACTED,
            "readOnly": True,
            "description": "Output-only redacted secret marker.",
        }


class SecretFileReference:
    """An immutable, redacted reference to one lexically validated secret file."""

    __slots__ = ("__path", "__source")
    __path: Path
    __source: SecretFileVariable

    def __init__(
        self,
        path: Path,
        *,
        source: SecretFileVariable,
        _token: object,
    ) -> None:
        if (
            _token is not _SECRET_REFERENCE_CONSTRUCTION_TOKEN
            or type(path) is not _CONCRETE_PATH_TYPE
            or type(source) is not SecretFileVariable
        ):
            raise TypeError("secret file references must be created with from_path")
        object.__setattr__(self, "_SecretFileReference__path", path)
        object.__setattr__(self, "_SecretFileReference__source", source)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("secret file references are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("secret file references are immutable")

    def __str__(self) -> str:
        return f"<secret-file-reference source={self.__source.value} configured=true>"

    def __repr__(self) -> str:
        return str(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __copy__(self) -> SecretFileReference:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> SecretFileReference:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("secret file references cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError("secret file references cannot be serialized")

    @property
    def source(self) -> SecretFileVariable:
        """Return the allowlisted source identity without disclosing the path."""

        return self.__source

    @property
    def configured(self) -> bool:
        """Report that this closed reference was successfully configured."""

        return True

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        source: SecretFileVariable,
        application_root: Path,
    ) -> SecretFileReference:
        """Create a reference using canonical POSIX path syntax without filesystem access."""

        if cls is not SecretFileReference:
            raise TypeError("secret file references do not support subclass factories")
        if type(source) is not SecretFileVariable:
            raise SecretFileError("secret file source is not supported")
        if type(application_root) is not _CONCRETE_PATH_TYPE:
            raise _error(source, "application root must be an exact pathlib.Path")

        root_is_absolute, root_components = _secret_path_components(
            application_root,
            source=source,
            allow_root=True,
        )
        if not root_is_absolute:
            raise _error(source, "application root must be absolute")

        path_is_absolute, path_components = _secret_path_components(path, source=source)
        absolute_components = (
            path_components if path_is_absolute else root_components + path_components
        )
        absolute_rendered = "/" + "/".join(absolute_components)
        if len(absolute_rendered.encode("utf-8")) > _MAX_SECRET_PATH_BYTES:
            raise _error(source, "path is not bounded UTF-8")

        absolute_path = _CONCRETE_PATH_TYPE(absolute_rendered)
        return SecretFileReference(
            absolute_path,
            source=source,
            _token=_SECRET_REFERENCE_CONSTRUCTION_TOKEN,
        )

    def load(self) -> RedactedSecret:
        """Load the referenced value through the hardened secret-file boundary."""

        return load_secret_file(self.__path, source=self.__source)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        del source_type, handler

        def configured_instance_or_redacted_marker(value: object) -> object:
            if type(value) is cls:
                return value
            return _REJECTED_SECRET_INPUT

        def serialize_metadata(value: object) -> dict[str, str | bool]:
            if type(value) is not cls:
                return {"source": "unsupported", "configured": False}
            reference = value
            return {
                "source": reference.__source.value,
                "configured": True,
            }

        metadata_schema = core_schema.typed_dict_schema(
            {
                "source": core_schema.typed_dict_field(core_schema.str_schema()),
                "configured": core_schema.typed_dict_field(core_schema.bool_schema()),
            }
        )
        return core_schema.chain_schema(
            [
                core_schema.no_info_plain_validator_function(
                    configured_instance_or_redacted_marker,
                ),
                core_schema.is_instance_schema(cls),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(
                serialize_metadata,
                return_schema=metadata_schema,
                when_used="always",
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_: core_schema.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Describe safe reference metadata without exposing the configured path."""

        del core_schema_
        if handler.mode == "validation":
            return {
                "not": {},
                "description": "Python SecretFileReference instance only; JSON input is rejected.",
            }
        return {
            "type": "object",
            "readOnly": True,
            "additionalProperties": False,
            "properties": {
                "source": {
                    "type": "string",
                    "enum": [source.value for source in SecretFileVariable],
                },
                "configured": {"type": "boolean", "const": True},
            },
            "required": ["source", "configured"],
            "description": "Output-only secret-file reference metadata.",
        }


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
    allow_root: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    if type(path) not in {str, _CONCRETE_PATH_TYPE}:
        raise _error(source, "path must be exact text or pathlib.Path")
    rendered = os.fspath(path)
    if type(rendered) is not str or not rendered or "\x00" in rendered or "\\" in rendered:
        raise _error(source, "path is not a canonical POSIX path")
    if len(rendered) > _MAX_SECRET_PATH_BYTES:
        raise _error(source, "path is not bounded UTF-8")

    encoding_failed = False
    encoded = b""
    try:
        encoded = rendered.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed or len(encoded) > _MAX_SECRET_PATH_BYTES:
        raise _error(source, "path is not bounded UTF-8")

    is_absolute = rendered.startswith("/")
    if allow_root and rendered == "/":
        return True, ()
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
                        file_status.st_ctime_ns,
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
                        after_status.st_ctime_ns,
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


def _bootstrap_error(reason: str) -> LocalSecretBootstrapError:
    return LocalSecretBootstrapError(f"local secret bootstrap {reason}")


def _bootstrap_open_flags() -> tuple[int, int, int]:
    flag_names = (
        "O_RDONLY",
        "O_WRONLY",
        "O_CLOEXEC",
        "O_CREAT",
        "O_EXCL",
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_NONBLOCK",
    )
    flags: dict[str, int] = {}
    for name in flag_names:
        value = getattr(os, name, None)
        if type(value) is not int:
            raise _bootstrap_error("is unsupported on this platform")
        flags[name] = value

    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    supports_fd = getattr(os, "supports_fd", ())
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", ())
    if (
        not callable(getattr(os, "geteuid", None))
        or os.chmod not in supports_dir_fd
        or os.link not in supports_dir_fd
        or os.link not in supports_follow_symlinks
        or os.listdir not in supports_fd
        or os.mkdir not in supports_dir_fd
        or os.open not in supports_dir_fd
        or os.stat not in supports_dir_fd
        or os.stat not in supports_follow_symlinks
        or os.unlink not in supports_dir_fd
    ):
        raise _bootstrap_error("is unsupported on this platform")

    directory_flags = (
        flags["O_RDONLY"] | flags["O_CLOEXEC"] | flags["O_DIRECTORY"] | flags["O_NOFOLLOW"]
    )
    file_flags = (
        flags["O_WRONLY"]
        | flags["O_CLOEXEC"]
        | flags["O_CREAT"]
        | flags["O_EXCL"]
        | flags["O_NOFOLLOW"]
    )
    existing_file_flags = (
        flags["O_RDONLY"] | flags["O_CLOEXEC"] | flags["O_NOFOLLOW"] | flags["O_NONBLOCK"]
    )
    return directory_flags, file_flags, existing_file_flags


def _close_bootstrap_descriptor(
    descriptor: int,
    *,
    failure: LocalSecretBootstrapError | None,
    resource: str,
) -> LocalSecretBootstrapError | None:
    try:
        os.close(descriptor)
    except OSError:
        if failure is None:
            return _bootstrap_error(f"could not close {resource} safely")
    return failure


def _lock_bootstrap_root(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _bootstrap_error("could not lock the application root safely") from None


def _bootstrap_root_components(application_root: Path) -> tuple[str, ...]:
    if type(application_root) is not _CONCRETE_PATH_TYPE:
        raise _bootstrap_error("requires an exact pathlib.Path application root")
    rendered = os.fspath(application_root)
    if (
        type(rendered) is not str
        or not rendered.startswith("/")
        or rendered == "/"
        or "\x00" in rendered
        or "\\" in rendered
    ):
        raise _bootstrap_error("requires a canonical absolute application root")
    try:
        encoded = rendered.encode("utf-8")
    except UnicodeEncodeError:
        raise _bootstrap_error("application root is not bounded UTF-8") from None
    if len(encoded) > _MAX_SECRET_PATH_BYTES:
        raise _bootstrap_error("application root is not bounded UTF-8")
    components = tuple(rendered.split("/")[1:])
    if not components or any(component in {"", ".", ".."} for component in components):
        raise _bootstrap_error("requires a canonical absolute application root")
    return components


def _open_bootstrap_root(application_root: Path, *, directory_flags: int) -> int:
    components = _bootstrap_root_components(application_root)
    directory_descriptor = -1
    failure: LocalSecretBootstrapError | None = None
    try:
        directory_descriptor, remaining_components = _open_walk_root(
            is_absolute=True,
            components=components,
            directory_flags=directory_flags,
        )
        for component in remaining_components:
            component_status = os.stat(
                component,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(component_status.st_mode):
                raise _bootstrap_error("application root contains a symbolic link")
            if not stat.S_ISDIR(component_status.st_mode):
                raise _bootstrap_error("application root is not a directory")
            next_descriptor = os.open(component, directory_flags, dir_fd=directory_descriptor)
            try:
                next_status = os.fstat(next_descriptor)
                if not stat.S_ISDIR(next_status.st_mode) or not _same_identity(
                    component_status, next_status
                ):
                    raise _bootstrap_error("application root changed during validation")
            except LocalSecretBootstrapError as error:
                close_failure = _close_bootstrap_descriptor(
                    next_descriptor,
                    failure=error,
                    resource="the application root",
                )
                raise (close_failure or error) from error
            except (OSError, RuntimeError, TypeError, ValueError):
                translated = _bootstrap_error("could not open the application root safely")
                close_failure = _close_bootstrap_descriptor(
                    next_descriptor,
                    failure=translated,
                    resource="the application root",
                )
                raise (close_failure or translated) from None

            close_failure = _close_bootstrap_descriptor(
                directory_descriptor,
                failure=None,
                resource="the application root",
            )
            if close_failure is not None:
                close_failure = _close_bootstrap_descriptor(
                    next_descriptor,
                    failure=close_failure,
                    resource="the application root",
                )
                directory_descriptor = -1
                raise close_failure or _bootstrap_error(
                    "could not close the application root safely"
                )
            directory_descriptor = next_descriptor

        root_status = os.fstat(directory_descriptor)
        if root_status.st_uid != os.geteuid():
            raise _bootstrap_error("application root is not owned by the current user")
        if stat.S_IMODE(root_status.st_mode) & 0o022:
            raise _bootstrap_error("application root is writable by another user")
    except LocalSecretBootstrapError as error:
        failure = error
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        failure = _bootstrap_error("could not open the application root safely")

    if failure is not None:
        original_failure = failure
        if directory_descriptor >= 0:
            failure = _close_bootstrap_descriptor(
                directory_descriptor,
                failure=failure,
                resource="the application root",
            )
        raise failure or original_failure
    return directory_descriptor


def _open_local_secret_directory(root_descriptor: int, *, directory_flags: int) -> int:
    created = False
    directory_descriptor = -1
    failure: LocalSecretBootstrapError | None = None
    try:
        os.mkdir(_LOCAL_SECRET_DIRECTORY, mode=0o700, dir_fd=root_descriptor)
        created = True
    except FileExistsError:
        pass
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _bootstrap_error("could not create the secret directory safely") from None

    try:
        path_status = os.stat(
            _LOCAL_SECRET_DIRECTORY,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(path_status.st_mode):
            raise _bootstrap_error("secret directory is a symbolic link")
        if not stat.S_ISDIR(path_status.st_mode):
            raise _bootstrap_error("secret directory is not a directory")
        if created:
            # The pinned root rejects other-UID writers. Linux lacks no-follow chmod, so identity
            # is checked again before the directory is opened with O_NOFOLLOW.
            os.chmod(_LOCAL_SECRET_DIRECTORY, 0o700, dir_fd=root_descriptor)
            adjusted_status = os.stat(
                _LOCAL_SECRET_DIRECTORY,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if not _same_identity(path_status, adjusted_status):
                raise _bootstrap_error("secret directory changed during creation")
            path_status = adjusted_status
        directory_descriptor = os.open(
            _LOCAL_SECRET_DIRECTORY,
            directory_flags,
            dir_fd=root_descriptor,
        )
        opened_status = os.fstat(directory_descriptor)
        if not _same_identity(path_status, opened_status):
            raise _bootstrap_error("secret directory changed during validation")
        if opened_status.st_uid != os.geteuid():
            raise _bootstrap_error("secret directory is not owned by the current user")
        if stat.S_IMODE(opened_status.st_mode) != 0o700:
            raise _bootstrap_error("secret directory permissions are not 0700")
        if created:
            os.fsync(root_descriptor)
    except LocalSecretBootstrapError as error:
        failure = error
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        failure = _bootstrap_error("could not open the secret directory safely")

    if failure is not None:
        original_failure = failure
        if directory_descriptor >= 0:
            failure = _close_bootstrap_descriptor(
                directory_descriptor,
                failure=failure,
                resource="the secret directory",
            )
        raise failure or original_failure
    return directory_descriptor


def _reject_stale_bootstrap_temporaries(directory_descriptor: int) -> None:
    try:
        entries = os.listdir(directory_descriptor)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _bootstrap_error("could not inspect the secret directory safely") from None
    if any(
        type(entry) is not str or entry.startswith(_LOCAL_BOOTSTRAP_TEMPORARY_PREFIX)
        for entry in entries
    ):
        raise _bootstrap_error("found an unresolved temporary secret")


def _validate_existing_local_secret(
    name: str,
    *,
    directory_descriptor: int,
    existing_file_flags: int,
) -> None:
    file_descriptor = -1
    failure: LocalSecretBootstrapError | None = None
    payload = b""
    expected_size = -1
    try:
        path_status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(path_status.st_mode):
            raise _bootstrap_error("an existing secret is a symbolic link")
        if not stat.S_ISREG(path_status.st_mode):
            raise _bootstrap_error("an existing secret is not a regular file")
        file_descriptor = os.open(name, existing_file_flags, dir_fd=directory_descriptor)
        opened_status = os.fstat(file_descriptor)
        if not _same_identity(path_status, opened_status):
            raise _bootstrap_error("an existing secret changed during validation")
        if opened_status.st_uid != os.geteuid():
            raise _bootstrap_error("an existing secret is not owned by the current user")
        if stat.S_IMODE(opened_status.st_mode) != 0o600:
            raise _bootstrap_error("existing secret permissions are not 0600")
        if opened_status.st_size > _MAX_SECRET_BYTES:
            raise _bootstrap_error("an existing secret exceeds the size limit")
        expected_size = opened_status.st_size
        before_read = (
            opened_status.st_dev,
            opened_status.st_ino,
            opened_status.st_size,
            opened_status.st_mtime_ns,
            opened_status.st_ctime_ns,
        )
        chunks: list[bytes] = []
        remaining = _MAX_SECRET_BYTES + 1
        while remaining:
            chunk = os.read(file_descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_status = os.fstat(file_descriptor)
        after_read = (
            after_status.st_dev,
            after_status.st_ino,
            after_status.st_size,
            after_status.st_mtime_ns,
            after_status.st_ctime_ns,
        )
        if before_read != after_read:
            raise _bootstrap_error("an existing secret changed while being read")
    except LocalSecretBootstrapError as error:
        failure = error
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        failure = _bootstrap_error("could not validate an existing secret safely")
    finally:
        if file_descriptor >= 0:
            failure = _close_bootstrap_descriptor(
                file_descriptor,
                failure=failure,
                resource="an existing secret",
            )

    if failure is not None:
        raise failure
    if len(payload) > _MAX_SECRET_BYTES:
        raise _bootstrap_error("an existing secret exceeds the size limit")
    if len(payload) != expected_size:
        raise _bootstrap_error("an existing secret could not be read completely")
    if b"\x00" in payload:
        raise _bootstrap_error("an existing secret contains a NUL byte")
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _bootstrap_error("an existing secret is not valid UTF-8") from None
    if value.endswith("\n"):
        value = value[:-1]
    if len(value.encode("utf-8")) < 32:
        raise _bootstrap_error("an existing secret is shorter than 32 bytes")


def _unlink_bootstrap_temporary_once(
    name: str,
    *,
    directory_descriptor: int,
    expected_status: os.stat_result | None,
    missing_ok: bool,
) -> LocalSecretBootstrapError | None:
    try:
        path_status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if expected_status is not None and not _same_identity(path_status, expected_status):
            return _bootstrap_error("temporary secret changed before cleanup")
        if not stat.S_ISREG(path_status.st_mode) or path_status.st_uid != os.geteuid():
            return _bootstrap_error("temporary secret cannot be cleaned safely")
        os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except FileNotFoundError:
        if missing_ok:
            return None
        return _bootstrap_error("could not remove a temporary secret safely")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _bootstrap_error("could not remove a temporary secret safely")
    return None


def _unlink_bootstrap_temporary(
    name: str,
    *,
    directory_descriptor: int,
    expected_status: os.stat_result | None,
) -> LocalSecretBootstrapError | None:
    try:
        return _unlink_bootstrap_temporary_once(
            name,
            directory_descriptor=directory_descriptor,
            expected_status=expected_status,
            missing_ok=False,
        )
    except BaseException as interrupted:
        cleanup_failure = _unlink_bootstrap_temporary_once(
            name,
            directory_descriptor=directory_descriptor,
            expected_status=expected_status,
            missing_ok=True,
        )
        if cleanup_failure is not None:
            raise cleanup_failure from interrupted
        raise interrupted.with_traceback(interrupted.__traceback__) from None


def _write_local_secret(
    name: str,
    *,
    directory_descriptor: int,
    file_flags: int,
    existing_file_flags: int,
) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _bootstrap_error("could not inspect an existing secret safely") from None
    else:
        _validate_existing_local_secret(
            name,
            directory_descriptor=directory_descriptor,
            existing_file_flags=existing_file_flags,
        )
        return False

    temporary_name = ""
    file_descriptor = -1
    created_status: os.stat_result | None = None
    for _ in range(8):
        temporary_name = f"{_LOCAL_BOOTSTRAP_TEMPORARY_PREFIX}{secrets_module.token_hex(16)}"
        try:
            file_descriptor = os.open(
                temporary_name,
                file_flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            break
        except FileExistsError:
            continue
        except (OSError, RuntimeError, TypeError, ValueError):
            raise _bootstrap_error("could not create a temporary secret safely") from None
    if file_descriptor < 0:
        raise _bootstrap_error("could not reserve a temporary secret safely")

    failure: LocalSecretBootstrapError | None = None
    interrupted: BaseException | None = None
    try:
        try:
            created_status = os.fstat(file_descriptor)
            payload = f"{secrets_module.token_urlsafe(_LOCAL_SECRET_TOKEN_BYTES)}\n".encode("ascii")
            os.fchmod(file_descriptor, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(file_descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("secret write made no progress")
                offset += written
            os.fsync(file_descriptor)
            final_status = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(final_status.st_mode)
                or final_status.st_uid != os.geteuid()
                or stat.S_IMODE(final_status.st_mode) != 0o600
                or final_status.st_size != len(payload)
            ):
                raise OSError("created secret metadata is invalid")
        except (OSError, RuntimeError, TypeError, ValueError):
            failure = _bootstrap_error("could not create a secret safely")
        finally:
            failure = _close_bootstrap_descriptor(
                file_descriptor,
                failure=failure,
                resource="a new secret",
            )
    except BaseException as error:
        interrupted = error

    if failure is not None or interrupted is not None:
        cleanup_failure = _unlink_bootstrap_temporary(
            temporary_name,
            directory_descriptor=directory_descriptor,
            expected_status=created_status,
        )
        if cleanup_failure is not None:
            raise cleanup_failure from interrupted
        if interrupted is not None:
            raise interrupted.with_traceback(interrupted.__traceback__)
        if failure is not None:
            raise failure

    if created_status is None:
        raise _bootstrap_error("temporary secret identity was not recorded")
    published = False
    interrupted = None
    try:
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        published = True
    except FileExistsError:
        pass
    except (OSError, RuntimeError, TypeError, ValueError):
        failure = _bootstrap_error("could not publish a secret safely")
    except BaseException as error:
        interrupted = error

    cleanup_failure = _unlink_bootstrap_temporary(
        temporary_name,
        directory_descriptor=directory_descriptor,
        expected_status=created_status,
    )
    if cleanup_failure is not None:
        raise cleanup_failure from interrupted
    if interrupted is not None:
        raise interrupted.with_traceback(interrupted.__traceback__)
    if failure is not None:
        raise failure

    if not published:
        _validate_existing_local_secret(
            name,
            directory_descriptor=directory_descriptor,
            existing_file_flags=existing_file_flags,
        )
        return False

    try:
        published_status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if not _same_identity(published_status, created_status):
            raise _bootstrap_error("published secret identity is invalid")
    except LocalSecretBootstrapError:
        raise
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        raise _bootstrap_error("could not verify a published secret safely") from None
    return True


def _bootstrap_local_secrets_locked(application_root: Path) -> LocalSecretBootstrapResult:
    directory_flags, file_flags, existing_file_flags = _bootstrap_open_flags()
    root_descriptor = -1
    secret_directory_descriptor = -1
    created: list[str] = []
    skipped: list[str] = []
    failure: LocalSecretBootstrapError | None = None
    try:
        try:
            root_descriptor = _open_bootstrap_root(
                application_root,
                directory_flags=directory_flags,
            )
            _lock_bootstrap_root(root_descriptor)
            secret_directory_descriptor = _open_local_secret_directory(
                root_descriptor,
                directory_flags=directory_flags,
            )
            _reject_stale_bootstrap_temporaries(secret_directory_descriptor)
            for name in LOCAL_BOOTSTRAP_FILENAMES:
                was_created = _write_local_secret(
                    name,
                    directory_descriptor=secret_directory_descriptor,
                    file_flags=file_flags,
                    existing_file_flags=existing_file_flags,
                )
                relative_name = f"{_LOCAL_SECRET_DIRECTORY}/{name}"
                (created if was_created else skipped).append(relative_name)
            os.fsync(secret_directory_descriptor)
        except LocalSecretBootstrapError as error:
            failure = error
        except (OSError, RuntimeError, TypeError, ValueError):
            failure = _bootstrap_error("failed safely")
    finally:
        for descriptor, resource in (
            (secret_directory_descriptor, "the secret directory"),
            (root_descriptor, "the application root"),
        ):
            if descriptor >= 0:
                failure = _close_bootstrap_descriptor(
                    descriptor,
                    failure=failure,
                    resource=resource,
                )

    if failure is not None:
        raise failure
    return LocalSecretBootstrapResult(created=tuple(created), skipped=tuple(skipped))


def bootstrap_local_secrets(application_root: Path) -> LocalSecretBootstrapResult:
    """Create fixed local infrastructure secrets without reading provider credentials.

    ``application_root`` is a trusted local CLI boundary. Every component is nevertheless opened
    relative to pinned descriptors without following symbolic links. Existing valid files are
    preserved byte-for-byte; no existing path is overwritten or repaired. An in-process mutex and
    an advisory lock on the pinned application root serialize cooperating local callers.
    """

    with _LOCAL_BOOTSTRAP_LOCK:
        return _bootstrap_local_secrets_locked(application_root)
