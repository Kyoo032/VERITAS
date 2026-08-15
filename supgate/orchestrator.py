"""Run orchestration (build plan §5, §12): probe scheduling, concurrency
semaphore, budget enforcement, bundle assembly."""

from __future__ import annotations

import asyncio
import inspect
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from supgate.baselines import (
    BaselineError,
    BaselineMatch,
    BaselineRecord,
    BaselineStore,
    select_baseline,
)
from supgate.evidence import EvidenceWriter, redact_payload
from supgate.keyid import key_fingerprint
from supgate.models import (
    SLA,
    Authenticity,
    BaselineReference,
    BudgetTracker,
    CalibrationSnapshot,
    Domain,
    InvocationConfig,
    ProbeResult,
    RunBundle,
    SurfaceMap,
    Transit,
    Verdict,
    Veto,
)
from supgate.probes.base import RunContext
from supgate.probes.d4_fingerprint import (
    claimed_families_of,
    claimed_providers_of,
    provider_of_family,
)
from supgate.registry import load_manifest_version, load_probes
from supgate.runtime import runtime_versions
from supgate.scoring import assurance, overall_score, score_domains
from supgate.tokenizers import DEFAULT_MODEL

P0_IDS = {"p0.echo", "p0.models", "p0.error_contract"}
ProbeCompletionCallback = Callable[[int, int, ProbeResult], Awaitable[None] | None]


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
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.budget_usd = budget_usd
        self.transport = transport

    async def run(
        self,
        *,
        endpoint: str,
        api_key: str,
        key_env: str | None = None,
        claimed_models: list[str],
        manifest_path: Path,
        mode: str = "adhoc",
        sla: SLA | None = None,
        out_dir: Path,
        model: str | None = None,
        budget_usd: float | None = None,
        baseline_root: Path | None = None,
        baseline_id: str | None = None,
        allow_family: bool = False,
        allow_coarse: bool = False,
        on_probe_complete: ProbeCompletionCallback | None = None,
        continue_forensics: bool = False,
        invocation: InvocationConfig | dict[str, object] | None = None,
    ) -> RunBundle:
        if mode not in {"adhoc", "full"}:
            raise ValueError(f"invalid mode {mode!r}: expected 'adhoc' or 'full' (build plan §12)")
        if "?" in endpoint or "#" in endpoint:
            raise ValueError("endpoint must be a base URL without query/fragment")
        match = _resolve_selected_baseline(
            baseline_root=baseline_root,
            baseline_id=baseline_id,
            claimed_models=claimed_models,
            allow_family=allow_family,
            allow_coarse=allow_coarse,
        )
        selected: BaselineRecord | None = match.record if match else None
        run_id = _run_id()
        started = _now()
        fp = key_fingerprint(api_key)
        evidence = EvidenceWriter(out_dir / "evidence", run_id, key_fingerprint=fp)
        run_model = model or (claimed_models[0] if claimed_models else DEFAULT_MODEL)
        budget = BudgetTracker(
            budget_usd=budget_usd if budget_usd is not None else self.budget_usd,
            model=run_model,
        )
        probes = load_probes(manifest_path)
        # P0 always runs first so the SurfaceMap feeds every skip rule (§10.1).
        probes = sorted(probes, key=lambda p: (p.id not in P0_IDS, p.id))

        semaphore = asyncio.Semaphore(self.concurrency)
        client = httpx.AsyncClient(transport=self.transport, timeout=self.timeout_s)
        surface = SurfaceMap()
        # P0 verdict map (probe_id -> verdict value), maintained during the
        # loop and exposed on RunContext for prerequisite gating (§10.3).
        p0_verdicts: dict[str, str] = {}
        ctx = RunContext(
            endpoint=endpoint,
            api_key=api_key,
            model=run_model,
            claimed_models=claimed_models,
            surface=surface,
            client=client,
            evidence=evidence,
            budget=budget,
            selected_baseline=selected,
            p0_verdicts=p0_verdicts,
        )
        results: list[ProbeResult] = []
        total_probes = len(probes)
        completed_probes = 0

        async def notify(result: ProbeResult) -> None:
            """Publish one completion without letting observers affect the run."""

            nonlocal completed_probes
            completed_probes += 1
            if on_probe_complete is not None:
                try:
                    callback_result = on_probe_complete(
                        completed_probes, total_probes, result
                    )
                    if inspect.isawaitable(callback_result):
                        await callback_result
                except Exception:  # noqa: BLE001 - progress observers are non-critical
                    pass

        async def execute(probe) -> ProbeResult:
            try:
                # The client SLA reaches D2 goodput thresholds before any load
                # probe runs (docs/05 §6 U1: "client SLA overrides").
                if sla is not None and getattr(probe, "apply_sla", None) is not None:
                    probe.apply_sla(sla)
                async with semaphore:
                    result = await self._run_probe(probe, ctx, mode, budget)
            except Exception as exc:  # noqa: BLE001 - setup hooks are part of the probe boundary
                result = _probe_error_result(probe, ctx, exc)
            await notify(result)
            return result

        try:
            p0_probes = [probe for probe in probes if probe.id in P0_IDS]
            remaining = [probe for probe in probes if probe.id not in P0_IDS]
            fast_stopped = False
            for index, probe in enumerate(p0_probes):
                result = await execute(probe)
                p0_verdicts[probe.id] = result.verdict.value
                results.append(result)
                if (
                    probe.id == "p0.echo"
                    and result.verdict == Verdict.FAIL
                    and not continue_forensics
                ):
                    # p0.echo established that the endpoint is dead.  Preserve
                    # the manifest contract by emitting one explicit result for
                    # every unstarted probe, but issue no more endpoint calls.
                    for unstarted in [*p0_probes[index + 1 :], *remaining]:
                        skipped = _skip_result(
                            unstarted,
                            "fast-stopped: p0.echo is fail, not pass; "
                            "no further endpoint requests",
                        )
                        results.append(skipped)
                        await notify(skipped)
                    fast_stopped = True
                    break
            if not fast_stopped:
                results.extend(await asyncio.gather(*(execute(probe) for probe in remaining)))
        finally:
            await client.aclose()

        domain_scores = score_domains(results)
        overall = overall_score(domain_scores)
        vetoes = _vetoes(results, claimed_models)
        transit = _transit(results, vetoes)
        inconclusive, inconclusive_reason = _inconclusive(results)
        authenticity = _authenticity(results)
        assurance_verdict = assurance(overall, domain_scores, vetoes, mode=mode)
        effective_budget = budget_usd if budget_usd is not None else self.budget_usd
        invocation_config = InvocationConfig.model_validate(invocation) if invocation is not None else InvocationConfig(
            base_url=endpoint,
            key_env=key_env,
            models=list(claimed_models),
            mode=mode,
            out=str(out_dir),
            sla=sla or SLA(),
            budget_usd=effective_budget,
            concurrency=self.concurrency,
            baseline_dir=str(baseline_root) if baseline_root is not None else None,
            baseline_id=baseline_id,
            automatic_baseline=baseline_id is None,
            allow_family_baseline=allow_family,
            allow_coarse_baseline=allow_coarse,
            timeout_s=self.timeout_s,
            continue_forensics=continue_forensics,
        )
        bundle = RunBundle(
            run_id=run_id,
            endpoint=endpoint,
            claimed_models=claimed_models,
            key_env=key_env,
            key_fingerprint=fp,
            mode=mode,
            started_at=started,
            finished_at=_now(),
            schema=2,
            versions={
                **runtime_versions(),
                "manifest": load_manifest_version(manifest_path),
                "baselines": selected.baseline_id if selected else "none",
                "schema": 2,
            },
            invocation=invocation_config,
            sla=sla or SLA(),
            cost=budget.summary(),
            overall_score=overall,
            domain_scores=domain_scores,
            assurance=assurance_verdict,
            vetoes=vetoes,
            calibration=_calibration(results, surface),
            surface=surface,
            baseline=(
                BaselineReference(
                    baseline_id=selected.baseline_id,
                    matched_on=match.matched_on,
                    captured_at=selected.captured_at,
                )
                if match is not None
                else None
            ),
            probes=results,
            transit=transit,
            inconclusive=inconclusive,
            inconclusive_reason=inconclusive_reason,
            authenticity=authenticity,
        )
        # Endpoint-controlled metrics can echo request secrets. Redact the
        # complete artifact, including the exact runtime key, before returning
        # or persisting it so all downstream views inherit R1-R7.
        safe_payload = redact_payload(bundle.model_dump(mode="json"), (api_key,))
        bundle = RunBundle.model_validate(safe_payload)
        bundle_path = out_dir / f"{run_id}.json"
        bundle_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        return bundle

    async def _run_probe(
        self, probe, ctx: RunContext, mode: str, budget: BudgetTracker
    ) -> ProbeResult:
        if reason := probe.skip_reason(ctx.surface):
            return _skip_result(probe, reason)
        # D4 prerequisite gating (§10.3): every d4.* probe requires
        # p0.echo == pass; d4.self_report additionally requires
        # p0.models == pass. The gate runs before budget/mode checks so a
        # dead endpoint deterministically SKIPs the whole D4 family with
        # zero attempts and no requests. WARN is not PASS, so it gates too.
        if probe.id.startswith("d4.") and ctx.p0_verdicts.get("p0.echo") != Verdict.PASS.value:
            return _skip_result(
                probe,
                f"p0.echo is {ctx.p0_verdicts.get('p0.echo', 'unset')}, not pass — D4 requires p0.echo pass",
            )
        if probe.id == "d4.self_report" and ctx.p0_verdicts.get("p0.models") != Verdict.PASS.value:
            return _skip_result(
                probe,
                f"p0.models is {ctx.p0_verdicts.get('p0.models', 'unset')}, not pass — d4.self_report requires p0.models pass",
            )
        if budget.blocked:
            return ProbeResult(
                probe_id=probe.id, domain=probe.domain, verdict=Verdict.WARN, score=0.0,
                weight=probe.weight, successes=0, attempts=0,
                notes=["budget-blocked: per-run cost cap exhausted (§12)"],
            )
        if mode == "adhoc" and probe.domain in {Domain.D2, Domain.D8}:
            return ProbeResult(
                probe_id=probe.id, domain=probe.domain, verdict=Verdict.SKIP, score=0.0,
                weight=probe.weight, successes=0, attempts=0, notes=["skipped: adhoc mode excludes load/capability probes"],
            )
        try:
            result = await probe.run(ctx)
        except Exception as exc:  # noqa: BLE001 - one malformed provider response must not abort the run
            result = _probe_error_result(probe, ctx, exc)
        result.weight = probe.weight
        result.evidence_ref = ctx.evidence.refs_for(probe.id)
        result.curl = ctx.evidence.curl_for(probe.id)
        return result


