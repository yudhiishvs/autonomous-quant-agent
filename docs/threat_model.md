# Threat Model

## Scope and evidence rule

This STRIDE model covers the self-hosted, single-operator platform, its preserved legacy path,
and the target paper-only platform boundary. Real-money trading and public multi-user hosting are
unsupported. A named control is **current** only when code and repository-local verification exist;
otherwise it is explicitly **planned**. External provider, hosted-database, container-runtime, and
operator recovery procedures are not treated as verified merely because they are documented.

Threat status uses only the repository status vocabulary:
`IMPLEMENTED_AND_VERIFIED`, `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`, `PARTIALLY_IMPLEMENTED`,
`NOT_IMPLEMENTED`, `BLOCKED`, and `INTENTIONALLY_DEFERRED`.

## Assets

The protected assets include at least:

1. market-data credentials;
2. paper-broker credentials;
3. operator token;
4. database-role credentials;
5. order authority;
6. account/position state;
7. experiment and signal artifacts;
8. market datasets;
9. immutable evidence;
10. audit history;
11. service availability; and
12. source and build integrity.

## Actors and failure sources

The model includes a malicious remote caller, malicious local user, compromised dependency,
compromised strategy plugin, compromised future AI agent, malicious or corrupted artifact,
compromised container, accidental developer error, replayed message, stale provider data,
broker/API ambiguity, and database race or corruption. The deployment operator and the external
market-data and paper-broker services are trust-boundary participants, not implicitly trusted
sources of correct content.

## Entry points

The entry points include FastAPI, the Streamlit server, CLI, YAML configuration, environment and
secret files, plugin entry points, market-data REST/WebSocket payloads, the paper-broker REST API
and trade-update stream, PostgreSQL, the Docker network, Parquet/JSON artifacts, and GitHub
Actions/dependencies. Some are target entry points whose services are not yet implemented;
documenting them does not claim that they are reachable today.

## Trust-boundary summary

- The local operator controls the host, configuration, secret files, process launch, and recovery.
- Market-data and broker transports authenticate endpoints but never make payload content trusted.
- Strategy and future model output is untrusted declarative data. It must never carry broker
  authority or bypass independent validation, risk, planning, persistence, and reconciliation.
- PostgreSQL is the target durable coordination boundary. Database-owner compromise remains outside
  application-level containment.
- CI and ordinary tests must use synthetic inputs and no provider credentials or external provider
  connection.
- Installed Python plugins are operator-trusted code and are not sandboxed. Removing broker objects
  and credentials from their interfaces reduces authority but is not an in-process sandbox.

## Threat register

### AQA-TM-001 — Operator API impersonation

- **STRIDE class:** Spoofing, Elevation of privilege.
- **Asset:** operator token, order authority, audit history.
- **Entry point:** FastAPI.
- **Precondition:** The target private API is running and a caller can reach its listener.
- **Attack/failure sequence:** A caller omits, guesses, replays, or malforms a bearer token and
  invokes a control route; weak parsing, comparison, or route authorization accepts it.
- **Impact:** Unauthorized operational mutation or disclosure without trustworthy attribution.
- **Preventive controls:** **Current:** no platform FastAPI control service or trade-mutation route
  exists. **Planned:** bounded strict request schemas, startup rejection of short tokens,
  constant-time comparison, default-deny route authorization, private binding, and no direct trade
  mutation route.
- **Detective controls:** **Current:** static architecture and configuration tests constrain generic
  platform authority. **Planned:** bounded authentication-failure metrics and audited control
  attempts without token material.
- **Recovery controls:** **Current:** local secret bootstrap preserves rather than overwrites the
  operator token. **Planned:** rotate the token, stop the API, inspect audit history, and restart only
  after authorization state is known.
- **Verification test:** Planned security-matrix cases 1–4 and 41 in Section 31; no executable API
  authentication test exists yet.
- **Residual risk:** A stolen valid single-operator token is full operator authority; the unresolved
  read-only dashboard/token design could become a confused-deputy path.
- **Owner/status:** API/security maintainer — `NOT_IMPLEMENTED`.

### AQA-TM-002 — Secret disclosure or path substitution

- **STRIDE class:** Information disclosure, Tampering, Spoofing.
- **Asset:** market-data credentials, paper-broker credentials, operator token,
  database-role credentials.
- **Entry point:** environment and secret files, CLI.
- **Precondition:** A process receives ambient credentials, an attacker can alter a secret path or
  its ancestors, or output/error handling renders sensitive values.
