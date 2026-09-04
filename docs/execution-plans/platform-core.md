# Platform core execution plan

Status: `PARTIALLY_IMPLEMENTED`

Active branch: `feature/market-data-platform`

Starting commit: `5690205b892053ea5fa12d7d590638a92003ff2c`

Baseline date: 2026-09-03

## 1. Project objective

Evolve the existing research and paper-simulation repository into a self-hosted,
single-operator platform with a generic, typed, deterministic core for market data, durable
scheduling, signed proposals, independent risk, paper-only execution, reconciliation, audit,
operator controls, and offline demonstration. Preserve the legacy backtest, replay, and paper
safety behavior while adding the new path.

## 2. User-visible outcome

A user can install the package, validate configuration, start a credential-free offline stack,
run the same synthetic vertical slice twice with identical logical evidence, inspect safe status
through a private API and read-only dashboard, and add a locally registered signal provider that
has no broker authority. Shadow mode records decisions and plans without submission. The paper
adapter exists behind independent default-deny gates but is not connected or credential-validated
in this program. Real-money execution does not exist.

## 3. Current repository state

The maintainer explicitly authorized continuation on `feature/market-data-platform`. The branch
started this program clean at commit `5690205`.

Implemented evidence at that commit:

- The legacy `adaptive_trader` package supplies historical backtesting, deterministic replay,
  observer and paper-only execution paths, risk controls, SQLite persistence, reporting, and a
  Streamlit dashboard.
- `adaptive_trader.collection` supplies a separate Alpaca IEX/raw one-minute REST/WebSocket
  collector, strict payload checks, PostgreSQL persistence, append-only observations, current-bar
  projection, checkpoints, leases, reconciliation overlap, and bounded shutdown.
- Alembic creates the current `market_data` schema. It is not the target `aqa_*` operational
  schema.
- CI uses locked dependencies, empty data and paper credentials, PostgreSQL 15, Ruff, mypy,
  pytest, synthetic backtest, replay, Compose validation, image builds, and a collector import
  boundary smoke test.
- The Dockerfile has nonroot locked application and collector stages. Current Compose defines
  collector, trader, dashboard, and paper-trader services, not the target separated platform
  topology.
- The current dashboard publishes `8501:8501` on all host interfaces and reads mounted runtime
  and output state directly. The target binds loopback only and reads through the private API.

The Phase 0 current/target matrix is:

| Subsystem | Current evidence | Current limitations | Reuse plan | New work | Security implications |
| --- | --- | --- | --- | --- | --- |
| Package and commands | Typed `adaptive_trader` package and two console scripts | No generic platform namespace or new aliases | Preserve every legacy import and command | Add the exact nonempty platform tree and composed aliases | New modules must not collapse service authority or expose broker objects |
| Configuration and secrets | Strict legacy YAML plus validated collector settings | Direct `APA_*` secret values; no immutable experiment or profiles | Reuse validation patterns only | Add exact experiment/profile contracts, canonical hashes, file-backed secrets, and doctor | Reject unknown fields, unsafe files, generic SDK variables, and secret serialization |
| Market data | Tested REST/WebSocket collector, normalization, overlap reconciliation, leases, and shutdown | Fixed 29-symbol universe, predecessor schema, checkpoint is not contiguous readiness | Adapt proven transport and lifecycle behavior behind generic contracts | Add canonical revisions, calendars, gaps, aggregation, basket watermarks, and datasets | Collector remains data-only, fixed-host, bounded, and unable to import trading code |
| Persistence | SQLite legacy evidence plus PostgreSQL collector migration and transactions | No 25-table `aqa_*` schema, service roles, unified repositories, or audit chain | Preserve legacy/collector state and additive migration history | Add PostgreSQL 16 operational schema, grants, repositories, and SQLite test compatibility | Least-privilege roles, transactional invariants, append-only history, and tamper detection |
| Scheduling and signals | Legacy cycle orchestration and strategy decisions | No durable slots, leases, registered proposal contract, or signed envelope | Characterize only compatibility-facing behavior | Add deterministic slots, recovery, entry points, built-ins, envelopes, and default-deny approval | Strategy code receives no broker credential or execution authority |
| Risk and execution | Tested long-only risk, paper gates, simulated broker, order state, and reconciliation | Not the required signed/short-aware contract or exact paper gates | Preserve regression path; reuse only isolated invariants | Add signed statistics, constraints, latches, planning, fake broker, paper adapter, and recovery | Risk cannot be bypassed; intent precedes effect; ambiguity and stale state fail closed |
| Jobs and control plane | No target job/outbox or private API; dashboard reads local state | No bounded remote-control surface or API-only presentation boundary | Retain Streamlit presentation where practical | Add durable jobs/outbox, authenticated allowlisted API, and API-backed dashboard | API has no trade route or broker import; dashboard has no database authority |
| Observability and operations | Existing logs, reports, health checks, Compose, and CI | No unified redaction, bounded metrics, audit verification, separated services, or full scans | Keep useful checks and compatibility commands | Add structured redaction, metrics, audit, offline-first Compose, SBOM, scans, and recovery proof | No secret/high-cardinality leakage; nonroot services receive only required mounts and roles |
| Verification and engineering memory | 375-test offline baseline, a prior PG15 observation, backtest/replay hashes, CI, and design README | The earlier PG15 invocation was not retained; PG16 runtime is unavailable locally; target vertical slice and exact public docs are absent | Preserve reproducible baselines as regression gates; keep the PG15 observation as context only | Add risk-specific suites, socket denial, deterministic demo, benchmark, runbooks, and traceability | Evidence must be credential-free, side-effect-free, reproducible, and explicit about gaps |

