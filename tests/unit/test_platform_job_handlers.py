"""Real bounded handler tests for every public durable-job type."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, insert, select

from adaptive_trader.platform.config import ExperimentDefinition, load_experiment
from adaptive_trader.platform.control.models import SafePageResponse
from adaptive_trader.platform.control.queries import ReadResource
from adaptive_trader.platform.data.aggregation import EffectiveBar
from adaptive_trader.platform.data.calendar import XnasExchangeCalendar
from adaptive_trader.platform.data.datasets import (
    DatasetFreezeRequest,
    DatasetGapSummary,
    LocalFilesystemArtifactStore,
)
from adaptive_trader.platform.data.normalization import CanonicalBar
from adaptive_trader.platform.data.watermarks import (
    DataGap,
    DataSeries,
    GapDetection,
    GapRepairCoverage,
    GapRepository,
    GapStatus,
)
from adaptive_trader.platform.domain import AuditWriter
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.jobs import (
    JobCreateRequest,
    JobExecutionError,
    JobLeaseGuard,
    JobPayload,
    JobRecord,
    JobRepository,
)
from adaptive_trader.platform.jobs.artifacts import (
    ImmutableJobArtifactStore,
    JobArtifactError,
)
from adaptive_trader.platform.jobs.handlers import PlatformJobHandlerSet
from adaptive_trader.platform.storage.datasets import DatasetManifestRepository
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_dataset_manifests,
    aqa_experiments,
    metadata,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_START = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
_NOW = datetime(2026, 9, 5, 16, 0, tzinfo=UTC)
_CORRELATION_ID = "0198fa2d-7b8c-7123-8abc-0123456789ab"


class _SafeQueries:
    def __init__(self, items: tuple[dict[str, Any], ...] = ()) -> None:
        self._items = items

    def page(self, resource: ReadResource, *, limit: int, offset: int) -> SafePageResponse:
        assert resource is ReadResource.DATA_GAPS
        selected = self._items[offset : offset + limit]
        return SafePageResponse(
            items=selected,
            limit=limit,
            offset=offset,
            count=len(selected),
        )

    def ready(self) -> bool:
        return True


@pytest.fixture(scope="module")
def experiment() -> ExperimentDefinition:
    return load_experiment(
        Path("experiments/semiconductor_network_intraday_v1.yaml"),
        config_root=PROJECT_ROOT / "configs",
    )


@pytest.fixture
def platform_engine(tmp_path: Path, experiment: ExperimentDefinition) -> Engine:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'platform.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={PLATFORM_SCHEMA: None})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=experiment.content_hash,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.experiment_version,
                schema_version=experiment.schema_version,
                configuration=experiment.model_dump(mode="json"),
                content_hash=experiment.content_hash,
                registered_at=_NOW,
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _job_repository(engine: Engine) -> JobRepository:
    return JobRepository(
        engine,
        audit=AuditRepository(engine, writer=AuditWriter.CONTROL),
    )


def _running_job(repository: JobRepository, payload: JobPayload, *, key: str) -> JobRecord:
    created = repository.create(
        JobCreateRequest(
            payload=payload,
            idempotency_key=key,
            correlation_id=_CORRELATION_ID,
            requested_at=_NOW,
        )
    )
    claimed = repository.claim_next(owner="job-worker-test", now=_NOW)
    assert claimed is not None and claimed.job_id == created.job_id
    return repository.mark_running(
        job_id=created.job_id,
        owner="job-worker-test",
        attempt_number=claimed.attempt_count,
        now=_NOW,
    )


def _lease(repository: JobRepository, job: JobRecord) -> JobLeaseGuard:
    return JobLeaseGuard(
        repository,
        job=job,
        owner="job-worker-test",
        clock=lambda: _NOW,
    )


def _demo_manifest() -> dict[str, Any]:
    logical = {
        "evidence_label": "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE",
        "proof": "deterministic-fixture",
        "schema": "offline-demo-evidence-v1",
    }
    return {**logical, "evidence_manifest_hash": sha256_hex(logical)}


def _handlers(
    *,
    experiment: ExperimentDefinition,
    engine: Engine,
    artifact_root: Path,
    queries: _SafeQueries | None = None,
    **capabilities: object,
) -> PlatformJobHandlerSet:
    return PlatformJobHandlerSet(
        experiment=experiment,
        audit=AuditRepository(engine),
        queries=queries or _SafeQueries(),
        artifacts=ImmutableJobArtifactStore(artifact_root),
        clock=lambda: _NOW,
        demo_evidence_provider=_demo_manifest,
        **capabilities,
    )


def test_data_quality_handler_verifies_audit_and_publishes_gap_evidence(
    tmp_path: Path,
    platform_engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    repository = _job_repository(platform_engine)
    job = _running_job(
        repository,
        JobPayload.data_quality_audit(experiment_hash=experiment.content_hash),
        key="quality-1",
    )
    gap_id = f"gap_{'a' * 64}"
    artifacts = (tmp_path / "artifacts").resolve()
    handlers = _handlers(
        experiment=experiment,
        engine=platform_engine,
        artifact_root=artifacts,
        queries=_SafeQueries(
            (
                {
                    "experiment_hash": experiment.content_hash,
                    "gap_id": gap_id,
                    "status": "open",
                },
            )
        ),
    )

    artifact_id = handlers.data_quality_audit(job=job, lease=_lease(repository, job))

    evidence = (artifacts / "job-evidence" / f"{artifact_id}.json").read_text(encoding="utf-8")
    assert gap_id in evidence
    assert '"unresolved_gap_count":1' in evidence
    assert '"audit_event_count":3' in evidence


def test_gap_repair_handler_reopens_authoritative_gap_and_requires_exact_coverage(
    tmp_path: Path,
    platform_engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    calendar = XnasExchangeCalendar()
    gaps = GapRepository(platform_engine, calendar=calendar)
    series = DataSeries("fixture", "iex", "raw", experiment.active_tradable[0], "1Min")
    detection = GapDetection(
        experiment_hash=experiment.content_hash,
        series=series,
        start_at=_START,
        end_at=_START + timedelta(minutes=1),
        reason_code="missing_expected_bar",
        detected_at=_NOW - timedelta(minutes=1),
    )
    persisted = gaps.record(detection)
    repository = _job_repository(platform_engine)
    job = _running_job(repository, JobPayload.gap_repair(gap_id=persisted.gap_id), key="gap-1")

    def repairer(claimed: DataGap) -> GapRepairCoverage:
        visible = gaps.get(claimed.gap_id)
        assert visible is not None
        assert visible.status is GapStatus.REPAIRING
        return GapRepairCoverage(
            series=claimed.series,
            start_at=claimed.start_at,
            end_at=claimed.end_at,
            observed_intervals=calendar.expected_intervals(
                start_at=claimed.start_at,
                end_at=claimed.end_at,
                timeframe=claimed.series.timeframe,
            ),
            completed_at=_NOW + timedelta(seconds=1),
        )

    handlers = _handlers(
        experiment=experiment,
        engine=platform_engine,
        artifact_root=(tmp_path / "artifacts").resolve(),
        gap_repository=gaps,
        gap_repairer=repairer,
    )
    handlers.gap_repair(job=job, lease=_lease(repository, job))

    repaired = gaps.get(persisted.gap_id)
    assert repaired is not None
    assert repaired.status is GapStatus.RESOLVED
    assert repaired.attempt_count == 1


def test_collector_authority_jobs_fail_closed_when_capability_is_absent(
    tmp_path: Path,
    platform_engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    repository = _job_repository(platform_engine)
    gap_job = _running_job(
        repository,
        JobPayload.gap_repair(gap_id=f"gap_{'b' * 64}"),
        key="gap-unavailable",
    )
    handlers = _handlers(
        experiment=experiment,
        engine=platform_engine,
        artifact_root=(tmp_path / "artifacts").resolve(),
    )

    with pytest.raises(JobExecutionError) as gap_error:
        handlers.gap_repair(job=gap_job, lease=_lease(repository, gap_job))
    assert gap_error.value.error.code == "repair_unavailable"

    repository.fail(
        job_id=gap_job.job_id,
        owner="job-worker-test",
        attempt_number=gap_job.attempt_count,
        now=_NOW,
        error=gap_error.value.error,
    )
    freeze_job = _running_job(
        repository,
        JobPayload.dataset_freeze(experiment_hash=experiment.content_hash),
        key="freeze-unavailable",
    )
    with pytest.raises(JobExecutionError) as freeze_error:
        handlers.dataset_freeze(job=freeze_job, lease=_lease(repository, freeze_job))
    assert freeze_error.value.error.code == "dataset_source_unavailable"


def _dataset_request(experiment: ExperimentDefinition) -> DatasetFreezeRequest:
    canonical = CanonicalBar(
        provider="alpaca",
        feed="iex",
        adjustment="raw",
        symbol=experiment.active_tradable[0],
        timeframe="1Min",
        source_mode="external_provider",
        interval_start_utc=_START,
        interval_end_utc=_START + timedelta(minutes=1),
        receipt_timestamp_utc=_START + timedelta(minutes=1, seconds=1),
        provider_event_timestamp_utc=_START,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000"),
        trade_count=10,
        vwap=Decimal("100.4"),
        schema_version=1,
        source_event_id="alpaca_fixture_1",
        quality_flags=("complete",),
        is_correction=False,
        correction_of_source_event_id=None,
    )
    return DatasetFreezeRequest(
        experiment=experiment,
        symbols=(experiment.active_tradable[0],),
        effective_bars=(EffectiveBar(f"bar_event_{'c' * 64}", 1, canonical),),
        range_start_utc=_START,
        range_end_utc=_START + timedelta(minutes=1),
        gap_summary=DatasetGapSummary(1, 0, 0, 0),
        source_git_commit="d" * 40,
        dirty_worktree=False,
        uv_lock_hash="e" * 64,
        created_at=_NOW,
    )


def test_dataset_freeze_handler_uses_typed_source_and_registers_immutable_artifact(
    tmp_path: Path,
    platform_engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    repository = _job_repository(platform_engine)
    job = _running_job(
        repository,
        JobPayload.dataset_freeze(experiment_hash=experiment.content_hash),
        key="freeze-1",
    )
    root = (tmp_path / "artifacts").resolve()
    dataset_store = LocalFilesystemArtifactStore(trusted_artifact_root=root)
    try:
        handlers = _handlers(
            experiment=experiment,
            engine=platform_engine,
            artifact_root=root,
            dataset_repository=DatasetManifestRepository(platform_engine),
            dataset_request_provider=lambda *, experiment_hash: _dataset_request(experiment),
            dataset_store=dataset_store,
        )
        artifact_id = handlers.dataset_freeze(job=job, lease=_lease(repository, job))
    finally:
        dataset_store.close()

    assert artifact_id.startswith("dataset_")
    with platform_engine.connect() as connection:
        registered_id = connection.scalar(
            select(aqa_dataset_manifests.c.artifact_id).where(
                aqa_dataset_manifests.c.dataset_id == artifact_id
            )
        )
    assert registered_id == artifact_id


def test_offline_demo_handler_rejects_unverified_evidence_and_publishes_verified_result(
    tmp_path: Path,
    platform_engine: Engine,
    experiment: ExperimentDefinition,
) -> None:
    repository = _job_repository(platform_engine)
    job = _running_job(
        repository,
        JobPayload.offline_demo(demo_id="fixture-1"),
        key="demo-1",
    )
    root = (tmp_path / "artifacts").resolve()
    handlers = _handlers(
        experiment=experiment,
        engine=platform_engine,
        artifact_root=root,
    )
    artifact_id = handlers.offline_demo(job=job, lease=_lease(repository, job))

    evidence = (root / "job-evidence" / f"{artifact_id}.json").read_text(encoding="utf-8")
    assert '"demo_id":"fixture-1"' in evidence
    assert '"evidence_label":"OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE"' in evidence

    invalid = PlatformJobHandlerSet(
        experiment=experiment,
        audit=AuditRepository(platform_engine),
        queries=_SafeQueries(),
        artifacts=ImmutableJobArtifactStore(root),
        clock=lambda: _NOW,
        demo_evidence_provider=lambda: {"schema": "offline-demo-evidence-v1"},
    )
    with pytest.raises(JobExecutionError) as error:
        invalid.offline_demo(job=job, lease=_lease(repository, job))
    assert error.value.error.code == "demo_evidence_invalid"


def test_job_artifact_store_is_content_addressed_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ImmutableJobArtifactStore(root)
    artifact_id = store.publish_json(prefix="quality", payload={"schema": "evidence-v1"})
    assert store.publish_json(prefix="quality", payload={"schema": "evidence-v1"}) == artifact_id

    target = root / "job-evidence" / f"{artifact_id}.json"
    target.write_text("{}\n", encoding="utf-8")
    with pytest.raises(JobArtifactError, match="conflicts"):
        store.publish_json(prefix="quality", payload={"schema": "evidence-v1"})
