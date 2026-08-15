---
type: doc
created: 2026-08-15
updated: 2026-08-15
agent: ai-agent
tags: [veritas, field-test, supplier-gateway, official-gateway]
---

# 13 — Field Test Report: Official supplier gateway Gateway

**Date:** 2026-08-15
**Operator decision:** this run is the official field test and serves as the
committed baseline reference for this release (see §5).

## 1. Target

| Field | Value |
|---|---|
| Supplier surface | the supplier gateway (owner: the operator) |
| Endpoint | `https://api.supplier.example/v1` |
| Model tested | `gpt-5.4` (claimed, from live `/models` catalog) |
| Key | Operator-supplied rotating test key, env-only (`--key-env`), never stored |
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

## 5. Stage 2 baseline status — owner decision

The hash-pinned baseline record could not be written before the rotating
test key expired (recording run p0.echo → 401; planner dry-run had passed:
6 requests, $0.04 nominal, billing omitted — unknown tiktoken encoding for
`gpt-5.4`). **Per owner decision 2026-08-15, the Stage 1 official run
committed under `golden/` serves as the official baseline reference for this
release.** A future `supgate baseline record` with a fresh key (~30s) remains
the path to a hash-pinned record; it must come from the live official
surface and cannot be synthesized from run evidence.

## 6. Reproduce

```powershell
$env:SUPPLIER_TEST_KEY = <fresh operator key>   # env-only, never committed
supgate run --base-url https://api.supplier.example/v1 `
  --key-env SUPPLIER_TEST_KEY --model gpt-5.4 --mode adhoc `
  --budget-usd 5 --concurrency 2 --out runs/operator-supplier-adhoc
Remove-Item Env:SUPPLIER_TEST_KEY
```
