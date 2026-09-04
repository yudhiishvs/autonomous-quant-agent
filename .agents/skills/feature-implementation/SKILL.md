---
name: feature-implementation
description: "Implement an approved repository feature as a tested vertical slice; do not use for exploration-only, diagnosis-only, or review-only requests."
---

# Feature implementation

1. Read the accepted requirements and plan, then confirm relevant constraints in [README.md](../../../README.md) and [ARCHITECTURE.md](../../../ARCHITECTURE.md).
2. Trace current behavior and find analogous code and tests before designing new interfaces.
3. Choose the smallest coherent design that preserves safety, ownership boundaries, and legacy compatibility where the active plan requires it.
4. Add or update behavioral tests that fail for the missing behavior without weakening existing assertions.
5. Implement one end-to-end slice, including validation, persistence, failure behavior, and observability where applicable.
6. Run the narrowest relevant tests, then formatting, linting, type checking, and the broader offline suite using [tooling.md](../../../docs/tooling.md).
7. Update only documentation whose stated behavior or operating procedure changed.
8. Review the actual diff for unrelated changes, leaked credentials, generated artifacts, unsafe broker access, and unsupported claims.

Treat [data_dictionary.md](../../../docs/data_dictionary.md) as the contract only for legacy SQLite state; use the applicable schema or platform data reference and runbook for other state changes.
