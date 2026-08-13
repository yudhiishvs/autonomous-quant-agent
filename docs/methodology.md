# Methodology

> **PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS**

## Research question and evidence types

The primary question is whether regime-aware allocation between momentum and mean-reversion strategies produces better risk-adjusted forward paper-trading performance than simpler static alternatives while remaining within identical risk constraints. Underperformance or no difference is a valid result.

Three kinds of evidence remain separate:

- **Historical simulation** applies explicit timing, transaction-cost, and slippage assumptions to past completed bars.
- **Deterministic replay** verifies live orchestration against controlled events and a fake broker; its returns are engineering evidence, not market evidence.
- **Forward paper trading** consumes market information as time passes and records decisions at evaluation time, but capital and fills are simulated by Alpaca paper trading.

When IEX is configured, reports state **REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET**. SIP reports state **REAL-TIME SIP FEED** only after entitlement confirmation.

## Price and return conventions

Let `P(i,t)` be the completed adjusted daily close of asset `i` for session `t`. Its simple return is:

```text
r(i,t) = P(i,t) / P(i,t-1) - 1
```

For a decision evaluated during session `t`, daily features end at `t-1`. A fresh current-session price may be used only for notional planning, sanity checks, and valuation; it does not enter the daily strategy signal.

## Momentum

With default lookback `L_m = 63`, momentum as of completed session `s` is:

```text
M(i,s) = P(i,s) / P(i,s-L_m) - 1
```

Assets with nonpositive momentum are excluded by default. Up to the three highest scores remain. Annualized volatility from the prior 20 completed daily returns is:

```text
sigma(i,s) = std(r(i,s-19:s), ddof=1) * sqrt(252)
```

Selected assets receive inverse-volatility weights:

```text
q(i,s) = 1 / sigma(i,s)
w_mom(i,s) = q(i,s) / sum_j q(j,s)
```

Zero, nonfinite, or unavailable volatility makes an asset ineligible rather than infinitely weighted. If none qualifies, the strategy returns all unused capital as cash. Metadata records lookbacks, prices, scores, selections, exclusions, volatilities, and weights.

## Mean reversion

For the trailing 20 completed closes:

```text
mu(i,s) = mean(P(i,s-19:s))
sd(i,s) = std(P(i,s-19:s), ddof=1)
z(i,s) = (P(i,s) - mu(i,s)) / sd(i,s)
```

An asset qualifies by default only when:

```text
z(i,s) < -0.5
P(i,s) > mean(P(i,s-99:s))
```

Up to three assets with the most negative z-scores receive inverse-volatility weights using the same 20-session estimate. Invalid dispersion or no qualifying assets produces cash, never a nonfinite position.

## Four-state regime

For benchmark SPY, trend is bull when the trailing 50-session moving average is at least the trailing 200-session moving average, and bear otherwise. Current annualized realized volatility is computed from 20 completed returns. Its threshold is the median of the trailing 252 observations of that 20-session volatility series.

```text
trend = bull if MA_50(s) >= MA_200(s), else bear
volatility = high if v_20(s) > median(v_20(s-251:s)), else low
```

The cross-product yields `bull_low_vol`, `bull_high_vol`, `bear_low_vol`, or `bear_high_vol`. Equality belongs to bull and low volatility. This is an interpretable classification, not a claim that the state will persist.

## Adaptive allocation

For regime `g` and asset `i`:

```text
w_proposed(i) = a_mom(g) * w_mom(i) + a_mr(g) * w_mr(i)
cash_proposed = 1 - sum_i w_proposed(i)
```

Default budgets are:

| Regime | Momentum | Mean reversion | Strategic cash |
| --- | ---: | ---: | ---: |
| `bull_low_vol` | 0.70 | 0.30 | 0.00 |
| `bull_high_vol` | 0.45 | 0.35 | 0.20 |
| `bear_low_vol` | 0.25 | 0.35 | 0.40 |
| `bear_high_vol` | 0.10 | 0.15 | 0.75 |

