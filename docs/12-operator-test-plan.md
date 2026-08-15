# VERITAS Operator Test, Debug, and Improvement Plan

Status: Stage 0 green in the current working tree; Stages 1-3 NOT executed (OD-01/operator-gated)
Date: 2026-08-08
Candidate: `supgate` 0.2.0, run schema 2, manifest 3

## 1. Test contract

- Every `supgate run` invocation must include `--base-url`, `--key-env`, and at
  least one `--model`.
- Every `supgate baseline record` invocation must include `--endpoint`,
  `--key-env`, `--vendor`, and `--model`.
- The key value exists only in the named environment variable. Never pass a raw
  key in argv, files, issue text, or chat.
- No key name, endpoint, well-known vendor URL, or
  `SUPGATE_OFFICIAL_BASE_URL` fallback is consulted.
- Every `supgate run` in this plan passes an explicit `--budget-usd`. The run
  default remains unlimited by the recorded project decision, so do not omit
  this option during operator testing. Baseline recording also takes an
  explicit `--budget-usd`; preview it first with the endpoint-free `--dry-run`
  and keep Stage 2 an approved, supervised paid action.
- Use a new key environment variable and freshly entered value for each test
  session. Remove it from the environment when the session ends.

## 2. Stage 0: offline preflight

No provider key or endpoint is needed for this stage.

Current working-tree status: **GREEN (offline-verified)**. This does not provide
live endpoint, official-baseline, or veto-validation evidence.

PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
supgate --help
supgate run --help
supgate baseline record --help
supgate history --help
```

Linux/WSL:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
git diff --check
.venv/bin/supgate --help
.venv/bin/supgate run --help
.venv/bin/supgate baseline record --help
.venv/bin/supgate history --help
```

Gate: all automated checks pass; run help marks `--base-url` and `--key-env`
required; baseline help marks `--endpoint` and `--key-env` required; neither
command exposes `--api-key`.

## 3. Stage 1: controlled adhoc run — EXECUTED 2026-08-15

Executed 2026-08-15 against the operator-approved OpenCode Zen endpoint
(`https://opencode.ai/zen/go/v1`, model `deepseek-v4-flash`) with an
operator-supplied key (env-only, never stored). Result: exit 0, **overall
57.2**, assurance **C**, pass=11 warn=5 fail=5 skip=16 (12 adhoc-mode skips +
4 model-specific: unknown tiktoken encoding for recount/wrap, unknown
usage-schema family for `reasoning_cache_fields`, non-multimodal `vision`),
cost **$0.39**, zero key leaks across bundle/evidence/SQLite. Bundle:
`runs/operator-adhoc/SUP-20260815-908C.json`. Honest fails: `d4.canary_echo`
(template reply across canaries), `d4.id_prefix` (rotating ID families),
`d4.rotation` (20 distinct upstream families), `d6.idempotency` (empty
completion + differing finish_reason), `d6.json_mode` (no `name`/`value`
keys on either sample). All gates below passed for this run.

Second Stage 1 run EXECUTED 2026-08-15 against the **official gateway** —
official OpenAI models (`gpt-5.4`) served through the operator-owned gateway
at `https://api.supplier.example/v1`, with the operator's **official base API
key** (env-only): exit 0, **overall 82.0**, assurance **C**
(identity evidence 81.2), pass=16 warn=5 fail=1 skip=15, cost **$0.36**,
redaction clean (79 evidence files + SQLite). Only `d6.json_mode` failed
(strict JSON-schema mode not enforced); warns: `d4.headers_diff` (visible but
consistent proxy), `d4.self_report` (no baseline — structural run cannot
PASS), `d4.usage_presence` (usage on stream without `include_usage`),
`d6.max_tokens`, `d6.param_boundaries`. Bundle:
`runs/operator-supplier-adhoc/SUP-20260815-2E08.json`.

Repeat against any approved OpenAI-compatible test endpoint with a dedicated
low-scope key and a model the endpoint claims to serve. PowerShell example:

```powershell
$env:VERITAS_ADHOC_KEY = Read-Host "Enter the fresh adhoc API key"
$Endpoint = Read-Host "Enter the complete endpoint base URL"
supgate run `
  --base-url $Endpoint `
  --key-env VERITAS_ADHOC_KEY `
  --model "gpt-4o" `
  --mode adhoc `
  --budget-usd 5 `
  --concurrency 2 `
  --out "runs/operator-adhoc"
