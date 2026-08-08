# VERITAS Output Data Contract

Status: living contract (M1 fields shipped; M2/M4 targets specified)
Owners: `docs/08-output-data-contract.md` (this file), `docs/06-m2-probe-spec.md`
Scope: documentation only. The contract describes `supgate/models.py`,
`supgate/evidence.py`, `supgate/scoring.py`, `supgate/store.py`, and the
`report`/`export-qa` surfaces; it does not change code.

## Table of contents

1. Contract principles
2. RunBundle: current schema (M1)
3. RunBundle: target schema (M2+)
4. ProbeResult, evidence refs, and curl
5. Calibration snapshot
6. SurfaceMap
7. Domain score
8. Veto and assurance
9. Transit
10. Baseline records
11. SQLite target schema and migrations
12. Versioning and backward compatibility
13. Redaction invariants
14. Evidence directory layout
15. QA export format (M4 target)
16. HTML/PDF report contract (M4 target)
17. Validation invariants
18. Migration rules

---

## 1. Contract principles

- The JSON run bundle is the single source of truth. HTML, PDF, QA exports,
  and the SQLite history are derived views; they must be regenerable from the
  bundle alone.
- All examples use placeholders: `https://api.supplier.example/v1`, models
  `gpt-4o` / `claude-3-5-sonnet-20241022`, keys shown only as the redacted
  `$SUPGATE_KEY` marker. No real secrets appear anywhere in this document or
  in any generated output (Section 13).
- Fields are additive. New fields may be added within a schema version;
  existing fields are never silently repurposed.
- Readers must ignore unknown fields; writers must emit all known fields
  (even null).
- Every emitted artifact states the producing version (`versions.schema`,
  `versions.supgate`, `versions.manifest`) so downstream tooling can gate on
  it.

---

## 2. RunBundle: current schema (M1)

Serialized by `RunBundle` in `supgate/models.py` and written by
`Orchestrator.run` to `<out>/<run_id>.json`. Current shape:

```json
{
  "run_id": "SUP-20260806-00A1",
  "endpoint": "https://api.supplier.example/v1",
  "claimed_models": ["gpt-4o"],
  "mode": "full",
  "started_at": "2026-08-06T09:00:00+00:00",
  "finished_at": "2026-08-06T09:02:10+00:00",
  "versions": {
    "supgate": "0.1.0",
    "manifest": "1",
    "baselines": "none (M2)"
  },
  "sla": { "ttft_s": 5.0, "tpot_ms": 500.0, "e2e_s": 60.0 },
  "overall_score": 87.5,
  "domain_scores": {
    "D6": { "domain": "D6", "score": 87.5, "probes": ["d6.chat.basic", "d6.chat.sse", "d6.usage_fields", "d6.idempotency"], "verdict_counts": { "pass": 3, "warn": 1 } }
  },
  "assurance": {
    "level": "C",
    "basis": ["identity evidence: n/a (no D4 evidence - M2); capabilities: n/a"],
    "vetoes": []
  },
  "vetoes": [],
  "calibration": {
    "p0_verdicts": { "p0.echo": "pass", "p0.models": "pass", "p0.error_contract": "pass" },
    "models_catalog": 200,
    "claimed_present": true,
    "responses_api": true,
    "messages_api": true,
    "captured_at": "2026-08-06T09:00:01+00:00"
  },
  "probes": [ ],
  "transit": { "hop_lower_bound": 1, "origin_class": "unknown" }
}
```

Notes on M1 shape:

- `domain_scores` is a dict keyed by `Domain.value`; only scored domains
  appear (skips and platform excluded).
- `assurance` embeds the same veto list as the top-level `vetoes`; the two
  must always be identical (validation invariant V7).
- `transit` is a placeholder (hop_lower_bound 1, origin_class unknown).
- `versions.baselines` is the string `"none (M2)"`.

---

## 3. RunBundle: target schema (M2+)

Additive changes over M1. `schema` becomes a first-class field; `surface` is
persisted; `baseline` records the matched baseline; `transit` is enriched;
`ProbeResult.metrics` is added; `cost` summarizes budget accounting.

