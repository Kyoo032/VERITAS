# VERITAS M2 Probe Specification (D4 Fingerprint + Billing)

Status: implementation-ready draft
Milestone: M2 (D4 relay fingerprints + billing forensics)
Domain: D4 (30% of overall score)
Owners: `docs/06-m2-probe-spec.md` (this file), `docs/08-output-data-contract.md`
Scope: probe contracts only. No code, tests, README, or packaging changes are made by this document.

## Table of contents

1. Scope and principles
2. Baseline records
3. Named runner interfaces
4. Fingerprint probes
5. Billing probes
6. M2 dependency graph and build order
7. Fixture test matrix
8. Cross-cutting requirements
9. Implementation questions (resolved and open)

---

## 1. Scope and principles

### 1.1 What this document defines

Implementation-ready contracts for the eleven D4 probes that land in M2.
Every probe contract specifies: purpose, prerequisites, exact request shape,
samples, timing, local calculation, Pass/Warn/Fail/Skip semantics, tolerance
and calibration source, evidence, cost estimate, signal family, veto
eligibility, false positive risks, and fixture tests.

The contracts are written so a runner can be implemented without further
design discussion. Where a decision is genuinely open, it is listed in
Section 9 and flagged `[OPEN]` inline.

M2 milestone scope includes all eleven D4 probes specified below. The
weekend Must tier targets ten core probes: d4.headers_diff, d4.id_prefix,
d4.model_echo, d4.self_report, d4.canary_echo, d4.sse_timing, d4.rotation,
d4.usage_presence, d4.recount_deviation, and d4.wrap_offset.
d4.reasoning_cache_fields is weekend Stretch: it remains part of the M2
contract (implemented, registered, and fixture-tested) but is scheduled
after the ten core probes. M2 is not complete until all eleven probes land.

### 1.2 Identity-claim discipline (do not overclaim)

VERITAS never asserts "this endpoint is provider X". D4 probes produce
*consistency* and *inconsistency* statements relative to (a) the claimed
model list supplied on the CLI and (b) reference baseline records:

- A probe PASS means "observation consistent with the claim".
- A probe FAIL means "observation materially inconsistent with the claim".
- No single probe, and no combination of self-report text, id prefix, or
  timing values, is treated as proof of model identity. Models can be
  instructed to lie about themselves; relays can rewrite ids, headers, and
  usage fields.
- Baseline records are reference fingerprints for structural comparison, not
  identity ground truth.
- Report language must use "consistent with / inconsistent with", never "is".
  Example note: "id prefix chatcmpl- is consistent with the OpenAI-style
  chat completions contract" -- NOT "endpoint is OpenAI".

### 1.3 Verdict semantics for M2 probes

The four-verdict enum from `supgate/models.py` (`Verdict`:
pass/warn/fail/skip) is unchanged. M2 adds per-probe meaning:

- PASS: all checks within tolerance; observation consistent with claim.
- WARN: observation is degraded, ambiguous, or partially inconsistent; the
  endpoint still responded. WARN never silently hides a defect.
- FAIL: material inconsistency or tamper/billing signal reproducible across
  samples.
- SKIP: a prerequisite is absent (missing surface, missing baseline when the
  probe is baseline-gated, claimed model family unknown to the tokenizer, or
  adhoc-mode exclusion). SKIP never lowers a domain score.

HTTP 429/5xx that persist after exactly one local backoff retry map to WARN
per the existing `supgate/probes/base.py` policy (`RateLimitError`,
`ServerError`, `warn_result`). Transport errors (no HTTP response: connection
refused, DNS failure, read timeout) stay FAIL: no response was received, so
the probe can neither pass nor warn, and the failed attempt's evidence is
saved. M2 probes reuse `request_with_retry` and `probe_result_with_warn`
unchanged; the verdict routing above is the only policy change.

Score mapping (unchanged from M1): PASS=100, WARN=50 (partial probes use
successes/attempts), FAIL=0, SKIP=0 and excluded from scoring.

### 1.4 Signal families

| Family | Probes | What it asserts |
| --- | --- | --- |
| identity_consistency | d4.headers_diff, d4.id_prefix, d4.self_report | observed relay surface is stable and consistent with the claim |
| generation_integrity | d4.model_echo, d4.canary_echo | outputs are genuinely generated and not templated/leaky |
| relay_timing | d4.sse_timing, d4.rotation | streaming and multi-request behavior matches one stable upstream path |
| billing_transparency | d4.usage_presence, d4.recount_deviation, d4.wrap_offset, d4.reasoning_cache_fields | reported usage is present, arithmetically sane, and not inflated |

### 1.5 Veto eligibility

There are exactly four veto codes (see `docs/08-output-data-contract.md`):
`reverse_identity`, `substitution`, `billing_inflation`, `hidden_origin`. A
veto immediately sets assurance to `Disqualified`. `tamper` is NOT a veto
code: it remains an evidence/authenticity label produced by d4.canary_echo
(`template` or `asymmetry`), recorded in the bundle `authenticity` object
(`verdict: confirmed_tampering`) and the assurance basis, and it always
requires corroboration before it is asserted.

- Veto-capable alone (single probe enough when the threshold is passed):
  - d4.recount_deviation -> billing_inflation (mean over-report >= 15%),
    independently confirmed by the calibrated multi-size recount: the
    deviation must exceed the FAIL gate across all prompt sizes
    (short/medium/long), not a single size or sample, and stay above the
    baseline-calibrated gate (baseline mean + 8*std, min 15%).
- Veto-capable only as corroborated signal (probe + at least one independent
  observation):
  - d4.rotation -> substitution (>= 3 distinct upstream families mapping to
    different official providers). Two-cluster rotation (F == 2) is evidence
    toward `suspected_substitution`, never a verdict or veto by itself. A
    second independent signal family must corroborate it before the report
    sets `authenticity.verdict: suspected_substitution`; this identity rule is
    separate from `billing_inflation` confirmation.
  - d4.headers_diff + d4.self_report -> hidden_origin (hop headers present
    AND self-report contradicts claim).
  - d4.wrap_offset + d4.recount_deviation -> hidden_origin (constant large
    prompt offset alongside confirmed over-reporting).
  - d4.id_prefix + d4.self_report -> reverse_identity (prefix of a different
    official family AND self-report agrees with that family).

The veto wiring lives in the orchestrator (`_vetoes`, currently reserved
empty). M2 fills it from the corroboration table above. None of
d4.model_echo, d4.canary_echo, d4.usage_presence, d4.sse_timing,
d4.reasoning_cache_fields may veto by themselves; they only add notes,
feed the `authenticity` object, and lower the domain score.

### 1.6 Calibration and tolerance sources

M2 tolerances come from one of three sources, in priority order:

1. Baseline record matched to the claimed model (preferred; recorded by the
   `baseline` command against an official endpoint before adversary runs).
2. In-run calibration (baseline-gated probes fall back to structural
   consistency across samples when no baseline exists).
3. Static defaults listed per probe. Static defaults are deliberately loose;
   they only catch gross anomalies. Every tolerance must be revisited when
   the first official baseline is recorded (Section 4.6).

---

## 2. Baseline records

The `supgate baseline` CLI became operational in M2. It records
official-endpoint fingerprints used as calibration source by D4
probes. The baseline record format below is schema v2 and supersedes the M1
build-plan section 13 scaffold; readers accept v1 baselines, writers emit v2.

### 2.1 Baseline record format

A baseline is a JSON file written to `baselines/` (or `--out`). Full contract
in `docs/08-output-data-contract.md`. Shape:

