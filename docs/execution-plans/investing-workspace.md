# Investing workspace implementation plan

## Objective and approved scope
Deliver the approved AQA interactive browser prototype at `/studio.html`: six connected destinations, beginner onboarding, contextual conversations, portfolio exploration, immutable strategy versions, illustrative evaluation, bounded automatic management, local persistence and recovery. This plan complements the full platform plan; backend architecture and schema sections remain unchanged there.

## Architecture and invariants
React/TypeScript/Vite. `model.ts` owns types and portfolio arithmetic; `fixtures.ts` owns synthetic inputs; `policy.ts` independently evaluates proposals; `operations.ts` owns immutable transitions; `persistence.ts` validates versioned local state; separate presentation modules own views. No provider, brokerage, auth or backend clients are imported. No dependency changes. No live execution. Currency calculations use integer cents at transaction boundaries. All report evidence is explicitly fixture-bound; arbitrary edits cannot inherit it. A running version is separate from the latest draft. Revocation cancels pending work and never closes positions.

## Authority and compatibility
On 2026-09-22 the maintainer delegated implementation, incremental commits and pushes for this prototype on `feature/investing-workspace`. This supersedes per-commit response pauses only for this scope. No merge, force push, paid action or public deployment. The existing dirty checkout is preserved; work occurs in an isolated worktree. The normal authenticated entry, protected services and frozen manifest are unchanged.

## Milestones and progress
- [x] Domain: add negative/positive policy, immutable-version, portfolio and storage tests; implement typed state, fixture evidence, transitions and validation. Run isolated domain tests and build; inspect staged diff; commit and push.
- [x] Workspace: replace the previous prototype in place with complete connected views, deterministic bounded workflows, dialogs and responsive Apple-inspired styling. Exercise onboarding, conversation/artifacts, portfolio changes, settings and all three operating modes. Build and run browser tests; commit and push.
- [x] Verification: complete accessibility, keyboard, cancellation/recovery, responsive screenshot, no-network, freeze and formatting checks; fix defects; update docs and review record; commit and push; open preview.

## Failure and recovery focus
Malformed/obsolete storage resets through explicit recovery, never a blank page. Stored state is untrusted and is structurally validated. Duplicate or canceled asynchronous jobs cannot activate. Missing evidence blocks; excess authority escalates; permission changes invalidate pending work. Quota failures retain in-memory work with a visible persistence warning. Multi-tab storage changes stop this tab rather than silently merging permissions. All input remains text; no eval or HTML injection. Reset affects only the demo namespace.

## Acceptance and verification
Use meaningful Playwright domain and browser tests, tsc, Vite build, Prettier, axe, screenshots at 1440/768/390, keyboard dialog checks, `git diff --check`, and the 61-file freeze verifier. Synthetic local UI tests do not establish production readiness. Reuse existing dependencies and installed Chrome. Production auth suites require their separate backend stack and are not substitutes for the isolated prototype suite.

## Review record and evidence
Pending implementation. Final review will trace persistence, policy and timing; inspect all staged files; record test results and residual limits here. Rollback removes the isolated studio entry without altering backend state.

### Domain milestone — 2026-09-22
`npm run check`, `npm run build`, five isolated domain tests, Prettier on new modules, `git diff --check`, and freeze verification (61 files) passed. Domain implementation is `IMPLEMENTED_AND_VERIFIED` at the synthetic boundary; UI integration remains pending. Security review: pure operations cannot reach network/files/credentials; JSON is bounded and structurally checked, no executable deserialization; immutable edits invalidate evidence; replay of activation is idempotent. Persistence is browser-local and is not tamper-proof authorization. Simplification review retained one pure evaluator and one transition module without new dependencies. Staged boundary: domain modules/tests, isolated test config and execution plan only.

### Connected workspace milestone — 2026-09-22
All six screens, onboarding, conversation/artifact flows, portfolio edits/previews, immutable strategy edits, reports, mode authority, automatic activation, activity and settings are connected. `npm run check`, `npm run build`, `npm run format:check`, and 23 isolated tests passed. Axe checks covered all six destinations at 1440/768/390 plus mobile artifact/report and authority dialog. Keyboard dialog trapping/restoration passed. Screenshots inspected at desktop/mobile Home and tablet Strategies. Full final evidence is still pending.

