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
Actions/dependencies. Provider-backed paths remain profile-gated and externally unvalidated;
listing an entry point does not authorize activating it.

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
- **Preventive controls:** **Current:** bounded strict request schemas, startup rejection of short
  tokens, constant-time digest comparison, default-deny route authorization, private binding, no
  direct trade mutation route, and a distinct derived dashboard read scope.
- **Detective controls:** **Current:** bounded authentication-failure/rate-limit metrics, safe error
  envelopes, audit-backed control operations, and route-inventory tests without token material.
- **Recovery controls:** **Current:** stop the API, rotate the owner-private operator-token file,
  restart control-api so it atomically rotates the derived dashboard bearer, and inspect audit/job
  state before resuming.
- **Verification test:** `tests/unit/test_platform_control_api.py` covers malformed tokens,
  authenticated route inventory, constant-time comparison use, and read-only scope denial.
- **Residual risk:** A stolen valid operator bearer retains bounded operator authority until
  rotation; loopback/private binding is not a distributed denial-of-service control.
- **Owner/status:** API/security maintainer — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` (deployment
  exposure unverified).

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
  creates no Alpaca secrets and serializes threads/processes; service-scoped Compose mounts; and a
  launcher that materializes only the selected role URL in a process-private file.
- **Detective controls:** **Current:** sentinel, rendering, hostile-path, race, mode, bootstrap
  collision, partial-write, ambient-environment, entrypoint-permission, and declarative credential
  matrix tests. **Planned:** deployed mount and process inventory checks.
- **Recovery controls:** **Current:** unsafe or ambiguous file state fails closed; bootstrap never
  replaces an existing secret; stop affected services, revoke or rotate external credentials,
  replace local secrets through the rotation runbook, and verify no durable leak.
- **Verification test:** `tests/unit/test_platform_security.py`,
  `tests/unit/test_platform_secret_bootstrap.py`, and
  `tests/unit/test_platform_runtime_settings.py`; container process tests live in
  `tests/safety/test_container_entrypoint.py` and `test_devsecops_configuration.py`.
- **Residual risk:** Host/root compromise defeats file permissions; advisory locking can be
  ineffective on unsupported filesystems; legacy processes can still inherit environment secrets.
- **Owner/status:** Platform security maintainer — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

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
  classification, durable gap lifecycle and downstream readiness checks.
- **Detective controls:** **Current:** reconciliation provenance, durable collector events,
  checkpoints, content hashes, and negative REST/WebSocket tests.
- **Recovery controls:** **Current:** capped retries, bounded overlap restart, stop on permanent
  failures, durable run/lease cleanup, duplicate-safe replay, bounded gap repair and
  downstream readiness blocking through canonical datasets.
- **Verification test:** `tests/test_collection_alpaca.py`, `tests/test_collection_contracts.py`,
  and `tests/test_collection_service.py`.
- **Residual risk:** Credentialed Alpaca behavior is `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.
  The canonical offline command denies Python socket/DNS operations, and container probes use
  `--network none`; this does not establish a host-wide egress firewall for deployed workers.
- **Owner/status:** Market-data maintainer — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

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
- **Residual risk:** Frozen-dataset manifests and audit-chain verification are implemented, but
  a database owner can disable constraints or alter both data and locally stored evidence.
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
  non-loopback TLS requirements, separate migration URL, collector transaction boundaries, and
  PostgreSQL 16 service roles with explicit grant/denial tests.
- **Detective controls:** **Current:** schema integrity checks and parameterized persistence tests.
  **Planned:** audited authorization failures without query/credential leakage.
- **Recovery controls:** **Current:** reject unsafe connection configuration and transaction
  failures. **Planned:** revoke role, rotate credentials, restore verified backup, and audit repair.
- **Verification test:** `tests/integration/test_collection_postgres.py`,
  `tests/integration/test_platform_postgres_roles.py` and
  `tests/integration/test_platform_backup_restore.py` exercise persistence, scoped role grants,
  denials and restored ACLs using real PostgreSQL.
- **Residual risk:** A database owner is outside the application trust boundary; deployed role
  configuration and TLS still require operator-host validation.
- **Owner/status:** Persistence/security maintainers — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

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
  deterministic client IDs, independent signed authorization and paper account hash gates.
- **Detective controls:** **Current:** static repository scans, gate reason tests, state-machine
  tests, and reconciliation checks.
- **Recovery controls:** **Current:** deny before client construction/submission and persist legacy
  halt state for discrepancies; platform durable latches, reconciliation and forced-flat
  recovery preserve ambiguity rather than retrying blind.
- **Verification test:** `tests/safety/test_static_repository_safety.py`,
  `tests/test_live_safety_matrix.py`, and runtime/profile gate tests.
