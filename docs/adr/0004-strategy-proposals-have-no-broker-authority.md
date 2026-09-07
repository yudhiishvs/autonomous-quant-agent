# ADR 0004: Strategy proposals have no broker authority

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-FUNC-005`, `REQ-ARCH-005`, `REQ-SIGNAL-001`

## Context

Strategy code may be experimental or installed by the operator. Allowing it to submit orders,
change risk, fetch arbitrary data, or load credentials would make proposal quality equivalent to
trading authority.

## Decision

A provider receives an immutable `DecisionContext` and returns a declarative, versioned,
hash-bound `SignalEnvelope`. The strategy process has no broker/data credentials and no order or
risk mutation privileges. The platform independently validates, risks, plans, persists, submits,
and reconciles.

## Alternatives considered

- Give each strategy a broker client: rejected because risk becomes optional.
- Treat locally installed code as a secure sandbox: rejected because Python process isolation is
  not a sandbox.
- Accept remote source or URLs: rejected because it adds code-execution and SSRF boundaries.

## Consequences

Providers are simple to test and replay but cannot perform independent live lookups. Extensions are
registered at installation through a fixed entry-point group and remain operator-trusted code.
Registration never implies promotion or paper eligibility.

## Security impact

Process credentials, import tests, strict envelopes, database grants, and absent API code-upload
surfaces contain a compromised provider. Deterministic risk still treats every proposal as
untrusted input.
