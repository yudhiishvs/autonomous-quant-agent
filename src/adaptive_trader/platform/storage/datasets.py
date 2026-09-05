"""Transactional registration for immutable dataset artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Engine, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.data.calendar import ExchangeCalendar
from adaptive_trader.platform.data.datasets import (
    ArtifactStore,
    DatasetFreezeRequest,
    DatasetStatus,
    DatasetValidationError,
    FrozenDataset,
    freeze_dataset,
)
from adaptive_trader.platform.domain import AuditPayload, AuditWriter, require_utc_instant
from adaptive_trader.platform.errors import AuditPersistenceError, DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import aqa_dataset_manifests
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", flags=re.ASCII)
_EXPECTED_MANIFEST_KEYS = frozenset(
    {
        "adjustment",
        "artifact_id",
        "correction_summary",
        "created_at",
        "dataset_id",
        "dataset_identity_version",
        "dirty_worktree",
        "experiment_hash",
        "experiment_id",
        "experiment_version",
        "feed",
        "gap_summary",
        "logical_hash",
        "manifest_hash",
        "manifest_schema_version",
        "parquet_encoding",
        "parquet_size_bytes",
        "physical_hash",
        "promotable",
        "provider",
        "range_end_utc",
        "range_start_utc",
        "roles",
        "row_count",
        "row_counts",
        "schema_version",
        "source_git_commit",
        "source_mode",
        "status",
        "symbols",
        "timeframe",
        "uv_lock_hash",
    }
)


class DatasetManifestPersistenceError(RuntimeError):
    """Raised when immutable artifact metadata cannot be registered safely."""


@dataclass(frozen=True, slots=True)
class DatasetManifestRegistration:
    """Path-free result of one idempotent database registration."""

    dataset_id: str
    artifact_id: str
    experiment_hash: str
    manifest_hash: str
    content_hash: str
    status: DatasetStatus
    promotable: bool
    created_at: datetime
    created: bool


class DatasetManifestRepository:
    """Register an immutable manifest and its audit evidence in one transaction."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("dataset manifest repository requires a SQLAlchemy Engine")
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("dataset manifest repository requires PostgreSQL or SQLite")
        self._engine = engine
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.COLLECTOR)

    def register(self, frozen: FrozenDataset) -> DatasetManifestRegistration:
        """Persist one verified manifest; exact retries return the existing record."""

        values = _manifest_values(frozen)
        try:
            with self._transactions.transaction() as connection:
                self._transactions.acquire_postgres_advisory_lock(
                    connection,
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.DATASET_MANIFEST,
                        frozen.dataset_id,
                    ),
                )
                statement = select(aqa_dataset_manifests).where(
                    aqa_dataset_manifests.c.dataset_id == frozen.dataset_id
                )
                if connection.dialect.name == "postgresql":
                    statement = statement.with_for_update()
                existing = connection.execute(statement).mappings().one_or_none()
                if existing is not None:
                    _require_same_manifest(existing, values)
                    return _registration(values, created=False)

                connection.execute(insert(aqa_dataset_manifests).values(**values))
                self._audit.append(
                    stream_id=f"aqa_collector:data:{frozen.dataset_id}",
                    event_type="dataset.frozen",
                    occurred_at=cast(datetime, values["created_at"]),
                    payload=AuditPayload.from_mapping(
                        {
                            "content_hash": values["content_hash"],
                            "dataset_id": frozen.dataset_id,
                            "idempotency_key": f"dataset_frozen_{frozen.manifest_hash}",
                            "manifest_hash": frozen.manifest_hash,
                            "status": frozen.status.value,
                        }
                    ),
                    connection=connection,
                )
                return _registration(values, created=True)
        except (DatasetValidationError, DatasetManifestPersistenceError):
            raise
        except AuditPersistenceError:
            raise DatasetManifestPersistenceError(
                "dataset manifest audit evidence could not be persisted"
            ) from None
        except IntegrityError:
            raise DatasetManifestPersistenceError(
                "dataset manifest registration lost its idempotency race"
            ) from None
        except SQLAlchemyError:
            raise DatasetManifestPersistenceError(
                "dataset manifest could not be persisted"
            ) from None


def freeze_and_register_dataset(
    request: DatasetFreezeRequest,
    *,
    store: ArtifactStore,
    repository: DatasetManifestRepository,
    calendar: ExchangeCalendar | None = None,
) -> tuple[FrozenDataset, DatasetManifestRegistration]:
    """Publish immutable bytes, then transactionally register their verified manifest.

    A crash between publication and registration can leave only a content-addressed orphan. A
    retry verifies those immutable bytes before performing the idempotent database transaction.
    """

    if type(repository) is not DatasetManifestRepository:
        raise TypeError("dataset registration requires a manifest repository")
    frozen = freeze_dataset(request, store=store, calendar=calendar)
    return frozen, repository.register(frozen)


