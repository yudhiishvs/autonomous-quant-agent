# Repository production audit — 2026-09-11

**Verdict: not ready for an unattended production deployment.** The repository has
substantial tested engineering, but passing most checks does not establish reliable
live operation or a research-ready historical archive.

## Scope and baseline

Reviewed local HEAD `6e2850dfaebc490b8a6650e1db6863de6d03e06d` plus the existing
uncommitted migration-registration repair and documentation changes. Published main
and this working tree are therefore different deliverables. This audit did not fix
runtime code, publish changes, activate collection or submit orders.

Coverage includes legacy backtest/replay and strategy interfaces, collector/provider
boundaries, canonical storage and aggregates, datasets, schema migrations and roles,
scheduler/job/worker lifecycles, risk and paper execution, API/dashboard boundaries,
Docker/Compose, CI/dependency controls, packaging, documentation and recovery tooling.
Automated scanning covered the runtime tree; manual review concentrated on entry points,
trust boundaries, state transitions and known failure paths. This is a repository-wide
engineering audit, not proof that every line or every possible production scenario is
correct. The 61 protected AI files were verified unchanged;
model performance and AI suitability are not certified.

No operational database, provider credentials or collected raw-data files were opened.
PostgreSQL tests used a new, isolated `collector_test` container. Scanner output was
redacted; findings below do not contain credentials.

## Findings requiring action

### F1 — HIGH: published migration cleanup breaks deployment registration

Location: [migrations/env.py](../../migrations/env.py:139),
[migration_runner.py](../../src/adaptive_trader/platform/storage/migration_runner.py:211).

On published main, final migration cleanup revokes INSERT on all business tables,
including the experiment-registration exception introduced in revision 0010. The CLI
can reach schema head and then fail at experiment registration. This was reproduced
in actual local activation on September 10 and by the new regression against the
original migration environment.

**State:** fixed and tested in the working tree, still uncommitted. Current integration
tests include that fix; their success cannot be attributed to the published revision.
A deployment built from published main still lacks it.

**Remediation:** review and publish the narrow registration-grant repair, retaining the
fresh-deployment/retry regression and exact privilege assertions. Do not grant broad
business DML or reset an existing database to work around this problem.

### F2 — MEDIUM: removed README anchor breaks CI and Security

Location: [.agents/skills/backtest-validation/SKILL.md](../../.agents/skills/backtest-validation/SKILL.md:15).

The link to `README.md#verification-and-test-plan` no longer resolves. The local full
offline run fails `test_skill_links_are_tracked_repository_authorities`; published CI
and Security fail on the same assertion. This is a documentation-link defect, not a
vulnerability identified by the security scanner.