```json
{
  "schema": 2,
  "run_id": "SUP-20260806-00A1",
  "endpoint": "https://api.supplier.example/v1",
  "claimed_models": ["gpt-4o"],
  "mode": "full",
  "started_at": "2026-08-06T09:00:00+00:00",
  "finished_at": "2026-08-06T09:03:40+00:00",
  "versions": {
    "supgate": "0.2.0",
    "manifest": "3",
    "schema": 2,
    "baselines": "BL-OFFICIAL-OPENAI-GPT4O-0001"
  },
  "sla": { "ttft_s": 5.0, "tpot_ms": 500.0, "e2e_s": 60.0 },
  "cost": {
    "estimated_usd": 0.059,
    "requests": 61,
    "prompt_tokens": 6120,
    "completion_tokens": 1840,
    "blocked": false
  },
  "overall_score": 87.3,
  "domain_scores": {
    "D6": { "domain": "D6", "score": 87.5, "probes": ["d6.chat.basic", "d6.chat.sse", "d6.usage_fields", "d6.idempotency"], "verdict_counts": { "pass": 3, "warn": 1 } },
    "D4": { "domain": "D4", "score": 83.0, "probes": ["d4.headers_diff", "d4.id_prefix", "d4.model_echo", "d4.self_report", "d4.canary_echo", "d4.sse_timing", "d4.rotation", "d4.usage_presence", "d4.recount_deviation", "d4.wrap_offset", "d4.reasoning_cache_fields"], "verdict_counts": { "pass": 10, "fail": 1 } }
  },
  "assurance": {
    "level": "B",
    "basis": [
      "stable relay (no reverse/mixing detected), capabilities verified, black-box",
      "identity evidence: 83.0 (D4 fingerprint/billing probes ran)"
    ],
    "vetoes": []
  },
  "vetoes": [],
  "calibration": {
    "p0_verdicts": { "p0.echo": "pass", "p0.models": "pass", "p0.error_contract": "pass" },
    "models_catalog": 200,
    "claimed_present": true,
    "responses_api": true,
    "messages_api": true,
    "captured_at": "2026-08-06T09:00:01+00:00"
  },
  "surface": {
    "models": ["gpt-4o", "gpt-4o-mini"],
    "claimed_present": true,
    "responses_api": true,
    "messages_api": true,
    "logprobs": false
  },
  "baseline": {
    "baseline_id": "BL-OFFICIAL-OPENAI-GPT4O-0001",
    "matched_on": ["gpt-4o"],
    "captured_at": "2026-08-06T00:00:00+00:00"
  },
  "transit": {
    "hop_lower_bound": 2,
    "origin_class": "gateway",
    "hop_hints": ["via: 1.1 prod-gateway", "x-served-by: cache-ewr1"]
  },
  "inconclusive": false,
  "inconclusive_reason": null,
  "authenticity": {
    "verdict": "consistent",
    "confidence": 0.87,
    "signal_families": ["identity_consistency", "generation_integrity", "relay_timing", "billing_transparency"]
  },
  "probes": []
}
```

Schema-2 additions and rules:

- `schema` (int): the bundle schema version. Absent values in M1 bundles are
  treated as `1`.
- `cost` (object): token/cost summary from `BudgetTracker`; `prompt_tokens`
  and `completion_tokens` are tokenizer-accurate in M2 (replacing the M1
  char/4 heuristic). `blocked` mirrors the tracker's budget cap state.
- `surface` (object): the persisted `SurfaceMap` (Section 6). Mirrors
  `calibration` but keeps full model lists (calibration keeps only the
  count).
- `baseline` (object): the matched baseline reference (Section 10) or `null`.
- `transit` (object): enriched hop analysis (Section 9).
- `inconclusive` (bool) + `inconclusive_reason` (string | null): a run is
  inconclusive when it cannot reach a confident conclusion (e.g. P0-required
  surfaces failed via transport errors, or a required surface is absent).
  When `inconclusive` is true the reason/basis is populated, and consumers
  must not treat an inconclusive run's scores as evidence of a defect.
