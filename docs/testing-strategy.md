# Testing Strategy

## Nonnegotiable environment rules

Before test-module collection, `tests/conftest.py` removes all four Alpaca credential variables
from the process and forces `APA_ENABLE_PAPER_ORDERS=NO`; its automatic fixture reapplies that
boundary for every test. The fixture also replaces `socket.create_connection` and
`socket.socket.connect` with failures; marked PostgreSQL tests permit those paths only to loopback.
A process-wide socket-denial wrapper remains `NOT_IMPLEMENTED`. Tests marked `postgres` may
connect only to loopback. No ordinary test loads ambient Alpaca credentials, opens an Alpaca
REST/WebSocket session, or submits a paper or real order.

The PostgreSQL integration module additionally requires both a loopback database named
`collector_test` and `APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES`; otherwise it skips or
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
| Security | Paper-only syntax, configuration rejection, runtime service/secret scope, secret path/owner/mode/content checks, redaction, credential, URL, import, and authorization boundaries | May replace provider responses; must not mock the authorization decision under test | Offline Python with empty credentials and TCP guard; owner: security reviewer plus affected maintainer | `uv run --no-sync pytest -q tests/safety tests/architecture tests/unit/test_platform_experiment.py tests/unit/test_platform_profiles.py tests/unit/test_platform_runtime_settings.py tests/unit/test_platform_security.py tests/test_config_safety.py tests/test_live_safety_matrix.py tests/test_collection_credentials.py tests/test_collection_runtime.py` | Treat the affected trust boundary as closed until the cause and regression are corrected. |
| Deterministic replay | Stable event ordering, restart identity, terminal state, and evidence hashes | May use recorded synthetic events and a fake broker; must not bypass production replay/orchestration | Offline Python; owner: application maintainer | `uv run --no-sync python -m adaptive_trader.cli replay --config configs/replay.yaml` | Locate the first divergent event or state before accepting later output. |
| Property-based | General invariants for canonicalization, state machines, and financial constraints | May generate bounded values; must not encode production logic in the oracle | Target: offline Python; owner: platform domain maintainer | `NOT_IMPLEMENTED` — no Hypothesis dependency or canonical property target exists yet | No property-coverage claim is available; explicit examples remain authoritative. |
| Fuzz | Parser and untrusted-input crash resistance with retained minimized regressions | May generate malformed bytes/objects; must not weaken schemas or resource bounds | Target: isolated offline process; owner: affected boundary maintainer | `NOT_IMPLEMENTED` — no reproducible fuzz harness exists | No fuzz-resistance claim is available. A discovered defect must become a deterministic regression. |
| Performance | Correctness-preserving throughput, latency distribution, allocation, and storage behavior | May use deterministic generated workloads; must not omit correctness/hash checks | Target: recorded local environment; owner: performance investigator plus affected maintainer | `NOT_IMPLEMENTED` — `scripts/benchmark_pipeline.py` does not yet exist | No performance claim or budget is supported until a repeatable baseline exists. |

The full pytest selection is:

```bash
uv run --no-sync pytest -q
```

Without `APA_TEST_POSTGRES_URL`, the guarded PostgreSQL module is skipped. CI starts
PostgreSQL and supplies the explicit disposable settings, so the same command includes it.

## Disposable PostgreSQL

After creating a local loopback PostgreSQL 15 database named `collector_test`:

```bash
APA_TEST_POSTGRES_URL=postgresql+psycopg://collector_test:collector_test@127.0.0.1:5432/collector_test \
APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES \
uv run --no-sync pytest -q -m postgres
```

The fixture migrates down to base, upgrades to head, and returns to base. The database must
contain no user data.

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

Branch coverage is diagnostic. The canonical, experiment, profile, CLI, and architecture tests
measure 90.88 percent branch coverage for `adaptive_trader.platform` at the preceding profile
boundary. The focused secret-file tests measure 92 percent branch coverage for
`adaptive_trader.platform.security`. The repository has coverage configuration but no automated
target-platform ratchet; that ratchet is `NOT_IMPLEMENTED`. Safety-critical transitions and
authorization gates require direct tests regardless of percentage.

Property tests are appropriate for canonical serialization, validation, state machines,
and financial invariants when generators add coverage beyond explicit cases. Fuzzing,
mutation testing, browser automation, and performance benchmarks are not current merge
gates and remain `NOT_IMPLEMENTED` until a concrete requirement and repeatable command
exist.

## Failure interpretation

- Network-guard failure: production code attempted undeclared external I/O.
- PostgreSQL refusal: the destructive-test target is not provably disposable.
- Backtest/replay drift: inspect input/config hashes and first differing durable state.
- Safety-test failure: treat the relevant trust boundary as closed until corrected.
- Nondeterministic failure: capture seed/event order and fix ownership or timing; do not add
  arbitrary sleeps.
