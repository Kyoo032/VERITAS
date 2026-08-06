# VERITAS — Vendor Endpoint Reliability, Identity, Tamper & Assurance System

VERITAS is a supplier supplier-assurance project for evaluating OpenAI-compatible
endpoints. Its `supgate` CLI runs a manifest-driven probe catalog against a
candidate endpoint, scores each domain, produces a run bundle with redacted
evidence, and records history locally.

**Milestone M1 scope** — foundation + protocol compliance only:

- **P0** harness self-check: liveness/echo (`p0.echo`), model catalog
  (`p0.models`), error contract (`p0.error_contract`).
- **D6** protocol compliance: chat basics, SSE framing, message shapes, JSON
  mode, tool passthrough, parameter boundaries, `max_tokens`, usage fields,
  vision, idempotency, Responses API.
- Scoring (weighted domain means + Supply Assurance Level) and evidence capture.

D4 (relay fingerprints / billing forensics), D2 (load), and D8 (capabilities)
are stubbed and out of M1 scope. The overall score reflects only the domains
that actually ran.

## Requirements

- Python >= 3.11
- Windows / PowerShell 5.1 (this repo is developed on Windows; commands below
  are PowerShell).

## Installation (`.venv`)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The `supgate` console script is then available on PATH while the venv is
active, or call it directly as `.\.venv\Scripts\supgate.exe`.

## CLI run

API keys are **env-only** — they are never passed on the command line. Set the
key in an environment variable and reference it by name with `--key-env`:

```powershell
$env:SUPGATE_KEY = "sk-..."                      # env-only key
supgate run `
  --base-url "https://api.supplier.example/v1" `
  --key-env SUPGATE_KEY `
  --model "gpt-4o" `
  --mode adhoc
```

Required options: `--base-url`, `--key-env`, and at least one `--model`
(repeatable for multiple claimed names).

Other options:

| Option            | Default        | Notes                                            |
| ----------------- | -------------- | ------------------------------------------------ |
| `--mode`          | `adhoc`        | `adhoc` or `full`                                |
| `--out`           | `runs`         | Output directory for bundles + evidence          |
| `--sla`           | defaults       | Client SLA, e.g. `--sla ttft=5,tpot=0.5,e2e=60`  |
| `--budget-usd`    | unlimited      | Per-run cost cap; blocks further probes when hit |
| `--concurrency`   | `10`           | Max concurrent probes, 1..50                     |

`--sla` keys: `ttft` (time-to-first-token) and `e2e` (end-to-end) are in
seconds; `tpot` (time-per-output-token) is also given in seconds and is
converted internally to milliseconds.

### `adhoc` vs `full`

Both modes currently run the **same implemented M1 catalog** (P0 + D6): the
manifest registers no D2/D8 probes yet, so `adhoc`'s load/capability skip rule
has nothing to skip. The split takes effect once M2/M3 probes register in the
manifest — `full` runs the entire implemented catalog while `adhoc` continues
to skip D2 (load) and D8 (capability) probes.

## Outputs and evidence

Each `run` produces, under `--out` (default `runs/`):

- **`SUP-YYYYMMDD-XXXX.json`** — the run bundle (the contract): run id,
  endpoint, claimed models, mode, SLA, per-domain scores, overall score,
  assurance verdict, and one `ProbeResult` per probe with verdict/score,
  attempt/success counts, notes, and evidence refs.
- **`evidence/SUP-YYYYMMDD-XXXX/`** — one JSON file per request/response
  exchange. Secrets are redacted (API keys and bearer tokens become
  `sk-abc****WXYZ` stubs, the `Authorization` header becomes
  `Bearer $SUPGATE_KEY`), and each request carries a reproducible redacted
  `curl` line so failures can be replayed without printing the key.

Runs are also recorded to a local SQLite history store (`~/.supgate/history.db`).
List recent runs with:

```powershell
supgate history --endpoint https://api.supplier.example/v1 --limit 10
```

`report`, `baseline`, and `export-qa` are CLI stubs that exit 0 and print a
placeholder message; they arrive in later milestones (M4 / M2 / M4).

## Test and lint

```powershell
.\.venv\Scripts\python.exe -m pytest -q      # full suite must pass
.\.venv\Scripts\python.exe -m ruff check .
```

Both must pass before a run is considered shippable.

## Exit codes

| Code | Meaning                                               |
| ---- | ----------------------------------------------------- |
| `0`  | Report produced                                       |
| `2`  | Endpoint unreachable (`p0.echo` failed — P0 dead)     |
| `3`  | Aborted: config/budget error, missing key/model, invalid SLA, or run failure |

Verdicts live in the report (bundle JSON), **not** the exit code — a run that
finishes with failing probes still exits `0`.

## Current M1 limitations / M2 next

- **M1 limitations**
  - Only P0 + D6 probes implemented. D4, D2, and D8 domains produce no scores,
    so Assurance caps at `C` (stable black-box `B` requires D4 fingerprint and
    D8 capability evidence; white-box `A` needs credentials).
  - Veto layer is a reserved empty list; no disqualifying signals yet.
  - Budget math is naive (chars/4 ≈ tokens at a blended $0.005/1K rate); no
    real tokenizer or per-model pricing.
  - Transport/hop analysis is placeholder (`hop_lower_bound: 1`,
    `origin_class: unknown`).
  - HTML/PDF report rendering, baselines, and QA export are CLI stubs.

- **M2 next**
  - D4 relay fingerprint probes (headers diff, id prefix, model echo,
    self-report, canary echo, SSE timing, rotation).
  - D4 billing forensics (usage presence/recount deviation, wrap offset,
    reasoning cache fields) — depends on tokenizers (tiktoken).
  - Wire real veto signals (reverse identity, substitution, billing
    inflation, hidden origin) and the `baseline` command.
  - Real pricing table for budget tracking.
