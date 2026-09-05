# Platform requirements

This ledger is the normalized specification for the platform-core program. It records current
evidence separately from target behavior so that design prose is never treated as implementation.
The starting point is commit `5690205b892053ea5fa12d7d590638a92003ff2c` on
`feature/market-data-platform`.

Source references use `Core` for the platform implementation directive and `Harness` for its
engineering appendix. Several closely related clauses are combined into one testable requirement;
normative constants are retained below rather than reproducing either source document.

## Status vocabulary

Only these values are used:

- `IMPLEMENTED_AND_VERIFIED`: the requirement's conforming behavior or required artifact exists
  and the stated verification passed.
- `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`: conforming behavior exists, but an external system or
  credential boundary has not been exercised.
- `PARTIALLY_IMPLEMENTED`: useful related behavior exists, but the complete contract does not.
- `NOT_IMPLEMENTED`: no conforming implementation exists.
- `BLOCKED`: progress requires a specifically unavailable authorization or capability.
- `INTENTIONALLY_DEFERRED`: the specification explicitly excludes the work from this program.

Status describes the repository state produced by the exact natural-change boundary under review;
before approval it is a proposed-state claim, and after commit it describes `HEAD`. Each boundary
updates affected rows alongside its implementation and pre-commit evidence. Approval and commit
metadata, which necessarily occur after the diff freezes, are recorded in the next active-plan
update. Baseline facts remain in assumptions or plan history. `—` means no additional assumption is
needed. A verified document proves only its documentation requirement; it cannot prove planned
runtime behavior.

## Governance, evidence, and repository integrity

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-GOV-001 | A phase is complete only with executable behavior, typed non-obvious contracts, risk-appropriate tests, focused and full checks, simplification and security reviews, accurate docs, a coherent diff, and explicit maintainer approval. | Core §0.1; Harness §42 | `docs/execution-plans/PLANS.md`; review process | Requirement trace plus review packet and command evidence | PARTIALLY_IMPLEMENTED | No implementation phase has completed this gate. |
| REQ-GOV-002 | Reports use only the six defined status values and avoid unsupported self-evaluative claims. | Core §0.2 | This ledger; active plan; final report | Documentation scan | IMPLEMENTED_AND_VERIFIED | Applies to program status reporting. |
| REQ-GOV-003 | The checkout is the source of implementation truth; inspect code, tests, configuration, state ownership, side effects, and existing conventions before editing. | Core §§0,8; Harness §§5,7,11C | Active plan and milestone workflow | Recorded reconnaissance and baseline evidence | PARTIALLY_IMPLEMENTED | The Phase 0 inspection matrix and command ledger are recorded in the active plan; inspection remains mandatory for later boundaries. |
| REQ-GOV-004 | Preserve unrelated work and legacy behavior; do not reset, clean, rewrite history, force-push, backdate, fabricate evidence, or discard user changes. | Core §§1,4.5,8; Harness §5 | Git workflow | Status/diff/history inspection | IMPLEMENTED_AND_VERIFIED | Compliance is procedural and rechecked before each change. |
| REQ-GOV-005 | Keep credentials, local databases/data, caches, logs, scratch material, transcripts, and unreviewed generated reports out of commits; preserve required provenance. | Core §§1.2–1.3,6.1; Harness §§5,41 | `.gitignore`, `.dockerignore`, review protocol | Secret scan, staged-file review, `git status --short` | IMPLEMENTED_AND_VERIFIED | Git and Docker exclusions cover the required secret and mutable-state classes; Git exceptions retain the placeholder environment file, reviewed editor settings, and deterministic data fixtures, and the review protocol still requires staged and untracked-file inspection. |
| REQ-GOV-006 | Each natural change has one coherent purpose, remains buildable, and is reviewed for a split near 800 meaningful handwritten lines. | Core §4.1 | `docs/execution-plans/PLANS.md` | Diff statistics and review packet | PARTIALLY_IMPLEMENTED | The planning standard defines the trigger; both documentation boundaries were evaluated against it. |
| REQ-GOV-007 | Before a commit, present the prescribed behavior, file, check, simplification, security, boundary, and message review packet; commit only after the exact approval response. | Core §§4.2–4.4 | Review workflow | Captured review packet and approval | PARTIALLY_IMPLEMENTED | The first two boundaries used exact review packets and approvals. The maintainer subsequently authorized uninterrupted continuation on this branch; review contents remain required even when a separate response is waived. |
| REQ-GOV-008 | Commit with configured identity and real time; do not add optional trailers or alter prior commits. Remote publication requires separate explicit authorization. | Core §§1.1–1.2,4.3–4.5 | Git workflow | `git show --format=fuller`, remote/status inspection | PARTIALLY_IMPLEMENTED | Commits `89a00dd794fc25a903be0e9461fe33736480b897`, `94ba212608b91e883a120791db6cb6b3f1e294c7`, and `d2b287effc6f046617e3d979381da10b0bbe556e` used configured identity, current time, and no trailer. The maintainer explicitly authorized branch publication and uninterrupted continuation; the remote head matched `d2b287e` after its fast-forward push. |
| REQ-GOV-009 | Implement milestones as tested vertical slices, repair root causes, update plan/docs, and review the full diff; the harness may not substitute for product behavior. | Harness §37 | Active plan; source/tests by milestone | Milestone evidence | PARTIALLY_IMPLEMENTED | Phase 0 inspection, requirements normalization, and durable planning are recorded; executable platform phases remain. |
| REQ-GOV-010 | Every material claim is backed by a test, command, scan, benchmark, inspected implementation, external response, or explicit unavailable-verification statement. | Harness §§11L,42–43; Core §43 | Requirements ledger, active plan, final report | Evidence audit | PARTIALLY_IMPLEMENTED | Phase 0 and both approved documentation commits have exact evidence in the active plan; evidence remains per-boundary. |
| REQ-GOV-011 | Every natural change receives a simplification pass and an 18-question security review covering inputs, privileges, compromise, state, side effects, replay, staleness, malformed data, concurrency, tests, and residual risk. | Core §§7.6,37 | Review packet; `docs/code-review.md` | Review record plus rerun checks | PARTIALLY_IMPLEMENTED | The first two boundaries received both reviews, and repository-local guidance now defines the required questions; ongoing compliance remains per-boundary. |
| REQ-GOV-012 | Final review uses independent security, concurrency/persistence/recovery, and maintainability/API/testing passes; all confirmed critical/high findings are fixed. | Core §36 Phase 10; Harness §40 | Final review records | Finding ledger and clean re-review | NOT_IMPLEMENTED | Separate reviewers are preferred when available. |

## Code quality and implementation discipline

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-QUAL-001 | Add an abstraction only for concrete coupling, duplication, substitution, or an external trust boundary; it needs a current consumer and must be clearer than a function, immutable value, existing primitive, or direct composition. | Core §7.1 | Whole repository; coding standards | Design/diff review with named consumer and removed alternative | PARTIALLY_IMPLEMENTED | Enforced for each new natural change; legacy structure is not rewritten solely for style. |
| REQ-QUAL-002 | Do not add pass-through managers/controllers, one-path factories, transaction-free repository wrappers, trivial-class fragmentation, speculative plugin frameworks, generic utility dumping grounds, needless inheritance/metaclasses/decorators/type parameters, unsupported switches, placeholders, dead shims, blanket exception swallowing, narration comments, test-count-only tests, internal-function mocks, large implementation snapshots, sleeps, wall-clock known answers, nondeterministic identifiers, or redundant dependencies. | Core §7.2 | Whole repository; static checks and review | Static scan, test review, simplification review | PARTIALLY_IMPLEMENTED | Existing code remains subject to touched-scope review. |
| REQ-QUAL-003 | Prefer explicit functions and control flow, frozen slotted dataclasses for meaningful values, enums for closed states, early returns, dependency injection only at external/time/persistence boundaries, domain names, typed public boundaries, context-managed resources, Decimal/UTC rules, pure calculations, and explicit scheduler/order transition tables. | Core §7.3 | New platform modules | Type, architecture, behavior, and diff review | PARTIALLY_IMPLEMENTED | Canonical serialization/hashing, frozen strict experiment contracts, and foundational deterministic-ID/UTC/Decimal domain values are pure and typed; later market-data, scheduler, persistence, risk, and order records remain. |
| REQ-QUAL-004 | Comments explain non-obvious design, safety, failure, external contracts, or timing; public non-obvious contracts get concise docstrings, while mechanical narration and generated signature restatement are absent. | Core §7.4 | Whole repository | Diff/comment review and linting | PARTIALLY_IMPLEMENTED | Applied to every touched file. |
| REQ-QUAL-005 | Catch exceptions only to recover, retry within bounds, translate external errors, add safe context, preserve audit state, or fail closed; a top-level catch records a redacted incident and failed bounded attempt and never converts failure to success. | Core §7.5 | Workers and external adapters | Failure-injection tests and exception-path review | PARTIALLY_IMPLEMENTED | Current collector and legacy paths cover a subset. |