- **Residual risk:** Alpaca paper interaction is not externally validated. The signed platform
  pipeline is implemented and fake-tested, while Main AI approval stays default-deny and frozen.
  Real-money operation remains unsupported.
- **Owner/status:** Execution/security maintainers — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

### AQA-TM-010 — Ambiguous broker submission or duplicate fill

- **STRIDE class:** Repudiation, Tampering, Denial of service.
- **Asset:** order authority, account/position state, immutable evidence, audit history.
- **Entry point:** paper-broker API response through the execution boundary, PostgreSQL.
- **Precondition:** Timeout, disconnect, partial acknowledgement, duplicate update, or process crash
  occurs around submission or fill persistence.
- **Attack/failure sequence:** The worker retries blindly, submits twice, applies a fill twice, or
  assumes success despite unresolved broker state.
- **Impact:** Duplicate exposure, incorrect position/cash, or missing evidence of external effects.
- **Preventive controls:** **Current:** legacy and platform intent-before-submit states,
  deterministic client IDs, idempotent fill/update handling, signed authorization, durable
  ambiguity latches, and reconciliation that blocks unknown broker state.
- **Detective controls:** **Current:** reconciliation compares broker orders/positions, persists
  halt latches and correlates durable execution/audit evidence.
- **Recovery controls:** **Current:** block on unknown order/position, persist halt state across
  restart, reconcile by client ID before retry, and retain durable failure evidence when flattening
  cannot complete safely.
- **Verification test:** `tests/test_live_safety_matrix.py`,
  `tests/unit/test_platform_execution_failure_recovery.py`,
  `tests/unit/test_platform_execution_evidence_boundaries.py` and
  `tests/integration/test_platform_execution_postgres.py` exercise replay, restart, reversal and
  durable execution evidence.
- **Residual risk:** External paper-broker timing is unvalidated. Rounded provider average prices
  can fail exact execution-evidence reconciliation; the failure blocks further exposure.
- **Owner/status:** Execution maintainer/operator — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

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
  and secret paths, bounded artifact-root syntax, immutable dataset manifests, signed signal/risk
  artifacts binding identities and freshness, and strict Parquet/JSON/YAML schemas and limits.
  No public artifact interface accepts executable model serialization or payload-selected commands.
- **Detective controls:** **Current:** known-answer canonical/hash, path-negative, manifest
  verification and durable artifact/decision binding tests.
- **Recovery controls:** **Current:** reject mismatched configuration before service construction.
  **Planned:** quarantine corrupt artifacts and rebuild only from verified immutable evidence.
- **Verification test:** `tests/unit/test_platform_canonical.py`,
  `tests/unit/test_platform_experiment.py`, `tests/unit/test_platform_profiles.py`, and runtime path
  tests; Section 31 cases 7–8, 17–18, 40, and 42 require completion at every artifact and dynamic-
  code boundary.
- **Residual risk:** Frozen manifests, signed signal/risk contracts, confined artifact publication
  and rejection of corrupt artifacts are implemented. Automatic operational quarantine and a
  universal dynamic-code sandbox are not claimed; host users with code-write authority remain
  trusted. See `test_platform_dataset_authority_boundaries.py` and `test_platform_job_artifacts.py`.
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
- **Preventive controls:** **Current:** append-only content-addressed collector observations,
  chained audit events, immutable decision receipts, manifest hashes, role-denied mutation and
  transactional execution evidence.
- **Detective controls:** **Current:** collector trigger/schema verification, `aqa audit verify`,
  chain-tamper tests and evidence-manifest comparison.
- **Recovery controls:** **Current:** preserve additional collector revisions rather than overwrite
  raw evidence. **Planned:** halt consequential work, restore from verified backup, and retain a
  durable incident describing any unrepairable gap.
- **Verification test:** `tests/integration/test_collection_postgres.py`,
  `tests/integration/test_platform_audit_postgres.py`, `tests/unit/test_platform_audit_repository.py`
  and `tests/unit/test_platform_audit_cli.py`.
- **Residual risk:** Database-owner and host compromise can rewrite rows, evidence and backups;
  a local hash chain cannot substitute for an independently protected external checkpoint.
- **Owner/status:** Audit/persistence maintainers — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

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
- **Preventive controls:** **Current:** strict Pydantic schemas, request and pagination bounds,
  fixed route inventory, parameterized queries, no caller-supplied URLs, bounded metric labels,
  independent read/mutation rate limits, and idempotent jobs.
- **Detective controls:** **Current:** safe rejection envelopes, bounded counters, audited job
  identity, route-inventory tests, and secret-sentinel tests.
- **Recovery controls:** **Current:** reject without sensitive detail, rotate the operator token if
  exposed, and resume idempotent work from durable job state.
- **Verification test:** `tests/unit/test_platform_control_api.py`, architecture boundary tests,
  job tests, and observability tests cover Section 31 cases 4–11, 36, 38, and 41.
