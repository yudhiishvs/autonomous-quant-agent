"""Security tests for owner-private platform secret files."""

from __future__ import annotations

import json
import logging
import os
import pickle
import stat
import traceback
from pathlib import Path, PosixPath

import pytest
from pydantic import BaseModel, ValidationError

from adaptive_trader.platform import (
    CanonicalizationError,
    RedactedSecret,
    SecretFileError,
    SecretFileVariable,
    canonical_json_bytes,
    load_secret_file,
)

SENTINEL = "TEST_AQA_DATA_SECRET_DO_NOT_LEAK"
SOURCE = SecretFileVariable.ALPACA_DATA_SECRET_KEY


class _SecretContainer(BaseModel):
    value: RedactedSecret


def _write_secret(path: Path, payload: bytes, *, mode: int = 0o600) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def test_secret_file_variable_inventory_is_exact() -> None:
    assert {variable.value for variable in SecretFileVariable} == {
        "AQA_DATABASE_URL_FILE",
        "AQA_OPERATOR_TOKEN_FILE",
        "AQA_ALPACA_DATA_API_KEY_FILE",
        "AQA_ALPACA_DATA_SECRET_KEY_FILE",
        "AQA_ALPACA_PAPER_API_KEY_FILE",
        "AQA_ALPACA_PAPER_SECRET_KEY_FILE",
        "AQA_PAPER_ACCOUNT_ID_HASH_FILE",
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"value", "value"),
        (b"value\n", "value"),
        (b"value\n\n", "value\n"),
        (b" value \n", " value "),
        (b"value\r\n", "value\r"),
    ],
)
def test_secret_loader_removes_exactly_one_terminal_lf(
    tmp_path: Path,
    payload: bytes,
    expected: str,
) -> None:
    path = _write_secret(tmp_path / "secret", payload)

    secret = load_secret_file(path, source=SOURCE)

    assert secret.reveal() == expected
    assert path.read_bytes() == payload


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_secret_loader_accepts_owner_private_readable_modes(tmp_path: Path, mode: int) -> None:
    path = _write_secret(tmp_path / "secret", b"value", mode=mode)

    assert load_secret_file(path, source=SOURCE).reveal() == "value"


@pytest.mark.parametrize("use_absolute_path", [False, True])
def test_secret_loader_anchors_paths_to_a_pinned_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_absolute_path: bool,
) -> None:
    absolute_path = _write_secret(tmp_path / "secret", b"value")
    monkeypatch.chdir(tmp_path)
    supplied_path = absolute_path if use_absolute_path else Path("secret")

    assert load_secret_file(supplied_path, source=SOURCE).reveal() == "value"


@pytest.mark.parametrize(
    "mode",
    [0o000, 0o200, 0o700, 0o640, 0o604, 0o666, 0o1400],
)
def test_secret_loader_rejects_unreadable_executable_or_shared_modes(
    tmp_path: Path,
    mode: int,
) -> None:
    path = _write_secret(tmp_path / "secret", b"value", mode=mode)

    with pytest.raises(SecretFileError, match=r"owner-private|safely"):
        load_secret_file(path, source=SOURCE)


def test_secret_loader_rejects_special_permission_bits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_secret(tmp_path / "secret", b"value")
    real_fstat = os.fstat

    def special_mode_fstat(file_descriptor: int) -> os.stat_result:
        current = real_fstat(file_descriptor)
        if not stat.S_ISREG(current.st_mode):
            return current
        values = list(current)
        values[stat.ST_MODE] = stat.S_IFREG | 0o4600
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", special_mode_fstat)

    with pytest.raises(SecretFileError, match="owner-private"):
        load_secret_file(path, source=SOURCE)


def test_secret_loader_rejects_file_owned_by_another_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_secret(tmp_path / "secret", b"value")
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)

    with pytest.raises(SecretFileError, match="current user"):
        load_secret_file(path, source=SOURCE)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "empty"),
        (b"\n", "empty"),
        (b"before\x00after", "NUL"),
        (b"\xff", "UTF-8"),
        (b"x" * (16 * 1024 + 1), "size limit"),
    ],
)
def test_secret_loader_rejects_invalid_content(
    tmp_path: Path,
    payload: bytes,
    reason: str,
) -> None:
    path = _write_secret(tmp_path / "secret", payload)

    with pytest.raises(SecretFileError, match=reason):
        load_secret_file(path, source=SOURCE)