## Product, scope, and compatibility

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-FUNC-001 | Deliver a self-hosted, single-operator, open-source platform supporting offline simulation, shadow operation, and paper execution only; real-money trading is unsupported. | Core §§0,2 | `src/adaptive_trader/platform/`; profiles | Mode/route/import tests and offline end-to-end test | PARTIALLY_IMPLEMENTED | Strict generic profiles now model all three modes and only a paper broker; runtime services and the offline end-to-end path remain. |
| REQ-FUNC-002 | Default installation requires no broker credentials, makes no external network calls, uses deterministic fixture data and a fake broker, and has submission disabled. | Core §§2,11.3,21,33,40 | `configs/platform/offline.yaml`; platform demo | Socket-denied demo and startup tests | PARTIALLY_IMPLEMENTED | Static validation and an empty injected runtime environment select fixture data, fake broker, disabled submission, and an application-root-confined SQLite path without reading secrets or constructing clients; the runnable offline stack/demo remains absent. |
| REQ-FUNC-003 | Shadow mode persists validated decisions and dry-run plans without constructing or calling a broker. | Core §§2,11.3 | `configs/platform/shadow.yaml`; strategy worker | Shadow integration and import-boundary tests | PARTIALLY_IMPLEMENTED | The strict shadow profile selects no broker and disables submission; decision persistence and the strategy worker remain absent. |
| REQ-FUNC-004 | Paper support is disabled in tracked configuration and requires all independent authorization gates; the default verifier denies approval. | Core §§2,18,20.6 | paper profile; authorization verifier; paper adapter | Every-gate negative tests and prohibited-endpoint scan | PARTIALLY_IMPLEMENTED | Both legacy and new tracked paper configuration disable submission; the new independent gate set and verifier remain absent. |
| REQ-FUNC-005 | Strategies are untrusted producers of declarative, versioned, signed proposals; the platform alone validates, risks, plans, persists, submits, and reconciles. | Core §§0,2,18–20 | `platform/signals`, `platform/risk`, `platform/execution` | Import-boundary, proposal-tamper, and risk-bypass tests | NOT_IMPLEMENTED | — |
| REQ-FUNC-006 | Consequential transitions are auditable and ambiguous state fails closed. | Core §§2,20,22,27 | repositories, audit chain, latches | Failure injection, tamper, ambiguity, and restart tests | PARTIALLY_IMPLEMENTED | Existing collector and legacy order paths have related controls, not the unified contract. |
| REQ-FUNC-007 | A new user can install, validate config, start the offline stack, run a deterministic demo, inspect status, and implement a registered signal provider without broker-recovery knowledge. | Core §§2,32–33 | package metadata, CLI, Compose, README, `examples/` | Clean-environment quickstart and extension validation | PARTIALLY_IMPLEMENTED | Both aliases validate static configuration and accept a generic provider identity; offline startup, demo, status, example, and quickstart remain absent. |
| REQ-FUNC-008 | Preserve `adaptive_trader`, `adaptive-portfolio-agent`, legacy commands, tests, backtest, replay, and long-only contracts while adding `autonomous-quant-agent` and `aqa` aliases. | Core §§3,9,18,19,20,36 | `pyproject.toml`; composed CLI; compatibility adapters | Legacy suite/backtest/replay and alias smoke tests | PARTIALLY_IMPLEMENTED | Both new aliases and legacy entry points coexist and the legacy regressions pass; the complete composed CLI and compatibility path remain. |
| REQ-SCOPE-001 | The platform core is generic; flagship symbols appear only in the versioned experiment configuration and experiment-specific tests. | Core §§2.1,11 | platform universe/config; experiment YAML | Static symbol scan and config contract tests | IMPLEMENTED_AND_VERIFIED | The generic configuration modules contain no flagship symbol literals; the existing legacy collection universe remains a separate compatibility boundary. |
| REQ-SCOPE-002 | Implement the non-model, non-backtester platform backbone; do not add model training/loading, features, labels, promotion statistics, or alter the legacy backtester except compatibility isolation. | Core §§0,18,42 | Whole repository | Diff inspection and legacy regression hashes | PARTIALLY_IMPLEMENTED | This is an ongoing scope constraint. |
| REQ-SCOPE-003 | Do not add C++, Rust, Go, Java, Node.js, TypeScript, React, Next.js, cloud resources, Kubernetes, or external queue/cache/orchestration systems in this program. | Core §§3.1–3.2 | Dependency and file manifests | Manifest/language scan | IMPLEMENTED_AND_VERIFIED | Existing Python/Streamlit/PostgreSQL design remains the target. |
| REQ-SCOPE-004 | Native optimization requires a later measured bottleneck and ADR; a public web frontend waits for stable APIs. | Core §3.1; §35 | `docs/performance.md`; ADRs | Benchmark and ADR presence before such dependencies | INTENTIONALLY_DEFERRED | — |

## Architecture and trust boundaries

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-ARCH-001 | New generic code uses the exact cohesive `src/adaptive_trader/platform/` module tree for canonical, config, domain, data, storage, scheduling, signals, risk, execution, jobs, API, and observability behavior; no empty placeholders. | Core §9 | `src/adaptive_trader/platform/` | Tree/content inspection and import tests | PARTIALLY_IMPLEMENTED | Nonempty canonical, hashing, universe, config, CLI, and security modules establish the package; the remaining required modules are absent. Existing `collection/` remains a legacy/reuse source until deliberately integrated. |
| REQ-ARCH-002 | Dependency direction is market data → canonical state → signal → risk → intent → execution policy → broker adapter; external I/O remains in adapters and broker objects do not enter the domain core. | Core §5; Harness §36A | Platform packages and architecture tests | AST/import-boundary tests | NOT_IMPLEMENTED | — |
| REQ-ARCH-003 | Collector may access only its scoped database credential and, for live data, Alpaca data credentials plus validated experiment/data/artifact paths; it has no paper credential, trading client, order/risk-control write, arbitrary URL, shell, or plugin authority. | Core §§5.1–5.2,15 | `platform/data/`; collector container/role | Import, credential-mount, grant, URL, and subprocess tests | PARTIALLY_IMPLEMENTED | Current collector image excludes the trading SDK/package and uses a narrow runtime group, but target secret and table contracts differ. |
| REQ-ARCH-004 | Scheduler may access only its scoped database credential and experiment/calendar/watermark/slot state; it has no Alpaca credential, broker, plugin-internal, order, or arbitrary-network authority. | Core §5.2 | `platform/scheduling/`; scheduler role/container | Import/grant/container tests | NOT_IMPLEMENTED | — |
| REQ-ARCH-005 | Strategy worker may access only its scoped database credential, immutable decision context, and registered providers; it writes signals/audit only and cannot access paper secrets, brokers, DDL, risk mutation, API-controlled arbitrary imports, or orders. | Core §§2.2,5.2 | `platform/signals/`; strategy role/container | Import/grant/config tests | NOT_IMPLEMENTED | Locally installed provider code is operator-trusted Python, not a sandbox. |
| REQ-ARCH-006 | Control API may access only its scoped database credential and operator token, with safe-read, bounded-job, and audited halt/resume authority; it cannot access Alpaca credentials, brokers, or any direct trade/arbitrary-code/path/URL/SQL operation. | Core §§5.2,24 | `platform/api/`; control role/container | Route inventory, import, auth, schema, and grant tests | NOT_IMPLEMENTED | — |
| REQ-ARCH-007 | Dashboard calls only private API read routes using its own API-token file and has no database, Alpaca credential, other filesystem-secret, broker, order control, or mutation client surface. | Core §§5.2,26 | `app.py` or platform dashboard entry; Compose | Import/config/browser tests | NOT_IMPLEMENTED | Existing dashboard reads SQLite directly. The exact secret inventory defines one operator token, so server-enforced read-only dashboard authorization must be resolved before verification. |
| REQ-ARCH-008 | Execution/reconciliation may access its scoped database credential and, only in the paper service, paper-broker credentials; it has no data-provider credential, model/plugin loading, arbitrary live endpoint, DDL, or real-money capability. | Core §5.2 | `platform/execution/`; execution role/container | Import, credential matrix, endpoint, and grant tests | NOT_IMPLEMENTED | — |
| REQ-ARCH-009 | Automated boundary tests reject forbidden collector/API/dashboard/strategy/execution imports, dangerous deserializers or code execution, generic SDK credential variables, and configurable live endpoints. | Core §5.3; Harness §23 | `tests/architecture/`, `tests/security/` | Isolated subprocess and AST/static tests | PARTIALLY_IMPLEMENTED | Collector image/legacy safety scans cover a subset; generic configuration/security modules have exact import allowlists, no ambient environment/process authority, and an exact generic-credential rejection inventory. Remaining service modules and isolated subprocess boundaries do not yet exist. |
| REQ-ARCH-010 | Use Python 3.11+, `uv`/`uv.lock`, immutable dataclasses internally, strict Pydantic v2 externally, SQLAlchemy 2, Alembic, PostgreSQL 16, PyArrow, FastAPI, Typer, Streamlit-through-API, pytest, Ruff, mypy, Prometheus metrics, and structured JSON logs. | Core §3 | `pyproject.toml`; platform modules; containers | Locked install, package, type, service, and dependency tests | PARTIALLY_IMPLEMENTED | Package metadata, Ruff, Mypy, Docker, and CI target Python 3.11, and the complete source tree passes Mypy at that language level. FastAPI, Prometheus, remaining platform contracts, the API-backed dashboard, and PG16 path remain. |