- **Attack/failure sequence:** A symlink, special file, insecure mode, ownership change, concurrent
  replacement, oversized value, or hostile exception redirects a read or leaks value/path context.
- **Impact:** Credential theft, credential substitution, unintended broker/database authority, or
  unsafe recovery.
- **Preventive controls:** **Current:** a closed seven-variable file-reference inventory; rejection
  of generic Alpaca variables; descriptor-relative, no-symlink, owner/mode/size/content checks;
  redacted immutable wrappers; rejected pickling; an exact nine-file owner-private bootstrap that
  creates no Alpaca secrets and serializes threads/processes. **Planned:** service-scoped mounts and
  launchers that remove unrelated ambient authority.
- **Detective controls:** **Current:** sentinel, rendering, hostile-path, race, mode, bootstrap
  collision, partial-write, and ambient-environment tests. **Planned:** deployed mount and process
  inventory checks.
- **Recovery controls:** **Current:** unsafe or ambiguous file state fails closed; bootstrap never
  replaces an existing secret. **Planned:** stop affected services, revoke/rotate external
  credentials, replace local secrets through the rotation runbook, and verify no durable leak.
- **Verification test:** `tests/unit/test_platform_security.py`,
  `tests/unit/test_platform_secret_bootstrap.py`, and
  `tests/unit/test_platform_runtime_settings.py`.
- **Residual risk:** Host/root compromise defeats file permissions; advisory locking can be
  ineffective on unsupported filesystems; legacy processes can still inherit environment secrets.
- **Owner/status:** Platform security maintainer — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-003 — Configuration authority escalation

- **STRIDE class:** Tampering, Elevation of privilege.
- **Asset:** order authority, experiment and signal artifacts, account/position state.
- **Entry point:** YAML configuration, CLI.
- **Precondition:** A malicious local user, corrupted artifact, or developer can supply or replace
  a profile/experiment file.
- **Attack/failure sequence:** Unknown fields, YAML aliases, unsafe paths, a mismatched content hash,
  hazardous mode/adapter combinations, or a live endpoint attempts to widen runtime authority.
- **Impact:** Wrong universe or policy, unintended network authority, or paper/live submission.
- **Preventive controls:** **Current:** strict anchor-free YAML, complete immutable profiles,
  mandatory experiment-hash pinning during composed platform-profile loading, confined no-symlink
  reads, reserved profile/mode checks, and tracked `submission_enabled: false`/`paper_only: true`;
  live broker endpoints and `paper=False` are statically rejected. **Planned:** all service
  commands consume only composed validated settings.
- **Detective controls:** **Current:** deterministic `doctor` and `config validate`, known hashes,
  configuration-negative tests, and static repository scans.
- **Recovery controls:** **Current:** invalid or ambiguous configuration fails startup without
  reading secrets or creating clients; restore a reviewed tracked profile and rerun validation.
- **Verification test:** `tests/unit/test_platform_profiles.py`,
  `tests/unit/test_platform_runtime_settings.py`, and
  `tests/safety/test_static_repository_safety.py`.
- **Residual risk:** A host user able to replace both executable code and configuration controls the
  process; later services could regress unless they reuse the validated composition boundary.
- **Owner/status:** Configuration maintainer — `IMPLEMENTED_AND_VERIFIED`.

### AQA-TM-004 — Compromised strategy or future agent bypasses policy

- **STRIDE class:** Elevation of privilege, Tampering.
- **Asset:** order authority, account/position state, experiment and signal artifacts.
- **Entry point:** plugin entry points.
- **Precondition:** A compromised strategy plugin or future AI agent executes in the strategy worker
  or emits a crafted proposal.
- **Attack/failure sequence:** The producer requests excluded symbols, stale or extreme targets,
  mutates policy identity, injects executable code, imports broker logic, or forges a signal hash.
- **Impact:** Unauthorized exposure, policy bypass, arbitrary code effects, or false attribution.
- **Preventive controls:** **Current:** generic experiment/config types contain no broker or network
  authority. **Planned:** registered entry-point IDs only, immutable decision context, signed
  envelopes, freshness/hash/identity checks, broker-free strategy interfaces, and independent risk
  and execution workers.
- **Detective controls:** **Current:** architecture tests keep provider/broker authority out of
  generic configuration. **Planned:** proposal rejection receipts, import-boundary tests, and audit
  events for every decision.
- **Recovery controls:** **Current:** tracked profiles leave submission disabled. **Planned:** reject
  the proposal, disable the provider, latch new exposure when state is ambiguous, and replay from
  durable trusted inputs.
