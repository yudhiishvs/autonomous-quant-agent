# Market-data collector runbook

## Purpose and current state

The standalone collector obtains raw one-minute IEX bars from Alpaca and persists them in a private PostgreSQL database. It performs historical catch-up, consumes live bars and updated-bar corrections, periodically reconciles completed regular-session intervals, and resumes from durable coverage checkpoints after a restart.

The implementation has been verified with fake Alpaca sources and a disposable PostgreSQL instance. It has not yet been connected to Alpaca, attached to a hosted production database, or deployed on an always-on runtime. PostgreSQL is the implemented storage tier; Parquet snapshots, object-storage archival, table partitioning, and automated retention are separate follow-up work.

## Data flow

~~~mermaid
flowchart LR
    REST["Alpaca historical IEX REST"] --> COLLECTOR["Singleton data-only collector"]
    WS["Alpaca IEX WebSocket bars + updated bars"] --> COLLECTOR
    COLLECTOR --> OBS["Append-only bar observations"]
    COLLECTOR --> CURRENT["Deterministic current bars"]
    COLLECTOR --> STATE["Runs, events, leases, and coverage checkpoints"]
    OBS --> PG["Private hosted PostgreSQL"]
    CURRENT --> PG
    STATE --> PG
    PG --> RESEARCH["Future dataset and research services"]
~~~

Historical REST is authoritative for successfully queried half-open regular-session intervals. The WebSocket path minimizes latency. Every minute, overlapping REST reconciliation closes gaps caused by disconnects and captures provider corrections. Training and backtesting read stored data and never depend on an open Alpaca connection.

## Collection and research boundaries

The immutable `collection-universe.v1` contract contains 29 symbols:

- Collected equities: `TSLA`, `UBER`, `GOOGL`, `NVDA`, `AMZN`, `AAPL`, `META`, `AMD`, `CSCO`, `NET`, `OKTA`, `ROKU`, `BOX`, `ZG`, `RBLX`, `SOUN`, `PUBM`, `HLIT`, `PAYC`, `WDAY`, `SNDK`, `RIVN`, `LCID`, `AAOI`, `AXTI`, and `INSG`.
- Context: `SPY` and `QQQ`.
- Benchmark: `SOXX`.

Collection membership never grants trading authority; all 29 members have `execution_authorized=false`. The separate research MVP remains limited to `NVDA`, `AMD`, `CSCO`, `SNDK`, `AAOI`, `AXTI`, `HLIT`, and `INSG`. The other collected equities and the context/benchmark symbols cannot become model targets, positions, or orders without a new versioned research contract and its complete validation cycle.

SNDK means Sandisk Corporation. WDC is not part of this collection contract, and its history must never be substituted for SNDK.

## Persistence model

Alembic owns the isolated `market_data` schema:

| Table | Purpose |
| --- | --- |
| `collection_universes` | Content-addressed collection membership and roles |
| `bar_observations` | Append-only raw observation and correction history |
| `current_bars` | One deterministic latest-value projection per bar identity |
| `collector_checkpoints` | Successfully committed half-open REST coverage per symbol |
| `collector_leases` | Singleton ownership with fencing tokens and expiry |
| `ingestion_runs` | Backfill/run lifecycle and counters |
| `collector_events` | Operational retries, reconnects, and failures |
| `data_gaps` | Reserved durable gap lifecycle for the later explicit gap detector |

The logical bar identity is:

~~~text
(provider, feed, adjustment, symbol, timeframe, bar_timestamp_utc)
~~~

An exact retransmission is idempotent. Changed content for the same identity is preserved as another append-only observation. It updates `current_bars` and increments the revision only when it wins deterministic source/correction precedence; lower-precedence observations remain as provenance without replacing the current projection. Database triggers reject updates, deletes, and truncation against `bar_observations`; reject checkpoint deletion and truncation; and reject checkpoint regression. Collector startup verifies that these triggers are present and enabled.

A successful REST interval may legitimately contain no bar for a thin IEX symbol. Its coverage checkpoint still advances because coverage means the interval was queried and committed, not that every symbol traded during every minute.

## Required runtime configuration

Inject these values through the runtime host's secret/configuration system. Never commit them, place them in container images, print them, or add them as CI secrets.