## Configuration, experiment, time, numeric, and secret contracts

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-CONFIG-001 | External/YAML/API models are frozen, strict, reject unknown fields, and produce immutable `PlatformConfig`, `RuntimeSettings`, universe, market-data, session, execution, and risk contracts. | Core §11 | `platform/config.py`, `platform/universe.py`, `platform/domain.py` | Positive/unknown-field/mutation/hash tests | PARTIALLY_IMPLEMENTED | Universe, market-data, session, risk, execution, profile, composed experiment, platform, service-scoped runtime settings, and foundational immutable domain values exist; later market-data/signal/risk/execution/API contracts remain. |
| REQ-CONFIG-002 | Symbols normalize to uppercase ASCII, match `^[A-Z][A-Z0-9.]{0,9}$`, are unique and role-disjoint, have a nonempty active set, and derive collection=active+benchmark+context and orders=active only. | Core §11.1 | `platform/universe.py` | Boundary/property tests including case collisions | IMPLEMENTED_AND_VERIFIED | Every stored role tuple and derived allowlist is immutable and alphabetically normalized. |
| REQ-CONFIG-003 | The shipped experiment exactly encodes eight active symbols, SOXX benchmark, QQQ/SPY context, 18 excluded symbols, Alpaca/IEX/raw 1Min→15Min/XNAS regular-hours data, exact session timing, risk groups, and policy constants. | Core §§2.1,11.2 | `configs/experiments/semiconductor_network_intraday_v1.yaml` | Known-answer parse and content-hash test | IMPLEMENTED_AND_VERIFIED | The strict definition has fixed canonical bytes and SHA-256 `c4e66f5a4886215306f3d25c98676ecf48479fac41db9b67848e445c1a46e431`. |
| REQ-CONFIG-004 | Experiment ID/version/hash are required; content is immutable; a supplied expected hash must match; WDC never satisfies SNDK. | Core §§2.1,11 | experiment loader | Known-answer hash, mutation, and WDC/SNDK tests | IMPLEMENTED_AND_VERIFIED | Every profile must pin the definition hash; the immutable `ExperimentSpec` adds signal-provider and execution-mode identity, and WDC is rejected. |
| REQ-CONFIG-005 | Explicit no-anchor offline, shadow, and paper profiles select the shipped experiment; every tracked profile sets submission false and doctor validates without constructing external clients. | Core §11.3 | `configs/platform/*.yaml`; platform CLI | YAML/doctor/client-construction tests | IMPLEMENTED_AND_VERIFIED | All three strict profiles are tracked with exact known hashes; offline declares SQLite fallback, and static doctor/config validation has no secret, network, broker, database, or plugin authority. |
| REQ-CONFIG-006 | Canonical JSON is UTF-8, lexicographically key-sorted and whitespace-free; UTC datetimes use six fractional digits plus `Z`; decimals are plain normalized strings; enums use values; tuples preserve order; sets, nonfinite numbers, secrets, and redacted wrappers are rejected. | Core §10.1 | `platform/canonical.py`, `platform/hashing.py` | Independently calculated byte/hash tests | IMPLEMENTED_AND_VERIFIED | Closed exact-type normalization rejects custom primitive/container subclasses, so secret and redacted wrappers cannot be coerced into hash inputs; fixed integer, depth, node, text, decimal, and output limits make acceptance independent of ordinary interpreter limits. |
| REQ-CONFIG-007 | Replayable domain IDs are deterministic and document prefix/hash inputs; random IDs are limited to non-replayable request correlation and injectable in known-answer tests. | Core §10.2 | platform domain modules | Repeated-run and known-answer tests | PARTIALLY_IMPLEMENTED | A frozen, bounded `DeterministicId` derives known-answer prefix/hash identities from canonical inputs; each later replayable record must still define its exact hash input, and correlation-ID injection remains. |
| REQ-CONFIG-008 | Public instants are timezone-aware UTC; sessions use injected `America/New_York` calendars; naive/local time is rejected; DST, early-close, lease, timeout, and freshness logic uses controllable clocks without test sleeps. | Core §10.3 | `platform/data/calendar.py`; scheduler/jobs | DST/early-close/fake-clock tests | PARTIALLY_IMPLEMENTED | Canonicalization rejects naive/custom-zone values and normalizes supported fixed offsets; the public domain primitive normalizes semantic zero-offset inputs to `datetime.UTC` and rejects naive/non-zero-offset values. Calendar, controllable clock, DST, early-close, lease, timeout, and freshness behavior remain. |
| REQ-CONFIG-009 | Money, price, quantity, weight, notional, cash, equity, and fees use `Decimal` at boundaries; statistical floats are isolated and finite; quantization names quantum/rounding; business invariants do not compare floats directly. | Core §10.4 | domain/risk/execution/storage models | Known-answer, nonfinite, rounding, and conservation tests | PARTIALLY_IMPLEMENTED | Foundational domain validation accepts only exact finite bounded `Decimal` values, and true-increment quantization requires a positive quantum and closed rounding mode under an isolated context; persistence, statistics, risk, execution, and conservation contracts remain. |
| REQ-SEC-001 | Platform services read secrets only through the seven `AQA_*_FILE` variables; ordinary settings use the six allowed nonsecret variables and safe defaults; generic SDK credential names are rejected. | Core §6.2 | `platform/security.py`, config, Compose | Environment inventory and startup tests | PARTIALLY_IMPLEMENTED | Runtime composition enforces the exact seven secret-file names, six nonsecret names/defaults, generic-name rejection, mode compatibility, and service-scoped references without loading values. Service command adoption and Compose mounts remain absent; current `APA_*` value-based collector variables are a predecessor. |
| REQ-SEC-002 | One secret-file loader rejects missing required files, directories, symlinks, world-readable mode, empty/NUL values; trims one trailing newline; wraps values with redacted `str`/`repr`; retains them only in memory and never persists them. | Core §6.3 | `platform/security.py` | File-mode/symlink/NUL/serialization/log tests | IMPLEMENTED_AND_VERIFIED | The POSIX loader descriptor-walks without following symlinks, pins file identity, accepts only current-owner modes 0400/0600, bounds content, removes exactly one LF, and returns an immutable, pickle-rejecting, Pydantic-redacted wrapper; service selection is tracked separately. |
| REQ-SEC-003 | Idempotent local bootstrap generates only database passwords and an operator token with cryptographic randomness, directory mode 0700 and file mode 0600; it never overwrites or prints values and never requests data-provider keys. | Core §6.4 | `scripts/bootstrap_local.py`; `platform.security`; CLI | Rerun, modes, output, collision, failure, and concurrency tests | IMPLEMENTED_AND_VERIFIED | The fixed nine-file inventory is created with atomic no-replace publication beneath a current-user-owned root that is not group/world-writable; valid files are preserved byte-for-byte, unsafe or incomplete state fails closed, cooperating local processes serialize, and CLI output contains relative paths only. |
| REQ-SEC-004 | Sentinel secrets are absent from stdout, stderr, logs, exceptions, responses, settings serialization, metrics, database/artifacts, and evidence across success and failure paths. | Core §6.5; §§27,31 | centralized redaction; security tests | Six-sentinel end-to-end redaction suite | PARTIALLY_IMPLEMENTED | One prescribed sentinel verifies wrapper/reference/runtime-settings/container/Pydantic/JSON-schema/generic JSON/log/canonicalization/error/traceback surfaces; the other sentinels and service, provider, database, API, broker, metrics, artifact, and evidence paths remain. |
| REQ-SEC-005 | A credential found in history blocks work and is reported by file/commit without reproducing the value; revocation/rotation is required. | Core §6.5 | Incident workflow | Committed-history secret scan | PARTIALLY_IMPLEMENTED | No exposure is currently known; dedicated history scan remains a Phase 8 gate. |

## Market data, persistence, and datasets

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-DATA-001 | `CanonicalBar` contains provider/feed/adjustment/symbol/timeframe, UTC interval and receipt/provider timestamps, Decimal OHLCV-related fields, schema/source/payload identity, quality flags, and correction metadata; identity is provider/feed/adjustment/symbol/timeframe/start. | Core §12 | `platform/domain.py`, `platform/data/normalization.py` | Construction and canonical hash tests | PARTIALLY_IMPLEMENTED | Current collection observation has related fields but not the exact contract. |
| REQ-DATA-002 | Bar validation enforces allowlist, role, supported series, UTC aligned start-inclusive/end-exclusive interval, positive coherent OHLC, nonnegative volume/trades/VWAP, finiteness, schema, and payload hash; execution-reference minute bars require VWAP. | Core §12 | normalization/provider boundary | Positive, negative, and property tests | PARTIALLY_IMPLEMENTED | Current collector validates most OHLC/time/volume constraints but not the target role/VWAP contract. |
| REQ-DATA-003 | Equivalent historical REST and real-time WebSocket events use one normalizer and yield byte-equivalent canonical payloads; feed is exactly IEX and adjustment raw with no fallback. | Core §§12.1,15 | `platform/data/normalization.py`, provider adapter | Historical/stream parity fixtures | PARTIALLY_IMPLEMENTED | Current direct adapter uses fixed IEX/raw endpoints and common observations; target canonical bytes remain. |
| REQ-DATA-004 | Ingestion is append-only: first effective payload inserts revision 1, identical delivery is a duplicate, changed payload appends revision N+1, and latest projection updates atomically without overwriting history. | Core §12.2 | storage repositories/tables | Known-answer, rollback, concurrent duplicate/correction tests | PARTIALLY_IMPLEMENTED | Existing market-data tables implement revisions/current projection under a different schema. |
| REQ-DATA-005 | Persist calendar-aware gaps before repair; repair is bounded, same-series, idempotent, and remains unresolved on partial/ambiguous results; post-decision corrections create incidents and never retroactive orders. | Core §12.3 | `platform/data/watermarks.py`, collector, repositories | Holiday/overnight/repair/correction tests | PARTIALLY_IMPLEMENTED | Existing schema reserves gaps, but target gap lifecycle is absent. |
| REQ-DATA-006 | Pure 1Min→15Min aggregation requires exactly 15 session-aligned minutes and calculates OHLC, summed volume, conditional trade count, volume-weighted VWAP, and ordered constituent lineage exactly; incomplete/cross-session buckets are ineligible. | Core §13 | `platform/data/aggregation.py` | Independent known-answer fixture | NOT_IMPLEMENTED | — |
| REQ-DATA-007 | Aggregation converges under out-of-order arrivals, shares historical/stream logic, revisions on effective corrections, and derives DST/early-close behavior from the calendar; benchmark/context aggregates never block active readiness. | Core §13 | aggregation/materialization service | Permutation, correction, calendar, restart tests | NOT_IMPLEMENTED | — |
| REQ-DATA-008 | Track highest contiguous quality-approved watermark per series and the minimum across active symbols; gaps/failed transactions/missing active symbols block progress, context does not, and restart reconstructs auditable state. | Core §16 | `platform/data/watermarks.py`; repositories | Gap, rollback, restart, and basket tests | PARTIALLY_IMPLEMENTED | Existing collector checkpoints track coverage but not the target quality/basket model. |
| REQ-DATA-009 | A generic provider boundary has deterministic fixture and Alpaca historical/stream adapters; provider hosts are constants and there is no scraping, trading client, arbitrary URL, or live credential validation in this program. | Core §15 | `platform/data/provider.py`, `collector.py` | Fake-client contract, import, endpoint, and socket-denial tests | PARTIALLY_IMPLEMENTED | Equivalent current collector adapters exist and are mock-tested; integration into the platform package remains required. |
| REQ-DATA-010 | Collector subscribes only to experiment collection symbols, validates before write, audits connection state, uses 1/2/4/8/16-second bounded retries and explicit rate limits, persists before watermark, handles ordering/gaps, and resumes durably without logging credentials. | Core §15 | platform collector service/container | Retry-clock, rate-limit, restart, ordering, redaction tests | PARTIALLY_IMPLEMENTED | Current collector covers most behavior with a 29-symbol universe and different credential/schema contracts. |
| REQ-STORE-001 | Operational persistence uses a redacted SQLAlchemy URL from `AQA_DATABASE_URL_FILE`; Compose uses PostgreSQL 16, while SQLite is restricted to isolated tests/offline legacy/demo; SQL is bound and transactions explicit. | Core §14.1 | `platform/storage/engine.py`; Compose | Dialect, redaction, SQL-injection, and PG16 integration tests | PARTIALLY_IMPLEMENTED | Existing collector uses PostgreSQL 15 validation and direct URL variables. |
| REQ-STORE-002 | Alembic upgrade from empty state creates the platform schema; operational startup never uses uncontrolled `create_all`; destructive downgrade of core market/order/audit data fails clearly. | Core §14.2 | `alembic.ini`, `migrations/` | Empty DB, prior-state upgrade, downgrade refusal, drift tests | PARTIALLY_IMPLEMENTED | Collector migration exists, but not the complete `aqa_` schema. |
| REQ-STORE-003 | Create all 25 required `aqa_` tables with explicit keys, relationships, checks, indexes, UTC timestamps, immutable append rows, versioned state, and specified uniqueness for bars, experiment, slots, signals, orders, jobs, outbox, and audit sequence. | Core §14.3 | `platform/storage/tables.py`; migrations | Metadata and PostgreSQL constraint tests | NOT_IMPLEMENTED | Table names are listed below. |
| REQ-STORE-004 | Provision seven least-privilege PostgreSQL roles with the exact collector/scheduler/strategy/execution/control/read-only/migration boundaries and prove representative unauthorized writes fail; passwords never enter SQL or Git. | Core §14.4 | bootstrap SQL/migrations; role tests | PG16 role/grant integration matrix | NOT_IMPLEMENTED | — |
| REQ-STORE-005 | Repository interfaces exist only for real transaction boundaries, return domain objects, and atomically cover bars/watermarks, slots, signals, risk/latches, intents, broker events/fills, reconciliation/incidents, jobs/outbox, and audit chain. | Core §14.5 | `platform/storage/repositories.py` | Transaction rollback/concurrency tests | NOT_IMPLEMENTED | — |
| REQ-STORE-006 | Immutable Parquet datasets use explicit schema/order/compression/row groups, staging plus atomic rename, validated derived artifact paths, physical/logical/manifest hashes, full lineage metadata, promotability, and overwrite protection. | Core §14.6 | `platform/data/datasets.py`; artifact store | Byte/logical identity, correction, overwrite, and path tests | NOT_IMPLEMENTED | Public fixtures are synthetic; provider data is never committed. |

