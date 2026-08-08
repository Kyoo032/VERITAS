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
    assert "d4.headers_diff" in ids
    assert "d4.id_prefix" in ids
    assert "d4.canary_echo" in ids
    assert "d4.sse_timing" in ids
    assert "d4.usage_presence" in ids
    assert "d4.recount_deviation" in ids
    assert "d4.wrap_offset" in ids
    assert "d4.reasoning_cache_fields" in ids
    assert "d2.load_matrix" in ids
    assert "d2.needle_recall" in ids
    assert "d8.tools.auto" in ids
    assert "d8.tools.forced" in ids
    assert "d8.tools.required" in ids
    assert "d8.tools.parallel" in ids
    assert "d8.tools.multiturn" in ids
    assert "d8.tools.stream" in ids
    assert "d8.structured_strict" in ids
    assert "d8.reasoning" in ids
    assert "d8.cutoff_battery" in ids
    assert "d8.prompt_caching" in ids
    assert len(ids) == len(set(ids)), "duplicate probe ids"


def test_manifest_version(manifest: Path):
    assert load_manifest_version(manifest) == "3"


def test_custom_runners_resolve(manifest: Path):
    probes = {p.id: p for p in load_probes(manifest)}
    assert type(probes["p0.echo"]).__name__ == "EchoProbe"
    assert type(probes["d6.chat.sse"]).__name__ == "SseProbe"
    assert type(probes["d4.headers_diff"]).__name__ == "HeadersDiffProbe"
    assert type(probes["d4.id_prefix"]).__name__ == "IdPrefixProbe"
    assert type(probes["d4.sse_timing"]).__name__ == "SseTimingProbe"
    assert type(probes["d4.usage_presence"]).__name__ == "UsagePresenceProbe"
    assert type(probes["d4.recount_deviation"]).__name__ == "RecountDeviationProbe"
    assert type(probes["d4.wrap_offset"]).__name__ == "WrapOffsetProbe"
    assert type(probes["d4.reasoning_cache_fields"]).__name__ == "ReasoningCacheFieldsProbe"
    assert type(probes["d2.load_matrix"]).__name__ == "LoadMatrixProbe"
    assert type(probes["d2.needle_recall"]).__name__ == "NeedleRecallProbe"
    assert type(probes["d8.tools.auto"]).__name__ == "ToolAutoProbe"
    assert type(probes["d8.structured_strict"]).__name__ == "StructuredStrictProbe"
    assert type(probes["d8.reasoning"]).__name__ == "ReasoningProbe"
    assert type(probes["d8.cutoff_battery"]).__name__ == "CutoffBatteryProbe"
    assert type(probes["d8.prompt_caching"]).__name__ == "PromptCachingProbe"


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
