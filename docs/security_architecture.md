# Security Architecture

## Purpose and status

This document defines the security boundaries for the current repository and the target generic
platform. It complements the operational threat discussion in [security-model.md](security-model.md),
the system map in [ARCHITECTURE.md](../ARCHITECTURE.md), and the normative requirement ledger in
[requirements.md](requirements.md).

The distinction between current and target state is mandatory:

- The legacy research/paper-simulation application and standalone market-data collector are
  executable today.
- The generic platform currently implements canonical serialization, hashing, immutable
  experiment/profile composition, service-scoped runtime-setting composition, hardened POSIX
  secret-file loading, local infrastructure-secret bootstrap, the additive platform schema,
  PostgreSQL authorization roles and safe views, atomic bar revision/symbol-watermark storage, and
  an append-only audit repository with a read-only verifier.
- The private API, API-backed dashboard, durable-job worker, offline collector, scheduler,
  strategy and fake-execution worker loops are implemented with bounded durable cycles and
  hardened Compose paths. Local and published main-branch CI include PostgreSQL 16 integration.
  The paper loop remains default-deny while Main AI approval is frozen. Operator-host deployment,
  real provider contracts and sustained operational behaviour require separate external evidence;
  the strict container vulnerability gate is still blocked.
- Alpaca-backed collector and legacy paper paths are
  `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`. Ordinary tests and CI do not contact Alpaca.
- Real-money execution is `INTENTIONALLY_DEFERRED` and prohibited by the supported product
  boundary.

Documentation is not an authorization control. A target design in this file must not be treated as
an implemented capability.

## Governing principle

Every component is untrusted until explicitly authorized, every external input is validated,
every privilege is minimized, every consequential action is auditable, and every ambiguous state
fails closed.

The practical consequence is that data collection does not confer execution authority, a strategy
proposal does not confer order authority, and an operator-facing HTTP process does not receive a
broker client merely because it can request bounded control-state changes.

## Critical invariants

These are normative target invariants. The current implementation enforces only the subsets named
in the capability and evidence matrices below; in particular, end-to-end redaction across every
service surface is still `PARTIALLY_IMPLEMENTED`.

1. No supported path creates a real-money order or constructs a non-paper trading client.
2. Ordinary tests, CI, and the default offline mode use no Alpaca credentials and make no Alpaca
   connection.
3. Collection membership is not research, strategy, risk, or execution authorization.
4. Only the active experiment's exact tradable-symbol set can reach order planning; benchmark,
   context, excluded, unknown, and aliased symbols cannot.
5. Strategy output is untrusted declarative data and cannot bypass independent validation, risk,
   planning, persistence, and reconciliation.
6. The execution/reconciliation process is the only target service permitted to receive paper
   credentials. It receives no data-provider credentials, plugin discovery, model loader, DDL
   authority, or generic trading hostname.
7. A durable order intent precedes every submission attempt. Ambiguous submission blocks new
   exposure and is resolved by deterministic client-order-ID lookup and reconciliation, never by a
   blind retry.
8. Missing, stale, malformed, nonfinite, mismatched, or internally inconsistent authorization
   input produces no new exposure.
9. Target services prevent secrets, database URLs, authorization headers, raw request/response
   objects, and credential-bearing exceptions from entering logs, metrics, API responses,
   artifacts, or audit payloads. Current verification covers the generic secret wrapper/settings
   boundary and a subset of legacy logging surfaces, not the complete target path.
10. Every target service receives only its explicit configuration, secret mounts, database role,
    network reachability, and persistent-state authority.
11. Consequential state is append-only or changed through validated state transitions; failures do
    not erase the evidence needed for replay, reconciliation, or incident analysis.
12. Tracked profiles keep paper submission disabled. An acknowledgement by itself is never
    sufficient authority to submit.

## Current topology

```mermaid
flowchart LR
    Operator["Local operator"] --> LegacyCLI["Legacy CLI"]
    Operator --> CollectorCLI["Collector CLI"]
    Operator --> PlatformCLI["Generic static CLI"]

    subgraph Legacy["Legacy application process boundary"]
        LegacyCLI --> LegacyData["Synthetic / replay / Alpaca data"]
        LegacyData --> LegacyDecision["Strategies / allocator / risk"]
        LegacyDecision --> LegacyExecution["Planner / fake or guarded paper adapter"]
        LegacyExecution --> SQLite["SQLite state and audit"]
        SQLite --> CurrentDashboard["Streamlit dashboard"]
    end

    subgraph Collector["Standalone collector boundary"]
        CollectorCLI --> FixedDataHosts["Fixed Alpaca data REST / IEX WebSocket"]
        FixedDataHosts --> Validation["Canonical bar validation"]
        Validation --> CollectorPG["External PostgreSQL market_data schema"]
    end

    subgraph Foundation["Generic platform foundation and persistence"]
        PlatformCLI --> StaticConfig["Strict config/profile validation"]
        PlatformCLI --> Bootstrap["Owner-private local secret bootstrap"]
        PlatformCLI --> AuditVerify["Read-only audit verification"]
        StaticConfig --> ScopedRefs["Service-scoped opaque secret references"]
        AuditVerify --> PlatformStorage["SQLite tests or PostgreSQL safe view"]
    end
```