```json
{
  "schema": 2,
  "baseline_id": "BL-OPENAI-GPT-4O-0001",
  "provider_label": "openai",
  "claimed_models": ["gpt-4o"],
  "captured_at": "2026-08-06T00:00:00+00:00",
  "surface": {
    "models_catalog": 200,
    "responses_api": true,
    "messages_api": true,
    "claimed_present": true
  },
  "fingerprints": {
    "id_prefix": { "family": "chatcmpl", "samples": 12, "consistent": true },
    "headers_stable_set": ["server", "content-type", "date"],
    "sse_ttft_ms": { "median": 900.0, "p90": 1600.0, "n": 9 },
    "sse_inter_chunk_ms": { "median": 40.0, "p90": 120.0, "n": 120 },
    "recount_deviation_pct": { "mean": 1.2, "std": 3.4, "n": 9 },
    "wrap_offset_tokens": { "mean": 3.1, "std": 1.2, "n": 12 },
    "usage_schema": { "cached_tokens": true, "reasoning_tokens": false }
  },
  "notes": []
}
```

### 2.2 Fields

- `provider_label`: free-text reference label only (openai, anthropic,
  generic). It is metadata about the baseline run, NOT an assertion about any
  later tested endpoint.
- `fingerprints`: per-signal reference statistics. Each entry carries its own
  sample count (`n`) so consumers know how much confidence to place in it.
- `surface`: snapshot of the discovered API surface at baseline time.

### 2.3 Capture procedure

- Run `supgate baseline record --vendor <vendor> --model <model> --endpoint
  <official-url> --key-env <fresh-env-name> --out baselines`.
- The baseline recorder captures the reference exchanges and folds their
  measurements into `fingerprints`.
- `provider_label` is supplied by the operator; it is never inferred.
- Every baseline run must have `p0.echo == pass`; otherwise it is rejected.

### 2.4 Matching rules

- A probe looks up baselines by `claimed_models` (exact or family-prefix
  match, e.g. `gpt-4o` vs `gpt-4o-*`).
- If no baseline matches, probes that are baseline-gated either SKIP (with
  the note "no baseline") or fall back to structural checks per probe
  contract. Fallback behavior is declared per probe in Sections 6 and 7.
- Only the newest matching baseline (highest `captured_at`) is used.

### 2.5 Storage

`baselines/<baseline_id>.json`. A run bundle records which baseline was used
in `versions.baselines` and `baseline` (target schema, see
`docs/08-output-data-contract.md`).

### 2.6 Tolerance adoption rule

Static tolerances in this spec are provisional. On the first recorded
baseline, the thresholds for d4.id_prefix, d4.sse_timing, d4.recount_deviation,
d4.wrap_offset, and d4.rotation must be re-derived from the baseline
distribution (mean + k*std, k per probe) and this document updated.

---

## 3. Named runner interfaces

### 3.1 Registration

Probes are manifest-driven. Each M2 probe gets a `runner` name and a custom
class registered in `supgate/registry.py` `CUSTOM_RUNNERS`, mirroring the
D6 pattern (`SseProbe`, `UsageFieldsProbe`, ...). The D4 stub modules
`supgate/probes/d4_fingerprint.py` and `supgate/probes/d4_billing.py` are
replaced by real implementations; `ProbeStub` classes are removed.

Manifest entry template (one per probe, exact values in Sections 6-7):

```yaml
  - id: d4.headers_diff
    domain: D4
    weight: 1.0
    samples: 2
    runner: d4.headers_diff
```

All M2 probes are custom runners; none use the generic `chat_completion`
path, because each needs bespoke multi-request or tokenizer logic.

### 3.2 Probe contract (existing)

Every probe implements the `Probe` protocol from
`supgate/probes/base.py`:

```python
class Probe(Protocol):
    id: str
    domain: Domain
    weight: float
    samples: int

    def skip_reason(self, surface: SurfaceMap) -> str | None: ...
    async def run(self, ctx: RunContext) -> ProbeResult: ...
```

### 3.3 RunContext streaming extension `[OPEN]`

d4.sse_timing needs per-chunk arrival timestamps. The current
`RunContext.request` buffers the full response (`response.text`) and cannot
observe chunk timing. M2 adds a streaming entry point:

```python
@dataclass
class StreamedEvent:
    delta: str          # concatenated content deltas so far at this event
    arrived_ms: float   # client-side monotonic arrival timestamp
    usage: dict | None  # usage block when present (include_usage)

class RunContext:
    async def stream(self, probe_id, path, *, payload, headers=None,
                     timeout_s=60.0) -> AsyncIterator[StreamedEvent]: ...
```

Contract:

