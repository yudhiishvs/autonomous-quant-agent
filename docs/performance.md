# Performance

## Current status

The collector uses bounded batches, indexed read paths, and concurrent live/reconciliation
workers. Legacy research uses vectorized pandas/NumPy calculations where already established.
A repeatable benchmark harness and performance budgets are `NOT_IMPLEMENTED`; no throughput,
latency, capacity, or scaling claim is supported yet.

No current bottleneck has been established by a retained profile. Existing batching and indexes
are correctness/resource controls, not proof that the corresponding path is fast enough.

## Sensitive operations

| Operation | Workload dimension | Correctness constraint |
| --- | --- | --- |
| Historical normalization | pages, 29 symbols, one-minute bars | Every payload validated; page/token bounds retained |
| PostgreSQL ingestion | observations per batch and identity contention | Each observation/projection batch is atomic and lease-fenced; coverage advances only after every request batch is durable |
| Current-bar projection | duplicate/correction candidates per identity | Deterministic precedence and revision semantics cannot change for speed |
| Collector recovery | XNYS sessions and checkpoint overlap | Coverage advancement is separately transactional and lease-fenced; safe duplicate replay, retry, and shutdown remain bounded |
| Legacy backtest | sessions, symbols, rolling windows, comparisons | Anti-look-ahead and deterministic artifact behavior preserved |
| Risk statistics | covariance window and symbol count | Nonfinite inputs fail closed; numerical semantics remain documented |
| Reporting/dashboard | SQLite rows and artifact size | Reads remain bounded and must not mutate authority state |

## Existing resource bounds

- Collector API symbol batches cannot exceed the 29-symbol collection universe.
- Database writes default to batches of 750 observations.
- REST pagination, retry attempts, response consumption, rate-limit delay, stream restart
  delay, connection pools, SQL statements, locks, and worker shutdown all have finite bounds.
- PostgreSQL indexes cover observation identity/recent lookup, current bars, active gaps,
  leases, and active-run readiness; insertion time uses a BRIN index.
- The dashboard limits allowlisted SQLite table reads to 500 rows by default.

These are implementation controls, not measured capacity promises.

The current collector checkpoint records the end of a successfully queried REST window. It does
not prove contiguous, quality-approved coverage: an empty or partial symbol response can still
advance that symbol's checkpoint, and the gap lifecycle is not active. Target platform watermarks
must close this readiness gap before downstream decisions rely on them.

## Metrics and memory

The future benchmark records distributions and correctness hashes for these units; no target value
is accepted until the representative workload exists:

| Operation | Timing/throughput unit | Resource measure |
| --- | --- | --- |
| Canonical normalization | events/second and microseconds/event | peak RSS MiB and allocated bytes/event |
| Transactional ingestion | committed bars/second and transaction p50/p95 milliseconds | peak batch MiB, pool occupancy, and stored bytes/bar |
| 15-minute aggregation | input bars/second and aggregate p50/p95 milliseconds | peak group/window MiB and output rows |
| Decision-slot claim | claim transaction p50/p95 milliseconds under declared contenders | lock-wait milliseconds and database connections |
| Signed risk | decision p50/p95 milliseconds for declared symbols/window | covariance-array MiB and peak RSS MiB |
| Fake execution/reconciliation | intents/second and cycle p50/p95 milliseconds | queued event count and peak RSS MiB |

Batch and page limits must prevent memory growth proportional to unbounded provider history.
Parquet freeze must stream or partition representative datasets rather than materialize duplicate
full copies without a measured need. Every benchmark records process peak RSS; database size and
index growth are recorded for persistence workloads. A speed improvement is invalid if it changes
logical hashes, drops append-only evidence, or removes a resource bound.

## Measurement procedure

Before a performance change:

1. Define one question and a representative deterministic workload.
2. Record Python, dependency lock hash, database version, CPU, memory, operating system, and
   dataset shape.
