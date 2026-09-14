# Security model

## Supported boundary

Autonomous Quant Agent is self-hosted and single-operator. Offline simulation is the default;
shadow has no broker; the only execution adapter is paper-only and remains disabled in tracked
configuration. Real-money execution, public hosting, multi-tenancy, OAuth custody, and arbitrary
strategy code submission are unsupported.

External data, local configuration, provider output, database rows, strategy proposals, API input,
and broker responses are untrusted until validated. Missing, stale, malformed, ambiguous, or
unauthorized state creates no new exposure.

## Protected assets

- separate Alpaca data and paper credential pairs;
- database-role URLs, operator/dashboard tokens, and approved paper-account hash;
- market observations, effective revisions, gaps, watermarks, and dataset lineage;
- experiment, signal, policy, risk, plan, intent, fill, position, reconciliation, latch, incident,
  job, outbox, and audit integrity;
- external side-effect identity and paper-order authority; and
- service/database/artifact availability.

## Trust zones and least privilege

| Zone | Permitted authority | Prohibited authority |
| --- | --- | --- |
| market-data worker | collector DB role; data-only key pair in live profile; fixed data hosts | paper keys, trading client, order/risk mutation, arbitrary URL/shell/plugin |
| scheduler | scheduler DB role; experiment/calendar/watermark/slot state | provider/broker keys, signal internals, orders, external network |
| strategy | strategy DB role; immutable context; registered provider | provider/paper keys, broker/execution imports, risk/order writes, remote code |
| execution | execution DB role; fake broker offline; paper files only in paper profile | data keys, model/plugin loading, DDL, configurable/live-money host |
| control API | control DB role and operator token; safe reads and bounded jobs/halt/resume | Alpaca keys, broker imports, direct order/fill mutation, URL/path/code/SQL input |
| dashboard | read-scoped API token | database or Alpaca credentials, mutation client, broker/order controls |
| migration | deployment-only schema-owner URL | long-running runtime use and ordinary business DML |
| CI/tests | synthetic/fake input; explicitly guarded loopback PostgreSQL | Alpaca secrets, non-loopback connections, provider/paper calls or orders |

PostgreSQL authorization roles and matching login principals are created separately. Runtime roles
receive explicit table/view/sequence/function grants rather than schema-wide mutation. The
dashboard has no database role.

Process separation and Compose networks reduce authority but are not a Python sandbox or host-level
outbound firewall. Installed extension packages remain operator-trusted code.

## Configuration and credentials

Strict frozen configuration rejects unknown fields, YAML aliases/merges/tags, duplicate keys,
oversize/deep input, path escape/symlinks, incompatible mode/adapter combinations, and expected-hash
mismatch. Flagship symbol authority lives only in the immutable experiment YAML. Collection,
benchmark, context, excluded, and order allowlists remain distinct.

Platform services accept secret values only through these file references:

```text
AQA_DATABASE_URL_FILE
AQA_OPERATOR_TOKEN_FILE
AQA_ALPACA_DATA_API_KEY_FILE
AQA_ALPACA_DATA_SECRET_KEY_FILE
AQA_ALPACA_PAPER_API_KEY_FILE
AQA_ALPACA_PAPER_SECRET_KEY_FILE
AQA_PAPER_ACCOUNT_ID_HASH_FILE
```

The hardened loader opens descriptor-relative paths without following symlinks, pins file identity,
requires a current-owner regular file in mode `0400` or `0600`, bounds content, rejects empty/NUL or
invalid UTF-8, trims one newline, and returns a non-pickleable redacted wrapper. Errors omit path,
value, and OS exception details.

`aqa secrets bootstrap-local` creates database-role passwords and an operator token only. It uses
cryptographic randomness, owner-only directories/files, atomic no-replace publication, and
idempotent validation. It never creates provider credentials or prints values.

Data and paper keys must be different operational pairs and mounted only into their process. No
credential belongs in YAML, `.env`, Git, image layers, database rows, fixtures, logs, metrics,
responses, evidence, benchmark output, or backups. Follow [secret rotation](secret_rotation.md).

## Input and data integrity

