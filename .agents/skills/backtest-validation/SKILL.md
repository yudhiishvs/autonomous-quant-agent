---
name: backtest-validation
description: "Validate historical simulation, model evaluation, or reported strategy performance; do not use for ordinary unit tests or as evidence of future profitability."
---

# Backtest validation

1. Freeze and identify the code, configuration, dependency lock, dataset, universe, feature schema, cost policy, risk policy, and seed.
2. Verify timestamp semantics, session calendars, decision watermarks, feature and label availability, splits, embargoes, and the no-look-ahead boundary in [methodology.md](../../../docs/methodology.md).
3. Inspect leakage from fitting, normalization, feature selection, corporate actions, revised data, symbol selection, repeated trials, and holdout access.
4. Validate fills, spreads, slippage, fees, latency, turnover, liquidity, shorting, cash, reversals, forced exits, and risk interventions with independent known-answer ledgers.
5. Compare against predeclared passive and active benchmarks under identical timing, costs, data, and risk constraints.
6. Repeat the run from frozen inputs and require stable decisions, ledgers, metrics, artifacts, and hashes except for documented runtime metadata.
7. Evaluate walk-forward and split design, per-symbol and per-regime stability, negative controls, cost stress, and sensitivity without post-hoc rule changes.
8. Check every reported metric and claim against [README.md](../../../README.md#verification-and-test-plan), including uncertainty, undefined values, limitations, and valid negative results.

Backtests must remain broker-free. Do not present simulated, replay, or paper evidence as live execution performance or a guarantee.
