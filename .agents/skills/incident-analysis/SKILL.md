---
name: incident-analysis
description: "Investigate an operational data, persistence, reconciliation, or paper-safety incident while preserving evidence; do not use for ordinary feature debugging without an incident boundary."
---

# Incident analysis

1. Contain possible financial authority, credential exposure, stale writers, and corrupt state before attempting recovery.
2. Follow [market_data_runbook.md](../../../docs/market_data_runbook.md) for collector incidents or [incident_response.md](../../../docs/incident_response.md) for the legacy paper path, then identify the affected mode, commit, configuration hash, run/correlation IDs, and first incorrect durable transition. For another subsystem without an implemented runbook, state that limitation and use its architecture and security authorities without borrowing an unrelated recovery procedure.
3. Preserve redacted logs, events, receipts, database metadata, and external acknowledgements without copying credentials, account identifiers, or raw sensitive payloads.
4. Build a deterministic timeline across input, clock, transaction, lease, intent, broker effect, reconciliation, latch, and operator-control boundaries.
5. Separate root cause from trigger and impact; state which safety control contained or failed to contain the event.
6. Reproduce with synthetic data, fake credentials, injected clocks, and a disposable database. Never replay an uncertain broker side effect.
7. Correct the narrowest root cause and add a behavioral regression covering the failure and recovery path.
8. Run the affected checks from [tooling.md](../../../docs/tooling.md), verify audit/replay consistency, and record residual risk and explicit operator recovery steps.

If a real credential may have been exposed, stop normal investigation, require revocation or rotation, and report only its location and redacted incident metadata.
