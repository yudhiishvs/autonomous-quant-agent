## Summary

Describe the observable change and the invariant it preserves or establishes.

## Verification

List every command actually run and its result. Distinguish local, PostgreSQL, container, and
external validation; do not mark an unexecuted check as passing.

## Trust boundaries

- External inputs changed:
- Credentials or secret files accessed:
- Network or broker side effects:
- Persistent state or migration impact:
- Recovery and rollback behavior:

## Safety checklist

- [ ] Tests cover success, malformed input, failure, and replay/restart behavior where relevant.
- [ ] No credential, account identifier, private URL, database, or generated artifact is included.
- [ ] No real-money route or endpoint was added or enabled.
- [ ] Paper submission remains default-deny and was not exercised unless explicitly authorized.
- [ ] Append-only evidence and deterministic identities remain verifiable.
- [ ] Documentation distinguishes verified, externally unvalidated, and deferred behavior.
- [ ] The diff contains no unrelated refactoring or generated files.
