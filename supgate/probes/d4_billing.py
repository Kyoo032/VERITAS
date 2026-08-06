"""D4 billing forensics (build plan §10.4) — milestone M2.

Not implemented in M1. Probes: d4.usage_presence, d4.recount_deviation,
d4.wrap_offset, d4.reasoning_cache_fields. Needs tokenizers (tiktoken).
"""

from __future__ import annotations

from supgate.models import Domain, ProbeResult, SurfaceMap
from supgate.probes.base import RunContext

DOMAIN = Domain.D4


class ProbeStub:
    id: str
    domain = DOMAIN
    weight = 1.0
    samples = 1

    def __init__(self, probe_id: str) -> None:
        self.id = probe_id

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return "probe not implemented until M2"

    async def run(self, ctx: RunContext) -> ProbeResult:
        raise NotImplementedError(f"{self.id} arrives in milestone M2")
