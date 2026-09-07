# PostgreSQL backup and restore

Logical backups are an operator-controlled recovery mechanism for operational PostgreSQL state.
They do not include uncommitted filesystem datasets or secret files. A backup is not considered
tested merely because `pg_dump` succeeded; it must restore into a fresh database and pass schema,
content, and audit verification.

## Automated destructive smoke test

The smoke test accepts only a loopback database named `collector_test` and requires an explicit
destructive acknowledgement. It resets that database's application schemas, migrates them,
populates deterministic synthetic state, creates a logical dump, restores it into a uniquely named
fresh database, compares the two, and removes the restored database.

Prerequisites:

- PostgreSQL 16 reachable only over loopback;
- managed database roles already bootstrapped;
- `createdb`, `dropdb`, `pg_dump`, and `psql` from a compatible PostgreSQL client;
- an empty/disposable database named exactly `collector_test`.

```bash
export APA_TEST_POSTGRES_URL='postgresql://USER:PASSWORD@127.0.0.1:5432/collector_test' # pragma: allowlist secret -- literal documentation placeholders
export APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES
uv run --no-sync python scripts/postgres_backup_restore_smoke.py
```

The smoke command rejects inherited `PGHOSTADDR`, `PGSERVICE`, `PGSERVICEFILE`, and `PGOPTIONS`
so libpq cannot redirect a validated loopback connection. Client subprocesses receive a sanitized
PostgreSQL environment and ignore local psql startup scripts.

Never aim this command at development, staging, production, or any database with useful state. It
drops the `aqa` and `market_data` schemas in `collector_test` before migration.

The script:

1. rejects non-loopback hosts and every database name except `collector_test`;
2. migrates the source to checked-in Alembic head;
3. writes deterministic experiment, bar, slot, signal, risk, plan, intent, order, fill,
   reconciliation, and audit state;
4. uses `pg_dump --format=plain --no-owner` with an argument vector;
5. passes the connection password only through the subprocess environment and never prints a URL;
6. rejects credential-shaped keys in fixture rows and verifies the connection password is absent
   from dump bytes;
7. creates a unique `collector_test_restore_*` database from `template0`;
8. restores under `aqa_migrate` with `psql --no-psqlrc --set ON_ERROR_STOP=1`, preserves
   relation/schema/column grants, and reapplies the source database ACL to the fresh drill database;
9. applies/checks migrations on the restored database;
10. compares row counts and independently calculated row hashes for every `aqa` table, checks the
    audit chain root and semantic database/schema/relation/column privileges, and requires
    nonempty slots, intents, fills, and reconciliations; and
11. drops only the generated restore database in a `finally` block.

A successful JSON result contains `status=ok`, `source_and_restore_identical=true`, the migration
revision, `privileges_preserved=true`, the required relation counts, and a snapshot content hash. It contains no connection
string, username, password, temporary path, or restored database name.

The guarded Pytest entry is:

```bash
uv run --no-sync pytest -q tests/integration/test_platform_backup_restore.py
```

If PostgreSQL client utilities or the guarded database are unavailable, the integration check is
skipped and status remains `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`; do not report it as passing.

## Operator backup

For a real self-hosted paper installation, stop or quiesce writers and use owner-private local
variables. The examples deliberately avoid embedding a password in the command or output.

```bash
install -d -m 700 backups
PGPASSWORD="$AQA_BACKUP_DATABASE_PASSWORD" pg_dump \
  --host 127.0.0.1 --port 5432 --username aqa_backup \
  --dbname autonomous_quant_agent \
  --format=custom --no-owner \
  --file backups/aqa-YYYYMMDDTHHMMSSZ.dump
chmod 600 backups/aqa-YYYYMMDDTHHMMSSZ.dump
```

Prefer a dedicated backup role with only the required read/lock authority. Do not reuse runtime
collector, strategy, execution, control, or dashboard credentials. Store the password outside shell
history where possible and unset it after use.

Record, outside the dump:

- application version and commit;
- Alembic revision;
- UTC creation time;
- database server and client major versions;
- SHA-256 of the completed dump;
- encryption location/key reference, retention class, and restoration test result.

Backups may contain market, account, position, order, fill, and incident information. Encrypt them
at rest, restrict access, test retention/deletion, and never commit them.

## Operator restore drill

Restore only into a new isolated database. Do not overwrite the source during a drill.

```bash
createdb --host 127.0.0.1 --port 5432 --username postgres \
  --template template0 aqa_restore_drill
pg_restore --host 127.0.0.1 --port 5432 --username postgres \
  --dbname aqa_restore_drill --no-owner --role aqa_migrate \
  --exit-on-error backups/aqa-YYYYMMDDTHHMMSSZ.dump
```

Use the same pre-provisioned role names as the source and restore under the migration owner.
Preserve ACL statements: upgrading an already-current database does not recreate grants stripped
from a dump. Reapply source database-level privileges separately, and verify representative runtime
logins retain their intended read/write access and prohibitions before starting workers.

Then run the checked-in migration tool against an owner-private URL file, verify the audit chain,
and compare a predeclared inventory of relation counts and immutable content hashes. Verify slots,
intents, broker-order projections, unique fills, reconciliations, incidents, latches, jobs/outbox,
and dataset-manifest paths. A successful schema check alone is insufficient.

Destroy the drill database only after recording the result. If any hash, sequence, migration, or
projection differs, preserve the isolated restore for investigation and treat the backup as failed.

## Recovery limitations

- A logical backup is not continuous point-in-time recovery.
- PostgreSQL state does not include Parquet objects; manifests must be matched to separately backed
  up immutable artifacts by logical and physical hash.
- Provider credentials and operator tokens are intentionally not backed up in database fixtures.
- Restoring stale order state does not authorize replay. Reconcile against the paper broker using
  deterministic IDs before any submission-enabled process starts.
- Restoring into a newer application requires migrations and compatibility review; downgrading core
  state is unsupported.