- **Verification test:** `tests/architecture/test_platform_configuration_boundary.py` is current;
  Section 31 cases 12–18, 22–23, and 40 remain planned.
- **Residual risk:** Locally installed Python is operator-trusted and arbitrary plugin sandboxing is
  `INTENTIONALLY_DEFERRED`; dependency or host compromise can escape interface-only controls.
- **Owner/status:** Strategy/risk maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-005 — Malicious, malformed, or stale market payload

- **STRIDE class:** Tampering, Denial of service.
- **Asset:** market datasets, service availability, experiment and signal artifacts.
- **Entry point:** market-data REST/WebSocket payloads.
- **Precondition:** Alpaca returns malformed, duplicated, unrequested, stale, future-dated, or
  adversarially large data, or the stream disconnects.
- **Attack/failure sequence:** The collector decodes a hostile response, accepts invalid semantics,
  stores an incomplete bar, or treats stale coverage as decision-ready.
- **Impact:** Corrupted history, false signals, missing data, resource exhaustion, or decisions on
  stale evidence.
- **Preventive controls:** **Current:** fixed official data endpoints/feed, disabled proxy
  inheritance, TLS verification, explicit bounds/timeouts, requested-symbol checks, canonical
  validation, completed-minute cutoffs, session windows, pagination bounds, and failure
  classification. **Planned:** explicit gap/readiness contracts for all downstream decisions.
- **Detective controls:** **Current:** reconciliation provenance, durable collector events,
  checkpoints, content hashes, and negative REST/WebSocket tests.
- **Recovery controls:** **Current:** capped retries, bounded overlap restart, stop on permanent
  failures, durable run/lease cleanup, and duplicate-safe replay. **Planned:** gap repair and
  downstream readiness blocking through canonical datasets.
- **Verification test:** `tests/test_collection_alpaca.py`, `tests/test_collection_contracts.py`,
  and `tests/test_collection_service.py`.
- **Residual risk:** Credentialed Alpaca behavior is `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`; the
  test socket guard is not process-wide, and explicit canonical gap lifecycle is absent.
- **Owner/status:** Market-data maintainer — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-006 — Replayed or corrected observation corrupts history

- **STRIDE class:** Tampering, Repudiation.
- **Asset:** market datasets, immutable evidence, audit history.
- **Entry point:** market-data REST/WebSocket payloads, PostgreSQL.
- **Precondition:** A response, stream frame, retry, or restart repeats an observation or carries a
  later correction for an existing bar identity.
- **Attack/failure sequence:** Retry identity is confused with content identity, a correction
  overwrites raw evidence, or the current projection selects the wrong revision.
- **Impact:** Duplicate data, lost provenance, nondeterministic replay, or an unverifiable dataset.
- **Preventive controls:** **Current:** stable observation/content hashes, append-only raw rows,
  idempotent observation insertion, deterministic correction precedence, projection lineage, and
  checkpoint series identity.
- **Detective controls:** **Current:** duplicate/correction counts, receipt bounds, immutable-row
  triggers, and restart/replay tests.
- **Recovery controls:** **Current:** replay is retry-idempotent; corrected content adds a new raw
  observation and revises the projection rather than erasing evidence.
- **Verification test:** `tests/test_collection_contracts.py` and
  `tests/integration/test_collection_postgres.py` duplicate, correction, projection, and append-only
  cases.
- **Residual risk:** Full frozen-dataset manifests and audit-chain verification are not implemented;
  a database owner can disable constraints or alter data.
- **Owner/status:** Data/persistence maintainers — `IMPLEMENTED_AND_VERIFIED` for the current
  collector boundary.

### AQA-TM-007 — Database race, stale writer, or corrupted coordination state

- **STRIDE class:** Tampering, Denial of service, Repudiation.
- **Asset:** market datasets, account/position state, immutable evidence, service availability.
- **Entry point:** PostgreSQL.
- **Precondition:** Concurrent collectors, a crashed lease holder, stale fencing token, disabled
  trigger, partial transaction, or corrupted row exists.
- **Attack/failure sequence:** Two workers claim authority, a superseded writer mutates state, a
  checkpoint regresses, or corrupt state is treated as valid.
- **Impact:** Split-brain ingestion, overwritten evidence, false readiness, or unavailable service.
- **Preventive controls:** **Current:** transactions, constraints, singleton lease, fencing on
  protected ingestion mutations, monotonic checkpoints, immutable observation triggers, and schema
  verification. **Planned:** equivalent durable jobs, scheduler slots, execution transitions, and
  fail-closed validation across all target services.
