# Non-AI architecture verification — 2026-09-06

## Acceptance state

The non-AI implementation and **canonical `make check` pass locally**. Overall acceptance and
activation remain **BLOCKED**: all three final images retain 54 OS findings each (51 HIGH and
3 CRITICAL) at the strict vulnerability gate. No quality floor, scanner severity or dependency
lock was weakened. The final test/coverage results are 3,039 offline passes, 160 real PostgreSQL
passes, 82.19% repository coverage and 85.69% platform coverage with branch measurement enabled.
Machine-readable results are in [the result receipt](non-ai-verification-results.json).

The working tree was already extensively modified at the start. Existing files and research/output state were preserved. No commit, push, deployment, Alpaca request, real credential inspection, account access, training, or external broker submission occurred.

## Requirement traceability

[The complete requirement index](non-ai-requirement-traceability.md) maps all 173 actual requirement IDs to implementation and concrete verification files. Frozen AI is a separate scope classification, `OUT_OF_SCOPE_FROZEN_AI`. Historical status tables are not evidence that this final checkout passed all gates.

## Implemented and reviewed changes

- Scheduled finite market-data catch-up and durable canonical processing; receipt-time/creation-time dataset causality checks; separate durable fixture ingestion, aggregation, and immutable Parquet freezing.
- Current-clock operational scheduling and AlwaysFlat strategy production; PostgreSQL role-separated shadow proposals and dry risk/execution plans; durable data/scheduler status with explicit unavailable state and optional fixture preview.
- Two immutable risk/execution stages for reversal, fresh post-close authorization and reconciliation, restart recovery, SQL UTC normalization, and append-only execution evidence verification.
- Bounded default-deny paper authorization consumption; no model-provider discovery or broker credentials in the gate path. Main AI approval remains frozen and unavailable rather than fabricated.
- Shared owner-private health evidence with no-follow directory access, atomic publication, strict numeric values, readiness withdrawal and cleanup; persisted metrics distinguish unavailability from zero.
- Least-privilege migrations through `20260906_0015`; logical backup/restore verifies content, audit and effective ACLs, preserves grants, rejects inherited libpq routing overrides and restores only into a uniquely named drill database.
- Complete-source CLI, runtime and installed-package boundaries; isolated platform/data/execution images; explicit build provenance for immutable datasets.
- Paper SDK facade retrieves bounded actual fill activities, preserves execution IDs, validates cancellation identity before its side effect, and hashes the complete broker observation. A documented paper-only simulated-fee policy and strict average-price check fail closed on unsupported evidence.
- Historical response symbol aliases must match their requested group; signal installation/restoration failures cannot bypass database engine disposal. New SQL outage/retry tests verify atomic rollback of execution evidence.

## Engineering harness

`make check` verifies frozen bytes and lock consistency, formatting/lint/types, socket-denied offline tests plus explicitly guarded PostgreSQL coverage, unchanged 74% repository/85% platform thresholds with two-decimal enforcement, disposable legacy regressions and demo, security/dependency/hooks, clean wheel installation, Compose and deterministic benchmarks. `.coveragerc` includes worker subprocesses and normalizes the explicitly cloned image-source test path without excluding production code. CI combines required offline and PostgreSQL coverage artifacts before enforcing the same floors. The default zero-decimal coverage report initially rounded 84.56% up to 85%; precision is now 2 and additional substantive failure tests measured 85.71% before the settled full rerun. No production exclusions were added.

Pinned pre-commit hooks include serial detect-secrets processing. Full working-tree hook verification includes tracked and untracked files without staging. Exact reviewed synthetic findings and evidence fingerprints are recorded; no broad secret-scan exclusions were added. Image workflows build all three runtime targets, verify non-root/import boundaries, generate SBOMs and retain a strict HIGH/CRITICAL gate. Invalid scanner/SBOM action commit references were replaced with verified official immutable release commits.

## Command ledger

Commands run from the repository root. Local Python invocations use `UV_CACHE_DIR=/private/tmp/aqa-uv-cache`; hook invocations use `PRE_COMMIT_HOME=/private/tmp/aqa-precommit`.

```sh
uv sync --locked --all-extras --group security
uv lock --check
uv run --no-sync python scripts/verify_main_ai_freeze.py
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src docker
make check
uv run --no-sync python scripts/verify_offline_commands.py regression
uv run --no-sync python scripts/verify_offline_commands.py demo
uv run --no-sync python scripts/verify_clean_package.py
uv run --no-sync bandit -c pyproject.toml -r src docker -ll -ii
uv run --no-sync pre-commit run --all-files
uv export --locked --all-extras --group security --no-emit-project --no-editable --format requirements-txt --output-file /private/tmp/aqa-non-ai-audit-requirements.txt
uv run --no-sync pip-audit --requirement /private/tmp/aqa-non-ai-audit-requirements.txt --require-hashes --disable-pip --progress-spinner=off
docker compose --env-file .env.example -f docker-compose.yml config --quiet
uv run --no-sync python scripts/benchmark_pipeline.py --warmups 2 --repeats 5 --iterations 25
git diff --check
git status --short
```