def _manifest_values(frozen: FrozenDataset) -> dict[str, Any]:
    if type(frozen) is not FrozenDataset:
        raise DatasetValidationError("dataset registration requires a frozen dataset")
    document = frozen.manifest
    if frozenset(document) != _EXPECTED_MANIFEST_KEYS:
        raise DatasetValidationError("dataset manifest has an unexpected contract")
    if canonical_json_bytes(document) != frozen.manifest_bytes:
        raise DatasetValidationError("dataset manifest is not canonically encoded")
    manifest_hash = document.get("manifest_hash")
    if type(manifest_hash) is not str or _HASH_PATTERN.fullmatch(manifest_hash) is None:
        raise DatasetValidationError("dataset manifest hash is invalid")
    unsigned = {key: value for key, value in document.items() if key != "manifest_hash"}
    if sha256_hex(unsigned) != manifest_hash or manifest_hash != frozen.manifest_hash:
        raise DatasetValidationError("dataset manifest hash does not match its content")
    scalar_matches = {
        "artifact_id": frozen.artifact_id,
        "dataset_id": frozen.dataset_id,
        "logical_hash": frozen.logical_hash,
        "parquet_size_bytes": frozen.parquet_size_bytes,
        "physical_hash": frozen.physical_hash,
        "promotable": frozen.promotable,
        "status": frozen.status.value,
    }
    if any(document.get(key) != value for key, value in scalar_matches.items()):
        raise DatasetValidationError("dataset manifest conflicts with its verified artifact")
    source_commit = document.get("source_git_commit")
    if type(source_commit) is not str or _COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise DatasetValidationError("dataset source commit is invalid")
    for key in ("experiment_hash", "uv_lock_hash"):
        value = document.get(key)
        if type(value) is not str or _HASH_PATTERN.fullmatch(value) is None:
            raise DatasetValidationError(f"dataset {key.replace('_', ' ')} is invalid")
    try:
        created_at = _utc_text(document.get("created_at"), field_name="created_at")
        range_start = _utc_text(document.get("range_start_utc"), field_name="range_start_utc")
        range_end = _utc_text(document.get("range_end_utc"), field_name="range_end_utc")
    except DomainValidationError:
        raise DatasetValidationError("dataset manifest timestamps are invalid") from None
    if created_at != frozen.created_at or range_end <= range_start:
        raise DatasetValidationError("dataset manifest time range is invalid")
    if type(document.get("schema_version")) is not int or document["schema_version"] < 1:
        raise DatasetValidationError("dataset schema version is invalid")
    if type(document.get("dirty_worktree")) is not bool:
        raise DatasetValidationError("dataset dirty-worktree state is invalid")
    for key in ("adjustment", "feed", "provider", "timeframe"):
        if type(document.get(key)) is not str or not document[key]:
            raise DatasetValidationError(f"dataset {key} is invalid")
    if type(document.get("roles")) is not list or type(document.get("symbols")) is not list:
        raise DatasetValidationError("dataset roles or symbols are invalid")
    if type(document.get("row_counts")) is not list:
        raise DatasetValidationError("dataset row counts are invalid")
    if (
        type(document.get("gap_summary")) is not dict
        or type(document.get("correction_summary")) is not dict
    ):
        raise DatasetValidationError("dataset summaries are invalid")

    values: dict[str, Any] = {
        "dataset_id": frozen.dataset_id,
        "artifact_id": frozen.artifact_id,
        "experiment_hash": document["experiment_hash"],
        "provider": document["provider"],
        "feed": document["feed"],
        "adjustment": document["adjustment"],
        "timeframe": document["timeframe"],
        "range_start_at": range_start,
        "range_end_at": range_end,
        "roles": document["roles"],
        "symbols": document["symbols"],
        "row_counts": document["row_counts"],
        "gap_summary": document["gap_summary"],
        "correction_summary": document["correction_summary"],
        "schema_version": document["schema_version"],
        "logical_hash": frozen.logical_hash,
        "physical_hash": frozen.physical_hash,
        "manifest_hash": frozen.manifest_hash,
        "source_git_commit": source_commit,
        "dirty_worktree": document["dirty_worktree"],
        "uv_lock_hash": document["uv_lock_hash"],
        "promotable": frozen.promotable,
        "status": frozen.status.value,
        "created_at": created_at,
    }
    values["content_hash"] = sha256_hex(("dataset_manifest_record_v1", values))
    return values


def _utc_text(value: object, *, field_name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise DomainValidationError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise DomainValidationError(f"{field_name} is invalid") from None
    return require_utc_instant(parsed, field_name=field_name)


def _require_same_manifest(existing: RowMapping, expected: dict[str, Any]) -> None:
    actual = {column.name: existing[column.name] for column in aqa_dataset_manifests.columns}
    if actual != expected:
        raise DatasetManifestPersistenceError(
            "dataset ID is already registered with different immutable content"
        )


def _registration(values: dict[str, Any], *, created: bool) -> DatasetManifestRegistration:
    return DatasetManifestRegistration(
        dataset_id=cast(str, values["dataset_id"]),
        artifact_id=cast(str, values["artifact_id"]),
        experiment_hash=cast(str, values["experiment_hash"]),
        manifest_hash=cast(str, values["manifest_hash"]),
        content_hash=cast(str, values["content_hash"]),
        status=DatasetStatus(cast(str, values["status"])),
        promotable=cast(bool, values["promotable"]),
        created_at=cast(datetime, values["created_at"]),
        created=created,
    )


__all__ = [
    "DatasetManifestPersistenceError",
    "DatasetManifestRegistration",
    "DatasetManifestRepository",
    "freeze_and_register_dataset",
]