- **Detective controls:** **Current:** migration/schema verification and durable correlated run and
  readiness state. **Planned:** incident/health metrics for every stale lease and corrupt state.
- **Recovery controls:** **Current:** expired takeover marks the previous run superseded, rejects
  stale mutations, and allows deterministic replay. **Planned:** tested backup restore and repair
  procedures.
- **Verification test:** `tests/integration/test_collection_postgres.py` migration, lease,
  fencing, checkpoint, and trigger cases.
- **Residual risk:** Tests require an available PostgreSQL integration environment; database-owner
  compromise, disabled constraints after startup, and operational backup loss remain.
- **Owner/status:** Persistence maintainer/operator — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-008 — SQL injection or database-role escalation

- **STRIDE class:** Tampering, Information disclosure, Elevation of privilege.
- **Asset:** database-role credentials, market datasets, account/position state, audit history.
- **Entry point:** PostgreSQL, FastAPI, CLI.
- **Precondition:** Untrusted text reaches SQL construction or a service receives an overprivileged
  database credential.
- **Attack/failure sequence:** Input changes query structure, selects another schema/host via URL
  parameters, or a compromised service writes tables outside its responsibility.
- **Impact:** Data disclosure, destructive mutation, falsified audit state, or privilege expansion.
- **Preventive controls:** **Current:** SQLAlchemy bound parameters, URL-routing override rejection,
  non-loopback TLS requirements, separate migration URL, and collector transaction boundaries.
  **Planned:** PostgreSQL 16 service roles with explicit grant/denial tests for every service.
- **Detective controls:** **Current:** schema integrity checks and parameterized persistence tests.
  **Planned:** audited authorization failures without query/credential leakage.
- **Recovery controls:** **Current:** reject unsafe connection configuration and transaction
  failures. **Planned:** revoke role, rotate credentials, restore verified backup, and audit repair.
- **Verification test:** Current collector persistence tests are in
  `tests/integration/test_collection_postgres.py`; Section 31 cases 6 and 35 remain incomplete as a
  full platform role matrix.
- **Residual risk:** Current migration does not provision or verify the target least-privilege roles;
  a database owner is outside the application trust boundary.
- **Owner/status:** Persistence/security maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-009 — Paper/live submission gate bypass

- **STRIDE class:** Elevation of privilege, Tampering.
- **Asset:** order authority, account/position state, paper-broker credentials.
- **Entry point:** CLI, YAML configuration, environment and secret files.
- **Precondition:** A caller can invoke execution code or alter tracked/runtime submission gates.
- **Attack/failure sequence:** A near-match acknowledgement, alternate endpoint, `paper=False`,
  unknown account, unsafe symbol, or skipped state transition reaches broker submission.
- **Impact:** Unauthorized paper order; a future regression could create real-money exposure.
- **Preventive controls:** **Current:** tracked submission disabled, literal `paper=True`, no live
  endpoint or alternate broker implementation, central legacy gate, intent-before-submit states,
  and deterministic client IDs. **Planned:** independent signed authorization and paper account hash
  gates in the target execution worker.
- **Detective controls:** **Current:** static repository scans, gate reason tests, state-machine
  tests, and reconciliation checks.
- **Recovery controls:** **Current:** deny before client construction/submission and persist legacy
  halt state for discrepancies. **Planned:** target durable latch, reconcile, and forced-flat
  incident workflow.
- **Verification test:** `tests/safety/test_static_repository_safety.py`,
  `tests/test_live_safety_matrix.py`, and runtime/profile gate tests.
- **Residual risk:** Legacy Alpaca paper interaction is not externally validated; the target signed
  risk/execution pipeline is not implemented. Real-money operation remains unsupported.
- **Owner/status:** Execution/security maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-010 — Ambiguous broker submission or duplicate fill

- **STRIDE class:** Repudiation, Tampering, Denial of service.
- **Asset:** order authority, account/position state, immutable evidence, audit history.
- **Entry point:** paper-broker API response through the execution boundary, PostgreSQL.
- **Precondition:** Timeout, disconnect, partial acknowledgement, duplicate update, or process crash
  occurs around submission or fill persistence.
- **Attack/failure sequence:** The worker retries blindly, submits twice, applies a fill twice, or
  assumes success despite unresolved broker state.
