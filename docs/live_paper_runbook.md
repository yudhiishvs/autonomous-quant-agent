# Real-Market Observer and Future Paper-Operation Runbook

> **PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS**

This runbook operates an Alpaca paper account only. It never authorizes real-money trading. Phase 2
commands assume the repository root, an installed environment, and `configs/observer.yaml`.
`configs/paper.yaml` and all submission operations are reserved for the separately approved future
procedure in `PHASE3_PAPER_ENABLEMENT_PROMPT.md`.

## 1. Establish the paper account

1. Sign in to Alpaca's paper environment and confirm the account is labeled paper.
2. Generate a new paper API key and secret. Never reuse live-account credentials.
3. Record the paper account identifier separately for operator verification; do not put it in source control.
4. Decide whether the project will use IEX or entitled SIP. Do not assume SIP entitlement.

The service must display one verified disclosure:

- **REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET**
- **REAL-TIME SIP FEED**

A requested feed that cannot be confirmed is a startup failure, not a fallback opportunity.

## 2. Install and configure the environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,dashboard]"
cp .env.example .env
chmod 600 .env
```

Put only paper credentials in the local environment:

```dotenv
APA_ALPACA_PAPER_API_KEY=<paper-key>
APA_ALPACA_PAPER_SECRET_KEY=<paper-secret>
APA_ENABLE_PAPER_ORDERS=NO
```

Never source `.env` as shell code. The full-session wrapper uses
`python-dotenv` to parse an existing ignored `.env` as data, imports only the
three approved names above, and never prints their values. It automatically uses
`.venv/bin/python` when available. Verify the active environment before any
connectivity attempt:

```bash
make check-paper-environment
```

When Section 9 starts a full session, the wrapper performs the safe dotenv load
and repeats this environment check before connectivity.

For standalone commands, inject the three approved names through a trusted
local credential manager or private process environment. Keep
`execution.paper_order_submission_enabled: false` in `configs/observer.yaml`
throughout Phase 2.

Review at least:

- ticker universe and SPY benchmark;
- IEX or SIP feed selection;
- Eastern evaluation time and catch-up cutoff;
- position, gross exposure, volatility, turnover, cash, order-size, drawdown, and daily-loss limits;
- database and output locations; and
- `paper_only: true`, regular-hours-only behavior, and disabled submission.

Commit configuration changes without any credential material. The normalized snapshot and configuration hash will be stored at runtime.

## 3. Run offline verification

Before touching Alpaca connectivity:

```bash
ruff format --check .
ruff check .
mypy src
pytest -q
python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic
python -m adaptive_trader.cli replay --config configs/replay.yaml
```

Confirm tests made no network requests, replay is deterministic, required artifacts exist, and restart scenarios do not duplicate fake orders.

## 4. Run doctor

```bash
python -m adaptive_trader.cli doctor --config configs/observer.yaml
```

Doctor is read-only. Require it to confirm:

- strict configuration validation;
- writable database, log, cache, and output locations;
- paper credentials are present but redacted;
- the account is an Alpaca paper account;
- configured symbols are active, tradable US equities or ETFs and fractionable when required;
- selected feed entitlement matches configuration; and
- no prohibited live configuration or code path is detected.

If connectivity is unavailable, record that fact. Never describe an unperformed check as successful.

## 5. Bootstrap history in observer mode

The live service bootstraps its required completed daily history before it permits a decision. Start observer mode:

```bash
python -m adaptive_trader.cli observe --config configs/observer.yaml
```

On first start, verify logs and status show the feed, full-universe
asset-validation, and completed-history preflight before either real-time stream
connects. Also verify paper-account, reconciliation, and observer-gate state.
After the first eligible scheduled evaluation, inspect its receipt in the
dashboard or generated report and verify:

- the configured number of completed sessions for every required symbol;
- history ending at the last completed market session, not an incomplete current daily bar;
- validated and deduplicated timestamps;
- the exact data source and feed;
- successful paper-account reconciliation before the decision; and
- submission disabled with an explicit observer reason.

Stop with one `Ctrl-C` and wait for graceful shutdown. Confirm the shutdown event and final reconciliation were persisted.

## 6. Verify the live observer

Run observer through a regular market session:

```bash
make observe
```

In another terminal:

```bash
python -m adaptive_trader.cli status --config configs/observer.yaml
```

Check minute events arrive with timezone-aware timestamps, all required symbols remain fresh, heartbeat and reconciliation advance, market open/close times are correct, only one scheduled receipt is created, and hypothetical order intents contain no broker order ID. Force or replay a stream disconnect before relying on persistent operation; verify unhealthy status, reconnect/backfill/deduplication, and a fresh-event requirement before recovery.

## 7. Run one dry paper cycle

Keep the enablement token `NO` and submission disabled in configuration:

```bash
python -m adaptive_trader.cli paper-once --config configs/observer.yaml --dry-run
```

Inspect the resulting receipt for the history cutoff, schedule and actual timestamp, market/feed/freshness, strategy metadata, regime, proposed and final targets, every risk action, order intents, and `dry-run` mode. Confirm no Alpaca broker order ID exists and reconcile the paper account afterward:

```bash
python -m adaptive_trader.cli reconcile --config configs/observer.yaml
```

## 8. Phase 2 stopping point

Do **not** enable simulated paper orders in Phase 2. Continue collecting evidence until
`observer-readiness --config configs/observer.yaml` returns `PASS` from at least five genuine
observer sessions, three genuine real-data dry runs, a controlled restart, complete governance,
and zero broker mutations. Leadership approval, user evidence review, and a new explicit decision
are also required. Only then may a separately reviewed, explicitly authorized paper-order
enablement procedure be considered. Phase 2 itself never enables submission.

## 9. Start the persistent observer and dashboard

Native processes:

```bash
# Terminal 1 — retains and audits the dedicated observer database
./scripts/run_full_observer_session.sh configs/observer.yaml

