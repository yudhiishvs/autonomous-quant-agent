# Resume evidence

Only reproduced command output belongs here. This document contains engineering facts, not planned
features, market results, or profitability claims.

## Deterministic offline vertical slice

Command:

```bash
uv run --no-sync python - <<'PY'
from pathlib import Path
from adaptive_trader.platform.demo import run_demo_twice

comparison = run_demo_twice(config_root=Path("configs"))
print(comparison.manifest_hash)
print(comparison.first.manifest())
PY
```

Measured on 2026-09-05:

- two fresh isolated runs produced the identical logical manifest hash
  `11bdac70a12a74b834499825b8923054d48f316a85e61c764e65684d65bf8beb`;
- the manifest contained 121 canonical one-minute event hashes, 120 effective one-minute event
  hashes, eight 15-minute aggregate hashes, two aggregate-revision hashes, nine watermarks, 21
  deterministic slot IDs, one signal hash, one signed-risk hash, two execution-plan hashes, three
  deterministic client-order IDs, two fill hashes, and three reconciliation hashes;
- all nine required restart/failure boundaries reported deterministic recovery with zero duplicate
  side effects and zero incidents;
- the broker transition evidence included `SUBMISSION_UNKNOWN`, recovery to `ACCEPTED`, and terminal
  `FILLED` state;
- forced flatten ended with no signed positions and deterministic cash/equity/buying-power values
  of `100000.00000000`, with restricted short proceeds `0`;
- the evidence label was `OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE`.

Focused tests:

```bash
uv run --no-sync pytest -q \
  tests/architecture/test_platform_configuration_boundary.py \
  tests/unit/test_platform_demo.py \
  tests/unit/test_pipeline_benchmark.py
```

Measured result on 2026-09-05: `10 passed`.

## Benchmark harness

Command used for a schema smoke measurement:

```bash
uv run --no-sync python scripts/benchmark_pipeline.py \
  --warmups 0 --repeats 1 --iterations 1
```

Measured on an Apple arm64 host with CPython 3.11.15 on 2026-09-05. The command emitted JSON for
all six required operations: canonical normalization, one-minute SQLite persistence, 15-minute
aggregation, decision-slot claim, signed-risk decision, and fake-order reconciliation. Each
reported positive elapsed, latency, and throughput values. This one-operation smoke is proof of the
measurement surface only; it is not a stable performance comparison, CI threshold, capacity claim,
or production service-level objective.

## Excluded claims

No Alpaca credential, provider connection, paper connection, or paper order participated in these
measurements. The results do not measure profitability, Sharpe, alpha, win rate, model accuracy,
live reliability, or strategy quality.

Repository-wide test count, coverage, PostgreSQL restore result, container result, remote-CI result,
and package-install result must be added only from their final successful commands.
