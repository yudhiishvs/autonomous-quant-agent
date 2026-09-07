# ADR 0005: Persist intent before external side effects

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-EXEC-004`, `REQ-EXEC-005`, `REQ-FUNC-006`

## Context

A process can fail immediately before, during, or after a broker request. Without a durable intent
and deterministic client ID, restart cannot distinguish an unsent request from an accepted request
whose response was lost.

## Decision

Persist the signed execution plan, every order intent, and the pre-submission state transactionally
before broker invocation. Use stable bounded client IDs. After a possible side effect, an exception
becomes `SUBMISSION_UNKNOWN`; resolve it by client-ID lookup and reconciliation, never blind retry.

## Alternatives considered

- Persist only the broker response: rejected because a crash before persistence loses identity.
- Retry every timeout: rejected because timeout-after-acceptance can duplicate exposure.
- Rely on in-memory request tracking: rejected because restart erases it.

## Consequences

Persistence is on the critical path and repositories must be available before submission. Exact
replay is idempotent; ambiguous state can reduce availability while reconciliation completes.

## Security impact

Intent-first state limits duplicate financial side effects and provides an auditable authorization
chain. Database failure denies submission. Logs and errors expose only safe identifiers, never the
credential or raw broker payload.
