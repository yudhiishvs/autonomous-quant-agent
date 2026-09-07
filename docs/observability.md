# Observability

The platform has three different evidence surfaces. Logs explain a bounded operational event,
Prometheus metrics summarize finite categories, and the append-only audit chain is the authority for
consequential transitions. None may contain credentials, raw provider/broker payloads, account
identifiers, database URLs, headers, environment dumps, or arbitrary external exception text.

## Implemented platform telemetry

`adaptive_trader.platform.observability` provides:

- structured JSON event construction with UTC timestamps and a closed field inventory;
- recursive redaction of secret-bearing keys and values before serialization;
- bounded event/service/mode/state/reason identifiers;
- Prometheus counters and gauges for data, readiness, slots, risk, orders, reconciliation, jobs,
  API authentication/rate limiting, and redaction/security events;
- a fixed label policy that rejects symbol, correlation/domain ID, path, URL, exception, free text,
  or secret-driven cardinality; and
- explicit liveness/readiness snapshots for bounded services.

The private API exposes authenticated `GET /metrics`. The only unauthenticated health routes are
`GET /health/live` and `GET /health/ready`. Tests verify the metric inventory, label bounds,
redaction sentinels, safe JSON schema, and service health semantics.

Hash-chained `aqa_audit_events` record actor-specific streams. `aqa audit verify` checks sequence,
previous hash, payload hash, event hash, expected head, and full-chain integrity. PostgreSQL roles
write through actor-scoped views/policies; control and read-only roles receive explicit safe views.

## Existing compatibility telemetry

The legacy application retains rotating JSON-lines logs and durable SQLite health, incident,
decision, order, fill, reconciliation, and report metadata. The standalone collector retains
PostgreSQL ingestion runs, events, checkpoints, leases, and safe status/readiness commands. These
compatibility surfaces do not redefine the platform audit or Prometheus contracts.

## Diagnostic event rules

The boundary that decides retry, rollback, or state transition owns one diagnostic. It should answer
which safe operation changed, which bounded category it affected, why it failed, and whether retry
is permitted.

Severity:

- `DEBUG`: bounded local diagnostic, disabled by default;
- `INFO`: lifecycle or consequential transition;
- `WARNING`: contained degradation, retry, duplicate, or recoverable discrepancy;
- `ERROR`: operation failed while state remained safe;
- `CRITICAL`: authority/integrity uncertainty requiring operator containment.

Stable categories include `validation`, `authentication`, `authorization`, `stale_state`,
`conflict`, `provider_retryable`, `dependency`, `ambiguous_effect`, `invariant`, and `internal`.
Public responses expose only a stable category, bounded reason, and safe correlation ID. Redaction
does not justify logging unsafe source objects.

## Metric-label policy

Allowed labels are reviewed finite enums such as service, mode, event category, state, bounded
reason code, timeframe, and provider/feed kind. Symbols, user input, free text, request/job/order
IDs, exception strings, paths, URLs, hostnames, usernames, account identifiers, and secrets are
forbidden.

Cardinality limits are correctness constraints. A new metric requires a test enumerating every
label key/value source and sentinel evidence that hostile input cannot create a new series.

## Health semantics

Liveness means a process can service its control loop. Readiness means it can safely perform its
declared workload now. Readiness is service-specific:

- data: schema is current, writer lease is valid, and persistence works;
- scheduler/strategy: required contiguous active-basket watermark and slot state are available;
- execution: schema is current, reconciliation is fresh/clean, no ambiguous order or blocking latch
  exists, and the configured mode's gates pass;
- API: bounded read/job repositories are available;
- dashboard: private API read routes are reachable.

Provider connectivity alone is not data readiness, and process liveness is not authority to trade.

## Correlation and audit

Correlation IDs connect bounded request/decision operations but are not Prometheus labels. Durable
domain IDs and hashes belong in database/audit evidence. A log or trace is never used to infer that
a broker side effect occurred.

Distributed tracing is intentionally not implemented for the single-host topology. If deployment
evidence later justifies it, a new decision must apply the same allowlist/redaction/cardinality
rules. Audit state remains authoritative.

## Alerts and retention

Alert routing, provider-specific thresholds, centralized log shipping, automated durable-state
retention, and incident paging are deployment-owner work and have not been validated in a hosted
environment. Suggested conditions include expired leases, unresolved gaps, watermark lag, expired
slots, active risk/reconciliation latches, ambiguous orders, failed flattening, dead jobs, database
pool exhaustion, backup failure, and repeated authentication/rate-limit events.

Select thresholds only after representative observation. Never retain a secret to improve
diagnostics. Database/event retention must preserve statutory/operator needs and audit/recovery
integrity; provider data remains subject to its license.
