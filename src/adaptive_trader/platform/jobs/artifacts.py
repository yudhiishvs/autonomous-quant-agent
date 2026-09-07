"""Descriptor-confined immutable JSON evidence for bounded jobs."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.jobs.models import require_artifact_id

_PREFIX = re.compile(r"^[a-z][a-z0-9-]{0,31}$", re.ASCII)
_BUCKET = "job-evidence"
_MAX_EVIDENCE_BYTES = 2_097_152
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW


class JobArtifactError(RuntimeError):
    """Immutable job evidence could not be safely published."""


class ImmutableJobArtifactStore:
    """Publish content-addressed evidence beneath one trusted artifact root."""

    def __init__(self, trusted_artifact_root: Path) -> None:
        if (
            type(trusted_artifact_root) is not type(Path())
            or not trusted_artifact_root.is_absolute()
        ):
            raise JobArtifactError("job artifact root must be an absolute Path")
        if trusted_artifact_root == Path(trusted_artifact_root.anchor) or any(
            component in {"", ".", ".."} for component in trusted_artifact_root.parts[1:]
        ):
            raise JobArtifactError("job artifact root is invalid")
        try:
            trusted_artifact_root.mkdir(mode=0o750, parents=True, exist_ok=True)
            if (
                trusted_artifact_root.is_symlink()
                or trusted_artifact_root.resolve(strict=True) != trusted_artifact_root
                or not stat.S_ISDIR(trusted_artifact_root.stat(follow_symlinks=False).st_mode)
            ):
                raise JobArtifactError("job artifact root is not a trusted directory")
        except JobArtifactError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise JobArtifactError("job artifact root could not be prepared") from None
        self._root = trusted_artifact_root

    def publish_json(self, *, prefix: str, payload: dict[str, JsonValue]) -> str:
        """Publish canonical JSON once and return its content-addressed artifact ID."""

        if type(prefix) is not str or _PREFIX.fullmatch(prefix) is None:
            raise JobArtifactError("job artifact prefix is invalid")
        if type(payload) is not dict:
            raise JobArtifactError("job artifact payload must be an object")
        try:
            encoded = canonical_json_bytes(payload) + b"\n"
        except (TypeError, UnicodeError, ValueError):
            raise JobArtifactError("job artifact payload is invalid") from None
        if not encoded or len(encoded) > _MAX_EVIDENCE_BYTES:
            raise JobArtifactError("job artifact payload exceeds the size limit")

        artifact_id = f"{prefix}-{sha256_hex(payload)}"
        require_artifact_id(artifact_id)
        root_fd = -1
        bucket_fd = -1
        try:
            root_fd = os.open(self._root, _DIRECTORY_FLAGS)
            try:
                os.mkdir(_BUCKET, mode=0o750, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            bucket_fd = os.open(_BUCKET, _DIRECTORY_FLAGS, dir_fd=root_fd)
            if not stat.S_ISDIR(os.fstat(bucket_fd).st_mode):
                raise JobArtifactError("job artifact bucket is invalid")
            self._publish_file(bucket_fd, f"{artifact_id}.json", encoded)
        except JobArtifactError:
            raise
        except (OSError, TypeError, ValueError):
            raise JobArtifactError("job artifact could not be published") from None
        finally:
            if bucket_fd >= 0:
                os.close(bucket_fd)
            if root_fd >= 0:
                os.close(root_fd)
        return artifact_id

    @staticmethod
    def _publish_file(directory_fd: int, filename: str, payload: bytes) -> None:
        staging_name = f".{filename}.{uuid4().hex}.tmp"
        descriptor = os.open(staging_name, _WRITE_FLAGS, 0o640, dir_fd=directory_fd)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise JobArtifactError("job artifact write did not make progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            try:
                os.link(
                    staging_name,
                    filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                ImmutableJobArtifactStore._require_existing(directory_fd, filename, payload)
            os.fsync(directory_fd)
        finally:
            os.close(descriptor)
            os.unlink(staging_name, dir_fd=directory_fd)
            os.fsync(directory_fd)

    @staticmethod
    def _require_existing(directory_fd: int, filename: str, expected: bytes) -> None:
        descriptor = os.open(filename, _READ_FLAGS | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_EVIDENCE_BYTES:
                raise JobArtifactError("existing job artifact is invalid")
            chunks: list[bytes] = []
            remaining = _MAX_EVIDENCE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if b"".join(chunks) != expected:
                raise JobArtifactError("job artifact identity conflicts with existing content")
        finally:
            os.close(descriptor)


__all__ = ["ImmutableJobArtifactStore", "JobArtifactError"]
