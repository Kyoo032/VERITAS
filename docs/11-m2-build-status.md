# VERITAS M2 Build Status

Status: ready for controlled operator testing; live acceptance pending operator inputs
Date: 2026-08-08

## Shipped scope

- S0 streaming transport and partial-evidence semantics.
- S1 tiktoken service, per-model pricing, and token-accurate budget summaries.
- S2 all seven D4 fingerprint probes.
- S3 schema-v2 official baseline management, transit/authenticity synthesis,
  and the four corroborated veto codes.
- S4 all four D4 billing probes, including the weekend-stretch
  `d4.reasoning_cache_fields` probe.
- Schema-2 run bundles and additive SQLite schema version 2 migration.
- Artifact-level redaction for evidence, bundles, and SQLite-derived views.

## Acceptance status

- Offline L0/L2 fixtures: required S0-S4 positive, boundary, retry, transport,
  false-positive, and redaction cases pass in the current working tree.
- P1 operator tooling is implemented and offline-verified in the current
  working tree: progress/final summaries, baseline planning and budget cap,
  normalized invocation/dependency metadata, JSON and detailed history output,
  timeout overrides/no-baseline warning, and default `p0.echo` fail-fast with
  opt-in `--continue-forensics`.
- Packaging: manifest is force-included in the wheel; package, harness, and
  manifest versions are aligned at `0.2.0` / schema 2 / manifest 3. The sdist
  excludes local keys, runs, baselines, coverage, and workspace tooling.
- Official OpenAI and Anthropic baseline capture: blocked until dedicated
  operator keys from OD-01 are available. The CLI path is fixture-verified.
- **Stage 1 controlled adhoc run EXECUTED 2026-08-15** against the
  operator-approved OpenCode Zen endpoint (`opencode.ai/zen/go/v1`,
  `deepseek-v4-flash`): exit 0, overall 57.2, assurance C, pass=11 warn=5
  fail=5 skip=16, cost $0.39, redaction clean (bundle/evidence/SQLite).
  Bundle `runs/operator-adhoc/SUP-20260815-908C.json`. Stage 2 (official
  OpenAI/Anthropic baselines) and Stage 3 (full 37-probe run) remain
  OD-01/operator-gated.
- **Stage 3 full-mode shakedown EXECUTED 2026-08-15** (operator-approved ahead of
  Stages 1-2 acceptance; no official baseline): exit 0, overall 50.3,
  assurance C, pass=13 warn=6 fail=9 skip=9, cost $3.18 ($25 cap). D2 three
  bands 100% goodput; `d2.needle_recall` FAIL (silent context truncation in
  an HTTP 200); D8 tools.auto/stream pass, structured_strict/multiturn/
  parallel fail. Bundle `runs/operator-full/SUP-20260815-3116.json`. Formal
  Stage 3 acceptance remains gated on OD-01 official baselines.
- **Stage 1 vs the official gateway EXECUTED 2026-08-15** — official OpenAI
  models (`gpt-5.4`, `api.supplier.example/v1`) with the official base API
  key: exit 0, overall
  **82.0**, assurance C, identity evidence **81.2**, pass=16 warn=5 fail=1
  skip=15, cost $0.36, redaction clean. Only `d6.json_mode` failed. Bundle
  `runs/operator-supplier-adhoc/SUP-20260815-2E08.json`. Cleanest supplier
  surface tested to date (zen: 57.2).
- **Stage 2 official baseline SATISFIED (2026-08-15)** — the official field
  test (official base API key, official OpenAI models) satisfies Stage 2;
  golden bundle committed as the official baseline reference. Gateway
  full-mode attempt aborted mid-run by key expiry (partial evidence, no
  bundle) — rerun pending a fresh key.
- supplier live run: blocked until the supplier evaluation key and approved target are
  provided. No paid or supplier endpoint was contacted during offline QA.
- Official OpenAI/Anthropic baselines, the supplier live run, and live veto
  validation remain OD-01/operator-gated. OD-01 remains owned by Rizky (budget
  line: personal); OD-03/04/05 and their existing owners remain open. Later
  live milestones remain subject to those operator and decision gates.

## Carry-over

- L1 content-hash-pinned golden bundle replay set.
- U4 live supplier run and manual evidence/curl review.
- Official OpenAI/Anthropic baseline capture and live D8/D2 calibration on
  real endpoints (operator-key-gated).
- HTML/PDF report and QA export remain M4 work (documented CLI failures today).

## Should-tier additions (registered)

- D2 `d2.load_matrix` (three bands, post-semaphore timers, TTFT/TPOT/ITL/E2E
  P50/P90, client-SLA goodput, concurrency 10) and `d2.needle_recall`
  (30K-token context, 20% needle) are registered in manifest v3 and excluded
  in `adhoc` mode.
- D8 capability probes (`d8.tools.*` six modes, `d8.structured_strict`,
  `d8.reasoning`, `d8.cutoff_battery`, `d8.prompt_caching`) are registered in
  manifest v3 and excluded in `adhoc` mode.
- Full-mode runs now score D2 (15%) and D8 (25%) per the build-plan weights;
  the assurance B path is reachable end to end when D8 evidence verifies.
- Cost note: `full` mode adds roughly 800K prompt tokens for the load matrix,
  which is the largest single per-run cost contributor; the per-run budget cap
  still applies.

## Pre-test hardening

- Run and baseline-record commands require an explicit key-env name and
  endpoint every time; no shared key/endpoint defaults are consulted.
- OpenAI-style text-part content is normalized across the probe catalog, and
  unexpected probe/setup exceptions become FAIL results without losing the
  run bundle.
- P0 completes first; remaining probes use the configured bounded concurrency.
- Unknown-tokenizer baselines retain identity/timing fingerprints and clearly
  omit billing calibration rather than aborting Anthropic-compatible capture.
- Prompt-caching hard/transport failures cannot be downgraded by partial
  successes or retry warnings.
- The finalized operator sequence and debug packet are in
  `12-operator-test-plan.md`.

## Rollback

The committed M1 fallback remains commit `41eeb6f`. M2 and the P1 operator
tooling are currently an uncommitted working-tree build, not merged or
released; no automatic rollback, commit, or tag was performed.
Ship/ship-minus/hold remains the owner's decision.
