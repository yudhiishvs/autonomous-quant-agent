# Deterministic demo runbook

`aqa demo` is a credential-free engineering proof. It is not Alpaca evidence and does not calculate
profitability, Sharpe, alpha, win rate, model accuracy, or strategy quality.

## Run

```bash
uv sync --locked --all-extras
uv run --no-sync aqa doctor
uv run --no-sync aqa demo --output outputs/demo/evidence.json
```

Machine-readable output:

```bash
uv run --no-sync aqa demo \
  --output outputs/demo/evidence.json \
  --json
```

No environment variable or secret file is required. The command constructs neither an Alpaca
transport nor an Alpaca broker. It uses two fresh temporary SQLite databases and a deterministic
fake broker.

## Flow

```text
synthetic 1-minute events -> canonical validation -> append-only revisions
-> duplicate convergence -> correction lineage -> exact 15-minute aggregation
-> durable gap repair -> symbol and active-basket watermarks -> 21 decision slots
-> non-promotable signal -> signed risk -> signed plan -> persisted intent
-> fake acceptance/fill -> reconciliation -> restart/replay -> forced flatten
-> audit verification -> safe read model -> immutable evidence manifest
```

The two runs must agree on canonical/effective event hashes, aggregate and revision hashes,
watermarks, slots, signal/risk/plan hashes, deterministic client IDs, broker transitions, fills,
reconciliations, final positions/account values, audit root, safe-read hash, and final manifest hash.
Absolute temporary paths, process IDs, and wall-clock run metadata are not part of logical identity.

The manifest label must be exactly:

```text
OFFLINE_FIXTURE_NOT_ALPACA_EVIDENCE
```

## Recovery boundaries

The evidence contains one result for each required boundary:

1. before event persistence;
2. after event persistence but before watermark update;
3. correction during aggregation;
4. after slot claim but before signal persistence;
5. after signal persistence but before risk decision;
6. after intent persistence but before submission;
7. fake-broker acceptance before response persistence;
8. before reconciliation;
9. before forced-flatten completion.

Every result must be deterministic recovery with zero duplicate side effects or a durable
fail-closed incident. The current fixture recovers safely and demonstrates that an acceptance
timeout becomes `SUBMISSION_UNKNOWN`, is found by deterministic client ID after restart, and is not
resubmitted.

## Evidence handling

The output path must be relative to the current application root. Publication rejects absolute
paths, traversal, symlink destinations, and conflicting overwrite. Parent directories are created
owner-only and the file is created with mode `0600`. Re-running against byte-identical evidence is
idempotent.

Do not commit `outputs/`, copy the manifest into documentation as a claimed market result, or relabel
it. A changed implementation may intentionally change the manifest hash; review the underlying
logical evidence rather than blessing a new hash automatically.

## Failure handling

- `configuration invalid`: run `aqa config validate`; do not bypass the pinned experiment hash.
- `offline verification failed`: preserve the failing output and run the focused demo tests.
- `different content`: choose a new relative output path or explicitly archive the old evidence;
  do not overwrite it in place.
- nondeterministic hash: compare the two run payloads for a wall-clock, temporary path, random ID,
  unordered query, or unseeded source leaking into identity.
- non-flat final state or ambiguous order: treat as a blocking safety defect, not a flaky test.

Focused verification:

```bash
uv run --no-sync pytest -q tests/unit/test_platform_demo.py
```
