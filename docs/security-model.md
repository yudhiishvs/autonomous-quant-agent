# Security Model

## Principle and supported boundary

External data, strategy output, and broker responses are untrusted until validated. Component
interfaces are intended to receive only their required authority, consequential transitions are
durable, and an ambiguous state blocks new exposure. Current process-launcher authority gaps are
recorded below rather than treated as implemented isolation.

Real-money trading and public multi-user hosting are unsupported. Tracked configuration
disables paper submission. Current external Alpaca paths are
`IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`; ordinary tests and CI use empty credentials and
patch the common Python TCP connection paths used by current adapters. Process-wide socket denial
is `NOT_IMPLEMENTED`.

## Protected assets

- Alpaca data and paper credential pairs
- database URLs and operator-owned account identifiers
- order authority, durable intents, positions, fills, and reconciliation state
- market observations, current projections, checkpoints, and collection-universe identity
- configuration, decision receipts, incidents, latches, audit history, and report integrity
- runtime and database availability

## Actors and failure sources

- The local operator owns configuration, databases, credentials, process startup, recovery, and
  any explicit paper-only activation.
- Alpaca Market Data and the optional Alpaca paper account are external services. Their payloads,
  timing, availability, and error text are untrusted even when transport authentication succeeds.
- PostgreSQL and filesystem operators control storage availability, access policy, backups, and
  host-level encryption; a stale process or misconfigured role is an internal threat source.
- Installed Python packages and future locally registered signal providers are operator-trusted
  code, not sandboxed tenants. They still must not receive broker objects or credentials through
  platform interfaces.
- Accidental operator error, malformed configuration, clock/calendar disagreement, process crash,
  network partition, duplicate delivery, stale leases, and partial broker acknowledgement are
  modeled as security-relevant failure sources because they can corrupt evidence or authority.
- A remote multi-user adversary is outside the supported deployment model. Future private API and
  dashboard inputs remain untrusted within the single-operator boundary.

## Current trust zones

| Zone | May access | Must not access |
| --- | --- | --- |
| Standalone collector | Data credential pair; runtime PostgreSQL credential; fixed data-provider hosts and configured PostgreSQL | Paper credentials, trading SDK/client, legacy execution, arbitrary provider hosts or undeclared destinations |
| Legacy strategy/allocation | Completed history and configuration | Broker credentials, direct broker mutation, risk-policy mutation |
| Legacy risk/execution | Approved proposal, account/market state, paper adapter when all gates pass | Real-money endpoint, unknown symbols, stale or ambiguous authorization |
| Legacy dashboard/reporting | Read-only SQLite and generated artifacts; the local launcher may inherit unrelated ambient variables | Application code does not read credentials or mutate a broker; process-level environment scrubbing is incomplete |
| CI and ordinary tests | Synthetic/fake inputs; loopback disposable PostgreSQL where marked | Alpaca secrets, external TCP calls through guarded paths, paper or real orders |
| Migration operator | Separate schema-owner PostgreSQL URL | Long-running collector process |

A dedicated least-privilege collector role is recommended in the current runbook but is not
provisioned or verified by the migration. Exact platform roles and denial tests remain target work.

The target scheduler, strategy worker, execution worker, private control API, API-backed
dashboard, and service-specific database roles are `NOT_IMPLEMENTED`. They must preserve
the dependency direction in `../ARCHITECTURE.md` when added.

## Untrusted inputs, external services, and data in transit

| Boundary | Untrusted input | Current transport and control |
| --- | --- | --- |
| Configuration and environment | YAML values, paths, mode flags, URLs, and credential-file presence | Strict YAML parsing, explicit injected-environment snapshots, exact service/secret scopes, opaque file references, the owner-private loader, and local infrastructure-secret bootstrap are implemented; service commands and container mounts are not |
| Standalone collector Alpaca data | REST pages, stream frames, timestamps, symbols, numeric values, rate-limit metadata, disconnects, and error categories | Direct HTTPS/WSS to fixed official data hosts using the collector data-key namespace, certificate/hostname verification, explicit timeouts, bounds, and shared canonical validation |
| Legacy Alpaca market data | Historical/stream SDK responses, account-scoped feed access, timestamps, bars, disconnects, and SDK exceptions | Locked `alpaca-py` clients using the legacy paper credential object/namespace; feed allowlist, bounded reconnects, completed-bar checks, durable bar storage, and redacted errors; it is not process-credential-isolated from legacy paper execution |
| Legacy Yahoo compatibility | Configured tickers/date bounds and yfinance responses | Optional `legacy-yahoo` dependency; normalization/empty-series validation applies, but fixed-host and explicit transport-timeout controls are not implemented |
| PostgreSQL | Configured URL, stored rows, lock timing, constraint failures, and connection errors | Loopback may be plaintext; non-loopback requires TLS with `sslmode=verify-full`; SQL is parameterized and routing override keys are rejected |
| Optional Alpaca paper boundary | Account, asset, order, fill, status, stream, and error responses | Fixed paper client with TLS through the locked SDK; construction and submission remain behind explicit legacy gates and are not externally validated |
| Local SQLite and artifacts | Existing rows, filenames, serialized JSON/CSV, and generated reports | Host filesystem permissions are the transport boundary; current dashboard has direct read access |
| Target private API/dashboard | Bearer token, route/query/path values, request sizes, job identifiers, and returned read models | `NOT_IMPLEMENTED`; target is loopback/private networking with bounded schemas, constant-time token checks, and no direct broker route |

