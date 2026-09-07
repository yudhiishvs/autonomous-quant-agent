# ADR 0009: Retain Python until profiling justifies native code

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-SCOPE-003`, `REQ-SCOPE-004`, `REQ-PERF-001`

## Context

Native code can increase build, packaging, portability, memory-safety, and vulnerability-management
cost. No measured bottleneck currently establishes that those costs improve the product.

## Decision

Implement the platform in typed Python 3.11. Maintain a deterministic benchmark for normalization,
persistence, aggregation, slot claim, risk, and fake execution/reconciliation. Add native code only
after repeatable benchmark and profiler evidence identifies a specific hot path and a new ADR shows
material benefit.

## Alternatives considered

- Add C++ preemptively: rejected because there is no measured target.
- Rewrite services in another language: rejected because it fragments contracts and operations.
- Set wall-clock CI limits: rejected because shared runners are unstable measurement environments.

## Consequences

Benchmark output is observational and machine-readable but is not an acceptance threshold. Optimize
algorithms, queries, and allocation only after profiling; preserve deterministic outputs across any
future implementation change.

## Security impact

Avoiding an unnecessary compiler/native dependency reduces supply-chain and memory-safety surface.
Future native artifacts require reproducible builds, SBOM coverage, input validation, and fuzzing.
