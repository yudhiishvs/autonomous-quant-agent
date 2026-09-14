# Public workspace verification — September 14, 2026

Status: `PARTIALLY_IMPLEMENTED` for the public product. This report covers the first
identity/configuration slice, not brokerage operation or public-launch readiness.

## Implemented boundary

`apps/public` contains a functioning React/TypeScript application and separate locked
Python API. Verified Keycloak issuer/subject identities receive opaque server sessions.
Users can create, inspect and reload owned immutable strategy versions. Names and the
bounded declarative definition are validated server-side. Saving grants no execution
authority; the UI and API explicitly report that execution is unavailable.

The additive `aqa_public` schema has its own migration history and restricted runtime
login. Existing `market_data`/private tables are untouched; no legacy records are assigned
to customers. `public_0002` adds request IDs to existing version records and enforces
owner-scoped uniqueness. A retried save returns the original row; conflicting content
returns 409. Customer row locks serialize quota checks and insertion across processes.

The development Compose project publishes only loopback ports, uses synthetic accounts
and local Mailpit email, and has no Alpaca configuration. Private bootstrap files remain
ignored with mode 0600 in a 0700 directory. PostgreSQL initialization copies the private
SQL into its container with postgres ownership instead of widening host permissions. A networkless, read-only
initialization container similarly copies the realm into a private volume owned by
Keycloak UID 1000; it exits before identity starts.
Keycloak's development database and bootstrap administrator are not production settings.

## Executed checks

Host: local macOS/Apple Silicon; PostgreSQL 16 in the separately labelled
`aqa-public-disposable` project, database `collector_test`, port 55438. Root commands run
from the repository root. PostgreSQL tests require all three acknowledgements in the
[application README](../../apps/public/README.md); ordinary checks skip them.

| Command / method | Actual result |
| --- | --- |
| `.venv/bin/python scripts/verify_no_network.py pytest -q` | 3,135 passed, 10 PostgreSQL skips, 2 warnings, 186.96s after the exact secret namespace assertions were updated |
| `.venv/bin/python scripts/verify_main_ai_freeze.py` | 61 protected files, inventory and dependency hashes pass |
| `UV_CACHE_DIR=/private/tmp/aqa-uv-cache make typecheck` | 157 root source files passed |
| Both legacy CLI regressions under `scripts/verify_no_network.py` | Synthetic backtest and replay exited 0; behavior not developed or exposed |
| `make -C apps/public check` | API Ruff/format/mypy, 14 offline identity tests (6 guarded PG skips), UI Prettier/TypeScript/Vite build passed |
| Opt-in API PostgreSQL suite | 20 passed, 2 dependency deprecation warnings, 1.58s |
| `PLAYWRIGHT_BROWSERS_PATH=/private/tmp/aqa-playwright npm run test:e2e --prefix apps/public/ui` | 5 passed: two independent identities, persistence/ownership/CSRF/logout, lost-response retry, three responsive/a11y widths |
| `make -C apps/public audit` | Locked production Python audit: no known advisories; npm audit: zero vulnerabilities at execution time |
| `uv build --project apps/public/api --out-dir /private/tmp/aqa-public-dist` | Source distribution and wheel built; still requires root shared sources at runtime |
| Root CI workflow/static-security selection | 17 passed |

The original root baseline was 3,065 passed, 10 skipped. An intermediate run caught a
secret inventory expectation missing the new names; the exact expectation was extended,
not bypassed. A negative JWT test initially failed while constructing the deliberately
unsupported token; allowing that algorithm in the test encoder made the test reach the
unchanged restrictive production decoder. Root whole-tree lint/format still reports the
pre-existing unused `src/adaptive_trader/web/schema.py` draft. That draft is excluded from
these commits and absent from the pushed tree.

Playwright used the real local OIDC provider and real PostgreSQL, not a mocked sign-in.
Screenshots at 390, 768 and 1440 pixels were visually inspected; automated WCAG 2/2.1/2.2
AA checks found no violations on those workspace states. A focused skip link originally
overlapped the brand; its placement was corrected and retested. No human usability study,
screen-reader audit, complete browser matrix or production load test was performed.
Registration reached email delivery and verification in a separate browser, but completed
registration and password recovery were not verified end-to-end. Seeded verified identities
cover the workspace journey, not the entire account lifecycle.

## Dependency review