- `stream` yields one `StreamedEvent` per received SSE `data:` payload.
- Arrival timestamps are `time.perf_counter()` based, captured at read time.
- Evidence capture happens once, at stream end, using the buffered text
  (identical to today's `_decode_body` text path). Chunk-level timing is
  stored only in the probe's `metrics`, not in evidence bodies.
- `stream` applies the same retry policy (one backoff retry on 429/5xx) via
  `request_with_retry` semantics.
- `[DONE]` is not yielded; presence is implied by iterator exhaustion.

This is the single dependency that forces changes outside probe files
(evidence path is untouched; `RunContext` gains one method). Implement first.

### 3.4 BaselineAware protocol

Probes that are calibration-gated expose baseline hooks:

```python
class BaselineAware(Protocol):
    def baseline_keys(self) -> list[str]:
        """Fingerprint keys this probe reads from a BaselineRecord."""

    def apply_baseline(self, baseline: dict) -> None:
        """Set tolerances from the matched baseline before run()."""
```

The orchestrator injects the matched baseline (Section 2.4) before calling
`run`. Probes without a matched baseline keep static defaults and record the
fallback in notes.

### 3.5 TokenizerService

Recount and offset math must not depend on a hardcoded encoding. Inject a
small service (wrapped tiktoken in M2):

```python
class TokenizerService(Protocol):
    def resolve_encoding(self, model: str) -> str | None:
        """Return tiktoken encoding name for the claimed model, or None."""

    def count(self, text: str, encoding: str) -> int: ...
```

- `resolve_encoding` matches the most-specific prefix first: `gpt-4o*`,
  `o1*`, `o3*`, `o4*` -> `o200k_base`; `gpt-4*`, `gpt-3.5*` ->
  `cl100k_base`;
  `text-*`, `davinci`, `curie` -> `cl100k_base` (legacy). Unknown models
  return `None` and the probe SKIPs with "unknown encoding for model".
  Implementations must use longest-prefix or explicit-priority matching so
  `gpt-4o*` never falls through to the broader `gpt-4*` rule.
- Counts operate on the request/response *text* only. Reported
  `prompt_tokens` includes chat-template overhead and, where present, image
  tokens; that overhead is exactly what wrap_offset measures. Recount must
  therefore report both the raw text count and the delta, never assert an
  exact equality against reported values (see tolerance sections).
- A hardcoded best-effort fallback table is acceptable before tiktoken is
  vendored, but the interface above is the contract.

### 3.6 Metrics output

Each M2 probe returns its computed values in a new `ProbeResult.metrics`
dict (target schema; additive, optional field). Example:

```json
{
  "probe_id": "d4.recount_deviation",
  "metrics": {
    "deviation_pct": 12.4,
    "reported_prompt_tokens": 120,
    "recounted_prompt_tokens": 105,
    "encoding": "o200k_base"
  }
}
```

Metrics feed baseline capture (Section 2.3) and QA exports.

---

## 4. Fingerprint probes

> Each probe below: purpose, prerequisites, request shape, samples, timing,
> local calculation, verdicts, tolerance/calibration, evidence, cost, signal
> family, veto eligibility, false positive risks, fixture tests.

---

### 4.1 d4.headers_diff

- **Runner:** `d4.headers_diff` -> `HeadersDiffProbe`
- **Weight:** 1.0 | **Samples:** 2 (logical pairs) | **Signal family:** identity_consistency
- **Purpose:** Detect whether a relay/hop sits between the client and the
  generation backend. A single-origin endpoint returns a stable header set
  across identical requests; a gateway inserts or rewrites hop indicators
  (`via`, `x-served-by`, `x-cache`, `x-upstream`, `server`, `cf-*`) and may
  vary them across requests.
- **Prerequisites:** p0.echo passed. Two pairs => 4 HTTP calls. The manifest
  `samples` value is logical samples (pairs); the runner owns the per-sample
  call multiplier, so this probe issues 4 physical calls.
- **Request shape (identical for every call in the probe):**

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "Reply with the single word ping."}],
  "max_tokens": 8,
  "temperature": 0
}
```

- **Timing:** 4 sequential or concurrent calls, timeout 60s each, ~1-2s total.
- **Local calculation:**
  1. For each response, lower-case header names; drop volatile headers:
     `date`, `content-length`, `x-request-id`, `request-id`, `x-ratelimit-*`,
     `connection`, `keep-alive`, `transfer-encoding`.
  2. Compute the stable set `S_i` per response. Compare `S_0` vs `S_1` (pair 1)
     and `S_2` vs `S_3` (pair 2).
  3. Hop markers `H = {via, x-served-by, x-upstream, x-cache, x-cache-status, x-proxy, x-forwarded-for, x-real-ip, cf-ray, cf-cache-status, server, x-powered-by}`: record presence and value per response.
  4. `pair_stable = (S_0 == S_1)`, same for pair 2.
- **Verdicts:**
  - PASS: both pairs stable AND no hop marker present in any response.
  - WARN: pairs stable but hop markers present (a proxy is visible but
    consistent), or single-pair instability.
  - FAIL: cross-request header-set instability in more than one pair (same
    endpoint, same request -> different header surface).
  - SKIP: p0.echo not pass.
- **Tolerance/calibration:** volatile-header exclusion list is the only
  tolerance. `server` and `cf-*` values may legitimately differ between
  requests behind load balancers; WARN-not-FAIL on value drift, FAIL only on
  set membership instability.
- **Evidence:** all 4 exchanges via `ctx.request` (redacted, with curl).
  Also save one `metrics.headers` summary (per-response stable set and hop
  markers).
- **Cost estimate:** 4 calls x ~23 tokens roundtrip ~ 92 tokens; well under
  $0.001 at the naive blended rate; negligible with the M2 pricing table.
- **Veto eligibility:** corroborated signal for `hidden_origin` when hop
  markers AND d4.self_report contradict the claim (Section 1.5). Never alone.
- **False positive risks:** CDN/WAF in front of a legitimate endpoint;
  multi-region load balancing; provider header churn over time (baseline is
  stale). Mitigate with the volatile-header list and pair-based (not
  cross-run) comparison.
- **Fixture tests** (`tests/fake_server.py` additions): toggles
  `hop_headers: list[str]`, `header_jitter: bool`.
  1. default server (no hop headers) -> PASS, both pairs stable.
  2. `hop_headers=["via", "x-served-by"]` consistently -> WARN.
  3. `header_jitter=True` (add/remove `x-extra` across requests) -> FAIL.
  4. force_429 -> WARN (reuse existing policy).

---

### 4.2 d4.id_prefix

- **Runner:** `d4.id_prefix` -> `IdPrefixProbe`
- **Weight:** 1.0 | **Samples:** 10 | **Signal family:** identity_consistency
- **Purpose:** Verify response id prefixes are (a) self-consistent across
  samples and (b) consistent with the claimed contract family and any
  baseline. A relay either forwards upstream ids unchanged (leaking a
  different origin) or fabricates ids (which often look inconsistent or
  generic).
- **Prerequisites:** p0.echo passed. Baseline optional; without a baseline the
  probe checks self-consistency only.
- **Request shape (10 calls):**

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "Say the word ping."}],
  "max_tokens": 8,
  "temperature": 0
}
```

- **Timing:** 10 calls, ~2-3s total.
- **Local calculation:**
  1. Extract `response.id` (chat completions) or `id` from the SSE first
     chunk (when streamed). Prefix = leading `[A-Za-z0-9_-]+` up to the first
     non-id character, or the full known family token
     (`chatcmpl`, `chatcmpl-`, `msg_`, `resp_`, `gen-`, `anthropic`, ...).
  2. `families = set(prefixes)`.
  3. If baseline exists: `baseline_family` = baseline
     `fingerprints.id_prefix.family`; `family_match = any(p == baseline_family for p in families)`.
  4. `claimed_family` = family implied by the claimed model contract when
     known (OpenAI chat -> `chatcmpl`; Responses -> `resp_`/`msg_`).
- **Verdicts:**
  - PASS: `len(families) == 1` AND (no baseline OR `family_match`) AND
    (no claimed_family mapping OR the single family is consistent).
  - WARN: `len(families) == 1` but the single family is not an official
    family (generic/custom prefix) -- note "custom id prefix"; or baseline
    exists but `family_match` is false.
  - FAIL: `len(families) > 1` (id family rotates across identical requests).
  - SKIP: p0.echo not pass.
- **Tolerance/calibration:** self-consistency is strict (any second family =
  FAIL). Baseline matching is exact-prefix only. The mapping table for
  `claimed_family` is a static, short, explicitly "not identity proof"
  reference list; unknown claimed models skip the claimed-family check.
- **Evidence:** 10 exchanges + `metrics.id_prefix` (prefixes list, families,
  baseline_family, family_match).
- **Cost estimate:** 10 x ~23 tokens ~ 230 tokens; < $0.002.
- **Veto eligibility:** corroborated signal for `reverse_identity` (prefix of
  a different official family + d4.self_report agrees) and `substitution`
  (id family rotates). Never alone.
- **False positive risks:** gateways that imitate `chatcmpl-` for all
  providers (prefix then leaks nothing); providers with per-region prefixes;
  Responses-vs-Chat endpoints returning different id families (that is a
  real rotation signal, but check the request path first).
- **Fixture tests:** fake server `id_prefix` (already present) and
  `id_prefix_jitter: bool`.
  1. stable `chatcmpl-` -> PASS.
  2. `id_prefix="gen-"` with claimed `gpt-4o` and no baseline -> WARN
     (custom prefix note).
  3. `id_prefix_jitter=True` (alternate `chatcmpl-`/`gen-`) -> FAIL.
  4. baseline present with family `chatcmpl`, server stable `chatcmpl-` ->
     PASS; server `gen-` with matching baseline `gen-` -> PASS (baseline
     overrides claimed mapping).
  5. reverse-identity baseline injection: baseline family `gen-` for claimed
     `gpt-4o`, server returns stable `gen-` prefixes, and `self_report_text`
     names the `gen-` family -> fixture asserting the `reverse_identity`
     veto corroboration (d4.id_prefix + d4.self_report agree on a different
     official family).

---

### 4.3 d4.model_echo

- **Runner:** `d4.model_echo` -> `ModelEchoProbe`
- **Weight:** 0.5 | **Samples:** 2 | **Signal family:** generation_integrity
- **Purpose:** Collect the model's self-reported identity string. Weak,
  corroborating-only signal: output is neither authoritative nor tamper-proof
  (models lie, relays rewrite). Weighted low so it cannot move a domain score
  by itself.
- **Prerequisites:** p0.echo passed. No baseline required.
- **Request shape (2 calls):**

```json
{
  "model": "{model}",
  "messages": [
    {"role": "user", "content": "Reply with exactly the model identifier that is running this conversation. Nothing else. No punctuation."}
  ],
  "max_tokens": 24,
  "temperature": 0
}
```

