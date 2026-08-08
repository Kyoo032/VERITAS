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
  false-positive, and redaction cases pass (`617 passed`).
- Packaging: manifest is force-included in the wheel; package, harness, and
  manifest versions are aligned at `0.2.0` / schema 2 / manifest 3. The sdist
  excludes local keys, runs, baselines, coverage, and workspace tooling.
- Official OpenAI and Anthropic baseline capture: blocked until dedicated
  operator keys from OD-01 are available. The CLI path is fixture-verified.
- supplier live run: blocked until the supplier evaluation key and approved target are
  provided. No paid or supplier endpoint was contacted during offline QA.

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

The committed M1 fallback remains commit `41eeb6f`. M2 is currently an
uncommitted working-tree build; no automatic rollback, commit, or tag was
performed. Ship/ship-minus/hold remains the owner's decision.
