"""D2 load performance (build plan §10.5) — milestone M3.

Not implemented in M1. Probes: d2.load_matrix (3 input bands x concurrency 10,
TTFT/TPOT/ITL/E2E percentiles, goodput vs SLA), d2.needle_recall.
"""

from __future__ import annotations

from supgate.models import Domain, ProbeResult, SurfaceMap
from supgate.probes.base import RunContext

DOMAIN = Domain.D2


class ProbeStub:
    id: str
    domain = DOMAIN
    weight = 1.0
    samples = 1

    def __init__(self, probe_id: str) -> None:
        self.id = probe_id

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return "probe not implemented until M3"

    async def run(self, ctx: RunContext) -> ProbeResult:
        raise NotImplementedError(f"{self.id} arrives in milestone M3")