## Scheduling, signals, risk, execution, and recovery

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-SCHED-001 | Preserve the legacy once-per-session scheduler and add deterministic XNAS slots at every 15-minute close from 09:45 through 14:30, with ready=C+60s, deadline=C+120s, and forced-flat 15:43/15:44/15:45 timing. | Core §17 | `platform/scheduling/` | Known-answer full-session/DST/early-close tests | NOT_IMPLEMENTED | — |
| REQ-SCHED-002 | Slots use deterministic IDs and exact metadata/states; transactional 30-second leases prevent duplicate work, inspect materialized results before reclaim, never catch up after deadline, and persist/audit every terminal or waiting outcome. | Core §17 | scheduler models/service/repository | Concurrency, expiry, restart, and missed-deadline tests | NOT_IMPLEMENTED | Exact states are listed below. |
| REQ-SCHED-003 | Unsupported early-close sessions create no entry slots and fail closed; forced risk reduction is handled separately. | Core §17; ADR requirement 12 | calendar/scheduler; ADR | Early-close integration test | NOT_IMPLEMENTED | — |
| REQ-SIGNAL-001 | `SignalEnvelope` is immutable, versioned, deterministic, hash-bound to slot/provider/experiment/data, time-bounded, contains every active symbol and availability/action/edge/target fields, and rejects unknown or inconsistent data. | Core §18 | `platform/signals/models.py` | Known-answer hash and boundary/tamper tests | NOT_IMPLEMENTED | — |
| REQ-SIGNAL-002 | Local provider discovery uses only packaging entry point `autonomous_quant_agent.signal_providers`; configuration selects a registered ID, and raw modules/classes/URLs/commands/source are rejected. | Core §2.2 | `platform/signals/providers.py`; package metadata | Discovery and malicious-config tests | NOT_IMPLEMENTED | Installed third-party providers are operator-trusted code and are not sandboxed. |
| REQ-SIGNAL-003 | Provide only `AlwaysFlatSignalProvider` and deterministic non-promotable `OfflineFixtureSignalProvider`; fixture use is offline/fake/submission-disabled and emits the exact first-slot NVDA/AMD proposal then flat. | Core §18 | provider implementations | Known-answer provider tests | NOT_IMPLEMENTED | — |
| REQ-SIGNAL-004 | Paper authorization defaults to `approved=false`, reason `model_approval_not_implemented`; fixture output can never qualify for paper. | Core §18 | authorization verifier | Negative authorization tests | NOT_IMPLEMENTED | — |
| REQ-RISK-001 | Preserve legacy long-only risk and add a separate signed policy receiving complete signal, position/order/account/price/security/reconciliation/equity-history/covariance/experiment/latch state; missing, stale, malformed, nonfinite, or mismatched input creates no exposure. | Core §§19,42 | `platform/risk/` | Input matrix and legacy regressions | NOT_IMPLEMENTED | Freshness: account 30s, security 300s, reconciliation 60s, price 120s. |
| REQ-RISK-002 | Statistics use 20 complete sessions × 25 within-session returns (500/symbol), `ddof=1`, annualization 6552, alphabetical order, finite inputs, eigenvalue floor `1e-8`, symmetric reconstruction, and hashed inputs/outputs. | Core §19.2 | `risk/statistics.py` | Independent numeric known-answer tests | NOT_IMPLEMENTED | NumPy floats remain internal; canonical Decimal conversion is required. |
| REQ-RISK-003 | Signed raw scores and volatility scaling follow the exact direction/25-bps saturation/max(sigma,0.20)/10%-vol formulas and fail flat when scores or forecast volatility cannot support a finite target. | Core §19.3 | `risk/policy.py` | Independent numeric and degenerate-case tests | NOT_IMPLEMENTED | — |
| REQ-RISK-004 | Apply shrink-only constraints in exact order: ±0.15 symbol, 1.00 gross, ±0.30 net, 0.60 configured groups, 0.40 correlation components above 0.80; recompute for at most eight passes to `1e-12`, else all-zero `risk_constraints_not_converged`; never redistribute clips. | Core §19.4 | `risk/policy.py` | Per-constraint/order/convergence/property tests | NOT_IMPLEMENTED | — |
| REQ-RISK-005 | Hard gates block new exposure for identity, expiry/deadline, basket/gap/correction/session, freshness, latch, ambiguity, symbol/asset/shortability/listing/capability/open-order, and numeric failures; validated risk reduction toward zero remains possible. | Core §19.5 | risk eligibility | Negative matrix and compromised-proposal tests | NOT_IMPLEMENTED | — |
| REQ-RISK-006 | Rebalance threshold is 0.25% of equity; below-band positions remain only absent a required reduction, while explicit zero bypasses the band. | Core §19.6 | risk policy | Boundary tests | NOT_IMPLEMENTED | — |
| REQ-RISK-007 | Session loss at -2% and deployment drawdown at -15% append durable ENGAGED events, force zero and survive restart; authenticated clear requires exact acknowledgement and appends CLEARED without modifying history. | Core §19.7 | `risk/latches.py`; API control | Threshold, restart, auth, acknowledgement, append-only tests | NOT_IMPLEMENTED | — |
| REQ-RISK-008 | Persist immutable risk decisions with proposal/final targets, before/after exposures, ordered controls/reasons, source times, latch/correlation identity, and policy/signal hashes. | Core §19.8 | risk models/repository | Persistence/hash/replay tests | NOT_IMPLEMENTED | — |
| REQ-EXEC-001 | Preserve legacy planner and implement signed quantity targets: long/fractional quantum `0.000001` ROUND_DOWN, shorts whole-share absolute quantum `1` ROUND_DOWN, zero exact. | Core §§20.1,42 | `platform/execution/planner.py` | Positive/negative/zero rounding tests | NOT_IMPLEMENTED | — |
| REQ-EXEC-002 | Support and label all ten long/short/flat increase/reduce/close/forced-flat effects. | Core §20.2 | execution models/planner | Transition state table tests | NOT_IMPLEMENTED | Effects are listed below. |
| REQ-EXEC-003 | A sign reversal persists and completes/reconciles the closing leg, proves zero within `0.000001`, refreshes/reruns risk before the deadline, then persists/submits the opening leg; any uncertainty blocks the second leg. | Core §20.3 | execution service/reconciliation | Every reversal failure-boundary and restart test | NOT_IMPLEMENTED | — |
| REQ-EXEC-004 | Persist intent before side effect; deterministic client IDs are ≤48 chars and stage-unique; min notional is $25 except flatten, max intent is 15% equity, max 16 intents, reductions sort first then symbol/sequence; short eligibility is rechecked and proceeds do not fund gross. | Core §20.4 | planner/service/repository | Known-ID, sorting, bound, restart, and side-effect-order tests | PARTIALLY_IMPLEMENTED | Legacy execution persists intent first; exact signed rules are absent. |
| REQ-EXEC-005 | Enforce the exact order state machine; post-submission exceptions become `SUBMISSION_UNKNOWN`, block exposure, never auto-retry, and resolve by deterministic client ID plus reconciliation; duplicate cumulative fills/updates are idempotent. | Core §20.5 | execution models/service | State-machine, timeout, duplicate, reconnect tests | PARTIALLY_IMPLEMENTED | Legacy paper path has analogous ambiguity handling. |
| REQ-EXEC-006 | Paper adapter is reachable only after all ten gates; constructs literal paper mode with no configurable trading host, contains no real-money path, and is never contacted during this program. | Core §20.6 | `execution/alpaca_paper.py` | Gate matrix, static endpoint scan, socket denial | PARTIALLY_IMPLEMENTED | Existing adapter is paper-only; new signal/account/profile gates remain. |
| REQ-EXEC-007 | Deterministic fake broker starts with $100,000 cash/equity/buying power, restricts short proceeds, conserves signed accounting, persists restart snapshots, and models the 15 specified fill/reject/timeout/duplicate/disconnect/staleness scenarios. | Core §21 | `execution/fake_broker.py` | Accounting/state/restart scenario matrix | PARTIALLY_IMPLEMENTED | Existing fake broker supports replay scenarios but not the full signed accounting contract. |
| REQ-EXEC-008 | Reconciliation reconstructs signed positions from fills, applies quantity `0.000001`, cash/equity `0.01` tolerances, validates traceable shorts, classifies all specified blocking/critical discrepancies, and engages a latch on either class. | Core §22 | `execution/reconciliation.py` | Every-discrepancy and tolerance tests | PARTIALLY_IMPLEMENTED | Legacy reconciliation is not the target signed model. |
| REQ-EXEC-009 | Forced flatten claims the slot, disables entries, cancels conflicting openings, reconciles, persists exact close intents, processes fills, and proves flat by 15:45; inability creates a blocking incident rather than success. | Core §22 | execution/scheduler services | Success, partial failure, restart, and deadline tests | NOT_IMPLEMENTED | — |

