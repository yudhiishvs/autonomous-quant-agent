# ADR 0011: Separate process and credential trust boundaries

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-ARCH-003` through `REQ-ARCH-008`, `REQ-SEC-001`

## Context

A single process or shared database/broker credential would let a dashboard, API, scheduler,
collector, or experimental strategy exceed its responsibility after compromise.

## Decision

Run collector, scheduler, strategy, execution, control API, and dashboard with distinct secret
mounts and PostgreSQL roles. Data and paper key pairs are separate. Only execution may receive paper
files; only live data may receive data files; dashboard receives only a read token. Secret values
are file-loaded and never placed in tracked config.

## Alternatives considered

- One application credential: rejected because authorization would depend only on code paths.
- Environment-value secrets: rejected because process dumps and inherited environments expand
  exposure.
- Give the API broker access: rejected because bounded control jobs do not require it.

## Consequences

Local operation has more role/password files and process configuration. Bootstrap and rotation must
preserve the matrix. Services communicate through typed database/API boundaries rather than shared
in-memory objects.

## Security impact

Compromise is contained to explicit grants and mounted files. Process separation is not a Python
sandbox or outbound firewall, so import, Compose, role-denial, and network tests remain required.