The legacy paths remain available and separate: the legacy dashboard still reads SQLite directly.
The generic platform now has explicit CLI entrypoints and a Compose graph for its API, API-backed
dashboard, durable-job worker, database bootstrap/migration, offline collector, scheduler,
strategy, fake execution and profiled provider workers. The graph has configuration, durable-cycle,
PostgreSQL and selected offline container probes; it has not been validated as a continuously
deployed service graph on an operator host.

### Current capability matrix

| Current component | May access or modify | Prohibited or absent | Status |
| --- | --- | --- | --- |
| Standalone collector | Collector data credentials, fixed Alpaca data hosts, configured PostgreSQL, validated bars, runs, leases, checkpoints, and collector events | Paper credentials, trading SDK/client, order state, legacy execution, arbitrary URL, DDL during runtime | `IMPLEMENTED_AND_VERIFIED` with fakes and PostgreSQL integration; external provider operation is `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` |
| Legacy strategy/allocation | Completed history and legacy configuration | Direct credential access, direct broker mutation, ownership of risk policy | `IMPLEMENTED_AND_VERIFIED` within the legacy path |
| Legacy execution/paper adapter | Legacy paper credential object after explicit gates; SQLite order/fill/reconciliation state | Real-money client, configurable trading host, unsupported symbols, silent retry of ambiguous submission | `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` for Alpaca paper operation |
| Legacy Streamlit dashboard | Direct read access to legacy SQLite and report artifacts | Generic-platform state and application-level order mutation | `IMPLEMENTED_AND_VERIFIED` only for the preserved legacy path |
| Generic control API and dashboard | Strict private API, bounded safe read models and control jobs, server-enforced read-only dashboard bearer | Broker/provider credentials, direct database access from dashboard, direct trade mutation routes | `IMPLEMENTED_AND_VERIFIED` locally; deployed exposure remains external evidence |
| Generic durable-job worker | Control-role database URL, closed handler registry, bounded claims/outbox delivery, immutable artifact root, private heartbeat | Operator, provider, paper, strategy, or broker credentials; arbitrary handlers, URLs, or commands | `IMPLEMENTED_AND_VERIFIED` locally; deployed restart behavior remains external evidence |
| Generic config/runtime composition | Explicitly injected environment mapping, strict profiles, immutable experiment identity, closed service/secret scope, opaque references | Ambient environment reads, secret-file reads during composition, network/client construction, persistent mutation | `IMPLEMENTED_AND_VERIFIED` |
| Generic secret loader | One explicitly referenced current-user-owned POSIX regular file in mode `0400` or `0600`, up to 16 KiB | Symlinks, directories/special files, shared modes, NUL, empty or invalid UTF-8 content, serialization of values | `IMPLEMENTED_AND_VERIFIED` |
| Local secret bootstrap | Exact fixed local infrastructure inventory beneath an owner-controlled application root | Alpaca keys, arbitrary filenames, overwrite, value output, unsafe existing state | `IMPLEMENTED_AND_VERIFIED` |
| Generic platform schema and database authorization | Additive platform schema; seven non-login authorization roles and matching login principals; normalized grants, audit row policies, security-barrier views, and service-scoped Compose credential adoption | Final published verification of the current revisions | `PARTIALLY_IMPLEMENTED` pending final PostgreSQL 16 and published-CI evidence |
| Generic market-data repository | Append-only bar revisions, independently hashed provenance, a version-fenced latest projection, and an optional quality-approved symbol watermark in one serialized transaction | Calendar/gap eligibility, basket watermark calculation, aggregation, datasets, and collector-service integration | `PARTIALLY_IMPLEMENTED` relative to the complete data platform |
| Generic audit repository and verifier | Closed writer/stream/event-family contracts, per-stream serialized append, canonical hash-chain verification, scoped writer views, and read-only `aqa audit verify` | Emission from every consequential later-phase transition and centralized metrics/logging | `PARTIALLY_IMPLEMENTED` relative to complete observability |
| Current CI | Locked Python 3.11 quality/offline checks, PostgreSQL 16 integration, secret/dependency/static scans, twice-run offline demo, Compose validation, locked image builds, SBOMs, Trivy, and CodeQL | Published workflow and scan evidence for the current revisions | `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` |

The standalone collector still uses the legacy `APA_*` runtime namespace. The generic platform
secret interface below is a new boundary and does not retroactively make the legacy processes
service-isolated.

## Target topology

The target is a self-hosted, single-operator, paper-only topology. PostgreSQL transactions,
durable jobs, and a transactional outbox are used instead of a separate message broker.