def _probe_error_result(probe, ctx: RunContext, exc: Exception) -> ProbeResult:
    refs = ctx.evidence.refs_for(probe.id)
    return ProbeResult(
        probe_id=probe.id,
        domain=probe.domain,
        verdict=Verdict.FAIL,
        score=0.0,
        weight=probe.weight,
        successes=0,
        attempts=max(1, len(refs)),
        notes=["probe raised an unexpected error; run continued and the bundle is incomplete for this probe"],
        error=f"{type(exc).__name__}: {exc}",
        evidence_ref=refs,
        curl=ctx.evidence.curl_for(probe.id),
    )


def _skip_result(probe, reason: str) -> ProbeResult:
    """SKIP with zero attempts; shared by skip_reason and D4 prerequisite gates."""

    return ProbeResult(
        probe_id=probe.id, domain=probe.domain, verdict=Verdict.SKIP, score=0.0,
        weight=probe.weight, successes=0, attempts=0, notes=[f"skipped: {reason}"],
    )


def _resolve_selected_baseline(
    *,
    baseline_root: Path | None,
    baseline_id: str | None,
    claimed_models: list[str],
    allow_family: bool,
    allow_coarse: bool,
) -> BaselineMatch | None:
    """Pick the run's baseline record (docs/08 §10.2) or None.

    An explicit ``baseline_id`` loads exactly that record and aborts with
    :class:`BaselineError` when it is missing, malformed, or incompatible
    with the claimed models under the allowed tiers. Auto-selection scans
    the store and matches exact version-pinned records only by default;
    family/coarse tiers are opt-in; malformed files are skipped; no match
    is allowed and yields None.
    """

    store = BaselineStore(baseline_root)
    if baseline_id is not None:
        record = store.get(baseline_id)
        if record is None:
            raise BaselineError(f"no baseline {baseline_id!r} in {store.root}")
        match = select_baseline(
            [record], claimed_models, allow_family=allow_family, allow_coarse=allow_coarse
        )
        if match is None:
            raise BaselineError(
                f"baseline {baseline_id!r} does not match claimed models {claimed_models}"
            )
        return match
    records, _errors = store.scan()
    return select_baseline(
        records, claimed_models, allow_family=allow_family, allow_coarse=allow_coarse
    )


