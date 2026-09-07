# Scheduled market-data collection

`aqa data collect-once` performs finite canonical Alpaca IEX/raw collection into an existing
governed PostgreSQL database. The implementation and synthetic contract tests are
`IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`; no provider credentials or scheduled external run
were exercised during implementation.

The command uses the established collector's singleton lease and fencing token. It restores
canonical work, resumes the earliest durable checkpoint with five minutes of reconciliation
overlap, clips requests to completed XNAS sessions, persists raw observations and canonical
revisions, drains aggregation/gaps/watermarks, and retries one eligible older gap. Overlap never
precedes the immutable configured history boundary. Exact duplicates remain idempotent;
corrections preserve history. Missing IEX trades remain explicit gaps.

Completed invocations release ownership and exit. Cancellation, interrupted database writes,
and failed derivation preserve committed checkpoints and durable work for the next invocation.
No runner filesystem checkpoint is needed. The existing continuous WebSocket collector remains
available and shares the same singleton lease; do not schedule a second owner beside it.

## Activation

1. Prepare the existing PostgreSQL endpoint using the governed migrations and collector role
   documented in [the collector runbook](market_data_runbook.md). Runtime collection does not
   migrate schema. Nonlocal endpoints require `sslmode=verify-full` and a trusted CA.
2. Configure repository secrets `AQA_DATABASE_URL`, `AQA_ALPACA_DATA_API_KEY`, and
   `AQA_ALPACA_DATA_SECRET_KEY`. The database URL must identify the least-privilege collector
   login, never an administrator or migration owner.
3. Set repository variable `AQA_MARKET_DATA_HISTORY_START` to the intended ISO date or UTC
   minute boundary for first use. After it is persisted, the variable may be omitted; a
   conflicting value fails closed.
4. Set repository variable `AQA_ENABLE_SCHEDULED_DATA_COLLECTION` to exactly `true` when
   external collection is authorized. Manual dispatch additionally requires `activate=true`.

The [market-data workflow](../.github/workflows/market-data.yml) is inactive without its repository
gate, accepts only the default branch, and runs on a GitHub-hosted Ubuntu runner. Its offset
five-minute weekday cadence covers 09:02–17:57 America/New_York; application calendar logic
remains authoritative for holidays, early closes and completed bars. GitHub can delay, skip,
cancel or replace queued invocations, so database recovery drives completeness. The workflow has
a 20-minute timeout and does not cancel an already-running invocation when a new one is queued.

GitHub's current schema supports an IANA timezone alongside POSIX cron, with a five-minute
minimum interval: [official schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).

The secret bridge creates a mode-0700 directory under `RUNNER_TEMP`, writes mode-0600 files,
removes raw secret environment variables before invoking the application, and cleans up on
normal or failed child exit. The application accepts only data-service file references; no
paper/account/operator secret is supplied. The workflow stores no canonical data artifact or
secret-bearing cache. GitHub runner termination may prevent cleanup, so secrets are limited
to the ephemeral runner and must never enter persistent runner caches.

## Reading the result

Successful collection emits bounded JSON counters and a health snapshot. `last_invocation_status`
and `last_invocation_completed_at` describe the latest finite invocation. They are separate from
`coverage_ready`, `research_ready`, and the daemon-only `service_ready`/`subscribed` fields.
A stopped one-shot can leave data ready. A successful invocation can leave research unready when
required symbols have missing bars, unresolved gaps, or incomplete aggregates.

For a local invocation, present owner-private files through `AQA_DATABASE_URL_FILE`,
`AQA_ALPACA_DATA_API_KEY_FILE`, and `AQA_ALPACA_DATA_SECRET_KEY_FILE`, set the initial nonsecret
history boundary if necessary, then run `aqa data collect-once`. Raw legacy secret variables
are accepted only by preserved compatibility commands, not by this command.

## Verification

`tests/test_collection_service.py` covers finite checkpoint recovery, missed sessions, overlap
clamping, closed-market no-op, singleton conflicts, old-gap repair, and failure recovery.
`tests/integration/test_collection_operations_postgres.py` covers fresh-service restart against
disposable PostgreSQL, canonical duplicate behavior, empty derived work and post-release readiness.
`tests/safety/test_scheduled_collection_workflow.py` executes the secret bridge with synthetic
values and verifies private file permissions, absence of raw secrets in child environment,
cleanup on success/failure, activation gates, schedule, and workflow authority.