- **Residual risk:** Single-process resource exhaustion and abuse by a holder of the valid operator
  token require deployment-level limits in addition to application validation.
- **Owner/status:** API maintainer — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` (deployment load
  testing pending).

### AQA-TM-014 — Compromised dashboard becomes a confused deputy

- **STRIDE class:** Elevation of privilege, Information disclosure.
- **Asset:** operator token, order authority, account/position state, market datasets.
- **Entry point:** Streamlit server, FastAPI.
- **Precondition:** A malicious remote caller reaches Streamlit, or the dashboard server is
  compromised while holding a token or inherited environment authority.
- **Attack/failure sequence:** The dashboard directly imports persistence/broker code, exposes data,
  or uses a full operator token to call control mutations despite presenting a read-only UI.
- **Impact:** Unauthorized control, credential disclosure, or data exfiltration.
- **Preventive controls:** **Current:** the platform dashboard uses API-only read models, imports no
  database or broker modules, joins only the private control network, mounts no base operator
  secret, and receives a domain-separated HMAC bearer through a read-only dedicated volume. The
  API enforces that bearer's `read_only` scope.
- **Detective controls:** **Current:** import-boundary, route-scope, response-schema, credential
  separation, entrypoint permission/symlink, and secret-sentinel tests.
- **Recovery controls:** **Current:** stop the dashboard and API, rotate the operator token, restart
  control-api to atomically rotate the derived bearer, inspect audit/job state, and then restart the
  dashboard.
- **Verification test:** `tests/unit/test_platform_dashboard.py`,
  `tests/unit/test_platform_control_api.py`, `tests/safety/test_container_entrypoint.py`, and
  `tests/safety/test_devsecops_configuration.py` cover Section 31 cases 5 and 21 plus mutation-scope
  denial.
- **Residual risk:** Docker/host administrators can inspect the named volume; dashboard compromise
  can disclose read models and consume read-rate capacity until credential rotation.
- **Owner/status:** API/dashboard/security maintainers — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`
  (deployment exposure unverified).

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
- **Preventive controls:** **Current:** a data-only collector target omits the trading SDK; Compose
  uses process-specific secret mounts, numeric non-root users, read-only filesystems, dropped
  capabilities, separate internal/provider networks, health checks, resource limits, and
  least-privilege database roles. Tracked paper submission is disabled.
- **Detective controls:** **Current:** collector import/static tests, declarative container-isolation
  tests, Compose validation, and a pinned Trivy image-scan workflow. A completed scan on a deployed
  image remains external evidence.
- **Recovery controls:** **Current:** stop the affected container and rotate exposed credentials.
  **Planned:** isolate network, rebuild from pinned images, restore verified state, and record an
  incident before resuming.
- **Verification test:** `tests/test_collection_credentials.py`, architecture tests,
  `tests/safety/test_devsecops_configuration.py`, and Compose validation. Host-runtime isolation is
  not proven by static configuration.
- **Residual risk:** Compose networks are not outbound firewalls; host/root and container-runtime
  compromise remain outside application containment.
- **Owner/status:** Infrastructure/security maintainers — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`
  (remote image-scan evidence pending).

### AQA-TM-016 — CI or dependency supply-chain compromise

- **STRIDE class:** Tampering, Elevation of privilege, Information disclosure.
- **Asset:** source integrity, immutable evidence, all credentials, service availability.
- **Entry point:** GitHub Actions/dependencies.
- **Precondition:** A dependency/action/base image is compromised, a workflow gains write authority,
  or a developer introduces an unlocked or unsafe build step.
- **Attack/failure sequence:** Malicious code executes during install/test/build, modifies artifacts,
  exfiltrates available secrets, or publishes an unreviewed image/package.
- **Impact:** Compromised releases, developer machines, runtime containers, or repository history.
- **Preventive controls:** **Current:** locked `uv.lock` installation, least-privilege workflows,
  no Alpaca secrets, offline tests, immutable action pins, bounded Dependabot updates, and no image
  publishing or release signing. Dependency/security tools are in a dedicated locked group.
- **Detective controls:** **Current:** format, lint, type, test, migration, replay, Compose, Gitleaks,
  pip-audit, Bandit, CodeQL, Trivy, and SBOM workflow gates. A checked-in gate is not evidence of a
  successful future remote run.
- **Recovery controls:** **Current:** revert a bad dependency/workflow through normal review and
  rebuild from the lock. **Planned:** revoke exposed credentials, quarantine artifacts, regenerate
  the lock after review, and issue a security advisory when applicable.
- **Verification test:** `.github/workflows/`, `.github/dependabot.yml`,
  `.pre-commit-config.yaml`, and `tests/safety/test_devsecops_configuration.py`; remote scan results
  remain external evidence.
- **Residual risk:** Locking preserves reproducibility, not trust; registry/action-owner compromise
  and malicious transitive code remain possible.
- **Owner/status:** Maintainers/security — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` (remote workflow
  evidence pending).

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
  externally stoppable streaming, lease fencing, completed-bar/session bounds, durable bounded jobs,
  API request/rate limits, health contracts, and Compose resource/restart bounds.
