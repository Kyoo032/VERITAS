"""supgate CLI (build plan §12).

Exit codes: 0 = report produced · 2 = endpoint unreachable (P0 dead)
· 3 = aborted (config/budget error). Verdicts live in the report, not
the exit code.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from supgate.baseline_recorder import record_baseline
from supgate.baselines import BaselineError, BaselineStore, select_baseline
from supgate.models import SLA
from supgate.orchestrator import Orchestrator, endpoint_dead, summary
from supgate.store import RunStore

app = typer.Typer(no_args_is_help=True, help="Supplier Admission Evaluation Tool")

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
    baseline_dir: Path = typer.Option(
        Path("baselines"),
        "--baseline-dir",
        help="Baseline store directory (docs/08 §10)",
    ),
    baseline_id: str = typer.Option(
        None,
        "--baseline-id",
        help="Load exactly this baseline id; aborts on missing/malformed/incompatible",
    ),
    allow_family_baseline: bool = typer.Option(
        False,
        "--allow-family-baseline",
        help="Also match family-prefix baselines",
    ),
    allow_coarse_baseline: bool = typer.Option(
        False,
        "--allow-coarse-baseline",
        help="Also match coarse vendor/token baselines",
    ),
) -> None:
    """Probe an OpenAI-compatible endpoint and produce a scored run bundle."""
    if mode not in {"adhoc", "full"}:
        _abort(f"invalid mode {mode!r} (expected adhoc or full)")
    api_key = _resolve_key(key_env)
    models = model or []
    if not models:
        _abort("at least one --model is required (claimed model name)")
    out.mkdir(parents=True, exist_ok=True)
    try:
        orchestrator = Orchestrator(concurrency=concurrency, budget_usd=budget_usd)
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
                baseline_root=baseline_dir,
                baseline_id=baseline_id,
                allow_family=allow_family_baseline,
                allow_coarse=allow_coarse_baseline,
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
    _abort(f"report rendering is not implemented until milestone M4 (bundle: {bundle})")


KNOWN_BASELINE_VENDORS = frozenset({"openai", "anthropic", "generic"})


baseline_app = typer.Typer(
    no_args_is_help=True,
    help="Official-endpoint baseline fingerprints: record, list, show, select",
)


@baseline_app.command("record")
def baseline_record(
    vendor: str = typer.Option(..., "--vendor", help="official vendor: openai | anthropic | generic"),
    model: str = typer.Option(..., "--model", help="claimed model name, e.g. gpt-4o"),
    key_env: str = typer.Option(..., "--key-env", help="env var holding the official API key (env-only)"),
    endpoint: str = typer.Option(
        ...,
        "--endpoint",
        help="official endpoint base URL, e.g. https://api.openai.com/v1 (required every run)",
    ),
    out: Path = typer.Option(Path("baselines"), "--out", help="baseline output directory"),
    evidence_out: Path = typer.Option(Path("runs"), "--evidence-out", help="redacted evidence output directory"),
    label: str = typer.Option(None, "--label", help="operator baseline label; defaults to the vendor"),
    model_version: str = typer.Option(None, "--model-version", help="pinned model version, e.g. gpt-4o-2024-08-06"),
    samples: int = typer.Option(3, "--samples", help="non-stream chat capture samples (1..10)"),
    streams: int = typer.Option(1, "--streams", help="streamed capture exchanges (1..5)"),
    confirm_official: bool = typer.Option(
        False,
        "--confirm-official",
        help="operator assertion that the target is an official endpoint (recorded as a note)",
    ),
) -> None:
    """Record official-endpoint fingerprints into baselines/<id>.json (schema v2)."""
    vendor_key = vendor.lower()
    if vendor_key not in KNOWN_BASELINE_VENDORS:
        _abort(f"unknown vendor {vendor!r} (expected openai, anthropic, or generic)")
    if samples < 1 or samples > 10 or streams < 1 or streams > 5:
        _abort("--samples must be within 1..10 and --streams within 1..5")
    api_key = _resolve_key(key_env)
    out.mkdir(parents=True, exist_ok=True)
    try:
        record = asyncio.run(
            record_baseline(
                vendor=vendor_key,
                model=model,
                api_key=api_key,
                endpoint=endpoint,
                out=out,
                label=label,
                model_version=model_version,
                samples=samples,
                streams=streams,
                evidence_root=evidence_out,
                confirmed_official=confirm_official,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _abort(f"baseline recording failed: {exc}")
    typer.echo(
        f"recorded baseline {record.baseline_id} ({record.captured_at}) -> {out / (record.baseline_id + '.json')}"
    )


@baseline_app.command("list")
def baseline_list(
    out: Path = typer.Option(Path("baselines"), "--out", help="baseline store directory"),
    vendor: str = typer.Option(None, "--vendor", help="filter by vendor or provider label"),
    model: str = typer.Option(None, "--model", help="filter by model name"),
    json_out: bool = typer.Option(False, "--json", help="emit JSON instead of a table"),
) -> None:
    """List baseline records in the store (newest first)."""
    store = BaselineStore(out)
    records, errors = store.scan()
    for error in errors:
        typer.secho(f"warning: skipped {error}", err=True)
    if vendor:
        records = [r for r in records if r.vendor == vendor or r.provider_label == vendor]
    if model:
        records = [r for r in records if r.model == model or model in r.claimed_models]
    records.sort(key=lambda r: (r.captured_at or "", r.baseline_id), reverse=True)
    if json_out:
        payload = {
            "schema": 2,
            "count": len(records),
            "baselines": [r.model_dump(mode="json") for r in records],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    if not records:
        typer.echo("no baselines recorded")
        return
    for record in records:
        typer.echo(
            f"{record.baseline_id}  vendor={record.vendor or '-'}  "
            f"model={record.model or '-'}  captured_at={record.captured_at or '-'}"
        )


@baseline_app.command("show")
def baseline_show(
    baseline_id: str = typer.Argument(..., help="baseline id, e.g. BL-OPENAI-GPT4O-0001"),
    out: Path = typer.Option(Path("baselines"), "--out", help="baseline store directory"),
) -> None:
    """Show one baseline record as JSON; rejects malformed/incompatible files."""
    store = BaselineStore(out)
    try:
        record = store.get(baseline_id)
    except BaselineError as exc:
        _abort(f"baseline {baseline_id}: {exc}")
    if record is None:
        _abort(f"no baseline {baseline_id!r} in {out}")
    typer.echo(record.model_dump_json(indent=2))


@baseline_app.command("select")
def baseline_select(
    model: list[str] = typer.Option(None, "--model", help="claimed model name(s); repeatable"),
    vendor: str = typer.Option(None, "--vendor", help="restrict to this vendor/provider label"),
    out: Path = typer.Option(Path("baselines"), "--out", help="baseline store directory"),
    allow_family: bool = typer.Option(False, "--allow-family", help="also match family-prefix baselines"),
    allow_coarse: bool = typer.Option(False, "--allow-coarse", help="also match coarse vendor/token baselines"),
) -> None:
    """Select the best matching baseline for the claimed model(s) (docs/08 §10.2)."""
    if not model:
        _abort("at least one --model is required")
    store = BaselineStore(out)
    records, errors = store.scan()
    for error in errors:
        typer.secho(f"warning: skipped {error}", err=True)
    match = select_baseline(records, model, vendor=vendor, allow_family=allow_family, allow_coarse=allow_coarse)
    if match is None:
        typer.echo(f"no matching baseline for {', '.join(model)} in {out}")
        return
    typer.echo(
        f"{match.record.baseline_id}  kind={match.kind.value}  "
        f"matched_on={','.join(match.matched_on)}  captured_at={match.record.captured_at or '-'}"
    )


app.add_typer(baseline_app, name="baseline", help="Record and manage official-endpoint baselines")


@app.command()
def export_qa(bundle: Path = typer.Argument(...)) -> None:
    """Export failed probes as numbered QA issues (M4)."""
    _abort(f"QA issue export is not implemented until milestone M4 (bundle: {bundle})")


if __name__ == "__main__":
    app()