- **Timing:** 2 calls, ~1s total.
- **Local calculation:**
  1. `echo = content.strip().lower()`, strip trailing punctuation.
  2. Normalize claimed names: each `claimed_models` entry lower-cased,
     aliases removed (e.g. `gpt-4o` also matches `gpt-4o-2024-08-06`).
  3. `match = any(c == echo or c in echo or echo in c for c in normalized_claimed)`.
  4. `contradicts = any(c2 != c1 and c2 in echo for c1 in normalized_claimed for c2 in ALL_KNOWN_FAMILIES if c2 not in normalized_claimed)`.
- **Verdicts:**
  - PASS: `match` in >= 1 of 2 samples and no `contradicts`.
  - WARN: no `match` (self-report names a different/unrecognized model) OR
    `contradicts` OR echo empty in >= 1 sample.
  - FAIL: **never alone.** This probe is structurally capped at WARN; it
    cannot FAIL. (See Section 1.2: self-report is never identity proof, so a
    mismatch is evidence for the notes, not a disqualifying signal.)
  - SKIP: p0.echo not pass.
- **Tolerance/calibration:** substring matching is loose on purpose. No
  baseline needed.
- **Evidence:** 2 exchanges + `metrics.model_echo` (raw echoes, claimed list,
  match flags).
- **Cost estimate:** 2 x ~50 tokens ~ 100 tokens; < $0.001.
- **Veto eligibility:** none.
- **False positive risks:** many models cannot name themselves; providers
  disable identity questions; rewrites by gateways; alias drift. All handled
  by capping at WARN and low weight.
- **Fixture tests:** fake server `model_echo: str | None`.
  1. `model_echo="gpt-4o"` -> PASS.
  2. `model_echo="some-other-model"` -> WARN with mismatch note.
  3. `model_echo=None` (generic reply) -> WARN.
  4. Assert no FAIL path exists (test documents the cap).

---

### 4.4 d4.self_report

- **Runner:** `d4.self_report` -> `SelfReportProbe`
- **Weight:** 1.0 | **Samples:** 2 | **Signal family:** identity_consistency
- **Purpose:** Two checks. (a) Ask the endpoint to describe its platform and
  compare against the claimed setup. (b) Compare `/models` catalog metadata
  (owner fields, extra attributes, ordering) against the baseline. A relay
  exposes either a different `/models` shape or a self-report that drifts
  from the claim.
- **Prerequisites:** p0.echo and p0.models passed (SurfaceMap populated).
  Baseline optional (structural checks always run).
- **Request shapes:**
  - (a) chat self-report (2 calls):

```json
{
  "model": "{model}",
  "messages": [
    {"role": "user", "content": "Describe in one sentence which hosting platform or provider API is serving this request."}
  ],
  "max_tokens": 48,
  "temperature": 0
}
```

  - (b) `GET /models` (reuses surface; no extra call -- compare against the
    SurfaceMap already captured by p0.models).
- **Timing:** 2 calls, ~1s total.
- **Local calculation:**
  1. `text = content.strip().lower()` per call.
  2. If baseline exists: `report_consistent = baseline.self_report` terms
     overlap `text` on any of: provider label tokens, "hosted on", model
     family names. Record `report_match`.
  3. `surface_drift`: compare current `/models` response metadata against
     baseline `surface` (count delta > 30% is a drift flag; `claimed_present`
     flipped is a hard flag).
- **Verdicts:**
  - PASS: `report_match` (when baseline present) AND no `surface_drift`
    hard flag.
  - WARN: no baseline (structural-only run) but self-report is stable across
    the 2 calls; or `report_match` false with a baseline; or mild catalog
    drift.
  - FAIL: self-report contradicts the claim across both samples AND
    `surface_drift` hard flag (`claimed_present` flipped). Requires both;
    never fails on text alone.
  - SKIP: p0.models failed.
- **Tolerance/calibration:** baseline token overlap is the tolerance. Without
  a baseline the probe cannot FAIL (consistent with the identity-claim
  discipline).
- **Evidence:** 2 exchanges + `metrics.self_report` (texts, report_match,
  surface_drift flags).
- **Cost estimate:** 2 x ~60 tokens ~ 120 tokens; < $0.001.
- **Veto eligibility:** corroborated signal for `hidden_origin` (with
  d4.headers_diff) and `reverse_identity` (with d4.id_prefix).
- **False positive risks:** provider marketing text varies; `/models`
  metadata is often empty/uninformative; gateways copy official catalog
  shapes. Corroboration rules keep these from vetoing.
- **Fixture tests:** fake server `self_report_text: str`,
  `models_metadata_drift: bool`.
  1. matching report + stable surface -> PASS.
  2. `self_report_text="other platform"` with baseline -> WARN.
  3. with baseline: `self_report_text="other platform"` AND
     `models_metadata_drift=True` (drop claimed model from `/models`) -> FAIL.
  4. no baseline + any report -> WARN (never FAIL).

---

### 4.5 d4.canary_echo

- **Runner:** `d4.canary_echo` -> `CanaryEchoProbe`
- **Weight:** 1.0 | **Samples:** 2 (two request pairs) | **Signal family:** generation_integrity
- **Purpose:** Detect output templating and cross-request contamination.
  Two independent checks:
  1. Exact echo of a high-entropy canary. A genuine generative path repeats
     it; a canned/templated responder cannot.
  2. Cross-request contamination: request A embeds canary alpha, request B
     embeds canary beta; if B's output contains alpha, the backend is
     leaking cached/templated responses across requests.
  3. Asymmetry check: p0.echo (short `PONG-<hex8>` nonce) passed but this
     longer canary fails. A relay that special-cases the p0 marker but cannot
     echo a longer token is a strong tamper signal.
- **Prerequisites:** p0.echo passed. The probe reuses the PONG- result from
  calibration for the asymmetry check.
- **Request shape (4 calls, two pairs):**

```json
{
  "model": "{model}",
  "messages": [
    {"role": "user", "content": "Reply with exactly this token and nothing else: VERITAS-<hex16>"}
  ],
  "max_tokens": 24,
  "temperature": 0
}
```

  Each pair uses a fresh random `<hex16>`; pair 2 uses a different canary
  from pair 1.
- **Timing:** 4 calls, ~1-2s total.
- **Local calculation:**
  1. Per call: `echo_ok = canary in content` and `echo_exact =
     content.strip() == canary`.
  2. `contamination = any(canary_A in response_B_text or canary_B in response_A_text for A,B in pairs)`.
  3. `template = any(response text identical across a pair even though canaries differ)`.
  4. `asymmetry = p0.echo pass AND all echo_exact failed`.
- **Verdicts:**
  - PASS: all `echo_exact` true, no `contamination`, no `template`.
  - WARN: >= 1 `echo_ok` but not exact (prefix/trailing-text matches);
    or single-pair contamination.
  - FAIL: `contamination` across both pairs; or `template`; or `asymmetry`.
  - SKIP: p0.echo not pass (nothing to compare against).
- **Tolerance/calibration:** exactness is strict; near-misses are WARN not
  FAIL because tokenizers/formatting can add whitespace. The asymmetry rule
  is the calibrated check against the calibration snapshot's p0.echo verdict.
- **Evidence:** 4 exchanges + `metrics.canary_echo` (canaries, per-call
  echo flags, contamination, template, asymmetry).
- **Cost estimate:** 4 x ~30 tokens ~ 120 tokens; < $0.001.
- **Veto eligibility:** none (tamper is not a veto code). `template` or
  `asymmetry` evidence is a tamper/authenticity label: it is recorded in
  `metrics.canary_echo` and, when corroborated by an independent observation
  (e.g. d4.wrap_offset constant wrapper or d4.recount_deviation over-report),
  supports the bundle `authenticity.verdict: confirmed_tampering` and the
  assurance basis. A single near-miss never rises to tampering.
