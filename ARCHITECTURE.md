# Architecture

## Public-product continuation

The [public-product plan](docs/execution-plans/multi-user-paper-platform.md) governs the
new multi-user release. `src/adaptive_trader/public_product` holds bounded non-AI strategy
contracts and a current-observation target evaluator. It reuses platform canonical hashing
and cannot authorize or submit orders. It does not change the backtester or frozen AI.
The public identity/web boundary is being developed separately from the private services
described below; those services must not be exposed as customer APIs.

The repository contains a preserved legacy research/paper prototype and a separate non-AI
platform implementation under `src/adaptive_trader/platform`. The current directive completes
repository code, tests and activation configuration; it does not authorize deployment or provider
calls. The main AI remains `OUT_OF_SCOPE_FROZEN_AI`, protected by the 61-file hash manifest.

This document describes inspected source, not a claim that every current integration check passed.
The final combined verification and current execution review remain pending in
[the active plan](docs/execution-plans/platform-core.md).

## Component ownership

| Boundary | Implementation and durable owner | Authority |
| --- | --- | --- |
| Provider collection | `collection/alpaca.py`, `service.py`, `postgres.py` | Dedicated data credentials; fixed Alpaca data hosts; fenced PostgreSQL `market_data` state |
| Canonical data | `collection/canonical.py`, `derived.py`, `recovery.py`; platform data/storage modules | Raw observation lineage, selected canonical revisions, queued derived sessions, gaps and watermarks; no trading authority |
| Immutable datasets | `collection/snapshots.py`, `platform/data/datasets.py`, `storage/datasets.py` | Consistent bounded reads, verified local Parquet/manifests, transactional PostgreSQL registration |
| Scheduling and workers | `platform/scheduling`, `operational_scheduler.py`, `operational_strategy.py`, `worker_runtime.py`, `service_cycles.py`, `jobs` | PostgreSQL slots, leases, jobs/outbox; current-clock operational and deterministic offline cycles |
| Strategy boundary | Existing frozen research plus declarative platform signal contracts | Proposals only; no credentials or broker authority; protected AI is unchanged |
| Risk and execution | `platform/risk`, `platform/execution`, `storage/execution.py` | Independent risk, durable intent, authorization, fake/paper adapters, reconciliation and accounting; current review corrections pending final checks |
| Private control | `platform/api`, `control`, `runtime.py` | Authenticated bounded read/control API with restricted database role |
| Dashboard | `platform/dashboard` | Read-only API client; legacy `app.py` remains a separate SQLite compatibility view |
| Operations | `platform/observability`, CLI, scripts and Compose | Redacted health/metrics, guarded backup/restore, package checks and deterministic benchmark/demo |

## Data flow and persistence

```text
fixed provider data hosts -> validated observations -> canonical revisions
-> durable derived work -> exact aggregates/gaps/readiness -> decision slots
-> declarative signal -> independent risk -> persisted order intent
-> selected fake/paper adapter -> fills/events -> reconciliation/audit
```

PostgreSQL is the platform operational authority, including collector state, scheduling,
risk/execution, jobs, audit and dataset registration. SQLite is restricted to legacy/offline and
isolated tests. Alembic owns production schemas; runtime startup validates schema instead of
creating it. A separate migration owner provides DDL authority.

Collector observation/projection writes mirror canonical winners and enqueue derived work within
fenced transactions. Checkpoints advance only after covered data is durable; overlap safely
replays interrupted work. Derived drains serialize consumers and check generation/configuration/gap
fences before publishing readiness. Missing minutes are explicit gaps and never fabricated.
Research readiness includes required-symbol coverage, stale data and pending work.

Snapshot reads use bounded consistent PostgreSQL transactions and effective revision lineage.
Parquet bytes and canonical manifests are immutable and verified on readback. Publication before
registration may leave a retryable content-addressed orphan. Creation cannot precede the dataset
range or selected observation receipt times. Unknown listing/corporate-action evidence requires a
non-promotable diagnostic export. A frozen current-selection dataset is not automatically an
as-of historical replay; consumers must enforce their decision cutoffs.

## Runtime modes and credentials

