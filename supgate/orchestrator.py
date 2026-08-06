"""Run orchestration (build plan §5, §12): probe scheduling, concurrency
semaphore, budget enforcement, bundle assembly."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from pathlib import Path

import httpx

from supgate import __version__
from supgate.evidence import EvidenceWriter
from supgate.models import (
    SLA,
    BudgetTracker,
    CalibrationSnapshot,
    Domain,
    ProbeResult,
    RunBundle,
    SurfaceMap,
    Verdict,
    Veto,
)
from supgate.probes.base import RunContext
from supgate.registry import load_manifest_version, load_probes
from supgate.scoring import assurance, overall_score, score_domains

P0_IDS = {"p0.echo", "p0.models", "p0.error_contract"}


class Orchestrator:
    def __init__(
        self,
        *,
        concurrency: int = 10,
        timeout_s: float = 60.0,
        budget_usd: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if concurrency < 1 or concurrency > 50:
            raise ValueError("concurrency must be within 1..50 (build plan §5)")
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.budget_usd = budget_usd
        self.transport = transport

    async def run(
        self,
        *,
        endpoint: str,
        api_key: str,
        claimed_models: list[str],
        manifest_path: Path,
        mode: str = "adhoc",
        sla: SLA | None = None,
        out_dir: Path,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> RunBundle:
        if mode not in {"adhoc", "full"}:
            raise ValueError(f"invalid mode {mode!r}: expected 'adhoc' or 'full' (build plan §12)")
        run_id = _run_id()
        started = _now()
        evidence = EvidenceWriter(out_dir / "evidence", run_id)
        budget = BudgetTracker(budget_usd=budget_usd if budget_usd is not None else self.budget_usd)
        probes = load_probes(manifest_path)
        # P0 always runs first so the SurfaceMap feeds every skip rule (§10.1).
        probes = sorted(probes, key=lambda p: (p.id not in P0_IDS, p.id))

        semaphore = asyncio.Semaphore(self.concurrency)
        client = httpx.AsyncClient(transport=self.transport, timeout=self.timeout_s)
        surface = SurfaceMap()
        ctx = RunContext(
            endpoint=endpoint,
            api_key=api_key,
            model=model or (claimed_models[0] if claimed_models else "gpt-4o"),
            claimed_models=claimed_models,
            surface=surface,
            client=client,
            evidence=evidence,
            budget=budget,
        )
        results: list[ProbeResult] = []
        try:
            for probe in probes:
                async with semaphore:
                    results.append(await self._run_probe(probe, ctx, mode, budget))
        finally:
            await client.aclose()

        domain_scores = score_domains(results)
        overall = overall_score(domain_scores)
        vetoes = _vetoes(results)
        assurance_verdict = assurance(overall, domain_scores, vetoes, mode=mode)
        bundle = RunBundle(
            run_id=run_id,
            endpoint=endpoint,
            claimed_models=claimed_models,
            mode=mode,
            started_at=started,
            finished_at=_now(),
            versions={
                "supgate": __version__,
                "manifest": load_manifest_version(manifest_path),
                "baselines": "none (M2)",
            },
            sla=sla or SLA(),
            overall_score=overall,
            domain_scores=domain_scores,
            assurance=assurance_verdict,
            vetoes=vetoes,
            calibration=_calibration(results, surface),
            probes=results,
            transit={"hop_lower_bound": 1, "origin_class": "unknown"},
        )
        bundle_path = out_dir / f"{run_id}.json"
        bundle_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        return bundle

    async def _run_probe(
        self, probe, ctx: RunContext, mode: str, budget: BudgetTracker
    ) -> ProbeResult:
        if reason := probe.skip_reason(ctx.surface):
            return ProbeResult(
                probe_id=probe.id, domain=probe.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0, notes=[f"skipped: {reason}"],
            )
        if budget.blocked:
            return ProbeResult(
                probe_id=probe.id, domain=probe.domain, verdict=Verdict.WARN, score=0.0,
                successes=0, attempts=0,
                notes=["budget-blocked: per-run cost cap exhausted (§12)"],
            )
        if mode == "adhoc" and probe.domain in {Domain.D2, Domain.D8}:
            return ProbeResult(
                probe_id=probe.id, domain=probe.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0, notes=["skipped: adhoc mode excludes load/capability probes"],
            )
        result = await probe.run(ctx)
        result.evidence_ref = ctx.evidence.refs_for(probe.id)
        result.curl = ctx.evidence.curl_for(probe.id)
        return result


def _vetoes(results: list[ProbeResult]) -> list[Veto]:
    """M2 wires real veto signals (reverse identity, substitution, billing
    inflation, hidden origin). The shape is reserved now (§7)."""

    return []


def _calibration(results: list[ProbeResult], surface: SurfaceMap) -> CalibrationSnapshot:
    """Snapshot P0 self-check + discovered surface into the bundle (§13)."""

    return CalibrationSnapshot(
        p0_verdicts={p.probe_id: p.verdict.value for p in results if p.probe_id in P0_IDS},
        models_catalog=len(surface.models),
        claimed_present=surface.claimed_present,
        responses_api=surface.responses_api,
        messages_api=surface.messages_api,
        captured_at=_now(),
    )


def _run_id() -> str:
    return f"SUP-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(2).upper()}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def endpoint_dead(bundle: RunBundle) -> bool:
    """True when p0.echo failed — exit code 2 (§12: 'endpoint unreachable')."""

    echo = next((p for p in bundle.probes if p.probe_id == "p0.echo"), None)
    return echo is not None and echo.verdict == Verdict.FAIL


def summary(bundle: RunBundle) -> str:
    counts = {v: 0 for v in Verdict}
    for probe in bundle.probes:
        counts[probe.verdict] += 1
    overall = f"{bundle.overall_score:.1f}" if bundle.overall_score is not None else "n/a"
    return (
        f"run {bundle.run_id}  endpoint={bundle.endpoint}  mode={bundle.mode}\n"
        f"overall={overall}  assurance={bundle.assurance.level.value}  "
        f"pass={counts[Verdict.PASS]} warn={counts[Verdict.WARN]} fail={counts[Verdict.FAIL]} skip={counts[Verdict.SKIP]}"
    )
