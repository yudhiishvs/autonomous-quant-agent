---
name: dependency-change-review
description: "Evaluate adding, removing, or upgrading a direct dependency or lockfile resolution; do not use when no dependency graph or runtime image changes."
---

# Dependency change review

1. Read [dependency-policy.md](../../../docs/dependency-policy.md) and explain the concrete capability or defect that requires the change.
2. Check whether the standard library or an existing dependency in [pyproject.toml](../../../pyproject.toml) already meets the need.
3. Review maintenance status, release history, compatibility, license, advisories, provenance, platform support, and transitive footprint.
4. Define a justified version range and preserve the repository's Python and runtime-group boundaries.
5. Update [uv.lock](../../../uv.lock) with the repository's package manager; never hand-edit it or perform an unrelated broad upgrade.
6. Add tests for the integration point, error handling, and optional or unavailable dependency behavior where applicable.
7. Run locked synchronization, static checks, and the offline suite. Run vulnerability or container scans only through a configured canonical command; otherwise record the missing scanner, inspect primary advisory sources, and make no vulnerability-free claim.
8. Review the lockfile and image diff, then document why the dependency and its operational cost are acceptable and give its removal path.

Keep collector-only dependencies isolated in the existing runtime group and preserve the locked commands documented in [tooling.md](../../../docs/tooling.md).