## Jobs, API, artifacts, dashboard, audit, and observability

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-JOB-001 | Durable jobs use the seven exact states, schema payload/hash/idempotency/correlation, max 3 attempts, 60-second lease, retry delays 5/10/20, safe errors, and atomic outbox creation; PG claims use `SKIP LOCKED` with deterministic SQLite tests. | Core §23 | `platform/jobs/`; storage | Concurrency, retry, expiry, rollback, replay tests | NOT_IMPLEMENTED | — |
| REQ-JOB-002 | Public jobs are limited to data-quality audit, gap repair, dataset freeze, and offline demo; payloads route work but do not authorize it and never carry command/path/URL/module/code. | Core §23 | job schemas/service; API | Strict-schema and route inventory tests | NOT_IMPLEMENTED | — |
| REQ-API-001 | Private FastAPI binds loopback by default, authenticates a ≥32-byte bearer token with constant-time comparison, has no cookies/CORS/docs by default, caps requests at 65,536 bytes and pages at 50/100, and rate-limits reads 120/min and mutations 10/min per token. | Core §24 | `platform/api/` | Startup/auth/limit/rate/header tests | NOT_IMPLEMENTED | In-process limiting is for one operator, not distributed denial-of-service defense. |
| REQ-API-002 | Expose only the two unauthenticated health routes, 16 authenticated read routes, and six authenticated bounded job/halt/resume routes specified below; no direct trade mutation exists. | Core §24.1 | API app/schemas | OpenAPI/route inventory and auth tests | NOT_IMPLEMENTED | Metrics is counted among authenticated read endpoints. |
| REQ-API-003 | API input is strict and versioned; errors are stable, safe, correlated, and stack-free; mutations use idempotency and audit; authenticated responses add no-store and nosniff plus restrictive CSP where applicable. | Core §24 | API middleware/schemas | Contract, malformed/unknown/duplicate/security-header tests | NOT_IMPLEMENTED | — |
| REQ-SEC-006 | Artifact IDs match `^[a-z0-9][a-z0-9._-]{0,127}$`; resolved paths stay under root and reject traversal, absolute/NUL paths, symlink components, escape, and conflicting overwrite; APIs accept IDs only. | Core §25.1 | artifact store/API schemas | Traversal/symlink/overwrite tests | NOT_IMPLEMENTED | — |
| REQ-SEC-007 | No public input accepts a URL; provider hosts are constants; no fetch/webhook/callback/import/metadata URL exists; loopback, metadata, IPv6-loopback, and file schemes are explicitly absent/rejected. | Core §25.2 | provider/API/job schemas | Route/schema and malicious-value tests | PARTIALLY_IMPLEMENTED | Current collector fixes provider hosts; platform request boundary remains. |
| REQ-SEC-008 | Runtime does not use unsafe object deserialization, untrusted `eval`/`exec`/`compile`, subprocess, or arbitrary imports; Parquet/JSON/YAML inputs enforce schema, size, path, and hash; no upload route exists. | Core §25.3 | platform runtime and architecture tests | Static scan and malformed artifact tests | NOT_IMPLEMENTED | — |
| REQ-UI-001 | Streamlit is read-only through the private API, uses only its API token, has no DB/broker/data credentials or order controls, and renders the specified safe health/data/scheduler/signal/exposure/risk/order/reconciliation/incident/demo summaries. | Core §26 | `app.py`; dashboard API client | Browser flow, import, credential, and route tests | NOT_IMPLEMENTED | Existing dashboard directly reads persistence; Phase 7 must resolve server-side read-only authorization within the exact secret contract. |
| REQ-OBS-001 | Central JSON logs contain only bounded safe identifiers and state/reason context; structurally redact secret-bearing keys and never log headers, env/settings dumps, URLs with credentials, raw payloads, or raw sensitive exceptions. | Core §27.1; Harness §33 | `platform/observability/logging.py` | Sentinel and structured-schema tests | PARTIALLY_IMPLEMENTED | Existing logging redacts some legacy secrets; target fields/tests remain. |
| REQ-OBS-002 | Every consequential experiment/data/dataset/slot/signal/risk/order/reconciliation/control/job/security transition emits append-only per-stream hash-chained audit state; `aqa audit verify` validates sequence/hash. | Core §27.2 | audit repository; CLI | Known-chain, tamper, concurrency, and command tests | NOT_IMPLEMENTED | — |
| REQ-OBS-003 | Prometheus metrics cover specified data, readiness, slot, risk, order, reconciliation, job, API, and redaction events with bounded labels; no symbol, free text, identifier, exception, or secret causes unbounded cardinality. | Core §27.3 | `platform/observability/metrics.py` | Metric inventory/cardinality/sentinel tests | NOT_IMPLEMENTED | — |
| REQ-OBS-004 | Long-running services have bounded work, explicit startup/shutdown/cancellation/failure propagation, useful liveness versus readiness, no detached tasks, and no timing-sleep tests. | Harness §§33,38–39 | all platform workers | Cancellation, timeout, shutdown, and health tests | PARTIALLY_IMPLEMENTED | Current collector has bounded shutdown; other target services are absent. |

## Containers, delivery, security engineering, and operations

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-OPS-001 | Locked multi-stage image uses verified base digests when resolvable, no unlocked resolution, numeric nonroot UID/GID, minimal runtime without compilers, read-only compatibility and explicit writable paths, no secrets/local state, health support, and OCI labels. | Core §28.1 | `Dockerfile`, `.dockerignore` | Image history/config/build and nonroot/read-only tests | PARTIALLY_IMPLEMENTED | Current images satisfy many controls but lack the full service image/OCI contract. |
| REQ-OPS-002 | Compose defines postgres, migrate, control API, data, scheduler, strategy, execution, dashboard, live-data, and paper-execution services; default is offline, live data uses `market-data` profile, paper uses `paper`, and DB debug is loopback-only. | Core §28.2 | `docker-compose.yml` | Compose config and service/profile startup tests | PARTIALLY_IMPLEMENTED | Current Compose has four differently scoped services and no default PostgreSQL platform stack. |
| REQ-OPS-003 | Publish only API/dashboard loopback ports by default; isolate service networks, drop all capabilities, prevent privilege escalation, use nonroot/read-only/tmpfs/health/bounded restart/resources, and mount only the credential matrix each service needs. | Core §28.2 | Compose | Static config plus runtime privilege/network/mount tests | PARTIALLY_IMPLEMENTED | Current services implement a subset; network separation is not an outbound firewall. |
| REQ-CI-001 | Pre-commit uses pinned Ruff, whitespace/EOF, YAML/TOML, private-key, secret, and large-file checks without running the slow integration suite. | Core §29.1 | `.pre-commit-config.yaml` | `pre-commit run --all-files` | NOT_IMPLEMENTED | — |
| REQ-CI-002 | CI has least-privilege, immutable-action, locked-install quality, PG16 integration, security, twice-run socket-denied demo, container/SBOM, and Python CodeQL gates; checkout does not retain credentials and no external provider secrets exist. | Core §29.2; Harness §29 | `.github/workflows/` | Workflow lint plus successful remote runs | PARTIALLY_IMPLEMENTED | Existing pinned offline-quality job covers format/lint/type/tests/PG15 migration/backtest/replay/Compose/images; the remaining jobs are absent. |
| REQ-CI-003 | Required gates never continue on error or hide pipeline failures; local `check` substantially matches CI; cache and failure artifacts are safe; supported versions matter and matrices remain small. | Harness §29 | Makefile/workflows | Workflow inspection and local/CI command comparison | PARTIALLY_IMPLEMENTED | — |
| REQ-CI-004 | Dependency maintenance covers Python-compatible lock updates, actions, and base images with grouped schedules; each production dependency has use, license, maintenance, security, transitive/deployment, version, and removal justification. | Core §§29.3; Harness §31 | Dependabot; `docs/dependency-policy.md`; lockfile | Config validation, lock check, vulnerability scan | NOT_IMPLEMENTED | One update system will be used. |
| REQ-CI-005 | Generate CycloneDX or SPDX dependency and image SBOMs for release CI; do not commit machine-specific SBOMs or publish/sign without explicit authorization. | Core §29.4 | CI scripts/workflow | SBOM schema/artifact inspection | NOT_IMPLEMENTED | — |
| REQ-SEC-009 | Maintain STRIDE threat records for specified assets, actors/failures, and entry points, each with preconditions, sequence, impact, prevention/detection/recovery, test, residual risk, owner/status. | Core §30 | `docs/threat_model.md` | Threat-to-test trace audit | IMPLEMENTED_AND_VERIFIED | Eighteen material threat records cover the required inventory, current/planned controls, executable and missing evidence, and residual risk; a safety test enforces the required inventories, fields, unique IDs, and status presence. |
| REQ-SEC-010 | Maintain vulnerability reporting, security architecture, incident, rotation, backup/restore, and failure-mode documentation; never claim an unexecuted control. | Core §30 | `SECURITY.md`; named docs | Documentation/evidence review | PARTIALLY_IMPLEMENTED | Vulnerability reporting, current/target security architecture, the STRIDE register, credential response, and legacy subsystem incident guidance exist; secret rotation, target failure modes, and executable backup/restore documentation remain. |
| REQ-SEC-011 | Implement all 44 security matrix behaviors covering API auth, redaction, injection/path/URL/size/schema, symbol/provider/signal/hash/freshness boundaries, import isolation, paper gates/endpoints, ambiguity/idempotency, corrupt state/config/secrets, PG grants, metrics, audit, jobs, network, scans, and flatten failure. | Core §31 | `tests/security/`, `tests/architecture/`, integration tests | Security matrix execution | PARTIALLY_IMPLEMENTED | Legacy and collector tests cover a subset; the platform matrix remains. |
| REQ-OPS-004 | Document and test logical PG backup/restore into a fresh DB, including migration/version, row/hash/audit/slot/intent/fill/reconciliation verification and absence of fixture secrets. | Core §34 | `docs/backup_restore.md`; integration script/test | Automated restore smoke test | NOT_IMPLEMENTED | Requires a local/CI PG16 instance. |

## Developer experience, engineering memory, testing, and performance