```mermaid
flowchart TD
    DataProvider["Untrusted market-data provider"] --> MDLive["market-data-live\ndata credentials only"]
    Fixture["Deterministic fixture"] --> MDWorker["market-data-worker\no provider credentials"]
    MDLive --> PG["PostgreSQL 16\nrole-scoped state"]
    MDWorker --> PG
    Scheduler["scheduler-worker"] <--> PG
    Strategy["strategy-worker\noperator-trusted plugins, no broker"] <--> PG
    Executor["execution-worker\nfake broker only"] <--> PG
    PaperExecutor["paper-execution-worker\npaper credentials only"] <--> PG
    PaperExecutor --> Paper["Alpaca Paper only"]
    API["control-api\noperator auth, no broker"] <--> PG
    Dashboard["dashboard\nAPI client only"] --> API
    Operator["Local operator"] --> API
    Migrate["migrate\nschema owner only"] --> PG
    Postgres["postgres bootstrap"] --> PG

    Strategy -->|"signed declarative proposal"| PG
    PG -->|"validated proposal"| Executor
    PG -->|"validated proposal"| PaperExecutor
```

Required decision direction:

```text
external data -> canonical events -> durable readiness -> decision slot
-> untrusted signed signal -> independent signed risk decision
-> persisted order intent -> selected fake/paper adapter
-> broker events/fills -> reconciliation -> audit
```

The implemented default Compose invocation is entirely offline and starts the database, migration,
control API, dashboard, control-authority durable-job worker, offline data, scheduler, strategy,
and fake-execution workers. `market-data-live` exists only
under the `market-data` profile and the default-deny `paper-execution-worker` only under
the `paper` profile; neither starts by default. Tracked paper configuration keeps submission
disabled and the default authorization verifier denies with `model_approval_not_implemented`.

### Target process capability matrix

These rows define the target separation. The checked-in Compose graph currently enforces the
database, migration, API, dashboard, job-worker, live-data profile, and fail-closed paper-profile
subsets by configuration and local tests. A completed domain-worker deployment inventory and
host-level egress policy remain external operator evidence.

| Target component | Allowed capabilities | Explicitly forbidden capabilities |
| --- | --- | --- |
| `postgres` | Initialize private PostgreSQL state from database bootstrap secrets | Public network exposure, broker/data credentials, application execution |
| `migrate` | Act as the trusted deployment-only schema owner and apply versioned schema/grants with the migration database URL; ordinary business DML is self-revoked | Long-running application work, runtime-service assignment, broker/data credentials, order submission |
| `control-api` | Read safe views; create bounded jobs/outbox records; write audited halt/resume events | Provider or paper credentials, broker adapters, raw market-data download, strategy/plugin installation, secret return, order submit/cancel/replace/flatten routes, arbitrary path/URL/code/SQL/shell |
| `job-worker` | Claim bounded control-plane jobs, publish the transactional outbox, and write immutable artifacts using the control database role | Operator/provider/paper credentials, strategy or broker execution, DDL, arbitrary URLs or commands |
| `market-data-worker` | Read experiment metadata; write fixture bars, gaps, watermarks, datasets, and audit state | Alpaca credentials in offline mode, paper credentials, trading client, order/risk tables, risk-latch clearing, model/plugin installation, arbitrary URL, shell execution |
| `scheduler-worker` | Read calendars/readiness; claim and transition durable slots; write audit state | Any Alpaca credential, provider/broker adapter, strategy internals, arbitrary network access, order submission |
| `strategy-worker` | Read immutable decision/data views; load locally registered signal providers; write signed signals and audit state | Broker/paper credentials, DDL, risk mutation, API-selected imports, order mutation |
| `execution-worker` | Read approved signals/data/security metadata; run independent risk; write latches, intents, fake orders/fills, reconciliation, incidents, and audit | Alpaca credentials, plugin/model/research-code loading, database-superuser access, DDL, arbitrary network, real-money mode |
| `dashboard` | Call authenticated private API and render bounded safe read models | Database driver/role, filesystem secrets other than its API token file, Alpaca credentials, order/control widgets |
| `market-data-live` | Fixed Alpaca data REST/WebSocket; collector database role; validated experiment collection allowlist | Paper credentials, trading SDK/client, order/risk authority, arbitrary host, feed fallback |
| `paper-execution-worker` | Approved signed decisions, independent risk, execution/reconciliation database role, literal paper adapter, paper credentials and account hash | Data credentials, model/plugin loading, DDL, configurable trading host, `paper=False`, real-money mode |

Locally installed strategy providers are operator-trusted Python code. They are not sandboxed, but
the platform interface and process launch must still withhold broker objects, credentials, DDL,
and execution imports. Arbitrary untrusted Python sandboxing is `INTENTIONALLY_DEFERRED`.

## Secret architecture

### Exact generic-platform secret-file interface

The generic platform accepts only these seven secret-file variables:

```text
AQA_DATABASE_URL_FILE
AQA_OPERATOR_TOKEN_FILE
AQA_ALPACA_DATA_API_KEY_FILE
AQA_ALPACA_DATA_SECRET_KEY_FILE
AQA_ALPACA_PAPER_API_KEY_FILE
AQA_ALPACA_PAPER_SECRET_KEY_FILE
AQA_PAPER_ACCOUNT_ID_HASH_FILE
```

Secret values must not be accepted directly through environment variables.

### Rejected generic credential variables

The generic platform rejects the presence of exactly these generic SDK variables:

