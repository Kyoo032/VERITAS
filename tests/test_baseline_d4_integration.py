"""Baseline-to-D4 integration (docs/06 §4.4/§4.7, docs/08 §10): a baseline
produced through ``record_baseline`` — not a hand-injected record — carries
the ``self_report.terms`` and ``rotation_families`` fingerprints that the
d4.self_report and d4.rotation probes consume. The stored baseline file is
the single source of truth: every probe run below loads it from disk."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from supgate.baseline_recorder import record_baseline
from supgate.baselines import BaselineStore
from supgate.evidence import EvidenceWriter
from supgate.models import BudgetTracker, SurfaceMap, Verdict
from supgate.probes.base import RunContext
from supgate.probes.d4_fingerprint import RotationProbe, SelfReportProbe


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)


async def _record(fake_server, transport, tmp_path: Path):
    """Record a baseline exactly like the CLI does (default p0 gate on)."""
    return await record_baseline(
        vendor="openai",
        model="gpt-4o",
        api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1",
        out=tmp_path / "baselines",
        evidence_root=tmp_path / "runs",
        transport=transport,
        captured_at="2026-08-06T00:00:00+00:00",
    )


def _probe_ctx(fake_server, tmp_path: Path, baseline) -> tuple[RunContext, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_server), timeout=10)
    ctx = RunContext(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=SurfaceMap(models=list(fake_server.models), claimed_present=True),
        client=client,
        evidence=EvidenceWriter(tmp_path / "evidence", "D4-INTEGRATION"),
        budget=BudgetTracker(),
        selected_baseline=baseline,
    )
    return ctx, client


async def test_recorded_baseline_feeds_self_report_match(fake_server, transport, tmp_path: Path):
    recorded = await _record(fake_server, transport, tmp_path)
    baseline = BaselineStore(tmp_path / "baselines").get(recorded.baseline_id)
    assert baseline is not None

    terms = baseline.fingerprints["self_report"]["terms"]
    assert terms  # reference terms exist in the persisted record
    assert any("openai" in term for term in terms)

    # The endpoint under test reports the same platform the baseline
    # observed; the probe must PASS through the recorded terms, not a stub.
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    ctx, client = _probe_ctx(fake_server, tmp_path, baseline)
    try:
        result = await SelfReportProbe().run(ctx)
    finally:
        await client.aclose()

    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["self_report"]
    assert metrics["report_match"] is True
    assert metrics["match_flags"] == [True, True]
    assert metrics["surface_drift"]["hard"] is False
    assert metrics["surface_drift"]["mild"] is False


async def test_recorded_baseline_self_report_mismatch_warns(fake_server, transport, tmp_path: Path):
    """The recorded terms are actually enforced: a contradictory report
    still WARNs with the recorded baseline attached."""
    recorded = await _record(fake_server, transport, tmp_path)
    baseline = BaselineStore(tmp_path / "baselines").get(recorded.baseline_id)
    assert baseline is not None

    fake_server.self_report_text = "other platform"
    ctx, client = _probe_ctx(fake_server, tmp_path, baseline)
    try:
        result = await SelfReportProbe().run(ctx)
    finally:
        await client.aclose()

    assert result.verdict == Verdict.WARN
    metrics = result.metrics["self_report"]
    assert metrics["report_match"] is False
    assert metrics["contradiction_both"] is True


async def test_recorded_baseline_feeds_rotation_f_expected(fake_server, transport, tmp_path: Path, no_sleep):
    recorded = await _record(fake_server, transport, tmp_path)
    baseline = BaselineStore(tmp_path / "baselines").get(recorded.baseline_id)
    assert baseline is not None

    # Expected-family data exists in the persisted baseline.
    assert isinstance(baseline.fingerprints["rotation_families"], int)
    assert baseline.fingerprints["rotation_families"] == 1

    ctx, client = _probe_ctx(fake_server, tmp_path, baseline)
    try:
        result = await RotationProbe().run(ctx)
    finally:
        await client.aclose()

    assert result.verdict == Verdict.PASS
    metrics = result.metrics["rotation"]
    assert metrics["F_expected"] == 1
    assert metrics["F"] == 1
