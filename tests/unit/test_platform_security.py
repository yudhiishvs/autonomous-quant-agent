"""Security tests for owner-private platform secret files."""

from __future__ import annotations

import copy
import json
import logging
import os
import pickle
import stat
import traceback
from pathlib import Path, PosixPath, PurePosixPath

import pytest
from pydantic import BaseModel, ValidationError

import adaptive_trader.platform.security as platform_security
from adaptive_trader.platform import (
    CanonicalizationError,
    RedactedSecret,
    SecretFileError,
    SecretFileVariable,
    canonical_json_bytes,
    load_secret_file,
)
from adaptive_trader.platform.security import SecretFileReference

SENTINEL = "TEST_AQA_DATA_SECRET_DO_NOT_LEAK"
SOURCE = SecretFileVariable.ALPACA_DATA_SECRET_KEY


class _SecretContainer(BaseModel):
    value: RedactedSecret


class _SecretReferenceContainer(BaseModel):
    value: SecretFileReference


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


def test_secret_types_publish_only_redacted_output_json_schemas() -> None:
    secret_input_schema = _SecretContainer.model_json_schema(mode="validation")
    reference_input_schema = _SecretReferenceContainer.model_json_schema(mode="validation")
    secret_output_schema = _SecretContainer.model_json_schema(mode="serialization")
    reference_output_schema = _SecretReferenceContainer.model_json_schema(mode="serialization")
    secret_schema = json.dumps((secret_input_schema, secret_output_schema), sort_keys=True)
    reference_schema = json.dumps((reference_input_schema, reference_output_schema), sort_keys=True)

    assert SENTINEL not in secret_schema
    assert SENTINEL not in reference_schema
    assert "/run/secrets" not in reference_schema
    assert '"const": "<redacted>"' in secret_schema
    assert '"readOnly": true' in secret_schema
    assert '"readOnly": true' in reference_schema
    assert secret_input_schema["properties"]["value"]["not"] == {}
    assert reference_input_schema["properties"]["value"]["not"] == {}

    with pytest.raises(ValidationError):
        _SecretContainer.model_validate_json('{"value":"<redacted>"}')
    with pytest.raises(ValidationError):
        _SecretReferenceContainer.model_validate_json(
            '{"value":{"source":"AQA_DATABASE_URL_FILE","configured":true}}'
        )


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


def test_secret_loader_rejects_same_size_rewrite_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_secret(tmp_path / "secret", b"aaaaaaaa")
    original_status = path.stat()
    real_fstat = os.fstat
    regular_file_calls = 0

    def rewrite_before_final_file_status(file_descriptor: int) -> os.stat_result:
        nonlocal regular_file_calls
        current = real_fstat(file_descriptor)
        if stat.S_ISREG(current.st_mode):
            regular_file_calls += 1
            if regular_file_calls == 2:
                path.write_bytes(b"bbbbbbbb")
                path.chmod(0o600)
                os.utime(
                    path,
                    ns=(original_status.st_atime_ns, original_status.st_mtime_ns),
                )
                return real_fstat(file_descriptor)
        return current

    monkeypatch.setattr(os, "fstat", rewrite_before_final_file_status)

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


