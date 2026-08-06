# Supplier Admission Evaluation Tool — Build Plan

*Exported from the Notion build plan on 6 Aug 2026. §1–10 mirror the page; §11–14 are implementation-level detail added in this export.*

> 🎯 **Supplier Gate (`supgate`)** — a personal, black-box admission tool that probes any OpenAI-compatible endpoint (upstream supplier, relay gateway, white-label domain) and produces a scored, evidence-backed report: protocol compliance, performance, relay tampering, capability contracts, model authenticity. Reference shape: report MOK-20260806-RKGX.

## 1. Why

- The chain (gateway → white-label partners → customers) is exposed end-to-end if an upstream supplier silently substitutes, mixes, or degrades models.
- **Core driver:** the supplier market is flooded with dirt-cheap "official model" endpoints. At those prices the economics usually only work by serving distilled/quantized/older substitutes or mixing cheaper models behind the claimed name. Admission testing is the defense before wiring any supplier into production.
- Four uses:
  - **Supplier admission** — score a candidate upstream/channel supplier before it enters production.
  - **Ongoing assurance** — scheduled reruns against production endpoints (natural extension of the Daily Model Health Monitor).
  - **Partner trust & SLA evidence** — attach reports to commercial docs (assurance level, goodput vs SLA thresholds).
  - **QA ammunition** — failed probes convert directly into the numbered-list QA issue format for the gateway.

## 2. Test Domains

| Domain | Measures | How |
| --- | --- | --- |
| **D2 Engineering performance** | Latency & throughput under load | TTFT/TPOT/ITL/E2E percentiles per input band (<8K, 8–16K, 16–32K) at concurrency 10; goodput vs SLA thresholds (TTFT≤5s, TPOT≤500ms, E2E≤60s); long-context needle verbatim recall (catches silent context compression) |
| **D4 Relay fulfillment (反代履约)** | Relay/reseller tampering with identity, model, or billing | Billing recount (local tokenizer vs reported usage), prompt-wrap offset, canary echo, vendor self-report, model alias/echo, response-id prefix & header fingerprints, origin classification, router/number-pool rotation sampling, transit hop lower bound |
| **D6 Protocol compliance** | Faithfulness to the OpenAI API contract | Chat non-stream + SSE, messages array shapes, JSON mode, tool_choice passthrough, vision, param boundaries, max_tokens, usage completeness, idempotency, /v1/models, Responses API |
| **D8 Tool & capability contract** | Agent-critical capabilities actually work | GPT/Claude tool calling (auto/forced/required/parallel/multiturn/stream), structured output (JSON mode + strict), reasoning models, prompt caching, knowledge-cutoff baseline via synthetic facts |
| **platform (unscored)** | Harness self-check | Error contract; no internal fingerprint leakage |

**Design rules (adopted from the reference):**

- **Relaying is neutral; tampering is penalized.** Hop count = unscored lower bound (≥1). Veto only on reverse identity, substitution, billing inflation, hidden origin.
- **Evidence-first:** every failed probe ships a redacted reproducible curl.
- **Dual output:** probe detail for engineers; Supply Assurance Level (A/B/C/Disqualified) for procurement. Black-box caps at B — A needs supplier credentials.
- **Skipped ≠ failed:** probes skip cleanly when the prerequisite API surface is absent, capped so skips can't zero a domain.
- **429 ≠ proven absence:** rate-limit-blocked probes score Warn, then rerun with backoff/higher-quota key before a final Fail. Either way it fails admission — but the verdict must say why.

## 3. Authenticity Signals (strongest → weakest)

Black-box probes strongly evidence substitution/tampering but never cryptographically prove distillation (good distills mimic surface behavior; providers legitimately update, quantize, A/B). Output is always a **calibrated verdict with confidence** — consistent / suspected substitution / confirmed tampering — never a binary "distilled: yes/no". "Suspected substitution" requires **2 independent signal families**.

1. **Protocol/fingerprint mismatches** — non-OpenAI model returning `chatcmpl-` ids, stripped official headers, missing `id`/`model`: near-conclusive for a relay shell, silent about the weights.
2. **Knowledge-boundary probes** — synthetic/dated facts near the claimed cutoff; distilled/older/smaller substitutes show a measurably different boundary. KBF flagged all 155 substitutions across 16 production endpoints; detects 5–10% mixed routing.
3. **Behavioral/statistical fingerprints** — per-model random-number distributions, lexical patterns; LLMmap identifies 42 model versions at >95% accuracy in ~8 queries; logprob tracking where exposed.
4. **Capability contracts** — wholesale failure of tools/structured output/reasoning = substitution, or a relay so lossy it's commercially equivalent.
5. **Billing forensics** — local token recount vs reported `usage` catches inflation and hidden prompt wrappers (constant offsets).
6. **Needle recall** — low verbatim recall of a long-context needle → silent truncation/compression.

