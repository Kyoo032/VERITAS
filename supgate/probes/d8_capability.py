"""D8 tool & capability contract (build plan §10.6) — milestone M3.

Not implemented in M1. Probes: d8.tools_gpt x6, d8.structured_strict,
d8.reasoning, d8.cutoff_battery, d8.prompt_caching, d8.claude_suite.
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
        return "probe not implemented until M3"

    async def run(self, ctx: RunContext) -> ProbeResult:
        raise NotImplementedError(f"{self.id} arrives in milestone M3")