```text
APCA_API_KEY_ID
APCA_API_SECRET_KEY
APCA_API_BASE_URL
ALPACA_API_KEY
ALPACA_SECRET_KEY
```

There is no configurable Alpaca trading hostname; the future adapter must construct the SDK with
literal `paper=True`.

### Exact non-secret runtime interface

The only generic-platform non-secret environment variables and their defaults are:

```text
AQA_CONFIG=configs/platform/offline.yaml
AQA_ARTIFACT_ROOT=outputs/artifacts
AQA_API_BASE_URL=http://127.0.0.1:8000
AQA_LOG_FORMAT=json
AQA_ENABLE_PAPER_ORDERS=NO
AQA_API_DOCS_ENABLED=NO
```

The implemented secret loader descriptor-walks a bounded canonical POSIX path without following
symlinks, requires a regular file owned by the current effective user in mode `0400` or `0600`,
reads no more than 16 KiB, rejects NUL/empty/invalid UTF-8 content, removes exactly one terminal LF,
and exposes the result through a nonserializable wrapper whose `str` and `repr` are `<redacted>`.
Its ordinary errors identify the allowlisted variable and a stable reason but not the secret value
or path. Secret use still requires an explicit future adapter-boundary call to `reveal()`.

### Exact local bootstrap output

`aqa secrets bootstrap-local` creates or preserves exactly these nine files under the ignored
owner-only `secrets/` directory:

```text
postgres_password
aqa_migrate_password
aqa_collector_password
aqa_scheduler_password
aqa_strategy_password
aqa_execution_password
aqa_control_password
aqa_readonly_password
operator_token
```

The bootstrap uses cryptographic randomness, creates the directory as `0700` and files as `0600`,
never overwrites an existing valid file, serializes concurrent threads/processes with local and
advisory locks, and prints only relative filenames. It neither requests nor creates Alpaca keys.
It is POSIX-only and relies on current-user ownership plus effective advisory locking on the host
filesystem.

### Target secret mount and database-role matrix

This matrix defines the service mounts. The checked-in Compose graph implements the rows for
database bootstrap, migration, API, dashboard, job worker, four offline domain workers, live-data
profile, and the default-deny paper worker. Offline workers receive only their scoped database
credentials. The underlying
PostgreSQL authorization roles, login principals, schema grants, row policies, and safe views are
implemented and covered by PostgreSQL integration. Each database URL file contains a
role-specific login URL even though every process refers to it through the same
`AQA_DATABASE_URL_FILE` variable.

| Service | Target secret mounts | Target PostgreSQL authority |
| --- | --- | --- |
| `postgres` | Database bootstrap password files only | Server bootstrap; not an application role |
| `migrate` | `AQA_DATABASE_URL_FILE` containing the `aqa_migrate_login` URL | Trusted deployment-only schema ownership and grants; ordinary business DML is self-revoked |
| `control-api` | `AQA_DATABASE_URL_FILE` containing the `aqa_control` URL; `AQA_OPERATOR_TOKEN_FILE` | Safe views; bounded jobs/outbox/halt/resume; no order/fill writes |
| `job-worker` | `AQA_DATABASE_URL_FILE` containing the `aqa_control` URL; no operator, provider, or paper credential | Claim bounded jobs, publish outbox transitions, and write immutable job artifacts |
| `market-data-worker` | Offline `AQA_DATABASE_URL_FILE` for `aqa_collector` when PostgreSQL is selected; no Alpaca file | Read experiment/security metadata; write bars/gaps/watermarks/datasets/audit |
| `scheduler-worker` | `AQA_DATABASE_URL_FILE` containing the `aqa_scheduler` URL | Read readiness; write slots/audit |
| `strategy-worker` | `AQA_DATABASE_URL_FILE` containing the `aqa_strategy` URL | Read decision/data views; write signals/audit |
| `execution-worker` | `AQA_DATABASE_URL_FILE` containing the `aqa_execution` URL | Read approved signal/data/security state; write risk/latches/execution/orders/fills/reconciliation/incidents/audit; no DDL |
| `dashboard` | `AQA_OPERATOR_TOKEN_FILE` points only to the HMAC-derived read bearer in a read-only service volume | No database role, operator mutation bearer, or direct database network |
| `market-data-live` | `AQA_DATABASE_URL_FILE` for `aqa_collector`; `AQA_ALPACA_DATA_API_KEY_FILE`; `AQA_ALPACA_DATA_SECRET_KEY_FILE` | Same collector role; no execution state authority |
| `paper-execution-worker` | `AQA_DATABASE_URL_FILE` for `aqa_execution`; `AQA_ALPACA_PAPER_API_KEY_FILE`; `AQA_ALPACA_PAPER_SECRET_KEY_FILE`; `AQA_PAPER_ACCOUNT_ID_HASH_FILE` | Same execution role; no DDL |

The cluster bootstrap creates seven non-login authorization roles and seven corresponding login
principals; each login inherits exactly one authorization role. `aqa_migrate` owns the managed
schemas, tables, views, and routines. It retains ownership and grant authority even though its
ordinary business-table DML is explicitly self-revoked, so it is a trusted deployment boundary,
not a containment boundary, and no runtime service may receive it. `aqa_readonly` has `SELECT` only
on explicit security-barrier safe views. It is not assigned to the dashboard, which must use the
API. No service receives one shared credential bundle.

