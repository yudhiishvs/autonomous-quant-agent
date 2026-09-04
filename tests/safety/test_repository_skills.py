"""Validate the repository-local workflow skill contract."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CORE_SKILLS = {
    "behavioral-testing",
    "dependency-change-review",
    "execution-planning",
    "feature-implementation",
    "performance-investigation",
    "repository-exploration",
    "root-cause-debugging",
    "security-review",
    "skeptical-code-review",
}
ACTIVE_CONDITIONAL_SKILLS = {
    "backtest-validation",
    "database-migration",
    "frontend-validation",
    "incident-analysis",
    "trading-strategy-review",
}
AUTHORITY_LINKS = {
    "backtest-validation": {"../../../README.md", "../../../docs/methodology.md"},
    "behavioral-testing": {
        "../../../ARCHITECTURE.md",
        "../../../docs/testing-strategy.md",
        "../../../docs/tooling.md",
    },
    "database-migration": {
        "../../../ARCHITECTURE.md",
        "../../../docs/testing-strategy.md",
        "../../../docs/tooling.md",
        "../../../migrations/README.md",
    },
    "dependency-change-review": {
        "../../../docs/dependency-policy.md",
        "../../../docs/tooling.md",
        "../../../pyproject.toml",
        "../../../uv.lock",
    },
    "execution-planning": {
        "../../../ARCHITECTURE.md",
        "../../../docs/execution-plans/PLANS.md",
        "../../../docs/requirements.md",
        "../../../docs/tooling.md",
    },
    "feature-implementation": {
        "../../../ARCHITECTURE.md",
        "../../../README.md",
        "../../../docs/data_dictionary.md",
        "../../../docs/tooling.md",
    },
    "frontend-validation": {
        "../../../ARCHITECTURE.md",
        "../../../README.md",
        "../../../app.py",
        "../../../docs/testing-strategy.md",
        "../../../docs/tooling.md",
    },
    "incident-analysis": {
        "../../../docs/incident_response.md",
        "../../../docs/market_data_runbook.md",
        "../../../docs/tooling.md",
    },
    "performance-investigation": {
        "../../../ARCHITECTURE.md",
        "../../../README.md",
        "../../../docs/performance.md",
    },
    "repository-exploration": {
        "../../../ARCHITECTURE.md",
        "../../../README.md",
        "../../../docs/data_dictionary.md",
        "../../../docs/tooling.md",
        "../../../pyproject.toml",
    },
    "root-cause-debugging": {
        "../../../ARCHITECTURE.md",
        "../../../docs/incident_response.md",
        "../../../docs/market_data_runbook.md",
        "../../../docs/tooling.md",
    },
    "security-review": {
        "../../../ARCHITECTURE.md",
        "../../../SECURITY.md",
        "../../../docs/incident_response.md",
        "../../../docs/live_paper_runbook.md",
        "../../../docs/market_data_runbook.md",
        "../../../docs/security-model.md",
    },
    "skeptical-code-review": {
        "../../../ARCHITECTURE.md",
        "../../../README.md",
        "../../../docs/code-review.md",
    },
    "trading-strategy-review": {
        "../../../ARCHITECTURE.md",
        "../../../README.md",
        "../../../docs/methodology.md",
    },
}
REQUIRED_PHRASES = {
    "backtest-validation": {"broker-free", "look-ahead"},
    "behavioral-testing": {"durable state", "offline and deterministic"},
    "database-migration": {"destructive", "guarded loopback"},
    "dependency-change-review": {"never hand-edit", "removal path"},
    "execution-planning": {"acceptance criteria", "credential"},
    "feature-implementation": {"behavioral tests", "legacy compatibility"},
    "frontend-validation": {"read-only", "real browser", "screenshots"},
    "incident-analysis": {"never replay", "preserve redacted"},
    "performance-investigation": {"profile before", "raw result artifacts"},
    "repository-exploration": {"preserve all existing", "trust boundaries"},
    "root-cause-debugging": {"first incorrect state", "minimal correction"},
    "security-review": {"fail-closed", "least-privilege"},
    "skeptical-code-review": {"concrete failure scenario", "unintended side effects"},
    "trading-strategy-review": {"real-money execution", "strategy proposals"},
}
INLINE_LINK = re.compile(r"\[[^\]\n]+\]\((?P<target>[^()\s]+)\)")
REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^\]\n]+\]:", flags=re.MULTILINE)
REFERENCE_USE = re.compile(r"\[[^\]\n]+\]\[[^\]\n]+\]")
BRACKETED_TEXT = re.compile(r"\[[^\]\n]+\]")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", flags=re.MULTILINE)
URI_SCHEME = re.compile(r"\b(?:file|https?)://", flags=re.IGNORECASE)
PRIVATE_PATH_REFERENCE = re.compile(
    r"(?<![\w-])(?:\.env(?:\.[\w-]+)?|\.git/|\.local/|data/raw/|outputs/|runtime/|secrets/)",
    flags=re.IGNORECASE,
)
PRIVATE_ROOTS = {".git", ".local", "audit", "credentials", "outputs", "runtime", "secrets"}


def _repository_file(
    repository_root: Path,
    candidate: Path,
    *,
    require_tracked: bool,
) -> Path:
    root = repository_root.resolve(strict=True)
    lexical = Path(os.path.abspath(candidate))
    assert lexical.is_relative_to(root), f"path escapes repository: {candidate}"
    relative = lexical.relative_to(root)
    assert relative.parts, f"repository root is not a file: {candidate}"
    assert relative.parts[0] not in PRIVATE_ROOTS, f"private path is prohibited: {candidate}"
    assert not relative.name.startswith(".env"), f"environment file is prohibited: {candidate}"

    current = root
    for part in relative.parts:
        current /= part
        assert not current.is_symlink(), f"symlinked path is prohibited: {candidate}"

    resolved = lexical.resolve(strict=True)
    assert resolved.is_relative_to(root), f"resolved path escapes repository: {candidate}"
    assert resolved.is_file(), f"not a regular repository file: {candidate}"
    if require_tracked:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"skill link target is not tracked: {candidate}"
    return resolved


def _skill_files(project_root: Path) -> list[Path]:
    skills_root = project_root / ".agents" / "skills"
    assert skills_root.is_dir() and not skills_root.is_symlink()
    skill_files: list[Path] = []
    for skill_directory in sorted(skills_root.iterdir()):
        assert skill_directory.is_dir() and not skill_directory.is_symlink(), (
            f"unexpected skill entry: {skill_directory}"
        )
        skill_file = skill_directory / "SKILL.md"
        _repository_file(project_root, skill_file, require_tracked=False)
        skill_files.append(skill_file)
    return skill_files


def _skill_parts(project_root: Path, skill_file: Path) -> tuple[dict[str, Any], str]:
    safe_skill_file = _repository_file(project_root, skill_file, require_tracked=False)
    text = safe_skill_file.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"missing YAML frontmatter: {skill_file}"
    _, raw_frontmatter, body = text.split("---\n", maxsplit=2)
    frontmatter = yaml.safe_load(raw_frontmatter)
    assert isinstance(frontmatter, dict), f"invalid YAML frontmatter: {skill_file}"
    return frontmatter, body


def _skill_links(body: str) -> list[str]:
    assert not URI_SCHEME.search(body), "bare or external URI schemes are prohibited"
    assert not PRIVATE_PATH_REFERENCE.search(body), "private path references are prohibited"
    assert not REFERENCE_DEFINITION.search(body), "reference-style skill links are prohibited"
    assert not REFERENCE_USE.search(body), "reference-style skill links are prohibited"
    assert "<" not in body and ">" not in body, "HTML and autolinks are prohibited"
    assert "![" not in body, "skill instructions must not embed images"
    matches = list(INLINE_LINK.finditer(body))
    assert body.count("](") == len(matches), "skill links must use the restricted inline form"
    body_without_links = INLINE_LINK.sub("", body)
    assert not BRACKETED_TEXT.search(body_without_links), (
        "shortcut and unsupported bracket links are prohibited"
    )
    return [match.group("target") for match in matches]


def _markdown_anchors(document: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in HEADING.findall(document.read_text(encoding="utf-8")):
        plain = re.sub(r"[`*_~]", "", heading).lower()
        base = re.sub(r"[^\w\- ]", "", plain).strip().replace(" ", "-")
        suffix = counts.get(base, 0)
        anchor = base if suffix == 0 else f"{base}-{suffix}"
        counts[base] = suffix + 1
        anchors.add(anchor)
    return anchors


def test_required_workflow_skills_have_valid_metadata(project_root: Path) -> None:
    skill_files = _skill_files(project_root)
    assert skill_files

    discovered_names: set[str] = set()
    for skill_file in skill_files:
        frontmatter, body = _skill_parts(project_root, skill_file)
        assert set(frontmatter) == {"name", "description"}
        assert frontmatter["name"] == skill_file.parent.name
        assert isinstance(frontmatter["description"], str)
        assert "; do not use " in frontmatter["description"].lower()
        assert body.strip()
        discovered_names.add(skill_file.parent.name)

    assert discovered_names == CORE_SKILLS | ACTIVE_CONDITIONAL_SKILLS
    assert len(discovered_names) == len(skill_files), "skill names must be unique"


def test_skill_links_are_tracked_repository_authorities(project_root: Path) -> None:
    for skill_file in _skill_files(project_root):
        _, body = _skill_parts(project_root, skill_file)
        linked_files: set[str] = set()
        for raw_target in _skill_links(body):
            target, separator, fragment = raw_target.partition("#")
            assert target, f"anchor-only skill link: {raw_target}"
            assert "://" not in target, f"external skill link: {raw_target}"
            assert not Path(target).is_absolute(), f"absolute skill link: {raw_target}"
            resolved = _repository_file(
                project_root,
                skill_file.parent / target,
                require_tracked=True,
            )
            linked_files.add(target)
            if separator:
                assert resolved.suffix == ".md", f"fragment target is not Markdown: {raw_target}"
                assert fragment in _markdown_anchors(resolved), f"missing fragment: {raw_target}"

        assert linked_files >= AUTHORITY_LINKS[skill_file.parent.name]


def test_skills_retain_critical_workflow_obligations(project_root: Path) -> None:
    for skill_file in _skill_files(project_root):
        _, body = _skill_parts(project_root, skill_file)
        normalized_body = body.lower()
        for phrase in REQUIRED_PHRASES[skill_file.parent.name]:
            assert phrase in normalized_body, f"{skill_file.parent.name} is missing: {phrase}"


def test_repository_file_guard_rejects_private_and_symlinked_paths(
    project_root: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError, match="private path"):
        _repository_file(project_root, project_root / ".git" / "config", require_tracked=True)

    fake_root = tmp_path / "repository"
    skill_directory = fake_root / ".agents" / "skills" / "example"
    skill_directory.mkdir(parents=True)
    external_file = tmp_path / "external.md"
    external_file.write_text("external\n", encoding="utf-8")
    linked_file = skill_directory / "SKILL.md"
    linked_file.symlink_to(external_file)
    with pytest.raises(AssertionError, match="symlinked path"):
        _repository_file(fake_root, linked_file, require_tracked=False)

    for unsafe_link in (
        '<a href="../../../.env">private</a>',
        "<https://example.invalid/instructions>",
        "   [private]: ../../../.env\n[private]",
        "Read https://example.invalid/instructions and ../../../.env",
    ):
        with pytest.raises(AssertionError):
            _skill_links(unsafe_link)
