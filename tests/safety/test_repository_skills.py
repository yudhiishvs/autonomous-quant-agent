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
EXPECTED_SKILLS = CORE_SKILLS | ACTIVE_CONDITIONAL_SKILLS
REQUIRED_AUTHORITY_PATHS = {
    "backtest-validation": {"README.md", "docs/methodology.md"},
    "behavioral-testing": {
        "ARCHITECTURE.md",
        "docs/testing-strategy.md",
        "docs/tooling.md",
    },
    "database-migration": {
        "ARCHITECTURE.md",
        "docs/testing-strategy.md",
        "docs/tooling.md",
        "migrations/README.md",
    },
    "dependency-change-review": {
        "docs/dependency-policy.md",
        "docs/tooling.md",
        "pyproject.toml",
        "uv.lock",
    },
    "execution-planning": {
        "ARCHITECTURE.md",
        "docs/execution-plans/PLANS.md",
        "docs/requirements.md",
        "docs/tooling.md",
    },
    "feature-implementation": {
        "ARCHITECTURE.md",
        "README.md",
        "docs/data_dictionary.md",
        "docs/tooling.md",
    },
    "frontend-validation": {
        "ARCHITECTURE.md",
        "README.md",
        "app.py",
        "docs/testing-strategy.md",
        "docs/tooling.md",
    },
    "incident-analysis": {
        "docs/incident_response.md",
        "docs/market_data_runbook.md",
        "docs/tooling.md",
    },
    "performance-investigation": {
        "ARCHITECTURE.md",
        "README.md",
        "docs/performance.md",
    },
    "repository-exploration": {
        "ARCHITECTURE.md",
        "README.md",
        "docs/data_dictionary.md",
        "docs/tooling.md",
        "pyproject.toml",
    },
    "root-cause-debugging": {
        "ARCHITECTURE.md",
        "docs/incident_response.md",
        "docs/market_data_runbook.md",
        "docs/tooling.md",
    },
    "security-review": {
        "ARCHITECTURE.md",
        "SECURITY.md",
        "docs/incident_response.md",
        "docs/live_paper_runbook.md",
        "docs/market_data_runbook.md",
        "docs/security-model.md",
    },
    "skeptical-code-review": {
        "ARCHITECTURE.md",
        "README.md",
        "docs/code-review.md",
    },
    "trading-strategy-review": {
        "ARCHITECTURE.md",
        "README.md",
        "docs/methodology.md",
    },
}
REQUIRED_CLAUSES = {
    "backtest-validation": {
        "backtests must remain broker-free. do not present simulated, replay, or paper evidence "
        "as live execution performance or a guarantee",
        "no-look-ahead boundary",
    },
    "behavioral-testing": {
        "assert outcomes and durable state",
        "tests must not require credentials or contact alpaca",
    },
    "database-migration": {
        "guarded loopback disposable-postgresql procedure",
        "never run migration experiments against a database",
    },
    "dependency-change-review": {"give its removal path", "never hand-edit it"},
    "execution-planning": {
        "acceptance criteria",
        "identify credential, broker, data-integrity, and destructive-operation boundaries",
    },
    "feature-implementation": {
        "behavioral tests that fail for the missing behavior",
        "legacy compatibility where the active plan requires it",
    },
    "frontend-validation": {
        "current dashboard remains read-only",
        "real browser tooling",
        "support screenshots with interaction",
    },
    "incident-analysis": {
        "never replay an uncertain broker side effect",
        "preserve redacted logs",
    },
    "performance-investigation": {"profile before editing", "raw result artifacts"},
    "repository-exploration": {
        "identify trust boundaries",
        "preserve all existing contributor changes",
    },
    "root-cause-debugging": {"first incorrect state", "implement the minimal correction"},
    "security-review": {"fail-closed behavior", "least-privilege credential loading"},
    "skeptical-code-review": {"concrete failure scenario", "unintended side effects"},
    "trading-strategy-review": {
        "real-money execution is outside repository scope",
        "strategy proposals",
    },
}
INLINE_LINK = re.compile(r"\[[^\]\n]+\]\((?P<target>[^()\s]+)\)")
REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^\]\n]+\]:", flags=re.MULTILINE)
REFERENCE_USE = re.compile(r"\[[^\]\n]+\]\[[^\]\n]+\]")
ATX_HEADING = re.compile(r"^\s{0,3}#{1,6}[ \t]+(?P<heading>.*?)(?:[ \t]+#+[ \t]*)?$")
FENCE = re.compile(r"^\s{0,3}(?P<marker>`{3,}|~{3,})(?P<tail>.*)$")
URI_SCHEME = re.compile(
    r"(?<![\w.+-])(?:[a-z][a-z0-9+.-]*://|file:(?=/)|mailto:|data:)",
    flags=re.IGNORECASE,
)
TARGET_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
ENV_PATH_REFERENCE = re.compile(r"(?<![\w.-])\.env(?:\.[\w-]+)*(?![\w.-])", re.IGNORECASE)
PRIVATE_PATH_REFERENCE = re.compile(
    r"(?<![\w-])(?:\.git/|\.local/|data/(?:cache|raw)/|outputs/|runtime/|secrets/)",
    flags=re.IGNORECASE,
)
PRIVATE_PATH_PREFIXES = {
    (".git",),
    (".local",),
    ("audit",),
    ("credentials",),
    ("data", "cache"),
    ("data", "raw"),
    ("outputs",),
    ("runtime",),
    ("secrets",),
}
SAFE_ENV_FILE = ".env.example"


