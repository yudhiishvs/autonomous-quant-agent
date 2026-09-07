"""Authoritative, bounded handlers for the closed durable-job contract."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, cast

from adaptive_trader.platform.canonical import JsonValue
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.control.queries import ControlQueryPort, ReadResource
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.datasets import (
    DatasetFreezeRequest,
    LocalFilesystemArtifactStore,
)
from adaptive_trader.platform.data.watermarks import (
    DataGap,
    GapRepairCoverage,
    GapRepository,
    GapStatus,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.jobs.artifacts import ImmutableJobArtifactStore
from adaptive_trader.platform.jobs.models import JobRecord, JobType, SafeJobError
from adaptive_trader.platform.jobs.worker import (
    BoundedJobHandlers,
    JobExecutionError,
    JobLeaseGuard,
    routing_value,
)
from adaptive_trader.platform.storage.datasets import (
    DatasetManifestRepository,
    freeze_and_register_dataset,
)
from adaptive_trader.platform.storage.repositories import AuditRepository

_PAGE_SIZE = 100
_MAX_AUDIT_PAGES = 100


class DatasetRequestProvider(Protocol):
    """Return a trusted freeze request after re-reading authoritative state."""

    def __call__(self, *, experiment_hash: str) -> DatasetFreezeRequest: ...


class DemoEvidenceProvider(Protocol):
    """Return one already cross-run-verified offline evidence manifest."""

    def __call__(self) -> dict[str, JsonValue]: ...


GapRepairer = Callable[[DataGap], GapRepairCoverage]


class PlatformJobHandlerSet:
    """Build all four handlers from explicit, capability-scoped dependencies.

    A control-only deployment deliberately omits collector-authority dependencies. In that
    deployment gap-repair and dataset-freeze jobs fail closed instead of borrowing broader
    database or provider authority.
    """

    def __init__(
        self,
        *,
        experiment: ExperimentDefinition,
        audit: AuditRepository,
        queries: ControlQueryPort,
        artifacts: ImmutableJobArtifactStore,
        clock: Callable[[], datetime],
        demo_evidence_provider: DemoEvidenceProvider,
        gap_repository: GapRepository | None = None,
        gap_repairer: GapRepairer | None = None,
        dataset_repository: DatasetManifestRepository | None = None,
        dataset_request_provider: DatasetRequestProvider | None = None,
        dataset_store: LocalFilesystemArtifactStore | None = None,
    ) -> None:
        if type(experiment) is not ExperimentDefinition:
            raise TypeError("job handlers require an experiment definition")
        if not isinstance(audit, AuditRepository):
            raise TypeError("job handlers require an audit repository")
        if not callable(getattr(queries, "page", None)):
            raise TypeError("job handlers require the safe query boundary")
        if not isinstance(artifacts, ImmutableJobArtifactStore):
            raise TypeError("job handlers require an immutable artifact store")
        if not callable(clock) or not callable(demo_evidence_provider):
            raise TypeError("job handlers require injected clocks and demo evidence")
        if (gap_repository is None) != (gap_repairer is None):
            raise TypeError("gap repair authority must be supplied as one capability")
        dataset_capabilities = (
            dataset_repository,
            dataset_request_provider,
            dataset_store,
        )
        if any(item is not None for item in dataset_capabilities) and any(
            item is None for item in dataset_capabilities
        ):
            raise TypeError("dataset freeze authority must be supplied as one capability")
        self._experiment = experiment
        self._audit = audit
        self._queries = queries
        self._artifacts = artifacts
        self._clock = clock
        self._demo_evidence_provider = demo_evidence_provider
        self._gap_repository = gap_repository
        self._gap_repairer = gap_repairer
        self._dataset_repository = dataset_repository
        self._dataset_request_provider = dataset_request_provider
        self._dataset_store = dataset_store

    def bounded(self) -> BoundedJobHandlers:
        return BoundedJobHandlers(
            data_quality_audit=self.data_quality_audit,
            gap_repair=self.gap_repair,
            dataset_freeze=self.dataset_freeze,
            offline_demo=self.offline_demo,
        )

    def data_quality_audit(self, *, job: JobRecord, lease: JobLeaseGuard) -> str:
        self._require_type(job, JobType.DATA_QUALITY_AUDIT)
        self._require_lease(lease)
        experiment_hash = routing_value(job.payload, "experiment_hash")
        self._require_active_experiment(experiment_hash)
        lease.checkpoint()
        report = self._audit.verify()
        gaps = self._unresolved_gap_ids(experiment_hash, lease=lease)
        payload: dict[str, JsonValue] = {
            "audit_event_count": report.event_count,
            "audit_stream_heads": [
                {
                    "event_hash": head.event_hash,
                    "sequence": head.sequence,
                    "stream_id": head.stream_id,
                }
                for head in report.stream_heads
            ],
            "experiment_hash": experiment_hash,
            "job_id": job.job_id,
            "schema": "data-quality-audit-v1",
            "unresolved_gap_count": len(gaps),
            "unresolved_gap_ids": list(gaps),
        }
        lease.checkpoint()
        return self._artifacts.publish_json(prefix="data-quality", payload=payload)

    def gap_repair(self, *, job: JobRecord, lease: JobLeaseGuard) -> None:
        self._require_type(job, JobType.GAP_REPAIR)
        self._require_lease(lease)
        gap_id = routing_value(job.payload, "gap_id")
        repository = self._gap_repository
        repairer = self._gap_repairer
        if repository is None or repairer is None:
            raise JobExecutionError(
                SafeJobError(
                    "repair_unavailable",
                    "Gap repair authority is unavailable in this service",
                )
            )
        lease.checkpoint()
        gap = repository.get(gap_id)
        if gap is None:
            raise JobExecutionError(SafeJobError("gap_not_found", "Requested gap does not exist"))
        self._require_active_experiment(gap.experiment_hash)
        if gap.status is GapStatus.RESOLVED:
            return
        now = self._clock()
        if gap.status is GapStatus.REPAIRING:
            lease.checkpoint()
            gap = repository.reopen_interrupted(gap.gap_id, reopened_at=now)
        lease.checkpoint()
        claimed = repository.begin_repair(gap.gap_id, attempted_at=now)
        try:
            lease.checkpoint()
            coverage = repairer(claimed)
        except Exception:
            lease.checkpoint()
            repository.reopen_interrupted(claimed.gap_id, reopened_at=self._clock())
            raise JobExecutionError(
                SafeJobError("repair_failed", "Gap repair failed without trusted coverage")
            ) from None
        lease.checkpoint()
        completed = repository.complete_repair(claimed.gap_id, coverage=coverage)
        if completed.status is not GapStatus.RESOLVED:
            raise JobExecutionError(
                SafeJobError("repair_incomplete", "Gap repair did not provide complete coverage")
            )

    def dataset_freeze(self, *, job: JobRecord, lease: JobLeaseGuard) -> str:
        self._require_type(job, JobType.DATASET_FREEZE)
        self._require_lease(lease)
        experiment_hash = routing_value(job.payload, "experiment_hash")
        self._require_active_experiment(experiment_hash)
        repository = self._dataset_repository
        provider = self._dataset_request_provider
        store = self._dataset_store
        if repository is None or provider is None or store is None:
            raise JobExecutionError(
                SafeJobError(
                    "dataset_source_unavailable",
                    "Dataset freeze input is unavailable in this service",
                )
            )
        lease.checkpoint()
        request = provider(experiment_hash=experiment_hash)
        if type(request) is not DatasetFreezeRequest or (
            request.experiment.content_hash != experiment_hash
        ):
            raise JobExecutionError(
                SafeJobError("dataset_source_invalid", "Dataset freeze input is not authoritative")
            )
        lease.checkpoint()
        frozen, registration = freeze_and_register_dataset(
            request,
            store=store,
            repository=repository,
            calendar=XnasExchangeCalendar(),
        )
        lease.checkpoint()
        if registration.artifact_id != frozen.artifact_id:
            raise JobExecutionError(
                SafeJobError("dataset_registration_invalid", "Dataset registration is invalid")
            )
        return registration.artifact_id

    def offline_demo(self, *, job: JobRecord, lease: JobLeaseGuard) -> str:
        self._require_type(job, JobType.OFFLINE_DEMO)
        self._require_lease(lease)
        demo_id = routing_value(job.payload, "demo_id")
        lease.checkpoint()
        manifest = self._demo_evidence_provider()
        lease.checkpoint()
        if type(manifest) is not dict or not _valid_demo_manifest(manifest):
            raise JobExecutionError(
                SafeJobError("demo_evidence_invalid", "Offline demo evidence is invalid")
            )
        payload = cast(dict[str, JsonValue], dict(manifest))
        payload["demo_id"] = demo_id
        payload["job_id"] = job.job_id
        payload["schema"] = "offline-demo-job-evidence-v1"
        lease.checkpoint()
        return self._artifacts.publish_json(prefix="offline-demo", payload=payload)

    def _unresolved_gap_ids(
        self,
        experiment_hash: str,
        *,
        lease: JobLeaseGuard,
    ) -> tuple[str, ...]:
        gap_ids: list[str] = []
        for page_number in range(_MAX_AUDIT_PAGES):
            lease.checkpoint()
            page = self._queries.page(
                ReadResource.DATA_GAPS,
                limit=_PAGE_SIZE,
                offset=page_number * _PAGE_SIZE,
            )
            for item in page.items:
                if item.get("experiment_hash") != experiment_hash:
                    continue
                if item.get("status") not in {"open", "repairing"}:
                    continue
                gap_id = item.get("gap_id")
                if type(gap_id) is not str:
                    raise JobExecutionError(
                        SafeJobError("quality_state_invalid", "Data quality state is invalid")
                    )
                gap_ids.append(gap_id)
            if page.count < _PAGE_SIZE:
                return tuple(sorted(gap_ids))
        raise JobExecutionError(
            SafeJobError("quality_scan_bounded", "Data quality state exceeds the audit bound")
        )

    def _require_active_experiment(self, experiment_hash: str) -> None:
        if experiment_hash != self._experiment.content_hash:
            raise JobExecutionError(
                SafeJobError("experiment_mismatch", "Job does not target the active experiment")
            )

    @staticmethod
    def _require_type(job: JobRecord, expected: JobType) -> None:
        if type(job) is not JobRecord or job.job_type is not expected:
            raise JobExecutionError(
                SafeJobError("invalid_job_type", "Job handler received an invalid job type")
            )

    @staticmethod
    def _require_lease(lease: JobLeaseGuard) -> None:
        if not isinstance(lease, JobLeaseGuard):
            raise JobExecutionError(
                SafeJobError("invalid_job_lease", "Job handler received an invalid lease")
            )


def _valid_demo_manifest(manifest: dict[str, JsonValue]) -> bool:
    expected_hash = manifest.get("evidence_manifest_hash")
    if (
        type(expected_hash) is not str
        or len(expected_hash) != 64
        or manifest.get("evidence_label") != "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE"
        or manifest.get("schema") != "offline-demo-evidence-v1"
    ):
        return False
    unsigned = {key: value for key, value in manifest.items() if key != "evidence_manifest_hash"}
    return sha256_hex(unsigned) == expected_hash


__all__ = [
    "DatasetRequestProvider",
    "DemoEvidenceProvider",
    "GapRepairer",
    "PlatformJobHandlerSet",
]