Transport security authenticates an endpoint, not its content. Every provider/broker payload still
requires semantic validation before persistence or authorization. Compose network segmentation is
not an egress firewall.

## Inputs and controls

- YAML configuration is strict and rejects hazardous live-trading terms.
- Collector symbols come from immutable `collection-universe.v1`; all 29 are explicitly
  unauthorized for execution.
- Bar contracts validate provider/feed/adjustment/timeframe, UTC timing, OHLC consistency,
  finite positive prices, nonnegative counts, hashes, and bounded payload protocols.
- PostgreSQL uses parameterized statements, explicit transactions, constraints, immutable
  triggers, monotonic checkpoints, and singleton fencing tokens.
- Provider URLs are constants. The collector rejects proxy inheritance and endpoint
  overrides; non-loopback PostgreSQL requires `sslmode=verify-full`.
- The platform secret primitive accepts only the closed seven-source enum and descriptor-walks a
  bounded canonical POSIX path without following symlinks. It requires a current-owner regular
  file in mode 0400 or 0600, reads at most 16 KiB of UTF-8 without NUL, and removes exactly one
  terminal LF; all rejection errors omit the path, value, and operating-system exception.
- Platform runtime composition accepts only an explicitly injected mapping, rejects generic Alpaca
  credential variables by presence, validates six nonsecret settings, and selects opaque
  secret-file references from an exact service/mode matrix without reading their contents.
- Local bootstrap creates only eight database-role passwords and one operator token beneath a
  current-user-owned application root. It serializes threads and processes, publishes owner-only
  files without replacement, preserves valid existing files, rejects unsafe or unresolved state,
  and emits only relative file names. It never reads or creates Alpaca credentials.
- Legacy broker construction fixes `paper=True`; static tests reject live endpoints,
  generic credential names, and alternate live broker classes.
- Durable intent precedes submission; deterministic client IDs and reconciliation contain
  duplicate/ambiguous effects.

## Credentials and data handling

Collector variables are `APA_ALPACA_DATA_API_KEY` and
`APA_ALPACA_DATA_SECRET_KEY`; the standalone collector image omits the Alpaca SDK and execution
modules. The legacy Alpaca market-data provider and paper adapter both consume a
`PaperCredentials` object loaded from the `APA_ALPACA_PAPER_*` namespace, so they are not separate
process authorities. These controls isolate the standalone collector only, and Alpaca keys are not
claimed to be provider-scoped as data-only.

No credential belongs in YAML, `.env.example`, Git, image layers, logs, reports, metrics,
database rows, or test fixtures. Current collector error paths report exception types and
credential wrappers redact their values. Legacy logging redacts known values and common
secret-bearing key/header forms.

The reusable platform loader keeps its result only in an immutable in-memory wrapper whose
ordinary, container, logging, and Pydantic renderings are `<redacted>` and whose pickling is
rejected. `RuntimeSettings` stores only opaque references whose rendering and JSON schema omit the
path; loading still requires an explicit call at the future adapter boundary. Neither primitive
inspects ambient process state, persists a value, or creates a client. No runtime service command
consumes the loader yet. Installed in-process Python remains trusted; process and mount isolation,
not object-construction tricks, is the credential security boundary.

When run from the repository root as documented, the POSIX-only
`aqa secrets bootstrap-local` command writes generated local infrastructure secrets to the ignored
`secrets/` directory beneath that current working directory. Those values intentionally persist on
disk for later database and API startup; they are never returned through the secret loader,
configuration models, or CLI output during bootstrap. Existing files are validated but never
overwritten.

PostgreSQL should be private and encrypted in transit; backups and encryption at rest are
operator/provider responsibilities. SQLite, runtime logs, downloaded data, and outputs are
ignored local files and require host-level access controls.

## Likely abuse and failure cases

