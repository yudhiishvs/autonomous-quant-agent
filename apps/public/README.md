# Public workspace development

This slice provides verified OIDC sign-in, revocable server sessions and user-owned immutable
strategy configurations in a React application. It is **not the completed paper-trading
product**. Alpaca paper OAuth/account connections are implemented with offline contract
verification and require a separately registered provider application. Approval, deployment,
execution and performance are not implemented. The [active plan](../../docs/execution-plans/multi-user-paper-platform.md) tracks
the remaining release work. Do not expose this development stack publicly.

## Local startup

Prerequisites: Python 3.11, uv, Node 22.12+ and Docker Compose. Start in the repository root.
Root dependencies and its frozen lock remain separate from this application's lock.

```sh
uv sync --project apps/public/api --locked --python 3.11
npm ci --prefix apps/public/ui
apps/public/api/.venv/bin/python apps/public/local.py
docker compose -f apps/public/compose.yml up -d
```

Bootstrap refuses to overwrite an existing `.local` directory. It creates private files for
the disposable database and identity provider; values are not printed. Wait for PostgreSQL
to become healthy and Keycloak to finish importing the realm, then:

```sh
python3 apps/public/run.py migrate
python3 apps/public/run.py api
```

In a second terminal, run `npm run dev --prefix apps/public/ui`. Open
[the workspace](http://127.0.0.1:5178). Register through the identity provider and open the
verification message in [the local mailbox](http://127.0.0.1:8128). All messages remain in
the disposable mailbox; no email is delivered externally. Complete registration, sign in,
and save a strategy version. Saving cannot place an order.

`run.py` uses explicit file paths, root shared-module imports, a loopback listener, and
disabled access logging so callback codes do not enter URL logs. Its migration mode selects
separate migration credentials. The API refuses a superuser, a bypass-RLS role, or an
unmigrated schema. Do not run ordinary Alembic commands against an operator database.

## Verification

```sh
make -C apps/public check
make -C apps/public audit
.venv/bin/python scripts/verify_no_network.py pytest -q tests/public_product
.venv/bin/python scripts/verify_main_ai_freeze.py
```

API offline tests deny networking and skip the explicit PostgreSQL selection. The opt-in
tests write synthetic records only to this stack's `collector_test` database on loopback
port 55438. Before enabling them, verify the container is the disposable one:

```sh
docker inspect aqa-public-disposable-postgres-1 --format '{{index .Config.Labels "aqa.disposable"}} {{json .NetworkSettings.Ports}}'
PYTHONPATH=src APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES AQA_PUBLIC_DISPOSABLE_TESTS=YES apps/public/api/.venv/bin/python -m pytest -q apps/public/api/tests
```

Expected label: `public-product-development`; only host binding `127.0.0.1:55438`. The
test fixture rejects any other database/role/endpoint. Never reuse these acknowledgements
for shared infrastructure. Tests deliberately leave synthetic records for inspection.

For browser tests with the API/UI running:

```sh
PYTHONPATH=src apps/public/api/.venv/bin/python apps/public/seed_users.py
cd apps/public/ui
npx playwright install chromium
npm run test:e2e
```

The seed command creates `fixture-one@example.invalid` and `fixture-two@example.invalid`
in the local realm only; their deliberately public test password is
`SYNTHETIC-LOCAL-PASSWORD-ONLY`. It never changes existing accounts. Browser tests use
independent contexts, real OIDC and real storage. They cover saved-state persistence,
cross-user lookups, CSRF, sign-out, keyboard focus, overflow and automated accessibility
at 390/768/1440 pixels. They are not human usability testing or a production load benchmark.

Stop the API/UI terminal processes and use `docker compose -f apps/public/compose.yml down`
when finished. Volumes and private `.local` files are retained; no operator volumes are
part of this project. Do not commit `.local`, browser traces, screenshots or database files.

## Security and remaining work

Authlib implements OAuth code exchange/PKCE; joserfc verifies RS256 signatures; Authlib's
OIDC claims validation checks issuer, audience, nonce and expiry. Only verified email
identities become application customers, keyed by issuer and subject. Email alone never
links identities. Keycloak access tokens stay encrypted in PostgreSQL with Fernet; keys
are separate owner-private files. Opaque session verifiers and CSRF verifiers are hashed.
The session cookie is HttpOnly; the CSRF nonce is intentionally browser-readable and
cannot authenticate a caller. No privileged material is placed in local/session storage.

The real provider is introspected on each authenticated request. An outage fails closed;
local logout still deletes the session if provider revocation fails. Keycloak's issuer
must include this client in the access-token audience, as configured by the bootstrap
mapper. Sessions last at most 15 minutes; refresh is not implemented. Sign-in explicitly
requires authentication again, even if the provider retains an SSO session.

Mutations check the exact configured Origin and session-bound CSRF token. Bodies, provider
responses, socket deadlines, SQL statements, pools and per-user version counts are bounded.
SQL queries derive ownership from the server session, and forced RLS protects version
rows even when a query omits its tenant predicate. The runtime role cannot update/delete
saved versions. Saves use owner-scoped request IDs: retrying the same request returns the
original version, while reusing its ID for different content is rejected. Identity/session tables are shared service tables, not RLS-protected;
runtime database compromise remains a cross-user threat requiring further hardening.

This is a local development slice, not a production deployment recipe. Remaining gates
include public registration abuse controls, privileged MFA enforcement, audited administration,
rotation/recovery of encryption keys, full recovery/deletion lifecycle verification,
production proxy/HTTPS/container configuration, SMTP, operational
monitoring, backups/restore and measured capacity. No hosting or market-data rights have
been validated for public launch. No real brokerage token or order has been used in these tests.


## Paper-account connection configuration

The normal application has no synthetic provider fallback. Without all three settings below,
it reports that Alpaca connections are unavailable; partial configuration fails startup:

- `AQA_PUBLIC_ALPACA_CLIENT_ID`: registered OAuth application ID.
- `AQA_PUBLIC_ALPACA_CLIENT_SECRET_FILE`: owner-private file containing that application's secret.
- `AQA_PUBLIC_BROKER_ENCRYPTION_KEY_FILE`: a separate owner-private Fernet key (32 random bytes,
  URL-safe base64 encoded). Do not reuse the identity encryption key.

The registered callback must match `AQA_PUBLIC_ORIGIN` plus `/broker/alpaca/callback`.
These are activation requirements, not values supplied by the local fixture. Collector API
keys are not OAuth application credentials. Actual provider validation has not occurred.
The API only exchanges consent and reads `/v2/account` on the fixed paper host. It has no
order-submission route. Account snapshots preserve exact decimal strings and record the
last verification time; they are not strategy performance or current execution permission.

Disconnect removes local access and fences pending consent. It does not revoke Alpaca's
provider grant or cancel open orders. Use Alpaca to manage those separately. The same
broker account cannot be claimed by another customer, even after disconnect. Three account
claims and five pending consent attempts are development bounds; deletion/claim release
and capacity policy remain unfinished.

For the additional **synthetic brokerage** browser contract, first stop the normal API
terminal (keep the UI and disposable Compose stack running). Then run:

```sh
PYTHONPATH=src APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES AQA_PUBLIC_DISPOSABLE_TESTS=YES apps/public/api/.venv/bin/python apps/public/api/tests/browser_app.py
```

In another terminal, run `npm run test:broker --prefix apps/public/ui`. The test server checks
the exact disposable DB/role and uses real local identity/storage with injected synthetic
Alpaca responses. The browser intercepts the consent redirect; no call reaches Alpaca.
Stop this test server afterward and restart `python3 apps/public/run.py api` for the normal
application. Never expose the test server. CI exercises both normal and contract modes.
See [connection evidence](../../docs/evidence/public-paper-connections-20260914.md).

## Explicit strategy approval

Select a saved version to configure USD risk ceilings, select an owned connected paper
account, review the server's summary and confirm it explicitly. The review lasts five
minutes; the approval expires 1, 7 or 30 days after the review was created. Confirmation
checks paper-account identity, connection generation and equity again. It signs the exact
owner/account/version/limits/expiry binding. A newer approval for the same account/version
revokes the previous one. Refreshing a balance does not invalidate approval; reconnecting
the account does. Approvals are configuration records: **execution remains unavailable**.
The 100-record development history limit includes drafts, expired and revoked approvals.
Loss and exposure ceilings are inputs for the remaining independent risk implementation,
not a guarantee of bounded losses.

`AQA_PUBLIC_APPROVAL_SIGNING_KEY_FILE` must name a separate owner-private file containing
32 random Ed25519 private-key bytes in URL-safe base64. The fresh disposable bootstrap
creates this file for local development. Existing local fixtures can add only the missing
key without reading or overwriting any other configuration:

```sh
apps/public/api/.venv/bin/python - <<'PY'
import base64
import os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

key = base64.urlsafe_b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
fd = os.open("apps/public/.local/approval_signing_key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(key)
print("Local approval key created privately.")
PY
python3 apps/public/run.py migrate
```

Restart the local API after migration. Missing signing configuration disables creation and
confirmation; it still permits revocation. Replacing the key makes existing signatures
unverifiable and requires fresh approval. Production key custody, backup and rotation remain
release gates. Do not rotate the key casually or reuse an encryption key for signing.
The normal application has no synthetic signing/provider fallback.
