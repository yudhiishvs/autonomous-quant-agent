---
name: root-cause-debugging
description: "Diagnose and minimally correct a reproducible defect; do not use when the task is feature design, speculative cleanup, or a workaround without an identified cause."
---

# Root-cause debugging

1. Reproduce the defect with the smallest deterministic command or test while preserving the original evidence.
2. Reduce the reproduction and identify the first incorrect state, not merely the final error.
3. Trace that state backward across interfaces, persistence, configuration, and external boundaries to its source.
4. State the root cause and explain why the observed symptom follows from it.
5. Add a focused regression test that fails for the cause and covers the meaningful boundary or recovery path.
6. Implement the minimal correction without loosening validation, safety gates, or test expectations.
7. Run neighboring tests and the applicable locked checks in [tooling.md](../../../docs/tooling.md).
8. Search for the same defect pattern elsewhere and verify the change fixes the cause rather than masking the symptom.

Use [ARCHITECTURE.md](../../../ARCHITECTURE.md) for state ownership and failure behavior. For operational failures, preserve evidence and follow [market_data_runbook.md](../../../docs/market_data_runbook.md) for the collector or [incident_response.md](../../../docs/incident_response.md) for the legacy paper path; do not apply either procedure outside its subsystem.
