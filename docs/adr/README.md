# Architecture decision records

Architecture decision records preserve choices that constrain multiple modules, public contracts,
security boundaries, persistence, operations, or compatibility. The active execution plan records
provisional decisions; promote a decision here when implementation makes it durable.

## Index

1. [ADR 0001: Separate the generic platform from shipped experiment configuration](0001-generic-platform-and-shipped-experiment.md)
   — Accepted, 2026-09-04.

The remaining exact decision-record backlog is listed in the
[requirements inventory](../requirements.md#normative-package-and-decision-record-inventory).
The [active plan](../execution-plans/platform-core.md) records provisional decisions until each
implementation boundary is reviewed.

## Conventions

- Name records `NNNN-short-decision-title.md` with a four-digit sequence.
- Copy `000-template.md`; do not edit the template in place.
- Use only `Proposed`, `Accepted`, `Superseded`, or `Rejected` as the decision status.
- Link superseding and superseded records in both directions.
- Record concrete alternatives, consequences, compatibility effects, security impact, and
  verification evidence.
- Never describe planned behavior as implemented or verified.