def _vetoes(results: list[ProbeResult], claimed_models: list[str]) -> list[Veto]:
    """M2 veto synthesis (docs/06 §1.5, docs/08 §8.1).

    Exactly four codes, each requiring the documented corroboration — a
    single probe, prompt size, or marker never vetoes (F == 2 rotation,
    canary tamper, hop headers alone, model echo, and one inflated size are
    all evidence, not vetoes). Details describe the observed measurement as
    consistent/inconsistent with the claim, never an identity assertion.
    """

    by_id = {p.probe_id: p for p in results}
    vetoes: list[Veto] = []

    recount = by_id.get("d4.recount_deviation")
    recount_confirmed = _recount_confirmed(recount)
    if recount_confirmed is not None:
        vetoes.append(recount_confirmed)

    hidden = _hidden_origin(by_id, recount_confirmed is not None)
    if hidden is not None:
        vetoes.append(hidden)

    reverse = _reverse_identity(by_id, claimed_models)
    if reverse is not None:
        vetoes.append(reverse)

    substitution = _substitution(by_id)
    if substitution is not None:
        vetoes.append(substitution)

    return vetoes


def _recount_confirmed(recount: ProbeResult | None) -> Veto | None:
    """billing_inflation: recount FAIL + every prompt size above the fail
    gate + mean above the gate (docs/08 §8.1: never a single size/sample).

    The confirmation is only valid when all three short/medium/long
    non-cached measurements were taken and each is above the fail gate: the
    probe's ``all_sizes_above_fail_gate`` flag AND a full three-sample
    ``per_size_deviation_pct`` list are both required, so a cached-excluded
    or otherwise incomplete sample set can never confirm a veto.
    """

    if recount is None or recount.verdict != Verdict.FAIL:
        return None
    metrics = recount.metrics.get("recount_deviation", {})
    mean_dev = metrics.get("mean_deviation_pct")
    fail_gate = metrics.get("fail_gate_pct")
    if not metrics.get("all_sizes_above_fail_gate"):
        return None
    per_size = metrics.get("per_size_deviation_pct")
    if not isinstance(per_size, list) or len(per_size) != 3:
        return None
    if not isinstance(mean_dev, (int, float)) or not isinstance(fail_gate, (int, float)):
        return None
    if not mean_dev > fail_gate:
        return None
    return Veto(
        code="billing_inflation",
        detail=(
            f"mean recount deviation +{mean_dev:.1f}% exceeds the calibrated FAIL gate "
            f"{fail_gate:.1f}% across every prompt size (short/medium/long) — "
            "inconsistent with the claimed model's encoding"
        ),
    )


