# Observability

## Current status

Legacy operational logging and durable SQLite health/evidence are
`IMPLEMENTED_AND_VERIFIED` in offline tests. Collector run/event/checkpoint/lease visibility
is `IMPLEMENTED_AND_VERIFIED` with disposable PostgreSQL. Provider-connected alerting,
Prometheus service metrics, distributed tracing, and centralized retention are
`NOT_IMPLEMENTED`.

## Legacy application signals

- `logging_config.py` writes human console output and rotating JSON lines under
  `runtime/logs/`; files rotate at 10 MiB with five backups.
- JSON fields currently include timestamp, level, logger, message, and selected event/run/
  decision/order/symbol/mode context.
- The redaction filter removes configured secret values and common authorization, token,
  and key/value forms from messages and exception chains.
- SQLite persists heartbeats, stream events, incidents, reconciliation findings, latches,
  decisions, orders, fills, and generated-report metadata.
- `adaptive-portfolio-agent status`, `report`, and the read-only Streamlit dashboard consume
  that state.

Legacy financial calculations in `metrics.py` are report metrics, not runtime telemetry.
Within the legacy path, `run_id` groups one application lifecycle, `decision_id` ties evaluation
and risk evidence together, and local/client/broker order identifiers connect order transitions
where available. These identifiers are not a cross-service request context.

## Collector signals

- `ingestion_runs` records mode, lifecycle, fencing identity, bounded counters, and safe
  error type.
- `collector_events` records retries, reconnects, reconciliation failures, and lifecycle
  events with bounded details.
- `collector_checkpoints` exposes the end of the last successfully queried REST window per exact
  series; it does not prove contiguous or quality-approved bar coverage.
- `collector_leases` plus the correlated active run expose singleton ownership.
- `adaptive-market-data status` returns counts and latest receipt time without loading data
  credentials.
- `adaptive-market-data ready` proves an unexpired canonical lease and its exact active run;
  it does not prove bar freshness, checkpoint completeness, or provider connectivity.

Collector events correlate to `run_id`; lease owner and fencing values diagnose singleton
ownership but are not general request identifiers. There is no shared current correlation or
request ID spanning the legacy process, collector, dashboard, or external calls.

## Diagnostic event rules

Logs and events must answer what operation changed, which safe identifier it affected, why
it failed, and whether retry is permitted. Use UTC timestamps and bounded identifiers.
Never emit credentials, headers, environment dumps, database URLs, raw payloads, account
identifiers, arbitrary exception text, or high-cardinality values as metric labels.

Expected severity use:

- `DEBUG`: bounded local diagnostic detail, disabled by default.
- `INFO`: lifecycle or consequential state transition.
- `WARNING`: degraded but contained state, retry, duplicate, or recoverable discrepancy.
- `ERROR`: operation failed and state remained safe.
- `CRITICAL`: authority or integrity uncertainty requiring operator containment.

Avoid duplicate messages at every layer. The boundary that decides retry, rollback, or state
transition owns the diagnostic.

Stable target error categories are:

- `validation`: malformed, out-of-range, unknown-field, or protocol input;
- `authentication` and `authorization`: missing/invalid identity or denied authority;
- `stale_state`: data, account, lease, watermark, or authorization is no longer current;
- `conflict`: duplicate identity, revision conflict, or already-claimed work;
- `provider_retryable`: bounded timeout, disconnect, rate limit, or declared transient failure;
- `dependency`: unavailable or failed database, filesystem, provider, or broker dependency;
- `ambiguous_effect`: an external side effect may have occurred but is not authoritative yet;
- `invariant`: a state-machine, integrity, audit, or safety invariant failed; and
- `internal`: an unexpected failure after sensitive details are redacted.

Public diagnostics expose the stable category and a bounded reason code, never an arbitrary
exception string. The original exception remains available only to the redacting log boundary.

## Health and alert semantics

Liveness means the process can service its control loop. Readiness means the component can
perform its declared workload; each readiness endpoint must document its exact proof.
Downstream decision readiness additionally requires contiguous eligible data, freshness,
supported session state, and clean reconciliation.

Potential collector alert categories are expired/missing leases, no active run, repeated stream
restart or historical-reconciliation failure, checkpoint lag during market hours, old latest
receipt time, storage pressure, backup failure, and pool exhaustion. Alert routing and thresholds
are `NOT_IMPLEMENTED`; hosted observations will be needed before selecting provider-specific
thresholds.

## Target telemetry

The target platform will standardize correlation IDs, redacted JSON logging, bounded-label
Prometheus metrics, per-service liveness/readiness, and audit hash-chain verification. That
shared observability package and `/metrics` route are `NOT_IMPLEMENTED`; current components
must not be described as already using them.

The required target metric inventory is:

| Area | Bounded measurements |
| --- | --- |
| Data | bars received, duplicates, corrections, invalid bars, gaps created/resolved, and watermark lag |
| Scheduling | slots ready, completed, skipped, and expired |
| Risk | blocks by bounded reason and latch state |
| Execution | intents, bounded order states, ambiguous submissions, and reconciliation discrepancies |
| Jobs | bounded job states, retries, and dead jobs |
| API/security | authentication failures, rate limits, and redaction/security validation failures |

Metric labels may use only reviewed finite dimensions such as service, mode, event category,
state, reason code, timeframe, and provider/feed enum. Symbol, user input, exception text,
correlation or domain identifiers, paths, URLs, and secrets are forbidden labels. Exact metric
names and types will be introduced with `platform/observability/metrics.py`; the inventory table
does not claim an implemented exporter.

Distributed tracing is not currently justified by the single-host topology and is
`NOT_IMPLEMENTED`. If later evidence warrants it, trace/span identifiers must be random safe
correlation values, attributes must follow the same bounded/redacted field policy, and neither
provider payloads nor credential-bearing transport details may be captured. Durable audit state,
not a trace backend, remains the authority for consequential transitions.

Rotating JSON logs are the only current automatic retention bound. Legacy SQLite and collector
PostgreSQL durable records have no automatic pruning or repository-wide retention policy; backup,
archival, deletion, log shipping, alert routing, and incident paging are deployment-owner choices.
Target retention controls remain `NOT_IMPLEMENTED`. Never retain a secret to improve diagnostics.