- `authenticity` (object): the report-level synthesis of D4 identity and
  integrity evidence. `verdict` is one of `consistent`,
  `suspected_substitution`, `confirmed_tampering`, `inconclusive`;
  `confidence` is in [0, 1]; `signal_families` lists the families that
  informed the verdict (Section 1.4 of `docs/06-m2-probe-spec.md`).
  `confirmed_tampering` requires corroborated canary/template evidence
  (tamper is an evidence label, not a veto); `suspected_substitution` is the
  result of at least two independent signal families. A two-cluster rotation
  result is evidence toward that verdict, never sufficient by itself.
- `ProbeResult.metrics` (Section 4) on each probe.

The schema-2 baseline record (Section 10) supersedes the M1 build-plan section 13
scaffold.

---

## 4. ProbeResult, evidence refs, and curl

Current (`supgate/models.py` `ProbeResult`) -- one entry per probe per run:

```json
{
  "probe_id": "d6.chat.basic",
  "domain": "D6",
  "verdict": "pass",
  "score": 100.0,
  "weight": 1.0,
  "successes": 3,
  "attempts": 3,
  "notes": [],
  "evidence_ref": ["SUP-20260806-00A1/d6.chat.basic_001.json", "SUP-20260806-00A1/d6.chat.basic_002.json"],
  "curl": "curl -sS -X POST 'https://api.supplier.example/v1/chat/completions' \\\n  -H 'Authorization: Bearer $SUPGATE_KEY' \\\n  -d '{\"model\":\"gpt-4o\",...}'",
  "samples": [],
  "error": null
}
```

Target adds `metrics` (per-probe computed values consumed by baselines, QA,
and reports):

```json
{
  "probe_id": "d4.recount_deviation",
  "domain": "D4",
  "verdict": "fail",
  "score": 0.0,
  "weight": 2.0,
  "successes": 0,
  "attempts": 3,
  "notes": ["mean recount deviation +21.4% over 3 samples is inconsistent with the claimed model's encoding"],
  "evidence_ref": ["SUP-20260806-00A1/d4.recount_deviation_001.json"],
  "curl": "curl -sS -X POST 'https://api.supplier.example/v1/chat/completions' \\\n  -H 'Authorization: Bearer $SUPGATE_KEY' \\\n  -d '{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":\"...\"}],\"max_tokens\":64,\"temperature\":0}'",
  "samples": [],
  "metrics": {
    "deviation_pct": 21.4,
    "reported_prompt_tokens": 148,
    "recounted_prompt_tokens": 122,
    "encoding": "o200k_base",
    "excluded_cached_samples": 1
  },
  "error": null
}
```

Contract rules:

- `evidence_ref` entries are relative paths into the run's evidence directory
  (Section 14); they never contain absolute paths or secrets.
- `curl` is the last redacted curl captured for the probe. It must contain
  `$SUPGATE_KEY` wherever an Authorization header existed and must never
  contain the live key (invariant V5). On transport errors the curl still
  exists (evidence was saved for the failed attempt).
- `metrics` is optional and additive; consumers must tolerate its absence in
  schema-1 bundles and for non-M2 probes.
- `samples` holds `TimingSample`s (kind in {ttft, tpot, itl, e2e}). Streaming
  micro-timings stay inside `metrics`, not `samples`, to keep this field
  stable.

---

## 5. Calibration snapshot

`CalibrationSnapshot` (captured once per run after P0). Field semantics are
unchanged between M1 and M2:

```json
{
  "p0_verdicts": { "p0.echo": "pass", "p0.models": "pass", "p0.error_contract": "warn" },
  "models_catalog": 200,
  "claimed_present": true,
  "responses_api": true,
  "messages_api": true,
  "captured_at": "2026-08-06T09:00:01+00:00"
}
```

- `p0_verdicts` is keyed by probe id with verdict strings; a `warn` or `fail`
  here degrades confidence but does not abort the run (only a failed
  `p0.echo` flips exit code to 2 via `endpoint_dead`).
