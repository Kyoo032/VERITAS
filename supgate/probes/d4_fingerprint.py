"""D4 relay fingerprints (build plan §10.3) — milestone M2.

Not implemented in M1. Probes: d4.headers_diff, d4.id_prefix,
d4.model_echo, d4.self_report, d4.canary_echo, d4.sse_timing, d4.rotation.
"""

from __future__ import annotations

from supgate.models import Domain, ProbeResult, SurfaceMap
from supgate.probes.base import RunContext

DOMAIN = Domain.D4


def placeholder_probe(probe_id: str) -> ProbeStub:
    return ProbeStub(probe_id)


class ProbeStub:
    """Manifest-registered stub; raises until the milestone lands."""

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
