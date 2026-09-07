# Market-data activation evidence

Evidence date: 2026-09-06. Overall Phase Zero status: `PARTIALLY_IMPLEMENTED`.
Real deployment and provider activation: `BLOCKED`.

The local data implementation is under verification. No authorized always-on target,
hosted PostgreSQL connection/CA reference, dedicated Alpaca data-secret file paths, or
initial history-start date has been established for live activation. No real Alpaca
authentication or subscription occurred, no hosted data service was started, and no
always-on or live-data success claim is made. A local Docker build or synthetic database
is not live activation. The Phase Zero gate continues to block unrelated implementation.

This evidence applies to the current inherited working tree based on HEAD
`5659f93ef17c439a121cc584eee000bc34154e4b`, not a new committed or published revision.
The [active plan](../execution-plans/platform-core.md) owns milestone completion;
[USER / Phase Zero requirements](../requirements.md#user--phase-zero-traceability) own
acceptance scope. Local canonical snapshot and guarded PostgreSQL checks now have evidence
below. Final source convergence and remaining performance/review gates are pending; concurrent
checkout edits prevented claiming that the built image matches the current source tree.

## Frozen boundary

The [main-AI manifest](main-ai-freeze.json) records 61 protected paths, reasons, pre-existing
Git state, and working-tree SHA-256 hashes. All 61 hashes matched at the local data
verification checkpoint. The protected baseline includes inherited changes; comparison
against clean HEAD would be the wrong test. Existing research outputs, runtime state,
raw data, and approved artifacts were preserved. No model training was performed.

Recheck before accepting any later milestone:

```bash
UV_CACHE_DIR=/private/tmp/aqa-uv-cache uv run --no-sync python scripts/verify_main_ai_freeze.py
```

No third-party dependency version or artifact was changed for the data image. The existing
lock was reconciled using `uv lock --offline` only to include already-declared root dependency
group metadata; comparison against the start snapshot retained all 143 distinct third-party
package records. This is dependency-preservation evidence, not a vulnerability-scan result.

## Recorded local checks

| Check | Observed result | Scope and limit |
| --- | --- | --- |
| Operations/calendar, deployment contract, and container entrypoint selection | 25 passed in 1.04s | Pure and isolated process tests; includes exact roles, closed sessions, DST, early close, first-minute lag, service isolation, and file-secret runtime boundary. |
| Guarded PostgreSQL operations suite | 21 passed in 10.88s | Real local PostgreSQL 16 with synthetic fixtures and actual collector-role permissions; no provider credentials or external database. |
| Data-only dump/restore case | 1 passed in 1.38s | Exact row counts and sorted row hashes matched across 36 tables / 43 rows; raw observation, canonical and checkpoint state included. |
| Canonical snapshot, existing dataset contracts and deployment tests | 63 passed in 2.22s | 25 new snapshot tests, 34 existing dataset tests and 4 deployment tests; includes readback corruption rejection, metadata uncertainty, correction lineage and consistent reads during a concurrent correction. |
| Later data PostgreSQL selection | 53 passed in 23.81s | Guarded disposable database; log `/private/tmp/aqa-final-data-postgres.log`. This is a data selection, not every platform integration test. |
| Final targeted snapshot and empty-history recovery cases | 2 passed in 9.16s | Restricted collector-role lifecycle exports 165 canonical snapshot rows and reuses the registration; empty historical REST coverage becomes explicit gap/recovery state. |
| Later offline data selection | 440 passed, 1 warning in 16.76s | Log `/private/tmp/aqa-final-data-offline.log`; the warning concerns the installed WebSocket legacy API. This is not a green whole-repository claim. |
| Focused Ruff format/lint and mypy | Passed for the three new operations/deployment test files | Does not certify repository-wide formatting, lint, or types. |
| Dedicated Compose configuration | Passed `config --quiet` | Validates interpolation and service configuration; does not start a service or prove mounted secret ownership. |
| Isolated market-data image build and runtime smoke | Build and CLI/snapshot help/import/CA checks passed; missing-secret startup failed closed | Exact image and 79 source/config/migration file hashes are retained in the immutable local release record. Three checkout files differed during verification; the image is not certified as the current checkout or deployed. |
| Frozen manifest | 61 protected files unchanged | Recheck at final verification and subsequent milestones. |

Exact focused command:

```bash
UV_CACHE_DIR=/private/tmp/aqa-uv-cache uv run --no-sync pytest -q tests/test_collection_operations.py tests/safety/test_market_data_deployment.py tests/safety/test_container_entrypoint.py
```

The later snapshot selection was:

```bash
UV_CACHE_DIR=/private/tmp/aqa-uv-cache uv run --no-sync pytest -q tests/test_collection_snapshots.py tests/unit/test_platform_datasets.py tests/safety/test_market_data_deployment.py
```

The legacy version-one known dataset identity and all 34 existing dataset tests still pass.
Optional version-two collection provenance records all 29 members, XNAS/calendar version,
historical coverage conditions, and scoped listing/corporate-action evidence. Missing metadata
is explicitly unknown; a diagnostic export cannot become promotable. Trusted source evidence
can be supplied without changing the frozen research definition. Changed evidence or canonical
corrections change dataset identity. Staged Parquet is read back before publication, and the
collector registers manifests using its existing advisory lock without needing UPDATE authority.

The PostgreSQL checks used a disposable container created during this task:
`aqa-phasezero-pg`, PostgreSQL 16, published only on `127.0.0.1:55432`, database and
synthetic test principal `collector_test`. The guarded suite may drop its test schemas
and install cluster roles, so it ran serially with both explicit test acknowledgements.
The fixture URL below contains only public synthetic test credentials; it must never be
replaced with an operational database for these tests.

```bash
APA_TEST_POSTGRES_URL=postgresql+psycopg://collector_test:collector_test@127.0.0.1:55432/collector_test APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES UV_CACHE_DIR=/private/tmp/aqa-uv-cache uv run --no-sync pytest -q tests/integration/test_collection_operations_postgres.py # pragma: allowlist secret -- synthetic loopback fixture
```

That 21-case invocation preceded addition of the separate opt-in restore case. It exercised
immutable universe/history configuration and collector permission denials; empty versus
healthy service state; exactly one fenced canonical run; subscription failure precedence;
all-29 same-feed REST checkpoint coverage; pending derived work; actual latest canonical
bar freshness; required-series gaps; and aggregate basket readiness. Broader persistence
and end-to-end results are recorded separately after the final combined run.

The restore case then ran with the same guards and an explicit test-container identity:

```bash
APA_TEST_POSTGRES_URL=postgresql+psycopg://collector_test:collector_test@127.0.0.1:55432/collector_test APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES APA_TEST_POSTGRES_CONTAINER=aqa-phasezero-pg UV_CACHE_DIR=/private/tmp/aqa-uv-cache uv run --no-sync pytest -q -s tests/integration/test_collection_operations_postgres.py::test_raw_canonical_and_checkpoint_state_survives_data_only_restore # pragma: allowlist secret -- synthetic loopback fixture
```

It verified the container's published port against the guarded URL, used container-native
`pg_dump` and `pg_restore`, and restored only `aqa` and `market_data` without roles, ownership,
or ACLs into a separately created `collector_restore_test` database. A pre-existing restore
database causes refusal. The test removed only the restore database it created and returned
the shared test database to the suite's cleanup boundary.

Measured result:

```json
{
  "evidence": "SYNTHETIC_DATA_ONLY_RESTORE",
  "table_count": 36,
  "row_count": 43,
  "source_and_restore_identical": true,
  "snapshot_sha256": "76c9745a86f86477bf90c7825d92a173e2f3b10672ab66f6625d7aeb06886076"
}
```

The hash identifies the equal source/restored snapshot for this run. Runtime identifiers
and timestamps mean a fresh fixture need not produce this same hash. The schema also
contains empty platform tables; their inclusion proves restore fidelity and does not invoke
or certify AI, research, risk, or execution behavior. This bounded synthetic recovery does
not validate the future host's backup storage, retention, encryption, point-in-time recovery,
or operator recovery procedure.

Local logs were kept outside Git at `/private/tmp/aqa-collection-operations-pg.log`,
`/private/tmp/aqa-data-only-restore.log`, and
`/private/tmp/aqa-market-data-phase-zero-build.log`. They are temporary execution records;
the measured outcomes above are the durable non-secret summary.

## Container and runtime boundary

The dedicated deployment is `docker-compose.market-data.yml`, using the Dockerfile
`market-data` target and `python -m adaptive_trader.collection.cli run`. It mounts only
the collector database URL and two data-credential files. It runs as UID/GID 10001 with
a read-only root, dropped capabilities, bounded tmpfs/resources/logs, a 90-second stop
allowance, and `restart: unless-stopped`. Migration credentials remain separate.

The isolated image was built locally as `aqa-market-data:phase-zero`, with image ID
`sha256:166ce790b282e1543b41437602ac3a96e60c40a03864547975eacd8eae357a9d`.
The [immutable release verification record](market-data-release-166ce790b282.json) contains
the exact build command, labels, dependency inventory and complete image source hash map.
Its record SHA-256 is
`29dc0cada5a0b19bccdc19ee90c19932ff89b53c9ab1b331b900bb256521432f`.
The local tag is mutable; use the image ID to identify this verified artifact.

Network-disabled, read-only containers verified CLI help and snapshot help, packaged
migration/config resources, 150 CA trust anchors, UID/GID 10001, and absence of broker,
strategy, risk, execution, scheduling, worker-runtime, and trading SDK imports. `run` and
`ready` without a database secret exited 1 with the sanitized message
`AQA_DATABASE_URL_FILE or APA_MARKET_DATA_DATABASE_URL must be set`. No image was published.

The image source map contains 79 files. At comparison time, 76 matched the checkout and
three differed: `migrations/env.py`,
`migrations/versions/20260906_0012_scheduler_reconciliation_view.py`, and
`src/adaptive_trader/platform/storage/tables.py`. These were concurrent changes outside this
slice; they were preserved. The strict comparison therefore exited 1. The source-map hash is
`3ea8fb11cd0fd0761a601b05de24e23910adb27ed4afd38eaab818f1f7e22f14`.
The Dockerfile hash was not captured at build time. The full PostgreSQL/data suite ran from
the working tree, so it is not claimed as a full integration run inside this exact image.
The record preserves a locally tested image while source convergence remains explicit.

Docker became available for disposable local verification after the baseline daemon probe.
That change supplies a local test runtime only. It does not establish an authorized
continuously available deployment host, external database, or provider entitlement.

## Inherited baseline failures

The pre-edit baseline used the writable temporary uv cache and produced:

```text
uv run --no-sync pytest -q -m 'not postgres'
1806 passed, 4 failed, 5 errors, 8 skipped in 92.07s

uv run --no-sync ruff check .
4 inherited demo findings

uv run --no-sync mypy src docker
1 inherited demo type error
```

The recorded failures included Compose/image contract drift, migration-head expectations,
service CLI expectations, and offline-demo canonical serialization. These are failures,
not waived or passing gates. A later check may supersede individual results only with
actual new evidence. Unrelated demo/AI work remains outside Phase Zero; no repository-wide
completion is claimed from the focused data checks. The first default-cache uv attempt
failed because of sandbox cache access; use of the temporary cache allowed the baseline.

## External activation requirements

| Required evidence | Current observation | Status |
| --- | --- | --- |
| Authorized always-on host or existing service identity, access method, boot/supervisor policy | No target reference supplied; no new paid resource created | BLOCKED |
| Persistent private PostgreSQL target, runtime and migration file references, CA/trust policy and backup owner | No operational database or secret-file references supplied | BLOCKED |
| Dedicated Alpaca data-key and secret-key file references readable only by the service identity | No paths supplied; no secrets inspected, logged, or committed | BLOCKED |
| Approved initial UTC history start and persisted 29-symbol universe identity | Initial range not supplied for activation; immutable local configuration is tested | BLOCKED |
| Real historical authentication, entitlement, IEX/raw request, persisted readback, and intended catch-up | Not attempted without the required target and credentials | BLOCKED |
| Real WebSocket authentication and exact 29-symbol acknowledgement | Not attempted | BLOCKED |
| Calendar-aware open/closed-market observation, latest completed session, gaps, fresh canonical bars, aggregates, and snapshot | No real provider or hosted state measured | BLOCKED |
| Controlled restart and failure recovery, singleton ownership and no duplicate economic data | Local synthetic behavior tested; deployed service behavior not measured | BLOCKED |
| Active host monitoring, alert/recovery ownership, external backup/restore, current service health | No deployed service to inspect | BLOCKED |

Supply non-secret target identifiers, the access mechanism and secret-file **paths**, the
database CA reference where needed, and the initial UTC history start. Credentials belong
in the authorized secret backend, never in chat or this document. The
[market-data runbook](../market_data_runbook.md) supplies reproducible migration, start,
status, recovery, and evidence commands. Deployment on an existing authorized target is
within the task scope; new spending still requires separate explicit authorization.

After local verification is complete and those inputs exist, execute real activation,
record non-secret proof tied to the deployed revision/image and target, rerun the frozen
manifest check, and verify the process is still healthy before reconsidering the Phase
Zero gate. Until then, the live gate is unmet and post-data implementation stays blocked.