- **False positive risks:** providers that refuse to echo long tokens
  verbatim (writes "VERITAS-..." with formatting); content filtering on
  hex-like tokens; very long canaries exceeding output limits. Use <= 24
  tokens of hex and tolerate whitespace drift.
- **Fixture tests:** fake server `template_mode: bool`,
  `contaminate_cross_request: bool`.
  1. normal echo -> PASS.
  2. `template_mode=True` (identical canned reply regardless of canary) ->
     FAIL (template).
  3. `contaminate_cross_request=True` -> FAIL (contamination).
  4. server echoes p0 PONG- nonce but returns "ok" for VERITAS- canary ->
     FAIL (asymmetry).

---

### 4.6 d4.sse_timing

- **Runner:** `d4.sse_timing` -> `SseTimingProbe`
- **Weight:** 1.0 | **Samples:** 3 (streams) | **Signal family:** relay_timing
- **Purpose:** Characterize the streaming envelope. TTFT (time to first
  chunk) and inter-chunk cadence are fingerprints of the upstream path. A
  relay that buffers the full upstream response then replays it shows all
  chunks arriving at once (negative inter-chunk variance, huge TTFT); a
  re-chunking proxy shows abnormal cadence versus the baseline.
- **Prerequisites:** p0.echo passed. Baseline-gated for the timing tolerance;
  without a baseline it runs structural checks only (buffering detection
  works without a baseline).
- **Request shape (3 calls):**

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "Count from 1 to 50."}],
  "max_tokens": 64,
  "stream": true,
  "stream_options": {"include_usage": true},
  "temperature": 0
}
```

- **Timing:** each stream expected 1-5s; 3 streams; uses
  `RunContext.stream` (Section 3.3).
- **Local calculation:**
  1. Per stream: `ttft_ms` = arrival time of first content event;
     `inter_chunk_ms` = list of deltas between content events;
     `total_ms`; `n_chunks`; `text` = concatenated deltas;
     `est_tokens` = len(text)/4 (char heuristic, replaced by tokenizer in
     the billing phase).
  2. `buffered = (n_chunks <= 2 and total_ms >= 500 and inter_chunk gap near total)` --
     single burst = classic relay buffering.
  3. With baseline: `ttft_z = (median_ttft - baseline_median_ttft) / baseline_p90_delta`;
     same for `median_inter_chunk`. `p90_inter_chunk` should not exceed
     baseline p90 by > 3x.
- **Verdicts:**
  - PASS: not buffered AND (no baseline OR within 2x baseline p90 on ttft
    and inter-chunk).
  - WARN: `buffered` once in 3 streams, or > 2x but <= 5x baseline p90.
  - FAIL: `buffered` in >= 2 of 3 streams (strong relay-buffering signal).
  - SKIP: p0.echo not pass.
- **Tolerance/calibration:** baseline percentile ratios (median, p90). Static
  fallback: `buffered` rule only; no absolute ms thresholds without a
  baseline (latency is endpoint-dependent).
- **Evidence:** 3 redacted SSE text exchanges + `metrics.sse_timing`
  (ttft_ms[], inter_chunk median/p90, buffered flags, total_ms).
- **Cost estimate:** 3 x ~120 tokens ~ 360 tokens; < $0.002.
- **Veto eligibility:** none alone. Note feeding `hidden_origin` only when
  combined with hop-header evidence.
- **False positive risks:** network jitter, server scheduling, provider-side
  streaming batching, client-side read buffering. Baseline + "buffered in 2
  of 3" threshold keep these out.
- **Fixture tests:** fake server `sse_chunk_delay_ms: int`,
  `buffer_streaming: bool`.
  1. normal paced stream -> PASS.
  2. `buffer_streaming=True` (emit all chunks after a 1s delay) -> FAIL
     (>=2 of 3 buffered).
  3. `buffer_streaming=True` but only 1 stream in 3 -> WARN (force via
     counter toggle).
  4. force_429 -> WARN.

---

### 4.7 d4.rotation

- **Runner:** `d4.rotation` -> `RotationProbe`
- **Weight:** 1.5 | **Samples:** 20 (spaced calls) | **Signal family:** relay_timing
- **Purpose:** Detect load-balanced routing across *different upstream
  backends* (or providers) within a single run. Identical temperature-0
  requests that produce multiple distinct response families are the
  fingerprint of fractional routing or model substitution at runtime.
- **Prerequisites:** p0.echo passed. No baseline required (clustering is
  self-contained); baseline refines the "expected" family count.
- **Request shape (20 spaced calls):**

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "Write the number 42 in words and stop."}],
  "max_tokens": 32,
  "temperature": 0
}
```

- **Timing:** 20 calls spaced ~1-2s apart, ~5-10s total; spacing exercises
  distinct routing decisions rather than reusing one warm path.
- **Local calculation:**
  1. Per response build a feature tuple:
     - `id_family` (from d4.id_prefix logic),
     - `content_bucket` = 3-gram shingle hash of `content`,
     - `usage_ratio` = prompt/completion token ratio rounded to 2 dp,
     - `server`/`x-served-by` header value,
     - `shape` fingerprint (reuse the `_shape` structural fingerprint from
       `supgate/probes/idempotency.py`).
  2. Cluster tuples: two responses are the same family if they agree on id
     family AND (content bucket OR usage ratio) -- a tolerant union. Count
     distinct families `F`.
  3. With baseline: `F_expected = baseline.fingerprints.rotation_families`.
- **Verdicts:**
  - PASS: `F == 1`.
  - WARN: `F == 2` (either two content variants or two id families) --
    evidence toward suspected substitution, not a verdict or veto by itself.
  - FAIL: `F >= 3` (multiple upstream families within one run).
  - SKIP: p0.echo not pass.
- **Tolerance/calibration:** temperature-0 nondeterminism exists on some
  backends, so a single pair of differing content is WARN, not FAIL. The
  FAIL threshold of 3 families is the calibrated gate; re-derive from a
  baseline once recorded.
- **Evidence:** 20 exchanges + `metrics.rotation` (per-sample feature tuples,
  family assignments, F, F_expected).
- **Cost estimate:** 20 x ~60 tokens ~ 1200 tokens; < $0.007.
- **Veto eligibility:** corroborated signal for `substitution` when `F >= 3`
  and the id families map to different official providers. `F == 2` is a
  signal toward `authenticity.verdict: suspected_substitution`, never a veto;
  the report sets that verdict only after a second independent signal family
  corroborates it. This is independent of `billing_inflation` confirmation.
- **False positive risks:** legitimate multi-region providers with different
  id prefixes per region; model version rollouts mid-run; heavy output
  nondeterminism; content filtering that varies output. WARN-at-2 and the
  tolerant union reduce these.
- **Fixture tests:** fake server `rotation_families: int` (number of distinct
  (id_prefix, content) buckets to rotate through).
  1. `rotation_families=1` -> PASS.
  2. `rotation_families=2` -> WARN.
  3. `rotation_families=3` -> FAIL.
  4. force_429 -> WARN.

---

## 5. Billing probes

> These depend on the TokenizerService (Section 3.5) and target usage
> transparency. They are the primary source of `billing_inflation` vetoes.

---

### 5.1 d4.usage_presence

- **Runner:** `d4.usage_presence` -> `UsagePresenceProbe`
- **Weight:** 1.0 | **Samples:** 3 (three forms) | **Signal family:** billing_transparency
- **Purpose:** Verify the usage block is present and arithmetically sane in
  every response form where the contract requires it, and absent only where
  the contract allows. Missing usage on a billable path means the operator
  cannot audit cost -- a transparency defect.
