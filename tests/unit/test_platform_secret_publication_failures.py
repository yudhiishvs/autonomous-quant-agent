"""Publication/loading failures preserve existing local files and disclose no payload."""

import hashlib
import os

import pytest

import adaptive_trader.platform.security as security
from adaptive_trader.platform.security import (
    LOCAL_BOOTSTRAP_FILENAMES,
    LocalSecretBootstrapError,
    bootstrap_local_secrets,
    load_local_bootstrap_secret,
)
from tests.unit.test_platform_secret_bootstrap import _patch_os_capability


def inventory(root):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "secrets").iterdir()
        if path.is_file()
    }


@pytest.fixture
def bootstrapped(tmp_path):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    bootstrap_local_secrets(root)
    return root


@pytest.mark.parametrize("name", ["../operator_token", "/etc/passwd", "", "not-registered", None])
def test_loader_rejects_unregistered_names_without_changing_files(bootstrapped, name):
    before = inventory(bootstrapped)
    with pytest.raises(LocalSecretBootstrapError):
        load_local_bootstrap_secret(bootstrapped, name)
    assert inventory(bootstrapped) == before


def test_loader_missing_secret_directory_never_creates_it(tmp_path):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    with pytest.raises(LocalSecretBootstrapError):
        load_local_bootstrap_secret(root, "operator_token")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("fault", ["read", "open", "stat"])
def test_loading_io_failure_preserves_originals_and_hides_error_payload(
    bootstrapped, monkeypatch, fault
):
    before = inventory(bootstrapped)
    original = getattr(security.os, fault)

    def fail(*args, **kwargs):
        if fault == "read" or (args and args[0] == "operator_token"):
            raise OSError("synthetic private payload must not escape")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        _patch_os_capability(patch, fault, fail)
        with pytest.raises(LocalSecretBootstrapError) as error:
            load_local_bootstrap_secret(bootstrapped, "operator_token")
        assert "synthetic private payload" not in str(error.value)
    assert inventory(bootstrapped) == before
    assert str(load_local_bootstrap_secret(bootstrapped, "operator_token")) == "<redacted>"


def test_short_writes_publish_complete_owner_private_secret(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    write = os.write
    calls = []

    def short_write(fd, payload):
        calls.append(len(payload))
        return write(fd, payload[:3])

    with monkeypatch.context() as patch:
        patch.setattr(security.os, "write", short_write)
        result = bootstrap_local_secrets(root)
    assert len(result.created) == len(LOCAL_BOOTSTRAP_FILENAMES)
    assert len(calls) > len(result.created)
    assert len(list((root / "secrets").iterdir())) == len(LOCAL_BOOTSTRAP_FILENAMES)
    for name in LOCAL_BOOTSTRAP_FILENAMES:
        assert (root / "secrets" / name).stat().st_mode & 0o777 == 0o600
        load_local_bootstrap_secret(root, name)


def test_concurrent_publication_collision_preserves_winning_file(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    link = os.link
    collided = []

    def competing_link(source, destination, **kwargs):
        if not collided:
            collided.append(destination)
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                os.write(fd, b"synthetic-local-test-token-1234567890\n")
            finally:
                os.close(fd)
            raise FileExistsError("concurrent publish")
        return link(source, destination, **kwargs)

    with monkeypatch.context() as patch:
        _patch_os_capability(patch, "link", competing_link)
        result = bootstrap_local_secrets(root)
    assert len(result.skipped) == 1
    assert (
        root / "secrets" / collided[0]
    ).read_bytes() == b"synthetic-local-test-token-1234567890\n"
    assert len(list((root / "secrets").iterdir())) == len(LOCAL_BOOTSTRAP_FILENAMES)


def test_failure_after_publication_preserves_valid_published_file_for_retry(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir(mode=0o700)
    stat = os.stat
    link = os.link
    published = []

    def record_link(source, destination, **kwargs):
        result = link(source, destination, **kwargs)
        published.append(destination)
        return result

    def fail_verification(path, *args, **kwargs):
        if published and path == published[-1] and kwargs.get("dir_fd") is not None:
            raise OSError("post-publication verification fault")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        _patch_os_capability(patch, "link", record_link)
        _patch_os_capability(patch, "stat", fail_verification)
        with pytest.raises(LocalSecretBootstrapError):
            bootstrap_local_secrets(root)
    before = inventory(root)
    assert len(before) == 1
    retried = bootstrap_local_secrets(root)
    assert len(retried.skipped) == 1
    assert all(inventory(root)[name] == digest for name, digest in before.items())
