"""Offline vertical-slice determinism, recovery, and publication tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adaptive_trader.platform.cli import app
from adaptive_trader.platform.demo import (
    EVIDENCE_LABEL,
    DemoComparison,
    DemoError,
    publish_demo_evidence,
    run_demo_twice,
)


@pytest.fixture(scope="module")
def comparison() -> DemoComparison:
    return run_demo_twice(config_root=Path("configs"))


def test_two_fresh_runs_have_identical_complete_logical_evidence(
    comparison: DemoComparison,
) -> None:
    first = comparison.first
    second = comparison.second

    assert first == second
    assert first.manifest_hash == second.manifest_hash == comparison.manifest_hash
    assert len(first.canonical_event_hashes) == 121
    assert len(first.effective_event_hashes) == 120
    assert len(first.aggregate_hashes) == 8
    assert len(first.aggregate_revision_hashes) == 2
    assert len(first.watermark_values) == 9
    assert len(first.slot_ids) == 21
    assert len(first.signal_hashes) == len(first.risk_decision_hashes) == 1
    assert len(first.execution_plan_hashes) == 2
    assert len(first.client_order_ids) == 3
    assert len(first.fill_hashes) == 2
    assert len(first.reconciliation_hashes) == 3
    assert first.final_signed_positions == ()
    assert dict(first.final_account_values) == {
        "buying_power": "100000.00000000",
        "cash": "100000.00000000",
        "equity": "100000.00000000",
        "restricted_short_proceeds": "0",
    }


def test_demo_exercises_every_restart_boundary_without_duplicate_side_effects(
    comparison: DemoComparison,
) -> None:
    evidence = comparison.first
    expected_points = {
        "before_event_persistence",
        "after_event_persistence_before_watermark",
        "correction_during_aggregation",
        "after_slot_claim_before_signal_persistence",
        "after_signal_persistence_before_risk_decision",
        "after_intent_persistence_before_submission",
        "fake_broker_acceptance_before_response_persistence",
        "before_reconciliation",
        "before_forced_flatten_completion",
    }

    assert {item.failure_point for item in evidence.recovery_evidence} == expected_points
    assert all(
        item.outcome == "RECOVERED_NO_DUPLICATES"
        and item.injected_failure_count == 1
        and item.duplicate_side_effects == 0
        and item.incident_count == 0
        for item in evidence.recovery_evidence
    )
    state_transitions = tuple(item[2] for item in evidence.fake_broker_event_sequence)
    assert "SUBMISSION_UNKNOWN" in state_transitions
    assert "ACCEPTED" in state_transitions
    assert state_transitions[-1] == "FILLED"


def test_manifest_is_explicitly_offline_and_contains_no_performance_claims(
    comparison: DemoComparison,
) -> None:
    manifest = comparison.first.manifest()
    serialized = json.dumps(manifest, sort_keys=True).lower()

    assert manifest["evidence_label"] == EVIDENCE_LABEL
    assert manifest["evidence_manifest_hash"] == comparison.manifest_hash
    for prohibited in (
        "profitability",
        "sharpe",
        "alpha",
        "win_rate",
        "model_accuracy",
        "temporary_path",
        "process_id",
    ):
        assert prohibited not in serialized


def test_manifest_publication_is_confined_immutable_and_idempotent(
    tmp_path: Path,
    comparison: DemoComparison,
) -> None:
    destination = Path("evidence/demo.json")

    first = publish_demo_evidence(
        comparison.first,
        destination,
        application_root=tmp_path,
    )
    second = publish_demo_evidence(
        comparison.first,
        destination,
        application_root=tmp_path,
    )

    assert first == second
    assert json.loads(first.read_bytes())["evidence_manifest_hash"] == comparison.manifest_hash
    with pytest.raises(DemoError, match="relative"):
        publish_demo_evidence(
            comparison.first,
            Path("../escape.json"),
            application_root=tmp_path,
        )
    first.write_text("different", encoding="utf-8")
    with pytest.raises(DemoError, match="different content"):
        publish_demo_evidence(
            comparison.first,
            destination,
            application_root=tmp_path,
        )


def test_demo_cli_emits_machine_readable_determinism_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comparison: DemoComparison,
) -> None:
    monkeypatch.chdir(tmp_path)
    repository_root = Path(__file__).resolve().parents[2]

    result = CliRunner().invoke(
        app,
        [
            "demo",
            "--config-root",
            str(repository_root / "configs"),
            "--output",
            "evidence.json",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "check": "demo",
        "deterministic": True,
        "evidence_label": EVIDENCE_LABEL,
        "evidence_manifest_hash": comparison.manifest_hash,
        "evidence_path": "evidence.json",
        "status": "ok",
    }
    assert (tmp_path / "evidence.json").is_file()