Unused strategy budget stays cash and is not redistributed. The static baseline uses 50% momentum and 50% mean reversion. Single-strategy baselines assign their proposal budget to one strategy. Allocation does not grant execution permission.

## Independent risk review

The broker-independent `RiskContext` carries account equity/cash, position weights, open-order conflicts, current-price timestamps, asset eligibility, freshness, market, and halt state into the independent engine. The live service records the same operational facts and applies the final submission gate after reconciliation. Together they apply controls in a deterministic order and record every effective intervention:

1. **System state:** operator halt, hard-stop, or blocking reconciliation prevents submission.
2. **Session:** paper submission requires an open regular-hours US market and a decision before the catch-up cutoff.
3. **Freshness:** every required price and stream health timestamp must be within configured bounds.
4. **Eligibility:** symbols must be configured, active, tradable US equities/ETFs and fractionable when required.
5. **Long-only:** negative target weights are rejected; sells cannot exceed an existing long position.
6. **Position cap:** each risky weight is clipped to `max_position_weight`.
7. **Gross exposure:** risky weights are proportionally reduced when their sum exceeds `max_gross_exposure`.
8. **Cash buffer:** risky exposure is capped at `1 - required_cash_buffer`; simulated margin buying power is ignored.
9. **Volatility target:** a 60-session covariance estimate produces `sigma_p = sqrt(w' * (252 Sigma_daily) * w)`. If above target, risky weights scale by `target / sigma_p`; exposure never scales up.
10. **Soft drawdown:** at the soft limit, gross risky exposure is capped at the configured reduced level.
11. **Hard drawdown:** at the hard limit, the target becomes cash and a persistent hard-stop latches.
12. **Daily loss:** session loss at or beyond the limit blocks new exposure for the remainder of that session.
13. **Turnover:** ordinary targets interpolate toward the proposal to remain within the configured maximum.
14. **Order constraints:** maximum order fraction, minimum notional, order count, open-order conflict, and stable client-ID checks apply.

Safety exits such as a hard-stop liquidation are not weakened merely to satisfy an ordinary turnover budget. Missing or nonfinite required risk inputs fail closed.

## Drawdown, daily loss, and turnover

For equity `E_t` and running peak `H_t = max_{u<=t} E_u`:

```text
drawdown_t = E_t / H_t - 1
```

External paper-account deposits and withdrawals are not trading P&L. With session-start equity `E_0` and cumulative external net cash flow `CF_t` since the start:

```text
daily_pnl_t = E_t - E_0 - CF_t
daily_loss_fraction_t = daily_pnl_t / E_0
```

If a cash flow cannot be classified reliably, the daily return is undefined and reported with a discontinuity warning rather than counted as strategy performance.

With risky and cash weights included, one-way turnover is:

```text
T = 0.5 * (sum_i abs(w_target(i) - w_current(i))
           + abs(cash_target - cash_current))
```

When `T > T_max`, ordinary movement is interpolated with `lambda = T_max / T`.

## Order planning and simulated execution

The planner converts final weights to target notionals using fresh simulated equity and current prices. It removes deltas below minimum notional, caps individual order size, emits reductions and sells before buys, and recalculates available simulated cash after fills. Orders are market, `DAY`, regular-hours only, and never set extended hours.

Each intent has a deterministic client order ID and is durably stored before submission. Paper execution then follows:

1. reconcile;
2. address stale/conflicting open paper orders only under configured policy;
3. submit permitted reductions/sells;
4. consume partial/final updates until timeout;
5. reconcile and recalculate cash/risk;
6. submit permitted buys;
7. consume updates and reconcile again; and
8. link final known status to the immutable decision.

Alpaca paper fills are simulated. They do not establish obtainable live execution quality.

## Historical timing and costs

For simulated execution session `t`, signals, regime, and covariance end at completed session `t-1`. The backtest applies the target at the configured session boundary, charges one-way transaction cost and adverse slippage, then applies session `t` returns. With default cost and slippage of five basis points each:

```text
cost_fraction = turnover * (transaction_cost_bps + slippage_bps) / 10,000
```

