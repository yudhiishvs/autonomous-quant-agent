# Developer guide

## Supported environment

| Surface | Support policy |
| --- | --- |
| Python | 3.11 |
| PostgreSQL | 16 for operational and integration use |
| Linux | primary CI and container target |
| macOS | supported local development target |
| Windows | native execution unsupported; hardened secret publication requires POSIX semantics |
| Config schema | version 1 |
| Signal contract | version 1 |

Use `uv` and the checked-in `uv.lock`. Do not install an independently resolved dependency graph
when validating a change.

```bash
uv sync --locked --all-extras
uv run --no-sync aqa doctor
uv run --no-sync pytest -q
```

The default profile is offline, deterministic, fake-broker, submission-disabled, and
credential-free. Provider connectivity is never a prerequisite for ordinary development.
Built source and wheel artifacts embed the shipped platform profiles, experiment definition, and
complete Alembic graph as package resources. Consequently, installed `aqa doctor` and `aqa demo`
defaults do not depend on a source checkout; explicit deployment profiles still use the validated
`--config` and `--application-root` boundaries.

## Repository map

- `src/adaptive_trader/platform/`: generic platform contracts and services;
- `configs/experiments/`: versioned experiment-specific values;
- `configs/platform/`: offline, shadow, and paper profiles;
- `migrations/`: forward Alembic history for operational state;
- `examples/`: disabled educational extensions;
- `tests/`: legacy, unit, architecture, safety, research, and guarded integration tests;
- `scripts/`: bounded verification, backup/restore, benchmark, and local bootstrap commands;
- `docs/adr/`: accepted architectural decisions and their consequences.

The generic platform package must not contain the shipped experiment's symbol literals. Load roles
from the immutable experiment contract and use `active_tradable` for order authority.

## Configuration and secrets

Validate profiles statically:

```bash
uv run --no-sync aqa config validate \
  --config platform/offline.yaml \
  --config-root configs
```

The runtime accepts only the documented `AQA_*_FILE` secret references. The file loader rejects
symlinks, directories, non-owner files, modes other than `0400`/`0600`, empty values, NUL bytes,
and changed file identity. It removes one trailing newline and returns a redacted wrapper.

Generate only local database-role passwords and the operator token with:

```bash
uv run --no-sync aqa secrets bootstrap-local
```

The command is idempotent, never overwrites a valid file, never creates provider keys, and prints
paths rather than values. Data and paper credentials remain separate. Do not use generic SDK
environment variables in the platform runtime.

## Development boundaries

The dependency direction is data → canonical state → signal → risk → intent → execution policy →
broker adapter. External I/O belongs at injected adapters. In particular:

- signal providers receive only `DecisionContext` and return `SignalEnvelope`;
- providers do not import storage, credentials, risk mutation, or execution;
- risk binds its output to the exact signal, policy, experiment, statistics, and source timestamps;
- order intent is durable before broker submission;
- unknown side-effect outcomes are reconciled by deterministic identity, never blindly retried;
- APIs accept bounded typed operations, not SQL, URLs, paths, commands, modules, or source code.

When adding a public contract, prefer an immutable slotted dataclass or a strict frozen Pydantic
model. Use timezone-aware UTC values and finite `Decimal` at financial boundaries. Hash replayable
identity through canonical serialization.

## Tests and checks

Run focused tests while editing, then the canonical gates:

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest -q
uv run --no-sync pytest --cov=adaptive_trader --cov-branch --cov-report=term-missing
uv run --no-sync python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic
uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml
docker compose --env-file .env.example -f docker-compose.yml config --quiet
git diff --check
```

Ordinary tests cannot use real credentials or non-loopback sockets. PostgreSQL tests are opt-in and
accept only an explicitly destructive loopback `collector_test` database. Never weaken a state
machine, skip a failing test, or reduce a security assertion to make CI pass.

## Versioning, migrations, and deprecation

The package follows semantic versioning. While the package is below `1.0`, minor releases may add
or change public contracts, but documented compatibility aliases are preserved deliberately.
After `1.0`, incompatible public changes require a major release.

Public APIs receive at least two minor releases of deprecation notice before removal. A security
vulnerability may require immediate disablement; the security advisory explains the exception.

Migrations are forward-only for market-data, order, fill, reconciliation, and audit history.
Operational startup never invokes `metadata.create_all`. A destructive downgrade is refused. New
migrations must upgrade an empty PostgreSQL 16 database and the preceding checked-in revision, and
must preserve repository/table agreement.

Security fixes target the latest released minor version. Older minors are not maintained unless a
release note explicitly says otherwise. No package is published to PyPI or GitHub Releases without
maintainer authorization.

## Benchmarking

```bash
uv run --no-sync python scripts/benchmark_pipeline.py \
  --warmups 2 --repeats 5 --iterations 25
```

The output is machine-readable observational data. It is not a latency service-level objective and
does not gate CI. Do not introduce native code until a repeatable benchmark plus profiler identifies
a specific bottleneck and a new ADR shows a material benefit.
