# Public paper product

## 1. Project objective
Build a free-to-use public, multi-user paper-trading product. This is the active plan;
platform-core.md retains private-platform evidence. Status: PARTIALLY_IMPLEMENTED.

## 2. User-visible outcome
Register, verify identity, connect a personal Alpaca paper account, define or integrate a
non-AI strategy, approve its immutable configuration and limits, run, inspect, stop,
export and delete. Every state must come from durable backend evidence.

## 3. Current repository state
Baseline branch feature/multi-user-paper-platform at a475242. Private control uses one
operator token; existing tables are experiment-scoped. PaperAuthorizationDecision rejects
approval and PaperExecutionCycle cannot execute an approved model. The SDK factory fixes
the paper host but takes operator key files; it has no customer OAuth lifecycle. Its order
summary requires exact weighted-average equality with FILL evidence; provider precision
remains unresolved. Trusted Python plugins are not a public-code sandbox.

Pre-existing uncommitted work: src/adaptive_trader/web schema/model sketches,
docs/product-planning.md, the September 14 gap assessment, this plan and platform-core
continuation text. The sketches model an internal simulator and remain unused and preserved.
Planning text is deliberately consolidated here; unrelated drafts are excluded from commits.

## 4. Requirements being addressed
PUB-001 verified identity and tenancy; PUB-002 broker OAuth; PUB-003 bounded immutable
strategies; PUB-004 independent non-AI approval/risk; PUB-005 durable execution/recovery;
PUB-006 truthful usable UI/reporting; PUB-007 external signals; PUB-008 public operations,
capacity, quotas, exports and deletion. See docs/requirements.md for the release ledger.

## 5. Scope
Public signup; Alpaca paper; US equities/ETFs; long-only whole shares; no leverage;
regular sessions; evaluation at least one minute apart; market and limit DAY policies.
Both hosted declarative rules and a scoped external signal API with Python client.

## 6. Explicit non-goals
AI, backtester development, arbitrary uploaded code, real-money trading, billing,
unlimited capacity claims, paid infrastructure, public exposure or actual broker orders
without additional authorization. Local doubles never substitute silently in production.

## 7. Existing architecture
Python/FastAPI, PostgreSQL, Alembic, durable jobs/leases, independently validated risk and
execution, append-only evidence and separate collector authority are retained. Existing
read-only dashboards remain private. Public strategies use platform canonical hashing.

## 8. Proposed implementation
Incremental modular public API and React/TypeScript UI; separately runnable workers and
collector. New schema owns customer data. Reuse existing execution behind narrow adapters
when its identity and strategy contracts are sufficient; do not reassign private records.
Keycloak provides maintained identity lifecycle, Authlib OAuth/OIDC, joserfc JWT validation,
and cryptography authenticated encryption. The app dependency lock is separate from the
frozen root graph. All identities derive from verified issuer+subject, never email linking.

## 9. Alternatives considered
Reusing the operator token would share authority. A custom password system duplicates
security-sensitive identity work. Running public Python uploads violates the chosen trust
boundary. Internal fake fills do not satisfy connected paper brokerage requirements.

## 10. Why the selected design is preferable
An additive public boundary preserves private state and the frozen research system while
making user ownership explicit. Maintained identity components cover lifecycle protocols.
Bounded declarative targets give useful behavior without exposing a code execution surface.

## 11. Architectural boundaries
Strategies propose values only; they receive no credentials, imports, filesystem or network
capability. Browser flags never establish asset eligibility, session state or ownership.
Collector data credentials remain separate from recoverable broker tokens and execution.
The new domain's content hash is identity, not an approval or an authorization token.

## 12. Data-flow changes
Verified OIDC -> opaque session -> owned immutable definition -> account-bound approval ->
durable deployment -> proposal -> independent account risk -> intent -> fixed paper broker
-> fills/reconciliation -> owned UI. Only the identity/configuration segment exists so far;
no public proposal currently reaches a broker.

## 13. State-ownership changes
A new schema stores issuer/subject identities, server sessions and owned versions. Strategy
rows use explicit predicates plus forced RLS; runtime lacks UPDATE/DELETE on versions.
Missing tenant context reads no rows. Privileged identity/session tables are not RLS-protected;
SQL injection or runtime database credential compromise remains a cross-customer risk.
Tenant context must be set locally per transaction from authenticated server state.

