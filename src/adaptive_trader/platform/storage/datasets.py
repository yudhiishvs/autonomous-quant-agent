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
    CollectionDatasetProvenance,
    DatasetFreezeRequest,
    DatasetStatus,
    DatasetValidationError,
    FrozenDataset,
    SnapshotMetadataEvidence,
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
                # The dataset advisory lock serializes registration. Row locking would
                # require UPDATE authority on this append-only collector table.
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
    expected_keys = _EXPECTED_MANIFEST_KEYS
    if document.get("manifest_schema_version") == 2:
        expected_keys = expected_keys | {"collection_provenance", "diagnostic_only"}
    if frozenset(document) != expected_keys:
        raise DatasetValidationError("dataset manifest has an unexpected contract")
    if (
        type(document.get("manifest_schema_version")) is not int
        or document["manifest_schema_version"] not in {1, 2}
        or document.get("dataset_identity_version") != document["manifest_schema_version"]
    ):
        raise DatasetValidationError("dataset manifest version is invalid")
    if document["manifest_schema_version"] == 2 and (
        type(document.get("diagnostic_only")) is not bool
        or (document["diagnostic_only"] and frozen.promotable)
        or (
            document.get("collection_provenance") is not None
            and type(document["collection_provenance"]) is not dict
        )
    ):
        raise DatasetValidationError("dataset collection extension is invalid")
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
    if document["manifest_schema_version"] == 2:
        _validate_collection_extension(document, created_at, range_start, range_end)
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


def _validate_collection_extension(
    document: dict[str, Any], created_at: datetime, range_start: datetime, range_end: datetime
) -> None:
    payload = document["collection_provenance"]
    if payload is None:
        if not document["diagnostic_only"]:
            raise DatasetValidationError("dataset collection extension is empty")
        return
    keys = {
        "schema_version",
        "universe_version",
        "universe_hash",
        "members",
        "calendar_name",
        "calendar_version",
        "history_start_utc",
        "pending_derived_sessions",
        "persisted_gap_state_hash",
        "unresolved_persisted_gaps",
        "lagging_checkpoint_symbols",
        "metadata_evidence",
    }
    if (
        set(payload) != keys
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise DatasetValidationError("dataset collection provenance contract is invalid")
    if (
        type(payload["members"]) is not list
        or type(payload["lagging_checkpoint_symbols"]) is not list
    ):
        raise DatasetValidationError("dataset collection members or checkpoint state is invalid")
    members: list[tuple[str, str, str]] = []
    for member in payload["members"]:
        if (
            type(member) is not dict
            or set(member) != {"symbol", "collection_role", "research_role", "execution_authorized"}
            or member["execution_authorized"] is not False
        ):
            raise DatasetValidationError("dataset collection member grants invalid authority")
        members.append((member["symbol"], member["collection_role"], member["research_role"]))
    metadata_payload = payload["metadata_evidence"]
    metadata = (
        None
        if metadata_payload == {"listing_status": "unknown", "corporate_action_status": "unknown"}
        else SnapshotMetadataEvidence.from_payload(metadata_payload)
    )
    provenance = CollectionDatasetProvenance(
        universe_version=payload["universe_version"],
        universe_hash=payload["universe_hash"],
        members=tuple(members),
        calendar_name=payload["calendar_name"],
        calendar_version=payload["calendar_version"],
        history_start_utc=_utc_text(payload["history_start_utc"], field_name="history_start_utc"),
        pending_derived_sessions=payload["pending_derived_sessions"],
        persisted_gap_state_hash=payload["persisted_gap_state_hash"],
        unresolved_persisted_gaps=payload["unresolved_persisted_gaps"],
        lagging_checkpoint_symbols=tuple(payload["lagging_checkpoint_symbols"]),
        metadata_evidence=metadata,
    )
    if provenance.history_start_utc > range_start or (
        document["promotable"] and not provenance.permits_promotion
    ):
        raise DatasetValidationError("dataset collection evidence cannot authorize promotion")
    if metadata is not None and (
        list(metadata.symbols) != document["symbols"]
        or metadata.range_start_utc > range_start
        or metadata.range_end_utc < range_end
        or metadata.observed_at > created_at
    ):
        raise DatasetValidationError("dataset metadata does not cover its registered snapshot")


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