- **Impact:** Duplicate exposure, incorrect position/cash, or missing evidence of external effects.
- **Preventive controls:** **Current:** legacy intent-before-submit state machine, deterministic
  client IDs, idempotent fill/update handling, and reconciliation that blocks unknown broker state.
  **Planned:** target durable outbox, signed authorization, ambiguity latch, and fake/paper adapter
  parity.
- **Detective controls:** **Current:** reconciliation compares broker orders/positions and persists
  halt latches. **Planned:** explicit target incidents and audit-chain correlation.
- **Recovery controls:** **Current:** block on unknown order/position and persist halt state across
  restart. **Planned:** reconcile by client ID before any retry, then flatten or leave a durable
  incident on failure.
- **Verification test:** `tests/test_live_safety_matrix.py` reconciliation, latch, transition, and
  duplicate-update cases; target Section 31 cases 28–30 and 44 remain incomplete.
- **Residual risk:** External paper-broker timeout behavior is
  `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`; target execution and forced-flatten workflows are absent.
- **Owner/status:** Execution maintainer/operator — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-011 — Artifact substitution, traversal, or replay

- **STRIDE class:** Tampering, Spoofing, Repudiation.
- **Asset:** experiment and signal artifacts, market datasets, immutable evidence.
- **Entry point:** Parquet/JSON artifacts, YAML configuration, CLI.
- **Precondition:** A malicious local user or corrupted artifact can influence a path or replace
  serialized content between creation and use.
- **Attack/failure sequence:** A traversal/symlink escapes the application root, modified content is
  paired with a stale hash, or an artifact from another experiment/provider/policy is replayed.
- **Impact:** Wrong data or authorization is accepted with misleading provenance.
- **Preventive controls:** **Current:** canonical serialization/hashing, immutable experiment/config
  models, a mandatory experiment pin at composed profile loading, confined no-symlink configuration
  and secret paths, and bounded artifact-root syntax. **Planned:** immutable dataset manifests and
  signed signal/risk artifacts binding all required identities and freshness; strict Parquet/JSON/
  YAML schemas and size/hash limits; and rejection of `pickle`, `joblib`, `dill`, `cloudpickle`,
  `marshal`, untrusted `eval`/`exec`/`compile`, payload-selected subprocesses, and arbitrary dynamic
  imports.
- **Detective controls:** **Current:** known-answer canonical/hash and path-negative tests.
  **Planned:** manifest verification and durable artifact-to-decision audit correlation.
- **Recovery controls:** **Current:** reject mismatched configuration before service construction.
  **Planned:** quarantine corrupt artifacts and rebuild only from verified immutable evidence.
- **Verification test:** `tests/unit/test_platform_canonical.py`,
  `tests/unit/test_platform_experiment.py`, `tests/unit/test_platform_profiles.py`, and runtime path
  tests; Section 31 cases 7–8, 17–18, 40, and 42 require completion at every artifact and dynamic-
  code boundary.
- **Residual risk:** Frozen dataset manifests, signal signing, artifact quarantine, and the target
  deserialization/dynamic-code static guard are not implemented; ordinary host users with
  code-write authority remain trusted.
- **Owner/status:** Data/platform security maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-012 — Audit or evidence tampering

- **STRIDE class:** Repudiation, Tampering.
- **Asset:** immutable evidence, audit history, account/position state.
- **Entry point:** PostgreSQL, Parquet/JSON artifacts.
- **Precondition:** A service, database owner, local user, or compromised container can update,
  delete, reorder, or omit consequential records.
- **Attack/failure sequence:** Evidence is rewritten without detection, an audit event is omitted,
  or an exported report no longer matches durable source state.
- **Impact:** Decisions and side effects cannot be reconstructed or attributed reliably.
- **Preventive controls:** **Current:** collector raw observations are append-only and content
  addressed. **Planned:** append-only chained audit events, immutable decision receipts, manifest
  hashes, role-denied mutation, and transactional recording of every consequential transition.
- **Detective controls:** **Current:** collector trigger/schema verification. **Planned:** `aqa audit
  verify`, chain-tamper tests, and evidence-manifest comparison.
- **Recovery controls:** **Current:** preserve additional collector revisions rather than overwrite
  raw evidence. **Planned:** halt consequential work, restore from verified backup, and retain a
  durable incident describing any unrepairable gap.
- **Verification test:** Collector append-only cases in
  `tests/integration/test_collection_postgres.py`; Section 31 case 37 is not implemented for the
  target audit chain.
- **Residual risk:** Database-owner and host compromise can rewrite rows and backups; no audit-chain
  verifier or complete immutable evidence pipeline exists.
