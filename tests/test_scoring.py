"""Scoring engine + assurance mapping (§7)."""

from __future__ import annotations

from supgate.models import Assurance, Domain, DomainScore, ProbeResult, Verdict, Veto
from supgate.scoring import assurance, overall_score, score_domains


def _r(probe_id: str, domain: Domain, verdict: Verdict, score: float = 0.0, weight: float = 1.0) -> ProbeResult:
    return ProbeResult(probe_id=probe_id, domain=domain, verdict=verdict, score=score, weight=weight)


def _d(domain: Domain, score: float) -> DomainScore:
    return DomainScore(domain=domain, score=score, probes=["x"])


def test_score_domains_skips_platform_and_skips():
    results = [
        _r("p0.echo", Domain.PLATFORM, Verdict.PASS, 100),
        _r("d6.chat.basic", Domain.D6, Verdict.PASS, 100),
        _r("d6.chat.sse", Domain.D6, Verdict.SKIP),
        _r("d6.responses_api", Domain.D6, Verdict.WARN, 50),
    ]
    scores = score_domains(results)
    assert "platform" not in scores
    d6 = scores["D6"]
    assert d6.score == 75.0
    assert d6.verdict_counts == {"pass": 1, "warn": 1}


def test_score_domains_weighted():
    results = [
        _r("a", Domain.D6, Verdict.PASS, 100, weight=2.0),
        _r("b", Domain.D6, Verdict.FAIL, 0, weight=1.0),
    ]
    scores = score_domains(results)
    assert scores["D6"].score == 66.7


def test_overall_normalizes_over_present_domains():
    scores = {"D6": _d(Domain.D6, 100), "D8": _d(Domain.D8, 0)}
    overall = overall_score(scores)
    assert overall is not None and round(overall, 1) == round(100 * 0.30 / 0.55, 1)


def test_overall_none_without_scored_domains():
    assert overall_score({}) is None


def test_assurance_disqualified_on_veto():
    verdict = assurance(95.0, {}, [Veto(code="billing_inflation", detail="recount deviated 88%")], mode="full")
    assert verdict.level == Assurance.DISQUALIFIED


def test_assurance_vetoes_equal_input_list():
    """Invariant V7: assurance.vetoes mirrors the top-level vetoes."""
    vetoes = [
        Veto(code="billing_inflation", detail="recount deviated 88%"),
        Veto(code="hidden_origin", detail="hop markers + contradiction"),
    ]
    verdict = assurance(95.0, {}, vetoes, mode="full")
    assert verdict.level == Assurance.DISQUALIFIED
    assert verdict.vetoes == vetoes
    assert [v.code for v in verdict.vetoes] == ["billing_inflation", "hidden_origin"]


def test_assurance_b_when_identity_and_capabilities_verified():
    scores = {"D4": _d(Domain.D4, 90), "D8": _d(Domain.D8, 90)}
    verdict = assurance(85.0, scores, [], mode="full")
    assert verdict.level == Assurance.B


def test_assurance_c_without_d4_evidence():
    scores = {"D6": _d(Domain.D6, 100)}
    verdict = assurance(100.0, scores, [], mode="full")
    assert verdict.level == Assurance.C
    assert any("no D4 evidence" in b for b in verdict.basis)
