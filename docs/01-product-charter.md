# VERITAS -- Product Charter

Status: baseline (2026-08-06, after M1)
Owner: Rizky (QA/product), with the commercial-evidence reviewer
Sources: `supgate-build-plan.md` (Notion export, 6 Aug 2026), M1 repo state, reference report MOK-20260806-RKGX

## 1. Identity and mission

- **Product name:** VERITAS -- Vendor Endpoint Reliability, Identity, Tamper & Assurance System.
- **CLI and package name:** `supgate` (kept unchanged to preserve commands and output contracts).
- **Mission:** Before any supplier endpoint is wired into production, admit it with evidence. VERITAS is a personal, black-box admission tool that probes any OpenAI-compatible endpoint (upstream supplier, relay gateway, white-label domain) and produces a scored, evidence-backed report covering protocol compliance, performance, relay tampering, capability contracts, and model authenticity.
- **Reference output shape:** report MOK-20260806-RKGX.
- **Tagline:** "Admit suppliers on evidence, not price."

## 2. Problem

- The chain (gateway -> white-label partners -> customers) is exposed end-to-end if an upstream supplier silently substitutes, mixes, or degrades models.
- Core driver: the supplier market is flooded with dirt-cheap "official model" endpoints. At those prices the economics usually only work by serving distilled, quantized, or older substitutes, or by mixing cheaper models behind the claimed name. Admission testing is the defense before any supplier is wired in.
- Reference evidence from MOK-20260806-RKGX: a non-OpenAI model emitting `chatcmpl-` ids (OpenAI-compat shell), stripped and rewritten official headers, 88% token-recount deviation (billing inflation), a constant +11 prompt-wrap offset (hidden injected system prompt), altered canary echo, and 0% goodput in every band.
- Research signal: KBF flagged all 155 substitutions across 16 production endpoints and detects 5-10% mixed routing. No globally used official model-identity checker exists; the gap is real. The closest incumbent (Artificial Analysis Endpoint Accuracy Index) covers quality parity only and is limited on open-weight models.

## 3. Target users

| User | Role | Primary use |
| --- | --- | --- |
| Rizky | QA / product | Supplier admission, ongoing assurance, QA ammunition for the gateway |
| the reviewer | Commercial evidence | Partner trust and SLA evidence; attach reports to commercial documents |
| White-label partners | Future service consumers | Scored, evidence-backed reports as a paid service |

## 4. Jobs to be done

| JTBD | Trigger | Desired outcome |
| --- | --- | --- |
| JTBD-1 Admit a supplier | About to wire a candidate endpoint into production | Know whether the endpoint is what it claims to be, with reproducible evidence, so I do not onboard a cheap substitute. |
| JTBD-2 Catch degradation | Supplier mixes or degrades models mid-contract | Catch it early and escalate upstream with a redacted, replayable failure packet, not a suspicion. |
| JTBD-3 Ground commercial claims | A partner asks about reliability or an SLA | Have an assurance level and goodput evidence to attach to commercial documents. |
| JTBD-4 Convert failures to work items | A probe fails during admission or rerun | Get a numbered QA issue in the locked submission format, ready to submit. |

## 5. Threat model

- **Adversaries:** upstream suppliers, relay/reseller gateways, white-label domains, account resellers.
- **Motives:** margin on serving cheaper models, quota pooling, reselling unstable or number-pooled accounts.
- **Capabilities:** full control of the endpoint; rewrite requests and responses; strip or replace official headers; inject hidden system prompts; mix routing (A/B splits, 5-10% fraction routing); emit spoofed ids; hide origin.
- **Hard to fake black-box:** the underlying model's statistical fingerprints (RNG distribution), knowledge-boundary recall, and logprob drift. GhostPrint shows spoofing risk, so no single signal is ever conclusive.
- **False-positive source:** legitimate provider behavior -- model updates, quantization, A/B tests, new routing. Guardrail: calibrated verdicts, confidence, and "suspected substitution" only with 2 independent signal families.

