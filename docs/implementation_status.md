# Implementation status

## Public product — September 14, 2026

The public multi-user product is `PARTIALLY_IMPLEMENTED`, not public-launch-ready.
The new declarative strategy contract, target evaluator and local validator have 48
passing focused tests. They have no execution authority. The local React workspace now uses verified Keycloak
identity and PostgreSQL ownership to save and inspect immutable versions, including safe
retries after lost responses. See [slice evidence](evidence/public-workspace-20260914.md)
and [startup instructions](../apps/public/README.md). Paper-account OAuth, ownership and
disconnect now have [offline/integration evidence](evidence/public-paper-connections-20260914.md);
actual provider compatibility and entitlements remain unvalidated. Explicit version/account/limits
review, signed confirmation and revocation now have [approval evidence](evidence/public-approvals-20260914.md).
Approval does not start execution. Public identity lifecycle, independent account-wide risk,
execution, reporting and operating requirements are tracked separately in
[the active plan](execution-plans/multi-user-paper-platform.md). Private-platform evidence
below must not be read as proof that the public release is complete.

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

Local activation on 2026-09-10 exposed a migration-cleanup defect: schema upgrades
completed, but deployment experiment registration lost its INSERT grants. The local
repair preserves the existing revision-0010 append-only exception. Live data validation
remains pending; schema migration alone does not establish successful activation.
The rebuilt local image subsequently completed migration successfully: database revision
0015 and one registered experiment were verified. The repair passed 90 PostgreSQL
migration/role tests, including fresh registration and idempotent retry.

On 2026-09-10, an isolated authenticated Alpaca IEX historical sample passed storage,
graceful restart, replay and aggregate checks (17,325 minute bars, 29 symbols). See
[evidence](evidence/alpaca-historical-validation-20260910.md). This narrow external
validation leaves streaming, full-history availability and research readiness pending.

Local hardening on 2026-09-12 verified the complete default offline Compose session and
restart with restricted PostgreSQL logins, plus full-platform logical restore. Revision
0016 exposes metadata-only startup compatibility checks. See
[evidence](evidence/local-hardening-20260912.md). Unattended production readiness remains
`PARTIALLY_IMPLEMENTED`; live streaming, archive capacity and provider precision remain
unverified. These changes are local and unpublished.

The 2026-09-13 continuation adds verified in-process history reuse, transactional
projection invalidation (revision 0017), populated archive measurement and an explicit
IEX receipt verifier. Local implementation evidence is recorded in
[evidence](evidence/local-hardening-20260913.md). IEX authentication/subscription and
the provider's synthetic test stream worked on Sunday; real minute-bar receipt and
persistence after reconnect remain `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`. The entire
system remains `PARTIALLY_IMPLEMENTED` for unattended production readiness.
