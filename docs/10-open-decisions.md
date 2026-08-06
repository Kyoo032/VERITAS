# VERITAS -- Open Decision Register

Status: active (2026-08-06)
Purpose: track decisions that must be made before or during the M2-M6 build. Each row names an owner, a deadline, options, a recommendation, and the blocking impact if left open.
How to use: resolve or update rows as the weekend plan (see `05-weekend-execution-plan.md`) executes. A row is "resolved" only when the recommendation is accepted and the owner records it.

## Summary

| ID | Decision | Owner | Deadline | Recommendation | Blocks |
| --- | --- | --- | --- | --- | --- |
| OD-01 | Baseline account ownership and cost | Rizky (budget: supplier) | Fri Aug 7 | Dedicated supplier eval keys on the company cost line | S3 baselines, M2 veto verification, M5 authenticity |
| OD-02 | Storage home for run bundles and reports | Rizky | Sun Aug 9 | Repo `runs/` for bundles; QA Reports attachments for final PDFs | M4 report distribution, M6 history |
| OD-03 | SLA tiers | Rizky + the reviewer | Sun Aug 9 | One global default + optional per-service-tier table | D2 goodput, SLA evidence |
| OD-04 | Report audience and distribution | the reviewer | Fri Aug 7 | Internal by default; partner-facing gated by commercial review | Legal wording, M4, M6 |
| OD-05 | Legal and commercial wording | the reviewer | Sun Aug 9 | Reuse white-label wording; counsel review before first partner use | M4 report text, OD-04 |
| OD-06 | Official-reference versioning | Rizky | Sat Aug 8 | Baseline per model version; family-level fingerprints as coarse layer | Fingerprint diff, cutoff battery, RNG fingerprint |
| OD-07 | Data retention | Rizky | Sun Aug 9 | Keep bundles forever; evidence per-run; keys never stored | Storage, privacy of customer prompts |
| OD-08 | Key handling | Rizky | Fri Aug 7 | Env-only at runtime; no .env in repo; rotate; secrets manager when partners onboard | Operational security, M6 scheduled runs |
| OD-09 | SWE-bench scope | Rizky | Sun Aug 9 | Optional stretch probe, not core M3 | M3 scope, D8 cost and weight |

---

## OD-01 -- Baseline account ownership and cost

- **Owner:** Rizky (budget line: personal).
- **Deadline:** Fri Aug 7 (before Sat baseline recording).
- **Options:**
  - (a) Dedicated supplier eval keys on the company cost line (new OpenAI + Anthropic org keys).
  - (b) Personal keys (existing subscriptions), low cost, but mixes personal and company spend.
  - (c) Shared account for the whole team, simplest but couples team usage.
- **Recommendation:** (a) dedicated eval keys on a personal cost line. Personal keys only as a fallback if the company line cannot be provisioned by the deadline. Canonical official-key env vars: `SUPGATE_OPENAI_OFFICIAL_KEY`, `SUPGATE_ANTHROPIC_OFFICIAL_KEY`, `SUPGATE_OFFICIAL_BASE_URL`.
- **Blocking impact:** without a baseline key, S3 baseline recording and M2 veto verification degrade to fixture-only replays; M5 authenticity baselines (cutoff battery, RNG fingerprint, logprob audit) cannot be calibrated. This is the single highest-risk open item.

## OD-02 -- Storage home for run bundles and reports

- **Owner:** Rizky.
- **Deadline:** Sun Aug 9 (before M4 report distribution).
- **Options:**
  - (a) Repo `runs/` (git-tracked JSON bundles + evidence), machine-consumable and versioned.
  - (b) Obsidian QA Reports attachments for final PDFs, close to the commercial workflow.
  - (c) Vault Reference folder.
  - (d) Object storage (S3) for large evidence volume.
- **Recommendation:** (a) for bundles and evidence (they stay small and must be replayable); (b) for final partner-facing PDFs. Keep (d) as a later option if M6 scheduled runs grow evidence volume.
- **Blocking impact:** M4 report distribution and M6 run history depend on where bundles live; wrong choice leaks customer prompts into the wrong place (see OD-07).

## OD-03 -- SLA tiers

- **Owner:** Rizky + the reviewer.
- **Deadline:** Sun Aug 9.
- **Options:**
  - (a) One global default (current `SLA()`: TTFT<=5s, TPOT<=500ms, E2E<=60s).
  - (b) Per-service-tier table (Standard / Enterprise / Official) as reference defaults.
  - (c) Per-run client SLA only, no tier table.
- **Recommendation:** keep the per-run client SLA as the contract (it already overrides), and add a tier table as reference defaults in the manifest so reports can state which tier's thresholds were used.
- **Blocking impact:** D2 goodput cannot be interpreted or presented as SLA evidence without a stated threshold source. This directly affects the reviewer's commercial use case.

## OD-04 -- Report audience and distribution

- **Owner:** the reviewer.
- **Deadline:** Fri Aug 7.
- **Options:**
  - (a) Internal only (Rizky + the reviewer + team).
  - (b) Partner-facing after commercial review (gate).
  - (c) Public per-vendor grading.
- **Recommendation:** (a) internal by default; (b) partner-facing only after commercial review; never (c). Probing supplier endpoints is normal acceptance testing, but reports stay internal and partner versions are reviewed (see `01-product-charter.md` Section 13).
- **Blocking impact:** sets the wording and redaction requirements for M4 reports, and governs whether endpoint identity is revealed in partner versions.