## 4. Product Definition

- **Input:** endpoint base URL, API key, claimed model name(s), mode (`adhoc` quick scan / `full` profile), optional client SLA thresholds.
- **Output:** JSON result bundle + rendered report (HTML → PDF per the PDF Export Style Guide) — overall score, assurance level + basis, domain table, per-probe detail with redacted reproducible curls, transit topology, methodology appendix.
- **Users:** Rizky (QA/product), the reviewer (commercial evidence); later white-label partners as a service.

## 5. Architecture

```text
CLI / trigger
  └─ Orchestrator (probe scheduler, concurrency semaphore ≤50, timeout budget)
       ├─ Probe Registry (each probe = id, domain, weight, run(), evidence(), skip rules)
       │    ├─ D2 load probes (async HTTP, streaming timers)
       │    ├─ D4 fingerprint/billing probes (+ local tokenizer)
       │    ├─ D6 protocol probes
       │    └─ D8 capability probes (GPT + Claude suites)
       ├─ Evidence Store (raw req/resp, redacted; per-probe curl repro)
       ├─ Scoring Engine (domain weights → overall; veto rules → assurance level)
       └─ Report Renderer (JSON → HTML → PDF)
```

- Timing starts **after** the local semaphore is acquired — load-host queueing never pollutes latency percentiles.
- TPOT/ITL exclude the first token (vLLM bench-serve convention, not LLMPerf).
- Keys redacted everywhere (`sk_l****5765` style); evidence stores redacted req/resp only; reports reference keys, never print them.
- **Stack:** Python 3.12 · `httpx` (async, streaming timers) · `tiktoken`/HF tokenizers for recount · `pydantic` result models · Jinja2 HTML → headless Chromium PDF · SQLite run history · YAML probe manifests so new probes register without code changes. (Reference tool is Go; Python builds faster for us and is fine at ≤50 concurrency.)

## 6. Probe Catalog v1 (build order)

1. **P0 foundation:** dummy echo, `/v1/models`, auth/error contract.
2. **D6 core:** chat basic ×3, SSE ×2, messages array shapes, max_tokens, param boundaries, usage fields, idempotency, JSON mode, tool passthrough, vision, Responses API (skip-aware).
3. **D4 fingerprints:** header capture & official-header diff, id-prefix classifier (`chatcmpl-`, `msg_`, vendor-specific), model echo/alias, canary echo, vendor self-report, SSE shape, usage wrap.
4. **D4 billing:** usage presence, local recount deviation, prompt-wrap offset, reasoning-token check, cache fields.
5. **D2 load:** 3 input bands × concurrency 10, TTFT/TPOT/ITL/E2E P50/P90, goodput vs SLA, needle recall.
6. **D8 capabilities:** GPT tool suite (auto/forced/required/parallel/multiturn/stream), JSON mode + strict structured output, reasoning probe, knowledge-cutoff synthetic-fact battery; Claude Messages suite with skip rules.
7. **v1.1 authenticity upgrades:** random-number distribution fingerprint vs reference model, logprob tracking when exposed, LLMmap-style 8-query classifier, KBF-style knowledge-boundary battery per claimed model family, mixed-routing detection via repeated sampling.

## 7. Scoring & Verdicts

- Per-probe: 0–100 (Pass=100, Warn=partial, Fail=0); N successes / attempts recorded.
- Domain score: weighted mean of scored probes (platform self-check and transit depth stay unscored).
- Overall: **D6 30% · D4 30% · D8 25% · D2 15%**.
- **Veto layer (independent of score):** reverse identity, confirmed substitution, billing inflation, hidden origin → Disqualified regardless of score.
- **Two axes, always both reported:** quality grade (score) and Supply Assurance Level — an endpoint can be honest yet unusable, or usable yet unverified.
- Assurance mapping: **A** = supplier-credential verification passed (white-box) · **B** = stable, no reverse/mixing, capabilities OK · **C** = basically usable, identity unconfirmed · **Disqualified** = tampering or broken contract.