Not yet present are the generic `adaptive_trader.platform` package, exact experiment/profiles,
secret-file interfaces, signed platform contracts, complete `aqa_*` schema and roles, aggregation,
basket watermarks, scheduler, jobs/outbox, private API, API-backed dashboard, unified audit chain,
offline vertical slice, PG16 restore proof, or the complete delivery/security harness.

## 4. Requirements being addressed

The authoritative normalized ledger is `docs/requirements.md`. This program addresses every
non-deferred requirement in these groups:

- governance and evidence: `REQ-GOV-*`;
- code quality and implementation discipline: `REQ-QUAL-*`;
- product, scope, compatibility: `REQ-FUNC-*`, `REQ-SCOPE-*`;
- architecture and trust: `REQ-ARCH-*`;
- configuration and secrets: `REQ-CONFIG-*`, `REQ-SEC-*`;
- data and persistence: `REQ-DATA-*`, `REQ-STORE-*`;
- scheduling, signals, risk, and execution: `REQ-SCHED-*`, `REQ-SIGNAL-*`, `REQ-RISK-*`,
  `REQ-EXEC-*`;
- jobs, API, UI, and observability: `REQ-JOB-*`, `REQ-API-*`, `REQ-UI-*`, `REQ-OBS-*`;
- operations and delivery: `REQ-OPS-*`, `REQ-CI-*`;
- public engineering surface: `REQ-DX-*`, `REQ-DOC-*`, `REQ-HARNESS-*`;
- tests, demo, recovery, and performance: `REQ-TEST-*`, `REQ-DEMO-*`, `REQ-PERF-*`.

## 5. Scope

- Add the generic platform package and exact immutable contracts.
- Add the flagship experiment as data, not domain logic.
- Separate service credentials and process/database authority.
- Build canonical market-data lineage, deterministic aggregation, gaps, and watermarks.
- Build durable decision scheduling and registered proposal boundaries.
- Build signed risk, latches, planning, fake execution, reconciliation, and forced flatten.
- Build bounded jobs/outbox, private control API, API-backed dashboard, audit, logs, and metrics.
- Build the separated offline-first Compose topology and delivery/security checks.
- Build a clean public command surface, documentation, examples, deterministic demo, recovery
  proof, and benchmark harness.
- Preserve the current collector and legacy path until compatibility tests prove a safe migration.

## 6. Explicit non-goals

- Model training/loading, feature or label work, strategy search, promotion statistics, and
  backtester changes beyond compatibility isolation.
- Real-money execution or a configurable trading endpoint.
- A credentialed external data or paper-broker validation run.
- Multi-user tenancy, public hosting, or custody of another operator's credentials.
- C++, Rust, Go, Java, Node.js, TypeScript, React, or Next.js.
- Kafka, RabbitMQ, Redis, Celery, Spark, Airflow, Kubernetes, Helm, Terraform, cloud resources,
  object-store services, OAuth, or public web deployment.
- Sandboxing locally installed Python extensions; they remain operator-trusted without broker
  credentials or execution authority.
- Publishing packages, images, releases, tags, remote branches, or pull requests without separate
  authorization.

## 7. Existing architecture

Two paths currently coexist:

1. The legacy application loads YAML into `AppConfig`, consumes synthetic/replay/external data,
   applies strategy/allocation/risk, persists SQLite evidence, and optionally reaches a fixed
   paper-only broker behind gates. The dashboard reads local persistence and artifacts.
2. The collection service loads `APA_*` settings, obtains an exclusive PostgreSQL lease, catches
   up completed IEX/raw minute bars over REST, streams bars and updated bars, periodically
   reconciles REST overlap, and atomically writes each observation batch with its current-bar
   projection. Queried-through checkpoints advance monotonically in a separate transaction,
   including after empty or partial responses; they do not prove contiguous readiness.

The paths share a repository and CI but not a canonical platform domain or complete PostgreSQL
operational model. This work adds a third, generic path and narrows compatibility adapters instead
of replacing either legacy path wholesale.

## 8. Proposed implementation

Implement in the exact Phase 0–10 order in Section 22. The design is additive:

- `platform/canonical.py`, `hashing.py`, `domain.py`, `config.py`, and `universe.py` establish one
  deterministic boundary for serialization, IDs, time, Decimal values, profiles, and experiment
  roles.
- `platform/security.py` owns redacted file-backed secrets. Each process receives only its own
  settings, database role, and mounts.
- `platform/storage` and Alembic own all `aqa_*` tables and atomic repositories. PostgreSQL 16 is
  operational; SQLite supports isolated deterministic tests and demo convenience.
- `platform/data` normalizes fixture and provider events into append-only canonical revisions,
  aggregates 15-minute bars, persists calendar-aware gaps and contiguous watermarks, and freezes
  immutable Parquet artifacts.
- `platform/scheduling` creates deterministic deadline-bound slots and leases them durably.
- `platform/signals` discovers only locally registered entry points and emits strict envelopes.
- `platform/risk` independently validates freshness, identity, latches, statistics, and shrink-only
  signed constraints.
