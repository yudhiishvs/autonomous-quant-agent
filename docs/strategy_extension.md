# Strategy extension

Signal providers propose; they do not trade. A provider receives an immutable `DecisionContext`
and returns one immutable `SignalEnvelope`. The platform validates the envelope, applies signed
risk, plans intents, persists them, and alone decides whether a paper adapter can be reached.

## Minimal provider

The disabled [always-flat example](../examples/always_flat_provider.py) is the reference:

```python
from adaptive_trader.platform.signals import AlwaysFlatSignalProvider

provider = AlwaysFlatSignalProvider()
envelope = provider.signal_for(decision_context)
```

The complete example is intentionally educational and makes no performance claim. It uses only the
supplied context, has no credential or broker access, is non-promotable, and is not selected by the
tracked paper profile.

Validate it with:

```bash
uv run --no-sync pytest -q tests/unit/test_example_signal_provider.py
```

## Registration

A separately installed package may expose a provider object through the entry-point group:

```toml
[project.entry-points."autonomous_quant_agent.signal_providers"]
my_provider = "my_package.provider:provider"
```

Configuration selects a registered provider ID and version. It never accepts a module path, class
name, URL, shell command, package-install request, source string, pickle, or uploaded code. Provider
installation is an operator trust decision made outside runtime.

## Output contract

An envelope is bound to:

- contract version and deterministic signal ID;
- slot and correlation IDs;
- provider ID, version, and source mode;
- experiment ID, version, and content hash;
- data-contract and policy hashes;
- exact source-bar end, creation time, and expiry;
- every active symbol, availability mask, action, optional edge, and target input;
- optional registered artifact identity; and
- canonical content hash.

Unknown fields, a missing or extra active symbol, an expired envelope, nonfinite values, identity or
hash mismatch, and unsupported actions fail closed. Benchmark, context, and excluded symbols cannot
become targets.

## Authority boundary

A provider must not import broker, execution service, credential, network transport, arbitrary
filesystem, subprocess, or database-write code. It cannot clear a latch, alter a risk policy,
submit an order, or authorize itself for paper operation. The strategy database role can read safe
decision/data views and append signal/audit rows only.

Installed Python is not sandboxed. Process separation and least-privilege credentials contain a
compromised provider's authority, but the operator remains responsible for reviewing extension
code. Use a dedicated package, pin its version and artifact hash, and exercise malicious-output
tests before registration.

## Safe development checklist

1. Keep calculations pure and deterministic for the same context.
2. Use only context data; do not fetch current prices independently.
3. Return all active symbols in canonical order.
4. Represent unavailable data explicitly and propose flat when required inputs are missing.
5. Inject clocks in tests; never depend on wall time or sleep.
6. Test unknown fields, stale context, hash mismatch, nonfinite input, and replay.
7. Leave promotion and paper eligibility false until a separate future model-approval design exists.
