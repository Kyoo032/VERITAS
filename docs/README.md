# VERITAS Design and Planning Index

Status: reviewed design pack for the Aug 8-9, 2026 build weekend
Project: `C:\Users\rizky\Documents\VERITAS`
CLI/package: `supgate`

## Purpose

This directory is the implementation source of truth for the next VERITAS
milestones. It separates current M1 behavior from target M2-M6 design so the
weekend build can proceed without rediscovering product, architecture, probe,
data, and validation decisions.

## Reading order

| Order | Document | Use it for |
| --- | --- | --- |
| 1 | [01-product-charter.md](01-product-charter.md) | Mission, scope, threat model, product principles, success criteria |
| 2 | [07-research-landscape.md](07-research-landscape.md) | Existing tools, primary research, build-vs-integrate choices |
| 3 | [02-system-architecture.md](02-system-architecture.md) | Components, trust boundaries, module ownership, topology |
| 4 | [03-evaluation-flow.md](03-evaluation-flow.md) | Run sequence, state machine, retries, skips, budgets, exits |
| 5 | [04-assurance-loop.md](04-assurance-loop.md) | Baseline-to-admission-to-monitoring operational loop |
| 6 | [05-weekend-execution-plan.md](05-weekend-execution-plan.md) | Timeboxed Friday/Saturday/Sunday work plan and acceptance gates |
| 7 | [06-m2-probe-spec.md](06-m2-probe-spec.md) | Implementation contracts for M2 D4 fingerprint and billing probes |
| 8 | [08-output-data-contract.md](08-output-data-contract.md) | Schema v2, evidence, baselines, SQLite, report/export contracts |
| 9 | [09-testing-validation-plan.md](09-testing-validation-plan.md) | Fixtures, replay, live validation, quality gates, cost safety |
| 10 | [10-open-decisions.md](10-open-decisions.md) | Owner decisions and blockers that must be resolved |
| 11 | [11-m2-build-status.md](11-m2-build-status.md) | Implemented scope, offline acceptance, live blockers, carry-over |

## Authority rules

When documents disagree, use this order:

1. Current source and tests define implemented M2 behavior.
2. `06-m2-probe-spec.md` defines M2 probe behavior and tolerances.
3. `08-output-data-contract.md` schema v2 defines target persisted output and
   supersedes the section 13 SQLite/baseline scaffold in the exported build
   plan.
4. `05-weekend-execution-plan.md` defines weekend priority only. It does not
   reduce the final milestone scope described in the product charter.
5. `10-open-decisions.md` records unresolved owner choices; unresolved items
   must not be silently guessed during implementation.

## Locked design rules

- Relaying is neutral; identity, billing, or origin tampering is penalized.
- Model authenticity is a calibrated verdict, never cryptographic proof.
- Suspected substitution and confirmed tampering require two independent
  signal families.
- Exactly four veto codes exist: `reverse_identity`, `substitution`,
  `billing_inflation`, `hidden_origin`.
- Independently confirmed multi-size billing inflation may veto without an
  identity-family label.
- One retry on HTTP 429/5xx, then Warn. Transport errors stay Fail.
- Skipped prerequisites do not lower a domain score.
- Every failure carries redacted evidence and a reproducible curl.
- Keys are env-only and never persisted.

## Current and target scope

| State | Scope |
| --- | --- |
| Current M2 | 37 registered probes: 25 in `adhoc` (3 P0 + 11 D6 + 11 D4), plus 2 D2 + 10 D8 in `full`; schema-2 bundles, baselines, vetoes, evidence, scoring, history |
| Weekend Must | Implemented and offline-verified; official baseline validation remains key-blocked |
| Weekend Stretch | `d4.reasoning_cache_fields` and D2/D8 implemented; M4 report and supplier live run carried over |
| Later | Authenticity v1.1, scheduled monitoring, and partner-facing reports |

## Build output

- Product charter and realistic weekend scope are documented.
- Architecture, trust boundaries, run flow, and assurance loop have Mermaid
  diagrams.
- M2 probe contracts and schema v2 are implemented and fixture-verified.
- Research landscape is sourced and separates facts, inference, and
  recommendation.
- Testing, replay, live-key isolation, and false-accusation guardrails are
  defined before implementation.
- The finalized operator sequence, bug packet, debug loop, and improvement
  backlog are in `12-operator-test-plan.md`.
- Open owner decisions remain visible in `10-open-decisions.md`.
- The exact technical status and carry-over are recorded in
  `11-m2-build-status.md`.
