# Contributing

Keep changes focused and explain the problem they solve. Start with `AGENTS.md`, then
read the affected code, neighboring tests, and relevant design notes.

## Setup

Use Python 3.11, uv, and a POSIX shell. Docker is needed for container and disposable
PostgreSQL checks, but not for the ordinary offline tests.

```bash
uv sync --locked --all-extras
```

Development does not require Alpaca credentials. Never commit secrets, local databases,
account records, downloaded market data, or generated runtime output.

## Making a change

Check `git status` first and preserve existing work. Follow the data from its entry
point to the code that stores it or performs an external action. Prefer a small fix
with a test that reproduces the problem over a broad refactor.

For behavior changes, test both the expected outcome and relevant failure cases.
Persistence changes often need duplicate, restart, rollback, or concurrency coverage.
Use injected clocks and fake providers so ordinary tests stay offline and repeatable.
Update the active execution plan for changes across modules, storage schemas, or
external authorization boundaries.

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest -q
git diff --check
```

Run the affected integration and regression checks too. PostgreSQL tests can reset
their database: use only the guarded disposable setup in
[testing-strategy.md](docs/testing-strategy.md). Never point them at shared data.

## Boundaries to preserve

- Strategy and presentation code cannot submit orders or obtain broker credentials.
- Market-data credentials remain separate from paper-account credentials.
- Record order intent before submission. Reconcile uncertain outcomes before retrying.
- Validate external data before storage, and keep secrets out of errors and logs.
- Real-money endpoints and `paper=False` are unsupported.

Use the existing UTC, Decimal, configuration, and persistence conventions. The
[coding standards](docs/coding-standards.md) explain them; the
[security model](docs/security-model.md) describes the trust boundaries.

## Dependencies and migrations

Explain why a new dependency is needed. Update dependency declarations and `uv.lock`
together using uv; don't hand-edit resolved lock entries. The current AI dependency
freeze still applies. Follow the [dependency policy](docs/dependency-policy.md).

Database changes need an Alembic migration and tests for existing data as well as a
fresh database. Describe recovery steps and any compatibility impact.

## Review and publication

A review should answer four questions:

1. What problem does this solve?
2. What changed, and what are the tradeoffs?
3. Which checks ran, and what did they show?
4. What remains uncertain or needs separate validation?

Inspect the full diff, including new files. Resolve confirmed blocker and high-severity
findings, and don't weaken a check to make it pass. Keep documentation consistent with
what the code actually does; distinguish simulated evidence from provider validation.

The [review guide](docs/code-review.md) owns the detailed approval procedure. Work only
within the maintainer's authorization: local editing does not authorize commits,
pushes, PRs, merges, history rewrites, or deployment. Stage only reviewed changes when
a commit is authorized.

Report suspected vulnerabilities through [SECURITY.md](SECURITY.md).
