"""Scoring engine (build plan §7): weighted domain means, overall score,
veto layer, and the Supply Assurance Level mapping."""

from __future__ import annotations

from supgate.models import (
    Assurance,
    AssuranceVerdict,
    Domain,
    DomainScore,
    ProbeResult,
    Verdict,
    Veto,
)

# §7: D6 30% · D4 30% · D8 25% · D2 15%. PLATFORM stays unscored.
DOMAIN_WEIGHTS: dict[Domain, float] = {
    Domain.D6: 0.30,
    Domain.D4: 0.30,
    Domain.D8: 0.25,
    Domain.D2: 0.15,
}

SCORED_DOMAINS = set(DOMAIN_WEIGHTS)


def score_domains(results: list[ProbeResult]) -> dict[str, DomainScore]:
    """Weighted mean of scored probes per domain; skips never lower a domain (§2)."""

    buckets: dict[Domain, list[ProbeResult]] = {}
    for result in results:
        if result.domain == Domain.PLATFORM or result.verdict == Verdict.SKIP:
            continue
        buckets.setdefault(result.domain, []).append(result)

    domain_scores: dict[str, DomainScore] = {}
    for domain, probes in buckets.items():
        weights = [p.weight for p in probes]
        total_weight = sum(weights) or 1.0
        score = sum(p.score * w for p, w in zip(probes, weights, strict=True)) / total_weight
        counts: dict[str, int] = {}
        for p in probes:
            counts[p.verdict.value] = counts.get(p.verdict.value, 0) + 1
        domain_scores[domain.value] = DomainScore(
            domain=domain,
            score=round(score, 1),
            probes=[p.probe_id for p in probes],
            verdict_counts=counts,
        )
    return domain_scores


def overall_score(domain_scores: dict[str, DomainScore]) -> float | None:
    """Normalize weights over the domains actually scored (§7 weighted mean)."""

    present = [d for d, s in domain_scores.items() if Domain(d) in SCORED_DOMAINS and s.probes]
    if not present:
        return None
    weights = {d: DOMAIN_WEIGHTS[Domain(d)] for d in present}
    total = sum(weights.values()) or 1.0
    return round(
        sum(domain_scores[d].score * weights[d] for d in present) / total,
        1,
    )


def assurance(
    overall: float | None,
    domain_scores: dict[str, DomainScore],
    vetoes: list[Veto],
    *,
    mode: str,
) -> AssuranceVerdict:
    """Supply Assurance Level (§7). Black-box caps at B; A needs credentials."""

    basis: list[str] = []
    if vetoes:
        for veto in vetoes:
            basis.append(f"veto {veto.code}: {veto.detail}")
        return AssuranceVerdict(level=Assurance.DISQUALIFIED, basis=basis, vetoes=vetoes)

    if overall is None:
        return AssuranceVerdict(
            level=Assurance.C,
            basis=["no scored domains — nothing verified yet"],
        )

    d4 = domain_scores.get(Domain.D4.value)
    d8 = domain_scores.get(Domain.D8.value)
    if d4 is not None and d4.score >= 80 and overall >= 70 and d8 is not None and d8.score >= 80:
        basis.append("stable relay (no reverse/mixing detected), capabilities verified, black-box")
        return AssuranceVerdict(level=Assurance.B, basis=basis)

    basis.append(
        f"identity evidence: {d4.score if d4 else 'n/a'} "
        f"({'D4 fingerprint/billing probes ran' if d4 else 'no D4 evidence — M2'}); "
        "capabilities: " + (f"{d8.score}" if d8 else "n/a")
    )
    return AssuranceVerdict(level=Assurance.C, basis=basis)
