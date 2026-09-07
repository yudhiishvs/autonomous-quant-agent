# Operations

This repository targets a self-hosted, single-operator, paper-only deployment. Offline is the
default. External market data and paper execution are separate opt-in profiles with different
credentials and authority.

## Modes

| Mode | Data | Signal | Broker | Submission |
| --- | --- | --- | --- | --- |
| offline | deterministic fixture | deterministic non-promotable fixture | deterministic fake | disabled |
| shadow | configured market-data adapter | registered/always-flat | none | impossible |
| paper | configured market-data adapter | registered/always-flat | literal paper-only adapter | disabled in tracked config; independent gates required |

There is no real-money mode or configurable trading host.

The paper adapter binds fills to actual execution IDs from bounded `FILL` account-activity
pages, verifies order/client identity and cumulative quantities, and refuses incomplete or
contradictory evidence. Its `paper-equity-zero-simulated-fees-v1` policy applies only to the fixed
paper host and US equities: Trading API fill activities omit fees and Alpaca paper simulation
excludes regulatory and borrow fees. Any supplied nonzero fee is rejected, and cash reconciliation
remains mandatory. See the official [activity contract](https://docs.alpaca.markets/us/docs/account-activities)
and [paper specification](https://docs.alpaca.markets/us/docs/paper-trading).

The adapter currently requires exact agreement between the reported order average and the
weighted execution average. A finite rounded provider average can therefore be rejected, even
when individual executions are valid. Such a response remains unresolved and cannot authorize
further exposure; no undocumented rounding tolerance is assumed. Real provider precision,
pagination timing and account reconciliation remain externally unvalidated.

## Preflight

```bash
uv sync --locked --all-extras
uv run --no-sync aqa doctor
docker compose --env-file .env.example -f docker-compose.yml config --quiet
```

Static validation does not prove that PostgreSQL, a provider, or a broker is reachable. Do not add
provider keys merely to run offline checks.

Bootstrap local database-role passwords and the operator token without overwriting existing files:

```bash
uv run --no-sync aqa secrets bootstrap-local
```

Inspect owner and modes before mounting any secret. Data and paper pairs must remain different and
service-specific. The dashboard gets only an API token; strategy and scheduler get no provider or
broker keys.

## Database lifecycle

Create the managed PostgreSQL roles through the documented bootstrap path, then apply checked-in
migrations with an owner-private database URL file:

```bash
uv run --no-sync aqa db migrate \
  --database-url-file .secrets/aqa_migrate_database_url \
  --application-root .
```

Operational startup does not use `metadata.create_all`. A service should remain unready when its
database revision is not the checked-in head. Core data/history downgrades are unsupported.

Run the guarded role/migration suite only against the exact disposable loopback database described
by the test fixtures. The tests can drop schemas and cluster roles.

## Service topology

Compose separates:

- PostgreSQL and one-shot migration;
- control API and read-only dashboard;
- market-data, scheduler, strategy, and execution workers;
- opt-in live-market-data and paper-execution workers.

The API and dashboard publish loopback ports only. PostgreSQL is unpublished by default. The
`market-data` and `paper` profiles are not part of default startup. Linux containers use a numeric
nonroot identity, dropped capabilities, no-new-privileges, read-only roots where supported, and
explicit temporary/writable mounts. Network segmentation is defense in depth, not an outbound
host firewall.

Review the fully rendered configuration before startup:

```bash
docker compose --env-file .env.example -f docker-compose.yml config
```

Never use a real secret file as `--env-file`, commit rendered Compose output, or assume a successful
configuration render proves runtime health.

## Health and readiness

- liveness means the process event loop can respond;
- readiness means required schema/state dependencies are usable;
- provider readiness must not fabricate data during an outage;
- an execution service is not ready for new exposure while reconciliation is stale, an ambiguous
  order exists, a blocking latch is active, or submission gates fail;
- dashboard failure does not grant or remove trading authority.

The unauthenticated API surface is limited to `GET /health/live` and `GET /health/ready`.
Authenticated responses are private/no-store. `GET /metrics` uses bounded enumerated labels and no
symbol, identifier, free text, or secret as a label value.

## Routine offline operation

```bash
uv run --no-sync aqa data status
uv run --no-sync aqa scheduler status
uv run --no-sync aqa demo --output outputs/demo/evidence.json
uv run --no-sync aqa audit verify --application-root .
```

The fixture ingest/aggregate/freeze CLI commands are bounded whole-slice verifications; they do not
run continuously. Worker services use durable leases/jobs and reconstruct state from PostgreSQL.

## Operational scheduling and shadow evaluation

`aqa data status --json` reads persisted basket watermarks; `aqa scheduler status --json` reads
current New York session slots. Both return at most 100 records plus explicit truncation and counts
within those records. `configured` is separate from `observation`; absent/inaccessible storage is
`unavailable`, and empty observed storage is `empty`. Neither claims provider or overall service health.
Use `--application-root` for existing offline `runtime/aqa-offline.sqlite3`; status never creates it.
Operational profiles use `--config platform/shadow.yaml --database-url-file PATH` with a dedicated
control/read-only login and safe views. `scheduler status --preview` explicitly shows the fixed
offline demonstration schedule. Use the authenticated operational API for broader health evidence.
`aqa data collect-once` performs bounded checkpoint-driven REST catch-up and derived processing;
it is an external data invocation and requires dedicated data secret files. The GitHub workflow
runs that same command only after explicit activation. See [scheduled collection](scheduled_market_data.md).

The scheduler and strategy service workers accept the configured shadow/paper profiles through
`AQA_CONFIG`. The operational scheduler uses the current injected UTC clock and New York calendar
date, persists the exact session schedule, expires overdue entries, recovers bounded abandoned
claims and claims ready slots for the strategy worker. Pending canonical work or unresolved gaps
block entry readiness through `aqa_operational_readiness_v`. Forced flatten remains a separate
slot contract. The strategy worker uses the registered AlwaysFlat baseline without changing AI.

`aqa shadow propose-once --slot-id <durable-slot-id>` uses the strategy database role to persist a
validated AlwaysFlat envelope from current durable data. `aqa shadow run-once --slot-id
<durable-slot-id>` uses the execution database role to consume that envelope, evaluate independent
signed risk and persist an actual unsubmitted plan. Configure each process's dedicated
`AQA_DATABASE_URL_FILE`; never give the strategy role order/risk writes or use migration credentials
as a shortcut. Both commands require the migrated database and current valid data; neither
constructs an Alpaca data or broker client. Missing/expired input is an explicit blocked/error
outcome, not a configuration-only success.

Shadow's diagnostic account is explicitly virtual cash-only, not a broker-account snapshot.
Its plans are non-authoritative diagnostics; unknown broker capabilities fail closed. Explicit
`--fixture` operates only in isolated offline/fake mode with fixture provenance and cannot authorize
paper submission. Final PostgreSQL role and integrated runtime verification are recorded separately
in the active plan; implementation existence does not certify those checks.

## Paper safety

Paper adapter invocation requires every independent gate: paper mode, configuration enablement,
exact acknowledgement, literal paper-only adapter, valid paper secret files, matching approved
account hash, approved non-fixture signal artifact, positive authorization verifier, fresh
session/data/account/security/reconciliation state, and no blocking latch.

Tracked profiles deny submission and the default approval verifier denies approval. Do not modify a
tracked profile as an operational shortcut. No paper connection or order is required to validate
ordinary development, CI, backup, benchmark, or demo paths.

After any worker or database restart, reconcile deterministic client IDs, fills, positions, cash,
equity, and account identity before considering new exposure. `SUBMISSION_UNKNOWN` is never an
automatic retry state.

## Shutdown and recovery

Stop producers before PostgreSQL when practical. Workers must finish or release bounded claims,
persist safe failure state, and propagate cancellation rather than detach tasks. On restart:

1. verify migrations and audit chains;
2. reconstruct leases, slots, latches, jobs, orders, and reconciliations;
3. inspect ambiguous or materialized work before reclaim;
4. refresh market/account/security state;
5. keep entries disabled until clean reconciliation; and
6. prove exact flatness when a forced-flat objective is active.

See [failure modes](failure_modes.md), [backup/restore](backup_restore.md), and
[incident response](incident_response.md).

## Logs and metrics

Logs are structured safe events with bounded event/service/state/reason values and correlation
identifiers. Never log headers, environment/settings dumps, URLs with credentials, raw provider
payloads, broker/account objects, or raw external exceptions. Redaction is structural and recursive;
it does not make unsafe logging acceptable.

Keep metrics labels enumerable. High-cardinality IDs belong in bounded logs or audit state, not in
Prometheus labels.

## Backups

Run regular encrypted logical backups plus independent immutable-artifact backups, and restore into
a fresh isolated database on a schedule. A dump is not validated until schema, row hashes, audit
chain, slots, intents, fills, and reconciliation state match. Follow
[backup/restore](backup_restore.md).