- **Prerequisites:** p0.echo passed. No tokenizer needed.
- **Request shapes (3 calls):**
  1. Non-stream (usage required):

```json
{"model": "{model}", "messages": [{"role": "user", "content": "What is 2+2?"}], "max_tokens": 32}
```

  2. Stream with `include_usage: true` (usage required in final chunk):

```json
{"model": "{model}", "messages": [{"role": "user", "content": "What is 2+2?"}], "max_tokens": 32, "stream": true, "stream_options": {"include_usage": true}}
```

  3. Stream without `include_usage` (usage optional; presence is tolerated):
    (same as form 2 minus `stream_options`).
- **Timing:** 3 calls, ~1-3s total (two streams).
- **Local calculation:**
  1. Form 1: `usage_ok = _usage_consistent(body.usage)` (reuse the D6
     `_usage_consistent` arithmetic check: ints and
     total == prompt + completion).
  2. Form 2: parse SSE, take the last event carrying `usage`;
     `stream_ok = usage present and _usage_consistent(usage)`.
  3. Form 3: record `usage_present_without_include`; informational only.
- **Verdicts:**
  - PASS: form 1 `usage_ok` AND form 2 `stream_ok`.
  - WARN: usage present but arithmetic-inconsistent in either form; or form 3
    emits usage (nonstandard but harmless).
  - FAIL: form 1 or form 2 on HTTP 200 with usage absent.
  - SKIP: p0.echo not pass.
- **Tolerance/calibration:** none beyond the arithmetic check; presence is a
  hard contract requirement on forms 1-2.
- **Evidence:** 3 exchanges + `metrics.usage_presence` (per-form usage dicts,
  presence flags, arithmetic results).
- **Cost estimate:** 3 x ~40 tokens ~ 120 tokens; < $0.001.
- **Veto eligibility:** none alone; persistent absence feeds
  `billing_inflation` only together with recount evidence.
- **False positive risks:** providers that only emit usage on the final SSE
  chunk and never in `include_usage` (spec divergence); usage omitted on
  empty completions. Form 3 tolerance covers the common divergence.
- **Fixture tests:** fake server `omit_usage_nonstream: bool`,
  `bad_usage_arithmetic: bool`.
  1. default (usage present, consistent) -> PASS.
  2. `omit_usage_nonstream=True` -> FAIL.
  3. `bad_usage_arithmetic=True` (total != prompt + completion) -> WARN.
  4. stream usage absent even with include_usage -> FAIL (fake server emits
     usage only when configured).

---

### 5.2 d4.recount_deviation

- **Runner:** `d4.recount_deviation` -> `RecountDeviationProbe`
- **Weight:** 2.0 | **Samples:** 3 | **Signal family:** billing_transparency
- **Purpose:** Independently token-count the prompt and completion text and
  compare against the reported `usage` values. Sustained over-reporting is
  billing inflation -- the single strongest M2 veto. Under-reporting is
  recorded as an anomaly but not a veto.
- **Prerequisites:** p0.echo passed. TokenizerService must resolve the
  claimed model's encoding; otherwise SKIP.
- **Request shape (3 calls, varied prompt lengths):**

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "<reference prompt 1>"}],
  "max_tokens": 64,
  "temperature": 0
}
```

  Reference prompts: short ("Say ping."), medium (a 40-word paragraph),
  long (a 200-word paragraph). Same prompt family, different lengths.
- **Timing:** 3 calls, ~1-2s total.
- **Local calculation:**
  1. `recounted_prompt = count(prompt_text, enc)` where prompt_text is the
     full serialized `messages` array text.
  2. `recounted_completion = count(content, enc)`.
  3. `dev_prompt_pct = (reported.prompt_tokens - recounted_prompt) / recounted_prompt * 100`.
  4. `dev_completion_pct` analogously.
  5. Aggregate: `mean_dev = mean(dev_prompt_pct over samples)`.
- **Verdicts:**
  - PASS: `mean_dev <= +5%` (allow small chat-template/overhead delta).
  - WARN: `+5% < mean_dev <= +15%`.
  - FAIL: `mean_dev > +15%` (reproducible over-reporting).
  - SKIP: encoding unresolved for the claimed model.
- **Tolerance/calibration:** +5%/+15% are static gates until the first
  baseline. Chat-template overhead, image tokens, and cached-token
  accounting mean exact equality is impossible; the gate is on the *mean*
  over 3 samples, never a single sample. Baseline re-derivation target:
  baseline `recount_deviation_pct.mean + 4*std` as the WARN gate and
  `mean + 8*std` (min 15%) as the FAIL gate.
- **Evidence:** 3 exchanges + `metrics.recount_deviation` (per-sample
  reported/recounted prompt+completion, deltas, encoding).
- **Cost estimate:** 3 x ~250 tokens ~ 750 tokens; < $0.004.
- **Veto eligibility:** sole veto for `billing_inflation` at FAIL
  (>= 15% mean over-reporting), independently confirmed by the calibrated
  multi-size recount: the deviation must exceed the FAIL gate across all
  three prompt sizes (short/medium/long), never a single size or sample, and
  must hold above the baseline-calibrated gate (baseline mean + 8*std,
  min 15%). This is separate from the two-independent-signal-family
  authenticity rule.
- **False positive risks:** wrong encoding for the model family; image/multimodal
  tokens not counted by the local text tokenizer; cached-token discounts
  (`prompt_tokens_details.cached_tokens`) that lower *billed* tokens but not
  reported ones; provider chat-template overhead that is prompt-independent
  (this is exactly what wrap_offset isolates -- run both before vetoing).
- **Fixture tests:** fake server `usage_offset_tokens: int` (adds a constant
  to reported prompt_tokens).
  1. offset 0 -> PASS.
  2. offset ~8% (varies with prompt) -> WARN.
  3. offset 30% of prompt -> FAIL.
  4. claimed model with unknown encoding (e.g. `mystery-model`) -> SKIP.

---

### 5.3 d4.wrap_offset

- **Runner:** `d4.wrap_offset` -> `WrapOffsetProbe`
- **Weight:** 1.0 | **Samples:** 4 | **Signal family:** billing_transparency
- **Purpose:** Isolate the *constant* component of prompt-token deviation.
  If reported prompt tokens exceed the recounted text by a near-constant
  amount across very different prompt lengths, that constant is a hidden
  wrapper (system prompt, routing marker, injected instructions) -- either a
  tamper signal or at minimum opaque billing.
- **Prerequisites:** p0.echo passed. TokenizerService required.
- **Request shape (4 calls, monotonic lengths):**

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "<length N text>"}],
  "max_tokens": 16,
  "temperature": 0
}
```

  Lengths: ~5, ~50, ~150, ~400 words.
- **Timing:** 4 calls, ~1-2s total.
- **Local calculation:**
  1. Per sample: `offset = reported.prompt_tokens - recounted_prompt`.
  2. `mean_offset`, `std_offset` over the 4 samples.
  3. `offset_stable = std_offset <= 0.25 * max(abs(mean_offset), 1)`.
- **Verdicts:**
  - PASS: `mean_offset` within +-4 tokens (typical chat-template overhead)
    OR `not offset_stable` (deviation grows with prompt length, i.e. no
    constant wrapper).
  - WARN: `offset_stable` and `4 < mean_offset <= 32`.
  - FAIL: `offset_stable` and `mean_offset > 32` (large constant hidden
    wrapper).
  - SKIP: encoding unresolved.