PostgreSQL commands use only the task-created `aqa-non-ai-tests-20260906` container on `127.0.0.1:55436`, the disposable database `collector_test`, and both `APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES` and `APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES`. The public synthetic test account is `collector_test`. `APA_TEST_POSTGRES_CONTAINER` selects that exact container. Native PostgreSQL utilities are provided through a temporary argument-vector Docker-exec bridge pinned to this container and port; actual PostgreSQL 16 commands perform the dump and restore. No PostgreSQL test ran against a hosted or operator database. Runs were serialized.

```sh
uv run --no-sync pytest -q -m postgres
uv run --no-sync pytest -q -m postgres --cov=adaptive_trader --cov-branch --cov-report=term
uv run --no-sync pytest -q tests/integration/test_platform_backup_restore.py
```

Application images were built locally with `docker build --target TARGET --tag TAG .` for `application`, `market-data`, and `execution`; the application build supplied the actual checkout revision with `AQA_VCS_REF`. Network-disabled runs verified actual durable ingest/aggregate/freeze, isolated import closures, numeric non-root user, owner-immutable application directories, and absent global installers. Image exports were scanned with pinned Trivy 0.72.0 by digest; the scanner received only disposable image/report mounts and no Docker socket. Gitleaks 8.30.1 by digest scanned committed history with networking disabled, read-only checkout access, and redacted output.

## Recorded results

| Settled verification | Result |
| --- | --- |
| Offline suite under socket denial | 3,039 passed; 9 PostgreSQL module deferrals; 2 deprecation warnings; 421.87 seconds |
| Explicitly guarded real PostgreSQL suite | 160 passed; 3,039 deselected; no skips; 346.51 seconds |
| Branch-enabled coverage.py score | 82.19% repository; 85.69% platform; unchanged 74%/85% floors, precision 2 |
| Final canonical command | `make check`: PASS, exit 0, including final freeze and diff checks |


- Freeze: 61 protected files, inventory and dependency fingerprints match throughout final checkpoints.
- Locked environment: setup and dependency audit passed; no known Python dependency vulnerabilities reported.
- Configured Bandit threshold: passed; this is not a claim that every lower-severity diagnostic is absent.
- Full working-tree pre-commit: passed after exact synthetic/evidence-fingerprint review and line-number updates.
- History scan: 55 commits, three reviewed synthetic historical fixtures; exact fingerprint exclusions; final scan reported no leaks.
- Clean package install and socket-denied demo: passed at migration 0015 outside the checkout, with the deterministic evidence hash unchanged from the earlier checkpoint.
- Docker builds/import boundaries: all three targets built and passed isolated checks. Application ingest/aggregate/Parquet freeze produced 208 rows and remained non-promotable.
- Image scan: 54 OS package findings per image, 51 HIGH/3 CRITICAL; zero Python findings after removing unused global build tooling. See [the complete residual review](container-vulnerability-review.md). Image CycloneDX inventory contains 175 components.
- Browser: local Streamlit fixture page rendered; empty states, invalid job reference, and valid synthetic job reference behaved correctly; the warning/error log was empty during the active check. This was a UI fixture check, not a live PostgreSQL/API deployment test.
- Sanitizers/race detectors: not applicable to these Python source changes. PostgreSQL concurrency tests and generated contract/fuzz-smoke tests provide bounded relevant evidence; no exhaustive fuzzing or mutation-score claim.
- Checkpoint offline suite: 2,266 passed, nine PostgreSQL modules intentionally skipped until the guarded stage, two upstream deprecation warnings. This checkpoint was superseded by the fresh socket-denied run: 2,918 passed, nine PostgreSQL module deferrals, two deprecation warnings in 438.08 seconds. A final open-order cap correction has 55 focused facade/activity passes. The same run passed 160 PostgreSQL tests with no skips in 295.87 seconds, then completed synthetic backtest and replay plus deterministic demo. Its security hook stopped only to refresh baseline line numbers; the hook rerun passed. After 118 additional failure-path tests and the cap regression, the settled fresh canonical run passed; its results supersede these checkpoints.

## Final benchmark receipt

Observed on CPython 3.11.15/macOS ARM64, two warmups, five repeats and 25 requested iterations.
These are local measurements, not CI thresholds or deployment capacity claims. The complete
[structured receipt](non-ai-benchmark.json) includes ranges and operation counts.

