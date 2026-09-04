# Code Review

## Review order

Review the requested behavior and complete diff, then inspect in this order:

1. unauthorized paper/real financial effects and safety-gate bypass;
2. data loss, incorrect corrections, checkpoint drift, or migration failure;
3. replay, idempotency, lease, concurrency, restart, and ambiguous-side-effect defects;
4. stale/nonfinite input acceptance and research look-ahead;
5. credential, injection, path, URL, deserialization, logging, and container exposure;
6. public CLI/config/schema/API compatibility;
7. tests that omit behavior or create false confidence;
8. unnecessary abstractions, dependencies, complexity, or performance regression;
9. inaccurate status, evidence, runbook, or architecture claims.

## Finding format

Use `BLOCKER`, `HIGH`, `MEDIUM`, `LOW`, or `NOTE` severity. Every actionable finding names
the file/symbol, concrete failure sequence, impact, smallest safe correction, missing test,
and confidence. Do not manufacture findings or classify style preference as a safety issue.

- `BLOCKER`: review cannot proceed safely, secret exposure, destructive ambiguity, or the
  change fundamentally violates the requested boundary.
- `HIGH`: likely financial authority, security, data-integrity, or irreversible recovery
  failure.
- `MEDIUM`: real correctness, operability, compatibility, or maintainability defect with a
  bounded workaround.
- `LOW`: narrow defect with limited impact.
- `NOTE`: verified observation or optional improvement, not required for acceptance.

All confirmed `BLOCKER` and `HIGH` findings are corrected before approval. Correct a
`MEDIUM` finding when the change is narrow and does not introduce greater risk; otherwise
record the residual risk in the active plan.

## Required review passes

### Behavior and persistence

- Trace entry point to state owner and external side effect.
- Check validation, units, UTC/session semantics, `Decimal` boundaries, and nonfinite values.
- Check transaction scope, constraints, migrations, rollback, duplicate input, correction,
  ordering, retry, cancellation, shutdown, and restart.
- Prove strategy output cannot bypass risk and order intent precedes broker submission.

### Security

Answer every question for each natural change:

1. What trust boundary changed?
2. What external input changed?
3. Which secrets can each changed component access?
4. Which privileges does each changed component require?
5. What happens when input is malicious?
6. What happens when the component is compromised?
7. Which persistent state can it modify?
8. Can it trigger a broker side effect?
9. Can it execute arbitrary code?
10. Can it access arbitrary files?
11. Can it make arbitrary network requests?
12. Can it leak secrets through logs, errors, metrics, or artifacts?
13. Can duplicate or replayed input produce a side effect twice?
14. Can stale input create an action?
15. Can malformed input bypass validation?
16. Can concurrency violate authorization or idempotency?
17. Which tests prove each mitigation?
18. What residual risk remains?

Every new attack surface needs a concrete mitigation with a test or an explicit residual-risk
entry. A phase cannot be marked complete while that accounting is missing.

Attempt malformed, stale, replayed, oversized, injected, and unauthorized inputs where the change
introduces such a boundary. Confirm dependency directions and verify credential-free behavior with
the current TCP guards; do not claim universal socket denial until it exists.

### Tests and evidence

- Expected results are independent of production logic.
- Tests assert state/outcome, not only mock calls, and include failure/recovery behavior.
- No test, assertion, marker, linter rule, type rule, or CI gate was weakened.
- Executed commands and counts match the report; unavailable checks remain explicit.
- Documentation distinguishes legacy, collector, target, and external validation.

### Simplification

Remove unused protocols, one-path factories, pass-through layers, duplicate parsing,
redundant trusted-boundary validation, dead configuration, excessive comments, unused
dependencies, and public APIs without callers. Retain complexity only when it enforces a
current boundary or recovery invariant.

## Diff and approval packet

Before requesting approval, inspect:

```bash
git status --short
git diff --check
git diff --stat
git diff --name-status
git diff
```

`git diff` omits untracked files. Also run `git status --short --untracked-files=all` and inspect
each proposed untracked file in full or with `git diff --no-index /dev/null -- <path>`. The review
packet must cover the complete proposed boundary and identify other worktree changes that remain
outside it.

Report changed and intentionally unchanged behavior, every file, exact command results,
simplification, trust/input/secret/state/side-effect analysis, replay/concurrency behavior,
security tests, and residual risks. Review approval authorizes only the stated commit
boundary; push, pull request, merge, release, and deployment require separate authorization.

End the packet with a proposed message and request one exact response:

- `APPROVE COMMIT: <message>` authorizes only that reviewed commit;
- `REQUEST CHANGES: <instructions>` requests another review iteration;
- `SPLIT COMMIT: <instructions>` changes the proposed boundary.

Stop before staging or committing until the exact approval is received. The only exception is a
maintainer delegation recorded for a specific branch and scope in the active execution plan. Under
that delegation, retain the same frozen review record and staged-diff checks but do not pause for
another response. A scope change or destructive, credentialed, financially consequential, or
irreversible action requires fresh authority, and commit delegation never implies remote
publication or history modification.
