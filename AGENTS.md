# Repository Mission

Autonomous Quant Agent is a self-hosted quantitative-research and paper-simulation
repository. Preserve deterministic evidence, explicit financial safety boundaries,
restart-safe state, and truthful implementation status. Real-money execution is outside
the supported system.

# Sources of Truth

- Product requirements: `docs/requirements.md`
- Implemented and target architecture: `ARCHITECTURE.md`
- Active execution plan: `docs/execution-plans/platform-core.md`
- Plan-writing standard: `docs/execution-plans/PLANS.md`
- Engineering principles: `docs/engineering-principles.md`
- Coding standards: `docs/coding-standards.md`
- Testing strategy: `docs/testing-strategy.md`
- Security and trust boundaries: `docs/security-model.md`
- Review rules: `docs/code-review.md`
- Tool versions and commands: `docs/tooling.md`
- Legacy prototype details: `docs/architecture.md`, `docs/data_dictionary.md`
- Collector operations: `docs/market_data_runbook.md`

Use only these implementation-status labels:
`IMPLEMENTED_AND_VERIFIED`, `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`,
`PARTIALLY_IMPLEMENTED`, `NOT_IMPLEMENTED`, `BLOCKED`, and
`INTENTIONALLY_DEFERRED`.

# Repository Map

- `src/adaptive_trader/`: installed Python package.
- `src/adaptive_trader/collection/`: isolated Alpaca data collector and PostgreSQL boundary.
- `src/adaptive_trader/strategies/`: legacy long-only strategy implementations.
- `tests/`: deterministic unit, component, regression, safety, and integration tests.
- `tests/integration/`: caller-authorized disposable PostgreSQL tests.
- `tests/safety/`: static repository and paper-only boundary checks.
- `configs/`: tracked non-secret legacy runtime profiles.
- `migrations/`: Alembic history for the collector-owned `market_data` schema.
- `docs/`: design, operating, evidence, and engineering documentation.
- `scripts/`: bounded operator workflows; inspect a script before running it.
- `app.py`: current read-only Streamlit view over legacy SQLite state.
- `runtime/`, `outputs/`, `data/cache/`, and `data/raw/`: ignored local or generated state;
  reviewed synthetic fixtures elsewhere under `data/` may be tracked.

# Canonical Commands

| Purpose | Exact command |
| --- | --- |
| Locked setup | `uv sync --locked --extra dev --extra dashboard` |
| Lock check | `uv lock --check` |
| Format files | `uv run --no-sync ruff format .` |
| Format check | `uv run --no-sync ruff format --check .` |
| Lint | `uv run --no-sync ruff check .` |
| Type check | `uv run --no-sync mypy src` |
| Unit/component offline tests | `uv run --no-sync pytest -q -m "not integration"` |
| Offline suite (PostgreSQL skips unless guarded below) | `uv run --no-sync pytest -q` |
| PostgreSQL integration | `APA_TEST_POSTGRES_URL=postgresql+psycopg://collector_test:collector_test@127.0.0.1:5432/collector_test APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES uv run --no-sync pytest -q -m postgres` |
| Security checks | `uv run --no-sync pytest -q tests/safety tests/test_config_safety.py tests/test_live_safety_matrix.py tests/test_collection_credentials.py tests/test_collection_runtime.py` |
| Synthetic regression | `uv run --no-sync python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic` |
| Replay regression | `uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml` |
| Compose validation | `docker compose --env-file .env.example -f docker-compose.yml config --quiet` |
| Application image | `docker build --target application --tag adaptive-portfolio-agent:validation .` |
| Collector image | `docker build --target market-data --tag adaptive-market-data:validation .` |
| Diff hygiene | `git diff --check` |

PostgreSQL integration is destructive and may run only against the loopback database named
`collector_test`; `docs/testing-strategy.md` owns the guard. CI installs the locked environment,
runs format-check/lint/type-check, migrates PostgreSQL, runs pytest and both legacy regressions,
validates Compose, builds both images, and tests the collector image with networking disabled. A
one-command local full harness and benchmark command are `NOT_IMPLEMENTED`; do not invent them.

# Required Workflow

