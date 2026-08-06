"""Authenticity probes v1.1 (build plan §10.7) — milestone M5.

Not implemented in M1. Probes: auth.rng_fingerprint, auth.llmmap,
auth.logprob_audit, auth.kbf_battery, auth.mixed_routing.
"""

from __future__ import annotations

from supgate.models import Domain, ProbeResult, SurfaceMap
from supgate.probes.base import RunContext

DOMAIN = Domain.D8


class ProbeStub:
    id: str
    domain = DOMAIN
    weight = 1.0
    samples = 1

    def __init__(self, probe_id: str) -> None:
        self.id = probe_id

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return "probe not implemented until M5"

    async def run(self, ctx: RunContext) -> ProbeResult:
        raise NotImplementedError(f"{self.id} arrives in milestone M5")
