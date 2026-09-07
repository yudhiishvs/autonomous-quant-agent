# ADR 0002: Add separate signed platform contracts

- Status: Accepted
- Date: 2026-09-05
- Owners: repository maintainers
- Related requirements: `REQ-FUNC-008`, `REQ-RISK-001`, `REQ-EXEC-001`

## Context

The legacy prototype is long-only and has stable backtest, replay, and receipt behavior. The new
platform needs signed positions, short eligibility, latches, multi-stage orders, and stricter
reconciliation. Mutating legacy records would change historical semantics and regression evidence.

## Decision

Keep legacy contracts intact. Define signed risk, execution, fill, reconciliation, and persistence
contracts under `adaptive_trader.platform`, with explicit adapters only where compatibility has a
current consumer.

## Alternatives considered

- Extend legacy records in place: rejected because optional signed fields create ambiguous state.
- Replace legacy behavior immediately: rejected because it destroys useful regression evidence.
- Fork the whole application: rejected because shared canonical and operational behavior would
  diverge.

## Consequences

There are two intentionally different domains during migration. Tests must state which contract
they exercise, and shared naming cannot imply wire compatibility. Legacy CLI, backtest, and replay
remain regression gates.

## Security impact

Separate strict records prevent a negative quantity or short authority from entering a long-only
path accidentally. Compatibility code must not translate a signed proposal into a broker action
without the complete platform risk and execution gates.