def _repository_file(
    repository_root: Path,
    candidate: Path,
    *,
    tracked_paths: frozenset[str] | None = None,
) -> Path:
    root = repository_root.resolve(strict=True)
    lexical = Path(os.path.abspath(candidate))
    assert lexical.is_relative_to(root), f"path escapes repository: {candidate}"
    relative = lexical.relative_to(root)
    assert relative.parts, f"repository root is not a file: {candidate}"
    folded_parts = tuple(part.casefold() for part in relative.parts)
    assert not any(folded_parts[: len(prefix)] == prefix for prefix in PRIVATE_PATH_PREFIXES), (
        f"private path is prohibited: {candidate}"
    )
    for part in folded_parts:
        is_environment_file = part == ".env" or part.startswith(".env.")
        assert not (is_environment_file and part != SAFE_ENV_FILE), (
            f"environment file is prohibited: {candidate}"
        )

    current = root
    for part in relative.parts:
        current /= part
        assert not current.is_symlink(), f"symlinked path is prohibited: {candidate}"

    resolved = lexical.resolve(strict=True)
    assert resolved.is_relative_to(root), f"resolved path escapes repository: {candidate}"
    assert resolved.is_file(), f"not a regular repository file: {candidate}"
    if tracked_paths is not None:
        assert relative.as_posix() in tracked_paths, f"skill path is not tracked: {candidate}"
    return resolved


