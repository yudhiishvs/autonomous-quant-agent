"""Durable, fixture-only data operations using canonical collection and artifact boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, select

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import RuntimeService, load_runtime_settings
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.datasets import (
    DatasetFreezeRequest,
    DatasetGapSummary,
    LocalFilesystemArtifactStore,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.service_cycles import (
    _OFFLINE_RECEIVED_AT,
    _OFFLINE_SESSION_CLOSE,
    _OFFLINE_SESSION_OPEN,
    OfflineMarketDataCycle,
    _stored_as_effective,
)
from adaptive_trader.platform.storage.datasets import (
    DatasetManifestRepository,
    freeze_and_register_dataset,
)
from adaptive_trader.platform.storage.engine import create_platform_engine
from adaptive_trader.platform.storage.experiments import ExperimentRepository
from adaptive_trader.platform.storage.market_data import BarIdentity, MarketDataRepository
from adaptive_trader.platform.storage.tables import (
    aqa_bar_events,
    aqa_bar_identities,
    aqa_bar_latest,
    metadata,
)

DataOperation = Literal["ingest-fixture", "aggregate", "freeze"]


class DataOperationError(RuntimeError):
    """A local data operation failed without disclosing database or artifact details."""


def run_data_operation(
    operation: DataOperation, *, config_root: Path, output: Path
) -> dict[str, object]:
    """Operate on the explicit offline profile's persistent SQLite fallback.

    The fixed fixture namespace cannot reach a provider or broker. Configuration values
    come from the requested trusted configs tree; ambient credential variables are ignored.
    """
    if operation not in {"ingest-fixture", "aggregate", "freeze"}:
        raise DataOperationError("data operation is unsupported")
    try:
        root = config_root.resolve(strict=True)
        if root.name != "configs":
            raise DataOperationError("data configuration root must be a configs directory")
        settings = load_runtime_settings(
            {}, service=RuntimeService.MARKET_DATA_WORKER, application_root=root.parent
        )
        engine = create_platform_engine(settings, application_name="aqa-offline-data-cli")
        try:
            metadata.create_all(engine)
            experiment = settings.platform.experiment.definition
            ExperimentRepository(engine).register(
                experiment, registered_at=_OFFLINE_SESSION_OPEN - timedelta(minutes=1)
            )
            cycle = OfflineMarketDataCycle(settings, engine)
            if operation == "ingest-fixture":
                result = cycle.ingest_fixture()
                evidence = _evidence(engine, operation=operation, timeframe="1Min")
                changed = result.work_units
            elif operation == "aggregate":
                result = cycle.aggregate_fixture()
                evidence = _evidence(engine, operation=operation, timeframe="15Min")
                changed = result.work_units
            else:
                repository = MarketDataRepository(engine)
                bars = []
                for symbol in experiment.active_tradable:
                    for index in range(26):
                        start = _OFFLINE_SESSION_OPEN + timedelta(minutes=15 * index)
                        event = repository.latest(
                            BarIdentity(
                                provider="fixture",
                                feed="iex",
                                adjustment="raw",
                                symbol=symbol,
                                timeframe="15Min",
                                start_at=start,
                                end_at=start + timedelta(minutes=15),
                            )
                        )
                        if event is None:
                            raise DataOperationError(
                                "aggregate the complete persisted fixture before freeze"
                            )
                        bars.append(_stored_as_effective(event))
                source_root = Path(__file__).resolve().parents[3]
                commit, dirty, lock_hash = _source_provenance(source_root)
                with LocalFilesystemArtifactStore(
                    trusted_artifact_root=settings.artifact_root
                ) as store:
                    frozen, registration = freeze_and_register_dataset(
                        DatasetFreezeRequest(
                            experiment=experiment,
                            symbols=experiment.active_tradable,
                            effective_bars=tuple(bars),
                            range_start_utc=_OFFLINE_SESSION_OPEN,
                            range_end_utc=_OFFLINE_SESSION_CLOSE,
                            gap_summary=DatasetGapSummary(len(bars), 0, 0, 0),
                            source_git_commit=commit,
                            dirty_worktree=dirty,
                            uv_lock_hash=lock_hash,
                            created_at=_OFFLINE_RECEIVED_AT + timedelta(minutes=1),
                        ),
                        store=store,
                        repository=DatasetManifestRepository(engine),
                        calendar=XnasExchangeCalendar(),
                    )
                if frozen.promotable:
                    raise DataOperationError("fixture dataset must remain non-promotable")
                evidence = frozen.manifest
                changed = int(registration.created)
            target = _publish(evidence, output, application_root=root.parent)
            return {
                "check": f"data {operation}",
                "status": "ok",
                "evidence_label": "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE",
                "evidence_path": target.relative_to(root.parent).as_posix(),
                "evidence_manifest_hash": sha256_hex(evidence),
                "row_count": evidence["row_count"],
                "work_units": changed,
                "promotable": False,
            }
        finally:
            engine.dispose()
    except DataOperationError:
        raise
    except Exception:
        raise DataOperationError("offline data operation failed") from None


def _evidence(engine: Engine, *, operation: str, timeframe: str) -> dict[str, object]:
    with engine.begin() as connection:
        hashes = tuple(
            connection.scalars(
                select(aqa_bar_events.c.content_hash)
                .select_from(
                    aqa_bar_latest.join(aqa_bar_identities).join(
                        aqa_bar_events,
                        aqa_bar_latest.c.bar_event_id == aqa_bar_events.c.bar_event_id,
                    )
                )
                .where(
                    aqa_bar_identities.c.provider == "fixture",
                    aqa_bar_identities.c.timeframe == timeframe,
                    aqa_bar_identities.c.start_at >= _OFFLINE_SESSION_OPEN,
                    aqa_bar_identities.c.end_at <= _OFFLINE_SESSION_CLOSE,
                )
                .order_by(aqa_bar_events.c.content_hash)
            )
        )
    digest = hashlib.sha256(b"data-cli-effective-events-v1\n")
    for event_hash in hashes:
        digest.update(event_hash.encode("ascii") + b"\n")
    return {
        "operation": operation,
        "row_count": len(hashes),
        "effective_events_hash": digest.hexdigest(),
        "range_start_utc": _OFFLINE_SESSION_OPEN,
        "range_end_utc": _OFFLINE_SESSION_CLOSE,
        "source_mode": "offline_fixture",
        "promotable": False,
    }


def _source_provenance(root: Path) -> tuple[str, bool, str]:
    """Read source identity only; no remote Git operation or working-tree mutation."""
    if not (root / ".git").exists():
        path = root / "source-provenance.json"
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        with os.fdopen(os.open(path, flags), "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > 4096
                or info.st_mode & 0o022
                or info.st_uid not in {0, os.geteuid()}
            ):
                raise DataOperationError("build source provenance is unsafe")
            value = json.load(stream)
        lock_hash = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
        if (
            type(value) is not dict
            or set(value)
            != {"schema_version", "source_git_commit", "dirty_worktree", "uv_lock_hash"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or type(value["source_git_commit"]) is not str
            or re.fullmatch(r"[0-9a-f]{40}", value["source_git_commit"]) is None
            or value["dirty_worktree"] is not True
            or value["uv_lock_hash"] != lock_hash
        ):
            raise DataOperationError("build source provenance is missing or invalid")
        return value["source_git_commit"], True, lock_hash
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    return commit, dirty, hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()


def _publish(value: dict[str, object], destination: Path, *, application_root: Path) -> Path:
    """Atomically publish an immutable receipt without traversing symbolic links."""
    if destination.is_absolute() or not destination.parts or ".." in destination.parts:
        raise DataOperationError("data evidence path must remain relative to the application root")
    root = application_root.resolve(strict=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".data-receipt-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        for part in destination.parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        payload = canonical_json_bytes(value) + b"\n"
        try:
            existing = os.open(destination.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with os.fdopen(existing, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > 1_048_576
                    or stream.read() != payload
                ):
                    raise DataOperationError("data evidence already contains different content")
            return root / destination
        file = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        created = True
        with os.fdopen(file, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary,
            destination.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.fsync(descriptor)
        return root / destination
    finally:
        try:
            if created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=descriptor)
        finally:
            os.close(descriptor)
