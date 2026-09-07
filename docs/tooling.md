# Tooling

Python 3.11+, setuptools, locked uv dependencies, Ruff, mypy, pytest/coverage, SQLAlchemy,
psycopg/PostgreSQL, Alembic, Typer, Streamlit/FastAPI, Docker/Compose and GitHub Actions form the
inspected stack. `pyproject.toml` and `uv.lock` own dependency constraints/resolution. CI and
containers pin their tool/image versions; no second formatter, type checker or runtime language
is required.

## Canonical command surface

The Makefile is the current harness, including bootstrap, offline checks, guarded integration,
security, package smoke, demo, benchmark, coverage and Compose validation. It is no longer a
pip-based legacy install wrapper.

| Purpose | Command |
| --- | --- |
| Locked setup | `make install` |
| Lock consistency | `uv lock --check` |
| Format non-AI files | `make format` |
| Format and lint checks | `make lint` |
| Type check source and container helpers | `make typecheck` |
| Network-denied offline tests | `make test` |
| Network-denied unit/component tests | `make unit` |
| Guarded PostgreSQL integration | `make integration` |
| Coverage and thresholds | `make coverage` |
| Legacy synthetic/replay regressions | `make regression` |
| Protected AI verification | `make freeze` |
| Complete local harness | `make check` |
| Deterministic fixture demo | `make demo` |
| Deterministic pipeline benchmark | `make benchmark` |
| Security suite/scans/hooks | `make security` |
| Package build / clean installation smoke | `make package` / `make package-smoke` |
| Compose validation | `make compose-config` |
| Local infrastructure-secret bootstrap | `uv run --no-sync aqa secrets bootstrap-local` |
| Diff hygiene | `git diff --check` |

`make check` requires a deliberately disposable PostgreSQL 16 cluster, its guarded loopback URL,
Docker Compose, locked dependencies and scanner/advisory access. Read
[testing strategy](testing-strategy.md) for every PostgreSQL guard, including the cluster-reset
acknowledgement; setting a database URL alone is not authorization. Never point tests at an
operator/shared/hosted database. A prerequisite failure is a failed/unexecuted check, not a skip
that proves acceptance. No external Alpaca credentials are needed for ordinary verification.

`make format` uses `scripts/format_non_ai.py` to preserve the protected AI manifest. Never fix
formatting by modifying frozen files. The full formatter check still reports actual issues.
Migrations use a separate owner credential; long-running roles never acquire DDL authority.

## Security and automation

The security target uses the locked security dependency group, Bandit, architecture/safety tests,
pre-commit hooks and hash-pinned dependency auditing. `.pre-commit-config.yaml`, `.secrets.baseline`,
Dependabot, CodeQL, security/container/SBOM workflows and package checks exist. Advisory downloads
and GitHub-hosted scanners are external tooling boundaries; recorded execution determines their
result. Checked-in definitions alone are not passing scans or successful remote workflow runs.

`.github/workflows/ci.yml` coordinates source, migration, offline and container verification.
The separate scheduled market-data workflow requires an explicit repository activation variable
and dedicated data secrets, runs `aqa data collect-once`, and uses durable PostgreSQL checkpoints.
It is intentionally inactive without operator configuration and is not an ordinary test command.
See [scheduled collection](scheduled_market_data.md) for activation and scheduling limitations.

## Current verification boundary

The current baseline recorded 2,015 passing offline tests and one failure; that non-AI failure
received a correction with 49 focused tests passing. Dataset causality checks passed 62 tests.
The initial fresh PostgreSQL attempt was refused by the missing disposable-cluster guard.
Final full-check, PostgreSQL, container, scanner and package results remain pending in the
[active execution plan](execution-plans/platform-core.md). No fresh success is inferred from older
runs, installed tools or valid configuration.


The canonical `make coverage` first requires the disposable PostgreSQL preflight, then
runs the socket-denied offline suite and appends the guarded PostgreSQL suite to the
same branch data. `make coverage-report` enforces the unchanged 74 percent repository
and 85 percent platform floors and writes XML/HTML evidence. `make coverage-offline`
collects offline evidence alone; `make coverage-postgres` appends guarded database
evidence. Neither partial collection establishes the combined gate. `make check`
executes PostgreSQL through coverage once. CI retains separate offline and PostgreSQL
jobs, uploads their actual hidden `.coverage` files, and requires both successful jobs
and both artifacts before combining data in the dependent coverage gate. Missing
PostgreSQL prerequisites or coverage artifacts fail verification; they are not skips.
