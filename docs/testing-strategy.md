# Testing Strategy

## Nonnegotiable environment rules

Before test-module collection, `tests/conftest.py` removes all four Alpaca credential variables
from the process and forces `APA_ENABLE_PAPER_ORDERS=NO`; its automatic fixture reapplies that
boundary for every test. The fixture also replaces `socket.create_connection` and
`socket.socket.connect` with failures; marked PostgreSQL tests permit those paths only to loopback.
`scripts/verify_no_network.py` runs a Python module with DNS and outbound socket operations denied;
the offline-demo workflow executes both demo runs through that wrapper. Tests marked `postgres`
may connect only to loopback. No ordinary test loads ambient Alpaca credentials, opens an Alpaca
REST/WebSocket session, or submits a paper or real order. The Python guard is not a host firewall;
container verification uses `--network none` where an operating-system boundary is required.

The PostgreSQL integration module additionally requires both a loopback database named
`collector_test`, `APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES`, and
`APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES`; otherwise it skips or
refuses startup. Never supply a shared or hosted database.

## Test categories

Each category has a distinct owner and failure meaning. “May replace” describes only a volatile
boundary that the test is permitted to fake; it never permits replacing the behavior being
verified.

| Category | Validates | May replace / must not replace | Environment and owner | Command | Failure interpretation |
| --- | --- | --- | --- | --- | --- |
| Unit/domain | Validation, calculations, hashes, and state rules | May replace clocks and external inputs; must not call the function under test to calculate expected values | Offline Python; owner: module maintainer | `uv run --no-sync pytest -q tests/test_allocator.py tests/test_risk.py tests/test_collection_contracts.py` | A local invariant or boundary contract is incorrect. |
| Component | Service behavior across cooperating in-memory boundaries | May fake Alpaca, clocks, and persistence when persistence is not the subject; must not replace observable state or recovery outcomes | Offline Python; owner: subsystem maintainer | `uv run --no-sync pytest -q tests/test_collection_service.py tests/test_live_replay_service.py` | Orchestration, error propagation, or recovery behavior regressed. |
| PostgreSQL integration | Alembic, triggers, transactions, leases, fencing, and idempotency | May fake Alpaca; must use PostgreSQL for SQL, isolation, trigger, and concurrency semantics | Explicitly disposable loopback `collector_test`; owner: persistence maintainer | Guarded `uv run --no-sync pytest -q -m postgres` command below | Treat schema, transaction, or concurrency safety as unverified until corrected and rerun. |
| External contract | Parsing and classification at fixed Alpaca data and guarded paper-adapter boundaries | May replace the remote peer with deterministic responses; must not bypass the production request/response parser | Offline Python with TCP guard; owner: adapter maintainer | `uv run --no-sync pytest -q tests/test_collection_alpaca.py tests/test_collection_credentials.py tests/test_collection_runtime.py` | The adapter no longer enforces the inspected external contract; no provider claim may advance. |
| Research regression | Causal timing, reporting, undefined metrics, and execution assumptions | May replace external data with deterministic frames; must not duplicate the financial invariant in expected-value logic | Offline Python; owner: research maintainer | `uv run --no-sync pytest -q tests/research` | Research evidence or its interpretation changed and must be explained before acceptance. |
| End-to-end regression | Public legacy backtest and replay entry points, artifacts, and orchestration | May replace market and broker boundaries with synthetic/replay providers; must not replace the CLI path or durable outputs | Offline Python and ignored temporary outputs; owner: application maintainer | Commands under “Regression commands” | A supported public workflow or deterministic evidence contract regressed. |
| Security | Paper-only syntax, configuration rejection, runtime service/secret scope, secret path/owner/mode/content/bootstrap checks, redaction, credential, URL, import, and authorization boundaries | May replace provider responses; must not mock the authorization decision under test | Offline Python with empty credentials and TCP guard; owner: security reviewer plus affected maintainer | `uv run --no-sync pytest -q tests/safety tests/architecture tests/unit/test_platform_experiment.py tests/unit/test_platform_profiles.py tests/unit/test_platform_runtime_settings.py tests/unit/test_platform_secret_bootstrap.py tests/unit/test_platform_security.py tests/test_config_safety.py tests/test_live_safety_matrix.py tests/test_collection_credentials.py tests/test_collection_runtime.py` | Treat the affected trust boundary as closed until the cause and regression are corrected. |
| Deterministic replay | Stable event ordering, restart identity, terminal state, and evidence hashes | May use recorded synthetic events and a fake broker; must not bypass production replay/orchestration | Offline Python; owner: application maintainer | `uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml` | Locate the first divergent event or state before accepting later output. |
| Generated properties | Canonical roundtrip/idempotence/order invariance, resource boundaries, and shrink-only financial constraints | Seeded bounded generation with independent invariants; no production implementation as oracle | Offline Python; owner: platform domain maintainer | `uv run --no-sync pytest -q tests/unit/test_platform_properties.py tests/unit/test_platform_data_aggregation.py tests/unit/test_platform_risk_policy.py` | A reproducible invariant failure; retain its seed and reduce it to a focused regression. No exhaustive property-coverage claim. |
| Fuzz smoke | Canonical malformed-object rejection, deterministic redacted errors and bounded nesting/node handling | Seeded malformed nested values; must not weaken schemas/resource limits | Offline Python; owner: canonical boundary maintainer | `uv run --no-sync pytest -q tests/unit/test_platform_properties.py` | Typed failure, redaction or resource-bound regression; this bounded smoke is not coverage-guided fuzzing or a general fuzz-resistance guarantee. |
| Performance | Correctness-preserving normalization, persistence, aggregation, claim, risk, and fake-execution measurements | May use deterministic generated workloads; must not omit correctness invariants | Recorded local environment; owner: performance investigator plus affected maintainer | `uv run --no-sync python scripts/benchmark_pipeline.py --warmups 2 --repeats 5 --iterations 25` | A harness or semantic regression must be fixed; timing variation alone is not a CI failure or capacity claim. |

