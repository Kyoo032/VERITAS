"""supgate CLI (build plan §12).

Exit codes: 0 = report produced · 2 = endpoint unreachable (P0 dead)
· 3 = aborted (config/budget error). Verdicts live in the report, not
the exit code.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from supgate.models import SLA
from supgate.orchestrator import Orchestrator, endpoint_dead, summary
from supgate.store import RunStore

app = typer.Typer(no_args_is_help=True, help="DPS Supplier Admission Evaluation Tool")

DEFAULT_MANIFEST = Path(__file__).parent / "manifests" / "probes.yaml"


def _resolve_key(key_env: str) -> str:
    key = os.environ.get(key_env)
    if not key:
        _abort(f"environment variable {key_env} is not set (keys are env-only, §12)")
    return key


def _abort(message: str, code: int = 3) -> None:
    typer.secho(message, err=True)
    raise typer.Exit(code=code)


SLA_KEY_ALIASES = {"ttft": "ttft_s", "tpot": "tpot_ms", "e2e": "e2e_s"}


def _parse_sla(raw: str | None) -> SLA:
    if not raw:
        return SLA()
    parsed: dict[str, float] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        field = SLA_KEY_ALIASES.get(key)
        if field is None:
            _abort(f"invalid SLA spec: unknown key {key!r} (expected ttft=5,tpot=0.5,e2e=60)")
        if not sep or not value:
            _abort(f"invalid SLA spec: missing value for {key!r} (expected ttft=5,tpot=0.5,e2e=60)")
        try:
            number = float(value)
        except ValueError:
            _abort(f"invalid SLA spec: {item!r} (expected ttft=5,tpot=0.5,e2e=60)")
        if number < 0:
            _abort(f"invalid SLA spec: {item!r} must not be negative")
        parsed[field] = number * 1000.0 if field == "tpot_ms" else number
    return SLA(**parsed)


@app.command()
def run(
    base_url: str = typer.Option(..., "--base-url", help="Endpoint base URL, e.g. https://api.supplier.example/v1"),
    key_env: str = typer.Option(..., "--key-env", help="Environment variable holding the API key"),
    model: list[str] = typer.Option(None, "--model", help="Claimed model name(s); repeatable"),
    mode: str = typer.Option("adhoc", "--mode", help="adhoc (quick scan) or full (entire catalog)"),
    out: Path = typer.Option(Path("runs"), "--out", help="Output directory for bundles + evidence"),
    sla: str = typer.Option(None, "--sla", help='Client SLA "ttft=5,tpot=0.5,e2e=60"'),
    budget_usd: float = typer.Option(None, "--budget-usd", help="Per-run cost cap"),
    concurrency: int = typer.Option(10, "--concurrency", help="Max concurrent probes (1..50)"),
) -> None:
    """Probe an OpenAI-compatible endpoint and produce a scored run bundle."""
    if mode not in {"adhoc", "full"}:
        _abort(f"invalid mode {mode!r} (expected adhoc or full)")
    api_key = _resolve_key(key_env)
    models = model or []
    if not models:
        _abort("at least one --model is required (claimed model name)")
    out.mkdir(parents=True, exist_ok=True)
    orchestrator = Orchestrator(concurrency=concurrency, budget_usd=budget_usd)
    try:
        bundle = asyncio.run(
            orchestrator.run(
                endpoint=base_url,
                api_key=api_key,
                claimed_models=models,
                manifest_path=DEFAULT_MANIFEST,
                mode=mode,
                sla=_parse_sla(sla),
                out_dir=out,
                budget_usd=budget_usd,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _abort(f"run aborted: {exc}")
    store = RunStore()
    store.record_run(bundle, out / f"{bundle.run_id}.json")
    typer.echo(summary(bundle))
    typer.echo(f"bundle: {out / (bundle.run_id + '.json')}")
    if endpoint_dead(bundle):
        typer.secho("endpoint unreachable (P0 dead)", err=True)
        raise typer.Exit(code=2)


@app.command()
def history(
    endpoint: str = typer.Option(None, "--endpoint"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """List past runs from the SQLite history store."""
    for row in RunStore().history(endpoint=endpoint, limit=limit):
        typer.echo(
            f"{row['run_id']}  {row['endpoint']}  {row['mode']}  "
            f"overall={row['overall']}  assurance={row['assurance']}  {row['started_at']}"
        )


@app.command()
def report(bundle: Path = typer.Argument(...), pdf: bool = typer.Option(False, "--pdf")) -> None:
    """Render a run bundle to HTML (PDF export arrives in M4)."""
    typer.echo(f"HTML report rendering for {bundle} arrives in milestone M4; JSON bundle is the contract today.")


@app.command()
def baseline() -> None:
    """Record official-endpoint fingerprints (M2)."""
    typer.echo("baseline recording arrives in milestone M2")


@app.command()
def export_qa(bundle: Path = typer.Argument(...)) -> None:
    """Export failed probes as numbered QA issues (M4)."""
    typer.echo(f"QA issue export for {bundle} arrives in milestone M4")


if __name__ == "__main__":
    app()