The frozen root manifests are unchanged. Separate `apps/public/api/uv.lock` and
`apps/public/ui/package-lock.json` isolate public app dependencies. Authlib 1.8.0 and
joserfc 1.7.5 (BSD-3-Clause) implement OAuth/OIDC and signature validation rather than custom
password/JWT code; cryptography 50.0.1 (Apache-2.0 OR BSD-3-Clause) supplies authenticated
Fernet encryption. Existing architectural choices are reused through FastAPI 0.141.1,
SQLAlchemy 2.0.52, Alembic 1.20.0, requests 2.34.2, Pydantic 2.13.5, PyYAML 6.0.3,
Uvicorn 0.53.0 and psycopg 3.3.5. Metadata licenses are MIT/BSD/Apache except psycopg's
LGPL-3.0-only; retain dependency notices and shared-library obligations in distribution.
These versions support Python 3.11. Binary cryptography/psycopg wheels add native supply
chain and platform costs. Cryptography 49+ no longer provides macOS Intel wheels; this
slice was verified on Apple Silicon, not Intel source builds.

The first audit detected three unique cryptography 48.0.1 advisories; the app-only lock
was upgraded to 50.0.1 and encryption/signature tests rerun. No advisory was suppressed. Pre-commit secret detection flagged the new environment-variable
identifier and the Keycloak password-policy expression; two explanatory inline false-positive
annotations cover those exact non-secret literals, with no file/scanner exclusion.
The application imports existing shared hashing/schema/security modules; PyYAML is needed
by the platform package import. A standalone wheel without those root sources is not a
supported deployment artifact yet. Removing identity support removes Authlib/joserfc;
removing recoverable tokens removes Fernet. React/React DOM provide UI state, TypeScript
and Vite compile it, and Playwright/axe/Prettier remain development-only. No component
library, analytics SDK, queue or hosted service was added.

Local images use observed immutable digests for PostgreSQL 16, Keycloak 26.7.3 and Mailpit
1.31.1. They are development infrastructure, not scanned production images. CI actions
are SHA-pinned, checkout credentials are not persisted, permission is contents:read, and
no deployment or broker order step exists. New CI checks the app independently, including
synthetic services and browser tests. Current image audits and deployment approval remain
separate launch requirements.

Primary references inspected September 14:
[Authlib HTTP client](https://docs.authlib.org/en/stable/oauth2/client/http/index.html),
[Keycloak OIDC endpoints](https://www.keycloak.org/securing-apps/oidc-layers),
[Keycloak releases](https://github.com/keycloak/keycloak/releases),
[cryptography changes](https://cryptography.io/en/latest/changelog/),
[Mailpit releases](https://github.com/axllent/mailpit/releases),
[setup-node v7](https://github.com/actions/setup-node/releases/tag/v7.0.0).
Installed package metadata and the executed audits supplied version/license evidence.

## Review and residual risks

Security/behavior review traced browser input through fixed-origin OAuth, signed claim
validation, one-use browser-bound state, session hashing, provider introspection and
parameterized owner-derived transactions. It covered guessed IDs, missing context,
connection reuse, forbidden row mutation, oversized bodies, malformed configurations,
expired/revoked sessions, IDP outage, concurrent quotas and lost responses. Runtime cannot
submit orders, execute user code, accept imports or choose arbitrary network/file paths.
Secrets enter runtime through the existing file loader; token/error URLs are not logged.
Provider response bytes and deadlines, request bytes, SQL deadlines/pools and query/version
counts are bounded. Encryption keys are separate from the database.

Simplification review retained one modular API, a small UI, maintained identity services
and PostgreSQL concurrency controls. No speculative worker, broker fake fallback, DSL or
custom password implementation is exposed. This atomic browser/backend/schema boundary
exceeds the usual 800-line guideline; splitting schema/API/fixture/UI into incomplete
commits would leave the verified sign-in/save journey unusable. Subsequent broker and
execution behavior will be separately reviewed.

No unresolved HIGH/BLOCKER finding remains for this **loopback-only partial slice**.
Public exposure remains blocked: registration abuse prevention, privileged MFA policy,
retention/cleanup, audited administration, encryption rotation/recovery, full account
recovery/export/deletion, HTTPS proxy/images, monitoring/restore and capacity are incomplete.
Runtime DB compromise can access shared identity/session tables and impersonate tenant
context; forced RLS is defense in depth, not isolation from a compromised API process.
Login IP limits currently use the direct peer; production proxy trust requires explicit
configuration. Provider introspection per request increases latency and outage coupling.
All limits are engineering bounds, not demonstrated public capacity. No benchmark meets
the requested 100 active browsers/500 deployments yet; no public launch limit is justified.

Broker OAuth, account verification, approval/risk, external signals, durable execution,
reconciliation/reporting and operating controls are still required. No public hosting,
market-data rights, broker credentials, paper orders or real-money execution was activated.