| Case | Preventive/detective control | Recovery |
| --- | --- | --- |
| Malformed or oversized provider payload | Bounded decoding, schema validation, protocol failure classification | Stop or bounded retry; record safe collector event |
| Duplicate/corrected bar | Stable observation/content identities and append-only rows | Deterministic projection and revision |
| Stale collector after failover | Lease expiry and fencing checked on every lease-protected ingestion mutation | Takeover marks stale run failed; stale writes reject |
| Credential disclosure | Ignore rules, redacted wrappers/logs, empty CI secrets, and direct sentinel tests for the file-loader rendering/error surfaces | Revoke/rotate, contain workload, correct leak path |
| SQL injection or routing override | SQLAlchemy binding and rejected URL override keys | Refuse startup/request; inspect audit evidence |
| Strategy bypass of risk | Separate strategy, risk, planner, and broker responsibilities | Block execution and add architecture regression test |
| Submission timeout or duplicate event | Intent-first persistence, stable client ID, idempotent events | Mark ambiguous and reconcile; never blind retry |
| Stale market/account state | Freshness and reconciliation gates | No new exposure until authoritative refresh |
| Dashboard mutation | Read-only code path and fixed table allowlist | Stop dashboard and inspect local state |

## Authentication and authorization

The current system is single-operator and has no HTTP control API. Local host/file/database
permissions are the operator boundary. The target bearer-token API, per-route authorization,
rate limiting, request-size limits, and audited control events are `NOT_IMPLEMENTED`.

The target specification currently names one operator token while requiring the dashboard to have
server-enforced read-only authority. Giving that same token to the dashboard would also authorize
bounded control mutations if authorization is token-only. The API design must resolve this before
`REQ-ARCH-007` or `REQ-UI-001` can be verified; a read-only client implementation alone is not an
authorization boundary.

Paper submission in the legacy path requires an explicit command, tracked config enablement,
an exact environment acknowledgement, verified paper account/credentials, fresh state,
open session, risk approval, and clean reconciliation. Because tracked configuration leaves
submission disabled, no default path submits.

## Operational controls

- Collector startup validates configuration and migration head, registers the content-addressed
  universe row, and acquires singleton lease ownership before ingestion; status/readiness commands
  do not load data secrets.
- Bounded retries, deadlines, worker joins, fencing tokens, intent-first persistence, stable
  identifiers, reconciliation, and latches contain duplicate, stale, ambiguous, or partial work.
- Every tracked platform profile disables submission. Static `aqa doctor` and
  `aqa config validate` use a mandatory experiment hash pin and reject symlinked configuration
  paths, invalid mode/adapter/provider combinations, and reserved profile-name mismatches without
  reading credentials or constructing external clients. `aqa secrets bootstrap-local` is the only
  current platform command with write authority and confines that authority to the fixed `secrets/`
  inventory beneath the current working directory; the directory is ignored when invoked from the
  repository root as documented. Legacy paper operation additionally requires an explicit command,
  exact acknowledgement, paper account verification, fresh state, open session, risk approval, and
  clean reconciliation.
- Current logs and durable events support local diagnosis with redaction. Credential rotation,
  incident containment, and reporting follow `../SECURITY.md` and the existing runbooks.
- Database access policy, TLS, backup retention, encryption at rest, host firewalling, and process
  supervision remain deployment-operator responsibilities. The target role/grant matrix,
  automated restore proof, audit chain, API rate limits, and platform service health contracts are
  `NOT_IMPLEMENTED`.

## Residual risks

- Credential-based Alpaca behavior and hosted database operation have not been validated.
- Legacy environment-variable secrets remain visible to their processes. Platform runtime settings
  select service-scoped opaque references and local infrastructure bootstrap is available, but
  service command integration, least-privilege mounts, and full sentinel integration are
  `NOT_IMPLEMENTED`.
- Bootstrap is supported on local macOS/Linux filesystems with the required descriptor-relative
  operations and advisory `flock` semantics. Reported API or lock failures fail closed; ineffective
  or node-local locking on other filesystems cannot be detected, and host-level compromise remains
  outside this boundary.
- The current dashboard reads SQLite directly rather than through an authenticated API.
- The local dashboard launcher removes legacy paper variables but can inherit data-provider and
  database variables from its parent environment; code does not consume them, but least-privilege
  process startup is not enforced.
- The target's single named operator token cannot yet give a compromised dashboard
  server-enforced read-only authorization; the API design conflict remains unresolved.
- Compose isolation is not an outbound firewall and the dashboard port is not loopback-bound.
- Explicit collector gap lifecycle, downstream readiness, backup/restore proof, retention,
  and artifact integrity are `NOT_IMPLEMENTED`.
- Stored collector-universe membership is not compared with the in-process contract at startup and
  has no immutability trigger; a database owner can change it without runtime detection.
- Locally installed future strategy plugins will be operator-trusted code; arbitrary Python
  sandboxing is `INTENTIONALLY_DEFERRED`.

Private reporting and credential-response steps are in `../SECURITY.md`.
