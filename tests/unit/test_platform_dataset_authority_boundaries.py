"""Dataset publication requires causal, typed, internally consistent provenance."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adaptive_trader.platform.data.datasets import (
    CollectionDatasetProvenance,
    DatasetValidationError,
    SnapshotMetadataEvidence,
)
from tests.unit.test_platform_datasets import _CREATED, _START, _request, experiment

__all__ = ["experiment"]


def evidence():
    return SnapshotMetadataEvidence(
        source="metadata",
        source_document_sha256="a" * 64,
        symbols=("AMD",),
        listing_status="active",
        corporate_action_status="clear",
        observed_at=_CREATED,
        range_start_utc=_START,
        range_end_utc=_START + timedelta(minutes=1),
    )


def provenance():
    return CollectionDatasetProvenance(
        universe_version="1",
        universe_hash="a" * 64,
        members=(("AMD", "collected_equity", "active"),),
        calendar_name="XNAS",
        calendar_version="1",
        history_start_utc=_START,
        pending_derived_sessions=0,
        persisted_gap_state_hash="b" * 64,
        unresolved_persisted_gaps=0,
        lagging_checkpoint_symbols=(),
        metadata_evidence=evidence(),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"symbols": "AMD"},
        {"symbols": [1]},
        {"source": 1},
        {"listing_status": None},
        {"observed_at": 1},
        {"observed_at": "invalid"},
        {"range_start_utc": "2026-07-06T13:30:00"},
    ],
)
def test_metadata_payload_cannot_coerce_provenance(change):
    payload = evidence().payload()
    with pytest.raises(DatasetValidationError):
        SnapshotMetadataEvidence.from_payload({**payload, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"source": "../metadata"},
        {"observed_at": _START},
        {"range_end_utc": _START},
        {"symbols": ("amd",)},
        {"symbols": ("AMD", "AMD")},
        {"listing_status": "approved"},
        {"corporate_action_status": "safe"},
    ],
)
def test_metadata_claim_must_match_causal_coverage_and_closed_status(change):
    with pytest.raises(DatasetValidationError):
        replace(evidence(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"universe_version": "../1"},
        {"calendar_name": ""},
        {"members": ()},
        {"members": (("AMD", "unknown", "active"),)},
        {"members": (("AMD", "collected_equity", "active"), ("AMD", "collected_equity", "active"))},
        {"pending_derived_sessions": True},
        {"pending_derived_sessions": -1},
        {"pending_derived_sessions": 2**63},
        {"unresolved_persisted_gaps": True},
        {"unresolved_persisted_gaps": -1},
        {"unresolved_persisted_gaps": 2**63},
        {"lagging_checkpoint_symbols": ("QQQ",)},
        {"lagging_checkpoint_symbols": ("AMD", "AMD")},
        {"metadata_evidence": {}},
    ],
)
def test_collection_authority_cannot_hide_unknown_members_or_unbounded_work(change):
    with pytest.raises(DatasetValidationError):
        replace(provenance(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"symbols": []},
        {"symbols": ("NVDA", "AMD")},
        {"symbols": ("QQQQ",)},
        {"effective_bars": []},
        {"effective_bars": (object(),)},
        {"gap_summary": {}},
        {"range_end_utc": _START},
        {"source_git_commit": "branch-name"},
        {"dirty_worktree": 1},
        {"diagnostic_only": 0},
        {"collection_provenance": {}},
    ],
)
def test_freeze_request_rejects_unvalidated_inputs_before_artifact_publication(experiment, change):
    with pytest.raises(DatasetValidationError):
        replace(_request(experiment), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"calendar_name": "XNYS"},
        {"history_start_utc": _START + timedelta(minutes=1)},
        {"members": (("NVDA", "collected_equity", "active"),), "metadata_evidence": None},
    ],
)
def test_valid_individual_provenance_cannot_be_attached_to_different_dataset(experiment, change):
    changed = replace(provenance(), **change)
    with pytest.raises(DatasetValidationError):
        replace(_request(experiment), collection_provenance=changed)


@pytest.fixture
def frozen_dataset(tmp_path, experiment):
    from adaptive_trader.platform.data.datasets import LocalFilesystemArtifactStore, freeze_dataset

    with LocalFilesystemArtifactStore(
        trusted_artifact_root=tmp_path.resolve() / "artifacts"
    ) as store:
        return freeze_dataset(_request(experiment), store=store)


@pytest.mark.parametrize(
    "change",
    [
        {"dataset_id": "dataset_" + "f" * 64},
        {"artifact_id": "dataset_" + "f" * 64},
        {"row_count": 0},
        {"row_count": True},
        {"row_count": 2**63},
        {"promotable": 1},
        {"status": "promotable"},
        {"promotable": False},
        {"parquet_size_bytes": -1},
        {"parquet_size_bytes": True},
        {"manifest_bytes": b""},
        {"manifest_bytes": "{}"},
        {"created": 1},
    ],
)
def test_frozen_artifact_identity_and_size_cannot_be_relabelled(frozen_dataset, change):
    from adaptive_trader.platform.data.datasets import ArtifactIntegrityError

    with pytest.raises(ArtifactIntegrityError):
        replace(frozen_dataset, **change)
