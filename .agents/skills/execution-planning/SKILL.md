---
name: execution-planning
description: "Turn a confirmed repository change into an executable, verifiable sequence; do not use for a trivial edit or when implementation is already complete."
---

# Execution planning

1. Read [PLANS.md](../../../docs/execution-plans/PLANS.md), the active plan, the applicable requirements in [requirements.md](../../../docs/requirements.md), and the relevant architecture or runbook sections.
2. Normalize the request into explicit in-scope behavior, non-goals, invariants, and acceptance criteria.
3. Inspect the current implementation and tests; do not plan against an assumed future layout.
4. Compare viable approaches and record why the chosen design is the smallest coherent change.
5. Divide work into dependency-ordered vertical milestones with concrete files and observable outcomes.
6. Define narrow tests, broader regression checks, static checks, migration or rollback checks, and operational validation.
7. Identify credential, broker, data-integrity, and destructive-operation boundaries before work starts.
8. Maintain the plan as evidence changes, marking completed work and documenting justified deviations.

Use the locked command surface in [tooling.md](../../../docs/tooling.md) and preserve the architectural invariants in [ARCHITECTURE.md](../../../ARCHITECTURE.md).