## 14. Public API or schema changes
Declarative v1 contract supports constant targets and moving-average targets. Defaults enter
canonical hashes. Maximum 200 consecutive completed minute bars, 100,000 target shares and
one-minute-to-one-day cadence are contract bounds, not safe execution limits. Public API
v1 currently supports sign-in, sign-out and saved versions; no trading endpoint is exposed.

## 15. Security implications
Exact issuer/audience/nonce/signature/expiry and verified email; PKCE for OIDC; backend-only
exchange. Opaque HttpOnly sessions, separate CSRF nonce, same-origin mutations and no-store
responses. Encrypted provider access tokens; token introspection on each request fails
closed. Shared database quotas and login limits. Further public abuse protection, request
deadlines, operator MFA enforcement, token rotation, logout lifecycle and audit review remain.

## 16. Performance implications
Version storage limited to 100/customer with a transactional customer lock. Queries bounded
to 100 rows; API pools bounded. Benchmark targets remain 100 active browsers and 500
one-minute deployments across at least 100 synthetic accounts. No capacity measured yet;
no public launch limits are justified yet. Per-request introspection needs measurement.

## 17. Migration or compatibility strategy
Additive aqa_public schema and separate migration/runtime roles. No legacy ownership backfill.
Root pyproject.toml, uv.lock and 61-file AI manifest remain unchanged. Backtester boundaries:
src/adaptive_trader/backtest.py, replay.py, CLI entry points, their tests and configs/backtest.yaml
and configs/replay.yaml. Run regressions; do not redesign or expose them.

## 18. Failure modes
Missing, stale, future or incomplete observations produce no actionable proposal. No target
is different from an explicit zero target. OAuth state is one-use and browser-bound. Invalid
sessions fail closed; local logout persists even if provider revocation is unavailable. Broker
ambiguity must reconcile before retry, but that public execution path is not implemented.

## 19. Rollback or recovery approach
Stop public services independently. Do not drop private schemas or volumes. Disposable local
fixtures have a dedicated Compose project and loopback ports. Production rollback, encrypted
backup/restore, retention and deletion drills remain required, not implied by migrations.

## 20. Test strategy
Offline domain/provider contracts; disposable PostgreSQL actual-role isolation, concurrent
quotas, pool reuse, API adversarial requests; two-user real browser tests; accessibility and
responsive review; migration/restore checks; load and overload tests. Preserve root gates,
frozen-AI verification and both legacy regressions. No ordinary test uses Alpaca credentials.

## 21. Acceptance criteria
Complete user journey with two independent users and no cross-account effects. Worker retries
recheck current approval/risk. UI states reflect durable outcomes. Full required checks pass.
Implementation completeness and public-launch readiness are separate judgments; neither is
established by the first authentication/configuration slice.

## 22. Ordered milestones
1. Baseline, bounded contract and disposable development environment.
2. Verified customer identity, tenant ownership and saved versions through a real browser.
3. Alpaca OAuth lifecycle and immutable hosted/external strategy management.
4. Non-AI approval, account risk, durable intent, worker/reconciliation and emergency controls.
5. Two-user connected start/observe/pause/stop/recovery workflow.
6. Reporting, exports, developer access, deletion and product refinement.
7. Multi-instance/load/security/restore/operations and release review.

## 23. Progress checklist
- Fresh root baseline: 3,065 passed, 10 skipped (PostgreSQL unavailable to offline run).
- Declarative contract/evaluator/CLI implemented; 48 focused tests passed.
- Existing secret loader extended with two names for new identity callers; 127 combined
  domain/secret-loader tests passed.
- Identity/configuration application: functioning local slice. Fourteen offline identity
  tests, actual PostgreSQL isolation/quota/idempotency tests and five real-browser tests
  passed. Registration completion/recovery and public operations remain incomplete.
- Commit 40532ec pushed to feature/multi-user-paper-platform; all five existing GitHub
  workflows succeeded for that exact SHA. No PR or deployment created.
- Subsequent execution/product/operations milestones NOT_IMPLEMENTED.