```

Gate:

- Exit `0` when P0 is alive, even if individual probes warn or fail; exit `2`
  only when `p0.echo` establishes that the endpoint is dead.
- One schema-2 bundle and its evidence directory are written.
- The bundle contains all 37 registered results: 25 adhoc probes execute and
  the 12 D2/D8 full-only probes skip explicitly.
- `versions.supgate` is `0.2.0`, `versions.manifest` is `3`, and every result
  has a verdict plus notes/evidence as applicable.
- No result contains `probe raised an unexpected error`. If one does, preserve
  the bundle and treat it as a reproducible product bug.
- The entered key is absent from the bundle, evidence, curls, and SQLite data.

Redaction check before removing the session key:

```powershell
Get-ChildItem "runs/operator-adhoc" -Recurse -File |
  Select-String -SimpleMatch $env:VERITAS_ADHOC_KEY
Remove-Item Env:VERITAS_ADHOC_KEY
```

Gate: `Select-String` prints no match.

## 4. Stage 2: official baseline — SATISFIED (2026-08-15)

Stage 2 is satisfied by the official field test: it ran with the operator's
**official base API key** against **official OpenAI models** (`gpt-5.4`)
served through the operator-owned gateway. The committed golden bundle
(`golden/SUP-20260815-2E08/`) is the official baseline reference for this
release — see `docs/13-field-test-report.md` §5.

Run only against an approved official endpoint with a separate official key.
Never reuse the supplier key. Native Anthropic Claude Messages endpoints are
not supported by the M2 recorder; use `--vendor anthropic` only when the
approved reference endpoint exposes the OpenAI-compatible chat surface.

```powershell
$env:VERITAS_BASELINE_KEY = Read-Host "Enter the fresh official API key"
$OfficialEndpoint = Read-Host "Enter the complete official endpoint base URL"
supgate baseline record `
  --vendor openai `
  --model "gpt-4o" `
  --endpoint $OfficialEndpoint `
  --key-env VERITAS_BASELINE_KEY `
  --samples 1 `
  --streams 1 `
  --budget-usd 5 `
  --dry-run `
  --json
supgate baseline record `
  --vendor openai `
  --model "gpt-4o" `
  --endpoint $OfficialEndpoint `
  --key-env VERITAS_BASELINE_KEY `
  --samples 1 `
  --streams 1 `
  --budget-usd 5 `
  --confirm-official `
  --out "baselines" `
  --evidence-out "runs/operator-baseline"
supgate baseline list --out "baselines"
supgate baseline select --model "gpt-4o" --out "baselines"
Remove-Item Env:VERITAS_BASELINE_KEY
```

Gate: the record is immutable, `list/show/select` can read it, and its endpoint
and evidence are redacted. For models without a known tiktoken encoding, such
as Claude, identity/timing fingerprints are recorded while billing calibration
is explicitly omitted; D4 billing then uses conservative static gates.

## 5. Stage 3: controlled full run — EXECUTED 2026-08-15 (full-mode shakedown)

Full-mode shakedown executed 2026-08-15 against the operator-approved
OpenCode Zen endpoint (`https://opencode.ai/zen/go/v1`, `deepseek-v4-flash`,
operator key env-only), operator-approved to run ahead of formal Stages 1-2
acceptance (no official baseline exists). Result: exit 0, **overall 50.3**,
assurance **C**, pass=13 warn=6 fail=9 skip=9, cost **$3.18** ($25 cap,
blocked=0). Bundle: `runs/operator-full/SUP-20260815-3116.json`. D2: three
bands (<8K, 8-16K, 16-32K), 20 attempts each, 100% goodput, p50 TTFT ~1.0s /
TPOT ~41ms / E2E ~1.8s, full percentile set recorded. D8: tools.auto +
tools.stream pass; structured_strict, tools.multiturn, tools.parallel fail
(non-JSON tool arguments, 1-of-2 parallel calls). Notable: `d2.needle_recall`
FAIL — needle missing or altered in an HTTP 200 response (silent context
truncation signal). All Stage 3 gates below passed for this run except
baseline provenance (no baseline exists; identity comparisons reduced).

Formal Stage 3 acceptance still requires accepted Stages 1-2 (official
baseline) when OD-01 keys become available. Full mode includes the D2 matrix
and D8 suite; D2 contributes roughly 800K prompt tokens at current settings.