- `models_catalog` is `len(SurfaceMap.models)`; `claimed_present` is whether
  any claimed model appears in the catalog. These drive skip rules
  (`no_claimed_model`) and assurance basis text.
- Consumers use this snapshot to decide how much weight to place on D4
  fingerprint findings: a `p0.error_contract == warn` means auth behavior was
  not fully verified, which is relevant context for identity probes.

---

## 6. SurfaceMap

Persisted in schema-2 as `bundle.surface`. Also carried in-memory during the
run (`supgate/models.py` `SurfaceMap`) and into calibration:

```json
{
  "models": ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet-20241022"],
  "claimed_present": true,
  "responses_api": true,
  "messages_api": true,
  "logprobs": false
}
```

- `models` is the `/models` catalog from `p0.models`; may be large, which is
  why calibration persists only the count.
- `responses_api` / `messages_api` / `logprobs` are booleans discovered by
  P0/D6 probes and drive `skip_if` conditions (`no_responses_api`,
  `no_messages_api`).
- M2 additions (additive, all optional): no schema change required beyond
  persisting the object. `d4.self_report` may annotate catalog metadata
  drift in probe `notes`, not in SurfaceMap.

---

## 7. Domain score

`DomainScore` per scored domain; keyed by `Domain.value` in `domain_scores`:

```json
{
  "domain": "D4",
  "score": 83.0,
  "probes": ["d4.headers_diff", "d4.id_prefix", "d4.model_echo", "d4.self_report", "d4.canary_echo", "d4.sse_timing", "d4.rotation", "d4.usage_presence", "d4.recount_deviation", "d4.wrap_offset", "d4.reasoning_cache_fields"],
  "verdict_counts": { "pass": 10, "fail": 1 }
}
```

Rules (unchanged from M1, `supgate/scoring.py`):

- Weighted mean of probe scores within the domain using per-probe `weight`;
  skips and platform results are excluded (never lower a domain).
- `overall_score` normalizes `DOMAIN_WEIGHTS` over the domains actually
  scored (D6 0.30, D4 0.30, D8 0.25, D2 0.15). If no scored domains exist,
  `overall_score` is `null`.
- `verdict_counts` counts every non-skip verdict string actually produced.
- Scores are 0..100, rounded to 1 dp.

---

## 8. Veto and assurance

### 8.1 Veto

```json
{
  "code": "billing_inflation",
  "detail": "recount deviation +21.4% over 3 samples is inconsistent with the claimed model's encoding"
}
```

Target veto codes (Section 1.5 of `docs/06-m2-probe-spec.md`):

| code | triggering evidence |
| --- | --- |
| `reverse_identity` | d4.id_prefix + d4.self_report agree on a different official family |
| `substitution` | d4.rotation >= 3 upstream families mapping to different official providers (F == 2 is a label, not a veto) |
| `billing_inflation` | d4.recount_deviation FAIL (mean over-report >= 15%), independently confirmed by the calibrated multi-size recount |
| `hidden_origin` | d4.headers_diff hop markers + d4.self_report contradiction; or d4.wrap_offset + d4.recount_deviation |

`tamper` is not a veto code. It remains an evidence/authenticity label
(d4.canary_echo `template`/`asymmetry`) that, when corroborated by an
independent observation, supports `authenticity.verdict: confirmed_tampering`
and the assurance basis.

Rules:

- Any non-empty `vetoes` sets `assurance.level` to `Disqualified`
  (`assurance()` in `scoring.py`), regardless of scores.
- A `billing_inflation` veto requires independent confirmation by the
  calibrated multi-size recount: the deviation must exceed the FAIL gate
  across all prompt sizes (short/medium/long), never a single size or
  sample, and stay above the baseline-calibrated gate (baseline mean +
  8*std, min 15%). This is separate from the two-independent-signal-family
  authenticity rule.
- `detail` strings must describe the measured observation, never assert an
  identity (e.g. "prefix gen- is inconsistent with the chatcmpl claim", not
  "endpoint is Anthropic").

### 8.2 Assurance

