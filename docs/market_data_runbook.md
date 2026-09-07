# Market-data collector runbook

## Canonical process and activation status

The production command is `python -m adaptive_trader.collection.cli run`; the installed
`adaptive-market-data run` alias delegates to the same CLI. `collection.CollectorService`
owns historical catch-up, the singleton fenced lease, durable coverage checkpoints,
live bars and updated bars, overlapping REST repair, and graceful shutdown. Its canonical
persistence path supplies the platform data tables. The platform worker entry point is
not a second production collector. The main AI remains frozen.

`docker-compose.market-data.yml` starts only `market-data-collector`. It consumes an
existing authorized persistent PostgreSQL database and provisions no database or cloud
service. It starts no strategy, scheduler, execution, API, or dashboard process. Its image
installs only locked market-data dependencies and excludes the Alpaca SDK and AI/execution
modules. Data collection has no paper-order or real-money capability.

Deployment configuration and offline tests are not live activation evidence. Real
provider authentication/subscription, target-host uptime, intended catch-up, target-database
recovery, and controlled restart remain `BLOCKED` until the authorized host, database,
and data-secret references are supplied and measured. The active execution plan owns
remaining Phase Zero acceptance work.

## Universe and provenance

The versioned `collection-universe.v1` has exactly 29 members. Its hash is persisted with
membership; every member has `execution_authorized=false`.

| Role | Symbols |
| --- | --- |
| Active research | AAOI, AMD, AXTI, CSCO, HLIT, INSG, NVDA, SNDK |
| Benchmark | SOXX |
| Context | QQQ, SPY |
| Collection only | AAPL, AMZN, BOX, GOOGL, LCID, META, NET, OKTA, PAYC, PUBM, RBLX, RIVN, ROKU, SOUN, TSLA, UBER, WDAY, ZG |

The frozen experiment remains eight active research plus three benchmark/context symbols;
the 18 exploratory names gain no research or execution authority. SNDK means Sandisk
Corporation; WDC history must never substitute for it.

Authoritative intake is Alpaca IEX, raw adjustment, one-minute bars. REST and WebSocket
hosts are fixed in the data adapter and cannot be replaced through runtime configuration.
The stream subscribes to bars and updated bars for all 29 symbols. Historical recovery and
canonical research aggregation use XNAS regular-session windows, including holidays,
early closes, and daylight-saving boundaries. This replaces the old collector's XNYS
selection without changing the frozen research calendar contract.

## Persistent state

| State | Purpose |
| --- | --- |
| `market_data.collection_universes` | Immutable collection membership/hash |
| `market_data.bar_observations` | Append-only provider evidence and corrections |
| `market_data.current_bars` | Deterministic current raw projection |
| `market_data.collector_checkpoints` | Durably committed half-open REST coverage per symbol |
| `market_data.collector_leases` | Singleton owner, fencing token, and expiry |
| `market_data.ingestion_runs`, `collector_events` | Run lifecycle, retries, and recovery evidence |
| `market_data.collector_configuration`, `canonical_work` | Immutable initial scope and durable derived-work queue |
| `aqa.aqa_bar_identities`, `aqa.aqa_bar_events`, `aqa.aqa_bar_latest` | Canonical identities, revisions, and current bars |
| `aqa.aqa_data_gaps`, `aqa.aqa_symbol_watermarks`, `aqa.aqa_basket_watermarks` | Explicit gaps and downstream readiness |
| `aqa.aqa_dataset_manifests` | Immutable dataset registrations/provenance |

Exact retransmissions are idempotent. Changed observations remain append-only evidence even
when source precedence prevents them from replacing the current bar. Never repair raw
history with updates/deletes. A successful thin-IEX query can return no trade: coverage is
not proof that every minute contains a bar. Required missing research constituents remain
incomplete; never synthesize bars.

The collector renews ownership during bounded I/O and fences writes. After restart it
resumes from the earliest durable coverage checkpoint with overlap. The initial history
boundary is persisted once; later restarts must preserve it or omit the setting and use
durable configuration. A conflicting history boundary fails closed. Periodic REST reconciliation
repairs downtime and late corrections. Derived state must replay idempotently after failure.

## Configuration and secret mounts

Supply paths through the approved host secret backend; never put values in commands, YAML,
Git, logs, status output, or evidence. Runtime secret loading uses `load_secret_file()`.

| Variable | Meaning |
| --- | --- |
| `AQA_DATABASE_URL_FILE` | Collector PostgreSQL URL file with least-privilege runtime login |
| `AQA_ALPACA_DATA_API_KEY_FILE` | Dedicated data API key file |
| `AQA_ALPACA_DATA_SECRET_KEY_FILE` | Dedicated data secret-key file |
| `AQA_MARKET_DATA_HISTORY_START` | Non-secret ISO date or timezone-aware initial history boundary |
| `AQA_ENABLE_PAPER_ORDERS` | Always `NO` for collection |