Cluster-global role creation is a separate one-time administrator operation. Fresh and already
governed databases migrate through `aqa_migrate_login`, which assumes `aqa_migrate`. A recognized
pre-governance database uses its validated sole legacy owner only through revision
`20260905_0004`: bootstrap grants one bounded non-inherited transition membership, the migration
transfers every managed object to `aqa_migrate`, cleanup revokes that temporary membership, and
later revisions reconnect through the migration login. Unknown revisions, unversioned managed
schemas, multi-head state, mixed owners, or unexpected memberships fail closed.

Audit access is deliberately asymmetric. Collector, scheduler, strategy, and execution writers
read only their actor/stream-scoped audit views, and row-level insert policies enforce their exact
actor, stream-prefix, and event-family contracts. Control and read-only authorization roles can
read the full `aqa_audit_events_v` and `aqa_audit_status_v` safe views. The read-only audit verifier
uses the full view and has no append path; PostgreSQL deployment must supply it the
`aqa_readonly_login` URL rather than a writer credential when service credentials are adopted.

## Authentication and authorization

### Implemented controls

The private FastAPI control plane binds `127.0.0.1:8000` by default, uses a bearer token loaded from
`AQA_OPERATOR_TOKEN_FILE`, requires at least 32 bytes, and compares tokens with
`hmac.compare_digest`. Cookies and CORS are disabled; interactive docs default off. Authenticated
responses use `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`, with a restrictive
content security policy where applicable. Read routes are limited to 120 requests/minute per token
and mutation routes to 10; this is loop protection, not distributed denial-of-service protection.

Only liveness/readiness are unauthenticated. Authenticated mutations are restricted to four
bounded job types and audited halt/resume events. No API route directly submits, cancels, replaces,
liquidates, or flattens an order.

The API implementation and route-scope behavior are executable-test verified. Binding beyond
loopback/private infrastructure, distributed rate limiting, and a completed remote deployment
remain operator responsibilities rather than properties of the application tests.

### Dashboard-scoped bearer resolution

The dashboard does not receive the operator mutation bearer. The control API derives a
domain-separated HMAC-SHA256 bearer from the loaded operator secret and recognizes that derived
value as `read_only`. Its entrypoint atomically writes the derived bearer to an owner-private file
in a dedicated named volume. The dashboard mounts that volume read-only and points its existing
`AQA_OPERATOR_TOKEN_FILE` interface to that derived file; it mounts no Compose secret and has no
database network.

API tests exercise every authenticated GET with the derived bearer and verify that all six POST
mutation routes return `403 insufficient_scope` without persisting a job or invoking latch control.
Container entrypoint tests reject unsafe permissions and symlinks, and a local offline container
probe verified that the dashboard view lacks `/run/secrets/operator_token` and cannot modify the
derived-token volume. Docker/host administrators remain in the deployment trust boundary.

## Input and code-execution controls

### Implemented boundaries

- Experiment and profile YAML use strict schemas, bounded no-symlink paths, no anchors/aliases,
  exact enum values, unknown-field rejection, and immutable models. Composed platform-profile
  loading requires the profile to pin the selected experiment hash; the lower-level standalone
  experiment loader accepts an optional expected hash for compatibility.
- Runtime settings read only an explicitly supplied mapping; reject unknown `AQA_*` names and
  generic Alpaca variables; validate service/mode combinations; and create no client, file, or
  network connection.
- Collector provider hosts are constants. Provider payloads undergo bounded decoding, exact symbol
  allowlisting, temporal/OHLCV validation, and canonical identity/hash construction before durable
  projection.
- SQL in the collector repository is parameterized and connection-routing override keys are
  rejected.
- Architecture tests constrain imports around generic configuration, collector trading authority,
  and prohibited platform capabilities.

### Target boundaries

- API and job schemas accept no arbitrary URL, filesystem path, raw SQL, shell command, environment
  variable, module/class/function path, Python source, broker credential, raw market-data transfer,
  strategy installation, or experiment replacement.
- Artifact APIs accept only IDs matching `^[a-z0-9][a-z0-9._-]{0,127}$`; storage rejects traversal,
  absolute/NUL paths, symlink components, root escape, and conflicting immutable overwrite.
- Platform runtime does not use `pickle`, `joblib`, `dill`, `cloudpickle`, `marshal`, untrusted
  `eval`/`exec`/`compile`, payload-driven subprocesses, or arbitrary dynamic imports.
- Parquet, JSON, and YAML artifacts receive size, schema, path, and content-hash validation. No
  upload endpoint exists.

The generic API, job, artifact, plugin-discovery, and signed-envelope input boundaries in this list
are implemented and locally tested. Deployment-level request filtering, process containment, and
host egress remain external controls.

## Network controls

### Current controls

- The standalone collector uses fixed official Alpaca data hosts, rejects proxy inheritance and
  endpoint overrides, and has no trading SDK import.