- `platform/execution` persists intent before broker calls, enforces close-first reversals and a
  strict state machine, and reconciles deterministic fake or gated paper state.
- `platform/jobs` uses PostgreSQL rows plus a transactional outbox for bounded background work.
- `platform/api` exposes only the allowlisted private control/read surface. Streamlit becomes an
  API client.
- `platform/observability` provides centralized redaction, bounded metrics, and append-only audit
  verification.
- One locked image serves separated commands in Compose where practical; default services remain
  offline.

## 9. Alternatives considered

- Mutate legacy dataclasses and long-only algorithms: rejected because signed semantics would
  silently change tested research behavior.
- Treat `adaptive_trader.collection` as the final platform boundary: rejected because its
  29-symbol universe, `APA_*` value credentials, table names, and readiness model conflict with
  the required experiment, secret, schema, and basket contracts. Its tested normalization,
  transport, lease, retry, and shutdown logic remains a reuse candidate.
- Replace the repository wholesale: rejected because it risks regression loss and obscures
  compatibility.
- Use a separate message broker: rejected because PostgreSQL transactions, leases, jobs, and an
  outbox satisfy the current single-operator workload with fewer failure domains.
- Let Streamlit query the database: rejected because it would grant presentation code persistence
  authority and duplicate read contracts.
- Pass secrets as ordinary environment values: rejected for the new platform because file mounts
  provide clearer per-process authority and avoid accidental serialization.
- Add native code, a separate frontend stack, or cloud orchestration now: rejected because no
  measured bottleneck or stable public API justifies the added build and security surface.

## 10. Why the selected design is preferable

An additive generic package keeps legacy regressions meaningful while making signed behavior
explicit. Immutable contracts and deterministic hashes permit restart/replay comparison.
PostgreSQL transactions align state and outbox changes without another service. Separate roles,
processes, secret files, and import checks make the least-authority boundaries mechanically
testable. A fake-broker offline slice tests the complete control flow without external side
effects. The design introduces only dependencies already required by a concrete external or
persistence boundary.

## 11. Architectural boundaries

```text
external market payloads
  -> data collector (collector database; data credential only in live-data service)
  -> canonical events / gaps / watermarks / datasets
  -> scheduler (scheduler database; no Alpaca credential)
  -> strategy worker (strategy database; proposal only)
  -> risk engine (non-bypassable)
  -> execution and reconciliation (execution database; paper credential only in paper service)
  -> paper adapter (default unreachable)

operator -> private API (operator token, safe reads and bounded jobs/control only)
dashboard -> private API (API token; dashboard client implements safe reads only)
private API -> PostgreSQL (scoped control database)
```

The collector cannot import execution or a trading client. Scheduler has no Alpaca or broker
credential. Strategy cannot import broker/execution services. API has no broker route or Alpaca
data/paper credential; its scoped database credential and operator token do not confer direct
trading authority. Dashboard has no database driver or mutation client surface. Execution cannot
load extensions or model artifacts. Domain code does not depend on web or ORM types. These rules
will be enforced by imports, database grants, container mounts/networks, strict schemas, and tests.

## 12. Data-flow changes

The platform path changes stored data flow to:

```text
fixture or fixed-host provider payload
  -> shared validation and canonical bytes
  -> append-only bar event + latest projection in one transaction
  -> gap and contiguous symbol watermark
  -> deterministic 15-minute aggregate revision + active-basket watermark
  -> immutable decision context and durable slot
  -> signed signal envelope
  -> signed risk receipt
  -> execution plan and durable intents
  -> fake or gated paper adapter
  -> order/fill events and reconciliation
  -> hash-chained audit and safe API read models
```

Parquet freeze reads approved canonical projections and writes a staging file, immutable artifact,
and manifest with logical and physical lineage. API jobs carry routing identity, not authority or
raw paths/URLs/code.

## 13. State-ownership changes

| Owner | State it may mutate | State it must not mutate |
| --- | --- | --- |
| migration process | schema and grants | runtime business state |
| collector | experiments/security reads; bars, gaps, watermarks, datasets, audit writes | risk, control, orders |
| scheduler | slots and audit | broker, signal internals, orders |
| strategy | signals and audit | risk, latches, orders, schema |
| execution | risk/latches/plans/orders/fills/reconciliation/incidents/audit | schema, experiments, provider data |
| control API | bounded jobs/outbox and halt/resume events | orders, fills, raw market data |
| dashboard | no persistent state | all database and broker state |

Append-only tables own history; versioned projections own current state. Repository operations own
transactions. Workers own leases only for bounded durations. External adapters never own domain
state.

## 14. Public API or schema changes

- Add `aqa` and `autonomous-quant-agent` console aliases while preserving
  `adaptive-portfolio-agent` and `adaptive-market-data`.
- Add immutable public `SignalProvider`, `DecisionContext`, `SignalEnvelope`, experiment, market
  data, risk, and execution contracts under the platform namespace.
- Add explicit schema-versioned YAML profiles and experiment configuration.
- Add 25 `aqa_*` tables listed in `docs/requirements.md`; migration is additive and does not
  destructively downgrade core state.
- Add the exact private FastAPI route allowlist from `REQ-API-002`. Only health is anonymous;
  mutations create jobs or latch events, never broker calls.
