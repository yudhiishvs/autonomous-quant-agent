# Paper-account connection verification — September 14, 2026

Connection implementation: `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`. The broader PUB-002
requirement remains `PARTIALLY_IMPLEMENTED` because public entitlements and activation are
unresolved. Public-product implementation and public-launch readiness are both incomplete.

## Behavior and contracts

The new account screen starts a server-authenticated, CSRF-protected paper-only consent
flow. Alpaca's authorization URL always includes `env=paper` and only `trading` scope.
State is random, single-use, expiring and bound to the initiating customer session.
Backend code exchanges the code, verifies the account using the fixed paper endpoint,
then stores an encrypted owner/account-bound grant and a decimal-preserving snapshot.
No endpoint in this slice submits, cancels or changes an order.

Application OAuth credentials differ from the collector's user API keys. The client
secret and broker encryption key use distinct file namespaces. The existing identity
transport moved into a shared module to enforce the same fixed-origin, no-redirect,
response-byte and network-deadline rules for both integrations. Provider errors and
response validation errors are sanitized. Closed response models omit token material and
the broker's full account identifier.

Migration `public_0003` creates owned connection/attempt/authority tables with forced RLS.
A global unique broker account claim prevents silent assignment across customers, including
concurrent attempts. Claims persist after disconnect. The runtime may update only the
session `token_hash` column to obtain PostgreSQL's row-sharing lock required to serialize
credential publication with logout; the application does not change that column.

Disconnect deletes the local recoverable token, increments a connection-generation fence
and invalidates pending consent in that workspace. In-flight older callbacks cannot
restore access. Refresh/revocation uses the account revision, so stale responses cannot
overwrite a new connection. Disconnect does not cancel accepted orders, close positions
or revoke the provider grant. The confirmation states these consequences. There is no
invented refresh/revoke endpoint; the user manages provider grants in Alpaca.

Balances are account snapshots, not strategy-attributed performance. Decimal strings remain
exact through storage/API/display. Provider snapshot timestamps are UTC; the browser renders
local time and labels them as the last check. These changes do not resolve the separate
private execution fill-summary/reconciliation precision limitation.

## Executed verification

Environment: the existing named, labelled disposable PostgreSQL 16 on loopback 55438,
real local Keycloak, and credential-free provider doubles. No Alpaca network call or broker
order was made. Canonical commands and startup remain in the [app README](../../apps/public/README.md).

| Check | Result |
| --- | --- |
| `make -C apps/public check` | Bandit, Ruff/format, mypy (9 application files), 38 offline tests with 13 guarded storage skips, UI format/TypeScript/Vite passed |
| Three-acknowledgement `pytest -q apps/public/api/tests` | 51 passed, 2 upstream deprecation warnings, 1.77s |
| Normal `npm run test:e2e --prefix apps/public/ui` | 5 real identity/storage browser tests passed with brokerage unconfigured |
| `npm run test:broker --prefix apps/public/ui` with the explicit test server | 1 integrated browser journey passed: paper consent, persistent account view, refresh, modal cancellation, confirmed disconnect and reload |
| Account screenshots and axe | 390/768/1440 widths inspected; no overflow or automated WCAG 2/2.1/2.2 AA violations, including the disconnect dialog |
| Fresh root offline suite | 3,157 passed, 10 guarded PostgreSQL skips, 2 warnings, 182.10s |
| Root typecheck and both preserved legacy CLI regressions | Passed; no backtester behavior was changed |
| AI freeze verifier | All 61 protected files/inventory/dependencies unchanged |

The real Authlib client was exercised through prepared HTTP requests: form-encoded client
secret exchange, fixed callback, paper-only bearer account reads, no redirects and finite
timeouts. Tests reject extra scopes, malformed grants, wrong account state/currency,
nonfinite or floating-point amounts, copied ciphertext, foreign account IDs, stale sessions,
replayed/denied callbacks and stale refreshes. They also cover distributed account claims,
version/account quotas, missing database tenant context and secret-bearing response errors.

Two test defects were corrected: fixed synthetic OAuth state strings collided across
repeat runs in the retained database, and the Content-Type assertion initially omitted the
valid UTF-8 parameter. Test states now have independent session prefixes; the assertion
checks the MIME type. The production uniqueness, expiry and transport rules were retained.
No existing database or test assertion was reset to hide either failure.

The browser contract server lives under `api/tests`, requires the three disposable flags,
checks the exact local database/role and is never imported by the normal application.
Only the provider interaction is synthetic; the app, identity and storage are real. The
browser intercepts the provider consent redirect, so it never reaches Alpaca. This proves
integration with the documented contract, not compatibility with an activated provider app.

## Review and remaining gates

Security/behavior review covered input → consent → one-use state → backend exchange →
verified account → encrypted publication → owned reads → revision-fenced disconnect. The
scope grants trading authority at the provider even though this application's connection
module has no order method; compromise of the API/key/DB combination could expose that
grant. Production isolation and outbound policy remain required. RLS does not protect
against a fully compromised API impersonating its own tenant context.

Simplification reused Authlib/Fernet, the existing bounded transport and tenant transactions;
no broker SDK, background queue, fake production fallback or public plugin loader was added.
No new dependency version was needed. There is no unresolved HIGH/BLOCKER finding for the
loopback connection slice. Provider activation, encryption rotation/recovery, complete
identity/admin lifecycle, account deletion/retention, production egress and capacity remain
public-launch blockers. Engineering limits of three account claims and five pending consent
attempts per customer are not measured public capacity. Disconnected claims still consume
quota; claim release needs a reviewed deletion/support policy.

Implementation still needed: non-AI approval, account-wide risk, external signals, workers,
current-data entitlement/collection, durable paper execution/reconciliation, reporting,
exports/deletion, emergency operations, backup/restore and the requested load benchmarks.

Official sources inspected September 14:
[Alpaca OAuth](https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api),
[paper account endpoint](https://docs.alpaca.markets/us/reference/getaccount-1),
[application registration/review](https://docs.alpaca.markets/us/docs/registering-your-app).
The documented flow does not establish PKCE, refresh-token or revocation endpoints; none
was invented. App registration/review and market-data rights remain externally unverified.