## 8. Milestones

| Phase | Deliverable | Verify by |
| --- | --- | --- |
| **M1 — Skeleton (wk 1)** | Orchestrator + probe registry + P0/D6-core against `api.supplier.example`; JSON output | Run vs known-good endpoint; hand-check 3 probe evidences |
| **M2 — Fingerprints & billing (wk 2)** | D4 suite + redacted curl repro | Run vs official OpenAI + one relay; confirm relay detected, official clean |
| **M3 — Load & capabilities (wk 3)** | D2 bands + D8 GPT/Claude suites, skip logic | Compare TTFT/TPOT vs manual curl timings; capability matrix matches known model capabilities |
| **M4 — Scoring + report (wk 4)** | Scoring engine, assurance levels, HTML/PDF report | Reproduce a MOK-style report end-to-end; extract PDF text and check vs JSON |
| **M5 — Authenticity v1.1 (wk 5–6)** | Cutoff battery, random-number fingerprint, logprob audit, mixed-routing sampler | Blind test: 2 endpoints, one deliberately mis-labeled model — tool must flag it |
| **M6 — Ops (wk 7)** | Scheduled report-only runs, run history, QA-issue export | One week of daily runs, spot-check alerts |

## 9. Risks & Guardrails

- **False accusations:** provider updates/quantization mimic substitution → always confidence + reproducible evidence, 2 independent signal families before "suspected substitution", re-baseline fingerprints per model version.
- **Cost & rate limits:** capability probes burn tokens and trip 429s → per-run budget caps, backoff, 429-blocked = Warn (see §2 rules).
- **Key safety:** evaluation keys are secrets — redact in all logs, evidence, and reports.
- **Legal/relationship:** probing supplier endpoints is normal acceptance testing, but reports stay internal; partner-facing versions go through commercial review.

## 10. Probe Specs — How & Why

**Gap audit (6 Aug 2026):** §1–9 lock the domains, build order, scoring, and report shape — but §6 alone is probe *names*, not contracts: no request shapes, no pass criteria, no sample counts, no tolerances. A MOK-grade report (61 evidence-backed probes) is not reproducible from names. This section is the missing layer — one row per probe: **How** (request → pass criteria) and **Why** (what a failure proves).

**Global defaults:** temperature 0 unless stated · 60s timeout · one backoff retry on 429/5xx then Warn (§2) · every probe stores redacted request/response + reproducible curl · verdicts are Pass / Warn / Fail / Skip with evidence, never bare.

### 10.1 P0 — Foundation

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `p0.echo` | Minimal chat: reply with exactly `PONG-`+nonce, max_tokens 16. Pass: 200 + nonce echoed. | Auth + request path work at all; separates "endpoint dead" from "probe failed" for everything downstream. |
| `p0.models` | GET `/v1/models`. Pass: 200 + JSON model list; record whether claimed model (or a known alias) is listed. | Surface discovery feeding all skip rules; a claimed model absent from its own catalog is an admission red flag. |
| `p0.error_contract` | Invalid key → 401; malformed body → 400; both must return an OpenAI-style error object (message/type/code). | Clients depend on error shape; nonstandard wrappers expose gateway middleware. Also self-check: no supgate fingerprint leaks into requests. |

