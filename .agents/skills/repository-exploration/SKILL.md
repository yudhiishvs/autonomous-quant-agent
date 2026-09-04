---
name: repository-exploration
description: "Map unfamiliar repository structure and behavior before planning, review, or implementation; do not use when the affected paths and behavior are already established."
---

# Repository exploration

Use this workflow to build an evidence-based map before proposing changes.

1. Confirm the repository root, current branch, working-tree state, and remotes. Preserve all existing contributor changes.
2. Read the relevant manifests and the project status, scope, safety boundaries, and canonical commands in [README.md](../../../README.md).
3. Map entry points, package boundaries, configuration, persistence, and external integrations using [ARCHITECTURE.md](../../../ARCHITECTURE.md) and [pyproject.toml](../../../pyproject.toml).
4. Trace the requested behavior from its entry point through state changes and outputs. Use [data_dictionary.md](../../../docs/data_dictionary.md) only for legacy SQLite state; use the applicable schema or platform data reference elsewhere.
5. Locate neighboring implementations, tests, fixtures, and operational scripts, then use [tooling.md](../../../docs/tooling.md) for locked commands; treat [Makefile](../../../Makefile) as a legacy convenience surface.
6. Identify trust boundaries, failure modes, compatibility constraints, and likely regression surfaces.
7. Summarize the current behavior, relevant files, unresolved questions, and risk before transitioning to planning or implementation.

Do not edit files during exploration unless the contributor explicitly transitions the task to implementation.
