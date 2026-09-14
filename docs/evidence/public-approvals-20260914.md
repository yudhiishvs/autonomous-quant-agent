# Explicit public strategy approvals — September 14, 2026

Status: `PARTIALLY_IMPLEMENTED` for the public approval/risk requirement. The review,
confirmation and revocation journey is implemented and locally verified. Account-wide risk,
execution-time authorization, workers and broker orders are not implemented by this slice.
No public release or actual Alpaca validation is claimed.

## Implemented behavior

An authenticated customer selects an immutable strategy version and an owned paper account,
sets bounded decimal USD limits and requests a review. The server verifies the provider's
paper-account identity and equity, checks ownership and strategy targets, then stores a
canonical binding with a five-minute review deadline. Confirmation requires the exact
reviewed fingerprint and explicit paper consent, a currently valid local session and another
provider account check. Database transactions serialize account changes and approval changes.
The signed binding includes owner, application account, physical broker-account fingerprint,
version/configuration hash, connection generation, risk-limit hash and exact UTC expiry.

Ed25519 uses the already locked maintained cryptography dependency. The signing key is a
separate private file, never part of database records or browser responses. A replacement
key invalidates old signatures. Missing signing configuration disables new approval, while
revocation still works. No new package or root lock change is required.

One active approval is allowed per account/version. A later confirmation revokes the older
receipt. Review retries use owner/request identity; conflicting payload reuse is rejected.
Repeated/concurrent confirmation produces one approval event. Revocation is append-only and
cannot be undone by changing the mutable state projection. A refresh changes account snapshot
revision only; reconnection/disconnect also increments the material connection generation.
The UI reloads approvals after account operations and labels approved configuration separately
from execution. Revocation does not cancel orders or close positions.

## Persistence and compatibility

Migration `public_0004` is additive within `aqa_public`. It adds per-account connection
generation and composite owner/object keys, immutable approval records and append-only events.
It assigns no legacy data to customers. Both new tables use forced RLS and owner indexes.
The restricted runtime may insert receipts, change transition columns and append events;
it cannot edit bindings or update/delete event evidence. A partial unique index enforces the
single active account/version receipt. Approval queries/history are bounded at 100 per owner;
the serialized lifetime development quota includes all drafts and revoked/expired records.

Existing local fixtures need the new private signing file and explicit migration before
restarting the API; the fresh fixture bootstrap supplies it. No runtime startup creates schema.
Rollback would discard approval evidence, so retain the additive schema and block execution
while investigating rather than applying a destructive downgrade to important records.

## Verification performed

- Existing root offline suite with `scripts/verify_no_network.py pytest -q`:
  **3,205 passed, 10 PostgreSQL skips, two warnings**, 189.39 seconds.
- `.venv/bin/mypy src docker`: **158 source files pass**. `make typecheck` initially hit
  the sandbox's unrelated uv cache restriction; the exact installed mypy command passed.
- AI freeze verifier: **61 protected files, inventory and dependencies unchanged**.
- Existing synthetic backtest and replay under the no-network launcher: **exit 0**.
  The backtest's reported risk hard stop is an expected synthetic outcome, not live evidence.
- `make -C apps/public check`: Bandit, Ruff, API typing, offline tests, UI formatting,
  TypeScript and production asset compilation pass. API offline: **39 passed, 21 guarded
  PostgreSQL skips**, two upstream deprecation warnings.
- API suite with all three explicit disposable acknowledgements: **60 passed**, two warnings,
  2.61 seconds. Target verified as task-owned `aqa-public-disposable-postgres-1`, disposable
  label, only loopback port 55438, `collector_test`, restricted runtime login.
- Synthetic broker browser journey: **one passed**. It uses actual local Keycloak and
  PostgreSQL; no request reaches Alpaca. Consent/connect/save/review/confirm/reload/revoke/
  disconnect were exercised; screenshots at **390, 768 and 1440 pixels** were inspected.
  No horizontal overflow or automated axe WCAG 2/2.1/2.2 A/AA violations in those states.
  The normal API was restored after the guarded synthetic server was stopped.

The normal workspace browser suite also passed all five tests after explicitly naming its
workspace status region; its original saved/sign-out assertions remain. Final staged checks
are recorded in the active plan.
Generated screenshots, local signing files, logs, databases and browser artifacts are ignored
and excluded from commits. No human usability study or assistive-technology user study occurred.

## Review record

Behavior/persistence review traced browser request through CSRF/authentication, provider
verification, owner transaction, immutable binding and signed transition. A stale approval
label after an account mutation was corrected by reloading the approval component. Repeated
and conflicting requests, simultaneous confirmation, expired review, account reconnect,
session sign-out and database role restrictions have outcome-based tests.

Security review:

- Changed boundary: authenticated customer consent to a signed configuration receipt;
  it does not cross an order-submission boundary.
- Inputs: closed strategy/risk/consent models, UUID references, exact fingerprint, existing
  body/time/rate limits. Money rejects float/bool/nonfinite/excess precision and contradictory
  ceilings; equity and target checks cannot be skipped through copied Pydantic objects.
- Secrets/privileges: API has its restricted database login, identity/provider encryption
  material and separate signing key; browser gets none. No runtime DDL or arbitrary file,
  host, code/import or command capability is added.
- Forgery/isolation: actual runtime-role tests cover guessed IDs, cross-owner references,
  composite constraints, missing tenant context, pool reuse, signature changes and wrong keys.
- Replay/concurrency: owner authority lock, immutable request hash, account revision and
  connection generation, unique active receipt, atomic permanent consent/revocation evidence.
- Staleness: fresh provider account and valid session checked again at confirmation; expired
  drafts cannot confirm, expired/reconnected/revoked receipts are not effective. Independent
  execution-time rechecks remain a mandatory next milestone.
- Compromise: direct database writes cannot forge signature bytes or erase runtime-protected
  revocation events. This is not protection against complete runtime-database compromise:
  the shared identity/session tables still permit a database attacker to impersonate a
  customer through the API. A compromised API also holds signing and broker material.
  Identity/session hardening, process/egress isolation and key custody remain release gates. PostgreSQL administrator/host compromise is outside RLS guarantees.
- Side effects: provider account reads and local approval records only; no order or cancel
  method, uploaded execution, unbounded worker or new external destination exists.
- Leakage: closed HTTP responses expose limits, fingerprints and state, not signature key,
  raw broker identifier or recoverable grants; existing sanitized response/provider errors
  remain. Generated fixture secrets are private and never printed/staged.

Simplification retained existing canonical hashing, private-file loading, broker transport,
owner transaction and React form patterns. Account verification is shared by refresh and
approval rather than duplicated. No custom cryptography, additional service or dependency was
introduced. No unresolved BLOCKER/HIGH finding remains in this bounded configuration slice;
that is not clearance to execute orders or publish the product.

## Remaining gates

The full user assignment remains incomplete: registration/recovery/admin/abuse validation,
external signal credentials/API, public market-data rights, independently coordinated risk,
durable deployments/intents/reservations, execution and reconciliation, reporting, exports,
deletion, production packaging/hosting/secret custody/restore and measured capacity. The
requested 100-active-browser/500-deployment targets have not been demonstrated. Free service
availability and real provider compatibility have not been established. AI/backtester remain
excluded. No PR, protected merge, public exposure, paid service or actual broker order occurred.
