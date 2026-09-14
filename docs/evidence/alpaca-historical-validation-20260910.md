# Alpaca historical validation — 2026-09-10

Scope: an authenticated IEX/raw minute-bar sample for the configured 29 symbols,
2026-09-08 inclusive to 2026-09-10 exclusive. Compose project
`aqa-data-validation-20260910` used an independent PostgreSQL volume and network.
No trading processes or AI components ran. Credentials were loaded by the existing
collector from private files and were not displayed.

The platform used source revision `6e2850dfaebc490b8a6650e1db6863de6d03e06d`
with the local, uncommitted migration-registration repair. This is local activation
evidence, not a published release or clean-source certification.

## Observed results

- Authentication and historical retrieval succeeded for all 29 symbols.
- A graceful Docker stop interrupted the first run after persisted data; it exited
  with the expected InterruptedError and its durable run status became stopped.
- Restarting the same bounded backfill completed. Replaying it again also completed.
- Final current-bar count: 17,325. All 29 checkpoints reached 2026-09-10 00:00 UTC.
- Canonical output: 17,325 one-minute identities and 319 fifteen-minute identities.
  Maximum canonical revision remained 1 after replay; no duplicate current identities.
- The ordered current identity/content fingerprint was identical before and after
  replay. The comparison used PostgreSQL MD5 solely as an equality diagnostic.
- Independent SQL checked OHLC ordering, nonnegative volume and requested time bounds;
  no violations were found. Each of the 319 aggregates matched 15 source minutes for
  open, high, low, close and summed volume.
- Final pending derived work and active run/lease counts were zero. No provider or
  reconciliation errors were reported in the status window.
- There were 51,651 raw observations across the interrupted/repeated requests. V2
  intentionally retains distinct receipt evidence; canonical economic bars stayed stable.
- Operational database readback remained at zero current bars and zero collection
  configurations. Its coverage boundary was not initialized by this test.

## Limits and next work

The sample is not research-ready. Canonical gap inspection found 559 open gap records;
final status reported 486 unresolved research gaps. A missing IEX minute is not by
itself proof of a transport failure or of no trading: classify the missing intervals
before relaxing any readiness gate. The completed historical cutoff is deliberately
behind the current session, and no stream was started.

This establishes bounded real historical intake, graceful stop/restart, replay stability
and aggregate arithmetic. It does not establish crash recovery, streaming, oldest
available feed/symbol history, listing/corporate-action completeness, full backfill,
backup/restore or always-on operation. The collector's single immutable history boundary
and listing evidence requirements need review before configuring a long-term archive.

The isolated PostgreSQL container is stopped with its sample volume retained. It is not
a disposable pytest target. No commits, pushes or PRs were created.