```json
{
  "level": "B",
  "basis": ["stable relay (no reverse/mixing detected), capabilities verified, black-box"],
  "vetoes": []
}
```

Mapping (unchanged):

- `Disqualified`: any veto.
- `A`: requires white-box credentials (out of scope).
- `B`: black-box stable relay -- `D4 >= 80` AND `D8 >= 80` AND
  `overall >= 70`, no vetoes.
- `C`: everything else (including "no D4 evidence -- M2").

M2 makes `B` reachable by actually scoring D4; the basis text includes the
D4 score line from `scoring.assurance`. `assurance.vetoes` is always a copy
of top-level `vetoes` (invariant V7).

---

## 9. Transit

M1 placeholder: `{"hop_lower_bound": 1, "origin_class": "unknown"}`.

M2 target (populated from d4.headers_diff hop markers + response headers):

```json
{
  "hop_lower_bound": 2,
  "origin_class": "gateway",
  "hop_hints": ["via: 1.1 prod-gateway", "x-served-by: cache-ewr1"]
}
```

- `hop_lower_bound`: 1 + count of `via` entries and proxy markers
  (`x-forwarded-for` hops, `x-proxy`, `cf-ray`). Conservative lower bound --
  an upper bound is impossible from the client side.
- `origin_class`: `official` (hop_lower_bound == 1 and no hop markers),
  `gateway`/`proxy` (hop markers present), `unknown` (no reliable evidence).
  This is a classification of the observed path, not an identity claim.
- `hop_hints`: redacted list of the raw hop-marker header lines observed
  (secrets already scrubbed by the evidence choke point).
- If a `hidden_origin` veto fires, `origin_class` must be set to
  `gateway`/`proxy` in the same bundle (consistency requirement, invariant
  V8).

---

## 10. Baseline records

Full format specified in `docs/06-m2-probe-spec.md` Section 2. This contract
section fixes the file-level and bundle-reference shape.

### 10.1 File format

A baseline is a standalone JSON file `baselines/<baseline_id>.json`:

```json
{
  "schema": 2,
  "baseline_id": "BL-OFFICIAL-OPENAI-GPT4O-0001",
  "provider_label": "openai",
  "claimed_models": ["gpt-4o"],
  "captured_at": "2026-08-06T00:00:00+00:00",
  "surface": { "models_catalog": 200, "responses_api": true, "messages_api": true, "claimed_present": true },
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

Rules:

- The baseline record format is schema v2 and supersedes the M1 build-plan
  section 13 scaffold. Readers accept v1 baselines; writers emit v2.
- `baseline_id` matches `BL-<LABEL>-<MODEL-OR-FAMILY>-<4-digit-seq>`.
- `provider_label` is operator-supplied metadata only (never inferred).
- Every `fingerprints` entry carries its own `n`.
- Baselines are written by the `baseline` CLI command, which requires
  `p0.echo == pass` on the recording run.

### 10.2 Bundle reference

A run bundle records which baseline was used:

```json
{
  "baseline": {
    "baseline_id": "BL-OFFICIAL-OPENAI-GPT4O-0001",
    "matched_on": ["gpt-4o"],
    "captured_at": "2026-08-06T00:00:00+00:00"
  }
}
```

plus `versions.baselines = "<baseline_id>"` (or `"none"` when no match).
Matching rules: newest matching baseline by `captured_at`; match on exact
model or family prefix; no match -> `null` reference and baseline-gated
probes fall back per `docs/06-m2-probe-spec.md`.

---

## 11. SQLite target schema and migrations

Current store (`supgate/store.py`, `~/.supgate/history.db`):

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    endpoint TEXT,
    model TEXT,
    mode TEXT,
    started_at TEXT,
    overall REAL,
    assurance TEXT,
    bundle_path TEXT
);

CREATE TABLE probe_results (
    run_id TEXT,
    probe_id TEXT,
    verdict TEXT,
    score REAL,
    attempts INTEGER,
    successes INTEGER,
    evidence_path TEXT
);
```