| ID | Requirement | Source | Implementation location | Verification | Status | Assumption |
| --- | --- | --- | --- | --- | --- | --- |
| REQ-DX-001 | Add exact safe `aqa` commands for doctor, config, secret bootstrap, migration, data status/fixture/aggregate/freeze, scheduler, shadow, demo, audit, API, and dashboard; preserve legacy commands and provide JSON where useful. | Core §32.1 | `platform/cli.py`; `pyproject.toml` | CLI help/exit/output/secret/network tests | PARTIALLY_IMPLEMENTED | `doctor`, `config validate`, and `secrets bootstrap-local` run through both new aliases with deterministic or path-only output and safe failures; the remaining commands are absent. |
| REQ-DX-002 | Build and clean-install sdist/wheel, use semantic versioning, document supported Python/PG/OS/schema/contract/migration/deprecation/security policy, and do not publish without authorization. | Core §§32.2,32.6 | package metadata; compatibility docs; CI | Build and fresh-environment smoke tests | NOT_IMPLEMENTED | — |
| REQ-DX-003 | Provide license, contributing/security/conduct/change files, issue forms, and PR template; preserve an existing license or use the prescribed MIT copyright only when absent. | Core §32.3 | repository root; `.github/` | File/content/license review | PARTIALLY_IMPLEMENTED | Contribution and private security-reporting policies exist; license, conduct, changelog, issue forms, and pull-request template remain. |
| REQ-DX-004 | README accurately separates verified, unvalidated, deferred, and unsupported behavior and includes product/safety architecture, five-minute offline use, config/provider example, demo, testing, data licensing, disclaimer, no-profit claim, no-real-money scope, and separated roadmap. | Core §32.4 | `README.md` | Quickstart execution and documentation claim audit | PARTIALLY_IMPLEMENTED | Current README describes the research design and collector but not the target developer path. |
| REQ-DX-005 | A small tested educational signal-provider example consumes only supplied context, has no broker/credential access, is disabled in paper config, and makes no performance claim. | Core §32.5 | `examples/`; tests/docs | Import boundary and runnable validation | NOT_IMPLEMENTED | — |
| REQ-DOC-001 | Maintain a concise repository map/operating agreement, current architecture, requirements, active plan, principles, standards, testing, security, performance, observability, dependency, and review documents without generic filler. | Harness §§3,8–14,21,30–33 | `AGENTS.md`, `ARCHITECTURE.md`, `docs/` | Link/content/claim review | IMPLEMENTED_AND_VERIFIED | The 159-line root operating agreement and indexed engineering-memory set cover every named authority with current/target status separated and reviewed local links. |
| REQ-DOC-002 | Architecture documentation covers 18 specified system concerns and, per major module, responsibility, allowed/forbidden dependencies, state, interface, and failure behavior; encode important boundaries mechanically. | Harness §12 | `ARCHITECTURE.md`; architecture tests | Architecture/implementation consistency review | PARTIALLY_IMPLEMENTED | The root cross-system architecture covers the 18 documentation concerns and current major-module responsibilities, state, interfaces, dependencies, and failure behavior; target dependency and authority boundaries still require implementation-time architecture tests. |
| REQ-DOC-003 | Coding/tooling/testing documents are stack-specific, name exact versions/commands/limits, avoid duplicate tools and blanket suppressions, and define behavior-oriented deterministic test categories and ownership. | Harness §§13–16,20–23 | named docs; `pyproject.toml`; Makefile | Documentation-to-config/command audit | IMPLEMENTED_AND_VERIFIED | The documents distinguish the current incomplete Makefile surface from canonical locked commands and mark unavailable test/tool categories explicitly. |
| REQ-DOC-004 | Maintain the prescribed operations, developer, strategy, demo, implementation-status, data dictionary, failure, security, and evidence docs plus 12 decision records with context, alternatives, consequences, and security impact. | Core §41 | `docs/`; `docs/adr/` | Required-file and current-state audit | PARTIALLY_IMPLEMENTED | ADR 0001 accepts the generic-platform/shipped-experiment boundary; the remaining 11 records and prescribed documents follow their implementation boundaries. |
| REQ-DOC-005 | Resume evidence contains only measured test/coverage/failure/hash/transition/migration/service/benchmark facts and never planned or profitability claims. | Core §41 | `docs/resume_evidence.md` | Evidence provenance audit | NOT_IMPLEMENTED | — |
| REQ-HARNESS-001 | Provide one real canonical command surface for bootstrap, build, format/check, lint, typecheck, unit/integration/regression/full tests, security, and full check, with applicable coverage/package/run/replay/demo/benchmark commands and no no-op targets. | Harness §20 | `Makefile` or equivalent | Execute each documented target | PARTIALLY_IMPLEMENTED | The existing Makefile has real legacy targets but uses unlocked pip for installation and lacks the complete platform harness. |
| REQ-HARNESS-002 | Add the nine focused core repository-local workflow skills for exploration, planning, implementation, debugging, behavioral testing, review, security, performance, and dependencies, plus API, migration, frontend, release, incident, trading, and backtest skills only when relevant; reference authoritative docs. | Harness §§27–28,36L | `.agents/skills/*/SKILL.md` | Frontmatter/content and representative workflow validation | IMPLEMENTED_AND_VERIFIED | Fourteen active skills are enforced as an exact inventory; the validator requires normalized tracked repository authorities and critical workflow clauses while rejecting symlinked, escaped, untracked, private, external, and unsupported link forms. |
| REQ-HARNESS-003 | Add minimal editor recommendations/settings and `.editorconfig` when useful; correctness remains in CLI/CI and personal preferences are not overridden. | Harness §19 | `.vscode/`; `.editorconfig` | Settings/schema and CLI parity review | IMPLEMENTED_AND_VERIFIED | Six non-overlapping stack-specific extension recommendations, repository-scoped Python/Ruff/Pytest settings, and cross-editor whitespace rules are present; every essential check remains in the command line and CI. |
| REQ-HARNESS-004 | Add only safe, schema-verified project-local hooks that terminate, time out, avoid recursion/exfiltration/secrets, and are independently tested; otherwise document exact activation and do not claim active status. | Harness §§25–26 | project-local hook config; validation scripts | Hook schema/test/observed activation | NOT_IMPLEMENTED | Hook support must be inspected before files are added. |
| REQ-TEST-001 | Tests are deterministic and behavior-oriented; every new behavior has risk-appropriate positive, negative, boundary, error, restart, and security cases, while bug fixes include regression reproduction where feasible. | Core §§0.1,38; Harness §§11F,21 | `tests/` by boundary | Test review and focused/full suite | PARTIALLY_IMPLEMENTED | Existing suite follows many of these rules; platform tests remain. |
| REQ-TEST-002 | Unit tests use no live network, external credential, wall-clock sleep, or order dependence; integration uses real ephemeral boundaries where practical; randomized failures expose seeds; time uses controllable clocks; expected values do not call production logic. | Core §§38,40; Harness §§21–22 | test fixtures and suites | Socket denial, repeat/shuffle, fixture review | PARTIALLY_IMPLEMENTED | The test bootstrap removes ambient Alpaca authority before collection and per test, and the current fixture blocks two common TCP paths; a universal socket-denial harness and full order-independence evidence remain. |
| REQ-TEST-003 | Cover every listed experiment, bar, correction, transaction, aggregation, calendar, watermark, slot, lease, signal, risk, transition, reversal, fill, ambiguity, restart, reconciliation, latch, flatten, API, path, secret, role, migration, backup, demo, and legacy invariant. | Core §§31,33,38 | platform unit/component/integration/e2e tests | Requirement-to-test matrix | PARTIALLY_IMPLEMENTED | The current 606-test offline suite covers canonicalization, experiment/universe/profile composition, static CLI, collector, and legacy subsets; 13 PostgreSQL tests exist, but fresh PostgreSQL 16 and most later platform boundaries remain. |
| REQ-TEST-004 | Measure branch coverage; repository coverage never falls below the 74% starting baseline, new platform targets ≥85% branch coverage except justified adapter paths, and every safety state machine/gate has direct tests regardless of percentage. | Core §38.3 | coverage config/CI | Branch coverage reports and ratchet | PARTIALLY_IMPLEMENTED | Focused platform branch coverage is 90.88%; the starting repository baseline is 74%, and no CI ratchet is configured yet. |
| REQ-TEST-005 | Before phase/final completion run locked all-extras sync, Ruff format/lint, mypy, full tests and branch coverage, legacy backtest/replay, Compose validation, diff check, and all implemented PG/security/image/SBOM/demo/restore/health gates. | Core §39; Harness §41 | canonical `check`; CI | Exact command result ledger | PARTIALLY_IMPLEMENTED | Phase 0 used current extras rather than the not-yet-supported all-extras target. |
| REQ-TEST-006 | Ordinary tests, CI, and demo deny outbound sockets while permitting explicitly required loopback/Unix integration; no real data/paper connection, order, or real secret participates. | Core §40 | socket guard script/fixture; workflows | Network-denial test plus final safety confirmation | PARTIALLY_IMPLEMENTED | Test bootstrap removes ambient Alpaca variables and forces submission off; two common TCP paths are denied and PostgreSQL tests allow loopback only. Process-wide denial and the target demo remain. |
| REQ-DEMO-001 | `aqa demo` runs the exact synthetic 1Min→canonical→revision→aggregate→gap/watermark→slot→signal→risk→intent→fake-fill→reconcile→restart/replay→flatten→audit/API/evidence vertical slice with no credential/network. | Core §33 | `platform/demo.py`; CLI | Fresh-state offline end-to-end test | NOT_IMPLEMENTED | Evidence label is `OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE`. |
| REQ-DEMO-002 | Two fresh demo runs produce identical logical event, aggregate, watermark, slot, signal, risk, plan, order, fill, reconciliation, account, audit, and manifest hashes; only temp paths/PIDs/wall-clock run metadata may differ. | Core §33 | demo evidence manifest; CI | Twice-run hash comparison | NOT_IMPLEMENTED | — |
| REQ-DEMO-003 | Inject all nine specified persistence/decision/side-effect/flatten failures; each recovers deterministically without duplication or leaves a durable fail-closed incident. | Core §33 | demo failure injection and tests | Scenario matrix | NOT_IMPLEMENTED | — |
| REQ-PERF-001 | Benchmark deterministic normalization, ingestion, aggregation, slot claim, risk, and fake execution/reconciliation with warmups/repeats, machine-readable results, environment metadata, and no unstable CI thresholds or unsupported claims. | Core §35; Harness §32 | `scripts/benchmark_pipeline.py`; `docs/performance.md` | Repeatable benchmark smoke and result schema | NOT_IMPLEMENTED | Benchmarking informs later work; it is not an acceptance latency budget. |

## Normative experiment values

`REQ-CONFIG-003` requires these exact, explicit values with alphabetically normalized role tuples:

- symbol-role enum values: `ACTIVE_TRADABLE`, `BENCHMARK_ONLY`, `CONTEXT_ONLY`, `EXCLUDED`;
- schema version `1`; experiment `semiconductor_network_intraday_v1`, version `1`;
- active: `AAOI, AMD, AXTI, CSCO, HLIT, INSG, NVDA, SNDK`;
- benchmark: `SOXX`; context: `QQQ, SPY`;
- excluded: `AAPL, AMZN, BOX, GOOGL, LCID, META, NET, OKTA, PAYC, PUBM, RBLX,
  RIVN, ROKU, SOUN, TSLA, UBER, WDAY, ZG`;
- provider/feed/adjustment: `alpaca/iex/raw`; source/decision timeframe: `1Min/15Min`;
  calendar `XNAS`; regular hours only;
- session: open `09:30:00`, close `16:00:00`, first/last strategy closes `09:45:00`
  and `14:30:00`, readiness/deadline delays `60/120` seconds, forced-flat target/submit/flat
  times `15:43:00/15:44:00/15:45:00`;
