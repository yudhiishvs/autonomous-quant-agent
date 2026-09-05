"""Adversarial tests for the local infrastructure-secret bootstrap."""

from __future__ import annotations

import hashlib
import json
import os
import select
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import pytest
from typer.testing import CliRunner

import adaptive_trader.platform.security as platform_security
from adaptive_trader.platform.cli import app
from adaptive_trader.platform.security import (
    LOCAL_BOOTSTRAP_FILENAMES,
    LocalSecretBootstrapError,
    bootstrap_local_secrets,
)

EXPECTED_FILENAMES = (
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
PROVIDER_SENTINEL = "TEST_AQA_DATA_SECRET_DO_NOT_LEAK"


def _application_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _secret_directory(root: Path) -> Path:
    directory = root / "secrets"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _write_existing(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.write_bytes(payload)
    path.chmod(mode)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _path_snapshot(path: Path) -> tuple[object, ...]:
    status = path.lstat()
    content_hash = ""
    link_target = ""
    if stat.S_ISREG(status.st_mode):
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    elif stat.S_ISLNK(status.st_mode):
        link_target = os.readlink(path)
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        content_hash,
        link_target,
    )


def _patch_os_capability(
    patcher: pytest.MonkeyPatch,
    name: str,
    replacement: object,
) -> None:
    original = getattr(os, name)
    for capability_name in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks"):
        capabilities = getattr(os, capability_name)
        if original in capabilities:
            updated = {replacement if item is original else item for item in capabilities}
            patcher.setattr(os, capability_name, updated)
    patcher.setattr(os, name, replacement)


def test_local_bootstrap_inventory_is_exact_and_provider_free() -> None:
    assert LOCAL_BOOTSTRAP_FILENAMES == EXPECTED_FILENAMES
    assert all("alpaca" not in name.lower() for name in LOCAL_BOOTSTRAP_FILENAMES)
    assert all("account" not in name.lower() for name in LOCAL_BOOTSTRAP_FILENAMES)
    assert all("database_url" not in name.lower() for name in LOCAL_BOOTSTRAP_FILENAMES)


def test_local_bootstrap_creates_only_owner_private_random_secrets(tmp_path: Path) -> None:
    root = _application_root(tmp_path)

    result = bootstrap_local_secrets(root)

    expected_paths = tuple(f"secrets/{name}" for name in EXPECTED_FILENAMES)
    assert result.created == expected_paths
    assert result.skipped == ()
    secret_directory = root / "secrets"
    assert _mode(secret_directory) == 0o700
    assert tuple(sorted(path.name for path in secret_directory.iterdir())) == tuple(
        sorted(EXPECTED_FILENAMES)
    )

    digests: set[str] = set()
    valid_token_bytes = frozenset(
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    for name in EXPECTED_FILENAMES:
        path = secret_directory / name
        payload = path.read_bytes()
        assert _mode(path) == 0o600
        assert payload.endswith(b"\n")
        assert len(payload) == 65
        assert set(payload[:-1]) <= valid_token_bytes
        digests.add(hashlib.sha256(payload).hexdigest())
    assert len(digests) == len(EXPECTED_FILENAMES)


def test_local_bootstrap_rerun_preserves_every_existing_file(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    bootstrap_local_secrets(root)
    secret_directory = root / "secrets"
    before = {
        name: (
            hashlib.sha256((secret_directory / name).read_bytes()).hexdigest(),
            (secret_directory / name).stat().st_ino,
            (secret_directory / name).stat().st_mtime_ns,
        )
        for name in EXPECTED_FILENAMES
    }

    result = bootstrap_local_secrets(root)

    after = {
        name: (
            hashlib.sha256((secret_directory / name).read_bytes()).hexdigest(),
            (secret_directory / name).stat().st_ino,
            (secret_directory / name).stat().st_mtime_ns,
        )
        for name in EXPECTED_FILENAMES
    }
    assert result.created == ()
    assert result.skipped == tuple(f"secrets/{name}" for name in EXPECTED_FILENAMES)
    assert after == before


def test_local_bootstrap_rerun_does_not_request_new_entropy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    bootstrap_local_secrets(root)

    def reject_entropy(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("a no-op rerun must not request entropy")

    monkeypatch.setattr(platform_security.secrets_module, "token_hex", reject_entropy)
    monkeypatch.setattr(platform_security.secrets_module, "token_urlsafe", reject_entropy)

    result = bootstrap_local_secrets(root)
    assert result.created == ()
    assert len(result.skipped) == len(EXPECTED_FILENAMES)


def test_local_bootstrap_uses_required_entropy_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _application_root(tmp_path)
    real_token_urlsafe = platform_security.secrets_module.token_urlsafe
    requested_sizes: list[int | None] = []

    def capture_size(size: int | None = None) -> str:
        requested_sizes.append(size)
        return real_token_urlsafe(size)

    monkeypatch.setattr(platform_security.secrets_module, "token_urlsafe", capture_size)

    bootstrap_local_secrets(root)
    assert requested_sizes == [48] * len(EXPECTED_FILENAMES)


def test_local_bootstrap_enforces_modes_under_hostile_umask(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    previous_umask = os.umask(0o777)
    try:
        bootstrap_local_secrets(root)
        observed_umask = os.umask(0o777)
        assert observed_umask == 0o777
    finally:
        os.umask(previous_umask)

    assert _mode(root / "secrets") == 0o700
    assert all(_mode(root / "secrets" / name) == 0o600 for name in EXPECTED_FILENAMES)


def test_local_bootstrap_preserves_valid_partial_state(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    directory = _secret_directory(root)
    existing = directory / "postgres_password"
    _write_existing(existing, b"existing-database-password-with-safe-length\n")
    before = (existing.read_bytes(), existing.stat().st_ino, existing.stat().st_mtime_ns)

    result = bootstrap_local_secrets(root)

    after = (existing.read_bytes(), existing.stat().st_ino, existing.stat().st_mtime_ns)
    assert after == before
    assert result.skipped == ("secrets/postgres_password",)
    assert result.created == tuple(f"secrets/{name}" for name in EXPECTED_FILENAMES[1:])


@pytest.mark.parametrize(
    "invalid_kind",
    ["directory", "fifo", "symlink", "shared-mode", "empty", "nul", "invalid-utf8", "oversize"],
)
def test_local_bootstrap_rejects_invalid_existing_paths_without_disclosure(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    root = _application_root(tmp_path)
    directory = _secret_directory(root)
    candidate = directory / "postgres_password"
    if invalid_kind == "directory":
        candidate.mkdir(mode=0o700)
    elif invalid_kind == "fifo":
        os.mkfifo(candidate, mode=0o600)
    elif invalid_kind == "symlink":
        target = tmp_path / PROVIDER_SENTINEL
        _write_existing(target, b"external-value\n")
        candidate.symlink_to(target)
    elif invalid_kind == "shared-mode":
        _write_existing(candidate, b"existing-value\n", mode=0o644)
    elif invalid_kind == "empty":
        _write_existing(candidate, b"")
    elif invalid_kind == "nul":
        _write_existing(candidate, f"before-{PROVIDER_SENTINEL}\x00-after".encode())
    elif invalid_kind == "invalid-utf8":
        _write_existing(candidate, b"\xff")
    else:
        _write_existing(candidate, b"x" * (16 * 1024 + 1))
    before = _path_snapshot(candidate)

    with pytest.raises(LocalSecretBootstrapError) as captured:
        bootstrap_local_secrets(root)

    assert _path_snapshot(candidate) == before
    rendered = str(captured.value)
    assert PROVIDER_SENTINEL not in rendered
    assert str(root) not in rendered
    assert "postgres_password" not in rendered


def test_local_bootstrap_rejects_short_existing_operator_token(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    directory = _secret_directory(root)
    candidate = directory / "operator_token"
    _write_existing(candidate, b"too-short\n")
    before = _path_snapshot(candidate)

    with pytest.raises(LocalSecretBootstrapError, match="shorter than 32 bytes"):
        bootstrap_local_secrets(root)
    assert _path_snapshot(candidate) == before


def test_local_bootstrap_rejects_symlinked_or_insecure_directories(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (root / "secrets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalSecretBootstrapError, match="symbolic link"):
        bootstrap_local_secrets(root)

    insecure_root = tmp_path / "insecure"
    insecure_root.mkdir(mode=0o777)
    insecure_root.chmod(0o777)
    with pytest.raises(LocalSecretBootstrapError, match="writable by another user"):
        bootstrap_local_secrets(insecure_root)


@pytest.mark.parametrize("collision", ["file", "shared-directory"])
def test_local_bootstrap_rejects_secret_directory_collisions(
    tmp_path: Path,
    collision: str,
) -> None:
    root = _application_root(tmp_path)
    secret_path = root / "secrets"
    if collision == "file":
        _write_existing(secret_path, b"not-a-directory" * 3)
    else:
        secret_path.mkdir(mode=0o755)
        secret_path.chmod(0o755)

    with pytest.raises(LocalSecretBootstrapError):
        bootstrap_local_secrets(root)


def test_local_bootstrap_rejects_symlinked_application_root(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    link = tmp_path / "project-link"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(LocalSecretBootstrapError, match="symbolic link"):
        bootstrap_local_secrets(link)


def test_local_bootstrap_rejects_untrusted_root_representations(tmp_path: Path) -> None:
    root = _application_root(tmp_path)

    class DerivedPath(type(Path())):
        pass

    invalid_roots = (
        Path("relative"),
        Path("/"),
        PurePosixPath(root),
        DerivedPath(root),
    )
    for invalid_root in invalid_roots:
        with pytest.raises(LocalSecretBootstrapError):
            bootstrap_local_secrets(invalid_root)  # type: ignore[arg-type]


def test_local_bootstrap_removes_only_its_partial_file_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    real_write = os.write
    write_calls = 0

    def fail_during_second_secret(file_descriptor: int, payload: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            return real_write(file_descriptor, payload[:5])
        if write_calls == 3:
            raise OSError("simulated write failure")
        return real_write(file_descriptor, payload)

    with monkeypatch.context() as write_guard:
        write_guard.setattr(platform_security.os, "write", fail_during_second_secret)
        with pytest.raises(LocalSecretBootstrapError, match="could not create"):
            bootstrap_local_secrets(root)

    directory = root / "secrets"
    assert (directory / "postgres_password").is_file()
    assert not (directory / "aqa_migrate_password").exists()

    result = bootstrap_local_secrets(root)
    assert result.skipped == ("secrets/postgres_password",)
    assert result.created == tuple(f"secrets/{name}" for name in EXPECTED_FILENAMES[1:])


@pytest.mark.parametrize(
    "interrupt_point",
    [
        "generation",
        "write",
        "publication-before-effect",
        "publication-after-effect",
        "cleanup-before-effect",
        "cleanup-after-effect",
    ],
)
def test_local_bootstrap_cleans_owned_temporary_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_point: str,
) -> None:
    root = _application_root(tmp_path)
    real_link = os.link
    real_unlink = os.unlink

    def interrupt_generation(size: int | None = None) -> str:
        del size
        raise KeyboardInterrupt

    def interrupt_write(file_descriptor: int, payload: bytes) -> int:
        del file_descriptor, payload
        raise KeyboardInterrupt

    def interrupt_publication(*args: object, **kwargs: object) -> None:
        if interrupt_point == "publication-after-effect":
            real_link(*args, **kwargs)
        raise KeyboardInterrupt

    unlink_calls = 0

    def interrupt_cleanup(*args: object, **kwargs: object) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 1:
            if interrupt_point == "cleanup-after-effect":
                real_unlink(*args, **kwargs)
            raise KeyboardInterrupt
        real_unlink(*args, **kwargs)

    with monkeypatch.context() as interrupt_guard:
        if interrupt_point == "generation":
            interrupt_guard.setattr(
                platform_security.secrets_module,
                "token_urlsafe",
                interrupt_generation,
            )
        elif interrupt_point == "write":
            interrupt_guard.setattr(platform_security.os, "write", interrupt_write)
        elif interrupt_point.startswith("publication"):
            _patch_os_capability(interrupt_guard, "link", interrupt_publication)
        else:
            _patch_os_capability(interrupt_guard, "unlink", interrupt_cleanup)

        with pytest.raises(KeyboardInterrupt):
            bootstrap_local_secrets(root)

    directory = root / "secrets"
    assert not any(path.name.startswith(".bootstrap-") for path in directory.iterdir())
    result = bootstrap_local_secrets(root)
    expected_skipped = (
        ("secrets/postgres_password",)
        if interrupt_point
        in {"publication-after-effect", "cleanup-before-effect", "cleanup-after-effect"}
        else ()
    )
    assert result.skipped == expected_skipped
    assert tuple(sorted(path.name for path in directory.iterdir())) == tuple(
        sorted(EXPECTED_FILENAMES)
    )


def test_local_bootstrap_removes_unpublished_file_after_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)

    with monkeypatch.context() as metadata_guard:
        metadata_guard.setattr(
            platform_security.os,
            "fchmod",
            lambda file_descriptor, mode: (_ for _ in ()).throw(OSError("simulated failure")),
        )
        with pytest.raises(LocalSecretBootstrapError, match="could not create"):
            bootstrap_local_secrets(root)

    assert tuple((root / "secrets").iterdir()) == ()
    assert len(bootstrap_local_secrets(root).created) == len(EXPECTED_FILENAMES)


def test_local_bootstrap_removes_temporary_file_when_initial_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    real_open = os.open
    real_fstat = os.fstat
    temporary_descriptor = -1

    def capture_temporary_descriptor(path: object, *args: object, **kwargs: object) -> int:
        nonlocal temporary_descriptor
        descriptor = real_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".bootstrap-"):
            temporary_descriptor = descriptor
        return descriptor

    def fail_temporary_fstat(file_descriptor: int) -> os.stat_result:
        if file_descriptor == temporary_descriptor:
            raise OSError("simulated fstat failure")
        return real_fstat(file_descriptor)

    with monkeypatch.context() as metadata_guard:
        _patch_os_capability(metadata_guard, "open", capture_temporary_descriptor)
        metadata_guard.setattr(platform_security.os, "fstat", fail_temporary_fstat)
        with pytest.raises(LocalSecretBootstrapError, match="could not create"):
            bootstrap_local_secrets(root)

    assert tuple((root / "secrets").iterdir()) == ()
    assert len(bootstrap_local_secrets(root).created) == len(EXPECTED_FILENAMES)


def test_local_bootstrap_rejects_an_incomplete_existing_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    directory = _secret_directory(root)
    _write_existing(directory / "postgres_password", b"x" * 40)
    real_read = os.read
    calls = 0

    def return_early_eof(file_descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_read(file_descriptor, min(size, 5))
        return b""

    monkeypatch.setattr(platform_security.os, "read", return_early_eof)

    with pytest.raises(LocalSecretBootstrapError, match="completely"):
        bootstrap_local_secrets(root)


@pytest.mark.parametrize("linked_to_final", [False, True])
def test_local_bootstrap_rejects_stale_temporary_secret_material(
    tmp_path: Path,
    linked_to_final: bool,
) -> None:
    root = _application_root(tmp_path)
    directory = _secret_directory(root)
    temporary = directory / ".bootstrap-stale"
    if linked_to_final:
        final = directory / "postgres_password"
        _write_existing(final, b"x" * 40)
        os.link(final, temporary)
    else:
        _write_existing(temporary, b"unpublished-secret-material" * 2)

    with pytest.raises(LocalSecretBootstrapError, match="unresolved temporary"):
        bootstrap_local_secrets(root)


def test_local_bootstrap_cleans_temporary_file_after_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)

    with monkeypatch.context() as link_guard:
        _patch_os_capability(
            link_guard,
            "link",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated link failure")),
        )
        with pytest.raises(LocalSecretBootstrapError, match="publish"):
            bootstrap_local_secrets(root)

    assert tuple((root / "secrets").iterdir()) == ()
    assert len(bootstrap_local_secrets(root).created) == len(EXPECTED_FILENAMES)


def test_local_bootstrap_fails_closed_when_temporary_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)

    with monkeypatch.context() as unlink_guard:
        _patch_os_capability(
            unlink_guard,
            "unlink",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated unlink failure")),
        )
        with pytest.raises(LocalSecretBootstrapError, match="remove"):
            bootstrap_local_secrets(root)

    entries = tuple(path.name for path in (root / "secrets").iterdir())
    assert "postgres_password" in entries
    assert any(name.startswith(".bootstrap-") for name in entries)
    with pytest.raises(LocalSecretBootstrapError, match="unresolved temporary"):
        bootstrap_local_secrets(root)


def test_local_bootstrap_cleans_unpublished_file_after_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_first_file_fsync(file_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("simulated fsync failure")
        real_fsync(file_descriptor)

    with monkeypatch.context() as fsync_guard:
        fsync_guard.setattr(platform_security.os, "fsync", fail_first_file_fsync)
        with pytest.raises(LocalSecretBootstrapError, match="could not create"):
            bootstrap_local_secrets(root)

    assert tuple((root / "secrets").iterdir()) == ()
    assert len(bootstrap_local_secrets(root).created) == len(EXPECTED_FILENAMES)


def test_concurrent_local_bootstraps_publish_each_secret_once(tmp_path: Path) -> None:
    root = _application_root(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(bootstrap_local_secrets, (root, root)))

    all_created = [path for result in results for path in result.created]
    all_skipped = [path for result in results for path in result.skipped]
    assert sorted(all_created) == sorted(f"secrets/{name}" for name in EXPECTED_FILENAMES)
    assert sorted(all_skipped) == sorted(f"secrets/{name}" for name in EXPECTED_FILENAMES)
    assert tuple(sorted(path.name for path in (root / "secrets").iterdir())) == tuple(
        sorted(EXPECTED_FILENAMES)
    )


def test_concurrent_processes_serialize_local_bootstrap(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    read_descriptor, write_descriptor = os.pipe()
    child_code = (
        "import os,sys; from pathlib import Path; "
        "from adaptive_trader.platform.security import bootstrap_local_secrets; "
        "os.read(int(sys.argv[2]), 1); bootstrap_local_secrets(Path(sys.argv[1]))"
    )
    processes: list[subprocess.Popen[str]] = []
    try:
        for _ in range(4):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", child_code, str(root), str(read_descriptor)],
                    pass_fds=(read_descriptor,),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        os.close(read_descriptor)
        read_descriptor = -1
        os.write(write_descriptor, b"x" * len(processes))
        os.close(write_descriptor)
        write_descriptor = -1
        results = [process.communicate(timeout=20) for process in processes]
    finally:
        for descriptor in (read_descriptor, write_descriptor):
            if descriptor >= 0:
                os.close(descriptor)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert all(process.returncode == 0 for process in processes)
    assert all(stdout == "" and stderr == "" for stdout, stderr in results)
    assert tuple(sorted(path.name for path in (root / "secrets").iterdir())) == tuple(
        sorted(EXPECTED_FILENAMES)
    )


def test_process_lock_is_held_while_secret_is_generated(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child_code = """
import json
import os
from pathlib import Path
import sys

from adaptive_trader.platform import security

root = Path(sys.argv[1])
ready = int(sys.argv[2])
release = int(sys.argv[3])
token_urlsafe = security.secrets_module.token_urlsafe
paused = False

def pause_with_lock(size: int | None = None) -> str:
    global paused
    if not paused:
        paused = True
        os.write(ready, b"x")
        if os.read(release, 1) != b"x":
            raise RuntimeError("release signal missing")
    return token_urlsafe(size)

security.secrets_module.token_urlsafe = pause_with_lock
result = security.bootstrap_local_secrets(root)
print(json.dumps({"created": result.created, "skipped": result.skipped}))
"""
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(root),
                str(ready_write),
                str(release_read),
            ],
            pass_fds=(ready_write, release_read),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(ready_write)
        ready_write = -1
        os.close(release_read)
        release_read = -1

        readable, _, _ = select.select((ready_read,), (), (), 10)
        assert readable == [ready_read]
        assert os.read(ready_read, 1) == b"x"

        probe_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            with pytest.raises(BlockingIOError):
                platform_security.fcntl.flock(
                    probe_descriptor,
                    platform_security.fcntl.LOCK_EX | platform_security.fcntl.LOCK_NB,
                )
        finally:
            os.close(probe_descriptor)

        os.write(release_write, b"x")
        os.close(release_write)
        release_write = -1
        stdout, stderr = process.communicate(timeout=10)
    finally:
        for descriptor in (ready_read, ready_write, release_read, release_write):
            if descriptor >= 0:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process is not None
    assert process.returncode == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "created": [f"secrets/{name}" for name in EXPECTED_FILENAMES],
        "skipped": [],
    }
    rerun = bootstrap_local_secrets(root)
    assert rerun.created == ()
    assert rerun.skipped == tuple(f"secrets/{name}" for name in EXPECTED_FILENAMES)


def test_local_bootstrap_fails_before_creation_when_root_lock_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)

    def reject_lock(file_descriptor: int, operation: int) -> None:
        del file_descriptor, operation
        raise OSError("simulated lock failure")

    monkeypatch.setattr(platform_security.fcntl, "flock", reject_lock)

    with pytest.raises(LocalSecretBootstrapError, match="lock"):
        bootstrap_local_secrets(root)
    assert not (root / "secrets").exists()


def test_local_bootstrap_releases_process_lock_after_interrupt(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    child_code = """
from pathlib import Path
import sys

from adaptive_trader.platform import security

root = Path(sys.argv[1])
open_directory = security._open_local_secret_directory

def interrupt(*args: object, **kwargs: object) -> int:
    del args, kwargs
    raise KeyboardInterrupt

security._open_local_secret_directory = interrupt
try:
    security.bootstrap_local_secrets(root)
except KeyboardInterrupt:
    pass
finally:
    security._open_local_secret_directory = open_directory

security.bootstrap_local_secrets(root)
"""

    result = subprocess.run(
        [sys.executable, "-c", child_code, str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert tuple(sorted(path.name for path in (root / "secrets").iterdir())) == tuple(
        sorted(EXPECTED_FILENAMES)
    )


def test_local_bootstrap_does_not_read_ambient_provider_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    for name in (
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "AQA_ALPACA_DATA_API_KEY_FILE",
        "AQA_ALPACA_DATA_SECRET_KEY_FILE",
    ):
        monkeypatch.setenv(name, PROVIDER_SENTINEL)

    bootstrap_local_secrets(root)

    contains_sentinel = any(
        PROVIDER_SENTINEL.encode() in (root / "secrets" / name).read_bytes()
        for name in EXPECTED_FILENAMES
    )
    assert not contains_sentinel


def test_local_bootstrap_cli_emits_paths_only_and_reports_reruns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    monkeypatch.chdir(root)
    runner = CliRunner()

    first = runner.invoke(app, ["secrets", "bootstrap-local", "--json"], catch_exceptions=False)
    assert first.exit_code == 0
    first_payload = json.loads(first.stdout)
    assert first_payload == {
        "created": [f"secrets/{name}" for name in EXPECTED_FILENAMES],
        "skipped": [],
        "status": "ok",
    }
    contains_value = any(
        (root / "secrets" / name).read_text(encoding="utf-8").strip() in first.output
        for name in EXPECTED_FILENAMES
    )
    assert not contains_value

    second = runner.invoke(app, ["secrets", "bootstrap-local", "--json"])
    assert second.exit_code == 0
    assert json.loads(second.stdout) == {
        "created": [],
        "skipped": [f"secrets/{name}" for name in EXPECTED_FILENAMES],
        "status": "ok",
    }


def test_local_bootstrap_cli_translates_failure_without_sensitive_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application_root(tmp_path)
    directory = _secret_directory(root)
    _write_existing(directory / "postgres_password", PROVIDER_SENTINEL.encode(), mode=0o644)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(app, ["secrets", "bootstrap-local", "--json"])

    assert result.exit_code == 2
    assert PROVIDER_SENTINEL not in result.output
    assert str(root) not in result.output
    assert json.loads(result.stderr) == {
        "error": "local secret bootstrap failed",
        "status": "error",
    }


def test_local_bootstrap_script_runs_the_fixed_command(tmp_path: Path) -> None:
    root = _application_root(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_local.py"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [f"created: secrets/{name}" for name in EXPECTED_FILENAMES]