Target schema (migrated via `PRAGMA user_version`, see Section 18):

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    endpoint TEXT,
    model TEXT,
    mode TEXT,
    started_at TEXT,
    overall REAL,
    assurance TEXT,
    bundle_path TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    baseline_id TEXT,
    calibration_json TEXT
);

CREATE TABLE probe_results (
    run_id TEXT,
    probe_id TEXT,
    verdict TEXT,
    score REAL,
    attempts INTEGER,
    successes INTEGER,
    evidence_path TEXT,
    metrics_json TEXT,
    notes_json TEXT
);

CREATE TABLE baselines (
    baseline_id TEXT PRIMARY KEY,
    provider_label TEXT,
    claimed_models TEXT,
    captured_at TEXT,
    fingerprints_json TEXT,
    bundle_path TEXT
);

CREATE TABLE vetoes (
    run_id TEXT,
    code TEXT,
    detail TEXT
);
```

- `schema_version`/`calibration_json`/`baseline_id` on `runs` are additive
  columns (safe `ALTER TABLE ADD COLUMN`).
- `probe_results.metrics_json`/`notes_json` hold JSON arrays/objects and are
  additive.
- `baselines` and `vetoes` are new tables.
- The history CLI reads only the `runs` projection (run_id, endpoint, model,
  mode, overall, assurance, started_at); schema-2 columns are for tooling.

---

## 12. Versioning and backward compatibility

### 12.1 Version numbers

| Artifact | Field | Semantics |
| --- | --- | --- |
| Run bundle | `versions.schema` (target) / absent (M1) | increments only on breaking field changes |
| Manifest | `versions.manifest` | YAML `version:` in `probes.yaml` |
| Harness | `versions.supgate` | package `__version__` |
| Baseline | file `schema` | baseline record layout version |
| History DB | `PRAGMA user_version` | store schema version |

### 12.2 Compatibility stance

- **Read side:** any reader accepting schema N must also accept N-1 (and
  older) by treating missing fields as their defaults. Readers must reject
  schema > N with an explicit "unsupported schema" error rather than guess.
- **Write side:** the writer emits every field of its schema version. It
  never writes fields it cannot populate with real values.
- **Additive policy:** adding an optional field (e.g. `ProbeResult.metrics`,
  `RunBundle.cost`) is allowed within a schema version. Removing or
  re-typing a field is a breaking change and requires a schema bump.
- **Manifest/harness decoupling:** a newer harness may run an older manifest;
  the bundle must record both versions so runs are reproducible.

### 12.3 Consumer guidance

- Tooling keys off `versions.schema`. If absent, treat as `1`.
- `overall_score` may be `null` (no scored domains); consumers must render
  `n/a`, never coerce to 0.
- `ProbeResult.curl` may be `null` (probe skipped before any request); treat
  as "no reproducible replay available".

---

## 13. Redaction invariants

Enforced at the single choke point (`supgate/evidence.py`; all traffic flows
through `RunContext.request`/`stream`).

| # | Invariant | Check |
| --- | --- | --- |
| R1 | No live API key, bearer token, or custom-auth secret appears in any emitted artifact (bundle, evidence, curl, history DB, QA export, reports). | regex + tests; `tests/test_evidence.py` |
| R2 | `Authorization` header is always stored as `Bearer $SUPGATE_KEY`. | `redact_headers` |
| R3 | Custom auth headers (`x-api-key`, `api-token`, `auth*`, `token`, ...) become `$SUPGATE_KEY`. | `_SENSITIVE_HEADER` |
| R4 | Sensitive query params become `$SUPGATE_KEY` in URLs and curls. | `_SENSITIVE_PARAM` / `redact_url` |
| R5 | Every curl that carried auth contains the literal token `$SUPGATE_KEY` and no key material. | `build_curl` + tests |
| R6 | `sk-...` keys keep a non-reconstructable stub (`sk-abc****WXYZ`); short keys truncate. | `redact_secrets` |
| R7 | Redaction happens at capture time, never at render time; reports/QA/PDF render only already-redacted data. | pipeline design |

Invariant R7 is the reason reports and QA exports take `bundle_path` as
input rather than re-reading the network. The invariant set must be re-verified
as part of any release (Section 18, migration rule MR5).

---

## 14. Evidence directory layout

```
runs/
  SUP-20260806-00A1.json                      # run bundle (contract)
  evidence/
    SUP-20260806-00A1/
      p0.echo_001.json
      p0.models_001.json
      p0.error_contract_001.json
      p0.error_contract_002.json
      d6.chat.basic_001.json
      ...
      d4.rotation_001.json
      d4.rotation_002.json
      ...
      d4.recount_deviation_001.json
      ...