- Non-local PostgreSQL connections require `sslmode=verify-full`; loopback and the exact
  internal-Compose hostname `postgres` may be plaintext within the private database network.
- Current CI supplies empty Alpaca variables. Its collector container smoke test runs with
  `--network none`, while the offline pytest guard blocks common Python TCP connection paths and
  permits explicitly marked loopback PostgreSQL integration.
- Current Compose provisions private PostgreSQL, one-shot bootstrap/migration jobs, the API,
  dashboard, dedicated control-authority job worker, separated internal/provider networks,
  service-scoped secret mounts, four offline domain workers, and loopback-only published ports.
  Live data, the default-deny paper worker, and database debugging remain explicit profiles.

### Target controls

- Only `127.0.0.1:8000:8000` and `127.0.0.1:8501:8501` are published by default. PostgreSQL is not
  published except under an explicit `db-debug` profile bound to loopback.
- Internal networks limit dashboard-to-API, API-to-database, and worker-to-database reachability.
  PostgreSQL has no public route.
- Default services have no provider dependency. Live data and paper execution require distinct,
  explicitly selected profiles and disjoint credentials.
- Ordinary tests, CI, and the offline demo deny external sockets while permitting only required
  loopback/Unix integration.

Compose segmentation is not a general outbound firewall. The topology is configuration-tested but
not runtime-verified on a production host; operators remain responsible for host/cloud egress
policy.

## Persistence and integrity controls

### Current controls

- Collector PostgreSQL writes use transactions, parameter binding, constraints, selected immutable
  triggers, stable identities, monotonic checkpoints, singleton leases, and fencing tokens.
  Observation/projection persistence precedes checkpoint advancement, so a crash causes safe replay
  rather than optimistic readiness.
- Generic platform migrations add the exact 25-table `aqa` schema without removing predecessor
  collector history. Bar identities/events are append-only, the latest projection is version
  fenced, and an eligible symbol-watermark mutation shares the serialized bar transaction. Stored
  identities, normalized semantics, provenance, revision links, projections, watermark lineage,
  timestamps, and hashes are independently revalidated on reads.
- Cluster bootstrap creates fixed authorization/login pairs, rejects unsafe existing role state,
  removes unsafe public/default/direct privileges, and keeps runtime grants service-specific. The
  migration role is a trusted deployment-only owner: ordinary business DML is self-revoked, but
  ownership/grant authority is necessarily retained and never assigned to a runtime service.
- Generic audit events use append-only per-stream sequences and canonical hash chaining. Writer
  repositories are bound to closed actors and scoped PostgreSQL views/policies; the verifier reads
  the full safe view through a read-only engine and can compare one stream with an externally
  supplied expected head.
- Legacy SQLite persists decision identity, intent/order/fill transitions, incidents, latches, and
  reconciliation facts. Intent-first persistence and deterministic client IDs limit duplicate
  broker effects.
- Secret values are neither configuration-model fields nor PostgreSQL data. Local bootstrap values
  remain ignored owner-private files.

### Remaining target controls

- Experiment, dataset, signal, risk, plan, order, fill, reconciliation, job, and artifact identities
  are immutable and content-addressed where specified.
- Calendar/gap logic must decide which bars are quality-approved, active-basket readiness must be
  materialized, and every consequential later-phase transition must emit through the implemented
  audit boundary.
- Durable jobs and outbox events are created atomically. Claims use leases and PostgreSQL
  `FOR UPDATE SKIP LOCKED`; payloads route work but never grant authorization.
- Logical backup/restore must be tested into a fresh database and reverify versions, row counts,
  hashes, audit chains, slots, intents, fills, and reconciliation state.

The remaining atomic repositories, jobs/outbox behavior, artifact store, emission wiring, and
tested backup/restore procedure are `NOT_IMPLEMENTED`. The schema, roles/grants, atomic
bar/latest/symbol-watermark repository, and audit chain/verifier are implemented candidates whose
final current-revision PostgreSQL 16 and published-CI evidence is still pending.

## Failure, restart, and side-effect containment

- Current collector retries only classified transient provider failures with bounded backoff,
  records run/events, persists before advancing coverage, and uses leases/fencing to reject stale
  writers. Explicit gap production and contiguous active-basket readiness are `NOT_IMPLEMENTED`.
- Current legacy execution persists intent before submission, treats post-submit uncertainty as
  ambiguous, and requires reconciliation instead of automatic resubmission.
- The target scheduler uses durable slots, transactional claims, 30-second leases, fixed deadlines,
  and no catch-up trading after a missed deadline. Materialized decisions are never recomputed.
- Target order state includes `SUBMISSION_UNKNOWN` and `RECONCILIATION_REQUIRED`; either blocks new
  exposure. Reversal closes and proves flat before an opposite opening leg is considered.
- Blocking or critical reconciliation discrepancies engage an append-only latch. Session-loss and
  deployment-drawdown latches survive restart and require authenticated, explicitly acknowledged
  clearing.
- Forced flatten must create a blocking incident when flat state cannot be proved by the deadline;
  it must not report success from an unverified broker state.
- Target workers restart from authoritative durable state, replay idempotently, and prefer a
  durable safe failure over an inferred transition.

