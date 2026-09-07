# ADR 0010: Keep scope self-hosted and paper-only

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-FUNC-001`, `REQ-FUNC-004`, `REQ-EXEC-006`

## Context

Public multi-user operation would require tenant isolation, regulated credential custody, account
linking, abuse controls, high availability, and a materially different threat model. Real-money
execution carries consequences beyond this educational platform.

## Decision

Support one self-hosted operator, offline simulation, shadow mode, and an explicitly gated Alpaca
paper adapter only. Do not add real-money endpoints, `paper=False`, public credential custody,
multi-tenancy, OAuth brokerage linking, or public deployment.

## Alternatives considered

- Public SaaS from the first release: rejected because custody and tenancy are unsolved.
- Add a dormant live-money switch: rejected because unreachable dangerous code is still risk.
- Support arbitrary brokers: rejected because current gates are specific and testable.

## Consequences

The product is not a hosted service or brokerage platform. Operators manage their own local
infrastructure. Public web/cloud work requires a separate program and security review.

## Security impact

No server stores other users' keys or grants internet trade controls. Tracked config denies paper
submission, and the adapter has a literal paper-only construction path. The project still requires
careful local secret handling and reconciliation.