## OD-05 -- Legal and commercial wording

- **Owner:** the reviewer.
- **Deadline:** Sun Aug 9 (before first report is shared outside the team).
- **Options:**
  - (a) Reuse existing supplier / white-label SLA and commercial wording where it exists.
  - (b) In-house template drafted by the team.
  - (c) Counsel-reviewed template.
- **Recommendation:** (a) for immediate use, with (c) before the first partner-facing report ships. Calibrated verdict language (consistent / suspected substitution / confirmed tampering) is mandatory in every report regardless of option.
- **Blocking impact:** without agreed wording, M4 reports cannot be distributed externally, which blocks the partner trust and SLA-evidence use case.

## OD-06 -- Official-reference versioning

- **Owner:** Rizky.
- **Deadline:** Sat Aug 8.
- **Options:**
  - (a) Pin baselines per model version (baseline JSON carries vendor/model/version/captured_at).
  - (b) Family-level fingerprints only (cheaper, coarser).
  - (c) Both: family-level for coarse checks, version-pinned for fine ones.
- **Recommendation:** (c) both, stored in `baselines/`. Re-baseline per model version is an explicit guardrail because provider updates and quantization otherwise cause false accusations.
- **Blocking impact:** fingerprint diff, cutoff battery, and the M5 RNG fingerprint cannot be scored against a stale or missing baseline; wrong versioning silently breaks the two-axis verdict.

## OD-07 -- Data retention

- **Owner:** Rizky.
- **Deadline:** Sun Aug 9.
- **Options:**
  - (a) Keep bundles and evidence forever (small, auditable).
  - (b) Fixed retention window (e.g., 90 days) with archiving.
  - (c) Retention by run mode: adhoc short, full long.
- **Recommendation:** (a) keep bundles forever in `runs/`; evidence retained per-run while it is needed for assurance claims; keys never stored; only redacted data is persisted. Revisit (d) object storage only if volume demands it.
- **Blocking impact:** evidence contains real customer prompts; retention and redaction policy must be settled before M6 scheduled runs persist data at scale (also feeds OD-02).

## OD-08 -- Key handling

- **Owner:** Rizky.
- **Deadline:** Fri Aug 7.
- **Options:**
  - (a) Env-only at runtime (current behavior, `--key-env`).
  - (b) `.env` file loaded by the CLI.
  - (c) OS keychain / secrets manager.
- **Recommendation:** (a) keep env-only at runtime; never a `.env` in the repo; rotate eval keys per engagement; adopt a secrets manager when white-label partners consume the tool as a service. Keys are redacted at the single evidence choke point and never printed in reports. Canonical official-key env vars: `SUPGATE_OPENAI_OFFICIAL_KEY`, `SUPGATE_ANTHROPIC_OFFICIAL_KEY`, `SUPGATE_OFFICIAL_BASE_URL`.
- **Blocking impact:** operational security for all live runs, and M6 scheduled runs need a defined key source for cron execution.

## OD-09 -- Scope of SWE-bench

- **Owner:** Rizky.
- **Deadline:** Sun Aug 9.
- **Options:**
  - (a) Not in scope.
  - (b) Optional stretch probe `d8.swebench_lite` (MIT licensed, Princeton NLP; Lite 300 / Verified 500; offline eval against OpenAI-compatible endpoints; ~$0.70/task agentless).
  - (c) Core M3 capability probe.
- **Recommendation:** (b) optional stretch. It is a strong agentic-capability probe and cheap to run, but it is not required for any assurance-level gate, and its token burn must stay inside the per-run budget.
- **Blocking impact:** M3 scope and D8 cost/weight. If adopted, it must register with skip logic and a budget cap so it cannot inflate D8 cost or trip 429s.

---

## Resolved

| ID | Decision | Date |
| --- | --- | --- |
| -- | Name the standalone project VERITAS | 2026-08-06 |
| -- | Keep `supgate` as the package/CLI name | 2026-08-06 |
| -- | Canonical official-key env vars: `SUPGATE_OPENAI_OFFICIAL_KEY`, `SUPGATE_ANTHROPIC_OFFICIAL_KEY`, `SUPGATE_OFFICIAL_BASE_URL` | 2026-08-06 |
| -- | Target baseline schema is `docs/08` schema v2, explicitly superseding the build-plan scaffold | 2026-08-06 |
| -- | M1 catalog is 14 probes (3 P0 + 11 D6) | 2026-08-06 |
| -- | Weekend Must = M2 core D4 (10 probes); `d4.reasoning_cache_fields` stays M2 scope, weekend Stretch | 2026-08-06 |
| -- | Weekend S0: `RunContext.stream` + `StreamedEvent` + retry/mid-stream semantics; transport FAIL, persistent 429/5xx after one retry WARN | 2026-08-06 |
| -- | Weekend S2 rescaled to 4-6h; D2/D8 remain Should, claude_suite + M4 report Stretch; B-path is a scoring unit test, not E2E | 2026-08-06 |
| -- | Budgets $5 (adhoc) / $25 (full) are operator inputs, not CLI defaults | 2026-08-06 |
| -- | Packaging risk softened: verify manifest YAML ships via a wheel build | 2026-08-06 |