3. Establish a reproducible baseline from the checked-in resolved benchmark configuration.
4. Profile the slow path and identify the measured bottleneck before editing.
5. Change one cohesive cause and repeat under equivalent conditions.
6. Run domain, persistence, replay, and safety tests to prove semantics are unchanged.
7. Report median, range or percentile, variance, and measurement limitations.
8. Retain the resolved configuration, environment, raw samples, profile, correctness hashes, and
   summary artifacts.
9. Review whether the change added complexity, coupling, or maintenance cost unsupported by the
   measured benefit; simplify or reject it when that trade is not justified.

Wall-clock thresholds from shared CI runners must not become merge gates. Stable structural
bounds, query plans, allocation limits, and deterministic operation counts may be gated.

## Planned benchmark surface

The active platform plan calls for deterministic measurements of canonical normalization,
one-minute persistence, 15-minute aggregation, decision-slot claiming, signed risk, and fake
order/reconciliation. Until `scripts/benchmark_pipeline.py` exists and produces reviewed
machine-readable evidence, this surface remains `NOT_IMPLEMENTED` and there is no valid benchmark
command to publish. Phase 9 must add the executable command and its input/output schema together.

The planned checked-in `benchmarks/pipeline-v1.json` resolves this deterministic workload; the
file and harness are both `NOT_IMPLEMENTED`:

| Setting | Version 1 value |
| --- | --- |
| Random seed; warm-up/measured runs | `20260808`; 2/5 |
| Input provenance | Offline `synthetic-market-v1` generator version 1, canonical `market-bar.v1` schema, and a recorded logical fixture hash; no provider credentials or network |
| Market-data shape | 11 collection symbols, 20 full sessions, 390 one-minute bars per symbol/session |
| Persistence backend | Disposable loopback PostgreSQL 16 for ingestion, slot, and execution/reconciliation persistence; CPU-only units receive the same immutable synthetic fixture |
| Ingestion/aggregation | Batch size 750; 15-minute buckets; transaction/bucket samples from the full fixture in every measured run |
| Slot contention | 100 slots with 8 concurrent claimers per measured run |
| Signed risk | 8 active symbols, exactly 500 within-session returns per symbol, and 500 timed decisions per measured run |
| Fake execution/reconciliation | 100 timed cycles per measured run, at most 8 intents per cycle |

Per-operation p50/p95 values use the individual transaction, bucket, claim, decision, or cycle
samples across all five measured runs. Five-value run-level summaries report median and range only;
they do not label a noisy maximum as p95. The resolved config records the generator source revision,
schema version, logical fixture hash, backend/version, isolation/reset procedure, and per-unit sample
count so a changed generator or database cannot masquerade as an equivalent baseline.

Each invocation will create one ignored local directory under `runtime/benchmarks/` containing
JSON files named `resolved-config.json`, `environment.json`, `raw-samples.json`, `profile.json`,
`correctness.json`, and `summary.json`. Every file will carry a schema version and benchmark run ID;
the summary will reference hashes of the other files. Evidence used for a claim must retain or
archive that whole directory and cite its hashes in the active plan. Machine-specific result
artifacts are not committed by default.

`environment.json` is an allowlist, never an environment dump: Python/runtime version, dependency
lock hash, PostgreSQL server version, operating-system name/version, CPU model/count, and total
memory MiB only. It excludes environment values, database URLs, credentials, account IDs,
hostnames, usernames, and absolute paths. Benchmark run IDs are opaque random identifiers with no
host-derived content. `profile.json` stores repository-relative module/function locations or
redacted external-package names, never absolute source paths. Before any result is retained or
archived, validation rejects unexpected fields and scans every artifact for sentinel secrets,
credential/URL patterns, usernames, hostnames, and absolute/private paths.

Do not add another runtime language or native extension before a committed benchmark and
profile identify a specific Python bottleneck and a design decision records the maintenance
tradeoff.