@pytest.mark.parametrize(
    ("supplied_path", "expected_path"),
    [
        ("secrets/key", Path("/trusted/application/secrets/key")),
        (Path("secrets/key"), Path("/trusted/application/secrets/key")),
        ("/mounted/secrets/key", Path("/mounted/secrets/key")),
        (Path("/mounted/secrets/key"), Path("/mounted/secrets/key")),
    ],
)
def test_secret_file_reference_anchors_relative_paths_and_loads_only_on_demand(
    supplied_path: str | Path,
    expected_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_marker = object()
    calls: list[tuple[str | Path, SecretFileVariable]] = []

    def fake_load_secret_file(
        path: str | Path,
        *,
        source: SecretFileVariable,
    ) -> object:
        calls.append((path, source))
        return loaded_marker

    monkeypatch.setattr(platform_security, "load_secret_file", fake_load_secret_file)

    reference = SecretFileReference.from_path(
        supplied_path,
        source=SOURCE,
        application_root=Path("/trusted/application"),
    )

    assert calls == []
    assert reference.source is SOURCE
    assert reference.configured is True
    assert reference.load() is loaded_marker
    assert calls == [(expected_path, SOURCE)]


def test_secret_file_reference_factory_is_purely_lexical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def filesystem_access_is_forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("reference construction attempted filesystem access")

    with monkeypatch.context() as lexical_guard:
        lexical_guard.setattr(Path, "resolve", filesystem_access_is_forbidden)
        lexical_guard.setattr(Path, "stat", filesystem_access_is_forbidden)
        lexical_guard.setattr(Path, "read_bytes", filesystem_access_is_forbidden)
        lexical_guard.setattr(platform_security.os, "stat", filesystem_access_is_forbidden)
        lexical_guard.setattr(platform_security.os, "open", filesystem_access_is_forbidden)
        lexical_guard.setattr(
            platform_security,
            "load_secret_file",
            filesystem_access_is_forbidden,
        )

        reference = SecretFileReference.from_path(
            "uncreated/secret",
            source=SOURCE,
            application_root=Path("/uncreated/application"),
        )

    assert reference.configured is True


def test_secret_file_reference_is_independent_of_later_working_directory_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_marker = object()
    selected_path: Path | None = None

    def capture_path(path: str | Path, *, source: SecretFileVariable) -> object:
        nonlocal selected_path
        assert source is SOURCE
        selected_path = Path(path)
        return loaded_marker

    monkeypatch.setattr(platform_security, "load_secret_file", capture_path)
    reference = SecretFileReference.from_path(
        "secrets/key",
        source=SOURCE,
        application_root=Path("/trusted/application"),
    )

    monkeypatch.chdir(tmp_path)

    assert reference.load() is loaded_marker
    assert selected_path == Path("/trusted/application/secrets/key")


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "./secret",
        "directory/../secret",
        "directory//secret",
        "secret/",
        "/",
        "//secret",
        "/directory//secret",
        "/secret/",
        "a\\b",
        f"{SENTINEL}\x00after",
        "\ud800",
        "a" * 4097,
    ],
)
def test_secret_file_reference_rejects_noncanonical_or_unbounded_paths(path: str) -> None:
    with pytest.raises(SecretFileError) as captured:
        SecretFileReference.from_path(
            path,
            source=SOURCE,
            application_root=Path("/trusted/application"),
        )

    rendered = "\n".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.args),
            "".join(traceback.format_exception(captured.value)),
        )
    )
    assert SENTINEL not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "application_root",
    [
        Path("relative"),
        Path("."),
        Path("/trusted/../escape"),
        Path("//trusted"),
    ],
)
def test_secret_file_reference_requires_an_absolute_canonical_application_root(
    application_root: Path,
) -> None:
    with pytest.raises(SecretFileError):
        SecretFileReference.from_path(
            "secret",
            source=SOURCE,
            application_root=application_root,
        )


def test_secret_file_reference_requires_an_exact_concrete_application_root() -> None:
    for application_root in ("/trusted", PurePosixPath("/trusted")):
        with pytest.raises(SecretFileError, match=r"exact pathlib\.Path"):
            SecretFileReference.from_path(
                "secret",
                source=SOURCE,
                application_root=application_root,  # type: ignore[arg-type]
            )


def test_secret_file_reference_accepts_the_root_and_enforces_anchored_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_marker = object()
    selected_path: Path | None = None

    def capture_path(path: str | Path, *, source: SecretFileVariable) -> object:
        nonlocal selected_path
        assert source is SOURCE
        selected_path = Path(path)
        return loaded_marker

    monkeypatch.setattr(platform_security, "load_secret_file", capture_path)
    maximum_reference = SecretFileReference.from_path(
        "a" * 4095,
        source=SOURCE,
        application_root=Path("/"),
    )

    assert maximum_reference.load() is loaded_marker
    assert selected_path == Path("/" + "a" * 4095)

    with pytest.raises(SecretFileError, match="bounded UTF-8"):
        SecretFileReference.from_path(
            "x",
            source=SOURCE,
            application_root=Path("/" + "a" * 4094),
        )