- **Tolerance/calibration:** the 4-token neutral band and 32-token fail gate
  are static until baseline. Baseline target: `wrap_offset_tokens.mean + 6*std`.
  Note: a prompt-length-dependent deviation (not stable) is NOT a wrapper
  signal -- it is tokenizer/overhead noise and must PASS.
- **Evidence:** 4 exchanges + `metrics.wrap_offset` (per-sample offset,
  mean, std, stability flag).
- **Cost estimate:** 4 x ~200 tokens ~ 800 tokens; < $0.004.
- **Veto eligibility:** corroborated signal for `hidden_origin` only with
  d4.recount_deviation FAIL (hidden wrapper that also inflates). Never
  `tamper` (tamper is not a veto code).
- **False positive risks:** vision/image tokens (large but prompt-*dependent*),
  cached-token reductions, tokenizer mismatch that adds a constant bias.
  The stability test and the requirement to pair with recount FAIL mitigate.
- **Fixture tests:** fake server `hidden_wrapper_tokens: int` (constant added
  to reported prompt_tokens).
  1. wrapper 0 -> PASS.
  2. wrapper 12 -> WARN.
  3. wrapper 64 -> FAIL.
  4. usage_offset_tokens=0 but prompts differ -> PASS (no constant).

---

### 5.4 d4.reasoning_cache_fields

- **Runner:** `d4.reasoning_cache_fields` -> `ReasoningCacheFieldsProbe`
- **Weight:** 1.0 | **Samples:** 3 | **Signal family:** billing_transparency
- **Purpose:** Verify `usage.prompt_tokens_details.{cached_tokens,text_tokens}`
  and `usage.completion_tokens_details.reasoning_tokens` are present and
  internally consistent for the claimed model family. Relays that strip or
  fabricate these fields break the operator's ability to audit cache and
  reasoning spend.
- **Prerequisites:** p0.echo passed. Baseline optional (schema expectation
  comes from the claimed family + baseline `usage_schema`).
- **Request shapes (3 calls):**
  1-2. Two identical calls with a large shared prefix (>= 300 tokens) to
        exercise caching:

```json
{
  "model": "{model}",
  "messages": [{"role": "user", "content": "<300-token prefix + suffix>"}],
  "max_tokens": 24,
  "temperature": 0
}
```

  3. Same request against the claimed model family; if the family supports
     reasoning tokens (o1/o3/o4), assert `reasoning_tokens` behavior.
- **Timing:** 3 calls, ~1-3s total.
- **Local calculation:**
  1. `expected_cached = family in baseline.usage_schema` or family known to
     emit cached tokens.
  2. `cached_ok`: when `prompt_tokens_details.cached_tokens` present:
     `0 <= cached_tokens <= prompt_tokens` AND
     `text_tokens == prompt_tokens - cached_tokens` when `text_tokens` present.
  3. `cache_delta_ok`: second identical call should show `cached_tokens >=`
     first call (or stable). Record delta.
  4. `reasoning_ok`: when `completion_tokens_details.reasoning_tokens`
     present: `0 <= reasoning_tokens <= completion_tokens`.
  5. `fields_present`: expected fields present for the claimed family.
- **Verdicts:**
  - PASS: `cached_ok` and `reasoning_ok` where present, and required fields
    present for the claimed family.
  - WARN: fields missing where the claimed family should emit them; or
    cache delta regresses (second call no longer cached).
  - FAIL: fields present but contradictory (`cached_tokens > prompt_tokens`,
    `reasoning_tokens > completion_tokens`).
  - SKIP: claimed model family unknown to the expectation table and no
    baseline `usage_schema`.
- **Tolerance/calibration:** expectation table maps known families to
  {cached: bool, reasoning: bool}. Baseline `usage_schema` overrides it.
- **Evidence:** 3 exchanges + `metrics.reasoning_cache_fields` (field
  presence, consistency flags, cache deltas).
- **Cost estimate:** 3 x ~400 tokens (large prefix) ~ 1200 tokens; ~$0.006
  (highest of the billing probes by design, but still negligible).
- **Veto eligibility:** none alone. Contradictory cache fields corroborate
  `billing_inflation` with recount FAIL.
- **False positive risks:** providers only emit `cached_tokens` on the second
  call; reasoning token fields absent when the model skipped reasoning;
  family tables drift with provider releases.
- **Fixture tests:** fake server `usage_schema_flags: dict` (cached_tokens,
  reasoning_tokens, caching_delta).
  1. full schema, cache grows between identical calls -> PASS.
  2. `usage_schema_flags={cached_tokens: false}` with o1 family claim ->
     WARN (missing fields).
  3. `cached_tokens > prompt_tokens` -> FAIL.
  4. unknown family + no baseline -> SKIP.

---

## 6. M2 dependency graph and build order

Dependency graph (edges = required before):

```
RunContext.stream (3.3)                 -> d4.sse_timing
Baseline format + `baseline` command    -> d4.id_prefix, d4.sse_timing,
                                           d4.recount_deviation, d4.wrap_offset,
                                           d4.self_report, d4.rotation
TokenizerService (3.5)                  -> d4.recount_deviation, d4.wrap_offset
d4.recount_deviation                    -> d4.wrap_offset veto corroboration
calibration snapshot (p0.echo)          -> d4.canary_echo asymmetry check
manifest registration + registry        -> all probes (plumbing, no ordering)
```

Recommended build order:

| Step | Deliverable | Unblocks |
| --- | --- | --- |
| 1 | `RunContext.stream` + `StreamedEvent` (Section 3.3) | sse_timing |
| 2 | Baseline record format, loader, `baseline` CLI | baseline-gated tolerances |
| 3 | d4.headers_diff, d4.id_prefix, d4.model_echo, d4.self_report | identity_consistency |
| 4 | d4.canary_echo | generation_integrity |
| 5 | d4.sse_timing | relay_timing |
| 6 | d4.usage_presence (no tokenizer) | billing_transparency baseline |
| 7 | TokenizerService + tiktoken vendoring | recount/wrap |
| 8 | d4.recount_deviation | primary veto |
| 9 | d4.wrap_offset | corroboration |
| 10 | d4.reasoning_cache_fields | audit completeness (weekend Stretch) |
| 11 | d4.rotation | substitution corroboration |
| 12 | Veto wiring in `_vetoes`, scoring integration, `ProbeResult.metrics` emission, manifest + registry update | full M2 run |

Steps 3-6 have no tokenizer dependency and can be built in parallel with
steps 1-2 where the team is available. Step 12 is the integration gate:
only after it is the README's "M2 next" list considered done.

Tier note: the weekend Must tier targets the ten core probes (d4.headers_diff,
d4.id_prefix, d4.model_echo, d4.self_report, d4.canary_echo, d4.sse_timing,
d4.rotation, d4.usage_presence, d4.recount_deviation, d4.wrap_offset);
d4.reasoning_cache_fields is weekend Stretch. Stretch status does not remove
it from the M2 contract: all eleven probes must be implemented, registered,
and fixture-tested before M2 is declared complete.

Manifest additions (one per probe; `domain: D4`):

```yaml
  - id: d4.headers_diff        # weight 1.0, samples 2 (logical pairs => 4 calls), runner d4.headers_diff
  - id: d4.id_prefix           # weight 1.0, samples 10, runner d4.id_prefix
  - id: d4.model_echo          # weight 0.5, samples 2, runner d4.model_echo
  - id: d4.self_report         # weight 1.0, samples 2, runner d4.self_report
  - id: d4.canary_echo         # weight 1.0, samples 2, runner d4.canary_echo
  - id: d4.sse_timing          # weight 1.0, samples 3, runner d4.sse_timing
  - id: d4.rotation            # weight 1.5, samples 20 (spaced calls), runner d4.rotation
  - id: d4.usage_presence      # weight 1.0, samples 3, runner d4.usage_presence
  - id: d4.recount_deviation   # weight 2.0, samples 3, runner d4.recount_deviation
  - id: d4.wrap_offset         # weight 1.0, samples 4, runner d4.wrap_offset
  - id: d4.reasoning_cache_fields  # weight 1.0, samples 3, runner d4.reasoning_cache_fields
```