- risk groups: `connectivity={AAOI,CSCO,HLIT,INSG}` and
  `compute_storage_materials={AMD,AXTI,NVDA,SNDK}`, each capped at `0.60`;
- policy `paper_v1` version `1`: absolute symbol `0.15`, gross `1.00`, net
  `[-0.30,0.30]`, cluster gross `0.40`, correlation threshold `0.80`, rebalance equity
  fraction `0.0025`, session loss `-0.02`, deployment drawdown `-0.15`, target annual
  volatility `0.10`, sigma floor `0.20`, edge saturation `25` bps, covariance eigenvalue
  floor `0.00000001`.

## Normative profile and secret inventories

`REQ-CONFIG-005` requires complete YAML without anchors. Every profile selects
`configs/experiments/semiconductor_network_intraday_v1.yaml`, sets `submission_enabled: false`,
and sets `paper_only: true`.

| Profile | Mode | Market data | Signal provider | Broker | Database behavior |
| --- | --- | --- | --- | --- | --- |
| `offline.yaml` | `offline` | deterministic fixture | deterministic non-promotable fixture | fake | secret-file URL when present; otherwise temporary/default offline SQLite |
| `shadow.yaml` | `shadow` | Alpaca data adapter | always flat | none | operational configured database |
| `paper.yaml` | `paper` | Alpaca data adapter | always flat | Alpaca paper adapter | operational configured database |

`REQ-SEC-001` permits only these secret-file variables:

- `AQA_DATABASE_URL_FILE`
- `AQA_OPERATOR_TOKEN_FILE`
- `AQA_ALPACA_DATA_API_KEY_FILE`
- `AQA_ALPACA_DATA_SECRET_KEY_FILE`
- `AQA_ALPACA_PAPER_API_KEY_FILE`
- `AQA_ALPACA_PAPER_SECRET_KEY_FILE`
- `AQA_PAPER_ACCOUNT_ID_HASH_FILE`

Permitted non-secret settings and defaults are:

```text
AQA_CONFIG=configs/platform/offline.yaml
AQA_ARTIFACT_ROOT=outputs/artifacts
AQA_API_BASE_URL=http://127.0.0.1:8000
AQA_LOG_FORMAT=json
AQA_ENABLE_PAPER_ORDERS=NO
AQA_API_DOCS_ENABLED=NO
```