- **Detective controls:** **Current:** collector run/failure events and readiness correlation, bounded
  Prometheus labels, stale job/slot/watermark metrics, and service health/readiness API routes.
- **Recovery controls:** **Current:** stop on permanent failure, release leases, resume with
  overlap/idempotent persistence, reclaim expired jobs, and follow documented restart/replay
  procedures. A failed safety dependency keeps consequential work unavailable.
- **Verification test:** `tests/test_collection_alpaca.py`, `tests/test_collection_service.py`,
  PostgreSQL lease tests, platform job/observability/control API tests, and declarative container
  health tests.
- **Residual risk:** External provider and hosted database availability cannot be guaranteed.
  Compose resource bounds, health probes and worker signal handling are implemented; their
  enforcement on the operator's host and sustained-load behaviour remain unvalidated.
- **Owner/status:** Operations and service maintainers — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`
  (deployment load and supervision evidence pending).

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
  checks, repository exclusion of local data/secrets, documented logical backup/restore commands,
  exact loopback/disposable-database guards, and credential-shaped fixture rejection.
- **Detective controls:** **Current:** the restore smoke creates a fresh database, runs/checks
  migrations, and verifies row counts, content hashes, audit root, slots, intents, fills,
  reconciliation, and absence of fixture secrets. Its real PostgreSQL path has not run in this
  environment.
- **Recovery controls:** **Current:** keep services fail-closed, restore a verified generation,
  reconcile external paper state, and record any evidence gap as an incident. Backup encryption,
  retention, and storage generation remain operator controls.
- **Verification test:** guard behavior is covered by
  `tests/unit/test_backup_restore_smoke_guard.py`; the real procedure is
  `tests/integration/test_platform_backup_restore.py` and skips without the explicit disposable
  PostgreSQL environment.
- **Residual risk:** Backup availability, encryption, retention, and database-owner access are the
  deployment operator's responsibility; a restore cannot reconstruct unrecorded broker effects.
- **Owner/status:** Deployment operator/persistence maintainer — `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`.

## Threat-to-test index

| Threat | Current executable evidence | Required remaining evidence |
| --- | --- | --- |
| AQA-TM-001 | Control API authentication, authorization, safe-error, and route-inventory tests | Deployed exposure and token-rotation exercise |
| AQA-TM-002 | Secret loader/bootstrap/runtime tests; container entrypoint and credential-matrix tests | Deployed mount and process inventory |
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
| AQA-TM-013 | Control API request, route, job, and metric tests | Deployment load/abuse validation |
| AQA-TM-014 | Dashboard import/client tests; derived-token scope, entrypoint, and Compose isolation tests | Deployed credential-rotation exercise |
| AQA-TM-015 | Collector import/static tests; Compose/runtime-boundary tests and local locked image probes | Completed remote Trivy scan and deployed isolation review |
| AQA-TM-016 | Locked offline CI, pre-commit, local dependency/static scans, and SBOM generation | Completed remote Gitleaks, Trivy, and CodeQL runs |
| AQA-TM-017 | Collector bounds/retry/lease plus platform job, API, observability, and container-health tests | Deployment load and restart drill |
| AQA-TM-018 | Restore guard unit tests and a guarded PostgreSQL integration procedure | Completed fresh PostgreSQL 16 restore smoke run |

Paths in this table are relative to `tests/` unless shown otherwise. A planned case is not evidence
of a current control.

## Cross-cutting residual risks

- Host/root compromise and a malicious database owner are outside application-level containment.
- Credentialed Alpaca data and paper behavior remains externally unvalidated.
- Ordinary test socket denial covers current common Python TCP paths, not every process or native
  network path.
- A completed signed signal/risk/execution deployment, forced-flatten drill, and backup/restore
  proof remain external operational evidence.
- Compose network separation is not an outbound firewall. Local offline probes verified numeric
  non-root operation, immutable code ownership, data-only dependencies, and the dashboard token
  volume; the target host/container runtime remains a deployment trust boundary.
- Locally installed Python plugins remain operator-trusted; arbitrary Python sandboxing is
  `INTENTIONALLY_DEFERRED`.
- Availability, encryption at rest, physical access, database backups, and host firewalling remain
  deployment-operator responsibilities.

## Update rule

Update this register when an entry point, authority boundary, external dependency, durable state
machine, or recovery procedure changes. A status may advance only with the corresponding executable
test or an actually executed external procedure; documentation, mocks, and planned CI jobs alone do
not establish external validation.
