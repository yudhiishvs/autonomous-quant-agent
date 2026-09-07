# ADR 0003: Use PostgreSQL operational state and immutable Parquet datasets

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-STORE-001`, `REQ-STORE-003`, `REQ-STORE-006`

## Context

Workers need transactions, uniqueness, leases, concurrent claims, projections, and audit history.
Research datasets need portable columnar snapshots with immutable lineage. One representation does
not serve both workloads safely.

## Decision

Use PostgreSQL 16 for operational state and coordination. Freeze research data as immutable Parquet
objects with explicit schema/order/compression, physical and logical hashes, and PostgreSQL
manifests. SQLite is limited to tests, the offline demo, and preserved legacy behavior.

## Alternatives considered

- Store everything in object storage: rejected because leases and atomic state transitions become
  fragile.
- Store research matrices only in PostgreSQL: rejected because portable immutable analytical
  snapshots and content identity are poorer.
- Add a distributed database: rejected because the single-operator workload does not justify it.

## Consequences

Backups must cover PostgreSQL and immutable objects separately, then verify manifest hashes across
both. Operational startup requires migrations; it never creates schema implicitly. Dataset
publication uses staging and no-replace semantics.

## Security impact

Database roles grant only service-specific views and mutations. Artifact paths are root-confined,
symlink-safe, and overwrite-protected. Provider data and database dumps are private and excluded
from Git and images.