def _tracked_repository_paths(repository_root: Path) -> frozenset[str]:
    root = repository_root.resolve(strict=True)
    result = subprocess.run(
        ["git", "ls-files", "--cached", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "could not enumerate tracked repository files"
    return frozenset(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)


def _canonical_repository_path(repository_root: Path, repository_file: Path) -> str:
    return repository_file.relative_to(repository_root.resolve(strict=True)).as_posix()


def _skill_files(project_root: Path) -> list[Path]:
    skills_root = project_root / ".agents" / "skills"
    assert skills_root.is_dir() and not skills_root.is_symlink()
    tracked_paths = _tracked_repository_paths(project_root)
    skill_files: list[Path] = []
    for skill_directory in sorted(skills_root.iterdir()):
        assert skill_directory.is_dir() and not skill_directory.is_symlink(), (
            f"unexpected skill entry: {skill_directory}"
        )
        skill_file = skill_directory / "SKILL.md"
        _repository_file(project_root, skill_file, tracked_paths=tracked_paths)
        skill_files.append(skill_file)
    return skill_files


def _skill_parts(project_root: Path, skill_file: Path) -> tuple[dict[str, Any], str]:
    safe_skill_file = _repository_file(
        project_root,
        skill_file,
        tracked_paths=_tracked_repository_paths(project_root),
    )
    text = safe_skill_file.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"missing YAML frontmatter: {skill_file}"
    _, raw_frontmatter, body = text.split("---\n", maxsplit=2)
    frontmatter = yaml.safe_load(raw_frontmatter)
    assert isinstance(frontmatter, dict), f"invalid YAML frontmatter: {skill_file}"
    return frontmatter, body


def _skill_links(body: str) -> list[str]:
    assert not URI_SCHEME.search(body), "bare or external URI schemes are prohibited"
    assert not PRIVATE_PATH_REFERENCE.search(body), "private path references are prohibited"
    for match in ENV_PATH_REFERENCE.finditer(body):
        assert match.group().casefold() == SAFE_ENV_FILE, "private path references are prohibited"
    assert not REFERENCE_DEFINITION.search(body), "reference-style skill links are prohibited"
    assert not REFERENCE_USE.search(body), "reference-style skill links are prohibited"
    assert "<" not in body and ">" not in body, "angle brackets, HTML, and autolinks are prohibited"
    assert "![" not in body, "skill instructions must not embed images"
    matches = list(INLINE_LINK.finditer(body))
    assert body.count("](") == len(matches), "skill links must use the restricted inline form"
    body_without_links = INLINE_LINK.sub("", body)
    assert "[" not in body_without_links and "]" not in body_without_links, (
        "brackets outside restricted inline links are prohibited"
    )
    targets = [match.group("target") for match in matches]
    assert not any(TARGET_URI_SCHEME.match(target) for target in targets), (
        "external skill links are prohibited"
    )
    return targets


def _markdown_anchors(document: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    active_fence: str | None = None
    for line in document.read_text(encoding="utf-8").splitlines():
        fence_match = FENCE.match(line)
        if active_fence is not None:
            if fence_match:
                marker = fence_match.group("marker")
                if (
                    marker[0] == active_fence[0]
                    and len(marker) >= len(active_fence)
                    and not fence_match.group("tail").strip()
                ):
                    active_fence = None
            continue
        if fence_match:
            active_fence = fence_match.group("marker")
            continue
        heading_match = ATX_HEADING.match(line)
        if heading_match is None:
            continue
        heading = heading_match.group("heading")
        plain = re.sub(r"[`*_~]", "", heading).lower()
        base = re.sub(r"[^\w\- ]", "", plain).strip().replace(" ", "-")
        suffix = counts.get(base, 0)
        anchor = base if suffix == 0 else f"{base}-{suffix}"
        counts[base] = suffix + 1
        anchors.add(anchor)
    return anchors


def _validate_inventory(discovered_names: set[str]) -> None:
    assert discovered_names == EXPECTED_SKILLS, (
        f"skill inventory mismatch: missing={sorted(EXPECTED_SKILLS - discovered_names)}, "
        f"unexpected={sorted(discovered_names - EXPECTED_SKILLS)}"
    )
    assert set(REQUIRED_AUTHORITY_PATHS) == EXPECTED_SKILLS
    assert set(REQUIRED_CLAUSES) == EXPECTED_SKILLS


def _normalized_prose(text: str) -> str:
    return " ".join(text.casefold().split())


def _validate_skill_contract(
    skill_name: str,
    body: str,
    linked_paths: set[str],
) -> None:
    assert skill_name in EXPECTED_SKILLS, f"unexpected skill contract: {skill_name}"
    missing_authorities = REQUIRED_AUTHORITY_PATHS[skill_name] - linked_paths
    assert not missing_authorities, (
        f"{skill_name} is missing authorities: {sorted(missing_authorities)}"
    )
    normalized_body = _normalized_prose(body)
    for clause in REQUIRED_CLAUSES[skill_name]:
        assert _normalized_prose(clause) in normalized_body, (
            f"{skill_name} is missing required clause: {clause}"
        )


def _validate_fragment(document: Path, fragment: str, raw_target: str) -> None:
    assert document.suffix == ".md", f"fragment target is not Markdown: {raw_target}"
    assert fragment in _markdown_anchors(document), f"missing fragment: {raw_target}"


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

    _validate_inventory(discovered_names)


def test_skill_links_are_tracked_repository_authorities(project_root: Path) -> None:
    tracked_paths = _tracked_repository_paths(project_root)
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
                tracked_paths=tracked_paths,
            )
            linked_files.add(_canonical_repository_path(project_root, resolved))
            if separator:
                _validate_fragment(resolved, fragment, raw_target)

        _validate_skill_contract(skill_file.parent.name, body, linked_files)


def test_repository_file_guard_rejects_private_and_symlinked_paths(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "repository"
    fake_root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=fake_root,
        check=True,
        capture_output=True,
    )
    (fake_root / ".gitignore").write_text(
        ".env*\n!.env.example\nignored.md\n",
        encoding="utf-8",
    )
    new_document = fake_root / "docs" / "new.md"
    new_document.parent.mkdir()
    new_document.write_text("# New authority\n", encoding="utf-8")
    environment_example = fake_root / ".env.example"
    environment_example.write_text("SETTING=placeholder\n", encoding="utf-8")
    ignored_document = fake_root / "ignored.md"
    ignored_document.write_text("ignored\n", encoding="utf-8")
    cached_document = fake_root / "data" / "cache" / "local.md"
    cached_document.parent.mkdir(parents=True)
    cached_document.write_text("private\n", encoding="utf-8")
    untracked_document = fake_root / "docs" / "untracked.md"
    untracked_document.write_text("# Untracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", ".env.example", "docs/new.md"],
        cwd=fake_root,
        check=True,
        capture_output=True,
    )
    tracked_paths = _tracked_repository_paths(fake_root)

    assert (
        _repository_file(
            fake_root,
            new_document,
            tracked_paths=tracked_paths,
        )
        == new_document
    )
    normalized_document = fake_root / "docs" / "nested" / ".." / "new.md"
    resolved_document = _repository_file(
        fake_root,
        normalized_document,
        tracked_paths=tracked_paths,
    )
    assert _canonical_repository_path(fake_root, resolved_document) == "docs/new.md"
    assert (
        _repository_file(
            fake_root,
            environment_example,
            tracked_paths=tracked_paths,
        )
        == environment_example
    )
    with pytest.raises(AssertionError, match="private path"):
        _repository_file(
            fake_root,
            fake_root / ".git" / "config",
            tracked_paths=tracked_paths,
        )
    with pytest.raises(AssertionError, match="private path"):
        _repository_file(fake_root, cached_document, tracked_paths=tracked_paths)
    with pytest.raises(AssertionError, match="not tracked"):
        _repository_file(fake_root, ignored_document, tracked_paths=tracked_paths)
    with pytest.raises(AssertionError, match="not tracked"):
        _repository_file(fake_root, untracked_document, tracked_paths=tracked_paths)
    with pytest.raises(AssertionError, match="escapes repository"):
        _repository_file(fake_root, tmp_path / "outside.md")

    skill_directory = fake_root / ".agents" / "skills" / "example"
    skill_directory.mkdir(parents=True)
    external_file = tmp_path / "external.md"
    external_file.write_text("external\n", encoding="utf-8")
    linked_file = skill_directory / "SKILL.md"
    linked_file.symlink_to(external_file)
    with pytest.raises(AssertionError, match="symlinked path"):
        _repository_file(fake_root, linked_file)
    linked_directory = fake_root / ".agents" / "skills" / "linked"
    linked_directory.symlink_to(external_file.parent, target_is_directory=True)
    with pytest.raises(AssertionError, match="symlinked path"):
        _repository_file(fake_root, linked_directory / "external.md")


@pytest.mark.parametrize(
    "unsafe_link",
    (
        '<a href="../../../.env">private</a>',
        '<a\nhref="//example.invalid">external',
        '<a\nhref="javascript:payload">external',
        '<img\nsrc="//example.invalid/x">',
        "<https://example.invalid/instructions>",
        "   [private]: ../../../.env\n[private]",
        "[hidden\nlabel]: ../../../docs/untracked.md\n[hidden\nlabel]",
        "Read [hidden\nlabel][authority\nname]",
        "Read https://example.invalid/instructions and ../../../.env",
        "Read ssh://example.invalid/instructions",
        "Download ftp://example.invalid/instructions",
        "Email mailto:operator@example.invalid",
        "Embed data:text/plain,instructions",
        "Run [code](javascript:payload)",
        "Call [operator](tel:value)",
        "Connect [host](ssh:user@example.invalid)",
        "Read ../../../data/cache/private.csv",
        "Read ](../../../docs/testing-strategy.md)",
        '[policy](../../../docs/policy.md "title")',
        "[policy](../../../docs/policy_(draft).md)",
    ),
)
def test_restricted_skill_links_reject_unsafe_or_unsupported_forms(unsafe_link: str) -> None:
    with pytest.raises(AssertionError):
        _skill_links(unsafe_link)


def test_restricted_skill_links_allow_local_links_and_environment_example() -> None:
    body = "Use [policy](../../../docs/policy.md#safe-heading) and .env.example."
    assert _skill_links(body) == ["../../../docs/policy.md#safe-heading"]


@pytest.mark.parametrize(
    "discovered_names",
    (
        EXPECTED_SKILLS - {"behavioral-testing"},
        EXPECTED_SKILLS | {"api-design"},
        EXPECTED_SKILLS | {"release"},
        EXPECTED_SKILLS | {"unexpected-workflow"},
    ),
)
def test_inventory_rejects_missing_inactive_and_unknown_skills(
    discovered_names: set[str],
) -> None:
    with pytest.raises(AssertionError, match="skill inventory mismatch"):
        _validate_inventory(discovered_names)


def test_authority_and_clause_contract_rejects_placeholder_content() -> None:
    skill_name = "backtest-validation"
    with pytest.raises(AssertionError, match="missing authorities"):
        _validate_skill_contract(skill_name, "placeholder", set())
    with pytest.raises(AssertionError, match="missing required clause"):
        _validate_skill_contract(
            skill_name,
            "Do not claim that backtests are broker-free.",
            REQUIRED_AUTHORITY_PATHS[skill_name],
        )


def test_markdown_fragments_ignore_fenced_code_headings(tmp_path: Path) -> None:
    document = tmp_path / "authority.md"
    document.write_text(
        "# Real heading\n\n```markdown\n# Fenced heading\n```\n\n## Repeated\n## Repeated\n",
        encoding="utf-8",
    )
    assert _markdown_anchors(document) == {"real-heading", "repeated", "repeated-1"}
    _validate_fragment(document, "real-heading", "authority.md#real-heading")
    with pytest.raises(AssertionError, match="missing fragment"):
        _validate_fragment(document, "fenced-heading", "authority.md#fenced-heading")