Evidence: [CI run](https://github.com/yudhiishvs/autonomous-quant-agent/actions/runs/34224877940),
[Security run](https://github.com/yudhiishvs/autonomous-quant-agent/actions/runs/34224878006).

**Remediation:** point the authority link at the appropriate existing documentation
section or restore a stable anchor. Keep the link-integrity test and rerun it after
editing. The earlier narrow documentation checks did not cover this skill-link test.

### F3 — MEDIUM: default Compose build argument fails validation

Location: [docker-compose.yml](../../docker-compose.yml:10),
[write_build_provenance.py](../../docker/write_build_provenance.py:18),
[Makefile](../../Makefile).

Compose defaults `AQA_VCS_REF` to `local`; provenance validation accepts only `unknown`
or a full lowercase 40-character revision. Consequently, an unset revision makes
ordinary Compose builds fail. The same mismatch appears in collector/execution build
arguments and affects convenience Make targets. Passing `local` directly reproduces
`ValueError: build source revision must be a full lowercase Git hash`.

**Remediation:** use a consistent valid fallback or resolve the revision explicitly in
the supported entry point, with a test for the default invocation. Retain truthful
dirty/unknown provenance; do not manufacture a clean-build claim.

### F4 — MEDIUM: long-history readiness work grows with the entire archive

Location: [derived.py](../../src/adaptive_trader/collection/derived.py:468),
[calendar.py](../../src/adaptive_trader/platform/data/calendar.py:167).

Every readiness refresh constructs expected intervals from the original history start
and reads effective historical events in batches. Batch reads bound individual database
queries, but do not bound the total history processed per refresh. Each dirty symbol
causes this refresh during a drain.

A calendar-only local probe for 2016-01-04 through 2026-09-10 constructed 1,043,760 minute
intervals in 1.32 seconds, with process peak RSS approximately 244 MiB, before reading
any bars or verifying their hashes. These measurements are machine-specific, and do
not prove an OOM or live latency failure. They demonstrate work proportional to archive
length in a path that must repeatedly keep up with new data. The collector has a 768 MiB
container limit and no demonstrated decade-scale throughput.

**Remediation:** benchmark the complete readiness drain on representative long history,
then use durable incremental coverage/checkpoints with bounded invalidation for historical
corrections. Preserve the generation fence and missing-data checks. Do not launch a
maximum-history archive assuming the two-day test establishes its capacity.

### F5 — MEDIUM: exact broker-average equality rejects rounded fill summaries

Location: [alpaca_paper.py](../../src/adaptive_trader/platform/execution/alpaca_paper.py:270).

The adapter compares the broker's reported average exactly with a Decimal weighted
average reconstructed from fills. An injected order with three one-share fills at
100, 100 and 101 and reported average 100.333333 raises
`paper order average differs from execution evidence`. Full quantities and execution
IDs agree. The existing tests exercise contradictory averages but not a documented
rounding policy.

**Qualification:** this is a reproduced compatibility limitation using injected input,
not evidence that a real Alpaca response with that precision was observed during this
audit. The current paper approval gate denies execution anyway. The failure is safe
but would obstruct reconciliation if such valid rounded summaries are received.

**Remediation:** establish the provider precision contract and test it before enabling
paper execution. Reconcile using execution evidence and a justified precision policy;
do not introduce an arbitrary tolerance or discard execution-ID checks.

### F6 — MEDIUM: request body is consumed before authentication without a deadline

Location: [control/api.py](../../src/adaptive_trader/platform/control/api.py:143),
[runtime.py](../../src/adaptive_trader/platform/runtime.py:219).

The middleware waits for the entire request body before reaching authentication.
The byte limit bounds payload size, but there is no receive deadline in that loop;
the server invocation also supplies no application concurrency limit. An async probe
with a stalled body needed an external timeout and never reached downstream auth.
A client able to reach this private API can hold requests open without authenticating.

**Qualification:** Compose binds the API to loopback. This is a hardening issue for
reachable deployments, not evidence of current public exposure or an authentication
bypass. The probe establishes missing application timeout behavior, not a load-tested
network denial of service.

**Remediation:** enforce an explicit body deadline and bounded request concurrency,
or demonstrate equivalent mandatory ingress controls. Add slow/incomplete-body tests.

## Operational and product gaps, distinct from defects

- The authenticated two-day sample has 486 unresolved research gaps. Its successful
  download and replay do not establish research readiness. IEX missing minutes must be
  classified; listing and corporate-action metadata remain separate evidence requirements.
- Earliest usable feed/symbol history, a complete long-range backfill, live WebSocket
  receipts, sustained lag/reconnect behavior and unattended operation remain unverified.
- The execution approval contract deliberately cannot approve a proposal in this release.
  This is an intentional safety boundary, not a bug to bypass. Live-money execution is
  unsupported; a universal backtester and RL environment are not delivered features.
- The default Compose workers use the offline profile. Starting that topology is not
  evidence that the existing live-data stream drives a complete operational strategy loop.
- Data-only dump/restore passed on synthetic isolated state. The separate full-platform
  backup proof could not run because native PostgreSQL utilities are absent. No operational
  backup schedule, off-host copy or recovery-time objective was established in this audit.
- Full local Git history still includes obsolete pre-rewrite branches. A scan of all refs
  reports 234 metadata/checksum matches from old commit `eba2e9a`; current HEAD history
  scans clean. Reconcile the obsolete refs deliberately rather than expanding ignore rules.
  No new credential exposure was established by these matches.

## Fresh validation results

| Check | Result |
| --- | --- |
| Ruff lint and formatting | Pass; 416 files formatted |
| Mypy `src docker` | Pass; 151 source files |
| Frozen AI integrity | Pass; 61 files and dependency fingerprints unchanged |
| Offline suite, network-disabled wrapper | 3,045 passed, 1 failed, 9 skipped; failure is F2 |
| PostgreSQL suite on fresh disposable cluster | 159 passed, 2 skipped |
| Follow-up data-only dump/restore | 1 passed; resolves one integration skip |
| Full-platform backup/restore | Not run: native createdb/dropdb/pg_dump/psql unavailable |
| Synthetic backtest and replay commands | Pass |
| Offline deterministic demo | Pass; stable manifest hash beginning `1805e436` |
| Clean wheel install, doctor and demo outside checkout | Pass |
| Bandit configured medium/high-confidence gate | No reportable findings; this is not a claim of zero low-confidence warnings |
| Locked Python dependency advisory audit | No known vulnerabilities found |
| Gitleaks current HEAD ancestry | Pass; 41 commits, configured exact exceptions retained |
| Gitleaks all local refs | 234 old-ref metadata/checksum matches; see limitation above |
| Platform and market-data local image HIGH/CRITICAL scans | No findings |

Checks used existing locked tools and current advisory retrieval. Images are local
ARM64 artifacts; these scans do not certify a fresh AMD64 deployment or the database
image. The execution image scan is recorded separately below because that existing
image is not a fresh build of the working tree. Full coverage thresholds were not
rerun with combined coverage instrumentation, and a hosted full-stack deployment was
not exercised. Automated checks cannot certify the absence of security defects.

## Recommended order

1. Resolve F1's publication gap and F2/F3 so a fresh checkout builds and verifies reliably.
2. Establish data-gap/listing/corporate-action policy and long-history capacity (F4).
3. Validate streaming, startup-to-dashboard behavior, alerts and operational restore drills.
4. Address F6 before exposing the API beyond its current private boundary.
5. Resolve F5 and the separately scoped approval implementation before paper execution.

The immediate project priority remains reliable non-AI infrastructure, not additional
models or reinforcement-learning features. No runtime changes were made by this audit.

The existing `aqa-execution:wolfi` image also returned zero HIGH/CRITICAL findings.
The disposable audit database was removed after verification; operational and historical
sample databases were not changed. Detailed local command logs are retained under
`/private/tmp/aqa-audit-20260911/` and may be removed by system temporary-file cleanup.