def _hidden_origin(by_id: dict[str, ProbeResult], recount_confirmed: bool) -> Veto | None:
    """hidden_origin: hop markers + self-report contradiction in both
    samples, or a large stable wrap offset alongside confirmed recount
    over-reporting (docs/06 §1.5)."""

    headers = by_id.get("d4.headers_diff")
    self_report = by_id.get("d4.self_report")
    if headers is not None and self_report is not None:
        hop_present = headers.metrics.get("headers", {}).get("hop_present")
        contradiction_both = self_report.metrics.get("self_report", {}).get("contradiction_both")
        if hop_present and contradiction_both:
            return Veto(
                code="hidden_origin",
                detail=(
                    "hop markers observed in response headers while the self-report "
                    "contradicts the claim in both samples — consistent with a relay "
                    "hiding its origin"
                ),
            )

    wrap = by_id.get("d4.wrap_offset")
    if wrap is None or wrap.verdict != Verdict.FAIL or not recount_confirmed:
        return None
    metrics = wrap.metrics.get("wrap_offset", {})
    mean_offset = metrics.get("mean_offset_tokens")
    fail_gate = metrics.get("fail_gate_tokens")
    if (
        metrics.get("offset_stable")
        and isinstance(mean_offset, (int, float))
        and isinstance(fail_gate, (int, float))
        and mean_offset > fail_gate
    ):
        return Veto(
            code="hidden_origin",
            detail=(
                f"stable hidden prompt wrapper of {mean_offset:.1f} tokens (above the "
                f"{fail_gate:.1f}-token fail gate) alongside confirmed recount "
                "over-reporting — consistent with a relay hiding its origin"
            ),
        )
    return None