- **Owner/status:** Audit/persistence maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-013 — API input injection or resource abuse

- **STRIDE class:** Tampering, Information disclosure, Denial of service.
- **Asset:** service availability, market datasets, audit history.
- **Entry point:** FastAPI.
- **Precondition:** The target private API is reachable by a malicious remote caller or malformed
  local client.
- **Attack/failure sequence:** Unknown fields, SQL-like strings, traversal, arbitrary URLs,
  oversized bodies, high-cardinality labels, or repeated job requests cross an insufficiently
  bounded route.
- **Impact:** Service exhaustion, internal access, data mutation, secret leakage, or audit flooding.
- **Preventive controls:** **Current:** the target API is absent and generic configuration has no
  network authority. **Planned:** strict Pydantic schemas, size/rate bounds, fixed route inventory,
  parameter binding, confined paths, no caller URLs, bounded metric labels, and idempotent jobs.
- **Detective controls:** **Current:** architecture checks. **Planned:** safe rejection logs, bounded
  counters, audited job identity, and route-inventory tests.
- **Recovery controls:** **Current:** no reachable platform route. **Planned:** reject without detail,
  cancel/expire abusive jobs, rotate token if exposed, and restart from durable job state.
- **Verification test:** Section 31 cases 4–11, 36, 38, and 41 are planned.
- **Residual risk:** Single-process resource exhaustion and abuse by a holder of the valid operator
  token require deployment-level limits in addition to application validation.
- **Owner/status:** API maintainer — `NOT_IMPLEMENTED`.

### AQA-TM-014 — Compromised dashboard becomes a confused deputy

- **STRIDE class:** Elevation of privilege, Information disclosure.
- **Asset:** operator token, order authority, account/position state, market datasets.
- **Entry point:** Streamlit server, FastAPI.
- **Precondition:** A malicious remote caller reaches Streamlit, or the dashboard server is
  compromised while holding a token or inherited environment authority.
- **Attack/failure sequence:** The dashboard directly imports persistence/broker code, exposes data,
  or uses a full operator token to call control mutations despite presenting a read-only UI.
- **Impact:** Unauthorized control, credential disclosure, or data exfiltration.
- **Preventive controls:** **Current:** the legacy dashboard does not construct a broker; its
  documented shell launcher and Compose path strip legacy paper variables, but `app.py` itself can
  inherit ambient variables and reads SQLite directly. **Planned:** API-only read models, no
  database or broker imports, loopback/private bind, and a distinct server-enforced read-only
  authority.
- **Detective controls:** **Current:** legacy dashboard/reporting tests and documented authority gap.
  **Planned:** import-boundary, route-scope, response-schema, and credential-sentinel tests.
- **Recovery controls:** **Current:** stop the local dashboard and inspect host state. **Planned:**
  revoke its scoped credential and preserve an audited incident.
- **Verification test:** Existing dashboard/reporting tests cover legacy behavior; Section 31 cases
  5 and 21 and the target API authorization tests remain.
- **Residual risk:** The specification names one operator token, which cannot provide server-enforced
  read-only dashboard authority; this design conflict is unresolved.
- **Owner/status:** API/dashboard/security maintainers — `BLOCKED`.

### AQA-TM-015 — Compromised container crosses service boundaries

- **STRIDE class:** Elevation of privilege, Information disclosure, Tampering.
- **Asset:** all credentials, order authority, market datasets, account/position state,
  service availability.
- **Entry point:** Docker network, environment and secret files.
- **Precondition:** A dependency or service is compromised inside a container, or Compose grants
  broad networks, mounts, capabilities, or credentials.
- **Attack/failure sequence:** The container reads another service's secret, imports forbidden code,
  reaches the broker/database with excess authority, or writes shared artifacts.
- **Impact:** Lateral movement, credential theft, unauthorized order attempts, or durable corruption.
- **Preventive controls:** **Current:** a standalone collector package/import boundary omits trading
  clients and uses separate data credential names; tracked paper submission is disabled.
  **Planned:** one service per image, exact secret mounts, non-root/read-only filesystems, dropped
  capabilities, service networks, health checks, and least-privilege database roles.
- **Detective controls:** **Current:** collector import/static tests and Compose configuration
  validation. **Planned:** runtime mount/network/UID/capability tests and container scanning.
- **Recovery controls:** **Current:** stop the affected container and rotate exposed credentials.
  **Planned:** isolate network, rebuild from pinned images, restore verified state, and record an
  incident before resuming.