The new platform must reject `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `APCA_API_BASE_URL`,
`ALPACA_API_KEY`, and `ALPACA_SECRET_KEY`. Secret-redaction tests use these exact non-secret
sentinels:

- `TEST_AQA_DATA_KEY_DO_NOT_LEAK`
- `TEST_AQA_DATA_SECRET_DO_NOT_LEAK`
- `TEST_AQA_PAPER_KEY_DO_NOT_LEAK`
- `TEST_AQA_PAPER_SECRET_DO_NOT_LEAK`
- `TEST_AQA_OPERATOR_TOKEN_DO_NOT_LEAK`
- `TEST_AQA_DATABASE_PASSWORD_DO_NOT_LEAK`

The paper-enable acknowledgement is `I_ACKNOWLEDGE_AQA_PAPER_ONLY`; the latch-clear
acknowledgement is `I_HAVE_REVIEWED_AQA_PAPER_STATE`; the default authorization denial reason is
`model_approval_not_implemented`.

## Normative risk and execution calculations

For direction `d` (`+1` long, `-1` short, `0` flat), expected edge in basis points, and annualized
volatility `sigma`, `REQ-RISK-003` requires:

```text
edge_fraction = min(abs(expected_edge_bps) / 25, 1)
denominator = max(sigma, 0.20)
raw_score = d * edge_fraction / denominator
forecast_vol = sqrt(s^T * covariance * s)
initial_multiplier = 0.10 / forecast_vol
pre_constraint_target = s * initial_multiplier
```

If every raw score is zero, return all-zero targets without calculating forecast volatility.
Otherwise, nonpositive or nonfinite forecast volatility fails flat. Apply shrink-only constraints
in this exact order: per-symbol clip `[-0.15,+0.15]`; gross scaling to `1.00`; positive net scaling by
`(0.30 + short_abs) / positive_total`; negative net scaling by
`(0.30 + positive_total) / short_abs`; risk-group scaling to `0.60` in lexicographic group order;
then absolute prior-correlation connected-component scaling to `0.40` for edges above `0.80`. Recompute
for at most eight passes until change is at most `1e-12` and every constraint passes; otherwise
return zero with `risk_constraints_not_converged`. Never redistribute clipped exposure.

Statistics use 20 prior complete full sessions, 26 fifteen-minute bars and 25 within-session log
returns per session, exactly 500 returns per active symbol, `ddof=1`, annualization `6552`, and an
eigenvalue floor of `1e-8`, in alphabetical symbol order. Session loss and deployment drawdown are:

```text
(account_equity - session_start_equity) / session_start_equity <= -0.02
(account_equity - deployment_high_water_equity) / deployment_high_water_equity <= -0.15
```

Rebalance calculations are `current_weight=q*p/E`,
`change_dollars=abs((w-current_weight)*E)`, and `threshold=0.0025*E`. An explicit zero bypasses
the band. Quantity planning uses `raw_target_quantity=w*E/p`; nonnegative targets quantize to
`0.000001` with `ROUND_DOWN`, negative targets to whole-share absolute quantum `1` with
`ROUND_DOWN`, and exact zero remains zero.

Deterministic client-order IDs start with `aqa`, contain fragments of experiment hash and decision
ID plus symbol, phase, sequence, target version, and final-hash fragment, and are at most 48
characters. The same logical stage retains its ID after restart and distinct stages never share an
ID. Minimum notional is `25.00` except forced flatten; one intent is at most 15% of current equity;
one plan has at most 16 intents; reductions precede increases, then sort by symbol and sequence.

`REQ-EXEC-006` requires every paper gate below before adapter invocation:

1. runtime mode is `paper`;
2. configuration `submission_enabled` is true;
3. `AQA_ENABLE_PAPER_ORDERS` equals `I_ACKNOWLEDGE_AQA_PAPER_ONLY`;
4. the adapter reports paper-only;
5. required paper secret files exist and pass permission checks;
6. approved account-ID hash matches the configured hash;
7. signal source is an approved non-fixture artifact;
8. the approval verifier returns approved;
9. session, data, account, security metadata, and reconciliation are fresh;
10. no blocking latch exists.

Tracked configuration denies gate 2 and the default verifier denies gate 8. The adapter uses
literal `paper=True`; no `paper=False`, real-money endpoint, or configurable trading host exists.

The fake broker starts with account ID `fake-paper-account-v1`, cash/equity/buying power
`100000.00`, restricted short proceeds `0.00`, and no positions. Its accounting is
`equity=cash+sum(signed_quantity*mark_price)` and
`buying_power=max(0,cash-restricted_short_proceeds)`. Opening a short increases cash and restricted
proceeds by notional; covering decreases cash by cover notional and decreases restricted proceeds
proportionally to covered original inventory, never below zero. Event IDs derive deterministically
from client order ID and event sequence. The broker supports exactly these 15
deterministic scenarios: full fill, partial fill, rejection, cancellation, expiration, timeout
before acceptance, timeout after acceptance, delayed update, duplicate client ID, duplicate
execution update, disconnect, stale account state, stale security metadata, shortability change,
and process restart from JSON snapshot.

## Normative package and decision-record inventory

`REQ-ARCH-001` requires this exact nonempty platform tree:

```text
src/adaptive_trader/platform/
├── __init__.py
├── canonical.py
├── config.py
├── constants.py
├── domain.py
├── errors.py
├── hashing.py
├── security.py
├── universe.py
├── cli.py
├── demo.py
├── data/{__init__,aggregation,calendar,collector,credentials,datasets,normalization,provider,watermarks}.py
├── storage/{__init__,engine,repositories,tables}.py
├── scheduling/{__init__,models,service}.py
├── signals/{__init__,models,providers}.py
├── risk/{__init__,latches,models,policy,statistics}.py
├── execution/{__init__,alpaca_paper,fake_broker,models,planner,reconciliation,service}.py
├── jobs/{__init__,models,service}.py
├── api/{__init__,app,auth,schemas}.py
└── observability/{__init__,logging,metrics}.py
```

The required supporting paths are the three `configs/platform/*.yaml` profiles, the shipped
experiment YAML, existing additive Alembic root and migration tree, `scripts/bootstrap_local.py`,
`scripts/benchmark_pipeline.py`, `scripts/verify_no_network.py`, `tests/unit/`, `tests/component/`,
`tests/integration/`, `tests/e2e/`, `tests/architecture/`, `tests/security/`, and `examples/`.

`REQ-DOC-004` requires these 12 decision records under `docs/adr/`:

1. generic platform core versus shipped fixed experiment;
2. separate signed platform contracts versus mutation of legacy backtest contracts;
3. PostgreSQL operational state plus immutable Parquet datasets;
4. strategy proposals have no broker authority;
5. intent persistence before external side effects;
6. close/reconcile before sign reversal;
7. PostgreSQL jobs/outbox instead of a message broker;
8. Streamlit retained until API stabilization;
9. Python retained until profiling justifies C++;
10. self-hosted paper-only scope and no public credential custody;
11. credential/process trust boundaries;
12. unsupported early-close behavior.

## Normative state and interface inventories

These inventories are part of the referenced requirements and are centralized here for mechanical
tests.

- `CanonicalBar` fields (`REQ-DATA-001`): provider, feed, adjustment, symbol, timeframe,
  start/end UTC, receipt UTC, optional provider-event UTC, OHLC, volume, optional trade count,
  optional VWAP, schema version, source event ID, payload hash, quality flags, and correction
  metadata. Identity is `(provider,feed,adjustment,symbol,timeframe,interval_start_utc)` and the
  interval is start-inclusive/end-exclusive.
- `SignalEnvelope` fields (`REQ-SIGNAL-001`): contract version, deterministic signal ID, slot and
  correlation IDs, provider ID/version/source mode, experiment ID/version/hash, data-contract hash,
  source-bar end, creation/expiration times, exact active-symbol set, availability mask,
  per-symbol `LONG`/`SHORT`/`FLAT` action, optional expected edge and signed-target inputs,
  nullable artifact ID/hash for non-model providers, and canonical content hash.
- Ingestion outcomes (`REQ-DATA-004`): first effective payload is `INSERTED` at revision 1;
  byte-equivalent redelivery is `DUPLICATE` without projection change; changed effective content is
  `CORRECTED` at revision N+1 with history retained and latest projection updated atomically.
- Required tables (`REQ-STORE-003`): `aqa_experiments`, `aqa_experiment_symbols`,
  `aqa_security_metadata_events`, `aqa_bar_identities`, `aqa_bar_events`, `aqa_bar_latest`,
  `aqa_data_gaps`, `aqa_symbol_watermarks`, `aqa_basket_watermarks`, `aqa_dataset_manifests`,
  `aqa_decision_slots`, `aqa_signal_envelopes`, `aqa_risk_latch_events`, `aqa_risk_decisions`,
  `aqa_execution_plans`, `aqa_order_intents`, `aqa_broker_orders`, `aqa_order_events`,
  `aqa_fills`, `aqa_reconciliations`, `aqa_incidents`, `aqa_jobs`, `aqa_job_attempts`,
  `aqa_outbox_events`, `aqa_audit_events`.
- Required uniqueness (`REQ-STORE-003`): bar identity on
  `(provider,feed,adjustment,symbol,timeframe,start)`; effective bar event on identity/revision;
  latest projection one row per identity; experiment ID/version; decision slot on experiment hash,
  source-bar end, and decision type; signal ID and content hash; client order ID; broker execution
  ID; job idempotency key per job type; outbox event ID; audit stream/sequence.
- Slot states (`REQ-SCHED-002`): `PENDING`, `WAITING_FOR_DATA`, `READY`, `CLAIMED`,
  `COMPLETED`, `SKIPPED`, `EXPIRED`, `FAILED`, `FLATTEN_REQUIRED`.
- Slot fields: deterministic slot ID; experiment ID/version/hash; signal-provider ID/version;
  session date; source interval start/end; ready time; deadline; decision type; state; claim owner;
  claim timestamp; 30-second lease expiry; attempt count; completion timestamp; reason code;
  correlation ID; content hash.
- Signal actions: `LONG`, `SHORT`, `FLAT`. First fixture slot sets NVDA `LONG/+25 bps`, AMD
  `SHORT/-25 bps`, and all other active symbols `FLAT`; later and forced-flat slots are all flat.
- Position effects (`REQ-EXEC-002`): `OPEN_LONG`, `INCREASE_LONG`, `REDUCE_LONG`,
  `CLOSE_LONG`, `OPEN_SHORT`, `INCREASE_SHORT`, `REDUCE_SHORT`, `CLOSE_SHORT`,
  `FORCED_FLAT_LONG`, `FORCED_FLAT_SHORT`.
- Order states (`REQ-EXEC-005`): `PLANNED`, `INTENT_COMMITTED`, `SUBMISSION_STARTED`,
  `SUBMITTED`, `ACCEPTED`, `PENDING`, `PARTIALLY_FILLED`, `FILLED`, `CANCEL_REQUESTED`,
  `CANCELED`, `REJECTED`, `EXPIRED`, `SUBMISSION_UNKNOWN`, `RECONCILIATION_REQUIRED`.
- Job states (`REQ-JOB-001`): `PENDING`, `CLAIMED`, `RUNNING`, `SUCCEEDED`, `FAILED`,
  `DEAD`, `CANCELED`. Public types: `DATA_QUALITY_AUDIT`, `GAP_REPAIR`, `DATASET_FREEZE`,
  `OFFLINE_DEMO`.
- Blocking reconciliation codes (`REQ-EXEC-008`): `duplicate_client_order_id`,
  `unknown_broker_order`, `missing_broker_order`, `filled_quantity_mismatch`,
  `order_state_mismatch`, `unexpected_position`, `position_sign_mismatch`,
  `position_quantity_mismatch`, `untraceable_short_position`, `ineligible_short_increase`,
  `account_id_mismatch`, `cash_mismatch`, `equity_mismatch`, `submission_unknown`.
- Critical reconciliation codes: `live_endpoint_detected`, `paper_false_detected`,
  `non_active_symbol_position`, `non_active_symbol_order`, `duplicate_execution_id`.
- Unauthenticated routes (`REQ-API-002`): `GET /health/live`, `GET /health/ready`.
- Authenticated reads: `GET /v1/system/status`, `/v1/experiment`, `/v1/data/status`,
  `/v1/data/gaps`, `/v1/datasets`, `/v1/decision-slots`, `/v1/signals`,
  `/v1/risk-decisions`, `/v1/risk/latches`, `/v1/orders`, `/v1/fills`,
  `/v1/reconciliations`, `/v1/incidents`, `/v1/jobs/{job_id}`, `/v1/audit/status`, and
  `GET /metrics`.
- Authenticated mutations: `POST /v1/jobs/data-quality-audit`, `/v1/jobs/gap-repair`,
  `/v1/jobs/dataset-freeze`, `/v1/jobs/offline-demo`, `/v1/risk/halt`, `/v1/risk/resume`.
- PostgreSQL roles (`REQ-STORE-004`): `aqa_migrate` owns schema migration/grants only;
  `aqa_collector` reads experiment/security metadata and writes bars/gaps/watermarks/datasets/audit;
  `aqa_scheduler` reads readiness and writes slots/audit; `aqa_strategy` reads decision/data views
  and writes signals/audit; `aqa_execution` reads approved signals/data/security metadata and writes
  risk/latches/execution/orders/fills/reconciliation/incidents/audit without DDL; `aqa_control`
  reads safe views and writes bounded jobs/outbox/halt/resume without order/fill writes; and
  `aqa_readonly` has `SELECT` only on explicit safe views. The dashboard has no database role.
- Redaction keys (`REQ-OBS-001`) contain any of: `secret`, `password`, `token`, `authorization`,
  `credential`, `api_key`, `private_key`, `cookie`, `connection_string`, `database_url`.
- Audit chaining (`REQ-OBS-002`) uses
  `SHA256(canonical_json(stream_id,sequence,previous_hash,event_type,actor,occurred_at,payload_hash))`
  for `event_hash` and rejects a sequence or hash break.

The offline demo injects failures at exactly these nine boundaries: before event persistence;
after event persistence but before watermark update; correction during aggregation; after slot
claim but before signal persistence; after signal persistence but before risk decision; after
intent persistence but before submission; fake-broker acceptance before response persistence;
before reconciliation; and before forced-flatten completion. Each must recover deterministically
without duplication or leave a durable fail-closed incident.

Demo output must not calculate or report profitability, Sharpe, alpha, win rate, model accuracy,
or strategy quality. It reports only deterministic engineering evidence labeled
`OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE`.

The minimum security behavior matrix (`REQ-SEC-011`) is:

1. invalid API token rejected;
2. malformed or short token rejected at startup;
3. constant-time token comparison path;
4. unauthorized route returns no sensitive detail;
5. secrets absent from logs, errors, responses, metrics, and artifacts;
6. SQL-injection string remains data;
7. path traversal rejected;
8. symlink escape rejected;
9. arbitrary URL not accepted;
10. oversized API request rejected;
11. unknown API fields rejected;
12. excluded symbol rejected at collection, signal, risk, and order boundaries;
13. benchmark or context symbol rejected at the order boundary;
14. unregistered signal provider rejected;
15. fixture signal rejected for paper;
16. stale signal rejected;
17. modified signal hash rejected;
18. experiment, data, or policy mismatch rejected;
19. collector cannot import a trading client;
20. API cannot import a broker;
21. dashboard cannot access database modules;
22. strategy provider cannot import the execution broker;
23. execution worker cannot load plugin or model code;
24. submission disabled by default;
25. `paper=False` absent;
26. configurable live hostname absent;
27. unknown broker account rejected;
28. ambiguous submission blocks and reconciles;
29. duplicate order does not resubmit;
30. duplicate fill update is idempotent;
31. corrupt or malformed database state fails closed;
32. invalid configuration fails startup;
33. missing required secret fails safely;
34. world-readable or symlink secret file rejected;
35. unauthorized PostgreSQL role write fails;
36. malicious metric-label input cannot create unbounded labels;
37. audit-chain tampering detected;
38. job replay and idempotency enforced;
39. stale short metadata blocks opening;
40. compromised strategy proposal cannot bypass risk;
41. API route inventory contains no trade mutation;
42. no external network occurs in tests or demo;
43. dependency, secret, and container scans are wired into CI;
44. forced-flatten failure creates an incident rather than success.

## Normative command and container inventory

`REQ-DX-001` requires these exact safe command paths under both new console aliases:

```text
aqa doctor
aqa config validate
aqa secrets bootstrap-local
aqa db migrate
aqa data status
aqa data ingest-fixture
aqa data aggregate
aqa data freeze
aqa scheduler status
aqa shadow run-once
aqa demo
aqa audit verify
aqa api serve
aqa dashboard serve
```

`REQ-OPS-002` requires the exact Compose services `postgres`, `migrate`, `control-api`,
`market-data-worker`, `scheduler-worker`, `strategy-worker`, `execution-worker`, `dashboard`,
`market-data-live`, and `paper-execution-worker`. Only `market-data-live` is in profile
`market-data`; only `paper-execution-worker` is in profile `paper`; neither starts by default.
Only API/dashboard publish `127.0.0.1:8000:8000` and `127.0.0.1:8501:8501`; PostgreSQL is
unpublished except for an explicit loopback-only `db-debug` profile.

The exact service credential matrix (`REQ-OPS-003`) is:

| Service | Secret authority |
| --- | --- |
| `postgres` | database bootstrap secrets only |
| `migrate` | migration database URL only |
| `control-api` | control database URL and operator token |
| `market-data-worker` | collector database URL; no Alpaca secret offline |
| `scheduler-worker` | scheduler database URL only |
| `strategy-worker` | strategy database URL only |
| `execution-worker` | execution database URL; fake broker only |
| `dashboard` | API token only |
| `market-data-live` | collector database URL and data credentials only |
| `paper-execution-worker` | execution database URL, paper credentials, and account hash only |

Every service is nonroot, drops all capabilities, prevents privilege escalation, uses a read-only
root where practical and tmpfs `/tmp`, and has service-specific volumes, health, bounded restart,
resource limits, and only required secret mounts. Network separation is not an outbound firewall.

## External dependencies and assumptions

- A local or CI PostgreSQL 16 service is required to verify migrations, roles, concurrency, and
  backup/restore. Its absence blocks only those executed checks, not offline implementation.
- Existing collector and legacy paper adapters have mock-based tests. The target platform adapters
  remain incomplete; when implemented they use primary contracts and mocks and remain
  `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` because this program must not use real credentials or
  connect to either external service.
- Container digest resolution, vulnerability databases, remote CI, package installation from a
  clean environment, and hook activation depend on available network/runtime support. Exact
  unavailable checks are reported rather than inferred.
- Locally installed strategy extensions are trusted by the operator. Process privilege separation
  limits broker authority, but arbitrary Python sandboxing is outside scope.
- PostgreSQL/Compose is the operational target. SQLite remains only for isolated tests, the
  deterministic offline path, and preserved legacy behavior.