def _reverse_identity(by_id: dict[str, ProbeResult], claimed_models: list[str]) -> Veto | None:
    """reverse_identity: one stable official id family inconsistent with
    every claimed family, and the self-report names that same different
    provider (docs/06 §1.5: d4.id_prefix + d4.self_report agree).

    With a mixed claim (e.g. ``['gpt-4o', 'claude-3-5-sonnet']``) the
    observed provider must be inconsistent with *every* claimed family —
    a ``msg_``/anthropic response is consistent with the claimed claude
    model and never vetoes.
    """

    id_prefix = by_id.get("d4.id_prefix")
    self_report = by_id.get("d4.self_report")
    if id_prefix is None or self_report is None or id_prefix.verdict == Verdict.FAIL:
        return None
    metrics = id_prefix.metrics.get("id_prefix", {})
    families = metrics.get("families")
    if not isinstance(families, list) or len(families) != 1:
        return None
    family = families[0]
    provider = provider_of_family(family)
    claimed_families = claimed_families_of(claimed_models)
    claimed_providers = claimed_providers_of(claimed_models)
    if not provider or not claimed_providers or provider in claimed_providers:
        return None
    hits = self_report.metrics.get("self_report", {}).get("family_hits_union")
    if not isinstance(hits, list) or provider not in hits:
        return None
    if len(claimed_families) == 1:
        claimed_desc = f"claimed {claimed_families[0]!r} ({provider_of_family(claimed_families[0])}) family"
    else:
        claimed_desc = (
            f"claimed families {claimed_families} (providers {sorted(claimed_providers)})"
        )
    return Veto(
        code="reverse_identity",
        detail=(
            f"stable id family {family!r} maps to {provider}, inconsistent with the "
            f"{claimed_desc}, and the self-report independently names {provider}"
        ),
    )


def _substitution(by_id: dict[str, ProbeResult]) -> Veto | None:
    """substitution: rotation FAIL (F >= 3) with >= 2 distinct mapped
    official providers (docs/06 §1.5: F == 2 is evidence, never a veto)."""

    rotation = by_id.get("d4.rotation")
    if rotation is None or rotation.verdict != Verdict.FAIL:
        return None
    metrics = rotation.metrics.get("rotation", {})
    family_count = metrics.get("F")
    providers = metrics.get("providers")
    if not isinstance(family_count, int) or family_count < 3:
        return None
    if not isinstance(providers, list) or len(providers) < 2:
        return None
    return Veto(
        code="substitution",
        detail=(
            f"rotation shows {family_count} distinct response families mapping to "
            f"official providers {sorted(providers)} — inconsistent with a single "
            "claimed upstream"
        ),
    )


