---
name: performance-investigation
description: "Investigate a measured latency, throughput, memory, storage, or scaling problem; do not use for speculative optimization without a reproducible baseline."
---

# Performance investigation

1. Read [performance.md](../../../docs/performance.md), then define the user-visible or operational performance question, workload, environment, and acceptable threshold.
2. Establish a repeatable baseline with warm-up, sample count, input size, resource limits, and correctness checks recorded.
3. Profile before editing and separate CPU, I/O, network, database, allocation, and contention costs.
4. Identify the measured bottleneck and confirm it explains a meaningful share of the baseline.
5. Implement the smallest improvement that preserves contracts in [ARCHITECTURE.md](../../../ARCHITECTURE.md) and the applicable data dictionary.
6. Benchmark before and after under equivalent conditions and representative data volumes.
7. Run behavioral and regression tests, including restart, idempotency, and deterministic-output checks where relevant.
8. Retain the benchmark configuration and relevant raw result artifacts, report distributions rather than a single favorable run, and document noise, tradeoffs, new limits, and remaining uncertainty.

Do not trade correctness, causality, evidence retention, or fail-closed behavior for speed. Use the workload and limitations described in [README.md](../../../README.md) to avoid unsupported scale claims.
