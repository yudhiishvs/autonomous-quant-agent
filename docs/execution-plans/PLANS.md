# Execution-plan standard

Execution plans are the repository's durable record for substantial changes. They explain
what will change, why the design was selected, how risk is contained, and what evidence is
required before a claim changes status. They complement `docs/requirements.md`; they do not
replace requirements, architecture decisions, tests, or operator runbooks. The current
[platform core execution plan](platform-core.md) follows this standard.

## When a plan is required

Create or update a plan for a new subsystem, a cross-module change, an architectural refactor,
a public contract, a database migration, persistence or concurrency behavior, authentication or
authorization, financial execution, security-sensitive behavior, deployment, or work spanning
multiple milestones. A small local correction may use a shorter plan only when it changes none
of those boundaries.

Name plans after the enduring outcome, not a ticket number or implementation mechanism. Keep
active plans in this directory and link them from repository navigation documentation.

## Required structure

Every substantial active plan contains these sections in this order:

1. Project objective
2. User-visible outcome
3. Current repository state
4. Requirements being addressed
5. Scope
6. Explicit non-goals
7. Existing architecture
8. Proposed implementation
9. Alternatives considered
10. Why the selected design is preferable
11. Architectural boundaries
12. Data-flow changes
13. State-ownership changes
14. Public API or schema changes
15. Security implications
16. Performance implications
17. Migration or compatibility strategy
18. Failure modes
19. Rollback or recovery approach
20. Test strategy
21. Acceptance criteria
22. Ordered milestones
23. Progress checklist
24. Decisions made
25. Unexpected discoveries
26. Validation evidence
27. Final outcome and remaining limitations

Remove a section only for a genuinely small plan and state why the section is inapplicable. The
repository-wide platform plan must retain all 27 sections.

## Authoring rules

- Inspect entry points, call paths, state ownership, tests, configuration, migrations, external
  boundaries, and analogous code before proposing a change.
- Reference stable requirement IDs from `docs/requirements.md`.
- Separate inspected current behavior from target behavior. A design document or README claim
  is not implementation evidence.
- State assumptions explicitly and make them easy to revise.
- Prefer the smallest coherent design that preserves existing behavior and leaves a runnable,
  testable repository at each milestone.
- Record rejected alternatives with their concrete tradeoff; do not invent alternatives merely
  to fill the section.
- Include exact invariants for money, time, identity, authorization, persistence, retries, and
  concurrency where they matter.
- Describe external interfaces from the locked dependency or protocol contract. Record any
  uncertainty when the primary contract cannot be inspected.
- Keep prose specific to repository modules, commands, tables, schemas, and failure modes.
- Do not include credentials, local data, transcripts, scratch reasoning, or unverifiable claims.

## Status and evidence

Use only these requirement and milestone statuses:

- `IMPLEMENTED_AND_VERIFIED`
- `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`
- `PARTIALLY_IMPLEMENTED`
- `NOT_IMPLEMENTED`
- `BLOCKED`
- `INTENTIONALLY_DEFERRED`

A status may advance to `IMPLEMENTED_AND_VERIFIED` only when the requirement's conforming behavior
or required artifact exists and its listed verification has actually passed. A verified plan or
other document proves only its documentation requirement and never substitutes for executable
runtime behavior. External adapters that have only mocked contract tests remain
`IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`. `BLOCKED` identifies a specific unavailable authorization
or capability; it is not a synonym for future work. Every retained command result records the
exact command, environment boundary, count or hash when applicable, and date. Non-command evidence
names its method, scope, reviewer or source, and date.

Never report a command as passing when it was not executed. Preserve failures and distinguish
pre-existing failures from regressions. Test counts, coverage, benchmark results, and hashes must
come from retained command output or an intentionally retained safe artifact.

## Living-plan workflow

For each milestone:

1. Identify the requirement IDs and invariants in scope.
2. Trace existing behavior and select the smallest implementation surface.
3. Add meaningful positive, negative, boundary, failure, restart, and security tests appropriate
   to the risk.
4. Implement one coherent vertical slice.
5. Run focused checks, repair root causes, then run the broader affected suite.
6. Update progress, decisions, discoveries, evidence, and requirement statuses in the same
   change.
7. Review the complete diff for unnecessary abstractions, duplicate logic, unused configuration,
   hidden side effects, unsafe defaults, secret exposure, unbounded work, and stale prose.
8. Perform an explicit security review whenever a trust boundary, external input, credential,
   network path, persistence path, serialization format, or financial action changes.

The plan is not an activity log. Keep decision history and evidence that remain useful after the
implementation changes. Replace obsolete forecasts with current facts while retaining material
decisions and discoveries.

## Milestone and review gates

A natural commit runs focused and materially affected repository checks. A milestone is eligible
for review only when required behavior is executable, public contracts are typed, appropriate
tests exist, the complete repository verification suite prescribed by `REQ-TEST-005` passes, the
diff is coherent, documentation matches behavior, and simplification and security reviews are
recorded. Passing tests alone does not establish completion.

Natural changes should be independently understandable and reversible without leaving a broken
schema or public contract. If a change grows beyond roughly 800 meaningful handwritten lines,
re-evaluate whether it contains separable boundaries; do not split an atomic schema/code change
or combine unrelated work to manipulate change count.

Before any commit, inspect status and the complete tracked and untracked diff, report behavior
changed and intentionally unchanged, enumerate checks and exact results, record simplification and
security findings, and propose the coherent boundary and message. Do not stage or commit while the
review packet is pending. The maintainer responds with exactly one of:

- `APPROVE COMMIT: <message>` to authorize only that reviewed commit;
- `REQUEST CHANGES: <instructions>` to request another review iteration;
- `SPLIT COMMIT: <instructions>` to change the boundary.

Stop after presenting the packet and do not edit its boundary until one response arrives.

After exact approval, verify the worktree still matches the reviewed diff, stage only the approved
paths or hunks, run the cached-diff checks, and commit with the exact approved message, configured
identity, and current time. Then inspect the commit and remaining worktree before continuing.
Remote publication and history-changing operations require separate authorization.

## Completion and archival

At the end of a plan, replace forecasts in Section 27 with the actual outcome, evidence, and
specific remaining limitations. Verify every acceptance criterion against the requirements
ledger, ensure unresolved high-severity findings are absent, and retain exact external activation
steps only where credentials, permissions, or infrastructure remain unavailable.

Completed plans remain in this directory as decision and verification history. If subsequent work
invalidates evidence or reopens a requirement, update the plan rather than leaving a misleading
completion claim.