def test_secret_loader_rejects_missing_directory_and_fifo(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(SecretFileError, match="missing"):
        load_secret_file(missing, source=SOURCE)

    directory = tmp_path / "directory"
    directory.mkdir()
    directory.chmod(0o700)
    with pytest.raises(SecretFileError, match=r"regular file|safely"):
        load_secret_file(directory, source=SOURCE)

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(SecretFileError, match="regular file"):
        load_secret_file(fifo, source=SOURCE)


def test_secret_loader_rejects_socket_file_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_secret(tmp_path / "secret", b"value")
    real_fstat = os.fstat

    def socket_fstat(file_descriptor: int) -> os.stat_result:
        current = real_fstat(file_descriptor)
        if not stat.S_ISREG(current.st_mode):
            return current
        values = list(current)
        values[stat.ST_MODE] = stat.S_IFSOCK | 0o600
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", socket_fstat)

    with pytest.raises(SecretFileError, match="regular file"):
        load_secret_file(path, source=SOURCE)


def test_secret_loader_rejects_final_and_ancestor_symlinks(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    target = _write_secret(real_directory / "secret", b"value")

    final_link = real_directory / "final-link"
    final_link.symlink_to(target)
    with pytest.raises(SecretFileError, match="symbolic link"):
        load_secret_file(final_link, source=SOURCE)

    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(SecretFileError, match="symbolic link"):
        load_secret_file(ancestor_link / "secret", source=SOURCE)


def test_secret_loader_translates_symlink_loop_without_path_disclosure(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)

    with pytest.raises(SecretFileError, match=r"symbolic link|safely") as captured:
        load_secret_file(first, source=SOURCE)

    assert str(first) not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "path",
    ["", "./secret", "directory/../secret", "directory//secret", "secret/", "a\\b"],
)
def test_secret_loader_rejects_noncanonical_paths(path: str) -> None:
    with pytest.raises(SecretFileError):
        load_secret_file(path, source=SOURCE)


def test_secret_loader_rejects_unsupported_source_without_rendering_it(tmp_path: Path) -> None:
    path = _write_secret(tmp_path / "secret", b"value")

    with pytest.raises(SecretFileError, match="source is not supported"):
        load_secret_file(path, source="AQA_UNKNOWN_FILE")  # type: ignore[arg-type]


def test_secret_loader_rejects_hostile_path_subclasses_without_disclosure(tmp_path: Path) -> None:
    class HostilePath(PosixPath):
        def __fspath__(self) -> str:
            raise RuntimeError(SENTINEL)

    hostile_path = HostilePath(tmp_path / "secret")

    with pytest.raises(SecretFileError) as captured:
        load_secret_file(hostile_path, source=SOURCE)

    assert SENTINEL not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_loader_fails_safely_when_posix_capability_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_secret(tmp_path / f"{SENTINEL}-path", b"value")
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(SecretFileError, match="unsupported") as captured:
        load_secret_file(path, source=SOURCE)

    assert SENTINEL not in str(captured.value)
    assert str(path) not in str(captured.value)


def test_secret_loader_pins_open_parent_during_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_directory = tmp_path / "selected"
    replacement_directory = tmp_path / "replacement"
    displaced_directory = tmp_path / "displaced"
    selected_directory.mkdir()
    replacement_directory.mkdir()
    selected = _write_secret(selected_directory / "secret", b"selected")
    _write_secret(replacement_directory / "secret", b"replacement")
    real_open = os.open
    swapped = False

    def replace_ancestor_before_leaf_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "secret" and dir_fd is not None and not swapped:
            selected_directory.rename(displaced_directory)
            replacement_directory.rename(selected_directory)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_ancestor_before_leaf_open)
    monkeypatch.setattr(
        os,
        "supports_dir_fd",
        os.supports_dir_fd | {replace_ancestor_before_leaf_open},
    )

    secret = load_secret_file(selected, source=SOURCE)

    assert swapped is True
    assert secret.reveal() == "selected"


