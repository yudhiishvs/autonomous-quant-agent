# Tooling

## Detected stack

- Python package under `src/adaptive_trader`, supporting Python 3.11+.
- Setuptools build metadata, `uv` dependency resolution, and checked-in `uv.lock`.
- Ruff formatting/linting, mypy type checking, pytest/pytest-cov tests.
- SQLAlchemy 2, SQLite legacy state, psycopg/PostgreSQL collector state, Alembic migrations.
- Typer CLIs, Streamlit dashboard, Docker multi-stage images, Docker Compose, GitHub Actions.

CI and Docker pin uv 0.11.7 and Python 3.11. The lock currently resolves Ruff 0.16.2,
mypy 2.3.0, pytest 9.1.1, pytest-cov 7.1.0, Alembic 1.19.1, SQLAlchemy 2.0.51, and
psycopg 3.3.5. `pyproject.toml` is authoritative for constraints; `uv.lock` is authoritative
for the complete resolution.

## Canonical local commands

These locked commands are authoritative. The current `Makefile` is a legacy convenience surface:
its `install` target uses pip and it does not yet provide the complete harness required by
`REQ-HARNESS-001`.

| Purpose | Command |
| --- | --- |
| Locked setup | `uv sync --locked --extra dev --extra dashboard` |
| Lock consistency | `uv lock --check` |
| Format files | `uv run --no-sync ruff format .` |
| Format check | `uv run --no-sync ruff format --check .` |
| Lint | `uv run --no-sync ruff check .` |
| Type check | `uv run --no-sync mypy src` |
| Offline tests | `uv run --no-sync pytest -q` |
| Branch coverage | `uv run --no-sync pytest --cov=adaptive_trader --cov-branch --cov-report=term-missing --cov-report=xml` |
| Synthetic regression | `uv run --no-sync python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic` |
| Replay regression | `uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml` |
| Compose validation | `docker compose --env-file .env.example -f docker-compose.yml config --quiet` |
| Application image | `docker build --target application --tag adaptive-portfolio-agent:validation .` |
| Collector image | `docker build --target market-data --tag adaptive-market-data:validation .` |
| Diff hygiene | `git diff --check` |

PostgreSQL integration requires the guarded loopback environment in
`testing-strategy.md`. Collector migrations use the separate
`APA_MARKET_DATA_MIGRATION_DATABASE_URL`; do not inject that URL into the long-running
collector.

## CI correspondence

`.github/workflows/ci.yml` runs Python 3.11 on Ubuntu 24.04 with empty Alpaca variables and
paper submission disabled. It installs the locked environment, runs format/lint/mypy,
migrates PostgreSQL, runs pytest and both legacy regressions, validates Compose, builds both
images, and starts collector smoke checks with Docker networking disabled. Actions and base
images are pinned to immutable revisions/digests.

Local commands and CI should use the same arguments. A convenience target may compose these
commands, but it must not replace or weaken them.

## Tool selection

- Ruff is the sole formatter/primary linter; do not add a competing formatter.
- Mypy is the sole type checker. Its current configured language level is 3.14 because of a
  documented installed NumPy-stub parsing constraint, while runtime compatibility and CI
  remain Python 3.11. Resolving that mismatch without suppression is planned work.
- Pytest is the sole test runner. Use plugins only for distinct needs such as coverage or
  async behavior.
- Alembic owns PostgreSQL schema evolution; `metadata.create_all` is not an operational
  migration mechanism.
- Docker validates the actual image boundary; Compose config validation must not start a
  broker-connected service.

## Tools not yet selected

The following tools are intentionally deferred, not silently treated as unnecessary:

- Secret scanning, dependency vulnerability analysis, CodeQL or equivalent static security
  analysis, container scanning, and SBOM generation remain `NOT_IMPLEMENTED` until Phase 8 can
  add pinned CI implementations, failure policy, and a documented advisory exception process as
  one verified delivery boundary. Selecting overlapping scanners earlier would create output with
  no established owner or triage path.
- A project pre-commit configuration remains `NOT_IMPLEMENTED` until the canonical fast command
  surface is complete. CI already enforces current required checks; a hook must call the same
  commands rather than add editor-only correctness.
- The deterministic benchmark runner remains `NOT_IMPLEMENTED` until the platform operations it
  must measure exist in Phase 9. Adding a runner now would benchmark only legacy paths and could
  not satisfy the required workload contract.

The local Docker client is installed, but the daemon was unavailable during Phase 0, so fresh
local image and PostgreSQL 16 runtime validation remain unavailable. GitHub Actions currently
verifies the existing PostgreSQL 15 and image boundaries. These are environment limitations, not
reasons to select a different build or database tool.

Add the smallest nonredundant tool set through the dependency and review policies; never invent
an action revision, image digest, scan result, or vulnerability-free claim.

No additional runtime language, message broker, orchestration platform, or cloud resource is
justified by the current implementation. Essential verification must remain available from
the command line and CI rather than depending on an editor extension.