The dependency-free generated suite uses four fixed seeds: 256 valid JSON trees exercise
value preservation, canonical idempotence and recursively permuted mapping order; 160 malformed
nested values exercise typed rejection and error redaction; 30 nesting shapes and 20 node counts
straddle the documented resource limits. Existing aggregation tests independently verify OHLC,
volume/trade-count/VWAP conservation and deterministic shuffled input. The existing risk property
matrix generates 50 signed weight vectors and asserts shrink-only behavior and every exposure cap.
This deliberately reuses existing financial invariants instead of adding a duplicate oracle or
changing the frozen dependency lock. The combined selection passed 37 tests in the current review.
It does not replace example-based edge, recovery, concurrency, or integration tests.

The full pytest selection is:

```bash
uv run --no-sync pytest -q
```

Without `APA_TEST_POSTGRES_URL`, the guarded PostgreSQL module is skipped. CI starts
PostgreSQL and supplies the explicit disposable settings, so the same command includes it.

## Disposable PostgreSQL

After creating a local loopback PostgreSQL 16 database named `collector_test`:

```bash
APA_TEST_POSTGRES_URL=postgresql+psycopg://collector_test:collector_test@127.0.0.1:5432/collector_test \
APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES \
APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES=YES \
uv run --no-sync pytest -q -m postgres
```

The fixture migrates down to base, upgrades to head, and returns to base. The database must
contain no user data.

The backup/restore smoke additionally requires `createdb`, `dropdb`, `pg_dump`, and `psql`:

```bash
uv run --no-sync pytest -q tests/integration/test_platform_backup_restore.py
```

It resets `collector_test`, restores into a generated fresh database, compares schema/row hashes and
audit state, then removes only the generated restore database. Without the client utilities it is
skipped and cannot be reported as passing.

## Regression commands

```bash
uv run --no-sync python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic
uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml
```

These runs prove deterministic application paths, not market performance. Generated files
under `runtime/` and `outputs/` remain ignored.

## Design rules

- Expected values are independently calculated; do not call production logic to construct
  the expected result.
- New behavior receives positive, negative, boundary, and error coverage proportional to
  its effect on data or financial authority.
- Time-sensitive behavior uses injected clocks; randomized behavior has an explicit seed.
- Test retries, cancellation, duplicate/out-of-order delivery, rollback, restart, and
  ambiguous side effects where the component supports them.
- Integration tests use the real persistence boundary when SQL constraints, isolation, or
  concurrency are the subject. Mock provider volatility, not internal business logic.
- Tests pass independently of order and leave no service, connection, or worker running.
- A bug correction preserves its minimized reproduction as a regression test.
- Never quarantine a flaky test permanently or lower a check to hide a regression.

## Coverage and conditional test types

Branch coverage is diagnostic and is measured by the canonical full command. The repository-wide
floor must not fall below the recorded 74 percent starting baseline, and new platform code targets
at least 85 percent branch coverage except for an explicitly justified external adapter path.
Safety-critical transitions and authorization gates require direct tests regardless of percentage.
Do not preserve an obsolete measured percentage in this document; record the exact current result
in the final command ledger.

Property tests are appropriate for canonical serialization, validation, state machines, and
financial invariants when generators add coverage beyond explicit cases. Fuzzing, mutation testing,
and browser automation are not current merge gates. The deterministic performance harness exists,
but unstable wall-clock values are intentionally not merge thresholds.

## Failure interpretation

- Network-guard failure: production code attempted undeclared external I/O.
- PostgreSQL refusal: the destructive-test target is not provably disposable.
- Backtest/replay drift: inspect input/config hashes and first differing durable state.
- Safety-test failure: treat the relevant trust boundary as closed until corrected.
- Nondeterministic failure: capture seed/event order and fix ownership or timing; do not add
  arbitrary sleeps.


The canonical `make coverage` first requires the disposable PostgreSQL preflight, then
runs the socket-denied offline suite and appends the guarded PostgreSQL suite to the
same branch data. `make coverage-report` enforces the unchanged 74 percent repository
and 85 percent platform floors with two-decimal precision and writes XML/HTML evidence.
Zero-decimal rounding cannot promote a result below the platform floor. All production
platform modules, including external adapters, remain in the report. `make coverage-offline`
collects offline evidence alone; `make coverage-postgres` appends guarded database
evidence. Neither partial collection establishes the combined gate. `make check`
executes PostgreSQL through coverage once. CI retains separate offline and PostgreSQL
jobs, uploads their actual hidden `.coverage` files, and requires both successful jobs
and both artifacts before combining data in the dependent coverage gate. Missing
PostgreSQL prerequisites or coverage artifacts fail verification; they are not skips.

Offline Make recipes remove the inherited disposable PostgreSQL URL and acknowledgements
only from their child environment. The subsequent guarded database stage retains the
operator-supplied variables. This prevents integration discovery/autouse fixtures from
bootstrapping roles during the socket-denied offline stage of the combined harness.