- **Verification test:** `tests/test_collection_credentials.py`, architecture tests, and current
  Compose validation; full runtime isolation matrix is absent.
- **Residual risk:** Compose networks are not outbound firewalls; host/root and container-runtime
  compromise remain outside application containment.
- **Owner/status:** Infrastructure/security maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-016 — CI or dependency supply-chain compromise

- **STRIDE class:** Tampering, Elevation of privilege, Information disclosure.
- **Asset:** source integrity, immutable evidence, all credentials, service availability.
- **Entry point:** GitHub Actions/dependencies.
- **Precondition:** A dependency/action/base image is compromised, a workflow gains write authority,
  or a developer introduces an unlocked or unsafe build step.
- **Attack/failure sequence:** Malicious code executes during install/test/build, modifies artifacts,
  exfiltrates available secrets, or publishes an unreviewed image/package.
- **Impact:** Compromised releases, developer machines, runtime containers, or repository history.
- **Preventive controls:** **Current:** locked `uv.lock` installation, least-privilege validation CI,
  no Alpaca secrets, offline tests, and pinned project dependencies. **Planned:** immutable action
  pinning throughout, secret/dependency/static/container scans, SBOMs, safe update policy, and no
  publishing without explicit authorization.
- **Detective controls:** **Current:** format, lint, type, test, migration, replay, and Compose/image
  validation gates. **Planned:** CodeQL, vulnerability, secret, and image scan gates with retained
  non-sensitive reports.
- **Recovery controls:** **Current:** revert a bad dependency/workflow through normal review and
  rebuild from the lock. **Planned:** revoke exposed credentials, quarantine artifacts, regenerate
  the lock after review, and issue a security advisory when applicable.
- **Verification test:** `.github/workflows/` and CI workflow tests/static review; Section 31 case 43
  remains incomplete for the full scan set.
- **Residual risk:** Locking preserves reproducibility, not trust; registry/action-owner compromise
  and malicious transitive code remain possible.
- **Owner/status:** Maintainers/security — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-017 — Availability loss through unbounded work or dependency failure

- **STRIDE class:** Denial of service.
- **Asset:** service availability, market datasets, account/position state.
- **Entry point:** market-data REST/WebSocket payloads, FastAPI, Streamlit server, PostgreSQL,
  Docker network.
- **Precondition:** A provider, caller, database, network, or process becomes slow, unavailable, or
  adversarially verbose.
- **Attack/failure sequence:** Unbounded pagination/frame volume, retry storms, blocked worker joins,
  oversized API work, database lock contention, or restart loops consume resources.
- **Impact:** Missed collection/decision windows, stale state, unavailable controls, or unsafe
  assumptions about service health.
- **Preventive controls:** **Current:** collector timeouts, page/observation bounds, capped retries,
  externally stoppable streaming, lease fencing, and completed-bar/session bounds. **Planned:** job
  budgets, API request/rate limits, worker deadlines, readiness/liveness contracts, and supervised
  restart policy for every target service.
- **Detective controls:** **Current:** collector run/failure events and readiness correlation.
  **Planned:** bounded Prometheus labels, stale-job/slot/watermark alerts, and service health APIs.
- **Recovery controls:** **Current:** stop on permanent failure, release lease, and resume with
  overlap/idempotent persistence. **Planned:** degraded read-only mode and documented restart/replay
  procedures across the full platform.
- **Verification test:** `tests/test_collection_alpaca.py`, `tests/test_collection_service.py`, and
  PostgreSQL lease tests; target API/job/health cases are absent.
- **Residual risk:** External provider and hosted database availability cannot be guaranteed;
  process-wide resource isolation and target service supervision are not implemented.
- **Owner/status:** Operations and service maintainers — `PARTIALLY_IMPLEMENTED`.

### AQA-TM-018 — Backup loss or corrupted recovery

- **STRIDE class:** Tampering, Denial of service, Repudiation.
- **Asset:** market datasets, account/position state, immutable evidence, audit history,
  service availability.
- **Entry point:** PostgreSQL, Parquet/JSON artifacts, CLI.
- **Precondition:** Storage fails, an operator restores the wrong backup/schema, or a backup contains
  silent corruption or credential material.
- **Attack/failure sequence:** Incomplete data is restored, migrations mismatch, hashes/rows/audit
  links are not checked, or services resume against ambiguous state.
- **Impact:** Permanent evidence loss, wrong positions, duplicate side effects, or an unverifiable
  operational history.