Compose defaults to `secrets/collector_database_url`, `secrets/alpaca_data_api_key`, and
`secrets/alpaca_data_secret_key`; set the corresponding file variables for different host
paths. The database URL shape is
`postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE?sslmode=verify-full`.
Nonlocal hosts require verified TLS including hostname validation; routing overrides are
rejected. The private database hostname must resolve from the container network. No
PostgreSQL port is published by this deployment.

The image sets `PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` so libpq uses the image's
public CA trust bundle with `verify-full`. A private-CA database requires an approved
read-only trust-bundle mount at that path; do not downgrade TLS verification. Migration
commands on the trusted host also need the database's trusted CA configured for libpq.
See [PostgreSQL TLS verification](https://www.postgresql.org/docs/16/libpq-ssl.html).

The image runs as UID/GID `10001`. Presented secrets must be regular, non-symlink files
owned by that UID with mode `0400` or `0600`. File-backed Compose implementations may ignore
requested UID/GID/mode; verify the actual backend before activation. Arrange ownership
through the approved host secret workflow. Do not weaken file validation or make secrets
world-readable. Host root and Docker administrators are part of the trust boundary.
See [Compose secret attributes](https://docs.docker.com/reference/compose-file/services/#secrets).

Legacy `APA_ALPACA_DATA_API_KEY`, `APA_ALPACA_DATA_SECRET_KEY`,
`APA_MARKET_DATA_DATABASE_URL`, and `APA_MARKET_DATA_HISTORY_START` are compatibility
inputs for existing callers. Production uses only canonical file-backed names. Both legacy
and canonical sources together are rejected as ambiguous. Do not source repository `.env`
into the collector. Paper keys, account hashes, operator tokens, administrator passwords,
and migration URLs are never mounted into the running service. Application isolation does
not imply Alpaca enforces a provider-side data-only credential scope.

## Database preparation

Use an existing approved persistent PostgreSQL service with a tested backup policy. Keep
DDL/ownership with the migration role and business writes with the governed collector role.
Never grant runtime table ownership or database administrator authority.

If roles are not bootstrapped, supply the owner-private infrastructure password files on
the trusted deployment checkout, then run:

```bash
uv run --no-sync python scripts/bootstrap_postgres_roles.py --admin-database-url-file /run/secrets/aqa-bootstrap-admin-database-url
```

Apply migrations there before starting collection:

```bash
uv run --no-sync python -m adaptive_trader.collection.cli migrate --database-url-file /run/secrets/aqa-migration-database-url --application-root /srv/autonomous-quant-agent
```

The application root must contain the owner-private platform role-password files. For a
pre-governance database, the target file identifies the validated legacy object owner;
add `--bootstrap-admin-database-url-file /run/secrets/aqa-bootstrap-admin-database-url`
only for that handoff or interrupted recovery. Governed descendants use `aqa_migrate_login`.
Migration credentials remain separate from the collector URL, and migration loads no Alpaca
keys. Startup verifies the migration head; it does not silently rewrite schema. Back up an
existing database before migration. See [backup/restore](backup_restore.md).

## Build and first activation

On the authorized always-on host, from the reviewed checkout:

```bash
docker compose --env-file .env.example -f docker-compose.market-data.yml config --quiet
docker compose --env-file .env.example -f docker-compose.market-data.yml build market-data-collector
```

Configuration validation starts nothing and requires no real secret values. Verify deployed
secret ownership, the private database/TLS path, image revision, resource capacity, and
Docker startup at host boot. Network segmentation is not an outbound firewall; use the
host's approved egress policy for the fixed data endpoints and database.

Choose the intended initial history boundary explicitly. The date below is an example,
not authorization to download more licensed history than the project needs:

```bash
export AQA_MARKET_DATA_HISTORY_START=2025-01-02
docker compose --env-file .env.example -f docker-compose.market-data.yml up -d market-data-collector
docker compose --env-file .env.example -f docker-compose.market-data.yml ps market-data-collector
docker compose --env-file .env.example -f docker-compose.market-data.yml logs --tail 100 market-data-collector
```

`run` performs catch-up before opening the ongoing stream/reconciliation loop. For a finite
backfill before starting the service, use the same image and runtime mounts:

```bash
docker compose --env-file .env.example -f docker-compose.market-data.yml run --rm --no-deps market-data-collector python -m adaptive_trader.collection.cli backfill --start 2025-01-02
```

Do not run a backfill beside the active canonical collector. Do not initialize target
coverage with a partial smoke interval. Use a separately designated disposable database
for smoke tests; destructive pytest integration must never target operational data.

## Health and continuous operation

The deployment uses `restart: unless-stopped`, an init process, a read-only root, bounded
temporary memory, resource limits, rotated logs, and a 90-second graceful-stop allowance.
Ensure Docker itself starts at host boot. A laptop terminal is not an always-on deployment.
Docker health status does not itself restart a hung container; alert on unhealthy/stale
state and use the host's managed recovery workflow.

```bash
docker compose --env-file .env.example -f docker-compose.market-data.yml exec -T market-data-collector python -m adaptive_trader.collection.cli status
docker compose --env-file .env.example -f docker-compose.market-data.yml exec -T market-data-collector python -m adaptive_trader.collection.cli ready
```

These commands use the database and never authenticate to Alpaca. `ready` requires the
active fenced run, subscription acknowledgement, recovered historical checkpoints, and no
pending derived sessions. Research readiness additionally requires fresh active symbols,
no unresolved required-symbol gaps, and the completed aggregate basket. The status snapshot
separates `service_ready`, `research_ready`, `subscribed`, `market_closed`, expected completed
coverage, lagging checkpoints, stale symbols, unresolved gaps, and pending derived sessions.
During holidays, weekends, overnight, and early closes, derive expected bars from the
exchange calendar. An open-session stale basket remains unusable even if the process lives.

Alert on expired ownership, repeated retry exhaustion, failed authentication/subscription,
open-session staleness, unexplained checkpoint lag, pending canonical work, required-symbol
gaps, database disk/connection exhaustion, and failed backups.

## Restart and incident recovery

Record a bounded status snapshot and canonical counts/revisions, then restart the deployed
collector in a controlled maintenance window:

```bash
docker compose --env-file .env.example -f docker-compose.market-data.yml restart market-data-collector
docker compose --env-file .env.example -f docker-compose.market-data.yml ps market-data-collector
docker compose --env-file .env.example -f docker-compose.market-data.yml exec -T market-data-collector python -m adaptive_trader.collection.cli status
```

Prove the prior run terminated, the new run acquired the singleton lease, checkpoints were
reused, overlap repair completed, economic observations were not duplicated, and readiness
recovered. Separately exercise supervisor recovery after a controlled unexpected process
termination; a manual restart alone does not prove the restart policy. Schedule the failure
test with the deployment owner and inspect recovery without modifying provider data.

On the first canonical startup, existing collector current rows are replayed under the
live lease in keyset pages of at most 750 rows. Intake begins after replay completes.
The immutable `canonical_projection_v2_rebuilt` event is written only after the last
committed page and bounded enqueue of configured historical coverage, including empty
provider ranges that must become explicit gaps. Historical enqueue advances in windows
of at most seven days. An interrupted replay starts its scan again; canonical economic identity
makes that replay idempotent. Subsequent starts skip this import when the completion event
exists. Preserve that event and the underlying observations; do not delete operational
state to force a restart. A large legacy import can take longer than the health start period,
so inspect run/projection progress before treating initial unreadiness as failure.

Normal reconnect overlap is supplemented by bounded repair of older canonical gaps.
`canonical_gap_reconciliation_started` durably records the attempt and gap cursor; at most
one additional session-bounded fetch starts per five minutes. Attempts rotate through
eligible gaps so a persistently empty interval cannot starve later gaps, and the cursor and
cooldown survive a process restart. Repair stays within the configured history range and
completed canonical IEX/raw/1Min sessions. An empty or partial REST result leaves missing
coverage unresolved; it never creates a replacement bar or authorizes research readiness.

| Incident | Recovery |
| --- | --- |
| Migration mismatch | Stop, back up, apply the reviewed migration, restart |
| Secret/authentication rejection | Correct the dedicated secret backend/entitlement; never add broker keys |
| Lease unavailable | Find the current owner; allow a confirmed dead lease to expire, never bypass fencing |
| Database outage | Restore availability and verify checkpoint-based catch-up |
| Provider disconnect | Observe bounded reconnect and REST repair; investigate repeated failures |
| Stale/incomplete research data | Keep readiness blocked, repair coverage, inspect missing constituents |
| Correction discrepancy | Inspect immutable observation/revision lineage; do not overwrite history |

Keep encrypted backups outside the collector host and enable point-in-time recovery when
supported. Restore a recent backup into isolated PostgreSQL, apply required migrations,
and compare universe hashes, observation identities, canonical revisions, checkpoint
coverage, and dataset manifest/file hashes. A backup is unverified until that restore
succeeds. The guarded harness in [backup/restore](backup_restore.md) is not permission to
reset an operational database. Never delete persistent data to resolve a collector restart.

The data-only integration restore case is
`tests/integration/test_collection_operations_postgres.py::test_raw_canonical_and_checkpoint_state_survives_data_only_restore`.
It requires the usual explicitly disposable `collector_test` URL and destructive/cluster-role
test acknowledgements, plus `APA_TEST_POSTGRES_CONTAINER` identifying that test container.
It checks the container's published loopback port against the guarded URL, invokes its
`pg_dump`/`pg_restore` utilities, dumps only `market_data` and `aqa` without roles/ownership/ACLs,
and compares all restored row hashes. It refuses an existing `collector_restore_test` database
and removes only the restore database it successfully created. This proves synthetic data
recovery; the production service's own backup and restore procedure still requires validation.

## Immutable canonical snapshots

The data-only `snapshot` command reads the canonical effective IEX/raw/1Min revisions from
one PostgreSQL `REPEATABLE READ READ ONLY` transaction, then publishes and registers an
immutable Parquet artifact. It freezes the existing eleven research/benchmark/context
symbols; the manifest additionally records all 29 collection members, their roles and
universe hash. Collection-only names gain no research or execution authority.

Run it from the trusted deployment checkout/runtime with the collector database URL file
and a private writable artifact root, separate from the read-only live service filesystem:

```bash
uv run --no-sync python -m adaptive_trader.collection.cli snapshot \
  --start "$SNAPSHOT_START_UTC" --end "$SNAPSHOT_END_UTC" \
  --artifact-root /srv/aqa-data/snapshots \
  --source-git-commit "$SOURCE_GIT_COMMIT" --uv-lock-sha256 "$UV_LOCK_SHA256" \
  --dirty-worktree --diagnostic
```

The source commit is the full 40-character revision and the lock value is the SHA-256 of
the deployed source lockfile. The command defaults to a dirty source declaration; use
`--clean-worktree` only after verifying that the deployed source has no uncommitted changes.
No provider authentication or broker credentials are needed for a database snapshot.

Each invocation is bounded to 31 calendar days and 250,000 expected rows, with canonical
reads batched by at most 390 identities and a bounded revision-chain limit. Partition longer
ranges into explicit snapshots; do not raise limits to hide expensive or malformed history.
The artifact is read back before publication and its exact values, schema, physical hash,
logical hash and immutable manifest are checked. Corrections or changed metadata create a
new logical dataset identity; an existing identity is never overwritten. A crash after file
publication but before database registration can leave a content-addressed orphan; retrying
the same request verifies it and completes idempotent registration.

Missing minutes retain all selected symbols and explicit gap counts. The manifest includes
the installed XNAS calendar version, initial history boundary, persisted gap-state hash,
unresolved persisted gaps, lagging checkpoint symbols and pending derived sessions. These
conditions block promotion. Explicit `--diagnostic` exports preserve them with a
non-promotable status instead of inventing complete coverage.

Listing and corporate-action evidence defaults to `unknown`. No current data transport
establishes those historical facts automatically. An optional `--metadata-file` accepts a
reviewed regular, non-symlink JSON file of at most 64 KiB with exactly these keys:
`schema_version` (integer 1), `source` (short source label), `source_document_sha256`,
`observed_at`, `range_start_utc`, `range_end_utc`, `symbols`, `listing_status`, and
`corporate_action_status`. Timestamps must be aware UTC instants; the evidence must cover
the entire requested range and exact sorted eleven-symbol selection, and its observation
time cannot follow snapshot creation. Retain and independently review the actual source
document matching its hash outside Git.

Only explicit listing `active` and corporate-action `clear` evidence permits promotion when
every other data/source gate passes. Both fields also accept `unknown` or `invalidated`,
which require diagnostic export. The parser validates scope and identity, not the truth of
an external attestation; importing this file is not provider-authentication evidence. Any
change to that evidence changes dataset identity and requires a new snapshot. Legacy dataset
requests without the collection extension retain their version-one identity and bytes.

## Required live evidence

Record non-secret results tied to the deployed code revision and host/database identity:

1. Real authenticated historical IEX/raw request, requested symbols/range, independently
   read-back persisted data, and durable checkpoint behavior.
2. Real WebSocket authentication and all 29 bar/updated-bar subscription acknowledgements.
3. Intended catch-up through the latest completed expected session, explained required-symbol
   gaps, and canonical readiness where the basket is complete.
4. Real live receipt and readback when open; when closed, explicitly record closed-market
   validation, the latest completed session, and that no new regular-session bar is expected.
5. Managed always-on process and persistent PostgreSQL, healthy active lease/run, controlled
   restart and transient-failure recovery without duplicate economic data.
6. Tested backup/restore and secret-free logs/evidence; leave the collector running.

Until measured, do not label the system deployed/externally verified or pass Phase Zero.
Credentials, secret contents, raw connection URLs, private bulk data, and broker account
details do not belong in evidence or Git.

Provider contract references: [Alpaca stock stream](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
and [historical stock bars](https://docs.alpaca.markets/reference/stockbarsingle-1).
