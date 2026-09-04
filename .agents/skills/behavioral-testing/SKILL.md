---
name: behavioral-testing
description: "Design or strengthen tests for externally meaningful repository behavior; do not use solely to mirror implementation details or inflate coverage."
---

# Behavioral testing

1. Read [testing-strategy.md](../../../docs/testing-strategy.md), then define the observable behavior, invariant, and failure consequence from [README.md](../../../README.md), [ARCHITECTURE.md](../../../ARCHITECTURE.md), or the applicable subsystem reference.
2. Enumerate positive, negative, boundary, idempotency, restart, and recovery cases that matter to a caller or operator.
3. Select the lowest test level that proves the contract, using a real ephemeral PostgreSQL instance when database behavior is essential.
4. Minimize mocking at the unit under test; fake external market or broker boundaries so ordinary tests remain offline and deterministic.
5. Use fixed clocks, seeds, fixtures, identifiers, and expected values. Never derive expected results from the implementation being tested.
6. Assert outcomes and durable state, including rejection and fail-closed behavior, rather than private call sequences.
7. Run the focused tests and the complete offline suite using the locked commands in [tooling.md](../../../docs/tooling.md).

Respect the markers and test paths in [pyproject.toml](../../../pyproject.toml). Tests must not require credentials or contact Alpaca in ordinary development or CI.