- Canonical JSON has bounded size/depth/nodes and rejects custom primitive subclasses, sets,
  secrets, nonfinite values, and nondeterministic encodings.
- Bars validate series identity, allowlist/role, aligned UTC interval, coherent positive OHLC,
  nonnegative counts, required VWAP, source mode, payload hash, and correction lineage.
- Duplicate events converge; corrections append a new immutable revision and atomically advance the
  latest projection.
- Gaps and active-basket watermarks prevent missing or ambiguous data from authorizing a slot.
- Dataset paths are root-confined, reject symlink/traversal/conflicting overwrite, and bind physical,
  logical, schema, input, experiment, and manifest hashes.
- Provider hosts are constants. Public schemas contain no arbitrary URL, fetch, webhook, callback,
  command, module, class, SQL, or upload field.
- Unsafe deserialization, dynamic evaluation/compilation, arbitrary import, and runtime package
  installation are absent from platform request paths.

Provider TLS authenticates an endpoint, not its content. Every response is still validated before
persistence or authority.

## Strategy, risk, and execution containment

Strategies return declarative `SignalEnvelope` values and have no broker authority. Envelopes bind
provider, slot, experiment, data, policy, time, symbols, availability, action, and content hash.
Fixture signals are non-promotable and paper-ineligible.

Signed risk receives complete positions, orders, account, prices, security metadata,
reconciliation, data integrity, equity history, statistics, experiment, and latch state. It applies
finite/freshness/identity gates and deterministic shrink-only constraints. Unknown or nonconvergent
input produces flat/no-execution output. Session-loss, drawdown, operator, and reconciliation
latches are append-only and survive restart.

Execution persists signed plans and deterministic intents before any broker side effect. A sign
reversal closes and reconciles to zero before opening the opposite side. Timeout after possible
acceptance becomes `SUBMISSION_UNKNOWN`; lookup and reconciliation resolve it, never blind retry.
Duplicate updates/fills are idempotent by stable identity and conflicting reuse fails closed.

Paper invocation requires all independent profile, acknowledgement, literal-paper adapter,
credential, account, signal artifact, approval, freshness, reconciliation, and latch gates. Tracked
config denies submission and the default approval verifier denies promotion. There is no
`paper=False` or real-money endpoint.

Forced flatten disables entries, cancels conflicting openings, reconciles, persists close intents,
processes fills, and proves exact zero. Failure creates a durable blocking incident rather than
success.

## API and dashboard

The private FastAPI app binds loopback by default. Only liveness/readiness are unauthenticated.
Authenticated routes use a minimum 32-byte bearer token, constant-time comparison, strict request
models, 65,536-byte request bound, bounded pages, read/mutation rate limits, no cookies/CORS/docs by
default, safe correlated errors, no-store/nosniff headers, and restrictive content policy where
applicable.

The route inventory has safe reads and bounded jobs/halt/resume only; it has no direct trade
mutation. Job payloads are routing data, not authority. Dashboard access is server-enforced read
only and its client cannot call mutation routes or import storage/broker modules.

## Observability and audit

Structured logs recursively redact credential-shaped keys/values and permit only bounded safe
fields. Prometheus labels use reviewed finite enums; hostile symbols, IDs, paths, errors, and free
text cannot create unbounded series. Raw headers, environment/settings dumps, URLs, payloads, and
external exceptions are never emitted.

Consequential transitions append per-stream audit events whose payload and chain hashes are
independently verified. `aqa audit verify` fails on payload, identity, sequence, previous-hash, or
head mismatch. Logs and metrics are diagnostic; audit/database state is authoritative.

## Network and test safety

Ordinary tests remove ambient Alpaca variables, force submission off, and deny common Python socket
connections. PostgreSQL-marked tests permit only loopback and require the exact disposable
`collector_test` guard. Provider/broker SDK behavior uses injected fakes/mocks. The offline demo
constructs no Alpaca transport or credentials.

A complete OS/process-wide outbound-denial wrapper has not been implemented; Compose network
separation is not an egress firewall. CI and final reports must state the exact network control
actually exercised.

## Supply chain and containers

