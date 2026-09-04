# Engineering Principles

These rules resolve design choices in this repository. The governing priorities are
financial safety, data integrity, deterministic evidence, and the smallest maintainable
implementation that preserves them.

## Evidence before claims

A checked-in interface or diagram is not implementation evidence. A material status claim
must name an executed test, command result, inspected invariant, migration result, or
external-system response. Use only the repository status vocabulary in
`docs/requirements.md#status-vocabulary`.

Keep evidence categories separate:

- synthetic backtests measure deterministic research behavior under stated assumptions;
- replay and fake brokers measure orchestration and recovery behavior;
- disposable PostgreSQL proves local persistence and migration behavior;
- only a recorded provider run can validate credential-based connectivity;
- Alpaca paper fills are simulated and cannot establish live execution quality.

Do not report profitability, low latency, or deployment reliability without corresponding
measurements.

## Financial authority is explicit and narrow

Data collection, strategy proposals, risk decisions, order planning, broker effects, and
reconciliation are separate authorities. Collection membership is not an order allowlist.
A proposal is untrusted data until independent validation and risk controls approve it.

The default path is offline and credential-free. Tracked configuration disables paper
submission. Real-money execution is unsupported. Missing, stale, contradictory, or
ambiguous state results in no new exposure.

## Preserve facts; derive projections

Consequential inputs and outcomes are append-only facts. Mutable tables such as
`current_bars`, checkpoints, order state, or heartbeats are bounded projections over those
facts. A correction appends new evidence; it does not erase the earlier observation.

Persist a logical order intent before an external submission begins. A timeout after that
boundary is ambiguous, not a failed request that is safe to repeat. Resolve ambiguity by
stable identity and reconciliation.

## Determinism is a functional requirement

Replayable identities derive from canonical inputs. Inject clocks, seeds, providers, and
repositories at volatile boundaries. Use UTC for instants and explicit exchange calendars
for sessions. Do not use wall-clock sleeps in tests or let local timezone influence a
decision.

Research code must preserve the anti-look-ahead boundary: a decision cannot consume data
that was unavailable at its recorded cutoff. Later corrections may create incidents or new
dataset versions; they do not rewrite an earlier decision.

## Prefer direct, bounded control flow

Use a function, frozen dataclass, context manager, or existing library primitive when it
expresses the behavior clearly. Add a protocol or adapter only for a current external
boundary, meaningful variation, or test substitution. The collector's historical source,
live source, and repository protocols are justified because they isolate network and
persistence effects.

Avoid pass-through managers, single-path factories, generic utility modules, inheritance
without substitutable behavior, configuration switches for unsupported modes, and public
interfaces without a caller.

All work is bounded: retries, response sizes, pages, database batches, queues, leases,
network waits, worker joins, and shutdown. Do not introduce detached work whose lifecycle
and failure propagation are unclear.

## Read before changing ownership

Before editing a subsystem:

1. Find its CLI, service, or import entry point.
2. Trace data, error, and side-effect paths.
3. Identify the table, file, or object that owns durable state.
4. Read neighboring unit, integration, safety, and restart tests.
5. Compare the legacy implementation and collector only where their semantics match.
6. State the invariant and trust boundary being changed.
7. Check the active execution plan and relevant decision records.

Do not copy legacy long-only contracts into the target signed platform and silently change
their meaning. Preserve legacy behavior behind compatibility entry points while adding a
separate versioned contract when semantics differ.

## Make invalid states difficult to represent

Use frozen dataclasses for immutable internal facts, strict boundary models for external
input, enums for genuine closed state sets, database constraints for cross-process
invariants, and state-transition checks for orders and leases. Use `Decimal` for money,
price, quantity, weight, and notional boundaries; reject nonfinite values before persistence
or risk evaluation.

Types and constraints should remove a demonstrated failure mode. Do not add elaborate type
machinery for appearance.

## Fail visibly and recover deliberately

Catch an error only to recover, perform a bounded retry, translate an external error into a
stable safe error, preserve transactional/audit invariants, or contain a top-level worker
failure. Unknown failure never becomes success.

Diagnostics should identify the operation, safe correlation identifiers, reason category,
and retryability without serializing secrets, headers, settings, database URLs, or raw
external payloads. Cleanup failure must not hide the primary failure.

## Test behavior, not implementation theater

Tests should prove domain outcomes, state transitions, persistence, idempotency, recovery,
and boundary rejection. A mock call alone does not prove behavior. Expected values must not
be calculated by the production function under test.

Use real local PostgreSQL for migration, trigger, transaction, and concurrency behavior;
use fakes for external Alpaca volatility. Ordinary tests require no credentials and guard the
common Python TCP connection paths used by current adapters; process-wide denial remains target
work.

Do not delete a failing test, weaken an assertion, broaden an ignore, or mark a required CI
gate optional to make a change pass.

## Optimize only a measured path

Correctness and auditability dominate speculative speed. Preserve deterministic batching
and indexes that support actual access patterns. Establish a repeatable benchmark and
profile before changing algorithms, adding caches, introducing concurrency, or adding a
native language. Performance claims include workload, environment, repetitions, variance,
and unchanged correctness evidence.

## Keep the repository explainable

Code, tests, schema, runbooks, requirements, and status must agree. Comments explain a
non-obvious invariant, ordering constraint, provider behavior, or failure mode; they do not
narrate syntax. Delete dead code, unused configuration, redundant conversion, duplicated
validation at trusted internal boundaries, and stale documentation during the same change.

The target is not maximum code volume or maximum tool count. The target is an auditable path
from validated input to safe state with reproducible evidence.