| Variable | Required by | Meaning |
| --- | --- | --- |
| `APA_ALPACA_DATA_API_KEY` | `backfill`, `run` | Dedicated collector API key |
| `APA_ALPACA_DATA_SECRET_KEY` | `backfill`, `run` | Dedicated collector secret |
| `APA_MARKET_DATA_DATABASE_URL` | `status`, `ready`, `backfill`, `run` | Least-privilege collector PostgreSQL URL using the psycopg driver |
| `APA_MARKET_DATA_MIGRATION_DATABASE_URL` | `migrate` only | Separate schema-owner URL; never inject it into the long-running collector |
| `APA_MARKET_DATA_HISTORY_START` | first run or `backfill` without `--start` | ISO date or timezone-aware initial boundary; required whenever any collection checkpoint is missing |

Use a database URL shaped like:

~~~text
postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE?sslmode=verify-full
~~~

Keep PostgreSQL private. Non-loopback connections must use `sslmode=verify-full` so both the certificate chain and hostname are verified; query-string connection-routing overrides are rejected. Reserve schema creation, DDL, trigger ownership, and destructive table privileges for the separate migration role. A PostgreSQL administrator can establish the continuously running role with equivalent least-privilege grants (substitute the actual role and database names):

~~~sql
REVOKE CREATE ON SCHEMA market_data FROM market_data_runtime;
GRANT USAGE ON SCHEMA market_data TO market_data_runtime;
GRANT SELECT, INSERT ON market_data.collection_universes TO market_data_runtime;
GRANT SELECT, INSERT, UPDATE ON market_data.ingestion_runs TO market_data_runtime;
GRANT SELECT, INSERT ON market_data.bar_observations TO market_data_runtime;
GRANT SELECT, INSERT, UPDATE ON market_data.current_bars TO market_data_runtime;
GRANT SELECT, INSERT, UPDATE ON market_data.collector_checkpoints TO market_data_runtime;
GRANT SELECT, INSERT, UPDATE ON market_data.collector_leases TO market_data_runtime;
GRANT SELECT, INSERT ON market_data.collector_events TO market_data_runtime;
GRANT SELECT ON market_data.data_gaps TO market_data_runtime;
REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA market_data FROM market_data_runtime;
~~~

The runtime uses bounded connect, pool, statement, lock, and idle-transaction waits so shutdown and failure behavior remain finite. Enable automated backups and point-in-time recovery where the provider supports them, and alert on storage growth and connection exhaustion. The Compose profile intentionally does not provision a local PostgreSQL volume.

The collector API key is application-isolated, not claimed to have a provider-enforced market-data-only scope. Paper-execution variables are separate and are not loaded by this process.

## First activation

Choose the initial history boundary deliberately. A date requests every eligible regular trading session from that date through the completed-bar cutoff; SNDK naturally has no pre-listing rows.

Apply migrations before starting any collector process:

~~~bash
uv run --no-sync adaptive-market-data migrate
uv run --no-sync adaptive-market-data status
~~~

`status` checks storage and never requires a collector to be active. The container health check uses the collector's `ready` subcommand, which performs an indexed ownership probe and succeeds only while the canonical singleton lease and its exact ingestion run are active; it does not repeatedly count the growing bar tables.

Optionally prove a small finite interval against a separate disposable database. Do not perform this partial smoke test against the target database because it intentionally creates coverage checkpoints:

~~~bash
uv run --no-sync adaptive-market-data backfill \
  --start 2026-09-01T13:30:00Z \
  --end 2026-09-01T20:00:00Z
uv run --no-sync adaptive-market-data status
~~~

For the target database, complete the intended historical backfill first and then start continuous collection:

~~~bash
uv run --no-sync adaptive-market-data backfill --start 2025-01-02
uv run --no-sync adaptive-market-data run
~~~

Alternatively, on a truly empty target database, `run --start-if-empty 2025-01-02` performs the catch-up before opening the stream. That option applies only while one or more required coverage checkpoints are missing. Once all 29 checkpoints exist, restarts derive their catch-up range from the database.

For the container path, migrate once and then start the restart-managed collector:

~~~bash
docker compose --profile market-data run --rm \
  -e APA_MARKET_DATA_MIGRATION_DATABASE_URL \
  market-data-collector python -m adaptive_trader.collection migrate