### 10.2 D6 — Protocol compliance

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `d6.chat.basic` ×3 | 3 sequential minimal chats. Pass: 3/3 return 200, non-empty message, finish_reason `stop`. | The core function. MOK failed 0/3 — an endpoint that can't chat is unusable whatever else it does. |
| `d6.chat.sse` ×2 | `stream: true`. Pass: 2/2 well-formed SSE — `data:` frames, `[DONE]` terminator, deltas reassemble to non-empty text. | Agent UIs need real streaming; malformed SSE breaks every downstream client. |
| `d6.messages.shapes` ×3 | (a) system+user; (b) 6-turn history; (c) trailing assistant prefill. Pass: 200 + coherent reply each. | Real apps send history; relays that re-serialize message arrays mangle these first. |
| `d6.json_mode` ×2 | `response_format: json_object`, prompt demands fixed keys. Pass: parseable JSON with required keys, twice. | Structured pipelines fail silently without it. |
| `d6.tool_passthrough` ×2 | tools[] with `tool_choice` `auto` / `none`. Pass: no 4xx; `none` → plain text; `auto` → text or schema-valid tool_call. | Gateways commonly strip tool fields — instant death for agent workloads. |
| `d6.param_boundaries` ×5 | temperature 0 and 2, top_p 0.01, n=2, stop sequence. Pass: honored (n=2 → 2 choices, stop truncates) or clean 400 — never a silent clamp. | Silent clamping means middleware rewrites requests — what else does it rewrite? |
| `d6.max_tokens` ×2 | max_tokens 1 → finish_reason `length`, ≈1 completion token; absurd value → clean 400 or documented cap. | Cost-control contract; feeds billing recount. |
| `d6.usage_fields` ×2 | Non-stream + stream with `include_usage`. Pass: usage present, total = prompt + completion. | All billing auditing rests on usage being present and arithmetically consistent. |
| `d6.idempotency` ×3 | Identical prompt ×3 at temperature 0. Pass: same structure and finish_reason; completion-length variance inside calibrated bound. | Wild variance at temp 0 hints at router/pool mixing behind one model name. |
| `d6.vision` | One `image_url` part (small data URL) + "describe". Pass: correct description; clean unsupported error → Skip for non-multimodal claims. | Verifies the multimodal claim is real, not just an accepted field. |
| `d6.responses_api` | Minimal POST `/v1/responses`. Pass: valid response; unsupported → recorded, dependent probes Skip. | Newer OpenAI surface; lagging relays reveal themselves here. |

### 10.3 D4 — Relay fingerprints

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `d4.headers_diff` | Capture all response headers; diff vs official vendor baseline (recorded from official accounts). Pass: consistent with claimed origin. | Stripped or foreign headers are the cheapest relay evidence (MOK: official headers stripped/rewritten). |
| `d4.id_prefix` | Classify `response.id` prefix (`chatcmpl-`, `msg_`, vendor patterns) across 10 samples vs claimed family. | A non-OpenAI model emitting `chatcmpl-` ids = OpenAI-compat shell — the exact MOK finding. |
| `d4.model_echo` | Compare `response.model` with requested name across probes; log alias map and its stability. | Blank/unstable echo = router or rebrand; a stable alias is tolerable but recorded. |
| `d4.self_report` | Ask name/vendor/cutoff in 3 phrasings. Pass: consistent with claimed family. | Weak alone, strong in combination (§3 two-family rule); MOK self-report was unclear. |
| `d4.canary_echo` | Unique canary string must come back verbatim. Pass: exact echo. | An altered canary = middleware rewriting content in flight (MOK: altered). |
| `d4.sse_timing` | Inter-chunk timing stats on streams. Pass: organic cadence; long stall + uniform burst = buffered fake stream. | Fake streaming invalidates TTFT and implies an extra buffering hop. |
| `d4.rotation` | 20 spaced calls; sample id patterns, header jitter, latency clusters. Pass: one stable backend. | Number-pool/account rotation = resold unstable supply (MOK was clean here — key reason it escaped Disqualified). |

### 10.4 D4 — Billing forensics

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `d4.usage_presence` | usage object on every response type, including final stream chunk. | No usage = unbillable and unauditable supply. |
| `d4.recount_deviation` | Local tokenizer recount vs reported prompt_tokens across 5 prompt sizes. Pass: deviation within calibrated tolerance after tokenizer correction. | MOK showed 88% deviation — direct billing-inflation evidence, veto-eligible (§7). |
| `d4.wrap_offset` | Fixed-size prompt series; check for constant positive offset (reported − recount) across sizes. Pass: offset ≈ 0. | A constant offset (+11 in MOK) = hidden injected system prompt: tampering + inflated billing. |
| `d4.reasoning_cache_fields` | reasoning_tokens sane (≤ completion tokens, only on reasoning models); repeated prefix → cached-token fields rise with TTFT drop. | Catches fake reasoning/cache line items on invoices. |

### 10.5 D2 — Performance

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `d2.load_matrix` | Per band <8K / 8–16K / 16–32K: 20 streamed requests at concurrency 10, timers start post-semaphore. Report TTFT/TPOT/ITL/E2E P50+P90, success rate, goodput = % of requests meeting TTFT≤5s + TPOT≤500ms + E2E≤60s (client SLA overrides). Pass bar: goodput ≥80% per band (proposal). | The SLA-evidence number partners buy on; MOK served 0 tokens at 0% goodput in every band. |
| `d2.needle_recall` | ≈30K-token prompt, needle planted at 20% depth, demand verbatim echo. Pass: exact match. | Silent context truncation/compression is a classic cost-cutting trick; kills RAG and long-doc workloads. |

