# Non-AI requirement traceability

This index covers all **173 unique requirement IDs** currently present in [requirements](../requirements.md). IDs and normative formulas are unchanged. Each row retains the normative implementation location and verification description; the family indexes below resolve concrete existing test paths. A mapped test is a review starting point, not a claim that it alone proves every clause.

The [current verification ledger](non-ai-architecture-verification.md) is authoritative for current command results and acceptance blockers. The latest reported offline run has **3,039 passing tests**; the settled PostgreSQL suite also passed 160 tests, with 82.19% repository and 85.69% platform branch-enabled coverage. All three images built, but the image security gate is **BLOCKED**: each image reports 54 OS findings (51 HIGH and 3 CRITICAL). Image build success does not satisfy that gate. Historical statuses in requirements are not promoted by this index. Local source/test presence is distinct from passing current whole-suite, PostgreSQL, coverage, container, backup, package, security and external-service validation.

## Frozen scope and external validation

`REQ-USER-001` and `REQ-SCOPE-002` preserve the protected main-AI implementation under **OUT_OF_SCOPE_FROZEN_AI**. Their protection checks remain in scope (`tests/safety/test_main_ai_freeze.py` and the reviewed manifest); changing training, inference internals, model loading or approval is outside scope. `REQ-SIGNAL-004` remains an implemented non-AI fail-closed approval contract, not permission to implement model approval. No requirement ID is silently omitted as frozen.

No live data request, paper order, broker credential, hosted scheduler activation or deployment is required for local implementation completion. Those external validations remain unavailable unless explicitly executed and recorded. Container/runtime availability and final coverage thresholds remain verification gates rather than evidence implied by source presence.

## Current integration and review corrections

Current-clock PostgreSQL scheduling is implemented in `platform/operational_scheduler.py`; strategy production in `platform/operational_strategy.py`; execution-role dry-run consumption in `platform/shadow.py`, with role-specific settings in `platform/shadow_settings.py`. Migration `20260906_0015_operational_readiness_views.py` exposes only bounded readiness authority. These replace the older configuration-only shadow description. `data status` now reads bounded persisted watermarks and `scheduler status` reads current-session durable slots; configured state, observed/empty/unavailable results and overall health are distinct. Fixture schedule preview requires `--preview`.

Bounded review repaired schedule-wide PostgreSQL audit lock ordering, limited risk history reads to the required preceding 20 sessions and active symbols, and rejected expired shadow leases. Focused tests exist at `tests/unit/test_operational_scheduler.py`, `test_platform_history_boundaries.py`, and `test_platform_collection_cycles.py`; current PostgreSQL, coverage and combined acceptance results are recorded in the [current verification ledger](non-ai-architecture-verification.md). These focused fixes do not establish complete acceptance; the image security blocker remains explicit above and in that ledger.

## Requirement inventory