Risky weights drift between rebalances according to relative returns; later turnover starts from those drifted holdings. Cash earns exactly zero in this proof-of-concept backtest. The model does not represent intraday price paths, capacity, queue position, or market impact.

The historical model executes at the adjusted session open (or an explicitly
recorded prior-close proxy when no open exists). It therefore approximates, but
does not reproduce, the live paper service's configured 10:05 ET evaluation and
cannot measure the intervening open-to-10:05 price path.

## Comparisons

Historical reporting includes:

- adaptive allocation with the risk engine;
- static 50/50 strategy allocation with the same risk engine;
- momentum only with the same risk engine;
- mean reversion only with the same risk engine;
- equal-weight buy-and-hold; and
- SPY buy-and-hold.

The adaptive-versus-static comparison best isolates the allocation rule. Passive references provide context but do not necessarily receive periodic active-risk intervention.

## Performance metrics

Metrics include total return, CAGR, annualized volatility, Sharpe, Sortino, maximum drawdown, Calmar, historical 95% value at risk, historical 95% conditional value at risk, positive-day percentage, average gross exposure, average cash allocation, total turnover, estimated transaction costs, rebalances, risk interventions, and hard-stop events. Definitions use 252 annual sessions unless configured otherwise. A zero denominator or insufficient sample returns a null/NaN value plus a metric-specific reason, never infinity or a fabricated `0.0`. Mathematically defined zeros remain zero: a flat observed return path has zero total return and volatility, while its Sharpe and Calmar ratios are undefined because their denominators are zero. Historical CSV files store undefined numeric cells as null with adjacent `<metric>_reason` columns, and Markdown renders them as `n/a` with the same explanations.

Historical metrics are reported for the full period, configured out-of-sample period, frozen
development/validation/holdout windows when present, calendar years, and regimes. Forward paper
metrics are computed from immutable daily account snapshots only after removing identified
external cash flows. Operational paper metrics include account turnover, decisions, submitted
orders, fully and partially filled orders, rejections, fill/partial-fill/rejection rates, adverse
paper slippage against an order-intent reference price when available, risk interventions, hard
stops, contiguous data-outage episodes, reconciliation discrepancies, and time in each regime. A
rate with no submitted-order denominator and slippage without a linked reference price remain null
with a reason.

For consecutive daily snapshots with net external flow `CF_t` during the interval:

```text
forward_return_t = (E_t - CF_t) / E_(t-1) - 1
```

If flow timing or identity is ambiguous, the interval is excluded and flagged. Forward paper performance begins at the established run baseline; resetting or transferring the paper account creates an explicit continuity event rather than silently splicing series. SPY comparison uses aligned observations and a documented convention.

## Decision receipts

Every scheduled evaluation—including skips and rejections—records IDs and versions, configuration hash, signal/schedule/evaluation timestamps, session and market status, feed/freshness/history cutoff, strategy and regime inputs, proposed and final weights/cash, account state, drawdown/daily loss, risk inputs/interventions, turnover/volatility, order intents, mode, broker identifiers when applicable, final known status, warnings, incidents, and skip reason.

The database receipt is immutable. Fills, reconciliations, incidents, and performance learned later are linked events. Forward receipts are stored authoritatively in SQLite and exported as reproducible JSONL and human-readable Markdown snapshots. Regenerating an export never edits the underlying receipt row.

## Limitations

- Paper fills are simulated and paper performance is not live-money performance.
- IEX is not the complete consolidated US market.
- Historical data can contain revisions, errors, survivorship, and present-day selection bias.
- Daily feature timing and simplified execution cannot reproduce every intraday condition.
- Constant costs and slippage omit dynamic spread, impact, liquidity, capacity, and queue effects.
- Covariance, volatility, trend, and z-score estimates are backward-looking and unstable around structural changes.
- Repeated parameter changes and multiple comparisons can overfit an apparently transparent system.
- Cash-flow classification, broker outages, and missing snapshots can make forward metrics undefined.
- No historical, replay, or forward paper result guarantees future performance.
