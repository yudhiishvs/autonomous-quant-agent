---
name: trading-strategy-review
description: "Review a strategy, allocator, risk, signal, or execution-policy change for research validity and paper-only operational safety; do not use for unrelated market-data plumbing."
---

# Trading strategy review

1. Confirm the research question, active universe, signal definition, state ownership, and promotion boundary in [README.md](../../../README.md) and [methodology.md](../../../docs/methodology.md).
2. Inspect signal timing, feature availability, eligibility, missing-data handling, direction and exposure semantics, and assumptions about market regime persistence.
3. Separate strategy proposals from independent allocation, risk approval, order planning, and broker authorization.
4. Review execution assumptions, session windows, liquidity, costs, slippage, short availability, position accounting, cash, corporate actions, and forced exits.
5. Verify limits, stale-data behavior, latches, idempotency, reconciliation, restart recovery, and failure dominance using [ARCHITECTURE.md](../../../ARCHITECTURE.md).
6. Prove that context, benchmark, and research-inactive symbols cannot become targets or orders and that no strategy can authorize execution.
7. Require known-answer, boundary, failure, and deterministic replay tests before accepting performance evidence.
8. Report unsupported assumptions and residual operational risk; historical or paper results never establish future profitability.

All execution review is limited to simulated paper capital. Real-money execution is outside repository scope.