| Operation | Median milliseconds per operation |
| --- | ---: |
| canonical normalization | 0.039060 |
| decision slot claim | 30.682791 |
| fake order reconciliation | 3.222257 |
| fifteen minute aggregation | 0.102908 |
| one minute ingestion persistence | 1.416108 |
| risk decision | 1.801245 |

## Adversarial review

Intermediate failures were retained and corrected rather than omitted: a full offline run after
the CLI smoke change had 3,038 passes and one stale assertion expecting the old internal helper.
That assertion now verifies public CLI invocation, failure propagation and a finite timeout;
all seven data-CLI tests pass. Detect-secrets refreshed existing reviewed baseline line numbers
after source/documentation edits, then passed without staging or broad exclusions. The separate
remaining-stage preflight passed security (including 125 architecture/safety tests), dependency
audit, clean wheel installation, installed doctor/demo, Compose and benchmark checks. The settled full rerun subsequently passed end to end; its actual counts are recorded above.

Material findings repaired include late-observation dataset causality, premature reversal completion, fixture-only operational producers, CLI aliases that did not perform their named operations, cross-role shadow composition, incorrect advisory-lock ordering, raw driver timezone values at canonical boundaries, incomplete restore privilege checks, inherited libpq routing overrides, mixed-case Nasdaq eligibility, health-root symlink mutation and unsafe invalidation, incomplete shutdown cleanup, memory restart acceptance of orphan execution evidence, contradictory historical symbol aliases, broker event/cancellation identity, capped open-order snapshots, and missing CLI resource discovery in the application image. Each correction has targeted regression coverage. The image pipeline is now checked through the actual public CLI in both local validation and CI, rather than bypassing CLI resource discovery. The final full harness passed; the separate image gate remains blocked.

## Remaining limitations

The strict image vulnerability gate is blocked by unresolved upstream OS findings. Separately,
the fixed paper facade rejects finite rounded weighted-average prices when they differ from exact
execution evidence; this is a documented fail-closed precision limitation. Model approval is frozen
and cannot authorize submission. No fixture, shadow plan or engineering test implies AI approval.

## External validation intentionally not performed

No Alpaca data authentication/subscription/REST request, paper account access/order, hosted PostgreSQL, hosted workflow activation, remote CI execution, production browser deployment, uptime claim or market-performance validation. Those integrations remain `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` where the code and contract tests exist. Main AI remains frozen. The residual base-image advisory gate is explicitly blocked rather than classified as an external credential limitation.

## Meaningful files and evidence locations

- `platform/data_cli.py`, `durable_status.py`, `operational_scheduler.py`,
  `operational_strategy.py`, `shadow.py`, `shadow_settings.py`: real durable public operations
  and role-separated current-clock services.
- `platform/execution/`, `storage/execution.py`, `scheduling/service.py`, `paper_cycle.py`:
  reversal stages, transactional receipts, complete broker evidence and default-deny consumption.
- `platform/service_health.py`, `runtime.py`, `worker_runtime.py`: shared filesystem validation
  and cleanup across startup, signal and shutdown failures.
- `platform/data/datasets.py`, `data/provider.py`, collection/canonical persistence:
  observation causality, fixed-series/symbol provenance and bounded recovery.
- Migrations 0014/0015 and `scripts/postgres_backup_restore_smoke.py`: role grants, staged
  execution identity and verified data/privilege restoration.
- `Makefile`, `.coveragerc`, `.pre-commit-config.yaml`, workflows, Dockerfile and verification
  scripts: reproducible local/CI checks, real CLI image smoke and isolated package verification.
- `AGENTS.md`, requirements/status/runbooks and evidence indexes: current scope and explicit
  acceptance evidence. Existing editor settings and local skill files were inspected; no new
  development-tool permission/configuration or skill installation was introduced.

The Git diff includes inherited dirty files, including protected dependency and AI changes from
before this assignment. It must not be interpreted as a list of changes authored in this task.
The freeze manifests establish the preserved baseline independently of Git HEAD.

## Manual activation boundary

Future activation requires separately authorized authentication, role-specific PostgreSQL endpoints, appropriate GitHub secrets/schedule activation, Alpaca data credentials, and deliberately authorized paper credentials. The code contains no real-money path. Manual activation must not bypass the unresolved image gate. The local canonical checks are complete; model approval remains frozen and default-deny.

## Verification cleanup

The task-created PostgreSQL container and its anonymous test volume were removed and their
absence verified. The temporary Streamlit server was stopped and its browser tab closed. Only
the three task-created image-export tar files were deleted; scan reports, SBOM and final local
verification image tags remain available. Earlier unrelated containers, inherited working-tree
changes and research/output artifacts were preserved.
