# Implementation status

The [2026-09-07 quality repair](evidence/quality-repair-20260907.md) records the current
CI/security findings and local fixes. Earlier command results below remain historical
evidence, not proof that the updated workflows have run on GitHub. Main CI passed after
the maintainer published the prior work; the strict image gate still blocks acceptance.

The non-AI runtime and operational corrections are implemented. Overall acceptance and activation
remain **BLOCKED** by the strict image-security gate: each final local image has 54 OS package
findings (51 HIGH/3 CRITICAL). The settled canonical `make check` passed end to end; completed evidence
and exact commands are recorded in [the verification ledger](evidence/non-ai-architecture-verification.md).
A build or focused test pass must not be substituted for an outstanding acceptance gate.

## Implementation and verification surfaces

| Surface | Implemented behavior | Executed evidence |
| --- | --- | --- |
| Configuration/security | Immutable profiles/universes, canonical hashes, UTC/Decimal contracts, private secret files and scoped roles | Freeze checks; static/type/security and negative-boundary tests |
| Collection/data | Fixed-series adapters, canonical corrections, fenced checkpoints, exact aggregates, durable gaps and readiness; actual fixture ingest/aggregate/freeze CLI | Socket-denied tests; real PostgreSQL integration; final image public CLI creates 4,290 minute rows and 208 dataset rows |
| Datasets | Causal bounded snapshots, immutable Parquet/manifests, lineage, registration and metadata policy | Late-observation, corruption, restart and artifact-publication tests |
| Operational persistence | Alembic head 0015, audit, risk/execution stages, jobs/outbox, scoped views and roles | 160 PostgreSQL tests at completed checkpoint; restore verifies data, audit and effective privileges |
| Service runtime | Current-clock scheduler, AlwaysFlat strategy, role-separated shadow and bounded durable workers | SQL integration, real SQLite job/outbox composition, lifecycle/health failure tests |
| Risk/paper execution | Signed risk, immutable intents, two-stage reversals, reconciliation/accounting, forced flatten and actual fill-activity evidence | Broker protocol, ambiguity/cap, atomic rollback and recovery tests; external provider not exercised |
| API/dashboard | Authenticated bounded control routes, read-only UI client, operator controls and authoritative metrics | API/client tests; local fixture browser check; metric projections/corrupt-state SQL tests |
| Operations/harness | Canonical Makefile, packaging, regression/demo, benchmark, security/hooks, CI and isolated runtime images | All three images build; final scan evidence matches image configurations; canonical full pass in ledger |

Default Compose includes PostgreSQL/bootstrap/migration, API, dashboard and offline workers.
Optional market-data and paper profiles isolate external credentials. Paper submission remains
disabled, and the frozen model-approval contract denies authorization. Durable health/readiness
and source presence do not override that denial or the image-security blocker.

## Coverage and evidence discipline

The repository floor remains 74% and platform floor 85%. Reports now use two-decimal precision;
84.56% cannot pass by rounding up. Additional substantive failure tests raised fresh platform coverage to 85.69%; repository
coverage is 82.19%. The final run passed 3,039 offline tests and 160 PostgreSQL tests.
There are no production coverage exclusions added for difficult adapter or error paths.

The [traceability index](evidence/non-ai-requirement-traceability.md) covers all 173 unique IDs.
Earlier requirement status tables describe historical milestones; current acceptance is governed
by executed results and explicit blockers in the final ledger, not by labels on old rows.

## Genuine limitations and external validation

The unresolved base-image advisories block activation. The fixed paper facade also rejects a
rounded broker average when it differs from exact fill evidence; no undocumented tolerance is
assumed. That precision limitation fails closed and is documented in operations.

Alpaca data authentication/entitlement, paper account/order access, hosted PostgreSQL/TLS,
deployment and remote GitHub workflow execution were intentionally not performed. Implemented
adapters remain `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`; this label does not hide a failed local
contract. No real credentials, provider requests, remote activation, commit or push were used.

## Frozen and excluded scope

Main AI is **OUT_OF_SCOPE_FROZEN_AI**. All 61 protected file hashes, inventory and dependency
fingerprints match the inherited working-tree baseline. Training, features, labels, research
strategies, models, promotion and approval behavior remain unchanged. The model approval gate
cannot authorize paper submission in this frozen state.

Real-money trading, public/multi-tenant hosting, paid/cloud provisioning, Kubernetes and release
publication remain outside scope. The offline demo is permanently labeled
`OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE`; test outcomes do not establish market performance.
