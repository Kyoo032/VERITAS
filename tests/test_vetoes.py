"""M2 synthesis: vetoes, transit, inconclusive, authenticity (docs/06 §1.4-§1.5,
docs/08 §3, §8-§9). Synthetic ProbeResults for every positive/guard; exactly
one real orchestrator run to keep runtime bounded."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from supgate.models import Domain, ProbeResult, Verdict, Veto
from supgate.orchestrator import (
    Orchestrator,
    _authenticity,
    _inconclusive,
    _transit,
    _vetoes,
)


def _r(
    probe_id: str,
    *,
    verdict: Verdict = Verdict.PASS,
    metrics: dict | None = None,
    attempts: int = 1,
    notes: list[str] | None = None,
) -> ProbeResult:
    return ProbeResult(
        probe_id=probe_id,
        domain=Domain.D4,
        verdict=verdict,
        score=100.0 if verdict == Verdict.PASS else 0.0,
        attempts=attempts,
        metrics=metrics or {},
        notes=notes or [],
    )


# --- metric builders mirroring the probe contracts (docs/06 §4-§5) -----------


def _recount(deviation: float = 60.0, fail_gate: float = 15.0, all_sizes: bool = True) -> dict:
    sizes = [deviation] * 3 if all_sizes else [deviation]
    return {
        "recount_deviation": {
            "mean_deviation_pct": deviation,
            "fail_gate_pct": fail_gate,
            "all_sizes_above_fail_gate": all_sizes,
            "per_size_deviation_pct": sizes,
        }
    }


def _headers(hop: bool = False, markers: list[dict] | None = None) -> dict:
    per_response = markers if markers is not None else [{"hop_markers": {}} for _ in range(4)]
    return {"headers": {"hop_present": hop, "per_response": per_response}}


def _self_report(contradiction_both: bool = False, hits: list[str] | None = None) -> dict:
    return {
        "self_report": {
            "contradiction_both": contradiction_both,
            "family_hits_union": hits or [],
        }
    }


def _id_prefix(families: list[str]) -> dict:
    return {"id_prefix": {"families": families}}


def _wrap(mean: float = 64.0, gate: float = 32.0, stable: bool = True) -> dict:
    return {"wrap_offset": {"mean_offset_tokens": mean, "fail_gate_tokens": gate, "offset_stable": stable}}


def _rotation(f: int = 1, providers: list[str] | None = None) -> dict:
    providers = providers or []
    return {"rotation": {"F": f, "providers": providers, "distinct_official_providers": len(providers)}}


def _canary(template: bool = False, asymmetry: bool = False) -> dict:
    return {"canary_echo": {"template": template, "asymmetry": asymmetry}}


def _codes(vetoes) -> list[str]:
    return [v.code for v in vetoes]


# --- billing_inflation --------------------------------------------------------


def test_billing_inflation_veto_on_confirmed_recount():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.FAIL, metrics=_recount()),
    ]
    vetoes = _vetoes(results, ["gpt-4o"])
    assert _codes(vetoes) == ["billing_inflation"]
    assert "mean recount deviation +60.0%" in vetoes[0].detail
    assert "inconsistent with the claimed model's encoding" in vetoes[0].detail


def test_one_inflated_prompt_size_never_vetoes():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.FAIL, metrics=_recount(all_sizes=False)),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_recount_warn_never_vetoes():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.WARN, metrics=_recount()),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_recount_fail_below_gate_never_vetoes():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.FAIL, metrics=_recount(deviation=10.0, all_sizes=True)),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_recount_two_measurements_never_vetoes_even_with_flag():
    # Defense-in-depth: even a metric set claiming all_sizes_above_fail_gate
    # cannot confirm the veto when fewer than three non-cached measurements
    # exist (per_size_deviation_pct has only 2 entries — the veto requires
    # exactly all three short/medium/long sizes, docs/06 §1.5).
    metrics = {
        "recount_deviation": {
            "mean_deviation_pct": 60.0,
            "fail_gate_pct": 15.0,
            "all_sizes_above_fail_gate": True,
            "per_size_deviation_pct": [60.0, 60.0],
        }
    }
    results = [_r("d4.recount_deviation", verdict=Verdict.FAIL, metrics=metrics)]
    assert _vetoes(results, ["gpt-4o"]) == []


# --- hidden_origin ------------------------------------------------------------


def test_hidden_origin_hop_markers_plus_self_report_contradiction():
    results = [
        _r("d4.headers_diff", verdict=Verdict.WARN, metrics=_headers(hop=True)),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(contradiction_both=True)),
    ]
    vetoes = _vetoes(results, ["gpt-4o"])
    assert _codes(vetoes) == ["hidden_origin"]
    assert "hop markers observed" in vetoes[0].detail


def test_headers_alone_never_vetoes():
    results = [_r("d4.headers_diff", verdict=Verdict.WARN, metrics=_headers(hop=True))]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_self_report_contradiction_alone_never_vetoes():
    results = [_r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(contradiction_both=True))]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_empty_self_report_terms_never_feed_hidden_origin():
    # A baseline with missing/empty self-report terms leaves contradiction_both
    # False (unknown, not contradiction), so hop markers plus an unjudgeable
    # report can never combine into a hidden_origin veto.
    results = [
        _r("d4.headers_diff", verdict=Verdict.WARN, metrics=_headers(hop=True)),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(contradiction_both=False)),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_hidden_origin_wrap_fail_with_confirmed_recount():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.FAIL, metrics=_recount()),
        _r("d4.wrap_offset", verdict=Verdict.FAIL, metrics=_wrap(mean=64.0, gate=32.0)),
    ]
    vetoes = _vetoes(results, ["gpt-4o"])
    assert _codes(vetoes) == ["billing_inflation", "hidden_origin"]
    hidden = next(v for v in vetoes if v.code == "hidden_origin")
    assert "hidden prompt wrapper of 64.0 tokens" in hidden.detail


def test_wrap_fail_without_confirmed_recount_never_vetoes():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.PASS, metrics=_recount()),
        _r("d4.wrap_offset", verdict=Verdict.FAIL, metrics=_wrap(mean=64.0, gate=32.0)),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_wrap_warn_never_vetoes_even_with_confirmed_recount():
    results = [
        _r("d4.recount_deviation", verdict=Verdict.FAIL, metrics=_recount()),
        _r("d4.wrap_offset", verdict=Verdict.WARN, metrics=_wrap(mean=12.0, gate=32.0)),
    ]
    # recount confirms billing_inflation, but the wrap WARN adds no
    # hidden_origin corroboration (docs/06 §5.3: WARN is not a wrapper signal).
    assert _codes(_vetoes(results, ["gpt-4o"])) == ["billing_inflation"]


# --- reverse_identity ---------------------------------------------------------


def test_reverse_identity_veto_on_stable_family_plus_self_report_hit():
    results = [
        _r("d4.id_prefix", verdict=Verdict.WARN, metrics=_id_prefix(["gen-"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["gemini"])),
    ]
    vetoes = _vetoes(results, ["gpt-4o"])
    assert _codes(vetoes) == ["reverse_identity"]
    assert "'gen-' maps to gemini" in vetoes[0].detail
    assert "claimed 'chatcmpl-' (openai) family" in vetoes[0].detail


def test_reverse_identity_requires_self_report_hit():
    results = [
        _r("d4.id_prefix", verdict=Verdict.WARN, metrics=_id_prefix(["gen-"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["anthropic"])),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_reverse_identity_requires_different_provider():
    results = [
        _r("d4.id_prefix", verdict=Verdict.WARN, metrics=_id_prefix(["msg_"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["anthropic"])),
    ]
    # claimed gpt-4o -> chatcmpl- (openai); observed msg_ (anthropic) with a
    # matching self-report hit WOULD veto, so pick a claim that matches the
    # observed family to prove the different-provider guard.
    assert _vetoes(results, ["claude-3-5-sonnet"]) == []
    assert _codes(_vetoes(results, ["gpt-4o"])) == ["reverse_identity"]


def test_reverse_identity_requires_single_stable_family():
    results = [
        _r("d4.id_prefix", verdict=Verdict.FAIL, metrics=_id_prefix(["chatcmpl-", "gen-"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["gemini"])),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_reverse_identity_unknown_claimed_family_never_vetoes():
    results = [
        _r("d4.id_prefix", verdict=Verdict.PASS, metrics=_id_prefix(["gen-"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["gemini"])),
    ]
    assert _vetoes(results, ["mystery-model"]) == []


def test_reverse_identity_mixed_claim_with_matching_family_never_vetoes():
    # A ['gpt-4o', 'claude-3-5-sonnet'] claim observed as msg_ (anthropic)
    # with an anthropic self-report is consistent with the claimed claude
    # model: reverse_identity may only veto when the observed provider is
    # inconsistent with EVERY known claimed family.
    results = [
        _r("d4.id_prefix", verdict=Verdict.WARN, metrics=_id_prefix(["msg_"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["anthropic"])),
    ]
    assert _vetoes(results, ["gpt-4o", "claude-3-5-sonnet"]) == []
    # The same claim with the observed provider flipped to an unclaimed one
    # (gemini) still vetoes — the guard is per-provider, not per-claim.
    results = [
        _r("d4.id_prefix", verdict=Verdict.WARN, metrics=_id_prefix(["gen-"])),
        _r("d4.self_report", verdict=Verdict.WARN, metrics=_self_report(hits=["gemini"])),
    ]
    vetoes = _vetoes(results, ["gpt-4o", "claude-3-5-sonnet"])
    assert _codes(vetoes) == ["reverse_identity"]
    assert "claimed families ['chatcmpl-', 'msg_']" in vetoes[0].detail


# --- substitution --------------------------------------------------------------


def test_substitution_veto_on_rotation_fail_with_two_official_providers():
    results = [
        _r("d4.rotation", verdict=Verdict.FAIL, metrics=_rotation(f=3, providers=["openai", "anthropic"])),
    ]
    vetoes = _vetoes(results, ["gpt-4o"])
    assert _codes(vetoes) == ["substitution"]
    assert "3 distinct response families" in vetoes[0].detail
    assert "official providers ['anthropic', 'openai']" in vetoes[0].detail


def test_rotation_f_two_never_vetoes():
    results = [
        _r("d4.rotation", verdict=Verdict.WARN, metrics=_rotation(f=2, providers=["openai", "anthropic"])),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_rotation_fail_one_provider_never_vetoes():
    results = [
        _r("d4.rotation", verdict=Verdict.FAIL, metrics=_rotation(f=3, providers=["openai"])),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_rotation_fail_custom_families_never_vetoes():
    results = [
        _r("d4.rotation", verdict=Verdict.FAIL, metrics=_rotation(f=3, providers=[])),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


# --- canary / model echo never veto --------------------------------------------


def test_canary_tamper_alone_never_vetoes():
    results = [
        _r("d4.canary_echo", verdict=Verdict.FAIL, metrics=_canary(template=True)),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


def test_model_echo_alone_never_vetoes():
    results = [
        _r("d4.model_echo", verdict=Verdict.WARN, notes=["echo names a known family outside the claim"]),
    ]
    assert _vetoes(results, ["gpt-4o"]) == []


# --- transit (docs/08 §9) ------------------------------------------------------


def test_transit_official_for_clean_headers_probe():
    results = [_r("d4.headers_diff", metrics=_headers())]
    transit = _transit(results, [])
    assert transit.hop_lower_bound == 1
    assert transit.origin_class == "official"
    assert transit.hop_hints == []


def test_transit_gateway_with_hop_markers_and_hop_count():
    markers = [
        {"hop_markers": {"via": "1.1 proxy-a, 1.0 proxy-b", "x-forwarded-for": "10.0.0.1, 10.0.0.2"}},
        {"hop_markers": {"x-proxy": "yes", "cf-ray": "abc123"}},
        {"hop_markers": {}},
        {"hop_markers": {"x-served-by": "cache-ewr1"}},
    ]
    results = [_r("d4.headers_diff", verdict=Verdict.WARN, metrics=_headers(markers=markers))]
    transit = _transit(results, [])
    # max hops: response 0 = 2 via + 2 xff = 4; response 1 = 1 + 1 = 2.
    assert transit.hop_lower_bound == 5
    assert transit.origin_class == "gateway"
    assert transit.hop_hints == [
        "cf-ray: abc123",
        "via: 1.1 proxy-a, 1.0 proxy-b",
        "x-forwarded-for: 10.0.0.1, 10.0.0.2",
        "x-proxy: yes",
        "x-served-by: cache-ewr1",
    ]


def test_transit_unknown_without_headers_metrics():
    assert _transit([], []).origin_class == "unknown"
    assert _transit([_r("d4.headers_diff", attempts=0, verdict=Verdict.SKIP)], []).origin_class == "unknown"


def test_transit_hidden_origin_veto_forces_gateway():
    results = [_r("d4.headers_diff", metrics=_headers())]
    vetoes = [Veto(code="hidden_origin", detail="hop markers + self-report contradiction")]
    transit = _transit(results, vetoes)
    assert transit.origin_class == "gateway"
    assert transit.hop_lower_bound == 1


# --- inconclusive (docs/08 §3) --------------------------------------------------


def _warn(probe_id: str, note: str) -> ProbeResult:
    return _r(probe_id, verdict=Verdict.WARN, attempts=1, notes=[note])


def test_inconclusive_echo_fail():
    echo = _r("p0.echo", verdict=Verdict.FAIL, attempts=1)
    inconclusive, reason = _inconclusive([echo])
    assert inconclusive is True
    assert "p0.echo is fail" in reason


def test_inconclusive_models_fail():
    models = _r("p0.models", verdict=Verdict.FAIL, attempts=1)
    inconclusive, reason = _inconclusive([_r("p0.echo"), models])
    assert inconclusive is True
    assert "p0.models failed" in reason


def test_inconclusive_over_quarter_retry_warns():
    results = [
        _r("p0.echo"),
        _warn("d6.chat.basic", "d6.chat.basic: rate-limited (429) after retry — Warn per §10"),
        _warn("d4.rotation", "d4.rotation: server error (status 500) after retry — Warn per §10"),
        _r("d4.headers_diff"),
        _r("d4.id_prefix"),
        _r("d4.recount_deviation"),
        _r("d4.wrap_offset"),
    ]
    inconclusive, reason = _inconclusive(results)
    assert inconclusive is True
    assert "2 of 7" in reason
    assert "after retry" in reason


def test_retry_warns_at_threshold_are_not_inconclusive():
    results = [
        _r("p0.echo"),
        _warn("d6.chat.basic", "d6.chat.basic: rate-limited (429) after retry — Warn per §10"),
        _r("d4.headers_diff"),
        _r("d4.id_prefix"),
    ]
    inconclusive, _ = _inconclusive(results)  # 1 of 4 = 25%, not > 25%
    assert inconclusive is False


def test_budget_blocked_warns_excluded_from_threshold():
    results = [
        _r("p0.echo"),
        _warn("d4.rotation", "budget-blocked: per-run cost cap exhausted (§12)"),
        _warn("d4.rotation", "budget-blocked: per-run cost cap exhausted (§12)"),
        _r("d4.headers_diff"),
    ]
    inconclusive, _ = _inconclusive(results)
    assert inconclusive is False


def test_clean_run_not_inconclusive():
    results = [_r("p0.echo"), _r("p0.models"), _r("d4.headers_diff")]
    inconclusive, reason = _inconclusive(results)
    assert inconclusive is False
    assert reason is None


# --- authenticity (docs/06 §1.4, docs/08 §3) -----------------------------------


def test_authenticity_consistent_needs_two_families():
    # A single exercised signal family can never be "consistent" (docs/08 §3:
    # at least two D4 signal families must be exercised).
    results = [
        _r("d4.headers_diff", metrics=_headers()),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "inconclusive"
    assert auth.confidence == 0.0
    results = [
        _r("d4.rotation", metrics=_rotation(f=1)),
    ]
    assert _authenticity(results).verdict == "inconclusive"


def test_authenticity_consistent_with_two_clean_families():
    results = [
        _r("d4.headers_diff", metrics=_headers()),
        _r("d4.canary_echo", metrics=_canary()),
        _r("d4.rotation", metrics=_rotation(f=1)),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "consistent"
    assert auth.confidence == 1.0
    assert auth.signal_families == ["generation_integrity", "identity_consistency", "relay_timing"]


def test_authenticity_adverse_warn_blocks_consistent():
    results = [
        _r("d4.headers_diff", metrics=_headers()),
        _r("d4.model_echo", verdict=Verdict.WARN, notes=["echo matches no claimed model"]),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "inconclusive"
    assert auth.confidence == 0.0


def test_authenticity_structural_warn_not_adverse():
    results = [
        _r("d4.headers_diff", metrics=_headers()),
        _r("d4.self_report", verdict=Verdict.WARN, notes=["no baseline — structural run cannot PASS or FAIL"]),
        _r("d4.canary_echo", metrics=_canary()),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "consistent"
    assert auth.signal_families == ["generation_integrity", "identity_consistency"]


def test_authenticity_suspected_substitution_needs_other_family():
    results = [
        _r("d4.rotation", verdict=Verdict.WARN, metrics=_rotation(f=2)),
        _r("d4.headers_diff", metrics=_headers()),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "inconclusive"  # no adverse evidence in another family


def test_authenticity_suspected_substitution_corroborated():
    results = [
        _r("d4.rotation", verdict=Verdict.WARN, metrics=_rotation(f=2)),
        _r("d4.model_echo", verdict=Verdict.WARN, notes=["echo names a known family outside the claim"]),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "suspected_substitution"
    assert auth.confidence == 0.8
    assert auth.signal_families == ["generation_integrity", "relay_timing"]


def test_authenticity_confirmed_tampering_needs_corroboration():
    results = [_r("d4.canary_echo", verdict=Verdict.FAIL, metrics=_canary(template=True))]
    assert _authenticity(results).verdict == "inconclusive"


def test_authenticity_confirmed_tampering_corroborated_by_wrap():
    results = [
        _r("d4.canary_echo", verdict=Verdict.FAIL, metrics=_canary(asymmetry=True)),
        _r("d4.wrap_offset", verdict=Verdict.FAIL, metrics=_wrap()),
    ]
    auth = _authenticity(results)
    assert auth.verdict == "confirmed_tampering"
    assert auth.confidence == 0.8
    assert auth.signal_families == ["billing_transparency", "generation_integrity"]


# --- one real orchestrator run -------------------------------------------------


async def test_real_run_wires_vetoes_transit_authenticity(fake_server, tmp_path: Path):
    manifest = tmp_path / "veto_manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "probes:\n"
        "  - id: p0.echo\n"
        "    domain: platform\n"
        "    runner: p0.echo\n"
        "    samples: 1\n"
        "  - id: p0.models\n"
        "    domain: platform\n"
        "    runner: p0.models\n"
        "    samples: 1\n"
        "  - id: d4.headers_diff\n"
        "    domain: D4\n"
        "    runner: d4.headers_diff\n"
        "    samples: 2\n"
        "  - id: d4.recount_deviation\n"
        "    domain: D4\n"
        "    runner: d4.recount_deviation\n"
        "    samples: 3\n",
        encoding="utf-8",
    )
    fake_server.usage_offset_pct = 30.0
    bundle = await Orchestrator(transport=httpx.ASGITransport(app=fake_server)).run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert [v.code for v in bundle.vetoes] == ["billing_inflation"]
    # V7: assurance.vetoes equals the top-level list element-for-element.
    assert bundle.assurance.vetoes == bundle.vetoes
    assert bundle.assurance.level.value == "Disqualified"
    # transit from the clean headers probe; recount FAIL keeps authenticity
    # inconclusive (adverse billing_transparency, no corroborating tamper).
    assert bundle.transit.origin_class == "official"
    assert bundle.transit.hop_lower_bound == 1
    assert bundle.inconclusive is False
    assert bundle.authenticity.verdict == "inconclusive"
    assert bundle.authenticity.confidence == 0.0

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["vetoes"][0]["code"] == "billing_inflation"
    assert parsed["assurance"]["vetoes"] == parsed["vetoes"]
    assert parsed["transit"]["origin_class"] == "official"