### 10.6 D8 — Capability contract

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `d8.tools_gpt` ×6 | auto · forced function · required · parallel (2 calls in one turn) · multiturn (tool result → final answer) · streamed tool_call deltas. Pass each: schema-valid tool_calls with correct arguments. | The capability agents live on; each mode fails independently in the wild, so each is its own probe. |
| `d8.structured_strict` ×2 | json_object, then json_schema with strict true. Pass: output validates exactly against the schema. | Strict mode is a hard, machine-checkable capability claim — substitutes often can't hold it. |
| `d8.reasoning` | Reasoning params accepted; gated multi-step task solved; reasoning-token accounting present. | Verifies the reasoning model you pay a premium for actually reasons. |
| `d8.cutoff_battery` | 12 dated real facts + 6 synthetic negatives bracketing the claimed cutoff, scored vs official family baseline. Pass: matches baseline pattern. | Cheapest strong substitution/distillation signal. MOK matched none of the 2019–2024 baselines → identity unconfirmed. |
| `d8.prompt_caching` | Long shared prefix ×3. Pass: cached-token fields populate and TTFT drops on repeats. | Cache claims are cost claims — verify before pricing on them. |
| `d8.claude_suite` | `/v1/messages`: system, tool_use blocks, streaming events. Skip cleanly (capped, §2) when the surface is absent. | Only meaningful when Claude is claimed; MOK's 10 clean skips are correct behavior. |

### 10.7 v1.1 — Authenticity upgrades

| Probe | How (request → pass) | Why (what failure proves) |
| --- | --- | --- |
| `auth.rng_fingerprint` | "Random integer 1–100, number only" ×200 at temperature 1; compare distribution vs official reference (divergence cutoff from baselines). | Each model has a stable, biased distribution — a cheap behavioral fingerprint. |
| `auth.llmmap` | ≈8 crafted queries → feature vector → nearest official model in reference bank. | LLMmap-style version ID at >95% accuracy, robust to wrappers and system prompts. |
| `auth.logprob_audit` | Where logprobs are exposed: fixed prompt battery, track top-k drift vs baseline runs. | Sharpest substitution/drift detector available black-box. |
| `auth.kbf_battery` | Knowledge-boundary recall battery per claimed model family (stable recall near the boundary). | KBF-class detection flags substitution even at 5–10% mixed routing. |
| `auth.mixed_routing` | Same prompt ×30 at temperature 0; cluster on id prefix, headers, latency, token counts, text similarity. Pass: one cluster. | Directly catches percentage-routing / A-B mixing behind one model name. |

**Calibration constants — set from baseline runs against official endpoints (M2/M5), code-level, not plan blockers:** recount tolerance per tokenizer family · wrap-offset alarm threshold · idempotency variance bound · RNG sample size and divergence cutoff · needle depth/length · goodput pass bar per band.

---

*Sections 11–14 below were added in this export — implementation scaffolding to make §1–10 directly buildable. They are not yet on the Notion page.*

## 11. Implementation Scaffold (added)

### 11.1 Repository layout

```text
supgate/
├── pyproject.toml              # Python 3.12; deps: httpx, pydantic, tiktoken, jinja2, pyyaml, typer
├── supgate/
│   ├── cli.py                  # entrypoints: run / report / baseline / history / export
│   ├── orchestrator.py         # async scheduler, semaphore ≤50, timeout + budget enforcement
│   ├── registry.py             # YAML manifests → probe instances
│   ├── probes/
│   │   ├── p0.py, d6_protocol.py, d4_fingerprint.py, d4_billing.py
│   │   └── d2_load.py, d8_capability.py, auth_v11.py
│   ├── models.py               # pydantic: ProbeResult, DomainScore, RunBundle, Assurance
│   ├── evidence.py             # redaction choke point + reproducible-curl builder
│   ├── scoring.py              # weighted means, veto rules, assurance mapping
│   ├── tokenizers.py           # tiktoken / HF recount per claimed model family
│   ├── store.py                # SQLite run history
│   └── report/                 # Jinja2 templates → HTML → headless Chromium PDF
├── manifests/probes.yaml       # probe registry: weights, samples, tolerances
├── baselines/                  # official-endpoint fingerprints per vendor/model version
└── tests/                      # unit + replay tests on recorded fixtures (no live calls)
```

