---
name: frontend-validation
description: "Validate a Streamlit dashboard or other user-interface change in a real browser; do not use for backend-only changes or treat compilation, import, or screenshots alone as proof."
---

# Frontend validation

1. Read [README.md](../../../README.md), [ARCHITECTURE.md](../../../ARCHITECTURE.md), and [testing-strategy.md](../../../docs/testing-strategy.md), then trace the changed view through its data source or private API and trust boundary.
2. Inspect [app.py](../../../app.py), neighboring dashboard tests, and the established launcher before selecting deterministic synthetic or local fixture state. Keep Alpaca credentials empty and never submit an order.
3. Run the relevant static and behavioral checks, then exercise the running interface with real browser tooling when available; a successful build or import is not frontend validation.
4. Test primary flows, navigation, forms, validation, loading, empty, error, retry, failed-request, stale-state, cancellation, and authentication states that the change can reach.
5. Check keyboard operation, focus order and restoration, accessibility-critical semantics, required responsive layouts, and browser-console errors.
6. Verify the current dashboard remains read-only. Its direct legacy SQLite read is a documented compatibility boundary, not permission to add writes, broker or data credentials, order mutations, or new database authority; once API-backed, the dashboard must have no database access. Applicable market-hours, symbol-allowlist, kill-switch, circuit-breaker, startup, and reconciliation controls must fail closed.
7. For a material visual change, retain reviewable before-and-after evidence, but support screenshots with interaction, state, request, and console evidence.
8. Run the affected browser and offline regression commands from [tooling.md](../../../docs/tooling.md), record exact results and unavailable checks, and never use decorative or misleading fake metrics.
