---
type: doc
created: 2026-08-15
updated: 2026-08-15
agent: ai-agent
tags: [veritas, field-test, supplier-gateway, official-gateway]
---

# 13 — Field Test Report: Official Gateway Field Test

**Date:** 2026-08-15
The test ran with the operator's **official base API key**; the tested model
(`gpt-5.4`) is served from **official OpenAI** upstreams through the
operator-owned gateway. The committed golden bundle is the official baseline
reference for this release (see §5).

## 1. Target

| Field | Value |
|---|---|
| Supplier surface | Operator-owned gateway serving official OpenAI models |
| Endpoint | `https://api.supplier.example/v1` |
| Model tested | `gpt-5.4` — official OpenAI, via the operator gateway |
| Key | Operator's **official base API key**, env-only (`--key-env`), never stored |
| Mode | adhoc (Stage 1, controlled) |
| Run id | `SUP-20260815-2E08` |
| supgate | 0.2.0 · manifest 3 · schema 2 |

## 2. Result

| Metric | Value |
|---|---|
| Exit code | 0 (P0 alive) |
| Overall | **82.0** |
| Assurance | **C** (identity evidence 81.2; capabilities n/a in adhoc) |
| Verdicts | 16 pass · 5 warn · 1 fail · 15 explicit skips |
| Cost | $0.36 estimated (budget cap $5, blocked=0 · 77 requests) |
| Redaction | key absent from bundle, 79 evidence files, and history DB |

Comparison: OpenCode Zen gateway scored **57.2** (9 fails incl. silent
context truncation) in the same probe battery on the same day.

## 3. Probe-level findings

**Fail (1)**

- `d6.json_mode` — strict JSON-schema mode not enforced: HTTP 200 but the
  payload does not honor `json_object` with `name`/`value` keys.

**Warn (5)**

- `d4.headers_diff` — hop markers present but consistent: a visible,
  well-behaved proxy (expected for a gateway product).
- `d4.self_report` — structural run cannot PASS without a recorded baseline.
- `d4.usage_presence` — usage emitted on stream without `include_usage`
  (nonstandard, harmless).
- `d6.max_tokens` — does not hard-stop at max_tokens (finish_reason not
  `length`).
- `d6.param_boundaries` — `n=2` did not return 2 choices.

**Identity probes all PASS** (`d4.canary_echo`, `d4.id_prefix`,
`d4.rotation`, `d4.model_echo`, `d4.sse_timing`) — no rotating upstream
families, no canary echoing, stable request-ID families.

## 4. Committed evidence (golden)

`golden/SUP-20260815-2E08/` — schema-2 bundle + 79 redacted evidence files.
Redaction re-verified at copy time (byte-level scan). See
`golden/SUP-20260815-2E08/README.md`.

## 5. Stage 2 baseline status — SATISFIED

Stage 2 is satisfied by the official field test: run with the official base
API key against the operator-owned gateway serving official OpenAI models,
and the committed golden bundle is the official baseline reference for this
release. A hash-pinned `supgate baseline record` remains available as an
optional future capture (~30s with a fresh key); it must come from the live
official surface and cannot be synthesized from run evidence.

## 6. Reproduce

```powershell
$env:SUPPLIER_TEST_KEY = <official base API key>   # env-only, never committed
supgate run --base-url https://api.supplier.example/v1 `
  --key-env SUPPLIER_TEST_KEY --model gpt-5.4 --mode adhoc `
  --budget-usd 5 --concurrency 2 --out runs/operator-supplier-adhoc
Remove-Item Env:SUPPLIER_TEST_KEY
```
