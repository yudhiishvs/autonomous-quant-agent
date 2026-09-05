"""Immutable, content-addressed Parquet datasets and local artifact storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Protocol, Self, cast, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.constants import MAX_SIGNED_64_BIT_INTEGER
from adaptive_trader.platform.data.aggregation import EffectiveBar
from adaptive_trader.platform.data.calendar import (
    CalendarValidationError,
    ExchangeCalendar,
    XnasExchangeCalendar,
)
from adaptive_trader.platform.data.normalization import CanonicalBar
from adaptive_trader.platform.domain import require_utc_instant
from adaptive_trader.platform.errors import CanonicalizationError, DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.universe import SymbolRole

_ARTIFACT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$", flags=re.ASCII)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", flags=re.ASCII)
_UTC_TEXT_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$",
    flags=re.ASCII,
)
_DATASET_IDENTITY_DOMAIN = b"aqa.logical.dataset.v1\x00"
_PARQUET_NAME = "bars.parquet"
_MANIFEST_NAME = "manifest.json"
_EXPECTED_ARTIFACT_MEMBERS = frozenset({_PARQUET_NAME, _MANIFEST_NAME})
_MANIFEST_MAX_BYTES = 1_048_576
_COPY_CHUNK_BYTES = 1_048_576
_ROW_GROUP_SIZE = 8_192
_WRITE_BATCH_SIZE = 1_024
_COMPRESSION = "zstd"
_COMPRESSION_LEVEL = 9
_FILE_MODE = 0o640
_DIRECTORY_MODE = 0o750
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_WRITE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class DatasetError(DomainValidationError):
    """Base failure at the immutable dataset boundary."""


class DatasetValidationError(DatasetError):
    """Raised when bars or lineage metadata cannot define a dataset."""


class ArtifactSecurityError(DatasetError):
    """Raised when an artifact path violates the filesystem trust boundary."""


class ArtifactConflictError(DatasetError):
    """Raised when an immutable artifact ID already names different bytes."""


class ArtifactIntegrityError(DatasetError):
    """Raised when a stored artifact is incomplete or fails content verification."""


class DatasetStatus(StrEnum):
    """Closed promotion state recorded in every dataset manifest."""

    PROMOTABLE = "promotable"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True, slots=True)
class DatasetGapSummary:
    """Calendar-derived gap evidence supplied to the dataset freeze boundary."""

    expected_rows: int
    missing_rows: int
    unresolved_gap_count: int
    repaired_gap_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "expected_rows",
            "missing_rows",
            "unresolved_gap_count",
            "repaired_gap_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= MAX_SIGNED_64_BIT_INTEGER:
                raise DatasetValidationError(f"dataset {field_name} is invalid")
        if self.missing_rows > self.expected_rows:
            raise DatasetValidationError("dataset missing rows exceed expected rows")
        if self.unresolved_gap_count > self.missing_rows:
            raise DatasetValidationError("dataset unresolved gaps exceed missing rows")

    def payload(self) -> dict[str, int]:
        """Return a fresh canonical manifest representation."""

        return {
            "expected_rows": self.expected_rows,
            "missing_rows": self.missing_rows,
            "repaired_gap_count": self.repaired_gap_count,
            "unresolved_gap_count": self.unresolved_gap_count,
        }


@dataclass(frozen=True, slots=True)
class DatasetCorrectionSummary:
    """Effective-revision evidence derived from the rows being frozen."""

    effective_corrected_rows: int
    superseded_revision_count: int

    def payload(self) -> dict[str, int]:
        """Return a fresh canonical manifest representation."""

        return {
            "effective_corrected_rows": self.effective_corrected_rows,
            "superseded_revision_count": self.superseded_revision_count,
        }


@dataclass(frozen=True, slots=True)
class DatasetFreezeRequest:
    """Trusted metadata and current effective events for one immutable freeze.

    ``symbols`` is the exact experiment subset represented by the dataset. It is an authority
    value, not inferred from rows, so a symbol with a fully missing range cannot disappear from
    the manifest. The gap summary is computed by the calendar-aware caller and reconciled here
    against the number of effective rows.
    """

    experiment: ExperimentDefinition
    symbols: tuple[str, ...]
    effective_bars: tuple[EffectiveBar, ...]
    range_start_utc: datetime
    range_end_utc: datetime
    gap_summary: DatasetGapSummary
    source_git_commit: str
    dirty_worktree: bool
    uv_lock_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.experiment) is not ExperimentDefinition:
            raise DatasetValidationError("dataset experiment is invalid")
        if type(self.symbols) is not tuple or not self.symbols:
            raise DatasetValidationError("dataset symbols must be a nonempty immutable tuple")
        if self.symbols != tuple(sorted(self.symbols)) or len(self.symbols) != len(
            set(self.symbols)
        ):
            raise DatasetValidationError("dataset symbols must be uniquely sorted")
        if any(symbol not in self.experiment.collection_allowlist for symbol in self.symbols):
            raise DatasetValidationError("dataset symbol is outside the experiment allowlist")
        if type(self.effective_bars) is not tuple or not self.effective_bars:
            raise DatasetValidationError(
                "dataset effective bars must be a nonempty immutable tuple"
            )
        if any(type(item) is not EffectiveBar for item in self.effective_bars):
            raise DatasetValidationError("dataset contains an invalid effective bar")
        if type(self.gap_summary) is not DatasetGapSummary:
            raise DatasetValidationError("dataset gap summary is invalid")

        range_start = _require_utc(self.range_start_utc, field_name="range_start_utc")
        range_end = _require_utc(self.range_end_utc, field_name="range_end_utc")
        created_at = _require_utc(self.created_at, field_name="created_at")
        if range_end <= range_start:
            raise DatasetValidationError("dataset range must be positive")
        object.__setattr__(self, "range_start_utc", range_start)
        object.__setattr__(self, "range_end_utc", range_end)
        object.__setattr__(self, "created_at", created_at)

        if (
            type(self.source_git_commit) is not str
            or _GIT_COMMIT_PATTERN.fullmatch(self.source_git_commit) is None
        ):
            raise DatasetValidationError("dataset source Git commit is invalid")
        _require_hash(self.uv_lock_hash, field_name="uv.lock hash")
        if type(self.dirty_worktree) is not bool:
            raise DatasetValidationError("dataset dirty-worktree flag must be boolean")


@dataclass(frozen=True, slots=True)
class StoredDatasetArtifact:
    """Path-free verification result returned by an artifact store."""

    artifact_id: str
    physical_hash: str
    parquet_size_bytes: int
    manifest_bytes: bytes
    created: bool

    def __post_init__(self) -> None:
        _validate_artifact_id(self.artifact_id)
        _require_hash(self.physical_hash, field_name="physical hash")
        if (
            type(self.parquet_size_bytes) is not int
            or not 0 <= self.parquet_size_bytes <= MAX_SIGNED_64_BIT_INTEGER
        ):
            raise ArtifactIntegrityError("artifact size is invalid")
        if (
            type(self.manifest_bytes) is not bytes
            or not self.manifest_bytes
            or len(self.manifest_bytes) > _MANIFEST_MAX_BYTES
        ):
            raise ArtifactIntegrityError("artifact manifest bytes are invalid")
        if type(self.created) is not bool:
            raise ArtifactIntegrityError("artifact creation result is invalid")


@runtime_checkable
class ArtifactStore(Protocol):
    """Path-free immutable storage boundary suitable for local or remote implementations."""

    def load_dataset(self, artifact_id: str) -> StoredDatasetArtifact | None:
        """Load and physically verify an artifact, or return ``None`` when absent."""

    def publish_dataset(
        self,
        artifact_id: str,
        *,
        parquet_stream: BinaryIO,
        physical_hash: str,
        parquet_size_bytes: int,
        manifest_bytes: bytes,
    ) -> StoredDatasetArtifact:
        """Publish staged bytes without replacing an existing artifact ID."""


@dataclass(frozen=True, slots=True)
class FrozenDataset:
    """Immutable, path-free result of a successful dataset freeze."""

    dataset_id: str
    artifact_id: str
    logical_hash: str
    physical_hash: str
    manifest_hash: str
    parquet_size_bytes: int
    row_count: int
    promotable: bool
    status: DatasetStatus
    created_at: datetime
    manifest_bytes: bytes
    created: bool

    def __post_init__(self) -> None:
        if self.dataset_id != f"dataset_{self.logical_hash}":
            raise ArtifactIntegrityError("dataset ID does not match its logical hash")
        if self.artifact_id != self.dataset_id:
            raise ArtifactIntegrityError("dataset artifact ID does not match its dataset ID")
        _validate_artifact_id(self.artifact_id)
        for field_name in ("logical_hash", "physical_hash", "manifest_hash"):
            _require_hash(getattr(self, field_name), field_name=field_name.replace("_", " "))
        if type(self.row_count) is not int or not 1 <= self.row_count <= MAX_SIGNED_64_BIT_INTEGER:
            raise ArtifactIntegrityError("dataset row count is invalid")
        if type(self.promotable) is not bool:
            raise ArtifactIntegrityError("dataset promotability is invalid")
        if type(self.status) is not DatasetStatus:
            raise ArtifactIntegrityError("dataset status is invalid")
        if self.promotable != (self.status is DatasetStatus.PROMOTABLE):
            raise ArtifactIntegrityError("dataset status conflicts with promotability")
        _require_utc(self.created_at, field_name="created_at")
        StoredDatasetArtifact(
            artifact_id=self.artifact_id,
            physical_hash=self.physical_hash,
            parquet_size_bytes=self.parquet_size_bytes,
            manifest_bytes=self.manifest_bytes,
            created=self.created,
        )

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a fresh decoded manifest without exposing mutable internal state."""

        return _decode_manifest(self.manifest_bytes)


