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
from typing import Any

import typer

from supgate.baseline_plan import plan_baseline_record
from supgate.baseline_recorder import record_baseline, validate_baseline_endpoint
from supgate.baselines import BaselineError, BaselineStore, select_baseline
from supgate.models import SLA, InvocationConfig, ProbeResult, RunBundle, Verdict
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


def _positive(value: float, option_name: str) -> None:
    if value <= 0:
        _abort(f"{option_name} must be positive")


def _run_json_payload(bundle: RunBundle, bundle_path: Path) -> dict[str, Any]:
    counts = {verdict.value: 0 for verdict in Verdict}
    for probe in bundle.probes:
        counts[probe.verdict.value] += 1
    return {
        "schema": bundle.schema,
        "run_id": bundle.run_id,
        "endpoint": bundle.endpoint,
        "mode": bundle.mode,
        "overall_score": bundle.overall_score,
        "assurance": bundle.assurance.level.value,
        "verdict_counts": counts,
        "baseline_id": bundle.baseline.baseline_id if bundle.baseline else None,
        "inconclusive": bundle.inconclusive,
        "inconclusive_reason": bundle.inconclusive_reason,
        "cost": bundle.cost.model_dump(mode="json"),
        "failed_probe_ids": [
            probe.probe_id for probe in bundle.probes if probe.verdict == Verdict.FAIL
        ],
        "bundle_path": str(bundle_path),
    }


def _baseline_plan_payload(
    *,
    vendor: str,
    model: str,
    key_env: str,
    endpoint: str,
    out: Path,
    evidence_out: Path,
    label: str | None,
    model_version: str | None,
    samples: int,
    streams: int,
    confirm_official: bool,
    budget_usd: float | None,
    timeout_s: float,
) -> dict[str, Any]:
    plan = plan_baseline_record(model=model, samples=samples, streams=streams)
    return {
        "operation": "baseline record",
        "vendor": vendor,
        "model": model,
        "key_env": key_env,
        "endpoint": endpoint,
        "out": str(out),
        "evidence_out": str(evidence_out),
        "label": label,
        "model_version": model_version,
        "samples": samples,
        "streams": streams,
        "confirm_official": confirm_official,
        "budget_usd": budget_usd,
        "timeout_s": timeout_s,
        "within_budget": budget_usd is None or plan.estimated_max_usd <= budget_usd,
        "plan": plan.to_dict(),
    }


def _print_baseline_preview(payload: dict[str, Any], *, err: bool = False) -> None:
    plan = payload["plan"]
    cap = payload["budget_usd"]
    cap_text = "none" if cap is None else f"${cap:.10f}"
    typer.echo(
        "baseline request/cost preview: "
        f"requests={plan['requests']} max_requests={plan['max_requests']} "
        f"estimated_usd=${plan['estimated_usd']:.10f} "
        f"estimated_max_usd=${plan['estimated_max_usd']:.10f} "
        f"budget_cap={cap_text}",
        err=err,
    )


