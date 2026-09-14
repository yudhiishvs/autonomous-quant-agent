# Local readiness validation — 2026-09-13

Status: `PARTIALLY_IMPLEMENTED` for unattended production readiness. The verified
local behavior below does not establish published-main status or live trading readiness.
All work remains local and uncommitted. AI is `OUT_OF_SCOPE_FROZEN_AI`; no broker order
was submitted. This report continues the September 11 audit and September 12 repairs.

## Changes and measurements

Readiness now retains a verified in-memory prefix through the previous UTC day. Current
session updates scan the suffix; older queued corrections and changed historical gap
fingerprints force a full rebuild. The digest is copied before reuse and remains exactly
equal to the original full-history hash. Cold starts rebuild the archive in bounded daily
batches. No hash state is serialized, deserialized or accepted through an external API.

Migration `20260913_0017` invalidates external minute and aggregate projection changes
in the same transaction, including direct collector-role writes and replacement of
external provenance. It uses SECURITY INVOKER, a fixed search path, qualified tables and
existing privileges. Aggregate creation can leave work queued for another idempotent
drain; pending work continues to deny operational readiness. Ordinary offline fixture
writes are excluded unless replacing previously external evidence.

The populated benchmark used PostgreSQL 16 in its own disposable loopback container,
limited to 1.5 CPUs and 1.5 GiB. It seeded canonical identities, immutable events and
latest projections directly in daily batches. It exercises the actual validating reader,
revision-chain checks and quality hash; it does not measure downloads, audit/intake
writes, derived processing or full-universe throughput.

| Measurement | Result |
| --- | --- |
| One-year baseline | 96,960 bars; 250 sessions; 26.16-second full scan |
| Ten-year baseline | 976,680 bars; 2,514 sessions; 260.23-second full scan |
| Ten-year continuation proof | Same 976,680 bars; 267.91-second cold scan |
| Cached final-session scan | One session; 0.099-second scan |
| Full/cached result equality | Exact equality, including quality hash |
| Peak Python RSS for continuation run | 203,292,672 bytes on macOS (about 194 MiB) |
| Continuation fixture seed time | 514.11 seconds; not an intake-rate measurement |

The ten-year quality hash was
`c359daebf83079ccb74068a7fc266782b3ca01b79341cfe59f87d1d3b9885c2b`.
Different runs share host resources, so these timings are observations, not a latency
SLA. Cold restart/recovery remains proportional to archive size. A complete 29-symbol
pipeline capacity and recovery-time target is still unverified.

## External data evidence

On Sunday, September 13, saved data credential files authenticated and subscribed to
IEX on two independent connections. No actual minute bars arrived during those bounded
observations. The provider's always-available synthetic test stream delivered two test
trades on each of two connections. Those events were not persisted as research data.

The permanent `scripts/verify_iex_stream.py` requires explicit `--allow-provider`, uses
two sequential IEX connections and checks receipt age. It returned
`IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` because both subscriptions had zero bars. Its
report explicitly leaves persistence and unattended-operation validation false. Eleven
unit tests cover no bars, stale/future bars, missing subscription, provider failure,
secret-free failure reporting, both connections, closure and argument acknowledgement.

These observations prove credential/endpoint access and synthetic transport only.
[Alpaca streaming documentation](https://docs.alpaca.markets/us/docs/streaming-market-data)
distinguishes the test feed from market feeds. No provider precision contract was found
that safely resolves the audit's F5 rounded-average reconciliation case. Exact financial
evidence checks and disabled paper authority remain in force.

## Verification and deployment

- Full network-disabled offline suite: 3,065 passed, ten PostgreSQL module skips.
- Full PostgreSQL suite before the aggregate-invalidation extension: 170 passed, one
  explicitly opt-in capacity skip. Both logical restore proofs ran with PostgreSQL's
  native utilities through temporary wrappers against disposable `collector_test`.
- The capacity test ran explicitly and passed separately; normal suites do not run a
  ten-year destructive capacity exercise implicitly.
- Both synthetic legacy backtest and replay passed with outbound networking disabled.
- Formatting (426 files), lint, typing (151 source files), frozen-AI verification
  (61 protected files), diff hygiene and the configured Bandit gate passed.
- The platform image built successfully. Isolated project
  `aqa-readiness-validation-20260913` completed 21 slots, 21 reconciliations and six
  fake fills. All eight persistent services were healthy before and after restarting
  the four domain workers; the counts were unchanged. Its volumes were retained.

The final aggregate-invalidation extension exposed two test-fixture issues: an aggregate
receipt timestamp incorrectly preceded its interval end, and a manually seeded shadow
fixture did not acknowledge its newly enforced pending work. The latter now first asserts
that pending work blocks execution, then explicitly acknowledges the synthetic fixture's
queue after its manual watermark calculation. Runtime role denial remains unchanged.
Final PostgreSQL rerun: **171 passed, one opt-in capacity skip**, 3,065 deselected,
238.20 seconds. Two existing dependency deprecation warnings remain. Both restore
proofs ran. The separate populated ten-year capacity test passed in 784.71 seconds.

The final rebuilt image was deployed to a fresh isolated project,
`aqa-readiness-final-20260913`, at revision 0017. All eight services became healthy;
21 slots, 21 reconciliations and six fake fills were verified before and after worker
restart. Both isolated Compose projects were stopped and removed after verification,
with volumes retained. The disposable PostgreSQL test container and the synthetic
capacity container/volume were removed. No new live collector was left running.

## Review and remaining acceptance work

The changed trust boundary is readiness reuse, plus an explicit operator stream probe.
No dependency, credential namespace or financial authority was added. Immutable event
verification remains on cold/suffix reads; earlier mutation is excluded by queued work
and gap fingerprints. The PostgreSQL trigger covers authorized projection changes;
privileged database administrators remain capable of bypassing controls. A compromised
collector already has data-write authority; it gains no trading permission here.

Known-answer digest tests, repeated prefix reuse, historical corrections, changed gaps,
projection tampering, transactional rollback, actual restricted database logins and
publication fencing cover the relevant recovery and concurrency cases. Duplicate
materialization does not append another revision. The probe uses a fixed data endpoint,
loads credentials through the existing file boundary, suppresses provider exception
text and prints only state/count/timestamp summaries. It cannot invoke broker code,
execute commands from provider input or choose arbitrary files/URLs. The benchmark's
reset requires the existing loopback database and both disposable-test acknowledgements.

The review found no unresolved local BLOCKER/HIGH finding in this change. The following
production acceptance work remains open and is not waived by these tests:

1. Real market-session receipt, durable persistence, reconnect and lag evidence.
2. Full-universe intake capacity, historical availability and research-gap classification.
3. Provider rounded-average reconciliation compatibility before any paper activation.
4. Sustained operation, alert delivery, backup retention and off-host recovery in the
   intended deployment, including an accepted cold-rebuild recovery time.

A weekday 10:00 America/New_York follow-up, “Validate market-session data,” is scheduled
in this task. It must preserve local-only/data-only authority and pause after validation
or a persistent user-action blocker. The computer and desktop app must remain running.
This follow-up does not itself establish any acceptance result.

Detailed temporary logs are under `/private/tmp/aqa-readiness-*` and
`/private/tmp/aqa-capacity-*`; they may expire. Operator databases and the previously
saved historical sample were not reset or used as integration-test targets.
