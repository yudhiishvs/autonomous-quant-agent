"""Immutable evidence survives interrupted writes and concurrent publication."""

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from adaptive_trader.platform.jobs.artifacts import ImmutableJobArtifactStore, JobArtifactError


def test_interrupted_write_never_publishes_partial_evidence_and_retry_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve() / "artifacts"
    store = ImmutableJobArtifactStore(root)
    original_write = os.write
    writes = 0

    def interrupted_write(descriptor: int, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        assert not list((root / "job-evidence").glob("*.json"))
        if writes == 1:
            return original_write(descriptor, payload[:3])
        raise OSError("injected disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", interrupted_write)
        with pytest.raises(JobArtifactError, match="could not be published"):
            store.publish_json(prefix="quality", payload={"schema": "evidence-v1"})
    assert not list((root / "job-evidence").iterdir())
    restarted = ImmutableJobArtifactStore(root)
    artifact_id = restarted.publish_json(prefix="quality", payload={"schema": "evidence-v1"})
    assert (root / "job-evidence" / f"{artifact_id}.json").read_bytes() == (
        b'{"schema":"evidence-v1"}\n'
    )


def test_abandoned_staging_file_does_not_block_restart(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "artifacts"
    bucket = root / "job-evidence"
    bucket.mkdir(parents=True)
    abandoned = bucket / ".quality-abandoned.tmp"
    abandoned.write_bytes(b'{"sch')
    store = ImmutableJobArtifactStore(root)
    artifact_id = store.publish_json(prefix="quality", payload={"schema": "evidence-v1"})
    assert (bucket / f"{artifact_id}.json").read_bytes() == b'{"schema":"evidence-v1"}\n'
    assert abandoned.read_bytes() == b'{"sch'


def test_file_fsync_failure_keeps_final_name_absent_and_retry_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve() / "artifacts"
    store = ImmutableJobArtifactStore(root)
    original_fsync = os.fsync

    def interrupted_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("injected durability failure")
        original_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", interrupted_fsync)
        with pytest.raises(JobArtifactError):
            store.publish_json(prefix="quality", payload={"value": 1})
    assert not list((root / "job-evidence").iterdir())
    artifact_id = store.publish_json(prefix="quality", payload={"value": 1})
    assert (root / "job-evidence" / f"{artifact_id}.json").read_bytes() == b'{"value":1}\n'


def test_concurrent_publishers_agree_on_one_complete_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve() / "artifacts"
    store = ImmutableJobArtifactStore(root)
    barrier = Barrier(4)
    original_link = os.link

    def synchronized_link(*args: object, **kwargs: object) -> None:
        barrier.wait(timeout=5)
        original_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [
            workers.submit(store.publish_json, prefix="quality", payload={"value": 1})
            for _ in range(4)
        ]
        ids = [future.result(timeout=10) for future in futures]
    assert len(set(ids)) == 1
    files = list((root / "job-evidence").iterdir())
    assert files == [root / "job-evidence" / f"{ids[0]}.json"]
    assert files[0].read_bytes() == b'{"value":1}\n'


def test_artifact_bucket_symlink_cannot_publish_outside_root(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "artifacts"
    store = ImmutableJobArtifactStore(root)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (root / "job-evidence").symlink_to(outside, target_is_directory=True)
    with pytest.raises(JobArtifactError):
        store.publish_json(prefix="quality", payload={"value": 1})
    assert not list(outside.iterdir())