**Veto-eligible signals (Disqualified regardless of score):** exactly four veto codes -- `reverse_identity`, `substitution`, `billing_inflation`, `hidden_origin`. Tamper/canary evidence is never a standalone veto; it only corroborates a `confirmed_tampering` authenticity label, which itself requires 2 independent signal families.

## 6. Product principles

1. **Evidence-first.** Every failed probe ships a redacted, reproducible curl.
2. **Relaying is neutral; tampering is penalized.** Hop count is an unscored lower bound (>=1). Veto only on reverse identity, substitution, billing inflation, hidden origin.
3. **Calibrated verdicts, never binary.** Output is a verdict with confidence and the supporting signal families: consistent / suspected substitution / confirmed tampering -- never "distilled: yes/no". "Suspected substitution" and "confirmed tampering" each require 2 independent signal families; a confirmed tampering label is never declared on canary/tamper evidence alone.
4. **Skipped is not failed.** Probes skip cleanly when the prerequisite API surface is absent; skips are capped so they cannot zero a domain.
5. **429 is not proven absence.** Rate-limit-blocked probes score Warn, never Fail; they rerun with backoff or a higher-quota key, and a heavily rate-limited run is marked inconclusive. Verdicts must say why.
6. **Keys are secrets.** Env-only input, redaction at a single evidence choke point, redacted everywhere, never in reports.
7. **Dual output.** Probe detail for engineers; Supply Assurance Level (A/B/C/Disqualified) for procurement. Black-box caps at B; A needs supplier credentials.
8. **Re-baseline per model version.** Fingerprints drift when providers update; a stale baseline causes false accusations.
9. **Cost-bounded by design.** Per-run budget caps, two run modes, calibration constants set from baseline runs.
10. **Deterministic and replayable.** YAML manifests, a pass DSL, and reproducible curls make every verdict auditable.

## 7. Scope

### In scope

| Area | Detail |
| --- | --- |
| Targets | Any OpenAI-compatible endpoint: upstream suppliers, relay gateway, white-label domains |
| Runs | `adhoc` (minutes, low token burn) and `full` (30-60 min, entire catalog) |
| Domains | D2 performance, D4 relay fulfillment, D6 protocol compliance, D8 tool and capability contract |
| Output | JSON run bundle (the contract) + rendered HTML/PDF report, redacted evidence, reproducible curls |
| Scoring | Weighted domain means + Supply Assurance Level + independent veto layer |
| History | SQLite run history; M6 adds scheduled reruns and trend spot-checks |
| Integrations | QA-issue export for the gateway; baseline recording against official endpoints |

### Out of scope (product)

- White-box static analysis of supplier source code.
- Cryptographically proving distillation (impossible black-box by design).
- Hosting or operating LLM endpoints; building our own models.
- Acting as a marketplace or procurement decision-maker.
- Public grading of vendors.

## 8. Historical M1 snapshot (2026-08-06)

This section records the rollback baseline, not the current implementation.
Current M2 scope and verification status are in `11-m2-build-status.md` and the
operator sequence is in `12-operator-test-plan.md`.

- **Verification:** 106 tests passing (14 probes: 3 P0 + 11 D6), ruff clean, bytecode compile clean.
- **Implemented:** P0 (echo, models, error contract); D6 suite (chat basic x3, SSE x2, message shapes x3, json mode x2, tool passthrough x2, param boundaries x5, max_tokens x2, usage fields x2, idempotency x3, vision, Responses API); scoring + assurance mapping; redacted evidence + reproducible curl; SQLite history; JSON bundles; CLI with `run` / `history`.
- **Not yet implemented (stubs that skip):** D4 fingerprints, D4 billing, D2 load, D8 capabilities, auth v1.1; veto layer (reserved empty); report / baseline / export-qa commands (CLI stubs).
- **Known simplifications:** budget is naive (chars/4 at a blended $0.005/1K); transport/hop analysis is placeholder (`hop_lower_bound: 1`, `origin_class: unknown`); no tokenizer or per-model pricing.
- **Consequence:** assurance caps at C today. B needs D4 (fingerprint/billing) and D8 (capabilities) evidence; A needs supplier credentials (outside black-box reach).

