---
name: database-migration
description: "Create or review an Alembic change to the operational PostgreSQL schema; do not use for the separate legacy SQLite store unless its migration process is explicitly in scope."
---

# Database migration

1. Read [ARCHITECTURE.md](../../../ARCHITECTURE.md), [migrations/README.md](../../../migrations/README.md), the applicable runbook or data dictionary, and the existing migration chain.
2. Describe the old and new schemas, invariants, compatibility window, data volume, lock risk, and recovery strategy.
3. Keep DDL explicit and versioned; application startup must detect drift rather than silently mutate production schema.
4. Add constraints for domain invariants and indexes for demonstrated access paths. Preserve append-only observations, monotonic checkpoints, and lease fencing.
5. Avoid destructive or irreversible transformations without a backup, staged rollout, verification query, and explicit authorization.
6. Keep schema-owner credentials separate from the least-privilege runtime role; never expose connection strings in output or fixtures.
7. Use only the guarded loopback disposable-PostgreSQL procedure in [testing-strategy.md](../../../docs/testing-strategy.md) to test upgrade from the previous revision, verify the head revision and triggers, and exercise affected repository operations.
8. Run static checks and the full offline suite from [tooling.md](../../../docs/tooling.md), inspect generated SQL when useful, and document deployment and rollback procedures.

Never run migration experiments against a database that has not been explicitly designated disposable.
