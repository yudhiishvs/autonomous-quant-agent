---
name: skeptical-code-review
description: "Review an actual repository diff for evidence-backed defects and regression risk; do not use to rewrite code based only on stylistic preference."
---

# Skeptical code review

1. Read [code-review.md](../../../docs/code-review.md), inspect the complete diff, surrounding code, requirements, and tests, and distinguish pre-existing behavior from the proposed change.
2. Trace changed behavior through state ownership, persistence, concurrency, retries, shutdown, and external boundaries.
3. Look for correctness defects, hidden state, unintended side effects, unsafe defaults, and incompatible contracts.
4. Challenge unnecessary abstractions and inspect tests for false confidence, missing assertions, unrealistic mocks, and untested failure paths.
5. Evaluate security, privacy, broker isolation, data integrity, operational recovery, and measured performance implications.
6. Compare claims against [README.md](../../../README.md), [ARCHITECTURE.md](../../../ARCHITECTURE.md), the applicable data dictionary, and relevant runbooks.
7. Classify findings by severity and cite the narrowest affected lines with a concrete failure scenario.
8. Recommend only corrections supported by reproducible evidence; explicitly report when no actionable finding remains.

Do not modify the reviewed diff unless the contributor separately requests implementation.
