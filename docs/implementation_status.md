# Implementation status

The offline platform and its non-AI services are implemented and tested. That does not
establish readiness for an externally connected deployment.

The container and heartbeat repairs in PRs [#19](https://github.com/yudhiishvs/autonomous-quant-agent/pull/19)
and [#20](https://github.com/yudhiishvs/autonomous-quant-agent/pull/20) passed all five
workflows on the merged revision: CI, Security, Container, CodeQL, and Offline Demo.
Git history was subsequently rewritten for documentation cleanup. Check
[Actions](https://github.com/yudhiishvs/autonomous-quant-agent/actions) for the result
on any later revision; historical success is not a guarantee for a new build.

## Verified behavior

| Area | What is covered |
| --- | --- |
| Market data | Validation, duplicate/correction lineage, fenced checkpoints, aggregation, gaps and readiness |
| Datasets | Immutable Parquet publication, manifests, causal snapshots and registration |
| Storage | PostgreSQL migrations, scoped roles, transactions, audit verification and restore checks |
| Workers | Durable jobs, outbox delivery, leases, health reporting and recovery paths |
| Paper execution | Risk checks, recorded intent, reconciliation, reversal and ambiguity handling with fake providers |
| API and dashboard | Authenticated control routes and read-only status views |
| Packaging | Installed-wheel checks, three isolated runtime images and an offline demo |

The fixture pipeline produces 4,290 minute rows and a 208-row frozen dataset. The
heartbeat repair added four deterministic regression cases covering job and outbox
completion on both success and failure paths.

The former container blocker—51 HIGH and three CRITICAL OS findings per Debian-based
image—was resolved by moving to a pinned Wolfi base with Python 3.11.16 and SQLite
3.53.4. All three rebuilt images passed the strict scanner without vulnerability
exceptions. Future rebuilds must still pass: advisory databases and transitive OS
packages can change.

## Remaining limitations

Alpaca adapters are `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`. Actual data entitlements,
paper account/order behavior, hosted PostgreSQL/TLS, and sustained service operation
still need external validation. None is established by the offline demo.

Paper reconciliation rejects a rounded broker average when it differs from exact fill
evidence. It blocks on the mismatch rather than assuming an undocumented tolerance.
This remains an integration limitation.

Main AI is `OUT_OF_SCOPE_FROZEN_AI`: the 61-file manifest and frozen dependencies are
unchanged by these repairs. Training and model approval are outside the completed
work. Tracked profiles disable submission, and the frozen approval gate cannot grant
paper-order authority. Real-money trading and public multi-user hosting are unsupported.

## Evidence

- [Development notes](development_notes.md): the two recent defects and their fixes.
- [Quality repair ledger](evidence/quality-repair-20260907.md): dated investigation results.
- [Architecture verification](evidence/non-ai-architecture-verification.md): earlier checks.
- [Requirement traceability](evidence/non-ai-requirement-traceability.md): requirement-to-code map.

Read the dated ledgers chronologically. Their old failure counts describe the builds
under investigation at that time, not the current container implementation. Coverage
floors remain 74% for the repository and 85% for the platform, with branch measurement
and two-decimal reporting.
