---
name: security-review
description: "Assess a change that crosses credentials, network, persistence, authorization, deployment, or broker boundaries; do not use as a generic style review."
---

# Security review

1. Identify protected assets, actors, entry points, trust boundaries, and the impact of compromise.
2. Enumerate untrusted inputs and verify validation, size limits, canonicalization, timeouts, retries, and fail-closed behavior.
3. Inspect authentication separately from authorization, especially symbol eligibility and paper-order gates.
4. Verify least-privilege credential loading, separation of data and execution secrets, redaction, rotation assumptions, and absence from code, logs, images, tests, and CI.
5. Review persistence, migrations, serialization, immutable evidence, transaction ownership, and destructive capabilities.
6. Inspect dependency and supply-chain changes, fixed network destinations, TLS requirements, container privileges, and exposed services.
7. Test likely abuse, replay, duplication, stale-state, outage, and shutdown cases using offline fakes or disposable infrastructure.
8. Record evidence, severity, mitigations, and residual risk without overstating what tests prove.

Treat [security-model.md](../../../docs/security-model.md) and [SECURITY.md](../../../SECURITY.md) as the security and reporting authorities, and use [ARCHITECTURE.md](../../../ARCHITECTURE.md) for trust boundaries. Scope [market_data_runbook.md](../../../docs/market_data_runbook.md) to the collector and [live_paper_runbook.md](../../../docs/live_paper_runbook.md) plus [incident_response.md](../../../docs/incident_response.md) to the legacy paper path.