The target slot/signal/risk/execution/job/reconciliation/flatten implementation is
`NOT_IMPLEMENTED`. Operational response must follow
[incident_response.md](incident_response.md) without deleting or rewriting adverse evidence.

## Logging, audit, metrics, and evidence

Current legacy logging applies structural/value redaction and the collector records bounded event
facts. The generic audit repository, hash verifier, safe views, and CLI verification command are
implemented, while the centralized logging/metrics layer and emission from all platform services
remain incomplete.

The target requires:

- structured JSON with bounded identifiers, state transitions, and stable reason codes;
- central structural redaction for keys containing `secret`, `password`, `token`, `authorization`,
  `credential`, `api_key`, `private_key`, `cookie`, `connection_string`, or `database_url`;
- append-only per-stream audit chaining with no secret-bearing payload;
- bounded-cardinality Prometheus labels that exclude free text, arbitrary identifiers, exception
  text, symbols supplied by users, and secrets;
- safe test/demo evidence containing engineering outcomes, never credentials or profitability
  claims.

No raw headers, environment/settings dumps, complete provider/broker messages, database URLs,
tokens, or raw sensitive exceptions may be logged. The Prometheus inventory and complete service
emission/redaction integration are `NOT_IMPLEMENTED`; the audit verifier itself is implemented.

## Container and CI controls

### Current controls

- GitHub Actions uses read-only repository permission, immutable action commits, checkout without
  persisted credentials, Python 3.11, `uv.lock`, Ruff, mypy, the full offline tests with branch
  coverage, synthetic backtest, deterministic replay, Compose validation, and empty Alpaca
  variables with paper enablement set to `NO`.
- A separate PostgreSQL 16 job covers migrations, authorization, repositories, idempotency, and
  concurrency. Security/container workflows configure Gitleaks, pip-audit, Bandit, CodeQL, Trivy,
  and CycloneDX/SPDX evidence without image publication.
- The platform and data-only collector images use numeric nonroot users. Compose applies read-only
  roots, dropped capabilities, no-new-privileges, bounded restart/resources, health checks,
  service-scoped mounts, and distinct trust-zone networks.

### Target controls

- One locked multi-stage image is reused across service commands, runs as a numeric nonroot user,
  carries no compiler/toolchain or secret in runtime layers, supports a read-only root, and exposes
  only explicit writable runtime/artifact mounts and `/tmp`.
- Every service drops all capabilities, sets `no-new-privileges`, uses `tmpfs` for `/tmp`, receives
  service-specific volumes/secrets, has health checks and bounded restart, and uses resource limits
  where supported.
- CI adds PostgreSQL 16 migration/role/concurrency tests, secret/dependency/static scans, twice-run
  socket-denied demo evidence, container SBOM/vulnerability scanning, and Python CodeQL with least
  permissions. Pull requests do not push images or publish/sign releases.
- Pre-commit pins formatting/linting, syntax, private-key/secret, and large-file checks without
  turning every commit into a full integration run.

The target container/Compose service set, pre-commit hooks, dependency/secret/static scans,
twice-run offline demo, container SBOM/vulnerability scans, and CodeQL jobs are checked in and
covered by static configuration tests. Workflow presence does not claim that a remote scan or
container runtime has passed; published CI evidence is still required. Base-image digests and
action commits are immutable and were resolved from their authoritative repositories rather than
inferred.

## Control and evidence matrix