DATASET_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("bar_event_id", pa.string(), nullable=False),
        pa.field("revision", pa.int64(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("feed", pa.string(), nullable=False),
        pa.field("adjustment", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("source_mode", pa.string(), nullable=False),
        pa.field("interval_start_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("interval_end_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("receipt_timestamp_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("provider_event_timestamp_utc", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume", pa.string(), nullable=False),
        pa.field("trade_count", pa.int64(), nullable=True),
        pa.field("vwap", pa.string(), nullable=True),
        pa.field("schema_version", pa.int32(), nullable=False),
        pa.field("source_event_id", pa.string(), nullable=False),
        pa.field("quality_flags", pa.list_(pa.string()), nullable=False),
        pa.field("is_correction", pa.bool_(), nullable=False),
        pa.field("correction_of_source_event_id", pa.string(), nullable=True),
        pa.field("payload_hash", pa.string(), nullable=False),
    ],
    metadata={
        b"aqa.dataset_schema": b"canonical_effective_bars_v1",
        b"aqa.decimal_encoding": b"canonical_plain_utf8",
    },
)


class LocalFilesystemArtifactStore:
    """Descriptor-confined immutable artifacts rooted at trusted runtime configuration.

    Callers pass the already validated ``RuntimeSettings.artifact_root``. Every later path is
    derived from an artifact ID; no method accepts a filename or output path. Descriptor-relative
    operations and ``O_NOFOLLOW`` keep publication inside the root even when hostile symbolic
    links are present. A process-shared advisory lock serializes the existence check and atomic
    directory rename used for publication.
    """

    def __init__(self, *, trusted_artifact_root: Path) -> None:
        if (
            type(trusted_artifact_root) is not type(Path())
            or not trusted_artifact_root.is_absolute()
            or trusted_artifact_root == Path(trusted_artifact_root.anchor)
        ):
            raise ArtifactSecurityError("artifact root must be a trusted absolute Path")
        if any(part in {".", "..", ""} for part in trusted_artifact_root.parts[1:]):
            raise ArtifactSecurityError("artifact root is not canonical")
        self._root_fd = _open_or_create_directory_chain(trusted_artifact_root)

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def __del__(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            self._root_fd = -1
            with suppress(OSError):
                os.close(descriptor)

    def close(self) -> None:
        """Release the retained trusted-root descriptor."""

        descriptor = self._root_fd
        if descriptor >= 0:
            self._root_fd = -1
            os.close(descriptor)

    def load_dataset(self, artifact_id: str) -> StoredDatasetArtifact | None:
        """Load an artifact after checking members, links, size, and physical hash."""

        validated = _validate_artifact_id(artifact_id)
        with self._locked_root() as root_fd:
            bucket_fd = _open_bucket(root_fd, validated, create=False)
            if bucket_fd is None:
                return None
            try:
                return _load_artifact(bucket_fd, validated)
            finally:
                os.close(bucket_fd)

    def publish_dataset(
        self,
        artifact_id: str,
        *,
        parquet_stream: BinaryIO,
        physical_hash: str,
        parquet_size_bytes: int,
        manifest_bytes: bytes,
    ) -> StoredDatasetArtifact:
        """Fsync staged members and atomically publish an immutable directory."""

        validated = _validate_artifact_id(artifact_id)
        _require_hash(physical_hash, field_name="physical hash")
        if (
            type(parquet_size_bytes) is not int
            or not 0 <= parquet_size_bytes <= MAX_SIGNED_64_BIT_INTEGER
        ):
            raise ArtifactIntegrityError("artifact size is invalid")
        if (
            type(manifest_bytes) is not bytes
            or not manifest_bytes
            or len(manifest_bytes) > _MANIFEST_MAX_BYTES
        ):
            raise ArtifactIntegrityError("artifact manifest bytes are invalid")
        if not callable(getattr(parquet_stream, "read", None)) or not callable(
            getattr(parquet_stream, "seek", None)
        ):
            raise ArtifactIntegrityError("artifact Parquet source is not seekable binary data")

        with self._locked_root() as root_fd:
            bucket_fd = _open_bucket(root_fd, validated, create=True)
            if bucket_fd is None:  # pragma: no cover - create=True is a closed internal contract
                raise ArtifactIntegrityError("artifact bucket creation failed")
            try:
                existing = _load_artifact(bucket_fd, validated)
                if existing is not None:
                    if (
                        existing.physical_hash == physical_hash
                        and existing.parquet_size_bytes == parquet_size_bytes
                        and existing.manifest_bytes == manifest_bytes
                    ):
                        return existing
                    raise ArtifactConflictError("artifact ID already contains different content")
                return _publish_staged_artifact(
                    bucket_fd,
                    validated,
                    parquet_stream=parquet_stream,
                    expected_hash=physical_hash,
                    expected_size=parquet_size_bytes,
                    manifest_bytes=manifest_bytes,
                )
            finally:
                os.close(bucket_fd)

    @contextmanager
    def _locked_root(self) -> Iterator[int]:
        self._require_open()
        root_fd = os.dup(self._root_fd)
        lock_fd = -1
        try:
            lock_fd = os.open(
                ".publish.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                _FILE_MODE,
                dir_fd=root_fd,
            )
            lock_status = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
                raise ArtifactSecurityError("artifact publication lock is unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield root_fd
        except OSError as exc:
            raise ArtifactSecurityError("artifact root could not be accessed safely") from exc
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(root_fd)

    def _require_open(self) -> None:
        if self._root_fd < 0:
            raise ArtifactSecurityError("artifact store is closed")


def freeze_dataset(
    request: DatasetFreezeRequest,
    *,
    store: ArtifactStore,
    calendar: ExchangeCalendar | None = None,
) -> FrozenDataset:
    """Freeze current effective bars into deterministic Parquet and an immutable manifest."""

    if type(request) is not DatasetFreezeRequest:
        raise DatasetValidationError("dataset freeze request is invalid")
    if not isinstance(store, ArtifactStore):
        raise DatasetValidationError("dataset artifact store is invalid")
    selected_calendar: ExchangeCalendar = XnasExchangeCalendar() if calendar is None else calendar
    if not isinstance(selected_calendar, ExchangeCalendar):
        raise DatasetValidationError("dataset exchange calendar is invalid")
    if selected_calendar.name != request.experiment.market_data.exchange_calendar:
        raise DatasetValidationError("dataset exchange calendar conflicts with the experiment")

    ordered = _validate_and_order_rows(request, calendar=selected_calendar)
    correction_summary = _correction_summary(ordered)
    row_counts = _row_counts(request.symbols, ordered)
    roles = _role_payload(request)
    first = ordered[0].bar
    intrinsically_promotable = (
        first.source_mode == "external_provider"
        and request.gap_summary.missing_rows == 0
        and request.gap_summary.unresolved_gap_count == 0
        and all(item.bar.has_promotable_provenance for item in ordered)
        and all("complete" in item.bar.quality_flags for item in ordered)
    )
    promotable = intrinsically_promotable and not request.dirty_worktree
    status = DatasetStatus.PROMOTABLE if promotable else DatasetStatus.DIAGNOSTIC
    logical_header = _logical_header(
        request,
        first=first,
        roles=roles,
        row_counts=row_counts,
        correction_summary=correction_summary,
    )
    logical_hash = _logical_dataset_hash(logical_header, ordered)
    dataset_id = f"dataset_{logical_hash}"

    with tempfile.TemporaryFile(mode="w+b") as parquet_stream:
        _write_parquet(ordered, parquet_stream)
        parquet_stream.flush()
        parquet_size_bytes, physical_hash = _hash_stream(parquet_stream)

        existing = store.load_dataset(dataset_id)
        if existing is not None:
            return _verified_result(
                existing,
                expected_header=logical_header,
                logical_hash=logical_hash,
                row_count=len(ordered),
                intrinsically_promotable=intrinsically_promotable,
                expected_physical_hash=physical_hash,
                expected_parquet_size=parquet_size_bytes,
            )

        manifest_base = _manifest_base(
            logical_header,
            dataset_id=dataset_id,
            logical_hash=logical_hash,
            physical_hash=physical_hash,
            parquet_size_bytes=parquet_size_bytes,
            created_at=request.created_at,
            source_git_commit=request.source_git_commit,
            dirty_worktree=request.dirty_worktree,
            uv_lock_hash=request.uv_lock_hash,
            promotable=promotable,
            status=status,
        )
        manifest_hash = sha256_hex(manifest_base)
        manifest_bytes = canonical_json_bytes({**manifest_base, "manifest_hash": manifest_hash})
        parquet_stream.seek(0)
        try:
            stored = store.publish_dataset(
                dataset_id,
                parquet_stream=parquet_stream,
                physical_hash=physical_hash,
                parquet_size_bytes=parquet_size_bytes,
                manifest_bytes=manifest_bytes,
            )
        except ArtifactConflictError:
            # Another producer may have atomically won an identical logical freeze with a
            # different creation timestamp. Its complete artifact must independently verify.
            recovered = store.load_dataset(dataset_id)
            if recovered is None:
                raise
            stored = recovered
        return _verified_result(
            stored,
            expected_header=logical_header,
            logical_hash=logical_hash,
            row_count=len(ordered),
            intrinsically_promotable=intrinsically_promotable,
            expected_physical_hash=physical_hash,
            expected_parquet_size=parquet_size_bytes,
        )


def _validate_and_order_rows(
    request: DatasetFreezeRequest,
    *,
    calendar: ExchangeCalendar,
) -> tuple[EffectiveBar, ...]:
    ordered = tuple(
        sorted(
            request.effective_bars,
            key=lambda item: (
                item.bar.symbol,
                item.bar.timeframe,
                item.bar.interval_start_utc,
                item.revision,
                item.bar_event_id,
            ),
        )
    )
    first = ordered[0].bar
    experiment_series = request.experiment.market_data
    if first.timeframe not in {
        experiment_series.source_timeframe,
        experiment_series.decision_timeframe,
    }:
        raise DatasetValidationError("dataset timeframe is outside the experiment contract")
    if (
        first.feed != experiment_series.feed
        or first.adjustment != experiment_series.adjustment
        or (
            first.source_mode == "external_provider"
            and first.provider != experiment_series.provider
        )
    ):
        raise DatasetValidationError("dataset series conflicts with the experiment contract")
    expected_series = (
        first.provider,
        first.feed,
        first.adjustment,
        first.timeframe,
        first.source_mode,
        first.schema_version,
    )
    identities: set[tuple[str, str, str, str, str, datetime]] = set()
    event_ids: set[str] = set()
    try:
        expected_intervals = calendar.expected_intervals(
            start_at=request.range_start_utc,
            end_at=request.range_end_utc,
            timeframe=first.timeframe,
        )
    except CalendarValidationError:
        raise DatasetValidationError("dataset range is invalid for its exchange calendar") from None
    expected_identities = {
        (symbol, interval.start_at, interval.end_at)
        for symbol in request.symbols
        for interval in expected_intervals
    }
    observed_identities: set[tuple[str, datetime, datetime]] = set()
    for item in ordered:
        bar = item.bar
        if (
            bar.provider,
            bar.feed,
            bar.adjustment,
            bar.timeframe,
            bar.source_mode,
            bar.schema_version,
        ) != expected_series:
            raise DatasetValidationError("dataset bars do not share one canonical series")
        if bar.symbol not in request.symbols:
            raise DatasetValidationError("dataset row is outside the exact symbol selection")
        if not (
            request.range_start_utc <= bar.interval_start_utc
            and bar.interval_end_utc <= request.range_end_utc
        ):
            raise DatasetValidationError("dataset row is outside the declared UTC range")
        observed_identity = (bar.symbol, bar.interval_start_utc, bar.interval_end_utc)
        if observed_identity not in expected_identities:
            raise DatasetValidationError("dataset row is not an expected exchange interval")
        if bar.identity_key in identities:
            raise DatasetValidationError(
                "dataset contains multiple effective rows for one identity"
            )
        if item.bar_event_id in event_ids:
            raise DatasetValidationError("dataset contains a duplicate effective event ID")
        identities.add(bar.identity_key)
        observed_identities.add(observed_identity)
        event_ids.add(item.bar_event_id)
    expected_rows = len(expected_identities)
    missing_rows = len(expected_identities - observed_identities)
    if (
        request.gap_summary.expected_rows != expected_rows
        or request.gap_summary.missing_rows != missing_rows
    ):
        raise DatasetValidationError(
            "dataset coverage does not reconcile with the exchange calendar"
        )
    return ordered


def _correction_summary(rows: tuple[EffectiveBar, ...]) -> DatasetCorrectionSummary:
    return DatasetCorrectionSummary(
        effective_corrected_rows=sum(item.revision > 1 for item in rows),
        superseded_revision_count=sum(item.revision - 1 for item in rows),
    )


def _role_payload(request: DatasetFreezeRequest) -> tuple[dict[str, object], ...]:
    selected = set(request.symbols)
    experiment = request.experiment
    memberships = (
        (SymbolRole.ACTIVE_TRADABLE, experiment.active_tradable),
        (SymbolRole.BENCHMARK_ONLY, experiment.benchmark_only),
        (SymbolRole.CONTEXT_ONLY, experiment.context_only),
        (SymbolRole.EXCLUDED, experiment.excluded),
    )
    return tuple(
        {
            "role": role.value,
            "symbols": tuple(symbol for symbol in symbols if symbol in selected),
        }
        for role, symbols in memberships
    )


def _row_counts(
    symbols: tuple[str, ...],
    rows: tuple[EffectiveBar, ...],
) -> tuple[dict[str, object], ...]:
    timeframe = rows[0].bar.timeframe
    return tuple(
        {
            "row_count": sum(item.bar.symbol == symbol for item in rows),
            "symbol": symbol,
            "timeframe": timeframe,
        }
        for symbol in symbols
    )


def _logical_header(
    request: DatasetFreezeRequest,
    *,
    first: CanonicalBar,
    roles: tuple[dict[str, object], ...],
    row_counts: tuple[dict[str, object], ...],
    correction_summary: DatasetCorrectionSummary,
) -> dict[str, object]:
    return {
        "adjustment": first.adjustment,
        "correction_summary": correction_summary.payload(),
        "dataset_identity_version": 1,
        "experiment_hash": request.experiment.content_hash,
        "experiment_id": request.experiment.experiment_id,
        "experiment_version": request.experiment.experiment_version,
        "feed": first.feed,
        "gap_summary": request.gap_summary.payload(),
        "provider": first.provider,
        "range_end_utc": request.range_end_utc,
        "range_start_utc": request.range_start_utc,
        "roles": roles,
        "row_count": len(request.effective_bars),
        "row_counts": row_counts,
        "schema_version": 1,
        "source_mode": first.source_mode,
        "symbols": request.symbols,
        "timeframe": first.timeframe,
    }


def _logical_dataset_hash(
    header: dict[str, object],
    rows: tuple[EffectiveBar, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(_DATASET_IDENTITY_DOMAIN)
    _update_framed_hash(digest, canonical_json_bytes(header))
    for item in rows:
        _update_framed_hash(digest, canonical_json_bytes(_logical_row(item)))
    return digest.hexdigest()


def _update_framed_hash(digest: Any, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, byteorder="big", signed=False))
    digest.update(payload)


def _logical_row(item: EffectiveBar) -> dict[str, object]:
    return {
        "bar": item.bar.canonical_payload(),
        "bar_event_id": item.bar_event_id,
        "revision": item.revision,
    }


def _parquet_row(item: EffectiveBar) -> dict[str, object]:
    bar = item.bar
    return {
        "bar_event_id": item.bar_event_id,
        "revision": item.revision,
        "provider": bar.provider,
        "feed": bar.feed,
        "adjustment": bar.adjustment,
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "source_mode": bar.source_mode,
        "interval_start_utc": bar.interval_start_utc,
        "interval_end_utc": bar.interval_end_utc,
        "receipt_timestamp_utc": bar.receipt_timestamp_utc,
        "provider_event_timestamp_utc": bar.provider_event_timestamp_utc,
        "open": _decimal_text(bar.open),
        "high": _decimal_text(bar.high),
        "low": _decimal_text(bar.low),
        "close": _decimal_text(bar.close),
        "volume": _decimal_text(bar.volume),
        "trade_count": bar.trade_count,
        "vwap": None if bar.vwap is None else _decimal_text(bar.vwap),
        "schema_version": bar.schema_version,
        "source_event_id": bar.source_event_id,
        "quality_flags": list(bar.quality_flags),
        "is_correction": bar.is_correction,
        "correction_of_source_event_id": bar.correction_of_source_event_id,
        "payload_hash": bar.payload_hash,
    }


def _write_parquet(rows: tuple[EffectiveBar, ...], stream: BinaryIO) -> None:
    table = pa.Table.from_pylist(
        [_parquet_row(item) for item in rows], schema=DATASET_PARQUET_SCHEMA
    )
    pq.write_table(
        table,
        stream,
        row_group_size=_ROW_GROUP_SIZE,
        version="2.6",
        use_dictionary=False,
        compression=_COMPRESSION,
        compression_level=_COMPRESSION_LEVEL,
        write_statistics=True,
        data_page_version="1.0",
        use_compliant_nested_type=True,
        write_batch_size=_WRITE_BATCH_SIZE,
        store_schema=True,
        write_page_index=False,
        write_page_checksum=True,
        store_decimal_as_integer=False,
    )


def _manifest_base(
    logical_header: dict[str, object],
    *,
    dataset_id: str,
    logical_hash: str,
    physical_hash: str,
    parquet_size_bytes: int,
    created_at: datetime,
    source_git_commit: str,
    dirty_worktree: bool,
    uv_lock_hash: str,
    promotable: bool,
    status: DatasetStatus,
) -> dict[str, object]:
    return {
        **logical_header,
        "artifact_id": dataset_id,
        "created_at": created_at,
        "dataset_id": dataset_id,
        "dirty_worktree": dirty_worktree,
        "logical_hash": logical_hash,
        "manifest_schema_version": 1,
        "parquet_encoding": _parquet_encoding_payload(),
        "parquet_size_bytes": parquet_size_bytes,
        "physical_hash": physical_hash,
        "promotable": promotable,
        "source_git_commit": source_git_commit,
        "status": status,
        "uv_lock_hash": uv_lock_hash,
    }


def _verified_result(
    stored: StoredDatasetArtifact,
    *,
    expected_header: dict[str, object],
    logical_hash: str,
    row_count: int,
    intrinsically_promotable: bool,
    expected_physical_hash: str,
    expected_parquet_size: int,
) -> FrozenDataset:
    if (
        stored.physical_hash != expected_physical_hash
        or stored.parquet_size_bytes != expected_parquet_size
    ):
        raise ArtifactIntegrityError("stored Parquet content does not match the logical dataset")
    document = _decode_manifest(stored.manifest_bytes)
    expected_id = f"dataset_{logical_hash}"
    expected_logical = _canonical_object(expected_header)
    supplied_manifest_hash = document.get("manifest_hash")
    supplied_created_at = document.get("created_at")
    if type(supplied_manifest_hash) is not str:
        raise ArtifactIntegrityError("stored manifest hash is invalid")
    _require_hash(supplied_manifest_hash, field_name="manifest hash")
    created_at = _parse_utc_text(supplied_created_at)
    content = {key: value for key, value in document.items() if key != "manifest_hash"}
    if sha256_hex(content) != supplied_manifest_hash:
        raise ArtifactIntegrityError("stored manifest hash does not match its content")
    expected_keys = frozenset(expected_logical) | {
        "artifact_id",
        "created_at",
        "dataset_id",
        "dirty_worktree",
        "logical_hash",
        "manifest_schema_version",
        "parquet_encoding",
        "parquet_size_bytes",
        "physical_hash",
        "promotable",
        "source_git_commit",
        "status",
        "uv_lock_hash",
    }
    if frozenset(content) != expected_keys or any(
        content.get(key) != value for key, value in expected_logical.items()
    ):
        raise ArtifactIntegrityError("stored manifest does not describe the logical dataset")
    _validate_manifest_envelope(
        content,
        expected_id=expected_id,
        expected_logical_hash=logical_hash,
        expected_physical_hash=expected_physical_hash,
        expected_parquet_size=expected_parquet_size,
        intrinsically_promotable=intrinsically_promotable,
    )
    stored_promotable = cast(bool, content["promotable"])
    stored_status = DatasetStatus(cast(str, content["status"]))
    return FrozenDataset(
        dataset_id=expected_id,
        artifact_id=stored.artifact_id,
        logical_hash=logical_hash,
        physical_hash=stored.physical_hash,
        manifest_hash=supplied_manifest_hash,
        parquet_size_bytes=stored.parquet_size_bytes,
        row_count=row_count,
        promotable=stored_promotable,
        status=stored_status,
        created_at=created_at,
        manifest_bytes=stored.manifest_bytes,
        created=stored.created,
    )


def _validate_manifest_envelope(
    content: dict[str, Any],
    *,
    expected_id: str,
    expected_logical_hash: str,
    expected_physical_hash: str,
    expected_parquet_size: int,
    intrinsically_promotable: bool,
) -> None:
    if (
        content.get("artifact_id") != expected_id
        or content.get("dataset_id") != expected_id
        or content.get("logical_hash") != expected_logical_hash
        or content.get("physical_hash") != expected_physical_hash
        or type(content.get("parquet_size_bytes")) is not int
        or content.get("parquet_size_bytes") != expected_parquet_size
        or type(content.get("manifest_schema_version")) is not int
        or content.get("manifest_schema_version") != 1
        or content.get("parquet_encoding") != _parquet_encoding_payload()
    ):
        raise ArtifactIntegrityError("stored manifest envelope is invalid")
    source_git_commit = content.get("source_git_commit")
    uv_lock_hash = content.get("uv_lock_hash")
    dirty_worktree = content.get("dirty_worktree")
    promotable = content.get("promotable")
    status_value = content.get("status")
    if (
        type(source_git_commit) is not str
        or _GIT_COMMIT_PATTERN.fullmatch(source_git_commit) is None
        or type(uv_lock_hash) is not str
        or _HASH_PATTERN.fullmatch(uv_lock_hash) is None
        or type(dirty_worktree) is not bool
        or type(promotable) is not bool
        or type(status_value) is not str
    ):
        raise ArtifactIntegrityError("stored manifest provenance is invalid")
    try:
        status = DatasetStatus(status_value)
    except ValueError:
        raise ArtifactIntegrityError("stored manifest promotion status is invalid") from None
    expected_promotable = intrinsically_promotable and not dirty_worktree
    if promotable != expected_promotable or promotable != (status is DatasetStatus.PROMOTABLE):
        raise ArtifactIntegrityError("stored manifest promotion status is invalid")


def _parquet_encoding_payload() -> dict[str, object]:
    return {
        "compression": _COMPRESSION,
        "compression_level": _COMPRESSION_LEVEL,
        "data_page_version": "1.0",
        "dictionary_encoding": False,
        "row_group_size": _ROW_GROUP_SIZE,
        "write_batch_size": _WRITE_BATCH_SIZE,
        "write_page_checksum": True,
    }


def _decode_manifest(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > _MANIFEST_MAX_BYTES:
        raise ArtifactIntegrityError("stored manifest bytes are invalid")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        decoded = json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError):
        raise ArtifactIntegrityError("stored manifest is not canonical JSON") from None
    if type(decoded) is not dict:
        raise ArtifactIntegrityError("stored manifest root is invalid")
    document = cast(dict[str, Any], decoded)
    try:
        canonical = canonical_json_bytes(document)
    except (CanonicalizationError, DomainValidationError):
        raise ArtifactIntegrityError("stored manifest content is invalid") from None
    if canonical != payload:
        raise ArtifactIntegrityError("stored manifest is not canonically encoded")
    return document


def _canonical_object(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(canonical_json_bytes(value)))


def _parse_utc_text(value: object) -> datetime:
    if type(value) is not str or _UTC_TEXT_PATTERN.fullmatch(value) is None:
        raise ArtifactIntegrityError("stored manifest creation timestamp is invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise ArtifactIntegrityError("stored manifest creation timestamp is invalid") from None


def _hash_stream(stream: BinaryIO) -> tuple[int, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        if type(chunk) is not bytes:
            raise ArtifactIntegrityError("artifact stream did not return bytes")
        size += len(chunk)
        if size > MAX_SIGNED_64_BIT_INTEGER:
            raise ArtifactIntegrityError("artifact exceeds the supported size")
        digest.update(chunk)
    stream.seek(0)
    return size, digest.hexdigest()


def _publish_staged_artifact(
    bucket_fd: int,
    artifact_id: str,
    *,
    parquet_stream: BinaryIO,
    expected_hash: str,
    expected_size: int,
    manifest_bytes: bytes,
) -> StoredDatasetArtifact:
    stage_name = f".stage-{secrets.token_hex(16)}"
    stage_fd = -1
    try:
        os.mkdir(stage_name, mode=_DIRECTORY_MODE, dir_fd=bucket_fd)
        stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=bucket_fd)
        actual_size, actual_hash = _write_stream_file(
            stage_fd,
            _PARQUET_NAME,
            parquet_stream,
        )
        if actual_size != expected_size or actual_hash != expected_hash:
            raise ArtifactIntegrityError("staged Parquet content changed during publication")
        _write_bytes_file(stage_fd, _MANIFEST_NAME, manifest_bytes)
        os.fsync(stage_fd)
        try:
            os.stat(artifact_id, dir_fd=bucket_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ArtifactConflictError("artifact ID became occupied during publication")
        os.rename(stage_name, artifact_id, src_dir_fd=bucket_fd, dst_dir_fd=bucket_fd)
        os.fsync(bucket_fd)
        return StoredDatasetArtifact(
            artifact_id=artifact_id,
            physical_hash=actual_hash,
            parquet_size_bytes=actual_size,
            manifest_bytes=manifest_bytes,
            created=True,
        )
    except OSError as exc:
        raise ArtifactSecurityError("artifact could not be published safely") from exc
    finally:
        if stage_fd >= 0:
            os.close(stage_fd)
        _remove_staging_directory(bucket_fd, stage_name)


def _write_stream_file(directory_fd: int, name: str, source: BinaryIO) -> tuple[int, str]:
    descriptor = os.open(name, _WRITE_FILE_FLAGS, _FILE_MODE, dir_fd=directory_fd)
    digest = hashlib.sha256()
    size = 0
    try:
        source.seek(0)
        with os.fdopen(descriptor, mode="wb", closefd=False) as destination:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if type(chunk) is not bytes:
                    raise ArtifactIntegrityError("artifact stream did not return bytes")
                size += len(chunk)
                if size > MAX_SIGNED_64_BIT_INTEGER:
                    raise ArtifactIntegrityError("artifact exceeds the supported size")
                destination.write(chunk)
                digest.update(chunk)
            destination.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return size, digest.hexdigest()


def _write_bytes_file(directory_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(name, _WRITE_FILE_FLAGS, _FILE_MODE, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, mode="wb", closefd=False) as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_staging_directory(bucket_fd: int, stage_name: str) -> None:
    try:
        stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=bucket_fd)
    except FileNotFoundError:
        return
    try:
        for name in (_PARQUET_NAME, _MANIFEST_NAME):
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=stage_fd)
    finally:
        os.close(stage_fd)
    with suppress(FileNotFoundError):
        os.rmdir(stage_name, dir_fd=bucket_fd)


def _load_artifact(bucket_fd: int, artifact_id: str) -> StoredDatasetArtifact | None:
    try:
        artifact_status = os.stat(artifact_id, dir_fd=bucket_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(artifact_status.st_mode):
        raise ArtifactSecurityError("artifact path is not a regular directory")
    try:
        artifact_fd = os.open(artifact_id, _DIRECTORY_FLAGS, dir_fd=bucket_fd)
    except OSError as exc:
        raise ArtifactSecurityError("artifact directory could not be opened safely") from exc
    try:
        try:
            members = frozenset(os.listdir(artifact_fd))
        except OSError as exc:
            raise ArtifactSecurityError("artifact members could not be inspected safely") from exc
        if members != _EXPECTED_ARTIFACT_MEMBERS:
            raise ArtifactIntegrityError("artifact has incomplete or unexpected members")
        parquet_size, physical_hash = _hash_regular_file(artifact_fd, _PARQUET_NAME)
        manifest_bytes = _read_regular_file(
            artifact_fd,
            _MANIFEST_NAME,
            maximum_bytes=_MANIFEST_MAX_BYTES,
        )
        if not manifest_bytes:
            raise ArtifactIntegrityError("artifact manifest is empty")
        return StoredDatasetArtifact(
            artifact_id=artifact_id,
            physical_hash=physical_hash,
            parquet_size_bytes=parquet_size,
            manifest_bytes=manifest_bytes,
            created=False,
        )
    finally:
        os.close(artifact_fd)


def _hash_regular_file(directory_fd: int, name: str) -> tuple[int, str]:
    descriptor = _open_regular_file(directory_fd, name)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SIGNED_64_BIT_INTEGER:
                raise ArtifactIntegrityError("artifact exceeds the supported size")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return size, digest.hexdigest()


def _read_regular_file(directory_fd: int, name: str, *, maximum_bytes: int) -> bytes:
    descriptor = _open_regular_file(directory_fd, name)
    try:
        size = os.fstat(descriptor).st_size
        if not 0 <= size <= maximum_bytes:
            raise ArtifactIntegrityError("artifact member exceeds its size limit")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _COPY_CHUNK_BYTES))
            if not chunk:
                raise ArtifactIntegrityError("artifact member was truncated during verification")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArtifactIntegrityError("artifact member changed during verification")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_regular_file(directory_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _READ_FILE_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise ArtifactSecurityError("artifact member could not be opened safely") from exc
    file_status = os.fstat(descriptor)
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
        os.close(descriptor)
        raise ArtifactSecurityError("artifact member is not a private regular file")
    return descriptor


def _open_bucket(root_fd: int, artifact_id: str, *, create: bool) -> int | None:
    bucket = hashlib.sha256(artifact_id.encode("ascii")).hexdigest()[:2]
    if create:
        try:
            os.mkdir(bucket, mode=_DIRECTORY_MODE, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ArtifactSecurityError("artifact bucket could not be created safely") from exc
    try:
        return os.open(bucket, _DIRECTORY_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactSecurityError("artifact bucket could not be opened safely") from exc


def _open_or_create_directory_chain(root: Path) -> int:
    descriptor = os.open(root.anchor, _DIRECTORY_FLAGS)
    try:
        for component in root.parts[1:]:
            try:
                os.mkdir(component, mode=_DIRECTORY_MODE, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise ArtifactSecurityError(
                    "artifact root contains an unsafe path component"
                ) from exc
            os.close(descriptor)
            descriptor = child
        os.set_inheritable(descriptor, False)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_artifact_id(value: object) -> str:
    if (
        type(value) is not str
        or ".." in value
        or "\x00" in value
        or _ARTIFACT_ID_PATTERN.fullmatch(value) is None
    ):
        raise ArtifactSecurityError("artifact ID is invalid")
    return value


def _require_hash(value: object, *, field_name: str) -> str:
    if type(value) is not str or _HASH_PATTERN.fullmatch(value) is None:
        raise DatasetValidationError(f"dataset {field_name} is invalid")
    return value


def _require_utc(value: object, *, field_name: str) -> datetime:
    try:
        return require_utc_instant(value, field_name=field_name)
    except DomainValidationError:
        raise DatasetValidationError(f"dataset {field_name} must be UTC") from None


def _decimal_text(value: Any) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


__all__ = [
    "DATASET_PARQUET_SCHEMA",
    "ArtifactConflictError",
    "ArtifactIntegrityError",
    "ArtifactSecurityError",
    "ArtifactStore",
    "DatasetCorrectionSummary",
    "DatasetError",
    "DatasetFreezeRequest",
    "DatasetGapSummary",
    "DatasetStatus",
    "DatasetValidationError",
    "FrozenDataset",
    "LocalFilesystemArtifactStore",
    "StoredDatasetArtifact",
    "freeze_dataset",
]
