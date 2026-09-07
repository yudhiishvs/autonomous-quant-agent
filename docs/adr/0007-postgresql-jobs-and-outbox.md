# ADR 0007: Use PostgreSQL jobs and outbox instead of a message broker

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-JOB-001`, `REQ-JOB-002`, `REQ-ARCH-006`

## Context

The single-operator control plane needs durable bounded work, idempotency, attempts, leases, and
events committed with job state. A separate queue would add deployment and failure modes without a
current throughput requirement.

## Decision

Store strict public job requests, attempts, leases, safe errors, and transactional outbox events in
PostgreSQL. Claim with deterministic ordering and `SKIP LOCKED` on PostgreSQL, with equivalent
serialized behavior in SQLite tests. Limit public job types and retry policy explicitly.

## Alternatives considered

- Redis/Celery: rejected because transactional coupling and another credential/service are not
  justified.
- In-process background tasks: rejected because process failure loses work.
- A cloud queue: rejected because cloud deployment is deferred.

## Consequences

PostgreSQL availability limits control-plane work. Workers must keep tasks bounded and leases short,
and an outbox consumer must be idempotent. A future broker requires measured contention evidence and
a migration ADR.

## Security impact

Payload schemas reject commands, code, URLs, paths, and arbitrary modules. The control role can
create only bounded jobs and cannot mutate orders/fills. Audit and outbox rows share the job
transaction, preventing unaudited success.