def _transit(results: list[ProbeResult], vetoes: list[Veto]) -> Transit:
    """docs/08 §9: conservative hop analysis from d4.headers_diff metrics.

    hop_lower_bound = 1 + max per-response (``via`` entries +
    ``x-forwarded-for`` hops + ``x-proxy`` + ``cf-ray``). Any hop marker
    classifies the path as a gateway; a reliable headers probe with no
    markers classifies it official; absent metrics leave it unknown. A
    ``hidden_origin`` veto forces gateway (invariant V8).
    """

    headers = next((p for p in results if p.probe_id == "d4.headers_diff"), None)
    max_hops = 0
    hints: set[str] = set()
    reliable = False
    if headers is not None:
        per_response = headers.metrics.get("headers", {}).get("per_response")
        responses = per_response if isinstance(per_response, list) else []
        reliable = bool(responses)
        for response in responses:
            hop_markers = response.get("hop_markers")
            if not isinstance(hop_markers, dict):
                continue
            hints.update(f"{name}: {value}" for name, value in hop_markers.items())
            max_hops = max(max_hops, _hop_count(hop_markers))

    if any(veto.code == "hidden_origin" for veto in vetoes) or hints:
        origin_class = "gateway"
    elif reliable:
        origin_class = "official"
    else:
        origin_class = "unknown"
    return Transit(
        hop_lower_bound=1 + max_hops,
        origin_class=origin_class,
        hop_hints=sorted(hints),
    )


def _hop_count(markers: dict[str, str]) -> int:
    """Hop evidence in one response (docs/08 §9): ``via`` entries +
    ``x-forwarded-for`` hops + ``x-proxy`` presence + ``cf-ray`` presence."""

    hops = 0
    for name, value in markers.items():
        if name in {"via", "x-forwarded-for"}:
            hops += len([part for part in value.split(",") if part.strip()])
        elif name in {"x-proxy", "cf-ray"}:
            hops += 1
    return hops


# Persistent-retry WARN markers (docs/06 §1.3): the shared retry policy
# emits exactly these notes when 429/5xx survive one backoff retry.
_RETRY_WARN_MARKERS = ("rate-limited", "after retry", "server error")


def _inconclusive(results: list[ProbeResult]) -> tuple[bool, str | None]:
    """docs/08 §3: a run is inconclusive when it cannot reach a confident
    conclusion — p0.echo not pass, p0.models failed, or > 25% of attempted
    probes degraded by persistent retry WARNs (budget-blocked excluded)."""

    echo = next((p for p in results if p.probe_id == "p0.echo"), None)
    if echo is not None and echo.verdict != Verdict.PASS:
        return True, f"p0.echo is {echo.verdict.value}, not pass — no reliable surface"
    models = next((p for p in results if p.probe_id == "p0.models"), None)
    if models is not None and models.verdict == Verdict.FAIL:
        return True, "p0.models failed — claimed model surface unverified"

    attempted = [p for p in results if p.verdict != Verdict.SKIP]
    retry_warns = [p for p in attempted if _is_persistent_retry_warn(p)]
    if attempted and len(retry_warns) / len(attempted) > 0.25:
        return (
            True,
            f"{len(retry_warns)} of {len(attempted)} attempted probes degraded by "
            "persistent 429/5xx after retry (> 25%)",
        )
    return False, None


def _is_persistent_retry_warn(result: ProbeResult) -> bool:
    if result.verdict != Verdict.WARN or result.attempts == 0:
        return False
    text = " ".join(result.notes)
    if "budget-blocked" in text:
        return False
    return any(marker in text for marker in _RETRY_WARN_MARKERS)


# docs/06 §1.4: D4 signal families -> probe ids.
_SIGNAL_FAMILY_PROBES: dict[str, tuple[str, ...]] = {
    "identity_consistency": ("d4.headers_diff", "d4.id_prefix", "d4.self_report"),
    "generation_integrity": ("d4.model_echo", "d4.canary_echo"),
    "relay_timing": ("d4.sse_timing", "d4.rotation"),
    "billing_transparency": (
        "d4.usage_presence",
        "d4.recount_deviation",
        "d4.wrap_offset",
        "d4.reasoning_cache_fields",
    ),
}

# WARNs that carry no adverse evidence: structural-only runs (no baseline)
# and transport degradation are conditions of the run, not signals of
# tampering or substitution.
_NON_ADVERSE_WARN_MARKERS = (
    "no baseline",
    "structural checks only",
    "budget-blocked",
    "rate-limited",
    "after retry",
    "server error",
)