`aqa` and `autonomous-quant-agent` expose platform commands. `adaptive-market-data` and the
collection module expose the canonical collector. `aqa data collect-once` resumes durable
PostgreSQL history, reconciles overlap/older gaps, drains derived work and exits; it requires no
persistent runner filesystem. The opt-in GitHub workflow runs that same path. Delayed or missed
workflow invocations change latency, not checkpoint authority. One-shot coverage success is
separate from daemon stream liveness and research readiness.

Default Compose includes PostgreSQL/bootstrap/migrations, private API, dashboard, job worker,
market-data fixture worker, scheduler, strategy boundary worker and fake execution worker.
The optional `market-data` and `paper` profiles isolate external data and paper credentials.
Tracked paper submission is disabled. Worker code exists; final lifecycle and paper-runtime
verification remain pending. Separate database roles and credential mounts constrain each process.
The API binds privately in Compose; public hosting/multi-tenancy are outside this assignment.
Operational scheduler/strategy workers also consume configured shadow/paper profiles: the scheduler
uses current calendar sessions and the strategy boundary produces only the built-in AlwaysFlat
baseline. Migration `20260906_0015` exposes pending-work/gap readiness without collector write
authority. Separate shadow proposal/execution commands enforce strategy/execution database roles
and persist ordinary signed-risk/planner evidence without broker construction. Their final
PostgreSQL integration evidence remains in the active plan.

Canonical credentials enter through validated dedicated `AQA_*_FILE` references and the hardened
secret loader. Legacy `APA_*` compatibility remains explicitly separate. Non-loopback PostgreSQL
requires verified TLS and rejects connection-routing overrides. Real-money endpoints are forbidden.

## Recovery and security invariants

1. Collection membership never grants research or execution authority.
2. Strategy output cannot bypass risk, persisted intent or execution authorization.
3. Ambiguous broker submission blocks retry/new exposure until reconciliation.
4. Duplicate events cannot double count fills or create duplicate economic actions.
5. Gaps, stale data, lease loss and malformed inputs fail closed at their owning boundary.
6. Observation availability and immutable lineage constrain deterministic replay.
7. Ordinary tests and demos deny provider networking; PostgreSQL integration requires explicit
   disposable loopback database and cluster guards.
8. Secrets, database URLs, authorization headers and raw provider payloads stay out of logs/artifacts.
9. Protected AI hashes must remain unchanged throughout non-AI implementation.

## Evidence and limitations

The private API/dashboard, persistent workers, Parquet datasets, explicit canonical gaps,
metrics, benchmark and full-check harness are implemented source surfaces, not missing scaffolds.
Their complete current acceptance is tracked in [implementation status](docs/implementation_status.md).
Fresh PostgreSQL, container, security scan and combined harness results must be recorded when run;
older evidence cannot certify concurrent changes. No actual Alpaca authentication, paper order,
remote GitHub workflow execution, hosted database activation or external deployment is claimed.

See [requirements](docs/requirements.md), [tooling](docs/tooling.md),
[security model](docs/security-model.md),
[security architecture](docs/security_architecture.md),
[threat model](docs/threat_model.md), [ADR index](docs/adr/README.md), and
[market-data runbook](docs/market_data_runbook.md).

Local runtime verification (2026-09-12) exercised the default offline Compose pipeline
under its separate PostgreSQL logins through complete fake execution and restart.
Migration 0016 provides a metadata-only schema-version view for worker startup; scheduler
readiness uses the existing safe operational view. Historical expected intervals are
streamed in daily batches, while readiness still scans the configured archive range.
See [local hardening evidence](docs/evidence/local-hardening-20260912.md); this does not
establish a live-data strategy pipeline or unattended production readiness.

## Verified history reuse (2026-09-13)

The collector keeps an in-process readiness hash prefix through the previous UTC day.
It reuses that prefix only when the earliest queued session is not older and historical
gap fingerprints are unchanged. A cold start or an older correction rebuilds the full
verified digest in daily batches. Publication still checks the original generation,
configuration and gap fence. Prefix state is never serialized or accepted as input.

Revision `20260913_0017` queues changes to external minute and aggregate projections
inside their transaction, including direct collector-role writes and replacement of
external provenance. Aggregate creation can advance the generation and require a
second idempotent drain; duplicate materialization does not create another revision.
The trigger uses existing caller privileges and a fixed search path. See the
[dated measurements](docs/evidence/local-hardening-20260913.md) for capacity limits.
