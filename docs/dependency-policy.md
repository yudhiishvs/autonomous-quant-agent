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
| `dashboard` extra | Streamlit presentation only |
| `legacy-yahoo` extra | Explicit compatibility data source only |
| `dev` extra | Ruff, mypy, pytest, async tests, and coverage |
| `market-data-runtime` group | Minimal collector runtime: Alembic, exchange calendar, psycopg, requests, SQLAlchemy, Typer, and websockets |

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
uv sync --locked --extra dev --extra dashboard
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

Automated dependency scanning and a documented advisory exception process are currently
`NOT_IMPLEMENTED` in the checked-in CI. Until they exist, no vulnerability-free claim is
permitted; manual review does not substitute for an executed scanner.

Dependency updates should be grouped by purpose. Security corrections may be isolated for
fast review, but must not silently change application behavior or bypass the locked graph.
