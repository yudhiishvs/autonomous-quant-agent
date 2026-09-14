# Declarative paper strategies

The v1 contract defines a target position for one US equity symbol. It cannot submit an
order or authorize execution. The existing private/model approval rejection is unchanged.

Validate the maintained example without credentials:

```sh
.venv/bin/python -m adaptive_trader.public_product configs/public_product/moving-average.json
```

The command prints the normalized configuration and its content hash, or exits 2 with a
redacted validation error. It reads at most 16 KiB. Saving or validating a definition does
not establish brokerage eligibility or approval.

Supported rules:

- `constant_target`: propose the configured nonnegative whole-share position. This is a
  total target, not an instruction to purchase that many shares on every evaluation.
- `moving_average_target`: compare the arithmetic means of the latest completed fast and
  slow minute windows. Propose the configured positive target when fast is strictly above
  slow; otherwise propose zero. Windows are bounded to 200 minutes. Equality proposes zero.

Both require current eligible observations during the regular market session. The evaluator
accepts only consecutive completed UTC minute bars and decimal-string prices. Missing
history, future observations, observations older than 30 seconds, or latest completed bars
at least 60 seconds old produce no target. No target means no instruction, including no
automatic liquidation. Decisions expire within 30 seconds and before their bars become
stale. Data adapters must independently establish feed entitlement, calendar state and
asset eligibility; callers cannot trust browser-supplied eligibility flags.

The evaluator performs one current-observation calculation, not a historical backtest. It
contains no portfolio, broker, file, import, credential or network capability. Arithmetic
mean comparisons use decimal cross-products to avoid rounded division. Configuration and
observation hashes reuse the platform canonical serializer and include material defaults.
They identify content; neither is an authorization token.

Version 1 accepts regular-session whole-share market or limit DAY order policies. A limit
policy records a bounded basis-point offset from a fresh execution-time reference; the
current evaluator only produces position targets and does not price or send an order.
Independent approval, risk and broker adapters are required before that policy can operate.
The 100,000-share schema maximum is an input bound, not a recommended or authorized limit.
Per-account risk must impose stricter limits at execution time.

Provider basis checked September 14, 2026: Alpaca documents market/limit orders and DAY
behavior in [Placing Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca). This
restricted product subset does not imply every syntactically valid symbol is tradable,
nor that provider data may be shared with other customers. Entitlements remain a launch gate.
