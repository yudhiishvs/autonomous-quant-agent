# Dependency Policy

## Admission rule

A production dependency needs a current caller and must provide a capability not clearly
and safely covered by the standard library or an existing locked package. Before addition,
record purpose, rejected alternatives, maintenance activity, license compatibility,
published vulnerabilities, transitive and binary cost, supported Python versions, version
constraint, deployment effect, and a plausible removal path.

Do not add packages for trivial convenience, a future architecture, duplicate formatting or
testing, or résumé breadth. Do not vendor source without its license and update plan.

## Current dependency boundaries

| Set | Purpose |
| --- | --- |
| Core project dependencies | Legacy research/application runtime, SQLAlchemy persistence, configuration, reporting, and guarded paper adapter |
| `dashboard` extra | Streamlit presentation plus a fixed GitPython security floor for its transitive dependency |
| `legacy-yahoo` extra | Explicit compatibility data source only |
| `dev` extra | Ruff, mypy, pytest, async tests, and coverage |
| `market-data-runtime` group | Data-only collector runtime, including its normalization, Parquet, configuration, database, HTTP, and WebSocket dependencies |
| `security` group | Pre-commit, secret scanning, Bandit, pip-audit, and CycloneDX generation |

`alpaca-py` remains a legacy application dependency. The collector uses fixed-host
`requests`/`websockets` transports and its image installs only `market-data-runtime`, so it
does not contain the trading SDK.

## Versions and locking

`pyproject.toml` declares supported ranges and `uv.lock` fixes the resolved graph. CI and
images use locked synchronization. At the current lock, representative versions include uv
0.11.7 in CI/images, Alembic 1.19.1, SQLAlchemy 2.0.51, psycopg 3.3.5, requests 2.34.2,
websockets 17.0.1, exchange-calendars 4.13.2, Ruff 0.16.2, mypy 2.3.0, pytest 9.1.1,
and pytest-cov 7.1.0.

Change metadata and the lock together with `uv`; never hand-edit resolved packages. Verify:

```bash
uv lock --check
uv sync --locked --all-extras
```

A collector dependency change also verifies:

```bash
uv sync --locked --only-group market-data-runtime --no-install-project
docker build --target market-data --tag adaptive-market-data:validation .
```

## Review procedure

1. Search the existing graph and standard library for the capability.
2. Verify the license and primary upstream release/security information.
3. Add the narrowest runtime, optional, or development declaration.
4. Regenerate `uv.lock` through `uv` and inspect direct and transitive changes.
5. Run affected unit/integration tests, full typing/linting, and image smoke checks.
6. Run the configured vulnerability scan and document unresolved advisories with scope,
   exploitability, owner, and removal/upgrade plan.
7. Remove the package if its caller is removed.

The checked-in security workflow installs `pip-audit` and Bandit from the locked `security`
dependency group. The container workflow scans both runtime image targets with Trivy and emits
CycloneDX/SPDX SBOM artifacts. Dependabot proposes bounded uv, GitHub Actions, and Docker updates;
it does not bypass review or update the lock outside a pull request. A configured scan is not a
claim that a future run will find no vulnerabilities.

The blocking Bandit gate covers all runtime Python under `src` plus the container support code.
Untrusted XML is parsed with `defusedxml`. The reviewed `B608` annotations are limited to SQLite
queries whose variable text is either placeholder arity produced solely from `?` characters or a
table name selected from a closed literal tuple; all values remain separately bound. The platform
`B506` annotation is similarly narrow: the custom YAML loader subclasses `SafeLoader`, and a
bounded parser-event pass rejects aliases and excess structure before construction. The runtime
`B104` annotation is a closed host allowlist for container servers whose publication is controlled
by Compose; its `B108` annotation uses an owner-private `0700` directory and no-follow descriptor
operations for local health markers. The container annotations are also bounded: `B108` covers a
fixed file beneath a private Compose tmpfs with a `0700` parent and exclusive no-follow creation;
`B104` covers the debug profile's in-container listener, whose sole host publication is fixed to
`127.0.0.1` and whose network is internal.

An advisory exception requires a reviewed, time-bounded repository change containing the advisory
ID, exact affected component, deployment exploitability, compensating control, owner, upstream
tracking link, and expiration date. Expired or undocumented ignores are prohibited. There are no
current checked-in exceptions.

Dependency updates should be grouped by purpose. Security corrections may be isolated for
fast review, but must not silently change application behavior or bypass the locked graph.

While the Main AI dependency freeze is active, routine uv version-update PRs are paused
with `open-pull-requests-limit: 0`. Security updates and the blocking locked dependency
audit remain active; a vulnerable frozen dependency requires a separately reviewed freeze
exception, never a rewritten baseline merely to pass CI. Existing routine update PRs do
not become valid by bypassing the freeze check. Restore the routine limit when the
maintainer explicitly ends the freeze. Docker updates retain Python 3.11 and may update
its patch releases and image digests; moving to another Python minor requires a planned
compatibility migration across local tools, CI, images, and dependencies.