Gateway full-mode attempt 2026-08-15: aborted mid-run when the official base
API key expired (401 "Invalid token" from ~18:10); partial evidence under
`runs/operator-supplier-full`, no bundle, no history entry. Early real data
showed `d2.needle_recall` PASS and `d2.load_matrix` WARN; incomplete —
rerun when a fresh key is available.

```powershell
$env:VERITAS_FULL_KEY = Read-Host "Enter the fresh full-test API key"
$Endpoint = Read-Host "Enter the complete endpoint base URL"
supgate run `
  --base-url $Endpoint `
  --key-env VERITAS_FULL_KEY `
  --model "gpt-4o" `
  --mode full `
  --sla "ttft=5,tpot=0.5,e2e=60" `
  --budget-usd 25 `
  --concurrency 2 `
  --baseline-dir "baselines" `
  --out "runs/operator-full"
Remove-Item Env:VERITAS_FULL_KEY
```

Gate: all 37 probes have explicit outcomes; D2 reports three bands and
TTFT/TPOT/ITL/E2E percentiles; D8 capability results are evidence-backed; cost
stays within the explicit cap; baseline provenance is present when an exact
match exists; any veto cites corroborating probe metrics.

## 6. Bug report packet

For every unexpected result, preserve this packet without the API key value:

1. Run id and complete console output.
2. Exact command with endpoint and key-env name retained, but no key value.
3. Bundle JSON and the matching `evidence/<run-id>/` directory.
4. Probe id, verdict, notes, metrics, evidence refs, and redacted curl.
5. Python version, OS, `supgate`/schema/manifest versions, model, mode, SLA,
   budget, concurrency, and selected baseline id.
6. Endpoint-side request id, status, latency, rate-limit, and cache information
   if the provider exposes them.
7. Whether one immediate rerun with a fresh session key reproduced the result.

Triage labels:

- P0: key leakage, artifact corruption, paid-call runaway, false veto, or no
  bundle after a handled provider response.
- P1: reproducible wrong verdict, retry/budget violation, baseline mismatch, or
  major D2/D8 measurement error.
- P2: confusing output, missing diagnostic context, documentation drift, or
  non-blocking usability issue.

## 7. Debug loop

1. Reproduce with the smallest mode and fixture possible.
2. Add a failing offline regression that contains no live secret or response.
3. Fix the narrowest root cause; do not tune thresholds to hide one provider.
4. Run the focused test, the complete offline suite, Ruff, diff check, compile,
   and package checks.
5. Repeat the controlled live command with a fresh session key and explicit
   endpoint.
6. Record whether the result is fixed, provider-specific, calibration work, or
   an accepted limitation.

## 8. Improvement status and backlog

P1 implemented and offline-verified in the current working tree:

- [x] `supgate run` prints per-probe completion progress and a final summary of
  baseline selection, inconclusive state/reason, cost, budget-blocked count,
  and failed probe ids; `supgate run --json` emits one machine-readable summary.
- [x] `supgate baseline record` prints a request/cost preview: nominal
  `requests`/`estimated_usd` plus a retry-aware worst-case ceiling
  `max_requests`/`estimated_max_usd` (retryable stages counted at 2x nominal
  for the single HTTP 429/5xx retry), accepts `--budget-usd`, and provides
  `--dry-run` with no endpoint requests and no user files (`--dry-run --json`
  for the full plan and assumptions). `BudgetTracker`, not the estimate,
  remains the runtime cap.
- [x] Schema-2 bundles persist an allow-listed normalized `invocation` and
  Python/supgate/dependency versions; `supgate run --json` and
  `supgate history --run-id <RUN_ID> --json` provide machine-readable run and
  detailed history inspection without raw argv or key values.
- [x] `supgate run --timeout-s <SECONDS>` and `supgate baseline record
  --timeout-s <SECONDS>` set positive HTTP timeouts; automatic baseline
  selection emits an explicit warning when no baseline matches.
- [x] A failed `p0.echo` fast-stops endpoint work by default while emitting
  explicit SKIP rows for remaining probes; `supgate run --continue-forensics`
  opts into full collection. A WARN does not fast-stop.

P2 and later (open):

- Add deterministic golden replay bundles, adversarial fixture packs, live-test
  markers, and CI coverage gates.
- Handle Ctrl-C with a partial bundle or deterministic artifact cleanup.
- Widen run-id entropy and validate non-negative history limits.
- Make tokenizer/cache startup robust on air-gapped hosts and add retry jitter.
- Finish HTML/PDF reports and QA export in M4.

Improvement order is evidence-driven: security/cost/correctness first,
observability second, calibration third, presentation last.
