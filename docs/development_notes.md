# Development notes

Two fixes show the kinds of failure this project needs to handle, how they were
reproduced, and how the resulting changes were tested.

## Container security: replacing the base was not enough

The original Debian-based runtime built successfully but failed the security gate with
51 HIGH and three CRITICAL OS findings per image. Updating Python dependencies would
not fix those OS packages. A Bookworm candidate also failed, and a hardened Debian
image still needed advisory exceptions that did not establish application-specific safety.

The adopted runtime uses a digest-pinned Wolfi base, Python 3.11.16, and SQLite 3.53.4.
It preserves the locked Python dependencies and separate platform, collector, and
execution images. Verification covered non-root permissions, absent global installers,
import boundaries, fixture ingestion and Parquet export, and SQLite FTS5 behavior.
All three images passed the strict scan without exceptions.

The tradeoff is maintaining another distribution's package choices. Direct Python and
SQLite versions are pinned; transitive OS packages resolve from the signed repository
and are recorded in build SBOMs. Rebuilds still need fresh scans and behavior checks.
See [PR #19](https://github.com/yudhiishvs/autonomous-quant-agent/pull/19).

## Worker completion: a timestamp race

A full CI run intermittently failed with a transition timestamp older than durable
state. The worker took its final checkpoint while its heartbeat thread was still active.
A heartbeat could then update the database before completion used that checkpoint.
The repository correctly rejected the stale timestamp.

Both job and outbox workers now stop and join the heartbeat before taking the final
checkpoint. The fix preserves lease ownership, attempt fencing, and forward-time
validation. Four deterministic tests force a heartbeat during shutdown and cover success
and failure for both workers. They failed before the change and passed afterward.

This is why a single green run was insufficient: the original failure depended on
scheduling. Reproducing the ordering directly made the regression test useful without
relying on repeated runs or arbitrary sleeps. See
[PR #20](https://github.com/yudhiishvs/autonomous-quant-agent/pull/20).
