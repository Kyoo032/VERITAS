# VERITAS Operator Test, Debug, and Improvement Plan

Status: finalized pre-user-testing plan
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
  this option during operator testing. Baseline recording has no cost cap yet;
  Stage 2 therefore uses the minimum sample counts and remains an approved,
  supervised paid action.
- Use a new key environment variable and freshly entered value for each test
  session. Remove it from the environment when the session ends.

## 2. Stage 0: offline preflight

No provider key or endpoint is needed for this stage.

PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
supgate --help
supgate run --help
supgate baseline record --help
```

Linux/WSL:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
git diff --check
.venv/bin/supgate --help
.venv/bin/supgate run --help
.venv/bin/supgate baseline record --help
```

Gate: all automated checks pass; run help marks `--base-url` and `--key-env`
required; baseline help marks `--endpoint` and `--key-env` required; neither
command exposes `--api-key`.

## 3. Stage 1: controlled adhoc run

Start with one approved OpenAI-compatible test endpoint, a dedicated low-scope
key, and a model the endpoint claims to serve. PowerShell example:

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

## 4. Stage 2: official baseline

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

## 5. Stage 3: controlled full run

Proceed only after Stages 1-2 are accepted. Full mode includes the D2 matrix
and D8 suite; D2 contributes roughly 800K prompt tokens at current settings.

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

## 8. Improvement backlog after first operator evidence

P1:

- Add per-probe progress plus a final summary of baseline selection,
  inconclusive state, cost, budget-blocked count, and failed probe ids.
- Add baseline-record cost preview/cap and a zero-request dry-run planner.
- Persist argv-derived run configuration and runtime dependency versions in the
  bundle; add JSON output and detailed history inspection.
- Add a CLI timeout override and an explicit warning when no baseline matches.
- Decide whether P0 failure should fast-stop by default with an opt-in
  continue-forensics mode.

P2:

- Add deterministic golden replay bundles, adversarial fixture packs, live-test
  markers, and CI coverage gates.
- Handle Ctrl-C with a partial bundle or deterministic artifact cleanup.
- Widen run-id entropy and validate non-negative history limits.
- Make tokenizer/cache startup robust on air-gapped hosts and add retry jitter.
- Finish HTML/PDF reports and QA export in M4.

Improvement order is evidence-driven: security/cost/correctness first,
observability second, calibration third, presentation last.
