# Coding Standards

## Python and module boundaries

- Support Python 3.11 behavior even when a developer uses a newer interpreter.
- Use `snake_case` for functions/modules, `PascalCase` for types, and domain names rather
  than generic manager/helper terminology.
- Keep external I/O in provider, database, CLI, or broker modules. Domain transformations
  should be pure where practical.
- Use absolute `adaptive_trader` imports across package modules; no wildcard imports or
  runtime `sys.path` mutation.
- Treat names exported from package `__init__.py` or console entry points as public. Keep
  incidental helpers private.
- Do not import legacy execution or broker modules from `adaptive_trader.collection`.
- New target-platform code must not change the semantics of legacy long-only contracts.
- Refactor when cohesion, ownership, or reviewability is poor; do not impose arbitrary file,
  function, or class line-count limits as a substitute for that judgment.
- Introduce protocols or dependency inversion only for a current external boundary, meaningful
  variation, or test seam. Do not invert every layer for appearance.

## Types, values, and ownership

- Annotate public and cross-module boundaries. Mypy errors are corrected, not broadly
  ignored; any narrow suppression states the external limitation.
- Prefer frozen, slotted dataclasses for immutable internal facts and strict Pydantic v2
  models for YAML, environment, API, or other untrusted input.
- Avoid optional fields when absence is not a valid domain state. Validate nullable external
  fields once, then pass a narrower internal type.
- Use timezone-aware UTC `datetime` values for instants and an explicit exchange calendar
  for session rules. Reject naive timestamps at public boundaries.
- Use `Decimal` for price, money, quantity, weight, notional, and fee boundaries. Floating
  point is limited to documented statistical calculations and must be finite.
- No mutable global state unless ownership, synchronization, reset behavior, and tests are
  documented.
- Give domain limits, protocol values, deadlines, and safety thresholds descriptive constants;
  obvious local literals do not need ceremonial names.
- Convert an external representation once at its owning boundary. Do not repeatedly shuttle
  equivalent internal values between dictionaries, ORM rows, dataframes, and domain objects.

## Errors and resources

- Error paths are explicit: classify retryable, user-correctable, security-sensitive,
  rollback-required, and ambiguous outcomes.
- Catch exceptions only to recover, retry within a bound, translate at a boundary, add safe
  context, or preserve durable state. Never use `except Exception: pass`.
- A top-level worker may contain an unknown exception only after recording a redacted
  failure and leaving state fail-closed.
- Use context managers for transactions, connections, streams, files, and temporary state.
- Set bounds for network calls, retries, pagination, batch size, leases, joins, and shutdown.
- Do not place secret values, headers, connection URLs, provider payloads, or raw exception
  objects into logs, metrics, API errors, or persisted diagnostic text.

## Persistence and concurrency

- Alembic owns operational PostgreSQL changes; runtime code does not create schema.
- Use SQLAlchemy parameter binding, explicit transactions, database constraints, and indexes
  tied to actual queries. Do not interpolate SQL.
- Preserve append-only facts. Update projections only in the same transaction as their
  authoritative event when atomicity is required.
- Collector ingestion/run/event/bar/checkpoint mutations after lease acquisition require its
  current fencing token. Idempotent universe registration and lease acquire/renew/release are
  explicit lifecycle exceptions. Checkpoint movement is monotonic and occurs only in the queried
  window transaction; it does not prove contiguous quality-approved coverage.
- Define task/thread ownership, cancellation, failure propagation, and a shared shutdown
  bound. Avoid detached tasks and sleep-based coordination.
- Stable event, signal, intent, and client-order identities make replay idempotent. A timeout
  after a possible side effect enters an ambiguous state and requires reconciliation.

## Configuration and logging

- Tracked configuration is non-secret, strict, versioned where it crosses a durable boundary,
  and explicit about disabled submission.
- Unsupported live-money terms and endpoints are rejected; no generic broker hostname.
- Environment access belongs in a settings/credential boundary, not scattered domain code.
- Log one useful event at a boundary or state transition. Include bounded safe identifiers,
  reason code, and retryability; avoid entry/exit noise and unbounded metric labels.

## Documentation and compatibility

- Public contracts with non-obvious invariants receive concise docstrings. Private helpers
  need none when names and types communicate the behavior.
- Comments explain why an ordering, security, provider, or recovery constraint exists.
- Do not leave commented-out code, required TODO stubs, placeholder branches, or unused
  compatibility wrappers.
- Breaking CLI, config, schema, or import changes require a migration path, version change,
  tests, and documentation. Prefer additive aliases before removing established commands.
- Generated lockfiles and migration metadata are changed with their owning tool; local
  caches, databases, reports, and downloaded data are never source files.

## Tests

- Place broad shared fixtures in `tests/conftest.py`, collector PostgreSQL tests in
  `tests/integration/`, and static boundary tests in `tests/safety/`.
- Use deterministic clocks, seeds, IDs, and synthetic payloads. Do not use wall-clock sleep.
- Verify outcomes and durable state, not only mock calls. Keep fixtures small and readable.
- Bug corrections include a regression case; new high-risk behavior includes malformed,
  boundary, replay, restart, and failure-path cases as applicable.
- Follow `docs/testing-strategy.md` and keep ordinary tests credential-free and offline.
