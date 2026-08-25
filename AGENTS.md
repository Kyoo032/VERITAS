# AGENTS.md

## Cursor Cloud specific instructions

VERITAS (`supgate`) is a single Python package: a CLI that black-box probes
OpenAI-compatible endpoints and writes scored, evidence-backed run bundles.
There is no server or frontend — the "application" is the `supgate` CLI.

- Package manager is `uv` (see `uv.lock`), installed at `~/.local/bin/uv` and on
  the PATH of login shells. The startup update script runs `uv sync --frozen
  --all-extras`, so dependencies are already installed when an agent starts.
- Standard commands (see `README.md` and `.github/workflows/ci.yml`):
  - Lint: `uv run ruff check .`
  - Tests: `uv run pytest -q`
  - Run the CLI: `uv run supgate --help`
- The interpreter is whatever `uv` provisions (>=3.11 per `pyproject.toml`);
  CI covers 3.11 and 3.13.
- Tests are fully offline and deterministic — they drive an in-process fake
  OpenAI ASGI app (`tests/fake_server.py`) via `httpx.ASGITransport`, so no
  network or API keys are needed. The suite is slow (~7 min, 600+ tests)
  because D2 load/timing probes measure real latency percentiles; give
  `pytest` a generous timeout and do not assume it hung.
- API keys are **env-only** and never passed on the command line. `supgate run`
  requires `--base-url`, `--key-env NAME` (the env var holding the key), and at
  least one `--model`.
- To exercise the real `run`/`history` flow without a paid endpoint, serve the
  repo's `tests.fake_server.FakeOpenAI` ASGI app locally (e.g.
  `uv run --with uvicorn` a script that calls `uvicorn.run(FakeOpenAI(), ...)`)
  and point `--base-url` at `http://127.0.0.1:<port>/v1` with key
  `sk-test-valid-key-0000000000` and model `gpt-4o`.
- Run history persists to `~/.supgate/history.db` (SQLite); run bundles and
  redacted evidence are written under `--out` (default `runs/`, git-ignored).