- Add deterministic artifact, evidence, audit, slot, signal, decision, plan, and order identities.

Any future breaking public contract follows semantic versioning and the documented deprecation
window. ORM rows remain private to storage modules.

## 15. Security implications

New attack surfaces include YAML and secret files, provider payloads, PostgreSQL, Parquet/JSON
artifacts, package entry points, bearer-authenticated HTTP, Streamlit, container networks, and
future paper-adapter responses. Controls are strict schemas and size bounds; fixed provider hosts;
path confinement; no uploads/arbitrary URLs/code/commands; redacted secret wrappers; service roles;
constant-time tokens; rate limits; intent-before-effect; idempotent IDs; stale-data and ambiguity
latches; append-only audit; nonroot read-only containers; socket-denied tests; and dependency,
secret, static, and image scans.

Every natural change reviews the threat model, updates it when assets, boundaries, inputs,
authority, persistence, or side effects change, and answers the required security review. Any
malformed, stale, missing, ambiguous, mismatched, or unauthorized input fails closed. No real
credential is read and no provider or broker connection is made during this program. Installed
third-party signal packages remain a documented local-code residual risk even without broker
authority. Compose network separation does not claim to be an outbound firewall.

The exact secret inventory defines one operator token while the authenticated API also exposes
bounded job and halt/resume mutations. If the dashboard receives that same bearer authority, a
compromised dashboard could invoke those routes even though its client implements reads only.
Phase 7 must resolve server-enforced read-only dashboard authorization within the exact secret
contract, or record a specification conflict and residual risk before `REQ-ARCH-007` and
`REQ-UI-001` can be verified.

## 16. Performance implications

Potential hot paths are canonical event normalization, transactional minute ingestion, aggregate
revision materialization, watermark computation, slot claiming, covariance/risk calculation, and
order/reconciliation event processing. Work, retries, pages, queues, labels, and waits remain
bounded. Indexes follow observed lookup/claim/idempotency patterns. Batch persistence is allowed
only when it preserves event and watermark atomicity.

The benchmark harness records deterministic inputs, warmups, repeats, environment metadata, and
machine-readable results. It does not impose unstable wall-clock CI gates. No native optimization
is considered without a repeatable profile showing a concrete bottleneck.

## 17. Migration or compatibility strategy

Use additive migrations and a separate platform package. Do not rename `adaptive_trader`, copy
legacy modules wholesale, or mutate legacy long-only models. Keep current commands and tests
passing. Reuse current collector algorithms only after adapting them to generic experiment,
canonical, secret-file, role, and table contracts with characterization tests.

Alembic upgrades both an empty PostgreSQL 16 database and the prior repository schema to head.
Core data downgrade is explicitly refused. SQLite remains isolated to legacy behavior, unit tests,
and offline demo convenience. Configuration is schema-versioned, explicit, and rejects unknown
keys. Dashboard migration changes its read source from SQLite/artifacts to the private API only
after API contract tests and browser validation pass.

## 18. Failure modes

- Invalid config, hash, role, secret file, symbol, nonfinite value, timestamp, or payload: reject at
  startup/boundary with safe context and no state change.
- Database or transaction failure: rollback all related history/projection/watermark/outbox state.
- Duplicate/out-of-order/corrected data: converge idempotently; unresolved intervals block
  contiguous readiness.
- Provider disconnect/rate limit: audit, retry on the bounded schedule, resume from durable state;
  never change feed.
- Missing active data or missed slot deadline: persist waiting/skipped/expired state and create no
  catch-up exposure.
- Unsupported early-close session: create no new-entry slot; separately configured forced-risk
  reduction fails closed when its safe timetable is unavailable.
- Worker crash or lease expiry: inspect materialized state before reclaim; never recompute an
  already materialized decision.
- Stale account/security/price/reconciliation state, latch, or unknown capability: no new exposure.
- Submission timeout after call begins: persist unknown state, do not retry, reconcile by client ID.
- Partial reversal close or failed reconciliation: block the opposite opening leg.
- Forced-flat uncertainty: persist a blocking incident and never report flat.
- Audit/artifact/hash corruption: fail verification and block dependent actions.
- API abuse: authenticate, bound size/page/rate, return safe errors, and expose no trade mutation.
- Shutdown: stop admission, cancel bounded work, reconcile where applicable, persist safe state,
  and exit without detached tasks.

## 19. Rollback or recovery approach

- Before external side effects, transaction rollback leaves no partial durable transition.
- After intent persistence, stable IDs and reconciliation recover without duplicate economic
  effects.
- Bar history, latch history, order/fill events, incidents, job attempts, outbox events, and audit
  events are append-only; corrections create new revisions rather than destructive rollback.
  Job rows are mutable versioned lease/state projections and may change only through guarded
  transitions.
- Expired job/slot leases are reclaimed only after authoritative-state inspection and within
  limits/deadlines.
- A logical PostgreSQL backup restores into a fresh database, migrates to head, and verifies row
  counts, hashes, audit, slots, intents, fills, and reconciliation.
- Immutable artifact IDs cannot be overwritten; a changed dataset receives a changed identity.
- A faulty additive runtime change can be reverted at the code/config level while retaining its
  schema and history. Destructive core-table downgrade is unavailable by design.

## 20. Test strategy

