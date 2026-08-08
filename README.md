# VERITAS — Vendor Endpoint Reliability, Identity, Tamper & Assurance System

VERITAS is a DPS supplier-assurance project for evaluating OpenAI-compatible
endpoints. Its `supgate` CLI runs a manifest-driven probe catalog against a
candidate endpoint, scores each domain, produces a run bundle with redacted
evidence, and records history locally.

**Milestone M2 scope** — protocol compliance plus relay and billing assurance:

- **P0** harness self-check: liveness/echo (`p0.echo`), model catalog
  (`p0.models`), error contract (`p0.error_contract`).
- **D6** protocol compliance: chat basics, SSE framing, message shapes, JSON
  mode, tool passthrough, parameter boundaries, `max_tokens`, usage fields,
  vision, idempotency, Responses API.
- **D4** relay fingerprints: response headers, id families, model echo,
  self-report, canary integrity, SSE timing, and backend rotation.
- **D4** billing forensics: usage presence, tiktoken recount, hidden prompt
  offsets, and reasoning/cache field consistency.
- **D2** (full mode): three-band load matrix with TTFT/TPOT/ITL/E2E percentiles,
  client-SLA goodput, and 30K-token needle recall.
- **D8** (full mode): six tool-call modes, strict structured output, reasoning,
  knowledge-cutoff battery, and prompt caching.
- Schema-2 bundles, official baseline recording, four corroborated vetoes,
  tokenizer-accurate budgets, redacted evidence, and SQLite history.

The overall score normalizes over the scored domains that actually ran.

## Requirements

- Python >= 3.11
- Windows / PowerShell 5.1 or Linux/WSL with a POSIX shell.

## Installation (`.venv`)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The `supgate` console script is then available on PATH while the venv is
active, or call it directly as `.\.venv\Scripts\supgate.exe`.

Linux/WSL:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

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

Both modes run the implemented catalog: `adhoc` covers P0 + D6 + D4, while
`full` additionally runs the D2 load matrix/needle recall and the D8 capability
suite, which are skipped in `adhoc`.

### Official baselines

The API key and endpoint are provided explicitly on every baseline run; there
are no shared defaults. The key is env-only, and the endpoint must be passed
each time:

```powershell
$env:MY_OFFICIAL_KEY = "sk-..."                       # fresh per session
supgate baseline record --vendor openai --model gpt-4o `
  --endpoint https://api.openai.com/v1 `
  --key-env MY_OFFICIAL_KEY --confirm-official
supgate baseline list
supgate baseline show BL-OPENAI-GPT-4O-0001
supgate baseline select --model gpt-4o
```

`--endpoint` and `--key-env` are required: `supgate baseline record` fails
loudly when either is missing, and `SUPGATE_OFFICIAL_BASE_URL` / well-known
vendor URLs are never consulted.

Runs auto-select exact baselines from `baselines/`. Family and coarse matching
are opt-in through `--allow-family-baseline` and `--allow-coarse-baseline`.

## Outputs and evidence

Each `run` produces, under `--out` (default `runs/`):

- **`SUP-YYYYMMDD-XXXX.json`** — schema-2 run bundle: identity, versions,
  tokenizer cost, surface/baseline references, scores, assurance/vetoes,
  transit/authenticity synthesis, and one evidence-backed result per probe.
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

`report` and `export-qa` are not implemented until M4 and fail explicitly with
exit code `3`; they never report false success. `baseline` is fully available.

## Test and lint

```powershell
.\.venv\Scripts\python.exe -m pytest -q      # full suite must pass
.\.venv\Scripts\python.exe -m ruff check .
```

Linux/WSL equivalents are `.venv/bin/python -m pytest -q` and
`.venv/bin/python -m ruff check .`.

Both must pass before a run is considered shippable.

## Exit codes

| Code | Meaning                                               |
| ---- | ----------------------------------------------------- |
| `0`  | Report produced                                       |
| `2`  | Endpoint unreachable (`p0.echo` failed — P0 dead)     |
| `3`  | Aborted: config/budget error, missing key/model, invalid SLA, or run failure |

Verdicts live in the report (bundle JSON), **not** the exit code — a run that
finishes with failing probes still exits `0`.

## Current limitations / next

- Assurance `B` is reachable in `full` mode when D8 capability evidence
  verifies; white-box `A` remains out of scope.
- D8 Claude Messages suite, authenticity v1.1 probes, HTML/PDF reports, and QA
  export remain later-milestone carry-over.
- Official OpenAI/Anthropic baselines and the DPS live run require operator
  keys; the offline fixture suite does not use paid credentials or network.
- Golden replay bundles remain a testing-plan carry-over; deterministic fake
  server and adversarial fixture coverage are the current offline gate.
