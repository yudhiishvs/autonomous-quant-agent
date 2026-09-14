# Local production hardening — 2026-09-12

Status: `PARTIALLY_IMPLEMENTED` for unattended production readiness; the repairs and
local verification listed below are `IMPLEMENTED_AND_VERIFIED`. This is a local working
tree result, not a statement about published main or a guarantee of defect-free software.
AI remains `OUT_OF_SCOPE_FROZEN_AI`; real-money execution remains unsupported.

## Changes

- Preserve the revision-0010 append-only experiment registration grants after migration
  cleanup. No general business-table mutation authority was restored.
- Repair the broken verification documentation link and use the accepted `unknown` build
  revision fallback. An unversioned local build remains non-promotable.
- Bound private API request bodies by a ten-second total deadline, 65,536 bytes, and
  128 Uvicorn connections/tasks. Partial disconnects never dispatch to the application.
- Generate historical expected intervals in UTC-day batches and consume them as an
  iterator. Canonical quality hashes, interval validation, correction fencing and gap
  checks are preserved. Full-history scan CPU and database work remain proportional to
  archive length; this is not an incremental-readiness implementation.
- Suppress routine third-party INFO SQL messages that were converted into misleading
  security errors. Warnings/errors still pass through the existing redaction boundary.
- Limit local Compose JSON logs to five 10 MB files per service, including PostgreSQL.
- Repair offline collector history reads to use its existing canonical-table authority.
- Add migration `20260912_0016`: a security-barrier view exposing only the migration
  revision. Worker startup reads this view without receiving collector-schema access.
- Make the offline scheduler read the existing operational readiness view, including
  pending-work and unresolved-gap denial, instead of a collector-owned table.
- Add real PostgreSQL login coverage for version-read/write denial and the entire offline
  collector/scheduler/strategy/fake-execution session across fresh worker instances.

## Local deployment evidence

Compose project `aqa-local-readiness-20260912` used its own database and volumes. The
operator database and authenticated historical sample were not test targets. The normal
image build succeeded without an explicit revision override, bootstrap and migration
completed, and all eight persistent services were healthy: PostgreSQL, API, dashboard,
job worker, market-data worker, scheduler, strategy worker and execution worker.

The fixture completed 21 slots, 21 reconciliations and six synthetic fills. Restarting
the four domain workers restored healthy status and preserved those counts. The guarded
PostgreSQL regression independently verifies all slots COMPLETED, all reconciliations
CLEAN, final positions flat, and unchanged reconciliation hashes after restart.
No provider request or real/paper broker order was sent; execution used the fake broker.

## Verification

- Network-disabled full offline suite: 3,051 passed, nine PostgreSQL module skips.
- Final combined PostgreSQL suite: 169 passed, zero skips, including both restore
  proofs and the new restricted-login complete-session/restart regression. Two existing
  dependency deprecation warnings remain. The first combined attempt was interrupted
  during the slower final test; the diagnostic rerun completed in 304.38 seconds.
- Full-platform logical backup/restore: passed, including source/restored content and
  privilege equality. Used the disposable PostgreSQL container's native utilities through
  temporary local wrappers; no host package installation or operator database dump.
- Synthetic legacy backtest and replay: passed with outbound networking disabled.
- Formatting (420 files), lint, type checking (151 files) and frozen-AI verification
  (61 protected files): passed. Configured Bandit medium/high-confidence gate: passed.
- Final affected offline tests after the scheduler repair: 55 passed.
- Ten-year empty-event readiness traversal completed in 8.19 seconds on this host;
  it correctly produced no ready watermark. This is a calendar/hash microbenchmark,
  not a populated database capacity or sustained-throughput proof.

## Security and residual risks

The API change narrows the unauthenticated resource boundary. No new external input,
credential namespace, network destination, dependency or financial authority was added.
The migration grants SELECT on a metadata-only view; runtime write denial and lack of
collector-schema access remain tested. Collector and scheduler reads retain their own
existing authority. Restart tests prove no duplicate durable fixture fills; they do not
prove provider-side recovery in live conditions. AI/dependency fingerprints are unchanged.

The September 11 audit remains historical evidence. F2, F3 and F6 are repaired locally;
F1's code repair is local but publication is intentionally outside this request. F4's
calendar memory allocation is reduced, while archive-scale capacity remains unverified.
F5's rounded broker-average compatibility remains unresolved: no documented provider
precision policy was established, so exact evidence checks and disabled paper approval
were preserved rather than adding an arbitrary tolerance.

Before unattended operational use, establish long-history throughput, classify the
sample's research gaps, verify actual streaming/reconnect/lag behavior, and exercise
backup retention, off-host recovery and alert delivery in the intended deployment.
The default offline topology does not establish a live-data strategy pipeline. A universal
backtester and RL environment remain separate product work. No claim of live readiness,
complete external validation or error-free operation is made.

All changes remain local and uncommitted. No PR, commit, push or history rewrite was made.

The isolated Compose test services were stopped and removed after verification; their
volumes were retained. No new live collector was left running. Detailed command logs
are under `/private/tmp/aqa-hardening-*` and may expire with system temporary cleanup.

The disposable `collector_test` container was removed after the final PostgreSQL pass.
