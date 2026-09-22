# AQA investing workspace prototype

## Implemented boundary

The isolated `/studio.html` React entry demonstrates a conversational investing workspace using synthetic data and scripted responses. It does not call the authenticated application, providers, brokerages or a backtester. The normal `index.html` entry and its security controls are unchanged. Initial intended scope is US adults 18+, US stocks and ETFs, one account with multiple strategy projects and bring-your-own provider API access. Live execution is unavailable.

## Experience

A white/soft-gray interface, restrained blue controls, system typography, generous spacing and progressive disclosure apply the supplied Apple-inspired design guidance without copying assets. Persistent desktop navigation becomes compact navigation on smaller screens. Home, Conversations, Portfolio, Strategies, Activity and Settings share typed state.

First use offers a separate synthetic demo entry and a dismissible three-step service simulation: eligibility declarations, sample holdings or $10,000 simulated cash, then the provider/API billing explanation. Ineligible declarations cannot complete service onboarding. Nothing implies identity verification.

Conversations support multiple local projects, names, deletion, retained drafts, contextual scripted portfolio/strategy/revision/research responses, unsupported-question acknowledgement, and loading/cancel/error/retry controls. Artifacts open beside the conversation on desktop and below it on mobile. Portfolio values include cash; manual sample holdings are validated, and allocation changes show trade/cash effects before explicit application.

Strategy rules have immutable version identities. Edits create new unreviewed versions without changing the running version or historical evidence. Predefined passing, failing and larger-capital fixtures demonstrate reports and policy outcomes. Custom edits do not magically produce those reports. Each report identifies assumptions, limitations, fees, slippage, illustrative curve/benchmark, trades, holdout and robustness checks.

## Independent authorization

- **Collaborative — Build and refine together.** Activation requires exact-version manual review.
- **Automatic Research — Research independently. Bring me proposals.** Produces a proposal and evidence but cannot activate it.
- **Automatic Management — Manage within the boundaries I set.** After explicit per-strategy/account authorization, a bounded sample cycle independently proposes, evaluates, approves and activates a qualifying revision. No per-revision confirmation is requested.

`policy.ts` is separate from conversation behavior. Missing/stale/failing evidence blocks. Exceeding assets, capital, sizing, timing or locked entry/exit/risk rules requires additional approval. Observation, research budget and update frequency are enforced. `activationDecision` also checks account capital and allocations reserved by other deployments. The system cannot change its authority. Configuring authority deliberately captures the current rule baseline. Pre-existing holdings default to excluded; supplied fixtures never change them. Exact-version human approval does not expand future automatic authority.

Changing authority or mode cancels pending work. Pausing research, stopping revisions, pausing new orders and separately confirming closure of sample holdings have distinct effects. None of the first three liquidates positions. Activation notifications and local Activity explain the decision and exact version, evidence association and unchanged existing-position effects. Simulation activation changes deployment state, not real orders or invented fills.

## State and recovery

`aqa.studio.demo.v2` holds only local demo state. Missing state seeds a fresh demo; malformed/obsolete state shows recovery instructions and is not silently overwritten. Settings reset clears only this namespace. Storage failures preserve in-memory work with a warning. Cross-tab changes cancel pending work and stop this tab from mutating or activating until reload/reset. Local JSON export includes synthetic strategies and settings, not credentials.

Provider choices are preview preferences. The API-key field is disabled, and demo connection stores a boolean only. A consumer chat subscription must not be assumed to include API usage. No credential or session storage is read by this entry. Free text is rendered by React; common credential-shaped text is removed from conversation input. Users are instructed not to enter personal information. This is not a general secret-detection guarantee or secure custody system.

## Local development and verification

From `apps/public/ui`:

```sh
npm run dev -- --port 5179
npm run check
npm run build
npm run format:check
npm exec playwright test -- --config playwright.studio.config.ts
```

Open `http://127.0.0.1:5179/studio.html`. The isolated suite uses installed Google Chrome and no authenticated backend. Normal application tests retain their existing configuration and require their separate service prerequisites. No dependency changes are needed.

On 2026-09-22: 32 isolated tests passed, including domain, eligibility, conversations, portfolio, version invalidation, modes, cancellation/retry, recovery, export, keyboard focus and axe checks across six destinations at 1440/768/390px. TypeScript, production build, frontend formatting and 61-file freeze verification passed. Final review evidence, the 127 passing safety/architecture tests and responsive follow-up are recorded in the [execution plan](execution-plans/investing-workspace.md).

## Limitations

Fixture evaluation is not actual backtesting, and sample observation days are not elapsed market observation. Automation is a bounded, visible demonstration while the tab is open; it is not an unattended scheduler. Local state and history are mutable and are not production authorization/audit infrastructure. No real credentials, provider integration, brokerage connection, investment recommendation, order execution or public deployment is included. Passing checks does not prove future profitability or production readiness.
