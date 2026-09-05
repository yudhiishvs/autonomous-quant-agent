# Security Policy

## Supported scope

There is no published stable release. Security reports are evaluated against the current
`main` branch. Older commits and local forks do not receive a separate support window.

The supported product boundary is self-hosted research, deterministic offline replay, and
simulated paper-account operation. Real-money trading, public multi-user hosting, custody of
another person's credentials, and arbitrary remote strategy execution are unsupported.

Implementation status:

- Offline backtest, replay, and safety tests: `IMPLEMENTED_AND_VERIFIED`
- Standalone collector with fake providers and disposable PostgreSQL:
  `IMPLEMENTED_AND_VERIFIED`
- Credential-based collector and legacy paper operation:
  `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`
- Generic signed platform and private control plane: `NOT_IMPLEMENTED`
- Real-money execution: `INTENTIONALLY_DEFERRED`

## Report a vulnerability privately

Use the repository's **Security** tab to open a private security advisory:

<https://github.com/yudhiishvs/autonomous-quant-agent/security/advisories/new>

If private reporting is unavailable, open a minimal public issue asking the maintainer to
establish a private channel. Do not include exploit details, credentials, account
identifiers, private data, or a proof-of-concept in that issue.

Include, when safe:

- affected commit and configuration mode;
- affected module, endpoint, command, or migration;
- prerequisites and a minimal reproduction using synthetic data and fake credentials;
- potential confidentiality, integrity, availability, or financial-authority impact;
- whether a broker or external provider was contacted;
- suggested mitigation, if known.

Never send a real key, token, private key, database URL, account identifier, or raw provider
payload. Replace values with named sentinels and revoke any credential that may have been
exposed.

## What receives priority

High-priority reports include:

- any path to a real-money endpoint or non-paper trading client;
- paper order submission that bypasses explicit configuration, acknowledgement, account,
  freshness, risk, or reconciliation gates;
- strategy, dashboard, API, or collector access to unauthorized broker capabilities;
- committed, logged, serialized, or returned credentials;
- duplicate economic side effects after timeout, replay, or restart;
- stale or malformed data authorizing exposure;
- unauthorized PostgreSQL or SQLite mutation, migration data loss, or audit tampering;
- arbitrary code, command, path, URL, SQL, or unsafe deserialization injection;
- a collector fencing/lease race that permits stale-writer mutation.

Reports about guaranteed returns, strategy profitability, or normal simulated-market loss
are not security vulnerabilities. A reproducible integrity defect in research evidence,
risk controls, accounting, or simulation may still be a correctness or safety issue.

## Coordinated handling

The maintainer will first confirm receipt and preserve evidence without reproducing secret
values. Triage should identify the affected trust boundary, supported configuration, first
incorrect state, and whether containment is required. No response or remediation deadline
is promised before the report is assessed.

Please avoid public disclosure until a correction or documented mitigation is available.
The maintainer may request a regression test using deterministic fixtures. External-system
testing must remain within accounts and infrastructure you own and must not submit orders or
degrade provider services.

## Immediate credential response

If any real credential appears in Git history, an artifact, a log, or a report:

1. Treat the credential as compromised; do not reproduce it in an issue or terminal output.
2. Revoke or rotate it at the provider.
3. Stop affected workloads and preserve only redacted incident metadata.
4. Determine every location and consumer that received it.
5. Correct the leak path and add sentinel-based regression coverage.
6. Decide separately, with maintainer approval, whether history remediation is necessary.

Deleting the value from the latest file is not credential revocation and does not remove it
from existing history or clones.

## Security design and verification

The implemented trust model, credential boundaries, storage privileges, test network guard,
and residual risks are documented in `docs/security-model.md`. Operational collector
containment and recovery are in `docs/market_data_runbook.md`; legacy incident actions are
in `docs/incident_response.md`.

Run the current security-focused behavioral checks with:

```bash
uv run --no-sync pytest -q tests/safety tests/architecture \
  tests/unit/test_platform_experiment.py tests/unit/test_platform_profiles.py \
  tests/unit/test_platform_runtime_settings.py \
  tests/unit/test_platform_secret_bootstrap.py \
  tests/unit/test_platform_security.py tests/test_config_safety.py \
  tests/test_live_safety_matrix.py tests/test_collection_credentials.py \
  tests/test_collection_runtime.py
```

The full CI path uses empty Alpaca variables, disables paper submission, guards the common Python
TCP connection paths used by current adapters, migrates disposable PostgreSQL, and smoke-tests the
collector image with `--network none`. Process-wide socket denial remains target work. These checks
do not constitute credential-based provider validation.