# Terminal 2; this wrapper removes credentials from the dashboard environment
./scripts/run_dashboard.sh
```

Open `http://127.0.0.1:8501`. Confirm the permanent paper warning, verified feed disclosure, current mode, heartbeat, streams, reconciliation, simulated account, positions, orders/fills, risk state, receipts, and forward paper performance.

The default Docker deployment also uses only the dedicated observer configuration:

```bash
docker compose up trader dashboard
```

Do not start the optional `paper` Compose profile during Phase 2.

## 10. Daily operator checklist

Before the evaluation time:

1. Run `status` and review current incidents and latches.
2. Confirm the displayed paper account and feed.
3. Confirm clock, next open/close, latest minute timestamps, and required-symbol freshness.
4. Confirm completed history cutoff and expected configuration hash/version.
5. Review paper cash, positions, open orders, exposure, drawdown, and daily loss.
6. Ensure reconciliation is clean and there is no unexplained external paper-account cash flow.

After evaluation:

1. Confirm exactly one receipt for the session.
2. Review proposed versus final targets and risk interventions.
3. In observer mode, confirm only hypothetical intents.
4. Confirm broker submissions, cancellations, and fills remain zero.
5. Check heartbeat and stream health continue after the cycle.

## 11. Stop the observer safely

Use one `Ctrl-C` for a foreground observer, or send `SIGTERM` once through the
process supervisor. The full-session wrapper forwards the signal, waits for the
observer to stop, and then preserves status, report, backup, integrity, and
session-audit evidence. For the default container, `docker compose stop trader`
sends the observer its normal termination signal.

Shutdown and final reconciliation are zero-mutation paths: broker submissions,
cancellations, replacements, closes, liquidations, and position changes must all
remain zero. Do not use a halt-with-cancellation, flatten, liquidation, paper-run,
or paper Compose command as an observer stop mechanism. If the process cannot
respond, preserve the incident before escalating to an ungraceful stop.

## 12. Investigate and restart without clearing evidence

Do not restart merely because a stream reconnected. First preserve the database,
logs, and latest receipt; inspect status and incidents; run read-only doctor when
connectivity or configuration may have changed; and reconcile without broker
mutation. Do not clear an unresolved latch, incident, or discrepancy merely to
obtain a healthy result.

Restart only the observer/full-session wrapper with submission still disabled.
It must require fresh connected data and a new clean reconciliation, while an
unresolved blocking discrepancy continues to prevent a decision. A daily-loss
latch expires only under its documented next-session rule; it is not manually
cleared for convenience.

## 13. Back up the database

SQLite's online backup API is safe while the service is running:

```bash
./scripts/backup_database.sh
```

The utility defaults to `runtime/primary_real_market_observer.db`, verifies
source and backup integrity, and creates an owner-only file under
`runtime/backups`. Copy verified backups to controlled storage and test
restoration on a copy at least once during the semester. The later readiness
gate also requires a hash-bound manifest proving that the separate backup
contains every accepted observer-session run. Never prune the only forward
record.

## 14. Recover after restart

1. Leave submission disabled or halt latched if the prior exit occurred during execution.
2. Preserve the database, logs, and most recent receipt.
3. Start with `status`, then `doctor` if connectivity/configuration may have changed.
4. Run `reconcile`; compare broker orders by deterministic client ID and cumulative fills.
5. Confirm the session's decision already exists and cannot run twice.
6. Resume observer only after the state is unambiguous and every observer gate passes. Do not run
   `paper-run` during Phase 2.

The service must never retry an ambiguous submission merely because the process restarted.

## 15. Feed outage

Leave the service halted from new exposure. Verify the unhealthy incident, reconnect backoff, REST backfill range, and deduplication. Require fresh minute data for all necessary symbols and a clean reconciliation before resuming. If SIP entitlement failed, fix entitlement/configuration; do not fall back silently to IEX.

## 16. Broker outage

Do not retry submissions whose result is unknown. Persist the ambiguity, query by client order ID when REST returns, process any queued trade updates, and reconcile orders, fills, positions, cash, and equity. Resume only after no blocking discrepancy remains.

## 17. Order discrepancy

For an unknown broker order, duplicate-order concern, unexpected symbol, position mismatch, or negative position:

1. halt with a specific reason;
2. preserve local and broker identifiers and timestamps without credentials;
3. reconcile read-only and classify severity;
4. do not cancel or flatten reflexively;
5. determine whether another process or a manual paper-account action occurred; and
6. follow [incident response](incident_response.md) before resume.

## 18. End a semester run

1. Halt and stop persistent services gracefully after the desired final session.
2. Reconcile until local and paper state are explained.
3. Generate the final report: `python -m adaptive_trader.cli report --config configs/observer.yaml`.
4. Create and verify a final database backup.
5. Archive configuration, strategy version, data summary, logs, reports, receipts, and backup checksums together.
6. Confirm submission remained false and the environment token remained `NO` throughout.
7. Revoke or rotate paper credentials if the project is ending.
8. Do not reset the paper account, delete the database, edit receipts, or splice another run into the record.

Label every presentation **forward paper-trading performance** and never imply real-money performance or profit.
