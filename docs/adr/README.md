# Architecture decision records

Architecture decision records preserve choices that constrain multiple modules, public contracts,
security boundaries, persistence, operations, or compatibility. The active execution plan records
provisional decisions; promote a decision here when implementation makes it durable.

## Index

1. [ADR 0001: Separate the generic platform from shipped experiment configuration](0001-generic-platform-and-shipped-experiment.md)
   — Accepted, 2026-09-04.
2. [ADR 0002: Add separate signed platform contracts](0002-separate-signed-platform-contracts.md)
   — Accepted, 2026-09-05.
3. [ADR 0003: Use PostgreSQL operational state and immutable Parquet datasets](0003-postgresql-operational-state-and-parquet-datasets.md)
   — Accepted, 2026-09-05.
4. [ADR 0004: Strategy proposals have no broker authority](0004-strategy-proposals-have-no-broker-authority.md)
   — Accepted, 2026-09-05.
5. [ADR 0005: Persist intent before external side effects](0005-persist-intent-before-side-effects.md)
   — Accepted, 2026-09-05.
6. [ADR 0006: Close and reconcile before sign reversal](0006-close-and-reconcile-before-sign-reversal.md)
   — Accepted, 2026-09-05.
7. [ADR 0007: Use PostgreSQL jobs and outbox instead of a message broker](0007-postgresql-jobs-and-outbox.md)
   — Accepted, 2026-09-05.
8. [ADR 0008: Retain Streamlit until the private API stabilizes](0008-retain-streamlit-until-api-stabilizes.md)
   — Accepted, 2026-09-05.
9. [ADR 0009: Retain Python until profiling justifies native code](0009-retain-python-until-profiling.md)
   — Accepted, 2026-09-05.
10. [ADR 0010: Keep scope self-hosted and paper-only](0010-self-hosted-paper-only-scope.md)
    — Accepted, 2026-09-05.
11. [ADR 0011: Separate process and credential trust boundaries](0011-process-credential-trust-boundaries.md)
    — Accepted, 2026-09-05.
12. [ADR 0012: Fail closed on unsupported early-close sessions](0012-fail-closed-on-early-close-sessions.md)
    — Accepted, 2026-09-05.

## Conventions

- Name records `NNNN-short-decision-title.md` with a four-digit sequence.
- Copy `000-template.md`; do not edit the template in place.
- Use only `Proposed`, `Accepted`, `Superseded`, or `Rejected` as the decision status.
- Link superseding and superseded records in both directions.
- Record concrete alternatives, consequences, compatibility effects, security impact, and
  verification evidence.
- Never describe planned behavior as implemented or verified.