## 9. M2-M6 outcomes

| Phase | Deliverable | Outcome / verify by |
| --- | --- | --- |
| M2 -- Fingerprints and billing | D4 suite (fingerprints + billing forensics), official baselines, veto wiring, real tokenizer | Run vs official OpenAI (clean) and one relay (detected); confirm relay caught, official clean |
| M3 -- Load and capabilities | D2 bands, D8 GPT/Claude suites, skip logic; optional d8.swebench_lite | TTFT/TPOT match manual curl timing; capability matrix matches known model abilities |
| M4 -- Scoring and report | Scoring finalized, assurance levels, HTML/PDF report, QA export | Reproduce a MOK-style report end-to-end; PDF text matches JSON fields |
| M5 -- Authenticity v1.1 | Cutoff battery, RNG fingerprint, logprob audit, mixed-routing sampler | Blind test: 2 endpoints, one deliberately mislabeled -- tool must flag it |
| M6 -- Ops | Scheduled report-only runs, run history trend, QA export | One week of daily runs; spot-check alerts |

## 10. Two-axis verdict model

Always report both axes. An endpoint can be honest yet unusable (high assurance, low score), or usable yet unverified (high score, C).

- **Axis 1 -- Quality grade:** overall score (0-100) = weighted mean of scored domains (D6 30%, D4 30%, D8 25%, D2 15%). Platform self-check and transit depth stay unscored.
- **Axis 2 -- Supply Assurance Level:** A (white-box verification with supplier credentials) / B (stable, no reverse or mixing, capabilities OK) / C (basically usable, identity unconfirmed) / Disqualified (tampering or broken contract).
- **Veto layer:** independent of the score. The four veto codes `reverse_identity`, `substitution`, `billing_inflation`, `hidden_origin` -> Disqualified regardless of score. Tamper/canary evidence corroborates but never vetoes alone; billing inflation may veto without an identity-family label.

## 11. Success metrics

| Metric | Target |
| --- | --- |
| M2 detection | Official endpoint clean; scripted relay detected on both axes |
| M5 blind test | Deliberately mislabeled endpoint flagged (calibrated verdict) |
| Mixed-routing sensitivity | Detects 5-10% fraction routing |
| False-acceptance | A known substitute is never admitted at assurance B or above |
| Run cost | adhoc <= $5, full <= $25 per run |
| Run time | adhoc in minutes; full in 30-60 min |
| Evidence coverage | Every failed probe has a redacted reproducible curl |
| Operational | One week of daily scheduled reruns (M6) with spot-checked alerts |

## 12. Non-goals

- No cryptographic proof of distillation; no "distilled: yes/no" output.
- No white-box source analysis of suppliers.
- No legal certification or formal audit opinion.
- No decision authority: the tool informs, humans admit.
- No public per-vendor grading.
- No billing, invoicing, or payment role.

## 13. Ethical and legal messaging

- **Calibrated, not binary.** Every report states a calibrated verdict with a confidence level and the supporting signal families (schema v2: authenticity verdict + confidence + signal_families, plus an inconclusive state when evidence is insufficient). "Suspected substitution" and "confirmed tampering" each require 2 independent signal families; tamper/canary evidence alone is never a standalone veto and never a confirmation. A bare "this is distilled" claim is never produced.
- **False-accusation guardrails.** Provider updates and quantization mimic substitution; therefore confidence is always stated, evidence is always attached, and fingerprints are re-baselined per model version.
- **Internal by default.** Probing supplier endpoints is normal acceptance testing, but reports stay internal. Partner-facing versions go through commercial review before distribution (see `10-open-decisions.md` OD-04, OD-05).
- **Redaction everywhere.** Keys, tokens, and sensitive headers are redacted at the single evidence choke point; reports reference keys, never print them.
- **Wording discipline.** Report language uses the calibrated set (consistent / suspected substitution / confirmed tampering), not accusatory or definitive claims about supplier intent.
