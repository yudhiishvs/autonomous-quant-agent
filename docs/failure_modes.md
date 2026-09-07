# Platform failure modes

The default response to missing, stale, malformed, ambiguous, or unauthorized state is to create no
new exposure. Risk reduction remains permitted only when its inputs and side effects can be proved
safe. These are operational responses, not availability guarantees.

| Failure | Detection | Safe behavior | Recovery evidence |
| --- | --- | --- | --- |
| invalid config or hash drift | strict parse and expected-hash comparison | startup fails before clients are built | corrected config passes `aqa config validate` |
| missing/unsafe secret file | descriptor-based ownership, mode, type, and identity checks | affected external service does not start | replace/rotate file, then explicit health check |
| provider unavailable | bounded adapter timeout/retry state | no fabricated bars; gap remains durable | backfill exact interval and verify contiguous watermark |
| duplicate bar | canonical identity/content match | return existing revision; no projection change | one event and unchanged latest hash |
| corrected bar | semantic content differs | append N+1 revision and update latest atomically | lineage and dependent aggregate revision hashes |
| partial gap repair | calendar-aware coverage mismatch | gap stays unresolved; watermark cannot cross it | complete exact coverage and durable resolution event |
| incomplete active basket | minimum active watermark missing/stale | decision slot waits, skips, or expires | all active series ready before deadline |
| unsupported early close | calendar marks shortened session | create no entry slots; risk reduction handled separately | explicit skip/incident; never reuse full-day timetable |
| expired slot lease | durable lease deadline | another worker inspects materialized result before reclaim | single terminal result and bounded attempt history |
| malformed/expired signal | strict envelope/context/hash validation | no signed risk decision permitting exposure | new valid envelope from registered provider |
| compromised strategy proposal | symbol/policy/risk constraints | proposal cannot bypass deterministic risk | signed risk decision shows shrink/block reasons |
| stale account/security/price/reconciliation | source timestamps exceed policy | block new exposure | refreshed snapshots bound into a new risk decision |
| risk constraints do not converge | eight-pass deterministic bound | all-zero target with explicit reason | decision receipt and no opening intent |
| session loss/drawdown | signed equity thresholds | append latch and force target zero | authenticated reviewed clear appends a new event |
| sign reversal | current and target signs differ | persist/complete/reconcile close; prove zero before opening | separate stage IDs and zero-position receipt |
| failure before broker call | intent committed but submission not started | restart can resume from durable intent after gates | audit/order history shows one submission boundary |
| timeout before acceptance | no broker order found | safe bounded retry only under defined state transition | deterministic lookup result and event history |
| timeout after acceptance | side effect may exist | mark `SUBMISSION_UNKNOWN`; never auto-retry | lookup by client ID then reconciliation |
| duplicate broker update/fill | event/execution identity repeats | exact duplicate is idempotent; conflict fails closed | one fill per broker execution ID |
| reconciliation discrepancy | signed fill/order/account comparison | blocking/critical result engages latch and incident | discrepancy receipt plus latch/incident chain |
| forced flatten incomplete | deadline or exact-zero proof fails | report failure and durable blocking incident | no success status until positions are exactly flat |
| audit tampering | sequence, previous hash, payload hash, or event hash mismatch | verification fails; affected state is untrusted | preserved database and incident investigation |
| job worker crash | claim/attempt lease expires | deterministic retry up to bounded policy; then dead | attempts and outbox remain durable/idempotent |
| API auth/rate/size failure | constant-time token check and bounded middleware | stable response with no sensitive detail; no mutation | correlated safe log/metric and unchanged state |
| dashboard compromise | dashboard token and read-only routes | no DB/broker credential or mutation capability | revoke dashboard token and inspect API audit |
| PostgreSQL unavailable | readiness/transaction failure | service becomes unready; no in-memory success claim | DB recovery, migration-head check, repository replay |
| backup restore mismatch | row/hash/audit/schema comparison | backup is rejected | preserve isolated restore and investigate |

## Restart rules

- Reconstruct state from durable rows rather than worker memory.
- Reuse deterministic event, slot, signal, plan, intent, client-order, fill, job, and outbox IDs.
- Inspect a materialized result before reclaiming an expired lease.
- Never convert an exception after a possible side effect into success or blind retry.
- Reconcile broker/account state before enabling new exposure after process or database recovery.
- Forced flatten remains an active blocking objective until exact zero is proven or an incident is
  recorded.

The offline demo exercises the nine critical persistence and side-effect restart boundaries. See
[demo runbook](demo_runbook.md). Production provider and paper-adapter recovery remains
credential-gated and is not evidenced by the synthetic demo.

## Escalation

Preserve immutable evidence, disable submission, capture bounded safe identifiers and timestamps,
and follow [incident response](incident_response.md). Never paste tokens, headers, database URLs,
raw provider payloads, or account details into an issue or log.
