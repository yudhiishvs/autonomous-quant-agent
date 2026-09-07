# ADR 0012: Fail closed on unsupported early-close sessions

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-SCHED-001`, `REQ-SCHED-003`, `REQ-EXEC-009`

## Context

The experiment has fixed full-session entry and flatten times. An exchange early close can make
those times invalid or leave insufficient time to enter, reconcile, and flatten safely.

## Decision

Derive sessions from the XNAS calendar and create no strategy entry slots on an early-close day.
Do not shift the fixed timetable automatically. Existing exposure remains a separate risk-reduction
obligation: disable entries and attempt bounded flatten/reconciliation using current supported
market state; inability to prove flat records a blocking incident.

## Alternatives considered

- Compress the schedule: rejected because it changes the experiment and data horizon implicitly.
- Treat the day as a full session: rejected because deadlines can occur after market close.
- Skip all activity including risk reduction: rejected because existing exposure still requires a
  safe response.

## Consequences

Early-close days produce no new strategy decisions. Supporting them later requires a new versioned
session contract, dataset/evaluation evidence, and migration/compatibility review.

## Security impact

Unknown or shortened session timing cannot authorize entry. Forced-risk failure is explicit and
auditable rather than mislabeled as successful flatness.
