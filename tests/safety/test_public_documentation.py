"""Public documentation inventory and evidence-boundary tests."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_DOCUMENTS = (
    "README.md",
    "docs/architecture.md",
    "docs/data_dictionary.md",
    "docs/security_architecture.md",
    "docs/threat_model.md",
    "docs/failure_modes.md",
    "docs/incident_response.md",
    "docs/secret_rotation.md",
    "docs/backup_restore.md",
    "docs/operations.md",
    "docs/developer_guide.md",
    "docs/strategy_extension.md",
    "docs/demo_runbook.md",
    "docs/implementation_status.md",
    "docs/resume_evidence.md",
)
ADR_HEADINGS = (
    "## Context",
    "## Decision",
    "## Alternatives considered",
    "## Consequences",
    "## Security impact",
)


def test_required_public_documentation_exists_and_is_nonempty() -> None:
    for relative_path in REQUIRED_DOCUMENTS:
        path = REPOSITORY_ROOT / relative_path
        assert path.is_file(), relative_path
        assert path.read_text(encoding="utf-8").strip(), relative_path


def test_required_decision_records_have_complete_sections() -> None:
    adr_root = REPOSITORY_ROOT / "docs" / "adr"

    for number in range(1, 13):
        matches = tuple(adr_root.glob(f"{number:04d}-*.md"))
        assert len(matches) == 1, f"ADR {number:04d} inventory: {matches!r}"
        text = matches[0].read_text(encoding="utf-8")
        assert all(heading in text for heading in ADR_HEADINGS), matches[0].name


def test_readme_separates_status_and_offline_safety_claims() -> None:
    text = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    for status in (
        "Implemented and offline verified",
        "Implemented but not credential-validated",
        "Intentionally deferred",
        "Unsupported",
    ):
        assert status in text
    assert "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE" in text
    assert "does not support real-money trading" in text


def test_resume_evidence_keeps_fixture_and_market_results_separate() -> None:
    text = (REPOSITORY_ROOT / "docs" / "resume_evidence.md").read_text(encoding="utf-8")

    assert "OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE" in text
    assert "two fresh isolated runs" in text
    assert "No Alpaca credential" in text
    assert "do not measure profitability" in text