def test_secret_file_reference_rendering_and_pydantic_serialization_hide_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reference = SecretFileReference.from_path(
        f"secrets/{SENTINEL}",
        source=SOURCE,
        application_root=Path(f"/trusted/{SENTINEL}"),
    )
    model = _SecretReferenceContainer(value=reference)
    expected_metadata = {"source": SOURCE.value, "configured": True}

    with caplog.at_level(logging.INFO):
        logging.getLogger("platform-secret-reference-test").info(
            "reference=%s",
            reference,
        )

    rendered_values = (
        str(reference),
        repr(reference),
        repr([reference]),
        repr({"reference": reference}),
        repr(model),
        repr(model.model_dump()),
        model.model_dump_json(),
        json.dumps({"reference": reference}, default=str),
        caplog.text,
    )
    assert all(SENTINEL not in rendered for rendered in rendered_values)
    assert str(reference) == f"<secret-file-reference source={SOURCE.value} configured=true>"
    assert repr(reference) == str(reference)
    assert model.model_dump() == {"value": expected_metadata}
    assert json.loads(model.model_dump_json()) == {"value": expected_metadata}


def test_secret_file_reference_rejects_direct_construction_mutation_and_pickling() -> None:
    reference = SecretFileReference.from_path(
        f"secrets/{SENTINEL}",
        source=SOURCE,
        application_root=Path("/trusted/application"),
    )

    with pytest.raises(TypeError, match="created with from_path"):
        SecretFileReference(Path(f"/{SENTINEL}"), source=SOURCE, _token=object())
    with pytest.raises(TypeError):
        vars(reference)
    with pytest.raises(AttributeError, match="immutable"):
        reference.source = SOURCE  # type: ignore[misc]
    with pytest.raises(AttributeError, match="immutable"):
        del reference.source
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(reference)

    assert copy.copy(reference) is reference
    assert copy.deepcopy(reference) is reference


def test_secret_file_reference_rejects_hostile_inputs_without_disclosure() -> None:
    class HostileText(str):
        def __str__(self) -> str:
            raise RuntimeError(SENTINEL)

        def __repr__(self) -> str:
            raise RuntimeError(SENTINEL)

    class HostilePath(PosixPath):
        def __fspath__(self) -> str:
            raise RuntimeError(SENTINEL)

    hostile_inputs: tuple[tuple[object, object, object], ...] = (
        (HostileText(f"/{SENTINEL}"), SOURCE, Path("/trusted")),
        (HostilePath(f"/{SENTINEL}"), SOURCE, Path("/trusted")),
        ("secret", SOURCE, HostilePath(f"/{SENTINEL}")),
        ("secret", HostileText(SENTINEL), Path("/trusted")),
    )

    for path, source, application_root in hostile_inputs:
        with pytest.raises((SecretFileError, TypeError)) as captured:
            SecretFileReference.from_path(
                path,  # type: ignore[arg-type]
                source=source,  # type: ignore[arg-type]
                application_root=application_root,  # type: ignore[arg-type]
            )

        rendered = "\n".join(
            (
                str(captured.value),
                repr(captured.value),
                repr(captured.value.args),
                "".join(traceback.format_exception(captured.value)),
            )
        )
        assert SENTINEL not in rendered
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_secret_file_reference_rejects_untrusted_pydantic_input_without_disclosure() -> None:
    hostile_input = {"source": SOURCE.value, "configured": True, "path": SENTINEL}

    with pytest.raises(ValidationError) as captured:
        _SecretReferenceContainer.model_validate({"value": hostile_input})
    with pytest.raises(ValidationError) as captured_json:
        _SecretReferenceContainer.model_validate_json(json.dumps({"value": hostile_input}))

    rendered_errors = (
        str(captured.value),
        repr(captured.value),
        repr(captured.value.errors()),
        captured.value.json(),
        str(captured_json.value),
        repr(captured_json.value),
        repr(captured_json.value.errors()),
        captured_json.value.json(),
    )
    assert all(SENTINEL not in rendered for rendered in rendered_errors)

    bypassed_model = _SecretReferenceContainer.model_construct(value=hostile_input)
    assert bypassed_model.model_dump() == {"value": {"source": "unsupported", "configured": False}}
    assert SENTINEL not in bypassed_model.model_dump_json()