Use independently calculated known-answer unit tests for hashes, aggregation, statistics,
rounding, and scheduling; state-machine/property tests for normalization, constraints, order and
job transitions; SQLite component tests for deterministic behavior; PG16 integration tests for
migrations, grants, transactions, concurrency, claiming, and restore; API contract/security tests;
architecture/import tests; socket-denied adapter/demo tests; fake-broker failure and restart tests;
and a complete offline vertical slice twice.

The complete suite preserves all legacy tests and hashes. Tests use injected clocks and seeds,
never real credentials, live network, order-dependent state, or arbitrary sleeps. Branch coverage
cannot drop below 74%; new platform code targets at least 85% except explicitly justified external
adapter paths. Direct tests are required for every safety state, constraint, and gate independent
of percentage.

## 21. Acceptance criteria

Acceptance requires every non-deferred entry in `docs/requirements.md` to be
`IMPLEMENTED_AND_VERIFIED` or, only for prohibited credentialed adapters,
`IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`. In particular:

- legacy tests/backtest/replay and public commands remain stable;
- platform algorithms contain no flagship symbol constants;
- exact experiment roles, profiles, canonical events, corrections, aggregation, gaps, watermarks,
  migrations, roles, datasets, slots, signals, risk, signed transitions, reversal barrier,
  intent-before-effect, ambiguity, reconciliation, and forced flatten pass direct tests;
- default startup/demo is credential-free, offline, deterministic, and submission-disabled;
- API/dashboard and service processes meet import, credential, grant, path, URL, serialization,
  redaction, and route boundaries;
- CI, scans, SBOM, packaging, twice-run demo, backup/restore, Compose health, and public quickstart
  have executed evidence;
- no tests or quality gates are weakened, no required implementation is a stub, and final review
  has no unresolved critical/high finding.

## 22. Ordered milestones

1. **Phase 0 — Audit and baseline:** inspect the repository, establish branch and exact baseline,
   and map current versus target behavior before runtime edits.
2. **Phase 1 — Project boundary and secure configuration:** platform foundation, canonical rules,
   experiment/profiles, secret files/bootstrap, doctor, hygiene, initial security docs, CLI aliases,
   Python 3.11 typing, and tests.
3. **Phase 2 — PostgreSQL, migrations, repositories, and audit chain:** exact schema, roles,
   constraints, transactions, audit verification, PG16 and SQLite tests.
4. **Phase 3 — Canonical data, corrections, gaps, aggregation, watermarks, and datasets:** shared
   normalization, revision lineage, calendars, Parquet, fixture/provider boundaries, and an
   unconnected data adapter.
5. **Phase 4 — Durable scheduling and signal boundary:** slots, leases, provider entry points,
   strict envelopes, built-ins, and default-deny authorization.
6. **Phase 5 — Signed risk and latches:** statistics, constraints, freshness/eligibility,
   rebalance rules, durable latches, receipts, and tests.
7. **Phase 6 — Signed execution, fake broker, reconciliation, and forced flatten:** transitions,
   close-first reversals, IDs, durable intents, ambiguity, signed accounting, paper adapter without
   connectivity, recovery, and tests.
8. **Phase 7 — Jobs, API, dashboard, and observability:** outbox, private auth/routes/rates,
   API-backed Streamlit, logs/redaction, metrics, and boundary tests.
9. **Phase 8 — Containers and delivery/security gates:** separated Compose, credentials/networks,
   pre-commit, CI jobs, scans, SBOM, update configuration, and health tests.
10. **Phase 9 — Public developer experience and deterministic demo:** CLI/package/community docs,
    quickstart/example, twice-run demo, backup/restore, benchmark, and evidence.
11. **Phase 10 — Final adversarial review:** independent security, concurrency/recovery, and
    maintainability/API/testing reviews; fixes; full verification and traceability.

Each phase may contain multiple natural changes discovered from the implementation. No fixed
commit count is planned.

## 23. Progress checklist

- [x] `IMPLEMENTED_AND_VERIFIED` — branch choice authorized and starting commit recorded.
- [x] `IMPLEMENTED_AND_VERIFIED` — Phase 0 repository inspection and local baseline recorded.
- [x] `IMPLEMENTED_AND_VERIFIED` — requirements normalized into stable IDs.
- [x] `IMPLEMENTED_AND_VERIFIED` — execution-plan standard and 27-section active plan created.
- [x] `IMPLEMENTED_AND_VERIFIED` — decision-record authority, index, and template established.
- [x] `IMPLEMENTED_AND_VERIFIED` — stack-specific engineering, coding, testing, dependency,
  tooling, and review standards established.
- [x] `IMPLEMENTED_AND_VERIFIED` — ordinary pytest startup removes ambient Alpaca authority and
  forces paper submission off before test-module collection.
- [x] `IMPLEMENTED_AND_VERIFIED` — the current/target architecture and security map documents
  major-module state/interfaces/dependencies/failures, actual authority gaps, external inputs,
  controls, and residual risks; target mechanical boundaries remain Phase 1 implementation work.
