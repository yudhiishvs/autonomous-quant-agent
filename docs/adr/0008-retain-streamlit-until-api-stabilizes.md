# ADR 0008: Retain Streamlit until the private API stabilizes

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-UI-001`, `REQ-ARCH-007`, `REQ-SCOPE-004`

## Context

The operator needs safe visibility, but a new public frontend would expand dependencies, auth,
deployment, and browser attack surface before the private read API is stable.

## Decision

Retain a minimal Streamlit dashboard as a read-only client of the loopback API. It receives only a
dashboard API token and cannot access PostgreSQL, provider credentials, broker code, or mutation
routes. Defer React/Next.js and public web hosting.

## Alternatives considered

- Keep direct database reads: rejected because presentation would hold database authority.
- Build a new JavaScript frontend now: rejected because API and custody requirements are still
  private/single-operator.
- Add controls to Streamlit: rejected because dashboard compromise must not mutate state.

## Consequences

Presentation capability remains intentionally modest. API response models become the stable
boundary and can later support another UI without granting new backend authority.

## Security impact

The dashboard token is scoped to read routes, responses are no-store, and no secret-bearing object
is rendered. Loopback publishing reduces exposure but does not replace host access control.
