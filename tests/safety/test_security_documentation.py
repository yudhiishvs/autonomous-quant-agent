"""Keep the platform security documentation complete and evidence-aware."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = REPOSITORY_ROOT / "docs" / "threat_model.md"
SECURITY_ARCHITECTURE = REPOSITORY_ROOT / "docs" / "security_architecture.md"

REQUIRED_ASSETS = {
    "market-data credentials",
    "paper-broker credentials",
    "operator token",
    "database-role credentials",
    "order authority",
    "account/position state",
    "experiment and signal artifacts",
    "market datasets",
    "immutable evidence",
    "audit history",
    "service availability",
}
REQUIRED_ACTORS = {
    "malicious remote caller",
    "malicious local user",
    "compromised dependency",
    "compromised strategy plugin",
    "compromised future AI agent",
    "malicious or corrupted artifact",
    "compromised container",
    "accidental developer error",
    "replayed message",
    "stale provider data",
    "broker/API ambiguity",
    "database race or corruption",
}
REQUIRED_ENTRY_POINTS = {
    "FastAPI",
    "Streamlit server",
    "CLI",
    "YAML configuration",
    "environment and secret files",
    "plugin entry points",
    "market-data REST/WebSocket payloads",
    "PostgreSQL",
    "Docker network",
    "Parquet/JSON artifacts",
    "GitHub Actions/dependencies",
}
REQUIRED_THREAT_FIELDS = (
    "STRIDE class",
    "Asset",
    "Entry point",
    "Precondition",
    "Attack/failure sequence",
    "Impact",
    "Preventive controls",
    "Detective controls",
    "Recovery controls",
    "Verification test",
    "Residual risk",
    "Owner/status",
)
ALLOWED_STATUSES = {
    "IMPLEMENTED_AND_VERIFIED",
    "IMPLEMENTED_NOT_EXTERNALLY_VALIDATED",
    "PARTIALLY_IMPLEMENTED",
    "NOT_IMPLEMENTED",
    "BLOCKED",
    "INTENTIONALLY_DEFERRED",
}
SECRET_FILE_VARIABLES = {
    "AQA_DATABASE_URL_FILE",
    "AQA_OPERATOR_TOKEN_FILE",
    "AQA_ALPACA_DATA_API_KEY_FILE",
    "AQA_ALPACA_DATA_SECRET_KEY_FILE",
    "AQA_ALPACA_PAPER_API_KEY_FILE",
    "AQA_ALPACA_PAPER_SECRET_KEY_FILE",
    "AQA_PAPER_ACCOUNT_ID_HASH_FILE",
}
BOOTSTRAP_FILES = {
    "postgres_password",
    "aqa_migrate_password",
    "aqa_collector_password",
    "aqa_scheduler_password",
    "aqa_strategy_password",
    "aqa_execution_password",
    "aqa_control_password",
    "aqa_readonly_password",
    "operator_token",
}
NONSECRET_DEFAULTS = {
    "AQA_CONFIG=configs/platform/offline.yaml",
    "AQA_ARTIFACT_ROOT=outputs/artifacts",
    "AQA_API_BASE_URL=http://127.0.0.1:8000",
    "AQA_LOG_FORMAT=json",
    "AQA_ENABLE_PAPER_ORDERS=NO",
    "AQA_API_DOCS_ENABLED=NO",
}
FORBIDDEN_GENERIC_VARIABLES = {
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "APCA_API_BASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
}
THREAT_HEADING = re.compile(r"^### (?P<id>AQA-TM-\d{3}) — .+$", re.MULTILINE)


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading) + len(heading)
    end = text.index(next_heading, start)
    return text[start:end]


def _listed_terms(section: str) -> set[str]:
    return {
        re.sub(r";?\s+and$", "", match.group(1).strip()).rstrip(".;")
        for match in re.finditer(r"^\s*\d+\.\s+(.+)$", section, re.MULTILINE)
    }


def _fenced_inventory(text: str, heading: str) -> set[str]:
    section = text.split(heading, maxsplit=1)[1]
    fenced = section.split("```text", maxsplit=1)[1].split("```", maxsplit=1)[0]
    return {line.strip() for line in fenced.splitlines() if line.strip()}


def test_threat_model_has_required_inventory() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")

    assert (
        _listed_terms(_section(text, "## Assets", "## Actors and failure sources"))
        >= REQUIRED_ASSETS
    )

    actors = " ".join(_section(text, "## Actors and failure sources", "## Entry points").split())
    for actor in REQUIRED_ACTORS:
        assert actor in actors

    entry_points = " ".join(_section(text, "## Entry points", "## Trust-boundary summary").split())
    for entry_point in REQUIRED_ENTRY_POINTS:
        assert entry_point in entry_points


def test_each_threat_has_the_complete_review_record() -> None:
    text = THREAT_MODEL.read_text(encoding="utf-8")
    matches = list(THREAT_HEADING.finditer(text))

    assert len(matches) >= 18
    assert len({match.group("id") for match in matches}) == len(matches)

    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else text.index("## Threat-to-test index", match.end())
        )
        record = text[match.end() : end]
        for field in REQUIRED_THREAT_FIELDS:
            assert f"- **{field}:**" in record, f"{match.group('id')} is missing {field}"
        owner_status = re.search(
            r"^- \*\*Owner/status:\*\* [^\n]+ — `(?P<status>[A-Z_]+)`",
            record,
            re.MULTILINE,
        )
        assert owner_status is not None, f"{match.group('id')} has no owner status"
        assert owner_status.group("status") in ALLOWED_STATUSES


def test_security_architecture_records_exact_secret_interfaces() -> None:
    text = SECURITY_ARCHITECTURE.read_text(encoding="utf-8")

    assert _fenced_inventory(text, "### Exact generic-platform secret-file interface") == (
        SECRET_FILE_VARIABLES
    )
    assert _fenced_inventory(text, "### Rejected generic credential variables") == (
        FORBIDDEN_GENERIC_VARIABLES
    )
    assert _fenced_inventory(text, "### Exact non-secret runtime interface") == NONSECRET_DEFAULTS
    assert _fenced_inventory(text, "### Exact local bootstrap output") == BOOTSTRAP_FILES


def test_security_documents_are_registered_as_authorities() -> None:
    authority_files = (
        "AGENTS.md",
        "ARCHITECTURE.md",
        "SECURITY.md",
        "docs/README.md",
        ".agents/skills/security-review/SKILL.md",
    )

    for relative_path in authority_files:
        text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        assert "threat_model.md" in text
        assert "security_architecture.md" in text