- [ ] `NOT_IMPLEMENTED` — Phase 1 project boundary and secure configuration.
- [ ] `NOT_IMPLEMENTED` — Phase 2 platform persistence and audit chain.
- [ ] `NOT_IMPLEMENTED` — Phase 3 canonical data/dataset pipeline.
- [ ] `NOT_IMPLEMENTED` — Phase 4 scheduler and signal boundary.
- [ ] `NOT_IMPLEMENTED` — Phase 5 signed risk and latches.
- [ ] `NOT_IMPLEMENTED` — Phase 6 signed execution and recovery.
- [ ] `NOT_IMPLEMENTED` — Phase 7 control plane and observability.
- [ ] `NOT_IMPLEMENTED` — Phase 8 container and delivery/security gates.
- [ ] `NOT_IMPLEMENTED` — Phase 9 public offline vertical slice and recovery evidence.
- [ ] `NOT_IMPLEMENTED` — Phase 10 final reviews and traceability.

## 24. Decisions made

| Decision | Rationale | Status |
| --- | --- | --- |
| Continue on `feature/market-data-platform` from `5690205`. | Explicit maintainer direction preserves the committed collector work. | IMPLEMENTED_AND_VERIFIED |
| Build a separate signed platform path under `adaptive_trader.platform`. | Protects legacy long-only contracts and follows the required public namespace. | NOT_IMPLEMENTED |
| Treat the flagship basket as versioned configuration. | Keeps algorithms reusable and makes symbol authority hashable/testable. | NOT_IMPLEMENTED |
| Retain Python, `uv`, PostgreSQL/SQLite, Typer, and Streamlit; add the required FastAPI boundary. | Preserves the implemented stack while adding the specified private API; FastAPI is not currently installed. | PARTIALLY_IMPLEMENTED |
| Use PostgreSQL jobs/outbox, not a message broker. | Current workload needs transactional durability more than another distributed dependency. | NOT_IMPLEMENTED |
| Move dashboard reads behind the API. | Removes database authority from presentation code. | NOT_IMPLEMENTED |
| Use only file-backed secrets in new services. | Provides explicit service mounts and prevents settings serialization. | NOT_IMPLEMENTED |
| Keep paper submission unreachable by default. | Current legacy configuration disables submission; the target adds its independent default-deny verifier, and this program performs no connection. | PARTIALLY_IMPLEMENTED |
| Reuse collector behavior selectively rather than rename it into place. | Current symbol, secret, schema, and readiness contracts differ materially. | NOT_IMPLEMENTED |
| Use `docs/adr/` as the single decision-record authority. | Core specifies this path; the harness's conceptual `docs/design-decisions/` directory is omitted to avoid two mutable indexes. | IMPLEMENTED_AND_VERIFIED |
| Treat unsupported early-close sessions as entry-ineligible and fail closed for forced-risk reduction. | The fixed regular-session timetable cannot be assumed safe on a shortened session. | NOT_IMPLEMENTED |
| Resolve dashboard read-only authorization before implementing the private API. | The exact secret list has one operator token, while a shared bearer token could authorize control mutations after dashboard compromise. | NOT_IMPLEMENTED |
| Continue natural commits without a separate approval pause after each internal review. | After approving the first two boundaries, the maintainer explicitly delegated continued work on this feature branch and requested no further approval prompts; coherent boundaries, full diff review, configured identity, and verification remain mandatory. | PARTIALLY_IMPLEMENTED |
| Fast-forward reviewed feature-branch checkpoints without repeated publication prompts. | The maintainer explicitly requested uninterrupted continuation after authorizing the feature branch push; history rewrite, force-push, pull-request merge, and publication outside this branch remain excluded. | PARTIALLY_IMPLEMENTED |

## 25. Unexpected discoveries

- The starting branch already contains a substantial collector: the five
  `tests/test_collection_*.py` modules collect 102 cases, and the PostgreSQL integration module
  collects 13 more. Phase 3 should adapt proven transport, validation, lease, reconciliation, and
  shutdown behavior rather than reimplement blindly.
- The collector's fixed universe has 29 collected symbols, while the target experiment collection
  allowlist is only eight active plus SOXX/QQQ/SPY; the 18 excluded symbols must not flow into the
  new platform.
- Current collector secrets are direct `APA_*` environment values, while the new platform permits
  only file-backed `AQA_*_FILE` interfaces.
- The existing migration uses schema `market_data` and PostgreSQL 15 CI. The target needs additive
  `aqa_*` tables, roles, PostgreSQL 16 tests, and an upgrade path that does not discard collector
  history.
- The current dashboard reads SQLite/artifacts directly, conflicting with the required API-only
  boundary, and Compose publishes its port on all host interfaces rather than loopback only.
- The current project virtual environment is Python 3.14.3 and mypy currently targets 3.14 because
  installed NumPy stubs use newer syntax; runtime and CI are Python 3.11. Phase 1 must resolve this
  honestly without fake typing.
- The ordinary pytest command previously depended on the caller to clear ambient Alpaca variables.
  The engineering-standards boundary moves that denial into test bootstrap and retains per-test
  cleanup; universal process-level socket denial is still separate target work.
- Collector historical observation batches commit before coverage advances in a separate fenced
  transaction. A crash between them causes idempotent overlap replay; documentation must not call
  the two service-level steps atomic.
- Collector universe registration is content-addressed but does not verify an existing row's stored
  members and has no immutability trigger; runtime verification remains platform work.
- Docker Compose configuration validation passed, but the Docker daemon became unavailable before
  a fresh PG16/image runtime validation. This is unavailable evidence, not a product failure.