Fresh domain review identified and corrected account-capital and locked-risk-rule gaps, empty-strategy/invalid-authority storage acceptance, and two fixture-report inconsistencies. Regression tests demonstrate the corrections. Simplification keeps views separate from deterministic policy and storage; no dependencies, schema, backend or auth changes. Security review confirms no API imports, credential inputs or financial network methods. Pending work is canceled on mode/authority/navigation changes and guarded by generation before completion. Browser state is deliberately not trusted production authorization.


### Final verification and review — 2026-09-22

`REQ-STUDIO-001`: `IMPLEMENTED_AND_VERIFIED` within the explicitly synthetic prototype boundary.

| Check | Observed result |
| --- | --- |
| `npm run check` | Passed |
| `npm run build` | Passed; both HTML entries produced; normal entry asset hashes unchanged from baseline |
| `npm run format:check` | Passed |
| `npm exec playwright test -- --config playwright.studio.config.ts` | 32 passed in 58.6 seconds |
| Final responsive/artifact subset after CSS adjustment | 3 passed in 7.3 seconds; axe, overflow and visible desktop composer assertions |
| `python -m pytest -q tests/safety tests/architecture` using existing repository environment | 127 passed in 5.25 seconds |
| `python3 scripts/verify_main_ai_freeze.py` | 61 protected files, inventory and dependencies unchanged |
| `git diff --check` / cached diff check | Passed |
| Normal authenticated browser suite discovery | Original six tests retained; isolated prototype excluded from that configuration and run explicitly with its own config |

Manual review inspected desktop Home/conversation/artifact, tablet Portfolio/Strategies and mobile Home/report screenshots. Keyboard Tab, Escape and focus restoration were also exercised in the in-app browser. Automated screenshots cover all six destinations and artifacts at 1440, 768 and 390 pixels. Mobile navigation labels were enlarged; desktop conversation and artifact panes now scroll independently so opening a long report does not hide the composer.

The expanded regression coverage includes malformed structures/references, duplicate identities, invalid authority/timestamps, aggregate capital reservations, research budgets/observation/frequency, draft/running separation, all policy outcomes, rejection of activation from a stale tab, version capacity, notification preferences, no provider/broker requests, credential-field disabling, conversation injection, storage quota failure, export/reset and exact-version activity links. The new rules use explicit 100-daily-close entry/exit conditions. Report values agree with their fixture curves and position sizing.

Final security review answers the repository review questions: the changed trust boundary is untrusted local demo input into validated synthetic state; no secret access or new privileges exist. Text is escaped, numeric/enum/reference inputs are bounded, and serialized state is size-limited and structurally checked. A compromised same-origin browser can alter its own demo, which is why this is not production authorization. Persistent mutation is limited to one namespace. There are no broker effects, arbitrary execution, arbitrary file access or network-call implementations. Export contains demo configuration only. Replayed activation for the same version is idempotent; stale callbacks are canceled/generation-checked; cross-tab changes stop further mutation/activation until reload/reset. Regression tests exercise malformed, stale, duplicate, oversized/capacity and unauthorized paths. Residual risks are local browser tampering, same-origin compromise and voluntary free-text disclosure; common key-pattern removal is not a universal secret detector. Nothing claims host-level outbound enforcement.

Simplification review split strategy dialogs from the main strategy view, retained pure policy/transition modules, and added no dependencies. A fresh focused review verified fixes for its prior account/risk/storage/report findings. The remaining version-capacity issue was fixed and regression-tested. No unresolved blocker/high finding remains. Existing auth, collector, approval and protected source files have no branch diff.

The complete backend/identity/database/browser stack and full Python integration suite were not run for this isolated frontend change. These checks do not establish production readiness. No brokerage, provider or public deployment was attempted. The local preview remains on port 5179. All scoped milestones are committed and pushed on the authorized feature branch; unrelated original-checkout work remains untouched.
