# Public workspace development

This slice provides verified OIDC sign-in, revocable server sessions and user-owned immutable
strategy configurations in a React application. It is **not the completed paper-trading
product**: no brokerage connection, approval, deployment, execution or performance endpoint
is exposed. The [active plan](../../docs/execution-plans/multi-user-paper-platform.md) tracks
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
been validated for public launch. No brokerage token or order has been used in these tests.
