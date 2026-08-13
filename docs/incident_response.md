# Incident Response

> **PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS**

Incidents affect a simulated Alpaca paper account, but they are handled with production discipline because the research record depends on timing, continuity, and honest evidence. Safety and preservation take precedence over completing a rebalance.

## Universal response

1. Persist a halt with a specific reason when state, data, or execution is uncertain.
2. Stop new decision admission; do not erase queued evidence.
3. Record UTC time, Eastern session date, run/decision/order identifiers, symptoms, current health, and operator actions. Never record credentials or authorization headers.
4. Preserve the database, JSONL receipts, logs, configuration snapshot, and relevant broker responses.
5. Use read-only status, doctor, and reconciliation before any mutation.
6. Resolve or explicitly explain every blocking discrepancy.
7. Resume only with review and the required acknowledgement. Never weaken a check to clear an incident.

Severity guidance:

- **Informational:** expected condition with no effect on evidence or safety.
- **Warning:** degraded behavior; observer work may continue but submission is blocked where relevant.
- **Blocking:** no new paper orders or decisions until resolved.
- **Critical:** persistent halt; operator review and recovery evidence required.

## Data-feed outage

**Detection:** stream disconnected, no expected events, provider errors, or heartbeat/data health degraded.

**Containment:** block new paper orders immediately. Keep the last known prices labeled stale; never value them as current for planning.

**Recovery:** reconnect with bounded exponential backoff, query historical REST for the exact missed interval, validate and deduplicate backfilled/streamed bars, and require a new fresh event for every required symbol. Confirm the selected IEX/SIP entitlement; never silently switch feeds. Reconcile before resume.

## Stale data

**Detection:** a required price timestamp exceeds `stale_after_seconds`, a required symbol is absent, or event time is out of order.

**Containment:** block the decision or exposure increase and record symbol-level age. Do not extend the threshold ad hoc.

**Recovery:** establish whether the market is closed, the symbol is halted, or the stream failed; backfill; validate timestamps; and require a fresh observation. Preserve the skipped receipt and reason.

## Broker API outage

**Detection:** REST timeouts, 5xx/rate errors, paper account unavailable, or an ambiguous submit response.

**Containment:** do not blindly retry a submission. Mark the intent unknown/reconciliation-required, halt later buys, and preserve its deterministic client order ID.

**Recovery:** when REST returns, query by client order ID, open/recent paper orders, fills, positions, cash, and equity. Apply queued trade events idempotently and reconcile. Resume only when the outcome is known or explicitly quarantined.

## Stream disconnect

Treat market and trade-update streams independently. A trade stream disconnect during open orders is blocking even if market bars continue. On reconnect, retrieve broker order state and fills before processing new decisions. Duplicate updates must not double-count fills.

## Duplicate-order concern

**Indicators:** repeated client order ID, two broker orders for one intent, retry after timeout, or unexpected open order near a restart.

**Response:** halt, query local intent/order/event history and broker history by client ID, compare symbol/side/notional/timestamps, and do not submit a compensating order until exposure is reconciled. Classify the root cause as duplicate event, duplicated intent, external/manual action, or unresolved. Add a linked incident; never delete either record.

## Unknown broker order

An Alpaca paper order not linked to a local intent is blocking. Halt, capture its redacted metadata, determine whether another process or a manual paper-account action created it, and reconcile resulting fills/positions/cash. Cancellation is a deliberate paper-account mutation and requires operator approval; the dashboard cannot perform it.

## Position mismatch

Compare symbol, signed quantity, average price where available, and market value at the same timestamp. Account for pending/partial fills and corporate actions. Persist both views and the tolerance used. Do not overwrite local state merely to make the difference disappear; record the reconciliation correction and its source.

## Negative position

A negative paper position violates the long-only invariant and is critical. Halt immediately, block all new orders, inspect partial fills and concurrent/manual orders, verify broker truth, and escalate to the designated operator and independent risk reviewer. Do not create an automatic buy merely to hide the state. Any approved simulated correction must be explicit, linked, and reconciled.

## Excessive paper loss

At the daily loss limit, block new exposure for the session. At the hard drawdown limit, target cash according to policy and persist the hard-stop latch. Verify external cash flows are not misclassified as P&L. Preserve the adverse record; do not reset equity, change the start date, or weaken limits to improve charts.

## Hard-stop activation

Confirm the triggering equity, peak, drawdown, price timestamps, and receipt. Follow any permitted sell plan through paper fills and reconciliation. The latch survives restart and is not cleared by a normal stream recovery. Review data integrity, paper account, strategy/risk evidence, and incident cause before any separately authorized reset/resume behavior.

## Corrupted database

**Detection:** SQLite errors, failed integrity check, migration failure, impossible uniqueness/foreign-key state, or unreadable pages.

**Containment:** halt, stop all writers gracefully, preserve the database plus WAL/SHM files and logs, and make a forensic copy. Do not run destructive repair against the only copy.

**Recovery:** select the newest verified backup, restore to a new path, run `PRAGMA integrity_check`, compare configuration/run/decision/order counts, and reconcile completely against the paper broker. Record the lost interval and recovery. If continuity cannot be established, end the run and start a new run ID; never splice silently.

## Exposed credential

Immediately revoke/rotate the Alpaca paper key, remove it from the process and local files, halt the service, and inspect repository history, logs, screenshots, build layers, and CI artifacts for exposure. Preserve incident evidence in a restricted location without reproducing the secret. Replace only local runtime credentials, rerun doctor, and reconcile the paper account. Never place the replacement in source control. If live credentials were mistakenly used, treat that as a severe boundary violation and verify that the hard-coded paper endpoint prevented use.

## System restart during execution

On restart, do not create a new decision or resubmit existing intents. Restore latches and durable state, identify orders in planned/submitting/accepted/partial/unknown states, query broker state by deterministic client ID, apply events idempotently, reconcile positions/cash/equity, and confirm the session decision uniqueness constraint. Start observer mode first if any ambiguity remains.

## Broker or paper-account cash-flow discontinuity

Although not a submission incident, an external deposit, withdrawal, or paper-account reset breaks performance continuity if not identified. Halt performance publication, record amount/timing/source when known, exclude it from return, and flag ambiguous intervals as undefined. A reset starts a new forward segment or run; it is never presented as strategy P&L.

## Closure record

An incident closes only when it contains the cause or explicit unresolved classification, impact window, affected decisions/orders, containment and recovery actions, reconciled final state, test or procedural prevention, approving operator, and closure timestamp. Closing an incident does not delete it or rewrite linked decisions.