```

One file per request/response exchange. File name: `<probe_id>_<seq:03d>.json`.
The `probe_id` may contain dots; `_seq` separates. Content (redacted):

```json
{
  "probe": "d4.recount_deviation",
  "request": {
    "method": "POST",
    "url": "https://api.supplier.example/v1/chat/completions",
    "headers": { "Authorization": "Bearer $SUPGATE_KEY", "content-type": "application/json" },
    "body": { "model": "gpt-4o", "messages": [{"role": "user", "content": "..."}], "max_tokens": 64, "temperature": 0 },
    "curl": "curl -sS -X POST 'https://api.supplier.example/v1/chat/completions' ..."
  },
  "response": {
    "status": 200,
    "headers": { "content-type": "application/json" },
    "body": { "id": "chatcmpl-EXAMPLE0000", "object": "chat.completion", "choices": [], "usage": { "prompt_tokens": 148, "completion_tokens": 9, "total_tokens": 157 } }
  },
  "captured_at": "2026-08-06T09:01:02+00:00"
}
```

Rules:

- `evidence_ref` values in `ProbeResult` are exactly `<run_id>/<file>` (the
  `EvidenceWriter.dir.name` + filename). Consumers join against the run's
  `evidence/` root.
- Streamed exchanges store the full buffered SSE text as the response body
  (consistent with M1 D6 SSE evidence); chunk-level timing lives in
  `ProbeResult.metrics`, never in evidence.
- Transport-error exchanges store `status: 0` and a text error body, with a
  redacted curl (already the M1 behavior).

---

## 15. QA export format (M4 target)

`supgate export-qa <bundle>` produces `<run_id>.qa.csv` (and an optional
`<run_id>.qa.json`). Scope: every non-pass probe is an issue.

CSV columns:

```
issue_id,run_id,endpoint,claimed_models,probe_id,domain,verdict,severity,expected_behavior,title,detail,evidence_ref,curl
QA-0001,SUP-20260806-00A1,https://api.supplier.example/v1,"gpt-4o",d4.recount_deviation,D4,fail,high,"reported usage matches an independent recount of prompt and completion text","Billing recount deviation","mean recount deviation +21.4% over 3 samples",SUP-20260806-00A1/d4.recount_deviation_001.json,"curl -sS ..."
```

`endpoint` and `claimed_models` are copied from the bundle top level so each
row is standalone; `expected_behavior` states the contract the probe
verifies. `claimed_models` and any field containing commas or quotes are
CSV-quoted.

JSON equivalent:

```json
{
  "schema": 1,
  "run_id": "SUP-20260806-00A1",
  "endpoint": "https://api.supplier.example/v1",
  "claimed_models": ["gpt-4o"],
  "generated_at": "2026-08-06T09:05:00+00:00",
  "issues": [
    {
      "issue_id": "QA-0001",
      "probe_id": "d4.recount_deviation",
      "domain": "D4",
      "verdict": "fail",
      "severity": "high",
      "expected_behavior": "reported usage matches an independent recount of prompt and completion text",
      "title": "Billing recount deviation",
      "detail": "mean recount deviation +21.4% over 3 samples",
      "evidence_ref": "SUP-20260806-00A1/d4.recount_deviation_001.json",
      "curl": "curl -sS ..."
    }
  ]
}
```

Severity mapping: `fail` -> high, `warn` -> medium; skips are excluded.
`issue_id` is sequential per export (`QA-NNNN`). All strings derive from
already-redacted bundle data (invariant R7). M4 milestone; the CLI stub
exists today.

---

## 16. HTML/PDF report contract (M4 target)

`supgate report <bundle> [--pdf]` renders a report from a bundle file only.
HTML is the primary artifact; PDF is the same content contract rendered
through a PDF engine. Both must be regenerable from the bundle JSON alone.

HTML (`<run_id>.html`, single self-contained file, no external assets):

1. Header: run id, endpoint, mode, timestamps, versions, schema.
2. Executive summary: overall score, assurance level + basis, veto list
   (or "no vetoes").
3. Domain scores table: domain, score, verdict_counts, probe list.
4. Assurance detail: calibration snapshot summary, baseline reference,
   transit (`hop_lower_bound`, `origin_class`).
5. Probe tables grouped by domain: probe id, verdict, score, weight,
   successes/attempts, key notes, `metrics` summary, evidence links,
   curl (pre-formatted, secret-free).
6. Redaction notice: generated date, statement that all secrets were
   redacted at capture time, and the `$SUPGATE_KEY` marker explanation.

PDF (`<run_id>.pdf`): same sections, no interactive links required (print to
file is acceptable), page header/footer with run id and page numbers. Must
not introduce any data not present in the HTML content contract.

Rules:

- Reports render from redacted data only; rendering code performs no further
  secret handling (invariant R7).
- HTML/PDF/QA are derived views; if the bundle is missing or unreadable the
  command exits 3 with an explicit message, never partial output.
- The `--pdf` flag is a rendering switch, not a different contract.

---

## 17. Validation invariants

Validators (used in tests and optionally in a `validate` command) assert:

| # | Invariant |
| --- | --- |
| V1 | `run_id` matches `^SUP-[0-9]{8}-[0-9A-F]{4}$`. |
| V2 | `overall_score` is `null` or in `[0, 100]`; every `domain_scores[*].score` is in `[0, 100]`. |
| V3 | Every `probe.verdict` is one of pass/warn/fail/skip. |
| V4 | `probe.score == 100` iff `probe.verdict == pass` (probe-level); partial pass -> warn with `successes/attempts`. |
| V5 | Every non-null `probe.curl` that carried auth contains `$SUPGATE_KEY`; no curl contains a live key (checked against a sentinel injected at test time). |
| V6 | `probe.evidence_ref` entries resolve to files under `evidence/<run_id>/` when the evidence directory is retained. |
| V7 | `assurance.vetoes` equals top-level `vetoes` element-for-element. |
| V8 | If any veto code is `hidden_origin`, `transit.origin_class` is `gateway` or `proxy`. |
| V9 | `finished_at` >= `started_at` (string comparison is valid for ISO-8601 UTC). |
| V10 | `versions.schema` (when present) is an integer; when absent the bundle is treated as schema 1. |
| V11 | `calibration.models_catalog` equals `len(surface.models)` when both are present (schema 2). |
| V12 | `probes` contains at least one entry with a non-skip verdict in a completed run (guards against a bundle that is all-skips produced by a harness bug). |

---

## 18. Migration rules

MR1. Any breaking change to the run bundle increments `versions.schema`,
    ships a forward reader, adds a migration test, and records the change in
    the changelog. Non-breaking additions do not bump.

MR2. Readers accept schema <= N and reject schema > N with an explicit
    error; they never silently guess at unknown schema fields.

MR3. SQLite migrations use `PRAGMA user_version`. Additive columns use
    `ALTER TABLE ADD COLUMN` with a `user_version` bump; destructive changes
    create a new database file rather than dropping columns in place.

MR4. Baseline `schema` bumps follow the same rule as MR1 but are file-level;
    `baselines/<id>.json` files are immutable after writing -- a re-record
    creates a new sequence number, never overwrites.

MR5. The redaction invariant set (Section 13) must be re-run as part of any
    release pipeline, including when new probe runners or new report/QA
    rendering paths are added.

MR6. Deprecated fields are removed only after two schema versions of read
    support and a recorded deprecation notice (additive-first policy).