def _print_history_detail(detail: dict[str, Any]) -> None:
    """Render one decoded history record without changing its JSON shape."""

    for key in (
        "run_id",
        "endpoint",
        "model",
        "mode",
        "started_at",
        "finished_at",
        "overall",
        "assurance",
        "schema_version",
        "baseline_id",
        "bundle_path",
        "key_env",
        "key_fingerprint",
    ):
        typer.echo(f"{key}: {detail.get(key)}")
    for key in ("invocation", "versions", "calibration"):
        typer.echo(f"{key}: {json.dumps(detail[key], sort_keys=True)}")
    typer.echo("probes:")
    for probe in detail["probes"]:
        typer.echo(
            f"  {probe['probe_id']}  verdict={probe['verdict']}  "
            f"score={probe['score']}  attempts={probe['attempts']}  "
            f"successes={probe['successes']}"
        )
        typer.echo(f"    metrics: {json.dumps(probe['metrics'], sort_keys=True)}")
        typer.echo(f"    notes: {json.dumps(probe['notes'])}")
        typer.echo(f"    evidence_ref: {json.dumps(probe['evidence_ref'])}")
    typer.echo("vetoes:")
    for veto in detail["vetoes"]:
        typer.echo(f"  {veto['code']}: {veto['detail']}")


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
    timeout_s: float = typer.Option(
        60.0,
        "--timeout-s",
        help="Positive HTTP timeout in seconds",
    ),
    continue_forensics: bool = typer.Option(
        False,
        "--continue-forensics",
        help="Continue all probes after p0.echo fails (default: fail fast)",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit exactly one machine-readable JSON summary on stdout",
    ),
) -> None:
    """Probe an OpenAI-compatible endpoint and produce a scored run bundle."""
    if mode not in {"adhoc", "full"}:
        _abort(f"invalid mode {mode!r} (expected adhoc or full)")
    _positive(timeout_s, "--timeout-s")
    models = model or []
    if not models:
        _abort("at least one --model is required (claimed model name)")
    parsed_sla = _parse_sla(sla)
    api_key = _resolve_key(key_env)
    out.mkdir(parents=True, exist_ok=True)
    try:
        orchestrator = Orchestrator(
            concurrency=concurrency, timeout_s=timeout_s, budget_usd=budget_usd
        )
        invocation = InvocationConfig(
            base_url=base_url,
            key_env=key_env,
            models=models,
            mode=mode,
            out=str(out),
            sla=parsed_sla,
            budget_usd=budget_usd,
            concurrency=concurrency,
            baseline_dir=str(baseline_dir),
            baseline_id=baseline_id,
            automatic_baseline=baseline_id is None,
            allow_family_baseline=allow_family_baseline,
            allow_coarse_baseline=allow_coarse_baseline,
            timeout_s=timeout_s,
            continue_forensics=continue_forensics,
        )

        def progress(completed: int, total: int, result: ProbeResult) -> None:
            typer.echo(f"{completed}/{total} {result.probe_id} {result.verdict.value}")

        bundle = asyncio.run(
            orchestrator.run(
                endpoint=base_url,
                api_key=api_key,
                key_env=key_env,
                claimed_models=models,
                manifest_path=DEFAULT_MANIFEST,
                mode=mode,
                sla=parsed_sla,
                out_dir=out,
                budget_usd=budget_usd,
                baseline_root=baseline_dir,
                baseline_id=baseline_id,
                allow_family=allow_family_baseline,
                allow_coarse=allow_coarse_baseline,
                on_probe_complete=None if json_out else progress,
                continue_forensics=continue_forensics,
                invocation=invocation,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _abort(f"run aborted: {exc}")
    store = RunStore()
    bundle_path = out / f"{bundle.run_id}.json"
    store.record_run(bundle, bundle_path)
    if baseline_id is None and bundle.baseline is None:
        typer.secho(
            "WARNING: automatic baseline selection found no matching baseline; "
            "identity comparisons are reduced.",
            err=True,
        )
    if json_out:
        typer.echo(json.dumps(_run_json_payload(bundle, bundle_path)))
    else:
        typer.echo(summary(bundle))
        typer.echo(f"bundle: {bundle_path}")
    if endpoint_dead(bundle):
        typer.secho("endpoint unreachable (P0 dead)", err=True)
        raise typer.Exit(code=2)


@app.command()
def history(
    endpoint: str = typer.Option(None, "--endpoint"),
    limit: int = typer.Option(10, "--limit"),
    run_id: str = typer.Option(None, "--run-id", help="Inspect one run in detail"),
    json_out: bool = typer.Option(False, "--json", help="Emit exactly one JSON value"),
) -> None:
    """List past runs or inspect one run from the SQLite history store."""
    if limit < 0:
        _abort("--limit must not be negative")
    store = RunStore()
    if run_id is not None:
        detail = store.inspect_run(run_id)
        if detail is None:
            _abort(f"run id {run_id!r} not found")
        if json_out:
            typer.echo(json.dumps(detail))
        else:
            _print_history_detail(detail)
        return
    rows = store.history(endpoint=endpoint, limit=limit)
    if json_out:
        typer.echo(json.dumps(rows))
        return
    for row in rows:
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
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print a deterministic plan without resolving the key or making endpoint "
            "requests (tiktoken encoding data may be fetched/cached on first use)"
        ),
    ),
    budget_usd: float = typer.Option(None, "--budget-usd", help="Positive recording cost cap"),
    timeout_s: float = typer.Option(
        60.0, "--timeout-s", help="Positive HTTP timeout in seconds"
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit exactly one machine-readable JSON object on stdout"
    ),
) -> None:
    """Record official-endpoint fingerprints into baselines/<id>.json (schema v2)."""
    vendor_key = vendor.lower()
    if vendor_key not in KNOWN_BASELINE_VENDORS:
        _abort(f"unknown vendor {vendor!r} (expected openai, anthropic, or generic)")
    if samples < 1 or samples > 10 or streams < 1 or streams > 5:
        _abort("--samples must be within 1..10 and --streams within 1..5")
    if budget_usd is not None:
        _positive(budget_usd, "--budget-usd")
    _positive(timeout_s, "--timeout-s")
    try:
        validate_baseline_endpoint(endpoint)
    except ValueError as exc:
        _abort(str(exc))
    plan_payload = _baseline_plan_payload(
        vendor=vendor_key,
        model=model,
        key_env=key_env,
        endpoint=endpoint,
        out=out,
        evidence_out=evidence_out,
        label=label,
        model_version=model_version,
        samples=samples,
        streams=streams,
        confirm_official=confirm_official,
        budget_usd=budget_usd,
        timeout_s=timeout_s,
    )
    if dry_run:
        if json_out:
            typer.echo(json.dumps({"dry_run": True, **plan_payload}))
        else:
            typer.echo(
                "dry-run: no key resolution, endpoint requests, or user files "
                "(tiktoken encoding data may be fetched/cached on first use)"
            )
            _print_baseline_preview(plan_payload)
        return

    # Preview before key resolution/client construction and therefore before
    # any paid endpoint request. JSON mode keeps stdout reserved for one object.
    _print_baseline_preview(plan_payload, err=json_out)
    api_key = _resolve_key(key_env)
    try:
        record = asyncio.run(
            record_baseline(
                vendor=vendor_key,
                model=model,
                api_key=api_key,
                key_env=key_env,
                endpoint=endpoint,
                out=out,
                label=label,
                model_version=model_version,
                samples=samples,
                streams=streams,
                evidence_root=evidence_out,
                confirmed_official=confirm_official,
                budget_usd=budget_usd,
                timeout_s=timeout_s,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _abort(f"baseline recording failed: {exc}")
    record_path = out / f"{record.baseline_id}.json"
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "dry_run": False,
                    "plan": plan_payload,
                    "baseline": record.model_dump(mode="json"),
                    "path": str(record_path),
                }
            )
        )
    else:
        typer.echo(
            f"recorded baseline {record.baseline_id} ({record.captured_at}) -> {record_path}"
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
