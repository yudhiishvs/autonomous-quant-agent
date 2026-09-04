# ADR 0001: Separate the generic platform from shipped experiment configuration

- Status: Accepted
- Date: 2026-09-04
- Owners: repository maintainers
- Related requirements: `REQ-ARCH-001`, `REQ-CONFIG-001`, `REQ-CONFIG-002`,
  `REQ-CONFIG-003`, `REQ-CONFIG-004`
- Supersedes: none
- Superseded by: none

## Context

The repository contains a legacy trading configuration and a 29-symbol collection contract. The
new platform requires a distinct eight-symbol order allowlist, three additional collection-only
symbols, explicit exclusions, signed-risk parameters, and deterministic experiment identity.
Putting those symbols into generic algorithms would make the platform experiment-specific, while
reusing the collection contract as order authority would allow the wrong symbols to become
targets.

The product description conceptually lists signal provider and execution mode as experiment
properties, but the normative experiment document omits them and assigns those runtime choices to
the offline, shadow, and paper profiles. The normative experiment document also omits a digest
field while requiring a calculated content hash and rejection of a supplied expected-hash
mismatch.

## Decision

The generic platform owns frozen strict contracts under `adaptive_trader.platform`; ticker values
exist only in the shipped experiment YAML and experiment-specific tests. `UniverseSpec` normalizes
and partitions roles, derives a collection allowlist from active, benchmark, and context symbols,
and derives order authority from active symbols alone. It never aliases symbols, including WDC and
SNDK.

`ExperimentDefinition` is the strict YAML contract containing the experiment identity, universe
roles, market-data identity, session rules, risk groups, and risk policy. Signal-provider and
execution-mode selection belong to the platform profiles because they change runtime authority.
Profile composition creates the required immutable `ExperimentSpec` containing those choices
alongside the verified definition. The definition-hash preimage is an explicit normalized mapping
containing every `ExperimentDefinition` field. Role tuples and risk groups are deterministically
ordered; decimals use the canonical decimal boundary. The composed experiment has its own hash
including its signal-provider and execution-mode identity. Runtime adapter, storage, broker, and
submission policy remain in the separately hashed `PlatformConfig` identity.

The exact experiment YAML does not contain a self-referential digest. `load_experiment` always
calculates the definition's `content_hash` after strict validation and optionally verifies a
lowercase SHA-256 expected hash supplied by a trusted caller. A platform profile that selects an
experiment must pin that expected definition hash; all three tracked profiles carry the known
digest explicitly.

The definition's `market_data.provider` identifies the intended external series contract; it does
not permit an offline fixture to claim Alpaca provenance. An offline `CanonicalBar` will identify
its actual provider as `fixture` and carry explicit non-promotable source mode. The composed
configuration hashes bind the definition hash, signal provider, execution mode, and runtime
adapter selection. Downstream data-contract checks must additionally bind source mode and dataset
identity. Shadow/live-data profiles must match the definition's Alpaca/IEX/raw identity; offline
fixture data can satisfy only an offline profile and can never count as external-provider
validation.

Experiment paths are relative to an explicit trusted configuration root. Loading rejects path
escape, symlink traversal, nonregular and oversized files, invalid UTF-8, NUL/BOM text, excessive
YAML depth or nodes, anchors, aliases, merge keys, explicit tags, duplicate keys, multiple
documents, unknown fields, broad type coercion, and invalid cross-field semantics.

## Alternatives considered

- Reuse `adaptive_trader.collection.universe`: rejected because collection membership carries no
  order authority and that contract intentionally includes all 26 equities plus three context or
  benchmark symbols.
- Add flagship tickers to generic algorithms: rejected because it prevents reusable experiments
  and hides authorization policy in code.
- Store runtime mode and provider selection in the experiment: rejected because offline, shadow,
  and paper profiles must use the same immutable experiment while granting different process
  authority.
- Put the expected digest in the experiment itself: rejected because the normative strict YAML
  schema does not contain it and hashing that field would be self-referential. The selecting
  profile is the appropriate independent trust anchor.
- Hash YAML bytes: rejected because comments, formatting, and mapping order are not semantic and
  would create operationally meaningless identities.
- Use ordinary `yaml.safe_load`: rejected because it permits aliases, merges, and duplicate
  last-write-wins keys that weaken review of tracked configuration.

## Consequences

The shipped experiment can change only by changing semantic content and therefore its hash.
Runtime services can distinguish collection eligibility from order eligibility without consulting
legacy constants. Equivalent YAML presentation produces the same identity, while any validated
logical change invalidates a pinned hash.

The platform-profile loader carries an expected definition hash, then composes the full
`ExperimentSpec` with signal-provider and execution-mode identity. Runtime adapter, broker,
storage, and submission authority remain in `PlatformConfig`. Existing legacy and collector
configuration remain unchanged until narrow compatibility adapters are deliberately introduced.

Profile composition enforces the external-series/runtime-adapter rule above and permanently
reserves the deterministic fixture provider to offline mode. Data contracts must still enforce
actual bar provenance, source mode, dataset identity, and readiness; this ADR does not claim that
offline bars, external bars, or their readiness checks are implemented.

The root-confined loader is POSIX-specific because it uses descriptor-relative opens and
`O_NOFOLLOW`; Windows support would require an equally strong platform-specific path implementation
before this boundary can be claimed there.

## Security impact

The experiment file is a new untrusted local-input boundary. It can grant collection membership
and, through the active role only, downstream order eligibility. Strict frozen models,
default-deny role derivation, bounded parsing, root confinement, no-follow file access, explicit
hash preimages, and independent hash pinning prevent malformed or substituted configuration from
silently changing that authority.

The loader reads no environment variables or secret files, constructs no provider or broker
client, makes no network request, writes no persistent state, and cannot submit an order. Error
translation omits attacker-controlled file contents and parser/validation exceptions. Repeated
loads are pure apart from bounded reads and have no side effects. A compromised caller may choose
its trusted root; service composition must therefore own that root rather than accept it from a
remote request. The profile loader cannot omit the hash pin, and composition rejects the fixture
adapter and deterministic fixture signal outside offline mode. Data normalization must still
preserve actual provider provenance and never relabel fixture data as Alpaca data.

## Verification

`tests/unit/test_platform_experiment.py` verifies every shipped value, role and allowlist behavior,
WDC/SNDK separation, a literal canonical preimage and independently calculated SHA-256, immutable
models, strict typing, cross-field limits, hash mismatch, semantic/presentation behavior, hostile
YAML, bounded text, path confinement, symlink rejection, and safe errors. Platform branch coverage
is measured with the canonical tests and must remain at least 85 percent. Repository Ruff, mypy,
offline pytest, legacy backtest/replay, and Compose validation remain required regression gates.
`tests/unit/test_platform_profiles.py` additionally verifies the exact tracked profiles, mandatory
pin, composed known-answer hashes, closed adapter/broker modes, generic provider identity,
offline-only fixture boundary, immutable models, CLI aliases, broker-free validation, and hostile
profile inputs.