def test_secret_loader_rejects_file_metadata_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_secret(tmp_path / "secret", b"before")
    real_fstat = os.fstat
    regular_file_calls = 0

    def mutate_before_final_file_status(file_descriptor: int) -> os.stat_result:
        nonlocal regular_file_calls
        current = real_fstat(file_descriptor)
        if stat.S_ISREG(current.st_mode):
            regular_file_calls += 1
            if regular_file_calls == 2:
                path.write_bytes(b"changed-value")
                path.chmod(0o600)
                return real_fstat(file_descriptor)
        return current

    monkeypatch.setattr(os, "fstat", mutate_before_final_file_status)

    with pytest.raises(SecretFileError, match="changed while being read"):
        load_secret_file(path, source=SOURCE)


def test_redacted_secret_never_renders_or_serializes_its_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _write_secret(tmp_path / "secret", SENTINEL.encode())
    secret = load_secret_file(path, source=SOURCE)
    model = _SecretContainer(value=secret)

    with caplog.at_level(logging.INFO):
        logging.getLogger("platform-secret-test").info("secret=%s", secret)

    rendered_values = (
        str(secret),
        repr(secret),
        repr([secret]),
        repr({"secret": secret}),
        repr(model),
        repr(model.model_dump()),
        model.model_dump_json(),
        json.dumps({"secret": secret}, default=str),
        caplog.text,
    )
    assert all(SENTINEL not in rendered for rendered in rendered_values)
    assert str(secret) == "<redacted>"
    assert repr(secret) == "<redacted>"
    assert model.model_dump() == {"value": "<redacted>"}
    assert model.model_dump_json() == '{"value":"<redacted>"}'

    with pytest.raises(CanonicalizationError) as captured:
        canonical_json_bytes(secret)
    assert SENTINEL not in str(captured.value)


def test_redacted_secret_rejects_direct_construction_pydantic_input_and_persistence(
    tmp_path: Path,
) -> None:
    path = _write_secret(tmp_path / "secret", SENTINEL.encode())
    secret = load_secret_file(path, source=SOURCE)

    with pytest.raises(TypeError, match="loaded from a secret file"):
        RedactedSecret(SENTINEL, _token=object())
    with pytest.raises(ValidationError) as captured:
        _SecretContainer.model_validate({"value": SENTINEL})
    with pytest.raises(ValidationError) as captured_json:
        _SecretContainer.model_validate_json(json.dumps({"value": SENTINEL}))
    with pytest.raises(TypeError):
        vars(secret)
    with pytest.raises(AttributeError, match="immutable"):
        secret.arbitrary = SENTINEL
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(secret)

    assert SENTINEL not in str(captured.value)
    assert SENTINEL not in repr(captured.value)
    assert SENTINEL not in repr(captured.value.errors())
    assert SENTINEL not in captured.value.json()
    assert SENTINEL not in str(captured_json.value)
    assert SENTINEL not in repr(captured_json.value)
    assert SENTINEL not in repr(captured_json.value.errors())
    assert SENTINEL not in captured_json.value.json()


def test_redacted_secret_uses_identity_equality_and_hashing(tmp_path: Path) -> None:
    first_path = _write_secret(tmp_path / "first", b"same")
    second_path = _write_secret(tmp_path / "second", b"same")
    first = load_secret_file(first_path, source=SOURCE)
    second = load_secret_file(second_path, source=SOURCE)

    assert first == first
    assert first != second
    assert hash(first) == id(first)


def test_secret_file_error_never_discloses_path_content_or_exception_context(
    tmp_path: Path,
) -> None:
    path = _write_secret(tmp_path / f"{SENTINEL}-path", f"{SENTINEL}\x00".encode())

    with pytest.raises(SecretFileError) as captured:
        load_secret_file(path, source=SOURCE)

    rendered = "\n".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.args),
            "".join(traceback.format_exception(captured.value)),
        )
    )
    assert SENTINEL not in rendered
    assert str(path) not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