| Requirement | Normative implementation / inspected source pointer | Verification obligation | Test index | Current acceptance |
| --- | --- | --- | --- | --- |
| `REQ-USER-001` | 61-file manifest and verified hashes; scope `OUT_OF_SCOPE_FROZEN_AI` | Completion directive acceptance and boundary tests | [USER](#tests-user) | Frozen AI preservation; protection evidence in final ledger |
| `REQ-USER-002` | Complete source/harness review; evidence and blockers in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [USER](#tests-user) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-USER-003` | Service settings, image boundaries, execution gates and safety tests; review outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [USER](#tests-user) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-COMPLETE-001` | `platform/scheduling`, `risk`, `execution`, `api`, `control`, `dashboard`, worker runtime and focused tests; acceptance outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [COMPLETE](#tests-complete) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-COMPLETE-002` | Makefile, scripts, workflows and runbooks; combined execution outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [COMPLETE](#tests-complete) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCHEDULED-001` | `aqa data collect-once`, `collection/service.py`, canonical/recovery/derived modules, entrypoint/service tests | Completion directive acceptance and boundary tests | [SCHEDULED](#tests-scheduled) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCHEDULED-002` | `.github/workflows/market-data.yml`; `tests/safety/test_scheduled_collection_workflow.py`; no remote invocation performed | Completion directive acceptance and boundary tests | [SCHEDULED](#tests-scheduled) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCHEDULED-003` | Collection runtime validation, workflow secret-file cleanup, `docs/scheduled_market_data.md`, credentials and operations tests | Completion directive acceptance and boundary tests | [SCHEDULED](#tests-scheduled) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-001` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-002` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-003` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-004` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-005` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-006` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-007` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-008` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-009` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-010` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-011` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-012` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-013` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-014` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-015` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-016` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-017` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-018` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-019` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-020` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-021` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-022` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PZ-023` | Inspected collector/data code and focused tests; combined verification outcome in [current verification ledger](non-ai-architecture-verification.md) | Completion directive acceptance and boundary tests | [PZ](#tests-pz) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-001` | `docs/execution-plans/PLANS.md`; review process | Requirement trace plus review packet and command evidence | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-002` | This ledger; active plan; final report | Documentation scan | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-003` | Active plan and milestone workflow | Recorded reconnaissance and baseline evidence | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-004` | Git workflow | Status/diff/history inspection | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-005` | `.gitignore`, `.dockerignore`, review protocol | Secret scan, staged-file review, `git status --short` | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-006` | `docs/execution-plans/PLANS.md` | Diff statistics and review packet | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-007` | Review workflow | Captured review packet and approval | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-008` | Git workflow | `git show --format=fuller`, remote/status inspection | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-009` | Active plan; source/tests by milestone | Milestone evidence | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-010` | Requirements ledger, active plan, final report | Evidence audit | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-011` | Review packet; `docs/code-review.md` | Review record plus rerun checks | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-GOV-012` | Final review records | Finding ledger and clean re-review | [GOV](#tests-gov) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-QUAL-001` | Whole repository; coding standards | Design/diff review with named consumer and removed alternative | [QUAL](#tests-qual) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-QUAL-002` | Whole repository; static checks and review | Static scan, test review, simplification review | [QUAL](#tests-qual) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-QUAL-003` | New platform modules | Type, architecture, behavior, and diff review | [QUAL](#tests-qual) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-QUAL-004` | Whole repository | Diff/comment review and linting | [QUAL](#tests-qual) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-QUAL-005` | Workers and external adapters | Failure-injection tests and exception-path review | [QUAL](#tests-qual) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-001` | `src/adaptive_trader/platform/`; profiles | Mode/route/import tests and offline end-to-end test | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-002` | `configs/platform/offline.yaml`; platform demo | Socket-denied demo and startup tests | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-003` | `configs/platform/shadow.yaml`; strategy worker | Shadow integration and import-boundary tests | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-004` | paper profile; authorization verifier; paper adapter | Every-gate negative tests and prohibited-endpoint scan | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-005` | `platform/signals`, `platform/risk`, `platform/execution` | Import-boundary, proposal-tamper, and risk-bypass tests | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-006` | repositories, audit chain, latches | Failure injection, tamper, ambiguity, and restart tests | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-007` | package metadata, CLI, Compose, README, `examples/` | Clean-environment quickstart and extension validation | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-FUNC-008` | `pyproject.toml`; composed CLI; compatibility adapters | Legacy suite/backtest/replay and alias smoke tests | [FUNC](#tests-func) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCOPE-001` | platform universe/config; experiment YAML | Static symbol scan and config contract tests | [SCOPE](#tests-scope) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCOPE-002` | Whole repository | Diff inspection and legacy regression hashes | [SCOPE](#tests-scope) | Frozen AI preservation; protection evidence in final ledger |
| `REQ-SCOPE-003` | Dependency and file manifests | Manifest/language scan | [SCOPE](#tests-scope) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCOPE-004` | `docs/performance.md`; ADRs | Benchmark and ADR presence before such dependencies | [SCOPE](#tests-scope) | Intentionally deferred by normative scope |
| `REQ-ARCH-001` | `src/adaptive_trader/platform/` | Tree/content inspection and import tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-002` | Platform packages and architecture tests | AST/import-boundary tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-003` | `platform/data/`; collector container/role | Import, credential-mount, grant, URL, and subprocess tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-004` | `platform/scheduling/`; scheduler role/container | Import/grant/container tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-005` | `platform/signals/`; strategy role/container | Import/grant/config tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-006` | `platform/control/`; control role/container | Route inventory, import, auth, schema, and grant tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-007` | `platform/dashboard/`; Compose | Import/config/client-route tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-008` | `platform/execution/`; execution role/container | Import, credential matrix, endpoint, and grant tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-009` | `tests/architecture/`, `tests/safety/` | Isolated subprocess and AST/static tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-ARCH-010` | `pyproject.toml`; platform modules; containers | Locked install, package, type, service, and dependency tests | [ARCH](#tests-arch) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-001` | `platform/config.py`, `platform/universe.py`, `platform/domain.py` | Positive/unknown-field/mutation/hash tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-002` | `platform/universe.py` | Boundary/property tests including case collisions | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-003` | `configs/experiments/semiconductor_network_intraday_v1.yaml` | Known-answer parse and content-hash test | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-004` | experiment loader | Known-answer hash, mutation, and WDC/SNDK tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-005` | `configs/platform/*.yaml`; platform CLI | YAML/doctor/client-construction tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-006` | `platform/canonical.py`, `platform/hashing.py` | Independently calculated byte/hash tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-007` | platform domain modules | Repeated-run and known-answer tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-008` | `platform/data/calendar.py`; scheduler/jobs | DST/early-close/fake-clock tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CONFIG-009` | domain/risk/execution/storage models | Known-answer, nonfinite, rounding, and conservation tests | [CONFIG](#tests-config) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-001` | `platform/security.py`, config, Compose | Environment inventory and startup tests | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-002` | `platform/security.py` | File-mode/symlink/NUL/serialization/log tests | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-003` | `scripts/bootstrap_local.py`; `platform.security`; CLI | Rerun, modes, output, collision, failure, and concurrency tests | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-004` | centralized redaction; security tests | Six-sentinel end-to-end redaction suite | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-005` | Incident workflow | Committed-history secret scan | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-001` | `platform/domain.py`, `platform/data/normalization.py` | Construction and canonical hash tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-002` | normalization/provider boundary | Positive, negative, and property tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-003` | `platform/data/normalization.py`, provider adapter | Historical/stream parity fixtures | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-004` | storage repositories/tables | Known-answer, rollback, concurrent duplicate/correction tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-005` | `platform/data/watermarks.py`, collector, repositories | Holiday/overnight/repair/correction tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-006` | `platform/data/aggregation.py` | Independent known-answer fixture | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-007` | aggregation/materialization service | Permutation, correction, calendar, restart tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-008` | `platform/data/watermarks.py`; repositories | Gap, rollback, restart, and basket tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-009` | `platform/data/provider.py`, `collector.py` | Fake-client contract, import, endpoint, and socket-denial tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DATA-010` | platform collector service/container | Retry-clock, rate-limit, restart, ordering, redaction tests | [DATA](#tests-data) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-STORE-001` | `platform/storage/engine.py`; Compose | Dialect, redaction, SQL-injection, and PG16 integration tests | [STORE](#tests-store) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-STORE-002` | `alembic.ini`, `migrations/` | Empty DB, prior-state upgrade, downgrade refusal, drift tests | [STORE](#tests-store) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-STORE-003` | `platform/storage/tables.py`; migrations | Metadata and PostgreSQL constraint tests | [STORE](#tests-store) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-STORE-004` | bootstrap SQL/migrations; role tests | PG16 role/grant integration matrix | [STORE](#tests-store) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-STORE-005` | `platform/storage/repositories.py` | Transaction rollback/concurrency tests | [STORE](#tests-store) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-STORE-006` | `platform/data/datasets.py`; artifact store | Byte/logical identity, correction, overwrite, and path tests | [STORE](#tests-store) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCHED-001` | `platform/scheduling/` | Known-answer full-session/DST/early-close tests | [SCHED](#tests-sched) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCHED-002` | scheduler models/service/repository | Concurrency, expiry, restart, and missed-deadline tests | [SCHED](#tests-sched) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SCHED-003` | calendar/scheduler; ADR | Early-close integration test | [SCHED](#tests-sched) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SIGNAL-001` | `platform/signals/models.py` | Known-answer hash and boundary/tamper tests | [SIGNAL](#tests-signal) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SIGNAL-002` | `platform/signals/providers.py`; package metadata | Discovery and malicious-config tests | [SIGNAL](#tests-signal) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SIGNAL-003` | provider implementations | Known-answer provider tests | [SIGNAL](#tests-signal) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SIGNAL-004` | authorization verifier | Negative authorization tests | [SIGNAL](#tests-signal) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-001` | `platform/risk/` | Input matrix and legacy regressions | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-002` | `risk/statistics.py` | Independent numeric known-answer tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-003` | `risk/policy.py` | Independent numeric and degenerate-case tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-004` | `risk/policy.py` | Per-constraint/order/convergence/property tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-005` | risk eligibility | Negative matrix and compromised-proposal tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-006` | risk policy | Boundary tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-007` | `risk/latches.py`; API control | Threshold, restart, auth, acknowledgement, append-only tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-RISK-008` | risk models/repository | Persistence/hash/replay tests | [RISK](#tests-risk) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-001` | `platform/execution/planner.py` | Positive/negative/zero rounding tests | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-002` | execution models/planner | Transition state table tests | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-003` | execution service/reconciliation | Every reversal failure-boundary and restart test | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-004` | planner/service/repository | Known-ID, sorting, bound, restart, and side-effect-order tests | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-005` | execution models/service | State-machine, timeout, duplicate, reconnect tests | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-006` | `execution/authorization.py`, broker adapter boundary | Gate matrix, static endpoint scan, socket denial | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-007` | `execution/broker.py` | Accounting/state/restart scenario matrix | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-008` | `execution/reconciliation.py` | Every-discrepancy and tolerance tests | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-EXEC-009` | execution/scheduler services | Success, partial failure, restart, and deadline tests | [EXEC](#tests-exec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-JOB-001` | `platform/jobs/`; storage | Concurrency, retry, expiry, rollback, replay tests | [JOB](#tests-job) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-JOB-002` | job schemas/service; API | Strict-schema and route inventory tests | [JOB](#tests-job) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-API-001` | `platform/control/` | Startup/auth/limit/rate/header tests | [API](#tests-api) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-API-002` | control API app/models | OpenAPI/route inventory and auth tests | [API](#tests-api) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-API-003` | control middleware/models | Contract, malformed/unknown/duplicate/security-header tests | [API](#tests-api) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-006` | artifact store/API schemas | Traversal/symlink/overwrite tests | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-007` | provider/API/job schemas | Route/schema and malicious-value tests | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-008` | platform runtime and architecture tests | Static scan and malformed artifact tests | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-UI-001` | `platform/dashboard/` | Client/route/import/credential tests | [UI](#tests-ui) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OBS-001` | `platform/observability/logging.py` | Sentinel and structured-schema tests | [OBS](#tests-obs) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OBS-002` | audit repository; CLI | Known-chain, tamper, concurrency, and command tests | [OBS](#tests-obs) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OBS-003` | `platform/observability/metrics.py` | Metric inventory/cardinality/sentinel tests | [OBS](#tests-obs) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OBS-004` | platform workers/health | Cancellation, timeout, shutdown, and health tests | [OBS](#tests-obs) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OPS-001` | `Dockerfile`, `.dockerignore` | Image history/config/build and nonroot/read-only tests | [OPS](#tests-ops) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OPS-002` | `docker-compose.yml` | Compose config and service/profile startup tests | [OPS](#tests-ops) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OPS-003` | Compose | Static config plus runtime privilege/network/mount tests | [OPS](#tests-ops) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CI-001` | `.pre-commit-config.yaml` | `pre-commit run --all-files` | [CI](#tests-ci) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CI-002` | `.github/workflows/` | Workflow lint plus successful remote runs | [CI](#tests-ci) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CI-003` | Makefile/workflows | Workflow inspection and local/CI command comparison | [CI](#tests-ci) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CI-004` | Dependabot; `docs/dependency-policy.md`; lockfile | Config validation, lock check, vulnerability scan | [CI](#tests-ci) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-CI-005` | CI scripts/workflow | SBOM schema/artifact inspection | [CI](#tests-ci) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-009` | `docs/threat_model.md` | Threat-to-test trace audit | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-010` | `SECURITY.md`; named docs | Documentation/evidence review | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-SEC-011` | `tests/security/`, `tests/architecture/`, integration tests | Security matrix execution | [SEC](#tests-sec) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-OPS-004` | `docs/backup_restore.md`; integration script/test | Automated restore smoke test | [OPS](#tests-ops) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DX-001` | `platform/cli.py`; `pyproject.toml` | CLI help/exit/output/secret/network tests | [DX](#tests-dx) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DX-002` | package metadata; compatibility docs; CI | Build and fresh-environment smoke tests | [DX](#tests-dx) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DX-003` | repository root; `.github/` | File/content/license review | [DX](#tests-dx) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DX-004` | `README.md` | Quickstart execution and documentation claim audit | [DX](#tests-dx) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DX-005` | `examples/`; tests/docs | Import boundary and runnable validation | [DX](#tests-dx) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DOC-001` | `AGENTS.md`, `ARCHITECTURE.md`, `docs/` | Link/content/claim review | [DOC](#tests-doc) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DOC-002` | `ARCHITECTURE.md`; architecture tests | Architecture/implementation consistency review | [DOC](#tests-doc) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DOC-003` | named docs; `pyproject.toml`; Makefile | Documentation-to-config/command audit | [DOC](#tests-doc) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DOC-004` | `docs/`; `docs/adr/` | Required-file and current-state audit | [DOC](#tests-doc) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DOC-005` | `docs/resume_evidence.md` | Evidence provenance audit | [DOC](#tests-doc) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-HARNESS-001` | `Makefile` or equivalent | Execute each documented target | [HARNESS](#tests-harness) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-HARNESS-002` | `.agents/skills/*/SKILL.md` | Frontmatter/content and representative workflow validation | [HARNESS](#tests-harness) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-HARNESS-003` | `.vscode/`; `.editorconfig` | Settings/schema and CLI parity review | [HARNESS](#tests-harness) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-HARNESS-004` | project-local hook config; validation scripts | Hook schema/test/observed activation | [HARNESS](#tests-harness) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-TEST-001` | `tests/` by boundary | Test review and focused/full suite | [TEST](#tests-test) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-TEST-002` | test fixtures and suites | Socket denial, repeat/shuffle, fixture review | [TEST](#tests-test) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-TEST-003` | platform unit/component/integration/e2e tests | Requirement-to-test matrix | [TEST](#tests-test) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-TEST-004` | coverage config/CI | Branch coverage reports and ratchet | [TEST](#tests-test) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-TEST-005` | canonical `check`; CI | Exact command result ledger | [TEST](#tests-test) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-TEST-006` | socket guard script/fixture; workflows | Network-denial test plus final safety confirmation | [TEST](#tests-test) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DEMO-001` | `platform/demo.py`; CLI | Fresh-state offline end-to-end test | [DEMO](#tests-demo) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DEMO-002` | demo evidence manifest; CI | Twice-run hash comparison | [DEMO](#tests-demo) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-DEMO-003` | demo failure injection and tests | Scenario matrix | [DEMO](#tests-demo) | [current verification ledger](non-ai-architecture-verification.md) |
| `REQ-PERF-001` | `scripts/benchmark_pipeline.py`; `docs/performance.md` | Repeatable benchmark smoke and result schema | [PERF](#tests-perf) | [current verification ledger](non-ai-architecture-verification.md) |

## Concrete verification paths

The following boundary indexes contain only files present at generation time. Procedural governance requirements additionally need the final review/command records; tests do not prove operator approval or publication history.

### Tests API

[`tests/unit/test_platform_control_api.py`](../../tests/unit/test_platform_control_api.py), [`tests/unit/test_platform_control_queries.py`](../../tests/unit/test_platform_control_queries.py), [`tests/unit/test_platform_operator_controls.py`](../../tests/unit/test_platform_operator_controls.py)

### Tests ARCH

[`tests/architecture/test_platform_configuration_boundary.py`](../../tests/architecture/test_platform_configuration_boundary.py), [`tests/architecture/test_platform_control_boundary.py`](../../tests/architecture/test_platform_control_boundary.py), [`tests/architecture/test_platform_data_boundary.py`](../../tests/architecture/test_platform_data_boundary.py), [`tests/architecture/test_platform_domain_boundary.py`](../../tests/architecture/test_platform_domain_boundary.py), [`tests/architecture/test_platform_execution_boundary.py`](../../tests/architecture/test_platform_execution_boundary.py), [`tests/architecture/test_platform_risk_boundary.py`](../../tests/architecture/test_platform_risk_boundary.py), [`tests/architecture/test_platform_signal_boundary.py`](../../tests/architecture/test_platform_signal_boundary.py), [`tests/architecture/test_platform_storage_boundary.py`](../../tests/architecture/test_platform_storage_boundary.py), [`tests/integration/test_platform_postgres_roles.py`](../../tests/integration/test_platform_postgres_roles.py)

### Tests CI

[`tests/safety/test_ci_workflow.py`](../../tests/safety/test_ci_workflow.py), [`tests/safety/test_devsecops_configuration.py`](../../tests/safety/test_devsecops_configuration.py)

### Tests COMPLETE

[`tests/safety/test_devsecops_configuration.py`](../../tests/safety/test_devsecops_configuration.py), [`tests/unit/test_operational_scheduler.py`](../../tests/unit/test_operational_scheduler.py), [`tests/unit/test_platform_demo.py`](../../tests/unit/test_platform_demo.py), [`tests/unit/test_platform_shadow.py`](../../tests/unit/test_platform_shadow.py)

### Tests CONFIG

[`tests/unit/test_platform_canonical.py`](../../tests/unit/test_platform_canonical.py), [`tests/unit/test_platform_experiment.py`](../../tests/unit/test_platform_experiment.py), [`tests/unit/test_platform_properties.py`](../../tests/unit/test_platform_properties.py), [`tests/unit/test_platform_runtime_settings.py`](../../tests/unit/test_platform_runtime_settings.py)

### Tests DATA

[`tests/integration/test_platform_market_data_postgres.py`](../../tests/integration/test_platform_market_data_postgres.py), [`tests/unit/test_platform_data_aggregation.py`](../../tests/unit/test_platform_data_aggregation.py), [`tests/unit/test_platform_data_cli.py`](../../tests/unit/test_platform_data_cli.py), [`tests/unit/test_platform_data_collector.py`](../../tests/unit/test_platform_data_collector.py), [`tests/unit/test_platform_data_materialization.py`](../../tests/unit/test_platform_data_materialization.py), [`tests/unit/test_platform_data_normalization.py`](../../tests/unit/test_platform_data_normalization.py), [`tests/unit/test_platform_data_provider.py`](../../tests/unit/test_platform_data_provider.py), [`tests/unit/test_platform_watermarks.py`](../../tests/unit/test_platform_watermarks.py), [`tests/unit/test_platform_data_failure_contracts.py`](../../tests/unit/test_platform_data_failure_contracts.py), [`tests/unit/test_platform_gap_recovery_adversarial.py`](../../tests/unit/test_platform_gap_recovery_adversarial.py)

### Tests DEMO

[`tests/unit/test_platform_demo.py`](../../tests/unit/test_platform_demo.py)

### Tests DOC

[`tests/safety/test_public_documentation.py`](../../tests/safety/test_public_documentation.py), [`tests/safety/test_security_documentation.py`](../../tests/safety/test_security_documentation.py)

### Tests DX

[`tests/safety/test_development_environment.py`](../../tests/safety/test_development_environment.py), [`tests/unit/test_platform_package_resources.py`](../../tests/unit/test_platform_package_resources.py)

### Tests EXEC

[`tests/integration/test_platform_execution_postgres.py`](../../tests/integration/test_platform_execution_postgres.py), [`tests/unit/test_platform_execution_broker.py`](../../tests/unit/test_platform_execution_broker.py), [`tests/unit/test_platform_execution_persistence.py`](../../tests/unit/test_platform_execution_persistence.py), [`tests/unit/test_platform_execution_planner.py`](../../tests/unit/test_platform_execution_planner.py), [`tests/unit/test_platform_execution_reconciliation.py`](../../tests/unit/test_platform_execution_reconciliation.py), [`tests/unit/test_platform_paper_cycle.py`](../../tests/unit/test_platform_paper_cycle.py), [`tests/unit/test_platform_alpaca_fill_evidence.py`](../../tests/unit/test_platform_alpaca_fill_evidence.py), [`tests/unit/test_platform_persistence_adversarial.py`](../../tests/unit/test_platform_persistence_adversarial.py)

### Tests FUNC

[`tests/unit/test_platform_demo.py`](../../tests/unit/test_platform_demo.py), [`tests/unit/test_platform_paper_cycle.py`](../../tests/unit/test_platform_paper_cycle.py), [`tests/unit/test_platform_shadow.py`](../../tests/unit/test_platform_shadow.py)

### Tests GOV

[`tests/safety/test_main_ai_freeze.py`](../../tests/safety/test_main_ai_freeze.py), [`tests/safety/test_public_documentation.py`](../../tests/safety/test_public_documentation.py)

### Tests HARNESS

[`tests/safety/test_development_environment.py`](../../tests/safety/test_development_environment.py), [`tests/safety/test_repository_skills.py`](../../tests/safety/test_repository_skills.py)

### Tests JOB

[`tests/integration/test_platform_jobs_postgres.py`](../../tests/integration/test_platform_jobs_postgres.py), [`tests/unit/test_platform_job_artifacts.py`](../../tests/unit/test_platform_job_artifacts.py), [`tests/unit/test_platform_job_handlers.py`](../../tests/unit/test_platform_job_handlers.py), [`tests/unit/test_platform_jobs.py`](../../tests/unit/test_platform_jobs.py), [`tests/unit/test_platform_jobs_migration.py`](../../tests/unit/test_platform_jobs_migration.py)

### Tests OBS

[`tests/unit/test_platform_audit_cli.py`](../../tests/unit/test_platform_audit_cli.py), [`tests/unit/test_platform_audit_repository.py`](../../tests/unit/test_platform_audit_repository.py), [`tests/unit/test_platform_observability.py`](../../tests/unit/test_platform_observability.py)

### Tests OPS

[`tests/integration/test_platform_backup_restore.py`](../../tests/integration/test_platform_backup_restore.py), [`tests/safety/test_container_entrypoint.py`](../../tests/safety/test_container_entrypoint.py), [`tests/unit/test_platform_service_runtime.py`](../../tests/unit/test_platform_service_runtime.py), [`tests/unit/test_platform_worker_cycles.py`](../../tests/unit/test_platform_worker_cycles.py), [`tests/unit/test_worker_runtime_failure_boundaries.py`](../../tests/unit/test_worker_runtime_failure_boundaries.py), [`tests/unit/test_public_runtime_command_boundaries.py`](../../tests/unit/test_public_runtime_command_boundaries.py), [`tests/unit/test_platform_service_health_security.py`](../../tests/unit/test_platform_service_health_security.py), [`tests/unit/test_platform_durable_status.py`](../../tests/unit/test_platform_durable_status.py)

### Tests PERF

[`tests/unit/test_pipeline_benchmark.py`](../../tests/unit/test_pipeline_benchmark.py)

### Tests PZ

[`tests/integration/test_collection_operations_postgres.py`](../../tests/integration/test_collection_operations_postgres.py), [`tests/integration/test_collection_postgres.py`](../../tests/integration/test_collection_postgres.py), [`tests/test_collection_alpaca.py`](../../tests/test_collection_alpaca.py), [`tests/test_collection_contracts.py`](../../tests/test_collection_contracts.py), [`tests/test_collection_credentials.py`](../../tests/test_collection_credentials.py), [`tests/test_collection_derived.py`](../../tests/test_collection_derived.py), [`tests/test_collection_entrypoints.py`](../../tests/test_collection_entrypoints.py), [`tests/test_collection_operations.py`](../../tests/test_collection_operations.py), [`tests/test_collection_runtime.py`](../../tests/test_collection_runtime.py), [`tests/test_collection_service.py`](../../tests/test_collection_service.py), [`tests/test_collection_snapshots.py`](../../tests/test_collection_snapshots.py), [`tests/unit/test_platform_data_aggregation.py`](../../tests/unit/test_platform_data_aggregation.py), [`tests/unit/test_platform_data_cli.py`](../../tests/unit/test_platform_data_cli.py), [`tests/unit/test_platform_data_collector.py`](../../tests/unit/test_platform_data_collector.py), [`tests/unit/test_platform_data_materialization.py`](../../tests/unit/test_platform_data_materialization.py), [`tests/unit/test_platform_data_normalization.py`](../../tests/unit/test_platform_data_normalization.py), [`tests/unit/test_platform_data_provider.py`](../../tests/unit/test_platform_data_provider.py), [`tests/unit/test_platform_datasets.py`](../../tests/unit/test_platform_datasets.py), [`tests/unit/test_platform_watermarks.py`](../../tests/unit/test_platform_watermarks.py)

### Tests QUAL

[`tests/architecture/test_platform_configuration_boundary.py`](../../tests/architecture/test_platform_configuration_boundary.py), [`tests/architecture/test_platform_control_boundary.py`](../../tests/architecture/test_platform_control_boundary.py), [`tests/architecture/test_platform_data_boundary.py`](../../tests/architecture/test_platform_data_boundary.py), [`tests/architecture/test_platform_domain_boundary.py`](../../tests/architecture/test_platform_domain_boundary.py), [`tests/architecture/test_platform_execution_boundary.py`](../../tests/architecture/test_platform_execution_boundary.py), [`tests/architecture/test_platform_risk_boundary.py`](../../tests/architecture/test_platform_risk_boundary.py), [`tests/architecture/test_platform_signal_boundary.py`](../../tests/architecture/test_platform_signal_boundary.py), [`tests/architecture/test_platform_storage_boundary.py`](../../tests/architecture/test_platform_storage_boundary.py), [`tests/unit/test_platform_service_runtime.py`](../../tests/unit/test_platform_service_runtime.py)

### Tests RISK

[`tests/unit/test_platform_risk_latches.py`](../../tests/unit/test_platform_risk_latches.py), [`tests/unit/test_platform_risk_policy.py`](../../tests/unit/test_platform_risk_policy.py), [`tests/unit/test_platform_risk_repository.py`](../../tests/unit/test_platform_risk_repository.py), [`tests/unit/test_platform_risk_statistics.py`](../../tests/unit/test_platform_risk_statistics.py), [`tests/unit/test_platform_signed_risk.py`](../../tests/unit/test_platform_signed_risk.py), [`tests/unit/test_platform_risk_adversarial.py`](../../tests/unit/test_platform_risk_adversarial.py)

### Tests SCHED

[`tests/integration/test_platform_postgres_roles.py`](../../tests/integration/test_platform_postgres_roles.py), [`tests/unit/test_operational_scheduler.py`](../../tests/unit/test_operational_scheduler.py), [`tests/unit/test_platform_scheduling.py`](../../tests/unit/test_platform_scheduling.py)

### Tests SCHEDULED

[`tests/integration/test_collection_operations_postgres.py`](../../tests/integration/test_collection_operations_postgres.py), [`tests/safety/test_scheduled_collection_workflow.py`](../../tests/safety/test_scheduled_collection_workflow.py), [`tests/test_collection_entrypoints.py`](../../tests/test_collection_entrypoints.py)

### Tests SCOPE

[`tests/architecture/test_platform_configuration_boundary.py`](../../tests/architecture/test_platform_configuration_boundary.py), [`tests/architecture/test_platform_control_boundary.py`](../../tests/architecture/test_platform_control_boundary.py), [`tests/architecture/test_platform_data_boundary.py`](../../tests/architecture/test_platform_data_boundary.py), [`tests/architecture/test_platform_domain_boundary.py`](../../tests/architecture/test_platform_domain_boundary.py), [`tests/architecture/test_platform_execution_boundary.py`](../../tests/architecture/test_platform_execution_boundary.py), [`tests/architecture/test_platform_risk_boundary.py`](../../tests/architecture/test_platform_risk_boundary.py), [`tests/architecture/test_platform_signal_boundary.py`](../../tests/architecture/test_platform_signal_boundary.py), [`tests/architecture/test_platform_storage_boundary.py`](../../tests/architecture/test_platform_storage_boundary.py), [`tests/safety/test_main_ai_freeze.py`](../../tests/safety/test_main_ai_freeze.py)

### Tests SEC

[`tests/integration/test_platform_postgres_roles.py`](../../tests/integration/test_platform_postgres_roles.py), [`tests/safety/test_static_repository_safety.py`](../../tests/safety/test_static_repository_safety.py), [`tests/unit/test_platform_security.py`](../../tests/unit/test_platform_security.py), [`tests/unit/test_platform_service_health_security.py`](../../tests/unit/test_platform_service_health_security.py), [`tests/unit/test_platform_secret_publication_failures.py`](../../tests/unit/test_platform_secret_publication_failures.py)

### Tests SIGNAL

[`tests/integration/test_platform_postgres_roles.py`](../../tests/integration/test_platform_postgres_roles.py), [`tests/unit/test_platform_shadow.py`](../../tests/unit/test_platform_shadow.py), [`tests/unit/test_platform_signals.py`](../../tests/unit/test_platform_signals.py), [`tests/unit/test_platform_adversarial_contracts.py`](../../tests/unit/test_platform_adversarial_contracts.py)

### Tests STORE

[`tests/integration/test_platform_postgres_migrations.py`](../../tests/integration/test_platform_postgres_migrations.py), [`tests/unit/test_platform_datasets.py`](../../tests/unit/test_platform_datasets.py), [`tests/unit/test_platform_storage_engine.py`](../../tests/unit/test_platform_storage_engine.py), [`tests/unit/test_platform_storage_tables.py`](../../tests/unit/test_platform_storage_tables.py), [`tests/unit/test_platform_persistence_adversarial.py`](../../tests/unit/test_platform_persistence_adversarial.py)

### Tests TEST

[`tests/safety/test_verify_no_network.py`](../../tests/safety/test_verify_no_network.py), [`tests/unit/test_platform_demo.py`](../../tests/unit/test_platform_demo.py), [`tests/unit/test_platform_properties.py`](../../tests/unit/test_platform_properties.py), [`tests/unit/test_platform_alpaca_fill_evidence.py`](../../tests/unit/test_platform_alpaca_fill_evidence.py), [`tests/unit/test_worker_runtime_failure_boundaries.py`](../../tests/unit/test_worker_runtime_failure_boundaries.py)

### Tests UI

[`tests/unit/test_platform_dashboard.py`](../../tests/unit/test_platform_dashboard.py), [`tests/unit/test_platform_dashboard_rendering.py`](../../tests/unit/test_platform_dashboard_rendering.py)

### Tests USER

[`tests/safety/test_main_ai_freeze.py`](../../tests/safety/test_main_ai_freeze.py), [`tests/safety/test_market_data_deployment.py`](../../tests/safety/test_market_data_deployment.py)