| Control | Current evidence | Status / limitation |
| --- | --- | --- |
| Closed seven-variable platform secret namespace | `tests/unit/test_platform_security.py::test_secret_file_variable_inventory_is_exact`; runtime inventory tests | `IMPLEMENTED_AND_VERIFIED` for the generic foundation |
| Owner-private, no-symlink, redacted secret loading | Positive, mode, ownership, file-type, symlink, race, size/content, serialization, and safe-error cases in `tests/unit/test_platform_security.py` | `IMPLEMENTED_AND_VERIFIED` on supported POSIX semantics; container entrypoint and API consume it |
| Fixed nine-file local bootstrap | Inventory, rerun, mode, hostile-umask, collision, partial-write, concurrency, output, and ambient-credential tests in `tests/unit/test_platform_secret_bootstrap.py` | `IMPLEMENTED_AND_VERIFIED`; no Alpaca keys generated |
| Immutable experiment/profile identity and default-deny profile composition | `tests/unit/test_platform_experiment.py`, `test_platform_profiles.py`, `test_platform_runtime_settings.py`, and static CLI tests | `IMPLEMENTED_AND_VERIFIED`; later services do not consume it |
| Service-scoped secret-reference selection | Exact service/profile/scope, omission, serialization, ambient-environment, no-secret-load tests, and declarative Compose mount tests | `PARTIALLY_IMPLEMENTED`: composition and topology are verified; host runtime mounts remain externally unvalidated |
| Collector cannot gain trading authority | `tests/architecture/` import/source boundaries, collector credential tests, and collector-only image smoke test | `IMPLEMENTED_AND_VERIFIED` for current module/image boundary; host process sandbox is limited |
| No ordinary test/CI Alpaca access | Empty CI variables, paper `NO`, pytest TCP guards, deterministic fakes, and collector container `--network none` smoke | `PARTIALLY_IMPLEMENTED`: common TCP paths are guarded, process-wide denial is absent |
| Submission disabled and paper-only | Tracked configuration, static endpoint/client scans, and legacy safety matrix | `IMPLEMENTED_AND_VERIFIED` as default/gate behavior; credential-based paper operation is not externally validated |
| Intent-first and ambiguity containment | Legacy execution/reconciliation unit, integration, and replay tests | `IMPLEMENTED_AND_VERIFIED` for legacy semantics; target signed execution is `NOT_IMPLEMENTED` |
| Collector transactional/fencing integrity | Predecessor collector repository/service tests plus generic bar/latest/symbol-watermark unit and PostgreSQL integration tests | `IMPLEMENTED_AND_VERIFIED` for the predecessor; generic repository behavior is implemented with final current-revision PostgreSQL 16/publication evidence pending |
| API authentication, authorization, size/rate limits, and no trade routes | `tests/unit/test_platform_control_api.py` and route/import boundaries | `IMPLEMENTED_AND_VERIFIED` locally; deployed listener remains external evidence |
| Read-only dashboard enforced by server authority | HMAC scope tests, dashboard client/import tests, entrypoint materialization tests, Compose mount separation, and offline volume probe | `IMPLEMENTED_AND_VERIFIED` locally; Docker administrators remain trusted |
| Target role grants and unauthorized-write denial | Fixed cluster bootstrap, additive grant migration, hostile-ACL normalization tests, and guarded PostgreSQL role matrix | `PARTIALLY_IMPLEMENTED`: database authorization exists; final current-revision PostgreSQL 16/publication evidence and service credential adoption remain |
| Hash-chained audit and bounded metrics | Audit domain/repository/CLI tests cover canonical payloads, writer authority, idempotency, concurrency, tampering, expected heads, safe failures, and scoped views | `PARTIALLY_IMPLEMENTED`: audit chain/verifier exists; complete emitters and bounded Prometheus metrics do not |
| Complete target CI security/supply-chain gates | Quality, PostgreSQL, Gitleaks/pip-audit/Bandit/static, twice-run offline demo, container/SBOM/Trivy, and CodeQL workflow tests | `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`: remote execution evidence remains |

Tests prove only the behavior they execute. External credentials, hosted databases, broker
responses, host firewall policy, backup restoration, and container-runtime enforcement require
separate recorded validation before their controls can be described as externally validated.

## Residual risks and unresolved decisions

- Credential-based Alpaca data and paper behavior has not been validated in this implementation
  program. No current test evidence proves provider entitlements, provider-side key scope, or
  broker behavior.
- The current legacy application can place market-data and paper capabilities in one process. The
  generic service-scoped secret interface does not repair that legacy authority boundary.
- Generic service commands, scoped secret loading, least-privilege database grants, Compose mounts,
  numeric process identities, and target networks are checked in and locally tested. A deployed
  process/credential inventory remains operator evidence.
- The control API derives a domain-separated read-only dashboard bearer from the operator token.
  Docker/host administrators can still inspect the named credential volume and remain trusted.
- The preserved legacy dashboard still reads SQLite directly. The generic dashboard is API-only,
  mounts no Compose secret, and publishes only on loopback by default.
- Local bootstrap depends on macOS/Linux descriptor-relative filesystem operations and advisory
  `flock`. Host compromise, ineffective locking on an unusual filesystem, and backup copies of
  local secrets remain outside the primitive's control.
- Compose network separation will reduce reachability but is not an outbound firewall. Host/cloud
  egress restrictions remain an operator responsibility.
- Installed strategy plugins are operator-trusted code and are not sandboxed. Credential, import,
  database-grant, network, and independent risk/execution boundaries reduce their authority but do
  not provide an in-process Python sandbox.
- A database owner, host administrator, compromised dependency, or compromised container may bypass
  application checks. Detection, least privilege, immutable evidence, restoration, and dependency
  scanning reduce but do not eliminate this risk.
- The stored collector-universe row is not yet reverified against the in-process contract at
  startup and lacks an immutability trigger.
- Deployment evidence for retention, complete cross-service audit emission, a real PostgreSQL
  backup/restore run, and target incident-response drills remains external. The implementation
  status ledger distinguishes those missing procedures from checked-in executable contracts.
- Current socket guards do not prove process-wide network denial. Secret/dependency/container
  scans, SBOM, offline-demo evidence comparison, CodeQL, and role/migration/concurrency gates are
  checked in but still require a successful published run for remote evidence.
- Real-money support and public multi-user hosting remain outside the supported security model.

## Change discipline

Any change that alters credentials, process authority, database grants, network reachability,
public routes, persistence semantics, submission gates, failure recovery, audit evidence, or
container/CI enforcement must update this document, [security-model.md](security-model.md), the
corresponding requirement status, and behavioral tests in the same natural change. A control moves
to `IMPLEMENTED_AND_VERIFIED` only after its stated test or procedure has actually run.
