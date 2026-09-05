# Signal-provider example

`always_flat_provider.py` demonstrates the deliberately narrow extension contract. It reads only
the immutable `DecisionContext` supplied by the strategy worker and returns a declarative,
hash-bound `SignalEnvelope`. It cannot access credentials, market-data transports, databases, or
broker clients.

The example is educational, is not enabled by any tracked runtime profile, makes no profitability
claim, and is permanently marked non-promotable and ineligible for paper submission.

Run its offline contract test with:

```bash
uv run --no-sync pytest -q tests/unit/test_example_signal_provider.py
```

A separately installed extension can register the same provider object through the
`autonomous_quant_agent.signal_providers` Python entry-point group. Registered output is still
validated against the current decision context before persistence.