Registry additions (`CUSTOM_RUNNERS`):

```python
    "d4.headers_diff": HeadersDiffProbe,
    "d4.id_prefix": IdPrefixProbe,
    "d4.model_echo": ModelEchoProbe,
    "d4.self_report": SelfReportProbe,
    "d4.canary_echo": CanaryEchoProbe,
    "d4.sse_timing": SseTimingProbe,
    "d4.rotation": RotationProbe,
    "d4.usage_presence": UsagePresenceProbe,
    "d4.recount_deviation": RecountDeviationProbe,
    "d4.wrap_offset": WrapOffsetProbe,
    "d4.reasoning_cache_fields": ReasoningCacheFieldsProbe,
```

The stub `ProbeStub.skip_reason` returns "not implemented until M2"; real
probes return `None` (all D4 probes require only p0, which the orchestrator
guarantees ran first). `adhoc` mode does NOT skip D4 (only D2/D8 are
excluded), so D4 probes run in both modes.

---

## 7. Fixture test matrix

Fixture harness: extend `tests/fake_server.py` `FakeOpenAI` with the mutable
attributes listed below and reuse `tests/conftest.py` (`fake_server`,
`transport`, `ctx`, `manifest`). Tests live in
`tests/test_probes_d4.py` (one file per probe or grouped, matching the
`test_probes_d6.py` pattern).

| Probe | New fake-server attribute(s) | Cases (pass/warn/fail/skip) |
| --- | --- | --- |
| d4.headers_diff | `hop_headers: list[str]`, `header_jitter: bool` | none, consistent hops (WARN), jitter (FAIL) |
| d4.id_prefix | `id_prefix_jitter: bool` | stable (PASS), custom single (WARN), jitter (FAIL), baseline override, reverse-identity injection (veto corroboration) |
| d4.model_echo | `model_echo` (str or None) | match (PASS), mismatch (WARN), none (WARN), no FAIL path |
| d4.self_report | `self_report_text: str`, `models_metadata_drift: bool` | match (PASS), drift w/ baseline (WARN), drift + no claim + baseline (FAIL), no baseline (WARN) |
| d4.canary_echo | `template_mode: bool`, `contaminate_cross_request: bool` | echo (PASS), template (FAIL), contamination (FAIL), p0-asymmetry (FAIL) |
| d4.sse_timing | `sse_chunk_delay_ms: int`, `buffer_streaming: bool` | paced (PASS), buffered x3 (FAIL), buffered x1 (WARN), 429 (WARN) |
| d4.rotation | `rotation_families: int` | 1 (PASS), 2 (WARN/label), 3 (FAIL), 20 spaced calls, 429 (WARN) |
| d4.usage_presence | `omit_usage_nonstream: bool`, `bad_usage_arithmetic: bool` | present (PASS), omitted (FAIL), bad math (WARN) |
| d4.recount_deviation | `usage_offset_tokens: int` | 0 (PASS), ~8% (WARN), 30% (FAIL), unknown encoding (SKIP) |
| d4.wrap_offset | `hidden_wrapper_tokens: int` | 0 (PASS), 12 (WARN), 64 (FAIL) |
| d4.reasoning_cache_fields | `usage_schema_flags: dict` | full (PASS), missing (WARN), contradictory (FAIL), unknown family (SKIP) |

Retry policy tests reuse the force_429/force_5xx toggles already in the fake
server; every probe must be tested under `force_429` to assert the "WARN
after exactly one retry" invariant. Transport-error tests (connection refused,
read timeout) must assert FAIL with the failed attempt's evidence saved
(Section 1.3).

---

## 8. Cross-cutting requirements

- **Budget:** all M2 probes add < $0.01 per run combined at the naive blended
  rate (Section 4-5 cost estimates); the per-run budget cap continues to
  apply through `BudgetTracker`. The M2 pricing table and tiktoken-based
  counting replace the char heuristic (out of scope for this spec; tracked
  in the README M2 list).
- **Evidence:** every request/response passes through `RunContext.request` or
  `RunContext.stream` so redaction and curl capture stay at the single choke
  point. No probe writes evidence directly.
- **Timing values:** all `TimingSample`s reuse `kind` in
  {ttft, tpot, itl, e2e}; new `sse_ttft`/`sse_inter_chunk` values are emitted
  as `metrics`, not as new TimingSample kinds, to keep `ProbeResult.samples`
  schema stable.
- **Mode behavior:** D4 probes run in `adhoc` and `full`; `full` additionally
  runs D2/D8 (unchanged).
- **Verdict honesty:** a probe must never PASS on data it could not obtain
  (e.g. FAIL "usage absent" only on HTTP 200 with a parseable body). All
  inconclusive states fall to WARN or SKIP, matching the M1 policy.

---

## 9. Implementation questions (resolved and open)

### 9.1 Resolved (canonical answers)

1. **Manifest samples vs. physical calls:** the manifest `samples` field
   stays "logical samples" (as in D6 `cases`); the runner owns the per-sample
   call multiplier. Canonical values: `d4.id_prefix` = 10 samples (10 calls);
   `d4.rotation` = 20 spaced calls; `d4.headers_diff` = 2 logical pairs =>
   4 physical calls.
2. **Veto note content:** veto `detail` strings are limited to
   non-identity-claim phrasing (e.g. "recount deviation +30% over 3 samples
   is inconsistent with the claimed model's encoding"). There are exactly
   four veto codes; `tamper` is an evidence/authenticity label, never a veto
   (Section 1.5).
3. **recount cached/vision token handling:** samples with
   `prompt_tokens_details.cached_tokens > 0` are excluded from the deviation
   mean (noted in `metrics`), and image-bearing requests are skipped for
   recount. The `billing_inflation` veto additionally requires the over-report
   to be independently confirmed by the calibrated multi-size recount across
   all prompt sizes (Section 5.2).

### 9.2 Genuinely open `[OPEN]`

1. **`RunContext.stream` error semantics:** should a mid-stream transport
   error yield a partial `AsyncIterator` then raise, or abort with a partial
   evidence doc? Contract draft assumes raise-after-partial-evidence (mirrors
   `ctx.request`); confirm before implementing sse_timing.
2. **Tokenizer vendoring:** tiktoken is a new runtime dependency
   (`pyproject.toml` change). Confirm vendoring the package vs. a pinned
   dependency and which encodings table to ship (the `o200k_base` mapping
   for o-series is the critical one).
3. **Baseline identity gate:** the `baseline` command needs an operator
   assertion that the target is "official". Confirm the CLI shape
   (`--label` required, `--confirm-official` flag) and whether baselines are
   stored per-operator or per-project.
4. **Rotation clustering:** the tolerant-union rule (id family OR
   content/usage agreement) is provisional. Confirm whether to add an
   explicit dedup on `_shape` and whether a baseline-provided family count
   should override the static F>=3 threshold.
5. **wrap_offset neutral band:** the +-4 token PASS band and 32-token FAIL
   gate are guesses until a baseline exists. Confirm the band values stay in
   the spec or move entirely to baseline-derived values in M2.
6. **Fixture scope:** the fake server's `_usage` uses len(text)//4; recount
   tests therefore cannot be perfectly deterministic without a real
   tokenizer. Confirm tests may inject a `TokenizerService` double returning
   deterministic counts (the design assumes yes). The reverse-identity
   baseline-injection fixture (Section 4.2) is required regardless.
