"""Untrusted manifests and execution bundles cannot acquire durable authority."""

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.data.datasets import (
    DatasetValidationError,
    LocalFilesystemArtifactStore,
    freeze_dataset,
)
from adaptive_trader.platform.execution.models import ExecutionValidationError
from adaptive_trader.platform.execution.planner import plan_signed_orders
from adaptive_trader.platform.execution.repository import MemoryExecutionRepository
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.datasets import DatasetManifestRepository
from adaptive_trader.platform.storage.tables import aqa_dataset_manifests
from tests.unit.test_platform_datasets import _request, dataset_engine, experiment
from tests.unit.test_platform_execution_planner import planning_request

__all__ = ["dataset_engine", "experiment"]


@pytest.fixture
def frozen(tmp_path, experiment):
    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "artifacts"
    ) as store:
        return freeze_dataset(_request(experiment), store=store)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "unrecognized"},
        {"manifest_schema_version": True},
        {"dataset_identity_version": 9},
        {"source_git_commit": "../untrusted"},
        {"experiment_hash": "A" * 64},
        {"uv_lock_hash": "bad"},
        {"created_at": "not-an-instant"},
        {"range_start_utc": "not-an-instant"},
        {"schema_version": True},
        {"schema_version": 0},
        {"dirty_worktree": 1},
        {"provider": ""},
        {"feed": None},
        {"adjustment": False},
        {"timeframe": 1},
        {"roles": {}},
        {"symbols": "AMD"},
        {"row_counts": {}},
        {"gap_summary": []},
        {"correction_summary": []},
    ],
)
def test_rehashed_malformed_manifest_never_registers(dataset_engine, frozen, change):
    document = {**frozen.manifest, **change}
    document.pop("manifest_hash")
    digest = sha256_hex(document)
    document["manifest_hash"] = digest
    corrupted = replace(frozen, manifest_hash=digest, manifest_bytes=canonical_json_bytes(document))
    repository = DatasetManifestRepository(dataset_engine)
    with pytest.raises(DatasetValidationError):
        repository.register(corrupted)
    with dataset_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(aqa_dataset_manifests)) == 0
    assert repository.register(frozen).created
    assert not repository.register(frozen).created


@pytest.mark.parametrize(
    "attack", ["missing_intents", "mutable_intents", "wrong_risk", "unknown_intent"]
)
def test_execution_bundle_rejection_is_atomic_and_valid_retry_is_idempotent(attack):
    request = planning_request(
        current=(("AAA", Decimal(0)),), target_weights=(("AAA", Decimal("0.1")),)
    )
    bundle = plan_signed_orders(request)
    repository = MemoryExecutionRepository()
    original = repository.export_state()
    intents = {
        "missing_intents": (),
        "mutable_intents": list(bundle.intents),
        "wrong_risk": bundle.intents,
        "unknown_intent": (object(),),
    }[attack]
    with pytest.raises(ExecutionValidationError):
        repository.persist_plan_and_intents(
            bundle.plan,
            intents,
            risk_decision=object() if attack == "wrong_risk" else request.risk_decision,
        )
    assert repository.export_state() == original
    repository.persist_plan_and_intents(
        bundle.plan, bundle.intents, risk_decision=request.risk_decision
    )
    persisted = repository.export_state()
    repository.persist_plan_and_intents(
        bundle.plan, bundle.intents, risk_decision=request.risk_decision
    )
    assert repository.export_state() == persisted
    assert len(persisted.plans) == 1
    assert len(persisted.intents) == 1