- The README is an extensive target design; code and tests, not that prose, determine statuses.

## 26. Validation evidence

Phase 0 command and evidence ledger for commit `5690205` on 2026-09-03; literal commands are retained where available:

| Command or evidence | Result |
| --- | --- |
| `uv sync --locked --extra dev --extra dashboard` | PASS — 105 packages resolved, 90 checked. |
| `uv run --no-sync ruff format --check .` | PASS — 92 files already formatted. |
| `uv run --no-sync ruff check .` | PASS. |
| `uv run --no-sync mypy src` | PASS — 44 source files. |
| `uv run --no-sync env APA_ALPACA_PAPER_API_KEY= APA_ALPACA_PAPER_SECRET_KEY= APA_ALPACA_DATA_API_KEY= APA_ALPACA_DATA_SECRET_KEY= APA_ENABLE_PAPER_ORDERS=NO pytest -q` | PASS — 375 passed, one PostgreSQL module skipped, one upstream WebSocket deprecation warning, 79.47 seconds. |
| Historical PostgreSQL 15 observation; exact invocation and run time were not retained | NOT_RUN as current verification — prior output reported 388 passing tests including 13 PostgreSQL tests; this is context only, not accepted evidence for PostgreSQL 16. |
| `uv run --no-sync env APA_ALPACA_PAPER_API_KEY= APA_ALPACA_PAPER_SECRET_KEY= APA_ALPACA_DATA_API_KEY= APA_ALPACA_DATA_SECRET_KEY= APA_ENABLE_PAPER_ORDERS=NO pytest --cov=adaptive_trader --cov-branch --cov-report=term-missing --cov-report=xml` | PASS — 375 passed, one skipped; 74% branch-aware total; 11,674 statements, 2,582 missed, 3,700 branches, 877 partial. |
| `uv run --no-sync python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic` | PASS. Bundle aggregate `6785a0d18516035cc916e169e049487ec5d9ee3e0ed913c36b2e35725ecac303`; report `ab401c5f926d25f5912ef9fb27e335aaacf2801296387608bbd7c990c297b4b1`; receipts `a8a09859c58844d977632aab802c203e43881ba221b1f0a11c20fa5876749740`. |
| `uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml` | PASS — 9 events, one cycle, three fake submissions; database hash `75d674da7760f58d5e6e3c36b6902325fd02bd97d340b4304832951a4e31cbde`. |
| `docker compose --env-file .env.example -f docker-compose.yml config --quiet` | PASS. |
| `git diff --check` before plan edits | PASS. |
| `docker info` | FAIL — the Docker daemon was unavailable; fresh PostgreSQL 16 runtime validation was therefore NOT_RUN. |

Approved natural-change evidence:

| Boundary | Commit evidence | Review evidence |
| --- | --- | --- |
| Normalized platform requirements | `89a00dd794fc25a903be0e9461fe33736480b897`; one new file; 608 insertions; SHA-256 `9838f5c90eb03a665250b0dfe6e7feb6de956056e61011678b28466e4e101e1e` | Maintainer supplied the exact authorization for `Document platform requirements`; author and committer timestamps are 2026-09-03 18:57:07 -0400; configured identity was used; no trailer, push, or pull request was added. |
| Platform execution plan | `94ba212608b91e883a120791db6cb6b3f1e294c7`; five files; 778 insertions and 21 deletions | Maintainer supplied the exact authorization for `Document platform execution plan`; author and committer timestamps are 2026-09-03 20:20:20 -0400; configured identity was used and no trailer was added. The maintainer separately authorized publication; local and `origin/feature/market-data-platform` heads both resolved to `94ba212`. |
| Published-branch verification | GitHub Actions run `33821556434` for exact head `94ba212608b91e883a120791db6cb6b3f1e294c7` | PASS at 2026-09-04 00:34:41 UTC — locked install, Ruff format/lint, mypy, PostgreSQL migration and full pytest, synthetic backtest, deterministic replay, Compose validation, both image builds, and network-disabled collector image checks all completed successfully. |
| Engineering standards and offline test isolation | `d2b287effc6f046617e3d979381da10b0bbe556e`; 11 files; 721 insertions and 16 deletions | Author and committer timestamps are 2026-09-03 20:49:14 -0400; configured identity was used and no trailer was added. The reviewed commit was fast-forward pushed and `origin/feature/market-data-platform` matched `d2b287e`. GitHub Actions run `33823350043` passed at 2026-09-04 00:55:49 UTC, including locked install, static checks, PostgreSQL migration/full tests, backtest/replay, Compose validation, both image builds, and network-disabled collector checks. |
| Initial architecture and security documentation | `165d4b978e3dc8b7f1759f42bc4825d0147e1678`; five files; 555 insertions and six deletions | Author and committer timestamps are 2026-09-03 21:01:25 -0400; configured identity was used and no trailer was added. A post-commit independent review found an incorrect service-level atomicity claim and incomplete current external-interface/component detail; the following documentation correction addresses those findings rather than treating this commit as final evidence. |

The exact five-file candidate was overlaid on a disposable archive of `89a00dd` and checked on
2026-09-03. The existing locked virtual environment was reused; no later worktree file was copied:

