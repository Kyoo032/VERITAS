"""Orchestrator: end-to-end runs, budgets, 429 policy, history store (§5, §12)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from supgate.models import Verdict
from supgate.orchestrator import Orchestrator, endpoint_dead
from supgate.store import RunStore


async def test_full_run_produces_bundle(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert bundle.run_id.startswith("SUP-")
    assert bundle.overall_score is not None
    assert "D6" in bundle.domain_scores
    assert bundle.assurance.level.value == "C"
    assert bundle.finished_at is not None
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.PASS
    d6_basic = next(p for p in bundle.probes if p.probe_id == "d6.chat.basic")
    assert d6_basic.verdict == Verdict.PASS
    assert d6_basic.evidence_ref, "evidence must be captured per probe"

    bundle_file = tmp_path / f"{bundle.run_id}.json"
    assert bundle_file.exists()
    parsed = json.loads(bundle_file.read_text(encoding="utf-8"))
    assert parsed["run_id"] == bundle.run_id
    assert parsed["domain_scores"]["D6"]["score"] > 0

    evidence_dir = tmp_path / "evidence" / bundle.run_id
    files = list(evidence_dir.glob("*.json"))
    assert len(files) >= 10, "each probe sample stores redacted evidence"


async def test_adhoc_mode_runs_core_catalog(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
    )
    assert bundle.mode == "adhoc"
    assert all(p.verdict in {Verdict.PASS, Verdict.WARN, Verdict.FAIL, Verdict.SKIP} for p in bundle.probes)
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.PASS


async def test_bad_key_fails_echo_and_flagsendpoint_dead(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key="sk-wrong",
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert endpoint_dead(bundle)
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.FAIL


async def test_budget_exhaustion_marks_probes_warn(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
        budget_usd=0.0000001,
    )
    budget_blocked = [p for p in bundle.probes if any("budget-blocked" in n for n in p.notes)]
    assert budget_blocked, "budget exhaustion must surface as explicit warns, never silent skips"
    assert all(p.verdict == Verdict.WARN for p in budget_blocked)


async def test_all_429_marks_manifest_probe_warn(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch):
    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.registry.asyncio.sleep", no_sleep)
    fake_server.force_429 = True
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    basic = next(p for p in bundle.probes if p.probe_id == "d6.chat.basic")
    assert basic.verdict == Verdict.WARN
    assert any("rate-limited" in n for n in basic.notes)


async def test_transport_error_is_captured_as_evidence(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    orchestrator.transport = CrashTransport()
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    assert endpoint_dead(bundle)
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert "unreachable" in " ".join(echo.notes)


async def test_store_records_run(fake_server, manifest: Path, tmp_path: Path):
    orchestrator = Orchestrator(transport=httpx.ASGITransport(app=fake_server))
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    store = RunStore(tmp_path / "history.db")
    store.record_run(bundle, tmp_path / f"{bundle.run_id}.json")
    rows = store.history()
    assert len(rows) == 1
    assert rows[0]["run_id"] == bundle.run_id
    assert rows[0]["overall"] == bundle.overall_score
    rows = store.history(endpoint="https://fake.example/v1")
    assert len(rows) == 1
    assert store.history(endpoint="https://other.example/v1") == []

