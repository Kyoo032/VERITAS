"""Orchestrator: end-to-end runs, budgets, 429 policy, history store (§5, §12)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from supgate.baselines import BaselineError, BaselineRecord, BaselineStore, BaselineSurface
from supgate.models import Domain, ProbeResult, Verdict
from supgate.orchestrator import Orchestrator, _resolve_selected_baseline, endpoint_dead
from supgate.probes.d4_fingerprint import HeadersDiffProbe
from supgate.probes.p0 import EchoProbe
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


async def test_probe_exception_becomes_fail_and_bundle_is_written(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
    class CrashingProbe:
        id = "p0.echo"
        domain = Domain.PLATFORM
        weight = 1.0
        samples = 1

        def skip_reason(self, surface):
            return None

        async def run(self, ctx):
            raise TypeError("provider returned malformed content")

    monkeypatch.setattr("supgate.orchestrator.load_probes", lambda _: [CrashingProbe()])
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
    )

    result = bundle.probes[0]
    assert result.verdict == Verdict.FAIL
    assert result.error == "TypeError: provider returned malformed content"
    assert any("run continued" in note for note in result.notes)
    assert (tmp_path / f"{bundle.run_id}.json").exists()


async def test_probe_setup_exception_becomes_fail_and_bundle_is_written(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
    class CrashingSetupProbe:
        id = "d6.crashing_setup"
        domain = Domain.D6
        weight = 1.0
        samples = 1

        def skip_reason(self, surface):
            raise ValueError("bad skip rule")

        async def run(self, ctx):
            raise AssertionError("must not run")

    monkeypatch.setattr("supgate.orchestrator.load_probes", lambda _: [CrashingSetupProbe()])
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
    )

    result = bundle.probes[0]
    assert result.verdict == Verdict.FAIL
    assert result.error == "ValueError: bad skip rule"
    assert (tmp_path / f"{bundle.run_id}.json").exists()


async def test_concurrency_limits_parallel_probe_execution(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
    active = 0
    max_active = 0

    class WaitingProbe:
        domain = Domain.D6
        weight = 1.0
        samples = 1

        def __init__(self, probe_id):
            self.id = probe_id

        def skip_reason(self, surface):
            return None

        async def run(self, ctx):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.PASS,
                score=100.0,
                successes=1,
                attempts=1,
            )

    monkeypatch.setattr(
        "supgate.orchestrator.load_probes",
        lambda _: [WaitingProbe(f"d6.wait.{i}") for i in range(4)],
    )
    limited = Orchestrator(concurrency=2, transport=orchestrator.transport)
    await limited.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
    )

    assert max_active == 2


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


async def test_bad_key_fails_echo_and_flagsendpoint_dead(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
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


async def test_budget_exhaustion_marks_probes_warn(
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
    budget_blocked = [p for p in bundle.probes if any("budget-blocked" in n for n in p.notes)]
    assert budget_blocked, "budget exhaustion must surface as explicit warns, never silent skips"
    assert all(p.verdict == Verdict.WARN for p in budget_blocked)


async def test_all_429_marks_manifest_probe_warn(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
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


# --- D4 prerequisite gating (§10.3) -----------------------------------------


class _FailModelsProbe:
    """Custom p0.models that FAILs so only the d4.self_report gate trips."""

    id = "p0.models"
    domain = Domain.PLATFORM
    weight = 1.0
    samples = 1

    def skip_reason(self, surface) -> str | None:
        return None

    async def run(self, ctx) -> ProbeResult:
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.FAIL,
            score=0.0,
            attempts=1,
            notes=["simulated /models failure"],
        )


class _P0VerdictAwareProbe:
    """Custom d4.* probe that surfaces the RunContext P0 verdict map."""

    id = "d4.p0_verdicts"
    domain = Domain.D4
    weight = 1.0
    samples = 1

    def skip_reason(self, surface) -> str | None:
        return None

    async def run(self, ctx) -> ProbeResult:
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=1,
            attempts=1,
            notes=[f"p0_verdicts={ctx.p0_verdicts}"],
        )


class _FakeSelfReportProbe:
    """Stand-in for the M2 d4.self_report stub; gated on p0.models."""

    id = "d4.self_report"
    domain = Domain.D4
    weight = 1.0
    samples = 2

    def skip_reason(self, surface) -> str | None:
        return None

    async def run(self, ctx) -> ProbeResult:
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=2,
            attempts=2,
            notes=["fake self report ran"],
        )


def _d4_gate_probes() -> list:
    return [
        EchoProbe(),
        _FailModelsProbe(),
        HeadersDiffProbe(),
        _P0VerdictAwareProbe(),
        _FakeSelfReportProbe(),
    ]


async def test_p0_pass_allows_d4_probes(orchestrator, fake_server, manifest, tmp_path):
    fake_server.model_echo = "gpt-4o"
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.PASS
    d4 = [p for p in bundle.probes if p.probe_id.startswith("d4.")]
    assert d4
    # No baseline selected: d4.self_report is structural-only and WARNs
    # (§4.4: without a baseline it can never PASS or FAIL).
    assert all(p.verdict == Verdict.PASS for p in d4 if p.probe_id != "d4.self_report")
    self_report = next(p for p in d4 if p.probe_id == "d4.self_report")
    assert self_report.verdict == Verdict.WARN
    assert self_report.score == 50.0
    assert any("no baseline" in note for note in self_report.notes)
    assert all(p.attempts > 0 for p in d4)
    assert bundle.calibration is not None
    assert bundle.calibration.p0_verdicts == {
        "p0.echo": "pass",
        "p0.models": "pass",
        "p0.error_contract": "pass",
    }


async def test_echo_fail_skips_all_d4_without_requests(orchestrator, fake_server, manifest, tmp_path):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key="sk-wrong",
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.FAIL
    d4 = [p for p in bundle.probes if p.probe_id.startswith("d4.")]
    assert d4
    assert all(p.verdict == Verdict.SKIP for p in d4)
    assert all(p.attempts == 0 for p in d4)
    assert all(p.evidence_ref == [] and p.curl is None for p in d4)
    assert all(any("p0.echo" in n and "not pass" in n for n in p.notes) for p in d4)


async def test_echo_warn_skips_all_d4(orchestrator, fake_server, manifest, tmp_path, monkeypatch):
    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.registry.asyncio.sleep", no_sleep)
    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", no_sleep)
    fake_server.force_429 = True
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.WARN
    d4 = [p for p in bundle.probes if p.probe_id.startswith("d4.")]
    assert d4
    assert all(p.verdict == Verdict.SKIP for p in d4)
    assert all(p.attempts == 0 for p in d4)


async def test_models_fail_gates_only_d4_self_report(orchestrator, fake_server, manifest, tmp_path, monkeypatch):
    monkeypatch.setattr("supgate.orchestrator.load_probes", lambda _: _d4_gate_probes())
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    models = next(p for p in bundle.probes if p.probe_id == "p0.models")
    assert echo.verdict == Verdict.PASS
    assert models.verdict == Verdict.FAIL
    headers = next(p for p in bundle.probes if p.probe_id == "d4.headers_diff")
    assert headers.verdict == Verdict.PASS
    assert headers.attempts == 2
    aware = next(p for p in bundle.probes if p.probe_id == "d4.p0_verdicts")
    assert aware.notes == ["p0_verdicts={'p0.echo': 'pass', 'p0.models': 'fail'}"]
    self_report = next(p for p in bundle.probes if p.probe_id == "d4.self_report")
    assert self_report.verdict == Verdict.SKIP
    assert self_report.attempts == 0
    assert self_report.evidence_ref == []
    assert any("p0.models" in n and "not pass" in n for n in self_report.notes)
    assert bundle.calibration is not None
    assert bundle.calibration.p0_verdicts == {"p0.echo": "pass", "p0.models": "fail"}


async def test_self_report_fails_on_baseline_hard_drift_corroboration(orchestrator, fake_server, manifest, tmp_path):
    BaselineStore(tmp_path / "baselines").save(
        BaselineRecord(
            baseline_id="BL-OPENAI-GPT4O-0001",
            provider_label="openai",
            vendor="openai",
            model="gpt-4o",
            claimed_models=["gpt-4o"],
            surface=BaselineSurface(models_catalog=3, claimed_present=True),
            fingerprints={"self_report": {"terms": ["openai", "gpt-4o"]}},
        )
    )
    fake_server.self_report_text = "other platform"
    fake_server.models_metadata_drift = True
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=tmp_path / "baselines",
    )
    self_report = next(p for p in bundle.probes if p.probe_id == "d4.self_report")
    assert self_report.verdict == Verdict.FAIL
    assert self_report.attempts == 2
    metrics = self_report.metrics["self_report"]
    assert metrics["contradiction_both"] is True
    assert metrics["surface_drift"]["hard"] is True
    assert metrics["surface_drift"]["claimed_present_now"] is False


async def test_transport_error_is_captured_as_evidence(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
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


# --- baseline selection (docs/08 §10.2) -------------------------------------


def _save_baseline(
    root: Path,
    baseline_id: str,
    *,
    model: str,
    vendor: str = "openai",
    captured_at: str = "2026-08-06T00:00:00+00:00",
) -> None:
    BaselineStore(root).save(
        BaselineRecord(
            baseline_id=baseline_id,
            provider_label=vendor,
            vendor=vendor,
            model=model,
            endpoint="https://api.openai.com/v1",
            captured_at=captured_at,
            claimed_models=[model],
        )
    )


def _resolve(**kwargs) -> BaselineRecord | None:
    match = _resolve_selected_baseline(
        **{
            "baseline_root": None,
            "baseline_id": None,
            "claimed_models": ["gpt-4o"],
            "allow_family": False,
            "allow_coarse": False,
            **kwargs,
        }
    )
    return None if match is None else match.record


def test_resolve_auto_selects_exact_by_default(tmp_path: Path):
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    record = _resolve(baseline_root=tmp_path, claimed_models=["gpt-4o"])
    assert record is not None and record.baseline_id == "BL-OPENAI-GPT4O-0001"


def test_resolve_no_match_returns_none(tmp_path: Path):
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    assert _resolve(baseline_root=tmp_path, claimed_models=["claude-3-5-sonnet-20241022"]) is None
    assert _resolve(baseline_root=tmp_path / "missing") is None


def test_resolve_explicit_id_loads_exactly(tmp_path: Path):
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0002", model="gpt-4o", captured_at="2026-08-07T00:00:00+00:00")
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    record = _resolve(baseline_root=tmp_path, baseline_id="BL-OPENAI-GPT4O-0001")
    assert record is not None and record.baseline_id == "BL-OPENAI-GPT4O-0001"


def test_resolve_explicit_missing_aborts(tmp_path: Path):
    with pytest.raises(BaselineError, match="no baseline 'BL-NOPE-0001'"):
        _resolve(baseline_root=tmp_path, baseline_id="BL-NOPE-0001")


def test_resolve_explicit_malformed_aborts(tmp_path: Path):
    (tmp_path / "BL-BROKEN-0001.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(BaselineError, match="not valid JSON"):
        _resolve(baseline_root=tmp_path, baseline_id="BL-BROKEN-0001")


def test_resolve_explicit_incompatible_model_aborts(tmp_path: Path):
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    with pytest.raises(BaselineError, match="does not match claimed models"):
        _resolve(
            baseline_root=tmp_path, baseline_id="BL-OPENAI-GPT4O-0001", claimed_models=["claude-3-5-sonnet-20241022"]
        )


def test_resolve_family_and_coarse_are_opt_in(tmp_path: Path):
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    assert _resolve(baseline_root=tmp_path, claimed_models=["gpt-4o-2024-08-06"]) is None
    family = _resolve(baseline_root=tmp_path, claimed_models=["gpt-4o-2024-08-06"], allow_family=True)
    assert family is not None and family.baseline_id == "BL-OPENAI-GPT4O-0001"

    coarse_root = tmp_path / "coarse"
    _save_baseline(coarse_root, "BL-OPENAI-GPT35-0001", model="gpt-3.5-turbo")
    assert _resolve(baseline_root=coarse_root, claimed_models=["gpt-4o"]) is None
    assert _resolve(baseline_root=coarse_root, claimed_models=["gpt-4o"], allow_family=True) is None
    coarse = _resolve(baseline_root=coarse_root, claimed_models=["gpt-4o"], allow_coarse=True)
    assert coarse is not None and coarse.baseline_id == "BL-OPENAI-GPT35-0001"


def test_resolve_auto_scan_skips_malformed(tmp_path: Path):
    _save_baseline(tmp_path, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    (tmp_path / "BL-BROKEN-0001.json").write_text('{"schema": 3}', encoding="utf-8")
    record = _resolve(baseline_root=tmp_path, claimed_models=["gpt-4o"])
    assert record is not None and record.baseline_id == "BL-OPENAI-GPT4O-0001"


async def test_run_records_selected_baseline_in_versions(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    baseline_root = tmp_path / "baselines"
    _save_baseline(baseline_root, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=baseline_root,
    )
    assert bundle.versions["baselines"] == "BL-OPENAI-GPT4O-0001"
    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["versions"]["baselines"] == "BL-OPENAI-GPT4O-0001"
    # Schema-2 baseline reference (docs/08 §10.2): id + match provenance.
    assert bundle.baseline is not None
    assert bundle.baseline.baseline_id == "BL-OPENAI-GPT4O-0001"
    assert bundle.baseline.matched_on == ["gpt-4o"]
    assert bundle.baseline.captured_at == "2026-08-06T00:00:00+00:00"
    assert parsed["baseline"] == {
        "baseline_id": "BL-OPENAI-GPT4O-0001",
        "matched_on": ["gpt-4o"],
        "captured_at": "2026-08-06T00:00:00+00:00",
    }


async def test_run_no_match_writes_none(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=tmp_path / "empty",
    )
    assert bundle.versions["baselines"] == "none"
    assert bundle.baseline is None
    parsed = json.loads((tmp_path / f"{bundle.run_id}.json").read_text(encoding="utf-8"))
    assert parsed["baseline"] is None


async def test_run_family_match_requires_allow_family(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path
):
    baseline_root = tmp_path / "baselines"
    _save_baseline(baseline_root, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o-2024-08-06"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=baseline_root,
    )
    assert bundle.versions["baselines"] == "none"
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o-2024-08-06"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=baseline_root,
        allow_family=True,
    )
    assert bundle.versions["baselines"] == "BL-OPENAI-GPT4O-0001"


async def test_run_explicit_missing_id_aborts(orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path):
    with pytest.raises(BaselineError, match="no baseline 'BL-NOPE-0001'"):
        await orchestrator.run(
            endpoint="https://fake.example/v1",
            api_key=fake_server.valid_key,
            claimed_models=["gpt-4o"],
            manifest_path=manifest,
            mode="adhoc",
            out_dir=tmp_path,
            baseline_root=tmp_path / "baselines",
            baseline_id="BL-NOPE-0001",
        )


class _BaselineAwareProbe:
    """Custom test probe that surfaces ctx.selected_baseline in its notes."""

    id = "test.baseline_aware"
    domain = Domain.PLATFORM
    weight = 1.0
    samples = 1

    def skip_reason(self, surface) -> str | None:
        return None

    async def run(self, ctx) -> ProbeResult:
        selected = ctx.selected_baseline
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=1,
            attempts=1,
            notes=[f"selected_baseline={selected.baseline_id if selected else 'none'}"],
        )


async def test_selected_baseline_reaches_custom_probe(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
    baseline_root = tmp_path / "baselines"
    _save_baseline(baseline_root, "BL-OPENAI-GPT4O-0001", model="gpt-4o")
    monkeypatch.setattr("supgate.orchestrator.load_probes", lambda _: [_BaselineAwareProbe()])
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=baseline_root,
    )
    probe = next(p for p in bundle.probes if p.probe_id == "test.baseline_aware")
    assert probe.notes == ["selected_baseline=BL-OPENAI-GPT4O-0001"]
    assert bundle.versions["baselines"] == "BL-OPENAI-GPT4O-0001"


async def test_custom_probe_sees_none_without_baseline(
    orchestrator: Orchestrator, fake_server, manifest: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("supgate.orchestrator.load_probes", lambda _: [_BaselineAwareProbe()])
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="adhoc",
        out_dir=tmp_path,
        baseline_root=tmp_path / "empty",
    )
    probe = next(p for p in bundle.probes if p.probe_id == "test.baseline_aware")
    assert probe.notes == ["selected_baseline=none"]
