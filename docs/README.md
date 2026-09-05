# Documentation Index

Start with the document that owns the question. Do not infer current behavior from a target
design or a roadmap item.

## Product and delivery

| Document | Authority |
| --- | --- |
| `../README.md` | Product context, research proposal, quickstart, and public status |
| `requirements.md` | Normalized requirement identifiers, implementation locations, and evidence |
| `execution-plans/PLANS.md` | Required structure and maintenance rules for execution plans |
| `execution-plans/platform-core.md` | Active milestones, decisions, progress, evidence, and limitations |

## Architecture and data

| Document | Authority |
| --- | --- |
| `../ARCHITECTURE.md` | Current dual-system architecture and target platform boundaries |
| `architecture.md` | Detailed legacy portfolio-application architecture |
| `data_dictionary.md` | Implemented legacy SQLite schema and export contracts |
| `market_data_runbook.md` | Implemented collector schema, activation, recovery, and credentials |
| `adr/README.md` | Architectural decision record index |

The root architecture is the cross-system map. The lowercase architecture and data
dictionary remain the legacy application's detailed references; they are not descriptions
of the target generic platform.

## Engineering policy

| Document | Authority |
| --- | --- |
| `engineering-principles.md` | Decision rules for scope, correctness, evidence, and simplicity |
| `coding-standards.md` | Python, persistence, error, concurrency, and public-contract conventions |
| `testing-strategy.md` | Test categories, network rules, PostgreSQL safety, and commands |
| `security-model.md` | Current assets, trust zones, controls, and residual risks |
| `security_architecture.md` | Current/target capability separation and target service controls |
| `threat_model.md` | STRIDE threat register, residual risk, and test traceability |
| `dependency-policy.md` | Dependency admission, locking, review, and removal policy |
| `code-review.md` | Finding severities and adversarial review procedure |
| `performance.md` | Measured-workflow policy and current sensitive paths |
| `observability.md` | Current logs, durable events, health semantics, and target gaps |
| `tooling.md` | Detected stack, exact local/CI tools, and command surface |

Root contributor policy is in `../CONTRIBUTING.md`; private vulnerability reporting is in
`../SECURITY.md`; repository-wide operating instructions are in `../AGENTS.md`.

## Operations and research evidence

| Document | Authority |
| --- | --- |
| `incident_response.md` | Legacy paper-simulation incident containment and recovery |
| `live_paper_runbook.md` | Legacy observer and paper-gate operating procedure |
| `market_data_runbook.md` | Collector migration, first activation, monitoring, and recovery |
| `methodology.md` | Legacy research calculations, timing, risk, and reporting conventions |

## Status interpretation

Use the status recorded beside each requirement or subsystem:

- `IMPLEMENTED_AND_VERIFIED`: conforming behavior or a required artifact exists and its stated
  verification has recorded local or CI evidence.
- `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED`: code exists, but the required external system or
  credentials were not used for validation.
- `PARTIALLY_IMPLEMENTED`: only an explicitly identified subset exists.
- `NOT_IMPLEMENTED`: documentation or target design exists without executable behavior.
- `BLOCKED`: work cannot proceed without a named authorization, credential, or dependency.
- `INTENTIONALLY_DEFERRED`: the item is outside the current implementation scope.

Historical backtest output, fake-broker replay, disposable PostgreSQL tests, hosted runtime
operation, and Alpaca credential validation are distinct kinds of evidence. A result in one
category must not be used to upgrade another category's status.

## Documentation maintenance

- Update requirements and the active plan with every material behavior change.
- Update architecture when ownership, dependency direction, persistence, or trust changes.
- Update runbooks only after the described operator path exists and its verification state
  is explicit.
- Record commands and exact results; use `NOT_IMPLEMENTED` or
  `IMPLEMENTED_NOT_EXTERNALLY_VALIDATED` instead of predictive claims.
- Never put credentials, raw provider data, account identifiers, prompt material, or local
  paths containing private state in documentation.
- Link to one source of truth rather than duplicating mutable tables or command lists.