docker compose --profile market-data up -d market-data-collector
docker compose --profile market-data logs -f market-data-collector
~~~

Do not start two collectors intentionally. The database lease rejects a second active owner. Every observation, checkpoint, and collector-event write requires the current fencing token, so a missing, expired, or superseded lease fails closed.

## Normal operation and recovery

`adaptive-market-data run` performs these stages:

1. Verify the migration revision and register the collection-universe hash.
2. Acquire and continuously renew the singleton database lease.
3. Catch up every symbol through historical REST using XNYS regular-session windows, including early closes.
4. Subscribe once to all 29 IEX bar and updated-bar streams.
5. Reconcile an overlapping completed interval every minute.
6. Restart a transiently failed WebSocket with capped exponential backoff and jitter; protocol or data-contract failures stop the process for operator attention.
7. Respect bounded provider rate-limit reset hints for historical requests without persisting response headers.
8. On `SIGTERM` or `SIGINT`, set a signal-safe stop flag, let bounded I/O return, join workers, finish the run record, and release the lease.

After an abrupt restart, retries from the same source deduplicate by stable observation ID. A REST replay of a bar first received through WebSocket may be retained as separate provenance, while the deterministic projection preserves one current logical bar. The collector resumes from the earliest per-symbol REST checkpoint with an overlap, so the recovery path favors replay over silent data loss.

Use `adaptive-market-data status` for a credential-free JSON snapshot of current bars, raw observations, checkpoints, unresolved gap rows, running runs, active leases, and the latest receipt timestamp. `ready` confirms process ownership, not checkpoint completion or data freshness; downstream consumers must enforce their own timestamp and coverage requirements. Operational monitoring should additionally alert on:

- a missing or expired collector lease;
- no active ingestion run while the service is expected to operate;
- repeated `market_data_stream_restart` or `historical_reconciliation_failed` events;
- checkpoint lag during regular market hours;
- an unexpectedly old latest receipt timestamp during regular market hours;
- database storage, backup, and connection-pool health.

## Incident actions

| Symptom | Safe action |
| --- | --- |
| Migration mismatch | Stop the collector, back up the database, run `adaptive-market-data migrate`, then restart |
| Database unavailable | Restore database service first; the collector fails closed and later resumes from checkpoints |
| Lease unavailable | Find the existing collector; do not bypass the lease. Wait for a confirmed stale lease to expire before restarting |
| Repeated WebSocket restarts | Keep the process running if REST reconciliation remains healthy; inspect provider/network status and collector events |
| Stale checkpoints | Stop duplicate collectors, verify database writes, then restart the singleton so overlapping REST repair runs |
| Unexpected correction | Inspect all rows for the identity in `bar_observations`; do not edit the append-only history |

Never repair production data by updating or deleting raw observations. Corrections enter through the same append path, and destructive migration/test commands must only target explicitly designated disposable databases.

## Safety guarantees

- The collection package uses direct fixed-host HTTPS and WebSocket transports; it imports no external Alpaca SDK, trading client, or order API.
- The dedicated collector image installs only the locked `market-data-runtime` dependency group and omits the application's execution modules, project distribution metadata, and external Alpaca SDK package.
- REST and WebSocket endpoints are fixed to Alpaca's official data hosts and cannot be supplied through runtime configuration.
- Only the dedicated data variable names are read by `backfill` and `run`.
- `migrate` and `status` never load Alpaca credentials.
- Logs and CLI failures do not render credential values or database passwords.
- CI supplies empty Alpaca variables, blocks non-loopback test networking, uses fake Alpaca sources, and performs no broker interaction.
- No collector command can submit a paper or live order.

## Current limitations

- No real credential-based Alpaca session or hosted always-on run has been recorded yet.
- No hosted PostgreSQL provider or runtime is provisioned by this repository.
- Historical catch-up and repair cover XNYS regular sessions; WebSocket observations outside those windows may be stored but are not yet guaranteed by REST recovery.
- `data_gaps` is schema-ready, but explicit gap-row production and resolution workflows are not implemented; coverage checkpoints and overlap reconciliation are the current recovery source of truth.
- Asset listing/tradability validation, corporate-action detection, explicit stale/out-of-order classification, immutable Parquet snapshots, object storage, partition/retention automation, and downstream dataset freezing remain follow-up work.