1. Inspect `git status`, the relevant source, neighboring tests, and applicable docs.
2. State the invariant and trust boundary affected by the change.
3. Update `docs/execution-plans/platform-core.md` for nontrivial work.
4. Implement the smallest coherent behavior; preserve legacy compatibility deliberately.
5. Add positive, negative, boundary, and recovery tests proportionate to risk.
6. Run the narrowest useful test selection, then affected broader checks.
7. Review new dependencies, migrations, configuration, and persistent side effects.
8. Inspect the complete tracked/staged diff and every proposed untracked file; verify generated,
   ignored, credential, database, and private state is absent without opening unrelated contents.
9. Perform the security, simplification, and adversarial reviews in `docs/code-review.md`.
10. Update requirements, architecture, operations, and status evidence together.
11. Follow the commit and publication authority in the active plan; absent scoped delegation, do
    not create a commit, push, or remote object without maintainer approval.
12. Verify every applicable Definition of Done item before commit or publication.

# Architecture Boundaries

- The current collector and legacy application are separate systems sharing a package name.
- `adaptive_trader.collection` may use data credentials and its `market_data` PostgreSQL schema.
- Collector code must not import legacy broker, execution, paper-account, or model code.
- Strategy code proposes values; it must not receive credentials or call a broker.
- Risk and execution authorization remain independent from strategy output.
- The dashboard is read-only; the target private API boundary is not yet implemented.
- SQLite remains legacy/offline state. PostgreSQL is the collector's operational state.
- External I/O belongs at explicit provider, database, CLI, or broker boundaries.
- Collection membership never grants research or execution authority.

# Testing Rules

- Ordinary tests require no Alpaca credentials and may not use external networking. The current
  fixture guards `socket.create_connection` and `socket.socket.connect`; a process-wide socket
  denial wrapper remains target work.
- The `postgres` marker permits loopback only and requires an explicitly disposable database.
- Never point integration tests at a shared, hosted, or operator database.
- Use injected clocks/fakes for time, retry, disconnect, and restart behavior.
- Assert durable state and domain outcomes, not only calls to mocks.
- Preserve deterministic replay and anti-look-ahead behavior.
- Do not skip, weaken, delete, or reorder tests to hide a defect.
- See `docs/testing-strategy.md` before introducing a new test category.

# Security Rules

- Never read, print, stage, or commit `.env`, credential, token, key, database, or raw-data files.
- Never introduce real-money endpoints, `paper=False`, or configurable trading hosts.
- Keep the standalone collector data authority separate from legacy paper authority; new target
  services require distinct credential namespaces, processes, images, and mounts. The legacy
  Alpaca market-data path still shares `PaperCredentials`, as documented in the security model.
- Validate provider payloads before persistence and authorization inputs before side effects.
- Persist order intent before any broker submission; ambiguity blocks retry and new exposure.
- Preserve append-only evidence, fencing tokens, idempotency keys, and fail-closed behavior.
- Use parameterized SQL and redact connection URLs and secret-bearing errors.
- No ordinary verification command may contact Alpaca or submit an order.
- Follow `SECURITY.md` and `docs/security-model.md` for reporting and residual risks.

# Dependency Rules

- A production dependency needs a current caller and a documented boundary or capability.
- Prefer the standard library or an existing locked package when it is clear and sufficient.
- Keep runtime, optional dashboard, developer, and collector-only dependency sets distinct.
- Change `pyproject.toml` and `uv.lock` together through `uv`; never hand-edit lock resolution.
- Run the applicable tests and dependency review described in `docs/dependency-policy.md`.

# Definition of Done

- Behavior and limitations match `docs/requirements.md` and the active plan.
- Formatting, linting, typing, affected tests, and `git diff --check` pass.
- The full offline suite and both legacy regressions pass for cross-cutting changes.
- PostgreSQL migrations and integration tests pass for collector storage changes.
- Security boundaries and credential-free/no-network defaults remain tested.
- Documentation describes what is implemented without upgrading unverified status.
- The diff contains no secrets, local data, caches, prompt material, or unrelated edits.
- No unresolved `BLOCKER` or `HIGH` review finding remains.

# Code Review Rules

Prioritize unauthorized financial effects, data corruption, replay/idempotency failure,
look-ahead, stale-data acceptance, credential leakage, migration risk, and misleading evidence.
Then review maintainability, tests, performance, and documentation. Findings must include a
reproducible failure scenario and the smallest safe correction. See `docs/code-review.md`.

# Instruction Maintenance

When a repeated defect, durable constraint, or reviewed decision changes how work is performed,
update the nearest authority in the same change. Add nested instructions only for a subtree with
materially different boundaries or commands.
Read and follow every newly created instruction file during the current run because it may not
activate automatically in an already-running process.
