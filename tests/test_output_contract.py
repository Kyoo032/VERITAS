"""Output contract (§13): bundle vetoes/calibration, per-probe curl,
summary robustness, strict mode validation, and skip-before-budget ordering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supgate.baselines import BaselineRecord, BaselineStore, BaselineSurface
from supgate.models import Domain, ProbeResult, RunBundle, Verdict
from supgate.orchestrator import Orchestrator, summary


async def test_bundle_has_top_level_vetoes_and_calibration(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert bundle.vetoes == []
    assert bundle.calibration is not None
    assert bundle.calibration.p0_verdicts["p0.echo"] == Verdict.PASS.value
    assert bundle.calibration.models_catalog == len(fake_server.models)
    assert bundle.calibration.claimed_present is True
    assert bundle.assurance.vetoes == bundle.vetoes

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert "vetoes" in parsed
    assert "calibration" in parsed
    assert parsed["calibration"]["p0_verdicts"]["p0.echo"] == "pass"


async def test_probe_results_carry_redacted_curl(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.curl, "each probe must carry the redacted curl from evidence"
    assert "$SUPGATE_KEY" in echo.curl
    assert fake_server.valid_key not in echo.curl
    d6_basic = next(p for p in bundle.probes if p.probe_id == "d6.chat.basic")
    assert d6_basic.curl is not None
    assert fake_server.valid_key not in d6_basic.curl


def test_summary_tolerates_missing_overall():
    bundle = RunBundle(
        run_id="SUP-X",
        endpoint="https://api.example/v1",
        claimed_models=["gpt-4o"],
        mode="full",
        started_at="2026-08-06T00:00:00+00:00",
        overall_score=None,
    )
    text = summary(bundle)
    assert "overall=n/a" in text
    assert "assurance=C" in text


async def test_invalid_mode_is_rejected(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    with pytest.raises(ValueError):
        await orchestrator.run(
            endpoint="https://fake.example/v1",
            api_key=fake_server.valid_key,
            claimed_models=["gpt-4o"],
            manifest_path=manifest,
            mode="sneaky",
            out_dir=tmp_path,
        )


async def test_endpoint_query_or_fragment_is_rejected(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    for endpoint in (
        "https://fake.example/v1?api_key=plain-secret",
        "https://fake.example/v1#token=plain-secret",
    ):
        with pytest.raises(ValueError, match="base URL without query/fragment"):
            await orchestrator.run(
                endpoint=endpoint,
                api_key=fake_server.valid_key,
                claimed_models=["gpt-4o"],
                manifest_path=manifest,
                out_dir=tmp_path,
            )


class _LeakyArtifactProbe:
    id = "test.leaky_artifact"
    domain = Domain.PLATFORM
    weight = 1.0
    samples = 1

    def skip_reason(self, surface):
        return None

    async def run(self, ctx):
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            attempts=1,
            successes=1,
            notes=[f"echoed {ctx.api_key}"],
            metrics={
                "raw": ctx.api_key,
                "bearer": f"Bearer {ctx.api_key}",
                "url": f"https://relay.example/debug?api_key={ctx.api_key}",
                "proxy-authorization": "Basic opaque-secret",
            },
        )


async def test_bundle_redacts_endpoint_controlled_metrics_and_exact_runtime_key(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("supgate.orchestrator.load_probes", lambda _: [_LeakyArtifactProbe()])
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        out_dir=tmp_path,
    )
    serialized = bundle.model_dump_json()
    disk = (tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8")
    for artifact in (serialized, disk):
        assert fake_server.valid_key not in artifact
        assert "opaque-secret" not in artifact
        assert "$SUPGATE_KEY" in artifact


async def test_skip_reason_beats_budget_blocked(
    orchestrator: Orchestrator, fake_server, tmp_path: Path
):
    manifest = tmp_path / "skip_manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "probes:\n"
        "  - id: p0.echo\n"
        "    domain: platform\n"
        "    runner: p0.echo\n"
        "    samples: 1\n"
        "  - id: x.skip_if\n"
        "    domain: D6\n"
        "    samples: 1\n"
        "    skip_if: [no_claimed_model]\n"
        "    request:\n"
        "      messages:\n"
        "        - role: user\n"
        "          content: hi\n"
        "    pass: status == 200\n",
        encoding="utf-8",
    )
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["missing-model"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
        budget_usd=0.0000001,
    )
    skip = next(p for p in bundle.probes if p.probe_id == "x.skip_if")
    assert skip.verdict == Verdict.SKIP
    assert "skipped" in " ".join(skip.notes)


# --- schema-2 emission (docs/08 §3) -----------------------------------------


async def test_bundle_emits_schema_2_versions(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert bundle.schema == 2
    # V10: versions.schema is an integer, not a string.
    assert bundle.versions["schema"] == 2
    assert isinstance(bundle.versions["schema"], int)
    assert bundle.versions["supgate"] == "0.2.0"

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["schema"] == 2
    assert parsed["versions"]["schema"] == 2
    assert parsed["versions"]["supgate"] == "0.2.0"


async def test_cost_summary_serialized_shape_is_exact(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    # docs/08 §3 cost shape: exactly these five fields — `model` must not
    # serialize even though the tracker keeps it in memory.
    assert set(bundle.cost.model_dump()) == {
        "estimated_usd",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "blocked",
    }
    assert bundle.cost.requests >= 1
    assert bundle.cost.prompt_tokens > 0
    assert bundle.cost.completion_tokens > 0
    assert bundle.cost.estimated_usd > 0
    assert bundle.cost.blocked is False

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert set(parsed["cost"]) == {
        "estimated_usd",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "blocked",
    }
    assert "model" not in parsed["cost"]
    assert parsed["cost"]["requests"] == bundle.cost.requests


async def test_surface_persisted_and_matches_calibration(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert bundle.surface.models == list(fake_server.models)
    assert bundle.surface.claimed_present is True
    assert bundle.calibration is not None
    # V11: calibration.models_catalog == len(surface.models) when both present.
    assert bundle.calibration.models_catalog == len(bundle.surface.models)

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["surface"]["models"] == list(fake_server.models)
    assert parsed["surface"]["claimed_present"] is True
    assert parsed["calibration"]["models_catalog"] == len(parsed["surface"]["models"])


async def test_baseline_reference_is_null_without_match(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
        baseline_root=tmp_path / "empty",
    )
    assert bundle.baseline is None
    assert bundle.versions["baselines"] == "none"

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["baseline"] is None
    assert parsed["versions"]["baselines"] == "none"


async def test_selected_baseline_reference_preserves_match(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    baseline_root = tmp_path / "baselines"
    BaselineStore(baseline_root).save(
        BaselineRecord(
            baseline_id="BL-OFFICIAL-OPENAI-GPT4O-0001",
            provider_label="openai",
            vendor="openai",
            model="gpt-4o",
            claimed_models=["gpt-4o"],
            captured_at="2026-08-06T00:00:00+00:00",
            surface=BaselineSurface(models_catalog=3, claimed_present=True),
        )
    )
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=baseline_root,
    )
    assert bundle.baseline is not None
    assert bundle.baseline.baseline_id == "BL-OFFICIAL-OPENAI-GPT4O-0001"
    assert bundle.baseline.matched_on == ["gpt-4o"]
    assert bundle.baseline.captured_at == "2026-08-06T00:00:00+00:00"
    assert bundle.versions["baselines"] == "BL-OFFICIAL-OPENAI-GPT4O-0001"

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["baseline"] == {
        "baseline_id": "BL-OFFICIAL-OPENAI-GPT4O-0001",
        "matched_on": ["gpt-4o"],
        "captured_at": "2026-08-06T00:00:00+00:00",
    }


async def test_transit_and_authenticity_typed_defaults(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    # docs/08 §9: the clean headers probe has no hop markers -> official.
    assert bundle.transit.hop_lower_bound == 1
    assert bundle.transit.origin_class == "official"
    assert bundle.transit.hop_hints == []
    # docs/08 §3: the model_echo mismatch warn is adverse D4 evidence, so
    # authenticity stays inconclusive with zero confidence.
    assert bundle.authenticity.verdict == "inconclusive"
    assert bundle.authenticity.confidence == 0.0
    assert bundle.authenticity.signal_families == []
    assert bundle.inconclusive is False
    assert bundle.inconclusive_reason is None

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["transit"] == {"hop_lower_bound": 1, "origin_class": "official", "hop_hints": []}
    assert parsed["authenticity"] == {
        "verdict": "inconclusive",
        "confidence": 0.0,
        "signal_families": [],
    }
    assert parsed["inconclusive"] is False
    assert parsed["inconclusive_reason"] is None


async def test_result_weights_propagate_from_probes(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    fake_server.model_echo = "gpt-4o"
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    # D4 manifest weights reach ProbeResult.weight (§7) via the orchestrator.
    expected = {
        "p0.echo": 1.0,
        "d4.headers_diff": 1.0,
        "d4.model_echo": 0.5,
        "d4.rotation": 1.5,
        "d4.recount_deviation": 2.0,
    }
    for probe_id, weight in expected.items():
        result = next(p for p in bundle.probes if p.probe_id == probe_id)
        assert result.weight == weight, f"{probe_id} weight {result.weight} != {weight}"

    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    rotation = next(p for p in parsed["probes"] if p["probe_id"] == "d4.rotation")
    assert rotation["weight"] == 1.5
    recount = next(p for p in parsed["probes"] if p["probe_id"] == "d4.recount_deviation")
    assert recount["weight"] == 2.0


async def test_budget_blocked_results_retain_probe_weight(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
        budget_usd=0.0000001,
    )
    blocked = [p for p in bundle.probes if any("budget-blocked" in n for n in p.notes)]
    assert blocked
    assert all(p.verdict == Verdict.WARN for p in blocked)
    rotation = next(p for p in blocked if p.probe_id == "d4.rotation")
    assert rotation.weight == 1.5
    recount = next(p for p in blocked if p.probe_id == "d4.recount_deviation")
    assert recount.weight == 2.0


async def test_skip_and_mode_skip_results_retain_probe_weight(
    orchestrator: Orchestrator, fake_server, tmp_path: Path
):
    manifest = tmp_path / "weight_manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "probes:\n"
        "  - id: p0.echo\n"
        "    domain: platform\n"
        "    runner: p0.echo\n"
        "    samples: 1\n"
        "  - id: x.skip_if\n"
        "    domain: D6\n"
        "    weight: 3.0\n"
        "    samples: 1\n"
        "    skip_if: [no_claimed_model]\n"
        "    request:\n"
        "      messages:\n"
        "        - role: user\n"
        "          content: hi\n"
        "    pass: status == 200\n"
        "  - id: x.load\n"
        "    domain: D2\n"
        "    weight: 2.0\n"
        "    samples: 1\n"
        "    request:\n"
        "      messages:\n"
        "        - role: user\n"
        "          content: hi\n"
        "    pass: status == 200\n",
        encoding="utf-8",
    )
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["missing-model"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
    )
    skip = next(p for p in bundle.probes if p.probe_id == "x.skip_if")
    assert skip.verdict == Verdict.SKIP
    assert skip.weight == 3.0
    mode_skip = next(p for p in bundle.probes if p.probe_id == "x.load")
    assert mode_skip.verdict == Verdict.SKIP
    assert mode_skip.weight == 2.0