### 11.2 Probe contract

```python
class Probe(Protocol):
    id: str            # e.g. "d6.json_mode"
    domain: Domain     # D2 | D4 | D6 | D8 | PLATFORM
    weight: float      # default 1.0 within its domain
    samples: int       # the ×N counts from §10

    def skip_reason(self, surface: SurfaceMap) -> str | None: ...
    async def run(self, ctx: RunContext) -> ProbeResult: ...
```

- `RunContext` — endpoint config, claimed models, shared async HTTP client, budget counter, baseline store, evidence writer.
- `ProbeResult` — verdict `pass|warn|fail|skip`, score 0–100, successes/attempts, timing samples, evidence refs, redacted curl, notes.
- `SurfaceMap` — built once by P0 (models list, Responses API, Claude Messages surface, logprobs exposure); drives all §2 skip rules.

### 11.3 YAML manifest (new probes without code changes)

```yaml
- id: d6.json_mode
  domain: D6
  weight: 1.0
  samples: 2
  runner: chat_completion          # generic runner covers most D6 probes
  request:
    response_format: { type: json_object }
    prompt: json_keys_demand
  pass: "json_parses and has_keys(['name', 'value'])"
  on_429: warn_then_retry
```

Custom-logic probes (load matrix, recount, fingerprints, RNG) register named runners implemented in `probes/`; manifests still own weight, samples, and tolerances so the §10 calibration constants stay editable without code changes.

## 12. CLI, Run Modes & Budgets (added)

```bash
supgate run --base-url https://api.supplier.example/v1 --key-env SUPGATE_KEY \
  --model gpt-4o --mode adhoc --out runs/
supgate run ... --mode full --sla "ttft=5,tpot=0.5,e2e=60" --budget-usd 25
supgate baseline record --vendor openai --model gpt-4o --key-env OPENAI_OFFICIAL_KEY
supgate report runs/SUP-20260806-XXXX.json --pdf
supgate export qa runs/SUP-20260806-XXXX.json    # numbered-list QA issue draft
supgate history --endpoint api.supplier.example
```

- **Modes:** `adhoc` = P0 + D6 core + D4 fingerprints + quick billing — minutes, minimal tokens, no load matrix. `full` = entire catalog incl. D2 bands, D8 suites, v1.1 authenticity — 30–60 min.
- **Budgets:** per-run cost cap (`--budget-usd`; suggested defaults: adhoc 5, full 25). Exhaustion → remaining probes Warn "budget-blocked", never silent skip; 429 handling per §2.
- **Keys:** env vars only (`--key-env`), never raw in argv; redaction applied at the single evidence-writer choke point (§5).
- **Exit codes:** 0 = report produced (verdicts live in the report, not the exit code) · 2 = endpoint unreachable (P0 dead) · 3 = aborted (config/budget error).

## 13. Output Contracts (added)

- **Run ID:** `SUP-YYYYMMDD-XXXX` — mirrors the MOK reference shape.
- **JSON bundle top level:** `run_id`, `endpoint`, `claimed_models[]`, `mode`, `started_at` / `finished_at`, `versions` (supgate / manifests / baselines), `sla`, `overall_score`, `domain_scores{}`, `assurance {level, basis[]}`, `vetoes[]`, `probes[]` (full §11.2 results), `transit {hop_lower_bound, origin_class}`, calibration snapshot.
- **SQLite history (M6):**

  ```sql
  runs(run_id PK, endpoint, model, mode, started_at, overall, assurance, bundle_path)
  probe_results(run_id, probe_id, verdict, score, attempts, successes, evidence_path)
  baselines(vendor, model, family, captured_at, kind, data_json)
  ```

- **QA-issue export:** one numbered item per failed probe — affected endpoint/URL, current behavior (one-line evidence + curl ref), expected behavior per the OpenAI contract — matching the locked QA submission format.

## 14. Open Decisions (added)

- Official baseline accounts (OpenAI/Anthropic keys) for fingerprint recording — owner and cost line.
- Storage home for run bundles and PDF reports (vault Reference vs QA Reports attachments).
- Default SLA thresholds per service tier (Standard/Enterprise/Official) or one global default.
- Whether M6 scheduled runs merge into the Daily Model Health Monitor or stay a separate trigger.