- **Preventive controls:** **Current:** collector content identities, immutable rows, schema-head
  checks, and repository exclusion of local data/secrets. **Planned:** documented logical PostgreSQL
  backup with versioned retention and encryption under operator control.
- **Detective controls:** **Current:** schema and collector integrity verification. **Planned:** an
  automated restore into a fresh PostgreSQL 16 database verifying migration head, row/hash/audit,
  slot, intent, fill, reconciliation, and absence of fixture secrets.
- **Recovery controls:** **Current:** no executed complete platform restore procedure. **Planned:**
  keep services fail-closed, restore a verified generation, reconcile external paper state, and
  record any evidence gap as an incident.
- **Verification test:** `REQ-OPS-004` restore smoke test is `NOT_IMPLEMENTED`.
- **Residual risk:** Backup availability, encryption, retention, and database-owner access are the
  deployment operator's responsibility; a restore cannot reconstruct unrecorded broker effects.
- **Owner/status:** Deployment operator/persistence maintainer — `NOT_IMPLEMENTED`.

## Threat-to-test index

| Threat | Current executable evidence | Required remaining evidence |
| --- | --- | --- |
| AQA-TM-001 | No reachable platform API | Security matrix 1–4, 41 |
| AQA-TM-002 | `tests/unit/test_platform_security.py`; `test_platform_secret_bootstrap.py`; `test_platform_runtime_settings.py` | Service mount/process sentinel tests |
| AQA-TM-003 | `test_platform_profiles.py`; `test_platform_runtime_settings.py`; `test_static_repository_safety.py` | Service-consumption integration tests |
| AQA-TM-004 | `test_platform_configuration_boundary.py` | Security matrix 12–18, 22–23, 40 |
| AQA-TM-005 | `test_collection_alpaca.py`; `test_collection_contracts.py`; `test_collection_service.py` | Gap/readiness and credentialed external validation |
| AQA-TM-006 | `test_collection_contracts.py`; `test_collection_postgres.py` | Frozen-dataset manifest replay |
| AQA-TM-007 | `test_collection_postgres.py` | Full platform state corruption and restore tests |
| AQA-TM-008 | Collector PostgreSQL integration tests | Security matrix 6, 35 across exact roles |
| AQA-TM-009 | `test_static_repository_safety.py`; `test_live_safety_matrix.py`; profile/runtime tests | Target signed execution gates |
| AQA-TM-010 | `test_live_safety_matrix.py` | Target security matrix 28–30, 44 |
| AQA-TM-011 | Canonical, experiment, profile, and runtime path unit tests | Dataset/signal manifest verification |
| AQA-TM-012 | Collector append-only trigger tests | Security matrix 37 and `aqa audit verify` |
| AQA-TM-013 | No reachable platform API | Security matrix 4–11, 36, 38, 41 |
| AQA-TM-014 | Legacy dashboard/reporting tests | API-backed dashboard import/auth/sentinel tests |
| AQA-TM-015 | Collector import/static tests; Compose config validation | Runtime container isolation and scan tests |
| AQA-TM-016 | Existing locked offline CI gates | Security matrix 43 and SBOM/scan gates |
| AQA-TM-017 | Collector bound/retry/stop/lease tests | API/job/health/restart tests |
| AQA-TM-018 | Collector schema/integrity tests | Fresh PostgreSQL 16 restore smoke test |

Paths in this table are relative to `tests/` unless shown otherwise. A planned case is not evidence
of a current control.

## Cross-cutting residual risks

- Host/root compromise and a malicious database owner are outside application-level containment.
- Credentialed Alpaca data and paper behavior remains externally unvalidated.
- Ordinary test socket denial covers current common Python TCP paths, not every process or native
  network path.
- The target API, API-backed dashboard, signed signal/risk/execution path, audit chain, service-role
  matrix, forced flatten, and backup/restore proof are not implemented.
- Compose network separation is not an outbound firewall, and target container hardening has not
  been runtime-verified.
- The single-token/read-only-dashboard authorization conflict must be resolved before the target API
  and dashboard can be considered safely implemented.
- Locally installed Python plugins remain operator-trusted; arbitrary Python sandboxing is
  `INTENTIONALLY_DEFERRED`.
- Availability, encryption at rest, physical access, database backups, and host firewalling remain
  deployment-operator responsibilities.

## Update rule

Update this register when an entry point, authority boundary, external dependency, durable state
machine, or recovery procedure changes. A status may advance only with the corresponding executable
test or an actually executed external procedure; documentation, mocks, and planned CI jobs alone do
not establish external validation.
