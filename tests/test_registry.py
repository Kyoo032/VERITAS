"""Registry: manifest loading, skip rules, custom runner mapping (§11.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from supgate.models import SurfaceMap
from supgate.registry import ProbeSpecError, load_manifest_version, load_probes


def test_manifest_loads_all_probes(manifest: Path):
    probes = load_probes(manifest)
    ids = [p.id for p in probes]
    assert "p0.echo" in ids
    assert "d6.chat.basic" in ids
    assert "d6.chat.sse" in ids
    assert "d6.responses_api" in ids
    assert "d6.vision" in ids
    assert "d6.usage_fields" in ids
    assert "d6.param_boundaries" in ids
    assert "d6.max_tokens" in ids
    assert "d6.messages.shapes" in ids
    assert "d6.tool_passthrough" in ids
    assert "d6.idempotency" in ids
    assert "d6.json_mode" in ids
    assert len(ids) == len(set(ids)), "duplicate probe ids"


def test_manifest_version(manifest: Path):
    assert load_manifest_version(manifest) == "1"


def test_custom_runners_resolve(manifest: Path):
    probes = {p.id: p for p in load_probes(manifest)}
    assert type(probes["p0.echo"]).__name__ == "EchoProbe"
    assert type(probes["d6.chat.sse"]).__name__ == "SseProbe"


def test_skip_reason_claimed_model_absent():
    from supgate.registry import ManifestProbe

    probe = ManifestProbe({"id": "x", "domain": "D6", "skip_if": ["no_claimed_model"]})
    assert probe.skip_reason(SurfaceMap(claimed_present=False)) is not None
    assert probe.skip_reason(SurfaceMap(claimed_present=True)) is None


def test_unknown_runner_raises(manifest: Path, tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("probes:\n  - id: x\n    domain: D6\n    runner: mystery\n", encoding="utf-8")
    with pytest.raises(ProbeSpecError):
        load_probes(bad)


def test_samples_mismatch_raises():
    from supgate.registry import ManifestProbe

    with pytest.raises(ProbeSpecError):
        ManifestProbe(
            {"id": "x", "domain": "D6", "samples": 2,
             "cases": [{"samples": 1}, {"samples": 1}, {"samples": 1}]}
        )
