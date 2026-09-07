# ADR 0006: Close and reconcile before sign reversal

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-EXEC-003`, `REQ-EXEC-008`, `REQ-EXEC-009`

## Context

Changing directly from long to short, or short to long, can temporarily exceed exposure and can
hide a failed closing fill behind a new opening order. Quantity, eligibility, price, or account
state may change between stages.

## Decision

A sign reversal emits only the closing stage. Persist, submit, complete, reconcile, and prove the
position is zero within the quantity tolerance. Refresh inputs and rerun risk before the original
deadline; only then may a distinct opening plan and client ID be created.

## Alternatives considered

- Submit close and open concurrently: rejected because it bypasses the zero-position barrier.
- Submit one oversized crossing order: rejected because broker fill behavior obscures exposure.
- Reuse the first risk receipt: rejected because state can become stale during the close.

## Consequences

Reversals take longer and may finish flat when the deadline expires. Each stage has independent,
auditable identity and can recover after restart without inferring completion.

## Security impact

Uncertainty blocks the opening leg. A compromised proposal cannot encode a crossing shortcut, and
stale shortability or account state is rechecked before new opposite-side exposure.
