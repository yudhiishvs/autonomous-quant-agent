# Performance

Performance work must preserve deterministic hashes, append-only evidence, transaction boundaries,
timeouts, and resource limits. A faster result with different semantics is a correctness failure.

## Implemented benchmark surface

`scripts/benchmark_pipeline.py` measures six deterministic offline operations:

| Operation | Reported unit |
| --- | --- |
| canonical normalization | latency and normalized events/second |
| one-minute SQLite ingestion/persistence | transaction latency and persisted bars/second |
| exact 15-minute aggregation | latency and aggregates/second |
| durable decision-slot claim | claim latency and claims/second |
| signed risk decision | latency and decisions/second |
| fake order plus reconciliation | cycle latency and cycles/second |

Run:

```bash
uv run --no-sync python scripts/benchmark_pipeline.py \
  --warmups 2 --repeats 5 --iterations 25
```

Output is one stable JSON document containing:

- schema and deterministic synthetic fixture identity;
- bounded warm-up, repeat, and iteration counts;
- Python implementation/version, OS platform, machine, and processor metadata;
- operation count per repeat;
- minimum, median, and maximum elapsed seconds;
- minimum, median, and maximum milliseconds per operation; and
- minimum, median, and maximum operations per second.

Arguments accept at most 100 warmups/repeats/iterations so a malformed local invocation cannot
create unbounded work. The harness uses no credentials, provider transport, broker network, random
market input, or committed machine-specific output.

## Interpretation

Measurements are observational. They are not capacity promises, latency objectives, market-timing
claims, or CI pass/fail thresholds. Shared runner wall-clock values are too noisy for merge gates.
Stable structural limits, deterministic operation counts, and correctness hashes may be gated.

The benchmark's SQLite persistence timing represents the credential-free local path, not
PostgreSQL production capacity. Run separate guarded PostgreSQL query/transaction profiling before
claiming operational throughput.

## Existing resource bounds

- provider pages, response bytes, retries, backoff, stream reconnect, and shutdown are finite;
- collection is constrained to the experiment collection allowlist;
- canonical text/nodes/depth/numbers and API requests/pages/rates are bounded;
- database transactions, leases, attempts, job retries, order intents, and deadlines are bounded;
- aggregation requires exactly one aligned 15-minute bucket of 15 one-minute bars;
- signed risk statistics use the fixed experiment window and at most eight constraint passes;
- artifact publication is root-confined and no-replace;
- dashboard and API reads are paginated.

These are resource and safety controls, not evidence of speed.

## Measurement procedure

Before optimizing:

1. state one measurable question and select a representative deterministic workload;
2. record source revision, lock hash, Python/database versions, OS/CPU, and input shape;
3. run warmups and multiple measured repeats;
4. retain raw samples and correctness hashes outside Git when a claim depends on them;
5. profile the slow path before changing code;
6. change one cohesive cause;
7. repeat under comparable conditions and report median/range plus limitations;
8. run domain, persistence, replay, demo, and safety regressions; and
9. reject complexity whose measured benefit does not justify maintenance/security cost.

Do not publish hostnames, usernames, absolute private paths, environment dumps, database URLs,
credentials, account IDs, or provider payloads with benchmark evidence.

## Native optimization decision

No C++ or other native extension is justified by current evidence. Python 3.11 remains the platform
language. Native code requires a committed repeatable benchmark, a profiler identifying a specific
Python bottleneck, an ADR comparing simpler algorithm/query/allocation changes, deterministic output
parity, reproducible packaging, SBOM/scan coverage, and a material measured benefit.
