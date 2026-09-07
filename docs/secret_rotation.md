# Secret rotation

The platform separates data-provider, paper-broker, database-role, operator, and approved-account
references. Rotation must preserve those boundaries. Never replace one category with a shared key.

## Immediate exposure response

1. Disable affected external profiles and keep paper submission disabled.
2. Revoke the exposed credential at its issuing system before editing repository history.
3. Preserve the file path, commit identity, access time, and affected service as evidence without
   reproducing the value.
4. Issue a new least-privilege credential and write it to a new owner-private file.
5. restart only the process authorized for that credential;
6. verify redacted startup/health, audit, and reconciliation state; and
7. remove the old file through an approved recoverable process and document completion.

If a credential entered Git history, revocation is mandatory even when the branch is deleted.
History rewriting is a separate coordinated repository incident and is not a substitute for
revocation.

## File requirements

Secret directories must be owned by the current service operator and not group/world writable.
Files must be regular, owner-only `0400` or `0600`, nonempty, NUL-free, and never symlinks. Store
exactly one value with at most one trailing newline. Do not use `.env` for platform secret values.

The runtime accepts these secret-file references only:

```text
AQA_DATABASE_URL_FILE
AQA_OPERATOR_TOKEN_FILE
AQA_ALPACA_DATA_API_KEY_FILE
AQA_ALPACA_DATA_SECRET_KEY_FILE
AQA_ALPACA_PAPER_API_KEY_FILE
AQA_ALPACA_PAPER_SECRET_KEY_FILE
AQA_PAPER_ACCOUNT_ID_HASH_FILE
```

The generic Alpaca SDK variables are rejected by the platform configuration boundary. The local
bootstrap command creates database-role passwords and an operator token only; it never creates or
requests provider keys.

## Rotation by category

### Market-data key pair

- Stop `market-data-live`; offline services remain available.
- Revoke and replace both data key files as one pair.
- Confirm the live-data process mounts no paper files and the paper process mounts no data files.
- Start the collector, confirm subscription allowlist and durable resume, then inspect redacted
  connection/audit state.
- Rotation does not authorize or test trading.

### Paper key pair and account hash

- Disable submission and stop `paper-execution-worker`.
- Reconcile every known client-order ID and account position before revocation when available.
- Revoke and replace both paper key files. Update the approved account-ID hash through its separate
  file only after independently confirming the intended paper account.
- Re-run every paper gate with submission still disabled. Enabling the tracked configuration or
  contacting the adapter requires separate operational authorization.
- Never use a data key as a paper key or assume that successful market-data access validates a
  broker account.

### Database role password

- Rotate one login role at a time through an administrator connection outside runtime services.
- Publish a new role-specific URL file atomically, restart only that service, and confirm its
  expected read/write denial matrix.
- Retire the old password after the new connection is healthy. Do not place passwords in migration
  SQL, command arguments, logs, or Git.
- Migration-owner rotation is deployment-only and must not grant ordinary business DML.

### Operator/dashboard token

- Generate at least 32 random bytes, publish through an owner-private file, and restart the private
  API/dashboard boundary.
- Revoke the prior token immediately after both consumers move.
- Review recent rate-limit/authentication metrics and control audit events. A dashboard compromise
  must not expose database or broker credentials.

## Verification

After rotation, confirm the old value fails, the new value is accepted only by the intended
process, logs/errors/metrics contain no sentinel, and no secret file is staged or in an image. For
paper credentials, keep submission disabled until account reconciliation is clean and all explicit
gates are reviewed.

Do not print, hash into public evidence, paste, or scan real values through ad hoc commands. Use the
repository's sentinel-based redaction tests and private issuer audit instead.