| Command or evidence | Result |
| --- | --- |
| `.venv/bin/ruff format --check .` | PASS — 97 files already formatted. |
| `.venv/bin/ruff check .` | PASS. |
| `.venv/bin/mypy src` | PASS — 44 source files. |
| `env APA_ALPACA_PAPER_API_KEY= APA_ALPACA_PAPER_SECRET_KEY= APA_ALPACA_DATA_API_KEY= APA_ALPACA_DATA_SECRET_KEY= APA_ENABLE_PAPER_ORDERS=NO .venv/bin/python -m pytest -q` | PASS — 375 passed, one PostgreSQL module skipped, one upstream WebSocket deprecation warning, 69.52 seconds. |
| `docker compose --env-file .env.example -f docker-compose.yml config --quiet` | PASS. |
| Ad hoc read-only candidate inspections | PASS — 142 unique seven-column requirements, 27 ordered plan sections, six ADR sections, three boundary-file local links, five-file whitespace, attribution, and private-material checks passed; three independent reviewers examined Core, Harness, and safety/maintainability scope. |

Engineering-standards candidate evidence on 2026-09-03:

| Command or evidence | Result |
| --- | --- |
| `env APA_ALPACA_DATA_API_KEY=sentinel-data-key APA_ALPACA_DATA_SECRET_KEY=sentinel-data-secret APA_ALPACA_PAPER_API_KEY=sentinel-paper-key APA_ALPACA_PAPER_SECRET_KEY=sentinel-paper-secret APA_ENABLE_PAPER_ORDERS=I_ACKNOWLEDGE_PAPER_ONLY .venv/bin/python -m pytest -q tests/test_offline_environment.py tests/test_collection_credentials.py tests/test_cli_modes.py` | PASS — 32 tests; test bootstrap removed the fake credential authority and forced submission off. |
| The same sentinel environment with `.venv/bin/python -m pytest -q` | PASS twice — 376 passed, one PostgreSQL module skipped, and one upstream WebSocket deprecation warning; the final exact-candidate run took 70.17 seconds. |
| `.venv/bin/ruff format --check .` | PASS — 125 files already formatted. |
| `.venv/bin/ruff check .` | PASS. |
| `.venv/bin/mypy src` | PASS — 44 source files. |
| `git diff --check` | PASS. |
| Initial `uv run --no-sync` format/lint/mypy attempts in the restricted process | FAIL before project tool execution — the process could not initialize the user-level uv cache; direct tools from the existing locked `.venv` then passed. |
| GitHub Actions run `33821556434` against preceding exact head `94ba212608b91e883a120791db6cb6b3f1e294c7` | PASS — all existing CI steps completed successfully at 2026-09-04 00:34:41 UTC. |
| Independent engineering-document review | PASS after corrections — test taxonomy, authority delegation, credential-free startup, coding rules, tool deferrals, and CI evidence were inspected against the appendix. |
| Independent test-safety and simplification review | PASS with no blocker — no check was weakened and the boundary remained coherent; residual limitations are clearing after plugin/conftest import, a current-name-only credential list, two guarded TCP paths rather than universal denial, duplicated sentinel inventory, and scrubbed environment persistence only for unusual in-process `pytest.main()` callers. |

Architecture/security-memory candidate evidence on 2026-09-03:

| Command or evidence | Result |
| --- | --- |
| `.venv/bin/ruff format --check .` | PASS — 125 files already formatted. |
| `.venv/bin/ruff check .` | PASS. |
| `.venv/bin/mypy src` | PASS — 44 source files. |
| `.venv/bin/python -m pytest -q` | PASS — 376 passed, one PostgreSQL module skipped, one upstream WebSocket deprecation warning, 75.95 seconds. |
| `git diff --check` and candidate link-target checks | PASS. |
| Candidate attribution, private-path, and credential-pattern scan | PASS — no match. |
| Corrective four-file `.venv/bin/ruff format --check .`, `.venv/bin/ruff check .`, and `.venv/bin/mypy src` | PASS — 125 files formatted, lint clean, and 44 source files type-checked. |
| Corrective four-file `.venv/bin/python -m pytest -q` | PASS — 376 passed, one PostgreSQL module skipped, one upstream WebSocket deprecation warning, 70.78 seconds. |
| Independent architecture and skeptical security/simplification re-reviews | PASS after correction — service-level checkpoint atomicity, legacy data adapters/credentials, module state/dependencies/failures, universe registration, and status language match the inspected implementation; no remaining finding. |

No real credential file was read, no external data or broker connection was made, and no order was
submitted during this baseline.

## 27. Final outcome and remaining limitations

Current outcome: `PARTIALLY_IMPLEMENTED`.

Phase 0 and the normalized requirements/planning slice are implemented with recorded evidence.
The existing collector and legacy regression suite provide reusable code and characterization, but
they do not satisfy the complete generic platform contract. Phases 1–10 remain as listed in the
progress checklist. Fresh PostgreSQL 16, container runtime, image scan, SBOM, clean-package
install, twice-run demo, and backup/restore evidence remain unavailable until their implementation
exists and the required services are available. GitHub Actions run `33823350043` passed for pushed
commit `d2b287e`; that run validates the existing PostgreSQL 15 and image boundaries, not the target
PostgreSQL 16 platform or any external adapter. External adapter credential validation is
intentionally prohibited in this program.

This section must be replaced with exact implemented outcomes, remaining requirement statuses,
final hashes, review findings, and external limitations after Phase 10; it cannot prove completion.