Locked dependencies, pinned actions, secret/static/dependency scans, Python CodeQL, nonroot
multi-stage images, package-build/install smoke, SBOM generation, and container scanning are
defined as separate validation gates. A checked-in workflow is not evidence that a remote run or a
fresh vulnerability database passed; report those results only after execution.

Images exclude credentials, local state, raw data, VCS metadata, caches, and development tooling.
Services use numeric nonroot identities, dropped capabilities, no-new-privileges, read-only roots
where supported, bounded resources/restarts, and explicit mounts.

## Backup and recovery

The guarded logical backup/restore smoke accepts only a loopback disposable `collector_test`,
populates deterministic state, restores into a fresh generated database, runs/checks migrations,
and compares row hashes, audit root, slots, intents, fills, and reconciliation. It also rejects
credential-shaped fixture fields. Its result is externally unvalidated when PostgreSQL client tools
are unavailable. See [backup/restore](backup_restore.md).

After recovery, reconcile deterministic broker identities/account state before enabling new
exposure. A restored database does not authorize replay.

## Residual risks

- No credential-based Alpaca data or paper behavior was validated in this completion work.
- No paper order was submitted; mocked adapter tests cannot prove provider availability or account
  configuration.
- Host compromise defeats local file/process boundaries.
- Locally installed Python extensions are not sandboxed.
- Network segmentation does not provide complete outbound enforcement.
- Hosted alert routing, centralized retention, high availability, and public denial-of-service
  defense are outside the self-hosted single-operator scope.
- Provider licensing, backup encryption/retention, filesystem/object-store durability, and host
  firewalling remain operator responsibilities.

Private reporting and response are in [SECURITY.md](../SECURITY.md), [incident response](incident_response.md),
and [failure modes](failure_modes.md).

Local hardening on 2026-09-12 bounds unauthenticated API body reception to ten seconds
and 65,536 bytes; the single Uvicorn process also limits concurrency to 128. Runtime
workers verify database compatibility through `aqa.aqa_schema_version_v` (migration
0016), a metadata-only SELECT view, without collector-schema access. Routine SQL INFO
logs are suppressed and warning/error redaction remains enforced. See
[local verification](evidence/local-hardening-20260912.md) for tested boundaries and
remaining operational limits.

## Readiness cache boundary (2026-09-13)

Readiness prefixes are trusted, process-local hash state, never pickled, persisted or
accepted through an API. Reuse depends on unchanged earlier work and gap fingerprints;
publication retains its transactional fence. Revision 0017 uses SECURITY INVOKER and
fully qualified tables to invalidate external minute/aggregate projection changes with
existing collector privileges. It grants no business-table access and cannot submit
orders. Historical tampering, correction, gap changes, rollback and restricted-role
writes remain tested. A compromised database administrator can bypass database
controls; this cache does not claim protection against that authority.

The explicit IEX verification script loads only data credential files, uses the adapter's
fixed endpoint, prints state/count/timestamp summaries and suppresses provider exception
text. It requires a provider acknowledgement and a bounded duration. It neither persists
market payloads nor accesses trading credentials. See the dated
[review and limits](evidence/local-hardening-20260913.md).

## Public workspace continuation (September 14, 2026)

The new loopback development application under `apps/public` uses maintained Keycloak
identity, confidential OIDC/PKCE, verified issuer/subject and server-only encrypted provider
tokens. It reuses `load_secret_file()` with distinct OIDC/encryption namespaces. Customer
strategy records use authenticated ownership plus forced RLS under a restricted login;
existing private state has no new owner. Session-bound CSRF and exact Origin checks protect
mutations. See the [review and remaining risks](evidence/public-workspace-20260914.md).
This partial application must not be exposed publicly. Brokerage authority, production
identity policy, key rotation, administration and operational validation are incomplete.

The paper-connection continuation adds separate OAuth-client and broker-encryption key
files, one-use session-bound state, global paper-account claims, forced RLS and disconnect
generations. Tokens are encrypted with owner/account binding; refreshes cannot overwrite
a newer revision. No order method exists in this slice, but a compromised API holding the
OAuth grant could misuse its provider trading scope. Production egress isolation, key
rotation and operational review remain required. See [connection review](evidence/public-paper-connections-20260914.md).
