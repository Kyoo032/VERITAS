# 07 Research Landscape: Model Identity, Endpoint Verification, and the Threat-Enabling Tooling

- **Owner:** AI Agent (documentation-only task)
- **Scope:** This document is a research landscape for VERITAS. It is documentation only; no
  code, tests, README, or `pyproject.toml` files were modified.
- **Companion:** README.md (M1 scope, D4/D8 stubbed) and `supgate/manifests/probes.yaml`.
- **Access convention:** every major claim carries a source URL and access date
  `2026-08-06`. Nothing in this file was verified interactively; it is a static snapshot.
- **Labeling:** claims are explicitly tagged **[FACT]** (stated by the cited source),
  **[INFERENCE]** (reasoning from cited facts), or **[RECOMMENDATION]** (VERITAS guidance).
  "Opaque" sources are flagged in the caveats section.

---

## 1. Executive summary

- **[FACT]** There is **no official cryptographic API-level standard** that lets an
  OpenAI-compatible client attest which model weights actually served a request. Vendors
  expose a `model` string; nothing in the wire protocol binds that string to a binary
  (KBF explicitly excludes cryptographic attestation from its threat model;
  https://arxiv.org/abs/2605.29524, accessed 2026-08-06).
- **[INFERENCE]** Therefore user-side model identity is necessarily a **behavioral,
  statistical** question, not a cryptographic one. A family of black-box fingerprinting and
  auditing techniques (LLMmap, TRAP, KBF, IRIS, "One Token Is Enough") attacks exactly this
  gap, with different tradeoffs in cost, robustness, and required signal.
- **[FACT]** A whole literature now shows that **relay endpoints measurably deviate** from
  advertised models: KBF flags 7/28 platform-model cells in a six-platform shadow audit;
  IRIS flags 14/15 same-model provider pairs on live endpoints; "One Token Is Enough"
  reports a proprietary-branded flagship endpoint distributionally indistinguishable from an
  open-weight model.
- **[FACT]** The adversarial side is real: `one-api` and `new-api` are widely deployed
  gateways whose model-mapping and quota/multiplier features **natively enable** silent model
  substitution and price arbitrage, and GhostPrint demonstrates that a fine-tuned weak model
  can **spoof** the fingerprinting methods themselves.
- **[RECOMMENDATION]** VERITAS should position itself as an **endpoint-assurance harness**
  (repeatable, evidence-bundled, budget-capped, protocol + fingerprint + billing +
  capability scoring) rather than another point fingerprinting tool. Its D4 domain is the
  natural home for KBF/IRIS-style audit probes; SWE-bench OSS-family subsets are viable only
  as an *optional* D8 capability probe, not a routine identity check.

---

## 2. The verification gap

### 2.1 No official cryptographic API model-identity standard

- **[FACT]** No major provider documented for this landscape ships an API request/response
  field that cryptographically proves which weights produced a completion. The relay
  auditing literature treats verifiable inference, zero-knowledge proofs, and trusted
  hardware as requiring *provider cooperation* and explicitly out of scope for a black-box
  auditor (KBF, section "Non-goals";
  https://arxiv.org/abs/2605.29524, accessed 2026-08-06).
- **[INFERENCE]** A bearer token or API key cannot carry model provenance. Keys attest *to
  the relay's control plane* that a request is authorized; they do not, and cannot by
  construction, attest which upstream served it.
- **[INFERENCE]** Consequences for VERITAS: identity findings must be expressed as
  *statistical consistency verdicts* (e.g., "consistent with claimed model at 95% confidence"
  or "different"), never as cryptographic proof of substitution.

### 2.2 API-key validity vs. key provenance

- **[FACT]** "Key validity" and "key provenance" are different claims:
  - **Validity** = the presented key is accepted by the endpoint (non-401/403 and normal
    `usage`/token accounting). This is what `supgate` P0 and D6 probes already exercise.
  - **Provenance** = which model/weights actually served the request. No API field or key
    property establishes provenance (see 2.1).
- **[FACT]** OpenRouter's own routing layer can serve a model slug from any of several
  providers, and by default load-balances across them by price
  (https://openrouter.ai/docs/guides/routing/provider-selection, accessed 2026-08-06).
  OpenRouter's opt-in router metadata explicitly separates `requested` (the slug the client
  sent) from the provider/model that actually served (`endpoints[].model`)
  (https://openrouter.ai/docs/guides/features/router-metadata, accessed 2026-08-06).
- **[INFERENCE]** A relay operator can therefore accept a valid key, record normal usage,
  and still silently change the served model. Key validity is a **necessary** but far from
  **sufficient** condition for "the claimed model was served."
- **[FACT]** The `one-api` gateway documents a **model mapping / redirect** feature that
  rewrites the user-requested model server-side (with an explicit warning that enabling it
  rebuilds the request body instead of passing it through)
  (https://github.com/songquanpeng/one-api, accessed 2026-08-06).
- **[RECOMMENDATION]** VERITAS reports should carry a "provenance evidence tier": e.g.,
  `T0 = none` (valid key only), `T1 = protocol-consistent`, `T2 = behavioral fingerprint
  matches reference`, `T3 = fingerprint + billing/cache forensics agree`. No current probe
  can reach a "cryptographic" tier.

---

## 3. The transparency-vs-verification landscape

### 3.1 OpenRouter: transparency without verification

- **[FACT]** OpenRouter is a multi-provider relay that exposes substantial *operational
  transparency*: provider lists and terms-of-service links, data-collection policy tags,
  zero-data-retention (ZDR) routing, provider pinning via `provider.order`/`only`/`ignore`,
  quantization filters, and price/throughput/latency-aware routing
  (https://openrouter.ai/docs/guides/routing/provider-selection, accessed 2026-08-06).
- **[FACT]** With the `X-OpenRouter-Metadata: enabled` header, every successful response
  includes `openrouter_metadata` disclosing `requested` vs. served provider/model, attempt
  count, fallbacks, and pipeline stages (compression, guardrails, healing, server tools)
  (https://openrouter.ai/docs/guides/features/router-metadata, accessed 2026-08-06).
- **[INFERENCE]** This is **disclosure, not verification**: it tells you which provider
  OpenRouter selected and attempted, not what weights that provider ran. A provider can lie
  to OpenRouter the same way it can lie to you.
- **[INFERENCE]** OpenRouter's own design acknowledges identity ambiguity: the metadata
  field exists precisely because "requested" may differ from "served." For VERITAS,
  OpenRouter is both a convenient *reference federation* (pin a provider, as KBF does) and
  the *canonical test market* in which all of the 2026 auditing papers measure deviation.

### 3.2 LMArena (formerly LMSYS Chatbot Arena)

- **[FACT]** LMArena ("Arena", run by Arena Intelligence) is a crowd-sourced Elo leaderboard
  built from blind human preference votes across Text, Code, WebDev, Vision, Document,
  Search, Image and Video arenas, plus an Agent arena
  (https://lmarena.ai/leaderboard, accessed 2026-08-06).
- **[INFERENCE]** Arena Elo measures *subjective preference quality* of whatever model the
  platform labels in each blind pair. It is a **ranking**, not an identity-verification
  instrument; it cannot tell you whether the backend behind a slug is the advertised model.
- **[RECOMMENDATION]** Use Arena-style leaderboards only as coarse, qualitative context for
  capability scoring. They are not a D4 or D8 probe.

### 3.3 Artificial Analysis and the Endpoint Accuracy Index (EAI)

- **[FACT]** Artificial Analysis launched the **Endpoint Accuracy Index** (article published
  August 4, 2026): "Measuring whether provider endpoints serve the same model quality as the
  reference" (https://artificialanalysis.ai/, accessed 2026-08-06;
  https://artificialanalysis.ai/articles/endpoint-accuracy-index, accessed 2026-08-06).
- **[FACT]** Methodology: three equally weighted evaluations run against each serverless
  endpoint and against Artificial Analysis's own **self-hosted reference deployment of the
  official weights**: tool calling (BFCL v4-500; 500 questions, 3 repeats), scientific
  reasoning (HLE-250; 250 questions, 10 repeats), and long-context recall (AA-LCR-25;
  25 questions, 10 repeats). 100% means the endpoint matches the reference within the
  reference's confidence interval. Coverage at launch: GLM-5.2, gpt-oss-120b, DeepSeek V4
  Pro (https://artificialanalysis.ai/articles/endpoint-accuracy-index, accessed 2026-08-06).
- **[FACT]** Headline results: restrictive output-token limits halved or worse HLE-250
  scores on some GLM-5.2 endpoints; gpt-oss-120b tool-call handling ranged 22% (BFCL-500)
  vs. 37% for the reference; DeepSeek V4 Pro endpoints were mostly at reference parity
  (https://artificialanalysis.ai/articles/endpoint-accuracy-index, accessed 2026-08-06).
- **[INFERENCE]** EAI is the strongest *public, third-party* evidence that "same model slug,
  different provider" measurably changes *capability*, not just identity. Its reference-vs-
  endpoint protocol is conceptually the same dual-oracle design as KBF and IRIS, but its
  purpose is **accuracy preservation**, not **identity/substitution detection**. It does not
  tell you whether a relay swapped models; it tells you how much quality a given serving
  stack preserved.
- **[RECOMMENDATION]** Treat EAI-style measurement as an *optional D8 capability probe*
  (does the served endpoint preserve the advertised model's capability), complementary to
  D4 identity probes. The self-hosted-reference pattern is directly reusable in VERITAS
  whenever the auditor holds official API credentials for the claimed model.

---

## 4. The active black-box fingerprinting/auditing arsenal

Ordered roughly by maturity (oldest first). Repos and licenses are noted; young/unproven
tools are flagged in Section 8.

### 4.1 LLMmap (arXiv 2407.15847, USENIX Security 25)

- **[FACT]** Active fingerprinting for LLM-integrated applications: sends a small set of
  hand-crafted queries (model meta-information, banner-grabbing, weak-alignment and
  malformed queries, prompt-injection-style triggers) and classifies the collected trace
  with a trained encoder. Identifies 42 LLM versions at >95% accuracy with as few as 8
  interactions, in closed-set and open-set modes; designed to be robust to unknown system
  prompts, sampling hyperparameters, and RAG/CoT wrappers
  (https://arxiv.org/abs/2407.15847, accessed 2026-08-06).
- **[FACT]** Banner grabbing is explicitly shown to be unreliable: models misstate their own
  identity (e.g., aya-23-35B claims "Coral", Phi-3-mini claims "GPT-4", SOLAR-10.7B claims
  "OpenAI"), so self-identification cannot be the identity mechanism
  (https://arxiv.org/html/2407.15847v4, accessed 2026-08-06).
- **[FACT]** Code: https://github.com/pasquini-dario/LLMmap, MIT license, 407 stars,
  6 commits, ships a pretrained open-set model with 52 behavioral templates (repo page,
  accessed 2026-08-06).
- **[INFERENCE]** LLMmap is a fast *discovery/identification* tool ("which model am I
  talking to"), not a *relay-audit* tool: it does not compare a suspect endpoint against a
  pinned reference and it uses security-sensitive probes (prompt-injection triggers, harmful
  requests) that KBF argues are unsuitable for routine third-party auditing
  (https://arxiv.org/abs/2605.29524, accessed 2026-08-06).

### 4.2 TRAP: BBIV (arXiv 2402.12991, ACL 2024 Findings)

- **[FACT]** Defines the **Black-box Identity Verification (BBIV)** problem: determine
  whether a third-party application uses a certain LLM through its chat function. TRAP
  repurposes adversarial suffixes (originally for jailbreaking) into a "honeypot": the
  target model emits a pre-defined answer while other models give random answers. Reported
  >95% true-positive rate at under 0.2% false-positive rate after a single interaction, and
  robustness to minor model modifications
  (https://arxiv.org/abs/2402.12991, accessed 2026-08-06).
- **[INFERENCE]** TRAP gives the strongest *single-interaction* evidence per query, but the
  trigger prompts are visibly adversarial and easy for a relay to special-case (detect the
  suffix, route to the claimed model). It is a good *high-signal one-shot* probe, a poor
  *renewable routine* audit.

### 4.3 KBF: Knowledge Boundary as Fingerprint (arXiv 2605.29524)

- **[FACT]** A low-cost black-box **relay-auditing** protocol: (1) generate numerical
  knowledge-boundary probes from an official reference API across 15 domains, (2) screen for
  reference stability and (optionally) contrast against a cheap substitute, (3) self-calibrate
  the reference's own error rate via a Clopper-Pearson 99% bound, then (4) test the suspect
  endpoint with a one-sided binomial test
  (https://arxiv.org/abs/2605.29524 and https://arxiv.org/html/2605.29524v2, accessed
  2026-08-06).
- **[FACT]** Reported results on 16 production endpoints via OpenRouter (8 families, 3 price
  tiers): all 155 economically relevant substitutions flagged at p<0.05 including all 12
  within-family downgrades, zero false positives on same-model controls; a full audit costs
  about $0.39 after a one-time ~$22 probe-generation run; 60/60 substitutions detected and
  0/30 false positives across six benign deployment configurations; mixed-routing detection
  reaches >=80% TPR for a same-class substitute at 13-35% rerouted traffic and >95% TPR for
  the hardest pair at ~43% reroute, while a budget-tier substitute is caught below 7%
  (https://arxiv.org/abs/2605.29524, accessed 2026-08-06).
- **[FACT]** Real-world shadow API audit: six platforms, 28 platform-model cells, ~$10; 7
  cells statistically inconsistent with the reference endpoint, concentrated on premium
  Claude endpoints; a lower-priced tier of the same advertised model was inconsistent while
  the higher-priced tier was consistent
  (https://arxiv.org/abs/2605.29524, accessed 2026-08-06).
- **[FACT]** Code and 16 ready-to-use probe sets: https://github.com/Ooo0ption/KBF, Apache
  2.0, 16 stars, single commit, Python scripts `kbf_test.py` / `generate_probes.py` with
  OpenRouter provider pinning (`allow_fallbacks: false`); verdicts are SAME / DIFF /
  UNDETERMINED (repo page, accessed 2026-08-06).
- **[INFERENCE]** KBF is currently the closest published match to a production-grade relay
  audit: ordinary-looking factual probes, precision-first statistics, cheap re-runs, and
  explicit non-claim on cryptographic attestation. Its weaknesses: single-author repo, one
  commit, 16 stars (young/unproven), probe sets pinned to OpenRouter providers as of March
  2026, and no peer-review venue listed as of access date.

### 4.4 IRIS: budgeted black-box auditing (arXiv 2607.20860)

- **[FACT]** A text-only audit that fingerprints the backend by asking endpoints to generate
  random numbers/strings. It is, per the authors, the first text-only audit to combine
  whole-stream substitution detection, fractional (epsilon) dilution detection, served-model
  attribution, routing-fraction estimation, and a self-sizing query budget (a cheap pilot
  fits exponential query-error decay before suspect queries are issued)
  (https://arxiv.org/abs/2607.20860, accessed 2026-08-06).
- **[FACT]** Reported results: 0.99 AUROC verifying the backend on an intra-family Qwen3
  ladder; on a commercial OpenRouter library it catches epsilon=0.3 dilution on
  margin-qualified pairs at 0.85 mean power with 0.017 false-positive rate and recovers
  epsilon to within 0.04; a live cross-provider audit flagged 14 of 15 same-model provider
  pairs via genuine quantization and kernel deviations, corroborated on third-party MET
  traces; adaptive allocation lifts the matched-budget target-hit rate from 73% to 87%
  (https://arxiv.org/abs/2607.20860, accessed 2026-08-06).
- **[INFERENCE]** IRIS addresses the two things KBF handles more cheaply/less directly:
  *partial* substitution (dilution) and *attribution* of the served backend, with a
  self-sized budget. Its tradeoff is lower per-query discriminative power (it needs
  distributional evidence), and as a July 2026 single-version preprint it is unproven in
  the field beyond the authors' measurements.

### 4.5 One Token Is Enough (arXiv 2607.10252)

- **[FACT]** Fingerprints an LLM as the empirical distribution of answers to trivial
  one-word prompts ("name a random number between 1 and 100") collected across four
  languages, at one output token per query. On 165 models served via OpenRouter: (i) the
  distributions are highly non-uniform (median cell entropy 1.0 bit) and model-specific,
  with split halves of the same model's samples about an order of magnitude closer than
  different models; (ii) Jensen-Shannon divergence between fingerprints recovers model
  lineage at 59.5% leave-one-out accuracy vs. 18.4% chance; (iii) a biometric-style
  verification protocol reaches 7.3% equal error rate with a full 40-cell battery and <11%
  with eight probe cells - roughly a hundred single-token queries per audit
  (https://arxiv.org/abs/2607.10252, accessed 2026-08-06).
- **[FACT]** Reports ecosystem anomalies, including a proprietary-branded flagship endpoint
  distributionally indistinguishable from an open-weight Qwen model; the protocol, prompts,
  raw data, and analysis code are released
  (https://arxiv.org/abs/2607.10252, accessed 2026-08-06).
- **[INFERENCE]** This is the cheapest credible identity evidence per query (single tokens,
  benign-looking prompts), which makes it attractive for *continuous/ambient* verification
  inside a live workload - but its EER (~7% full, ~11% reduced) is too weak alone for a
  decisive audit; it is a screening signal, best fused with KBF/IRIS-style tests. As a
  July 2026 single-author preprint with no venue listed, treat all numbers as
  unindependently-replicated.

### 4.6 GhostPrint: the spoofing limitation of all of the above (arXiv 2606.16100)

- **[FACT]** Introduces **fingerprint spoofing**: a malicious provider serves a weaker model
  that has been parameter-efficiently fine-tuned to mimic a stronger model, evading
  user-side fingerprinting. Provides a formal argument that user-side resource constraints
  (finite query budgets and weak fingerprinting classifiers) make current fingerprinting
  vulnerable, and builds GhostPrint from surrogate modeling, reward-ranked fine-tuning, and
  knowledge distillation
  (https://arxiv.org/abs/2606.16100, accessed 2026-08-06).
- **[FACT]** Reports that GhostPrint lets weak models consistently bypass representative
  fingerprint methods in both static and continual fingerprinting settings while retaining
  utility at low fine-tuning cost
  (https://arxiv.org/abs/2606.16100, accessed 2026-08-06).
- **[INFERENCE]** This is the ceiling on the entire behavioral-auditing family: any
  user-side statistical test can in principle be tuned against. It raises the *cost of
  evasion* rather than eliminating it. Response options that remain: rotate/refresh private
  probe sets (as KBF recommends), combine independent signals (identity + billing + protocol
  + capability), run continuous monitoring rather than one-shot audits, and escalate to
  provider-credentialed white-box checks when Assurance level A is required. No current
  approach makes spoofing impossible without provider cooperation
  (https://arxiv.org/abs/2605.29524, accessed 2026-08-06).

### 4.7 Adjacent techniques referenced by the above (context only)

- **[FACT]** KBF benchmarks against MET (Model Equality Testing; two-sample MMD over
  completion distributions), ZeroPrint (perturbation/Jacobian-based fingerprints), and
  LLMmap, and describes their operational weaknesses for relay auditing (distributional
  fragility to wrappers for MET; threshold calibration for ZeroPrint; visible probe sets for
  LLMmap) (https://arxiv.org/html/2605.29524v2, accessed 2026-08-06).

---

## 5. Threat-enabling relay tooling: one-api and new-api

- **[FACT]** `one-api` (songquanpeng/one-api, MIT, ~36.2k stars) is a self-hosted "LLM API
  management and key redistribution system": it unifies many providers behind one
  OpenAI-compatible endpoint, supports channels, load balancing, token management
  (expiry/quota/IP/model allowlists), redemption codes, user/channel groups with custom
  pricing multipliers, per-channel model lists, model mapping/redirect, auto-retry, and
  per-channel model testing (https://github.com/songquanpeng/one-api, accessed 2026-08-06).
- **[FACT]** `new-api` (QuantumNous/new-api, AGPL-3.0, ~44.5k stars) is the actively developed
  fork: cross-format conversion (OpenAI-compatible <-> Claude Messages <-> Gemini), channel
  weighted random, failure retry, per-request usage accounting plus cache-hit billing for
  multiple providers, reasoning-effort model suffixes (o3/gpt-5/claude/gemini),
  thinking-to-content conversion, and an official key-quota tool
  (https://github.com/QuantumNous/new-api, accessed 2026-08-06).
- **[FACT]** Both projects exist for lawful gateway/aggregation use and carry terms-of-use
  warnings; the feature lists above are quoted as documentation facts, not as a claim that
  the projects themselves are malicious (https://github.com/songquanpeng/one-api and
  https://github.com/QuantumNous/new-api, accessed 2026-08-06).
- **[INFERENCE]** The same feature set is a **threat-enabling toolkit** for the abuses the
  auditing literature measures:
  - **Model mapping / redirect** (documented in one-api) silently rewrites the requested
    `model` server-side - a direct substitution enabler, and it can be scoped per channel,
    so one advertised slug can serve multiple different backends to different customers.
  - **Groups + multipliers** (both projects) allow per-customer pricing on top of a
    substituted backend - the economic incentive IRIS/KBF model.
  - **Format conversion** (new-api) means a relay can advertise e.g. a Claude model but
    translate requests to a Gemini or OpenAI-compatible channel - detection must survive
    protocol translation, which complicates D6 protocol probes.
  - **Per-channel model lists** (one-api) let an operator advertise models a channel does
    not actually serve, and the channel test prompt defaults to "Print your model name
    exactly..." - i.e., the weak self-report signal LLMmap already discredits.
- **[INFERENCE]** Prevalence is high enough to matter: 36k and 44k GitHub stars respectively
  indicate large self-hosting populations, and the 2026 auditing papers (KBF, IRIS, One
  Token) measure real deviation on real relays.
- **[RECOMMENDATION]** VERITAS's D4 domain should treat `one-api`/`new-api`-class gateways as
  the *reference adversary deployment*. Detection signals should assume: model-field
  rewriting, per-customer routing, format translation, multiplier-based misbilling, and the
  ability to answer fingerprint prompts by routing them to the claimed model.

---

## 6. SWE-bench family as an optional D8 capability probe

### 6.1 Facts

- **[FACT]** SWE-bench is the canonical real-world software-engineering benchmark (models
  resolve GitHub issues by producing a patch); MIT-licensed repository
  (https://github.com/SWE-bench/SWE-bench, accessed 2026-08-06). Evaluation is
  Docker-containerized and resource-intensive (recommended >=120 GB free disk, 16 GB RAM,
  8 CPU cores) (https://github.com/SWE-bench/SWE-bench, accessed 2026-08-06).
- **[FACT]** Official splits and sizes per swebench.com (accessed 2026-08-06):
  - **Full** = the full test set (2,294 instances; the leaderboard reports "% Resolved out
    of 2294 Full").
  - **Lite** = a 300-instance subset curated for less costly evaluation.
  - **Verified** = a 500-instance subset that human software engineers confirmed are
    solvable (collaboration with OpenAI Preparedness).
  (https://swebench.com/, https://github.com/SWE-bench/SWE-bench, accessed 2026-08-06.)
- **[FACT]** Datasets are distributed on Hugging Face
  (`SWE-bench/SWE-bench`, `SWE-bench/SWE-bench_Lite`, `SWE-bench/SWE-bench_Verified`) and
  cloud evaluation is available via `sb-cli` (https://github.com/SWE-bench/SWE-bench,
  accessed 2026-08-06).
- **[CAVEAT]** The specific **"SWE-bench OSS"** repository could not be reached on
  2026-08-06 (`github.com/swe-bench/SWE-bench-OSS` and `github.com/SWE-bench/SWE-bench-OSS`
  both returned HTTP 404; a GitHub repository search for "SWE-bench OSS" returned no
  canonical match). The Full/Lite/Verified facts above are taken from the official SWE-bench
  repository and site, which are unambiguous primary sources for those splits. Any claim
  that specifically depends on the OSS fork's license or contents remains **unverified in
  this landscape**.

### 6.2 Suitability analysis

- **[INFERENCE]** SWE-bench (Full/Lite/Verified, and the community OSS variant if its repo
  becomes reachable) is a good **capability** probe: it discriminates model capability the
  way the Endpoint Accuracy Index does, so it can detect a materially weaker substitute even
  when identity probes are spoofed or insufficient.
- **[INFERENCE]** It is a poor *identity* probe: pass@1 on 300-2,294 instances is expensive
  (thousands of tokens, Docker builds, hours), slow, stochastic, and only flags *capability
  gaps*, not provenance. Two different-but-equal-capability models would look identical;
  a capability-preserving quantization (like several EAI results) would be near-invisible.
- **[INFERENCE]** Cost/size ordering for VERITAS use: Verified (500, human-confirmed
  solvable) is the most trustworthy but heaviest; Lite (300) is the common lightweight
  choice; Full (2,294) is research-grade. A full run exceeds what a budget-capped
  `supgate` run should do by default.
- **[RECOMMENDATION]** Add SWE-bench as an **optional, opt-in D8 probe** (`d8.swe_bench_lite`
  as a *sampled* subset with a hard token/cost cap), not part of the default catalog.
  Document its verdicts as "capability-preservation" evidence feeding D8, kept separate from
  D4 identity verdicts. Reconsider the OSS repo only if it becomes reachable and its
  license is confirmed.

---

## 7. Competitor matrix

| Capability | LLMmap | TRAP | KBF | IRIS | One Token Is Enough | Artificial Analysis EAI | LMArena | OpenRouter | VERITAS (target) |
|---|---|---|---|---|---|---|---|---|---|
| Identity/attribution (which model) | Yes (closed/open set) | Yes (single target) | No (consistency only) | Yes (attribution) | Yes (weak, EER ~7-11%) | No (accuracy only) | No | No (disclosure only) | D4: consistency + limited attribution |
| Substitution detection | Indirect | Yes | Yes (full) | Yes (full + dilution) | Yes (weak) | Indirect (quality loss) | No | No | D4: KBF/IRIS-class |
| Fractional/dilution detection | No | No | Yes (partial) | Yes (epsilon estimate) | No | No | No | No | D4 backlog |
| Query cost per audit | ~8 queries | ~1 query | ~$0.4 (post ~$22 setup) | self-sized pilot | ~100 single tokens | high (3 evals x repeats) | N/A (crowd) | N/A | budget-capped |
| Signal required | text only | text only | text only | text only | text only | text only | human votes | routing data | text only |
| Robust to wrappers/RAG | Designed for | Partially | Yes (measured) | Yes (measured) | Untested (simple prompts) | Reference vs endpoint | N/A | N/A | D6-aware |
| Billing/usage forensics | No | No | No | No | No | No | No | Partial (usage) | D4 billing (M2) |
| Protocol compliance | No | No | No | No | No | No | No | Partial | P0 + D6 (M1) |
| Capability preservation probe | No | No | No | No | No | Yes | Yes (quality) | No | D8 (M3), optional SWE-bench |
| License/repo | MIT / pasquini-dario/LLMmap | n/a (paper) | Apache-2.0 / Ooo0ption/KBF | preprint (no repo found) | preprint (release claimed) | closed service | closed service | closed service | MIT (this project) |
| Maturity | USENIX 25, 407 stars | ACL 2024 | May 2026, 1 commit, 16 stars | Jul 2026 preprint | Jul 2026 preprint | live 2026 | live | live | M1 shipping |

*Cell notes:* "n/a (paper)" = no official artifact located for TRAP in this landscape.
KBF/IRIS/One-Token are flagged as young/unproven (single-commit repos or single-version
preprints, no independent replication found).

---

## 8. Build vs. integrate

| Decision | Option A: build | Option B: integrate | Assessment |
|---|---|---|---|
| D4 identity core | Reimplement KBF-style knowledge-boundary probes in-house | Vendor/wrap `github.com/Ooo0ption/KBF` (Apache-2.0) via subprocess or HTTP | **Hybrid.** Integrate KBF's *probe-generation* and *statistics* logic (Clopper-Pearson + binomial, per-pinned-provider) as the first D4 engine; do not fork its repo. Build the VERITAS manifest/runner/evidence layer around it. |
| One-token screening | Implement the 40-cell random-number battery | Copy the released prompt set (license-dependent) | **Build**, but reuse the released prompts as the seed set; it is ~100 lines and keeps D4 cheap for continuous monitoring. |
| Dilution + attribution | Full IRIS reimplementation | None available (no repo found) | **Defer to backlog.** Schedule only after KBF-core ships; IRIS is July 2026, unproven. |
| Capability probe | Wrap SWE-bench Lite (sampled, Docker) | None needed | **Build as optional** D8 probe, gated by `--mode full` and a cost cap. |
| Relay adversary emulation | Run a local one-api/new-api instance as a test fixture | Use official Docker images | **Build** (test fixture only). Provides deterministic substitution/mapping scenarios for D4 probe validation. |
| EAI-style reference benchmarking | Self-host reference weights + BFCL/HLE subsets | Follow AA methodology page | **Defer**; heavy, research-grade, optional D8. |

---

## 9. Novelty thesis for VERITAS

- **[FACT]** The 2026 literature is a set of *point tools*: KBF (consistency audit), IRIS
  (dilution + attribution), One-Token (cheap screening), TRAP (one-shot trigger), LLMmap
  (discovery). None of them is a repeatable assurance harness; none binds identity evidence
  to protocol compliance, billing forensics, or capability preservation in one scored run
  (https://arxiv.org/abs/2605.29524, https://arxiv.org/abs/2607.20860,
  https://arxiv.org/abs/2607.10252, https://arxiv.org/abs/2402.12991,
  https://arxiv.org/abs/2407.15847; all accessed 2026-08-06).
- **[FACT]** The commercial transparency layer (OpenRouter metadata) is disclosure without
  verification, and the quality layer (Artificial Analysis EAI) is verification of quality
  without identity (https://openrouter.ai/docs/guides/features/router-metadata,
  https://artificialanalysis.ai/articles/endpoint-accuracy-index; accessed 2026-08-06).
- **[INFERENCE]** VERITAS's differentiated claim is therefore **unified supplier assurance**:
  a manifest-driven, evidence-bundled, budget-capped harness that (a) verifies the
  OpenAI-compatible protocol contract (P0/D6), (b) audits relay identity with
  KBF/IRIS-class statistical probes against a pinned reference (D4), (c) performs billing
  forensics on usage/cache fields (D4), and (d) optionally probes capability preservation
  (D8/SWE-bench). No single cited artifact spans these four domains.
- **[INFERENCE]** The combination is more than the sum because the GhostPrint limitation is
  mitigated by *signal fusion*: an attacker must simultaneously spoof identity probes,
  keep protocol/billing behavior consistent, preserve capability, and survive continuous
  monitoring - much harder than evading one fingerprint family
  (https://arxiv.org/abs/2606.16100, https://arxiv.org/abs/2607.10252; accessed 2026-08-06).
- **[RECOMMENDATION]** State this thesis in README/docs as "assurance-grade, evidence-tied,
  multi-signal black-box supplier evaluation," and keep the D4/D8 separation explicit so the
  novelty claim stays honest (identity vs. capability are different measurements).

---

## 10. Research caveats and source-quality notes

1. **[FACT]** Three of the six core papers (IRIS 2607.20860, One Token Is Enough 2607.10252,
   GhostPrint 2606.16100) are **single-version preprints (v1)** with no listed peer-review
   venue as of 2026-08-06. KBF (2605.29524) is v2 but also has no listed venue. All numbers
   above are author-reported; no independent replication was located.
2. **[FACT]** Artifact maturity is low where it exists: KBF repo = 1 commit, 16 stars, 0
   watchers; LLMmap repo = 6 commits, 407 stars (mature enough for USENIX-attributed code).
   No official artifact was found for TRAP; no repository was found for IRIS; the
   One-Token release is claimed in the abstract but its URL is not given there.
3. **[CAVEAT]** The **"SWE-bench OSS"** repository URL 404'd on 2026-08-06; Full/Lite/Verified
   facts were sourced from the official SWE-bench repo/site instead. Any OSS-specific license
   or content claim is unverified here.
4. **[CAVEAT]** LMArena and Artificial Analysis are **commercial, closed** services whose
   leaderboard pages are JS-heavy; figures quoted here are the static text rendered at
   access time and may change without notice. EAI coverage rotates (models enter/exit).
5. **[CAVEAT]** OpenRouter, one-api, and new-api pages are vendor documentation; feature
   descriptions are quoted as documentation facts, and "threat-enabling" is an inference
   about the documented feature set, not an accusation about the maintainers.
6. **[CAVEAT]** The model names appearing in 2026 sources (GPT-5.4/5.5/5.6, Claude Opus 5,
   Qwen3.5/3.8, DeepSeek V4 Pro/Flash, Gemini 3.x, gpt-oss-120b, etc.) reflect the cited
   sources' publishing dates. VERITAS must not hard-code these; reference fingerprints and
   probe sets should be generated fresh against the current claimed model, as KBF
   recommends (https://github.com/Ooo0ption/KBF, accessed 2026-08-06).
7. **[CAVEAT]** arXiv pages list "arXiv:260x/2607" identifiers that postdate this author's
   training data; this landscape reports them exactly as fetched on 2026-08-06 and makes no
   claim about their existence outside this snapshot.
8. **[INFERENCE]** Statistical figures (FPR/TPR/AUROC/EER/cost) are single-source and
   environment-specific (OpenRouter provider selection, sampling settings, time of
   measurement). Treat them as order-of-magnitude, not guarantees, when setting VERITAS
   thresholds.

---

## 11. Prioritized integration backlog

Priority = P0 (M2/D4 foundation) -> P1 (M2/D4 enhancement) -> P2 (M3/D8 optional).

| # | Priority | Item | Rationale (cited above) |
|---|---|---|---|
| 1 | P0 | Ship KBF-core as the first D4 identity probe: probe generation from a pinned reference API, Clopper-Pearson self-calibration, binomial verdict (SAME/DIFF/UNDETERMINED). | Most mature, cheapest, precision-first relay audit; Apache-2.0; directly fits D4 (https://arxiv.org/abs/2605.29524; https://github.com/Ooo0ption/KBF). |
| 2 | P0 | Bind D4 to a **pinned OpenRouter provider** (allow_fallbacks=false) and record routing metadata when available. | Prevents cross-provider noise; KBF documents this need (KBF repo README; https://openrouter.ai/docs/guides/routing/provider-selection). |
| 3 | P0 | Add a **model-mapping detection probe** against one-api/new-api-style relays (advertise X, request Y, observe served behavior). | Directly addresses the documented threat-enabling feature (https://github.com/songquanpeng/one-api; https://github.com/QuantumNous/new-api). |
| 4 | P1 | Add **One-Token-style cheap screening** (random-number battery, ~100 single-token queries) for continuous/ambient monitoring, fused with KBF. | Lowest per-query cost; EER ~7-11% is too weak alone but good as a canary (https://arxiv.org/abs/2607.10252). |
| 5 | P1 | Add **dilution/fraction detection** (IRIS-style) as a D4 sub-probe; defer full attribution until IRIS has a released artifact. | IRIS is the only text-only dilution estimator located; unproven/young (https://arxiv.org/abs/2607.20860). |
| 6 | P1 | Implement **probe rotation / renewal**: regenerate private probe sets per audit; never rely on published sets for decisive verdicts. | Countermeasure to probe special-casing and GhostPrint-style spoofing (KBF README; https://arxiv.org/abs/2606.16100). |
| 7 | P2 | Add optional **D8 SWE-bench Lite (sampled)** capability-preservation probe, cost-capped and opt-in only. | Capability is a spoof-resistant corroborating signal (https://swebench.com/; https://github.com/SWE-bench/SWE-bench). |
| 8 | P2 | Add a **local one-api/new-api test fixture** (Docker) for deterministic relay-adversary scenarios. | Validates probes against the reference adversary deployment. |
| 9 | P2 | Optionally mirror **EAI-style** self-hosted-reference accuracy benchmarking for high-value models. | Provides the accuracy-preservation axis (https://artificialanalysis.ai/articles/endpoint-accuracy-index). |
| 10 | P2 | Document the **provenance evidence tier** (T0-T3, Section 2.2) in report output so users never read behavioral verdicts as cryptographic proof. | Keeps claims honest per Section 2. |

---

## 12. Source register (all accessed 2026-08-06)

| ID | Source | URL |
|---|---|---|
| S1 | LLMmap abstract (USENIX Security 25) | https://arxiv.org/abs/2407.15847 |
| S2 | LLMmap full text (v4) | https://arxiv.org/html/2407.15847v4 |
| S3 | LLMmap repository (MIT) | https://github.com/pasquini-dario/LLMmap |
| S4 | TRAP / BBIV (ACL 2024 Findings) | https://arxiv.org/abs/2402.12991 |
| S5 | KBF abstract + full text (v2) | https://arxiv.org/abs/2605.29524 |
| S6 | KBF full text (v2, HTML) | https://arxiv.org/html/2605.29524v2 |
| S7 | KBF repository (Apache-2.0) | https://github.com/Ooo0ption/KBF |
| S8 | IRIS (v1) | https://arxiv.org/abs/2607.20860 |
| S9 | One Token Is Enough (v1) | https://arxiv.org/abs/2607.10252 |
| S10 | GhostPrint / fingerprint spoofing (v1) | https://arxiv.org/abs/2606.16100 |
| S11 | Artificial Analysis homepage | https://artificialanalysis.ai/ |
| S12 | Artificial Analysis EAI launch article | https://artificialanalysis.ai/articles/endpoint-accuracy-index |
| S13 | LMArena leaderboard | https://lmarena.ai/leaderboard |
| S14 | OpenRouter provider routing docs | https://openrouter.ai/docs/guides/routing/provider-selection |
| S15 | OpenRouter router metadata docs | https://openrouter.ai/docs/guides/features/router-metadata |
| S16 | OpenRouter principles | https://openrouter.ai/docs/guides/overview/principles |
| S17 | one-api repository (MIT) | https://github.com/songquanpeng/one-api |
| S18 | new-api repository (AGPL-3.0) | https://github.com/QuantumNous/new-api |
| S19 | SWE-bench leaderboards / splits | https://swebench.com/ |
| S20 | SWE-bench repository (MIT) | https://github.com/SWE-bench/SWE-bench |

*Note:* `github.com/swe-bench/SWE-bench-OSS` and `github.com/SWE-bench/SWE-bench-OSS`
returned HTTP 404 on 2026-08-06 and are deliberately excluded from the register as
unverifiable.

---

*End of research landscape. Documentation only; no project files were modified.*
