"""Output contract (§13): bundle vetoes/calibration, per-probe curl,
summary robustness, strict mode validation, and skip-before-budget ordering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supgate.models import RunBundle, Verdict
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
