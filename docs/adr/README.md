# Architecture decision records

Architecture decision records preserve choices that constrain multiple modules, public contracts,
security boundaries, persistence, operations, or compatibility. The active execution plan records
provisional decisions; promote a decision here when implementation makes it durable.

## Index

No architectural decision has yet been promoted to an `Accepted` ADR. The exact 12-record backlog
is listed in the
[requirements inventory](../requirements.md#normative-package-and-decision-record-inventory), and
the [active plan](../execution-plans/platform-core.md) records implementation timing and
provisional decisions until each boundary is reviewed.

## Conventions

- Name records `NNNN-short-decision-title.md` with a four-digit sequence.
- Copy `000-template.md`; do not edit the template in place.
- Use only `Proposed`, `Accepted`, `Superseded`, or `Rejected` as the decision status.
- Link superseding and superseded records in both directions.
- Record concrete alternatives, consequences, compatibility effects, security impact, and
  verification evidence.
- Never describe planned behavior as implemented or verified.