## 24. Decisions made
Maintainer explicitly delegated scoped commits and feature-branch pushes on September 14;
this replaces commit response pauses only. No PR, merge, force-push, deployment, paid service
or broker order is authorized. Freeze each reviewed boundary before staging. Use configured
identity. Existing neutral branch retained. Inspect workflows before each initial push.
One active plan; prior planning brief and gap assessment are historical inputs.

## 25. Unexpected discoveries
Root platform package imports config on import, so the separate API also requires its
existing PyYAML dependency. Pre-existing unused web sketches fail root formatting/import
checks. A FastAPI closure/forward-annotation defect found in the browser was fixed. Initial
local fixture path and CSS import mistakes were corrected rather than bypassing checks.

## 26. Validation evidence
September 14, local macOS, base a475242 plus working changes:
- .venv/bin/python scripts/verify_no_network.py pytest -q: 3065 passed, 10 skipped, 2 warnings.
- .venv/bin/python scripts/verify_main_ai_freeze.py: 61 protected files/inventory/dependencies pass.
- .venv/bin/python scripts/verify_no_network.py adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic: exit 0.
- .venv/bin/python scripts/verify_no_network.py adaptive_trader.cli replay --config configs/replay.yaml: exit 0.
- .venv/bin/python scripts/verify_no_network.py pytest -q tests/public_product tests/unit/test_platform_runtime_settings.py tests/unit/test_platform_security.py: 376 passed.
- PYTHONPATH=src apps/public/api/.venv/bin/python scripts/verify_no_network.py pytest -q apps/public/api/tests: 11 identity tests passed before PostgreSQL tests were added.
- Opt-in apps/public/api/tests/test_postgres.py with three disposable acknowledgements: five passed; dedicated collector_test at loopback 55438, restricted runtime role, verified Docker disposable label.
- npm run build in apps/public/ui: passed TypeScript and Vite build.
These results are partial evidence; no public availability, broker orders or capacity claim.

Reviewed boundary 1: src/adaptive_trader/public_product, its tests and maintained JSON
example, public-strategies.md and current requirement/architecture/plan navigation. No
root dependency changes, broker calls, persistence or approval authority in this boundary.
Security review covered unknown fields, decimal coercion, window/input bounds, UTC/future/
stale data, copied-model revalidation and error redaction. Simplification retained two
closed rule types and existing canonical hashing; no plugin evaluator or DSL. CLI behavior
reviewed through valid and oversized/invalid inputs. No unresolved high finding in this
boundary; browser/provider/application changes are a separate candidate.

Fresh root run after the two exact secret-inventory expectations were extended: 3,135
passed, 10 PostgreSQL skips, two warnings in 186.96s. Frozen manifest still passes.
Root typecheck passes (157 source files). Scoped format/lint/type checks pass; whole-tree
format/lint still report only the pre-existing unused web/schema.py draft. Bandit scoped
to public_product reports no findings. Both legacy regressions passed as above.
First committed message: feat(strategies): add bounded declarative paper targets.

Reviewed boundary 2: apps/public API, UI, additive migrations, synthetic development
fixture/runner, locked dependencies and dedicated CI; the two shared secret namespace
names and their exact tests; status/security/dependency/evidence documentation. Complete
review and commands: docs/evidence/public-workspace-20260914.md. Confirmed scoped delegation
from section 24 applies. Only generated private local state and prior drafts remain outside
this boundary. Proposed message: feat(auth): add isolated customer strategy workspaces.
Fourteen offline identity tests, twenty opt-in API tests (including six actual PostgreSQL
cases) and five browser tests pass, including an actual lost-save
response followed by an idempotent retry. Audits report no known Python/npm vulnerabilities.
Production app packaging, identity administration/abuse and provider activation are pending.

## 27. Final outcome and remaining limitations
Implementation complete: no. Public-launch ready: no. Work is partial; the strategy slice is pushed and the identity/workspace slice is being
committed after final checks. Next: verify its dedicated GitHub workflow, then implement
Alpaca OAuth account connection with strict paper consent and ownership, followed by
independent approval/risk/execution. Complete account lifecycle and operational gates remain.
External gates: provider application registration/consent, data storage/redistribution and
per-user entitlements, available hosting and budget, DNS/TLS/secrets/email, monitoring,
restore/capacity evidence, legal/owner review, usability testing and independent security review.
