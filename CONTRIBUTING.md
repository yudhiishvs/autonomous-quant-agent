# Contributing

This project accepts focused changes that preserve deterministic research evidence,
paper-only safety, and explicit data ownership. Start with `AGENTS.md`, then read the
requirements, active execution plan, architecture, and tests for the subsystem being
changed.

## Supported development environment

- Python 3.11 or newer; CI exercises Python 3.11.
- `uv` with the checked-in `uv.lock`.
- Docker with Compose for configuration, image, and disposable PostgreSQL checks.
- A POSIX-like shell for the tracked operator scripts.

Install the locked developer and dashboard dependencies:

```bash
uv sync --locked --extra dev --extra dashboard
```

Do not place credentials in a tracked file. Copying `.env.example` for a local external
run is an operator action; ordinary development and tests require no Alpaca credentials.

## Choose a coherent change

Before editing:

1. Inspect `git status` and preserve existing work.
2. Trace the affected entry point, data flow, state owner, and error path.
3. Read neighboring tests and applicable design decisions.
4. Identify the invariant being changed.
5. Update `docs/execution-plans/platform-core.md` when the change crosses modules, changes
   persistent state, adds an external boundary, or affects financial authorization.

Do not mix broad refactoring, dependency changes, schema changes, and feature behavior in
one review unless they are inseparable. Preserve legacy command and import compatibility
unless a documented migration explicitly replaces it.

## Implement and test

Use frozen dataclasses for immutable internal records, strict Pydantic models at untrusted
configuration/API boundaries, timezone-aware UTC values, and `Decimal` at financial
boundaries. Keep network, database, clock, and broker effects behind explicit seams.

Every behavior change needs tests at the lowest level that proves the contract, plus the
relevant integration or regression path. Include negative, malformed, boundary, restart,
and concurrency cases when those failures could corrupt data or authorize an action.

Run at least:

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest -q
git diff --check
```

Cross-cutting legacy changes also run:

```bash
uv run --no-sync python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic
uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml
```

Collector persistence or migration changes require a loopback PostgreSQL database named
`collector_test`; follow `docs/testing-strategy.md`. Never run destructive integration
fixtures against a hosted or shared database.

## Security expectations

- Never commit credentials, tokens, private keys, database URLs with passwords, local
  databases, downloaded market data, or generated runtime output.
- Never add a real-money endpoint, `paper=False`, a configurable trading hostname, or an
  order path reachable from strategy or presentation code.
- Keep collector credentials separate from paper-account credentials in code, runtime
  configuration, containers, and tests.
- Validate external payloads before persistence. Preserve append-only evidence and
  idempotent identities.
- Treat a timeout after a possible external side effect as ambiguous; do not retry until
  authoritative reconciliation resolves it.
- Redact secrets structurally from exceptions, logs, metrics, reports, and test output.

Report a suspected vulnerability through the private process in `SECURITY.md`, not in a
public issue containing exploit details or secret material.

## Dependencies and migrations

A new dependency must solve a current problem not clearly covered by the standard library
or an existing package. Document its purpose, maintenance, license, vulnerability posture,
runtime cost, version strategy, and removal path in `docs/dependency-policy.md`. Use `uv` to
update `uv.lock`; never edit resolved entries manually.

Alembic owns operational PostgreSQL schema changes. A migration must work from an empty
database, preserve existing data, define rollback/recovery behavior, and include constraint,
concurrency, and least-privilege tests as applicable. Runtime startup must detect schema
drift rather than silently create or mutate tables.

## Documentation and status

Update the nearest source of truth when behavior, commands, schemas, trust boundaries, or
limitations change. Do not describe target design as current behavior. Use only:

- `IMPLEMENTED_AND_VERIFIED`
- `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`
- `PARTIALLY_IMPLEMENTED`
- `NOT_IMPLEMENTED`
- `BLOCKED`
- `INTENTIONALLY_DEFERRED`

Evidence must name an executed command, inspected implementation, or external response.
Backtest output is research evidence, fake-broker replay is engineering evidence, and
Alpaca paper fills remain simulated; do not blur those categories or claim profitability.

## Review and commit workflow

Before requesting commit approval:

```bash
git status --short
git diff --check
git diff --stat
git diff --name-status
git diff
```

Because ordinary `git diff` omits untracked files, also inspect
`git status --short --untracked-files=all` and every proposed untracked file in full. The exact
inspection and approval protocol is in `docs/code-review.md`.

The review packet must state purpose, changed and unchanged behavior, every file, exact
checks and results, simplification findings, security findings, residual risk, the proposed
boundary, and a concise suggested message. A maintainer must explicitly approve that exact
reviewed change before a commit is created unless the active plan records a scoped standing
delegation. The delegation removes only the repeated response pause; it does not remove review or
staged-diff checks. Commit authority does not authorize a push, pull request, merge, release, image
publication, or deployment unless those actions are separately named.

Stage only reviewed paths or hunks. Do not add authorship trailers, rewrite history,
manipulate dates, or force-push. Commit messages should be concrete, imperative, concise,
and free of marketing language.

## Pull requests

A pull request should explain:

- the observable behavior and invariant affected;
- compatibility and migration effects;
- trust boundaries and possible side effects;
- exact commands and results;
- externally unvalidated behavior;
- residual risks and recovery steps.

Reviewers follow `docs/code-review.md`. Resolve every confirmed `BLOCKER` and `HIGH`
finding before merge. Do not weaken tests or quality gates to obtain a passing check.