def _adverse(result: ProbeResult) -> bool:
    """Adverse D4 evidence: FAIL, or WARN with evidence notes (never a
    structural-only or transport-degraded warn)."""

    if result.verdict == Verdict.FAIL:
        return True
    if result.verdict != Verdict.WARN:
        return False
    text = " ".join(result.notes)
    return not any(marker in text for marker in _NON_ADVERSE_WARN_MARKERS)


def _family_of_probe(probe_id: str) -> str | None:
    for family, probe_ids in _SIGNAL_FAMILY_PROBES.items():
        if probe_id in probe_ids:
            return family
    return None


def _authenticity(results: list[ProbeResult]) -> Authenticity:
    """docs/08 §3 + docs/06 §1.4: report-level D4 synthesis.

    ``confirmed_tampering`` requires canary template/asymmetry corroborated
    by an independent headers/wrap/recount signal; ``suspected_substitution``
    requires rotation F >= 2 plus adverse evidence from another signal
    family; ``consistent`` requires >= 2 exercised families with no adverse
    non-skip D4 results; otherwise inconclusive. Confidence is the exercised
    clean-family ratio for consistent, 0.8 for corroborated
    suspected/confirmed, 0 for inconclusive (deterministic, bounded [0, 1]).
    """

    by_id = {p.probe_id: p for p in results}
    exercised: list[str] = []
    adverse_families: list[str] = []
    for family, probe_ids in _SIGNAL_FAMILY_PROBES.items():
        family_results = [p for pid in probe_ids if (p := by_id.get(pid)) is not None]
        if any(p.verdict != Verdict.SKIP for p in family_results):
            exercised.append(family)
        if any(_adverse(p) for p in family_results):
            adverse_families.append(family)

    canary = by_id.get("d4.canary_echo")
    canary_tamper = False
    if canary is not None:
        metrics = canary.metrics.get("canary_echo", {})
        canary_tamper = bool(metrics.get("template") or metrics.get("asymmetry"))
    if canary_tamper:
        corroborating = [
            pid
            for pid in ("d4.headers_diff", "d4.wrap_offset", "d4.recount_deviation")
            if (p := by_id.get(pid)) is not None and p.verdict == Verdict.FAIL
        ]
        if corroborating:
            families = sorted({"generation_integrity", *(_family_of_probe(pid) for pid in corroborating)})
            return Authenticity(
                verdict="confirmed_tampering",
                confidence=0.8,
                signal_families=[f for f in families if f is not None],
            )

    rotation = by_id.get("d4.rotation")
    if rotation is not None:
        rotation_f = rotation.metrics.get("rotation", {}).get("F")
        other_adverse = sorted(f for f in adverse_families if f != "relay_timing")
        if isinstance(rotation_f, int) and rotation_f >= 2 and other_adverse:
            return Authenticity(
                verdict="suspected_substitution",
                confidence=0.8,
                signal_families=sorted({"relay_timing", *other_adverse}),
            )

    if len(exercised) >= 2 and not adverse_families:
        clean = [f for f in exercised if f not in adverse_families]
        return Authenticity(
            verdict="consistent",
            confidence=len(clean) / len(exercised),
            signal_families=sorted(exercised),
        )

    return Authenticity()


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
    baseline_id = bundle.baseline.baseline_id if bundle.baseline is not None else "none"
    reason = bundle.inconclusive_reason or "none"
    budget_blocked = sum(
        any("budget-blocked" in note for note in probe.notes) for probe in bundle.probes
    )
    failed_ids = [probe.probe_id for probe in bundle.probes if probe.verdict == Verdict.FAIL]
    failed = ",".join(failed_ids) if failed_ids else "none"
    return (
        f"run {bundle.run_id}  endpoint={bundle.endpoint}  mode={bundle.mode}\n"
        f"overall={overall}  assurance={bundle.assurance.level.value}  "
        f"pass={counts[Verdict.PASS]} warn={counts[Verdict.WARN]} "
        f"fail={counts[Verdict.FAIL]} skip={counts[Verdict.SKIP]}\n"
        f"baseline={baseline_id}  inconclusive={str(bundle.inconclusive).lower()}  "
        f"reason={reason}  estimated_usd=${bundle.cost.estimated_usd:.6f}  "
        f"budget_blocked={budget_blocked}  failed_probe_ids={failed}"
    )
