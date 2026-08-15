"""CLI behavior (§12): key handling, exit codes, stubs."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from supgate.baselines import BaselineError
from supgate.cli import _parse_sla, app
from supgate.models import SLA, Domain, ProbeResult, RunBundle, Verdict

runner = CliRunner()


def test_parse_sla_maps_ttft_tpot_e2e():
    sla = _parse_sla("ttft=5,tpot=0.5,e2e=60")
    assert sla == SLA(ttft_s=5.0, tpot_ms=500.0, e2e_s=60.0)


def test_parse_sla_empty_returns_defaults():
    assert _parse_sla(None) == SLA()
    assert _parse_sla("") == SLA()


def test_parse_sla_unknown_key_aborts(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--sla", "tttf=5"],
    )
    assert result.exit_code == 3
    assert "tttf" in result.output


def test_parse_sla_missing_value_aborts(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--sla", "ttft=,e2e=60"],
    )
    assert result.exit_code == 3


def test_parse_sla_negative_value_aborts(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--sla", "ttft=-5"],
    )
    assert result.exit_code == 3


def test_run_rejects_invalid_mode(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--mode", "quick"],
    )
    assert result.exit_code == 3
    assert "quick" in result.output


def test_run_aborts_without_key_env(monkeypatch):
    monkeypatch.delenv("SUPGATE_KEY", raising=False)
    result = runner.invoke(app, ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o"])
    assert result.exit_code == 3
    assert "SUPGATE_KEY" in result.output


def test_run_requires_model(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(app, ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY"])
    assert result.exit_code == 3
    assert "model" in result.output


def test_run_concurrency_out_of_range_aborts(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--concurrency", "99"],
    )
    assert result.exit_code == 3
    assert "concurrency" in result.output
    assert "run aborted:" in result.output


def test_invalid_sla_aborts(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--sla", "ttft=nope"],
    )
    assert result.exit_code == 3


def test_invalid_sla_prints_specific_error_once_not_run_aborted(monkeypatch):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY", "--model", "gpt-4o", "--sla", "ttft=nope"],
    )
    assert result.exit_code == 3
    assert result.output.count("invalid SLA spec: 'ttft=nope'") == 1
    assert "run aborted:" not in result.output


def test_history_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("supgate.cli.RunStore", lambda: __import__("supgate.store", fromlist=["RunStore"]).RunStore(tmp_path / "h.db"))
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0


def _fake_bundle() -> RunBundle:
    return RunBundle(
        run_id="SUP-TEST-0001",
        endpoint="https://x.example/v1",
        claimed_models=["gpt-4o"],
        mode="adhoc",
        started_at="2026-08-07T00:00:00+00:00",
        finished_at="2026-08-07T00:00:01+00:00",
        versions={"supgate": "0.1.0", "manifest": "1", "baselines": "none"},
    )


def test_run_wires_baseline_options(monkeypatch, tmp_path):
    captured = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs

        async def run(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return _fake_bundle()

    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    monkeypatch.setattr("supgate.cli.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "supgate.cli.RunStore",
        lambda: __import__("supgate.store", fromlist=["RunStore"]).RunStore(tmp_path / "h.db"),
    )
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY",
         "--model", "gpt-4o", "--baseline-dir", str(tmp_path / "b"),
         "--baseline-id", "BL-OPENAI-GPT4O-0001",
         "--allow-family-baseline", "--allow-coarse-baseline",
         "--timeout-s", "12.5", "--continue-forensics"],
    )
    assert result.exit_code == 0
    assert captured["run_kwargs"]["baseline_root"] == tmp_path / "b"
    assert captured["run_kwargs"]["baseline_id"] == "BL-OPENAI-GPT4O-0001"
    assert captured["run_kwargs"]["allow_family"] is True
    assert captured["run_kwargs"]["allow_coarse"] is True
    assert captured["init_kwargs"]["budget_usd"] is None
    assert captured["init_kwargs"]["timeout_s"] == 12.5
    assert captured["run_kwargs"]["continue_forensics"] is True
    invocation = captured["run_kwargs"]["invocation"]
    assert invocation.key_env == "SUPGATE_KEY"
    assert invocation.timeout_s == 12.5
    assert invocation.continue_forensics is True
    assert "sk-test" not in invocation.model_dump_json()


def test_run_defaults_baseline_dir_to_baselines(monkeypatch, tmp_path):
    captured = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            captured.update(kwargs)
            return _fake_bundle()

    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    monkeypatch.setattr("supgate.cli.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "supgate.cli.RunStore",
        lambda: __import__("supgate.store", fromlist=["RunStore"]).RunStore(tmp_path / "h.db"),
    )
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY",
         "--model", "gpt-4o"],
    )
    assert result.exit_code == 0
    assert captured["baseline_root"] == Path("baselines")
    assert captured["baseline_id"] is None
    assert captured["allow_family"] is False
    assert captured["allow_coarse"] is False


def test_run_explicit_missing_baseline_aborts(monkeypatch, tmp_path):
    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            raise BaselineError("no baseline 'BL-NOPE-0001' in baselines")

    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    monkeypatch.setattr("supgate.cli.Orchestrator", FakeOrchestrator)
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY",
         "--model", "gpt-4o", "--baseline-id", "BL-NOPE-0001"],
    )
    assert result.exit_code == 3
    assert "no baseline 'BL-NOPE-0001'" in result.output
    assert "run aborted:" in result.output


def test_run_rejects_non_positive_timeout_before_key_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPGATE_KEY", raising=False)
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY",
         "--model", "gpt-4o", "--timeout-s", "0", "--out", str(tmp_path / "runs")],
    )
    assert result.exit_code == 3
    assert "--timeout-s must be positive" in result.output
    assert not (tmp_path / "runs").exists()


def test_run_json_is_one_object_without_progress_and_warns_on_no_baseline(
    monkeypatch, tmp_path
):
    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            assert kwargs["on_probe_complete"] is None
            return _fake_bundle()

    monkeypatch.setenv("SUPGATE_KEY", "raw-secret-value")
    monkeypatch.setattr("supgate.cli.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "supgate.cli.RunStore",
        lambda: __import__("supgate.store", fromlist=["RunStore"]).RunStore(tmp_path / "h.db"),
    )
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY",
         "--model", "gpt-4o", "--json", "--out", str(tmp_path / "runs")],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "SUP-TEST-0001"
    assert payload["bundle_path"].endswith("SUP-TEST-0001.json")
    assert "p0.echo" not in result.stdout
    assert "raw-secret-value" not in result.stdout
    assert "WARNING: automatic baseline selection found no matching baseline" in result.stderr


def test_run_text_progress_uses_completion_callback(monkeypatch, tmp_path):
    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            callback = kwargs["on_probe_complete"]
            callback(
                1, 2,
                ProbeResult(probe_id="p0.echo", domain=Domain.PLATFORM, verdict=Verdict.PASS),
            )
            callback(
                2, 2,
                ProbeResult(probe_id="p0.models", domain=Domain.PLATFORM, verdict=Verdict.WARN),
            )
            return _fake_bundle()

    monkeypatch.setenv("SUPGATE_KEY", "sk-test")
    monkeypatch.setattr("supgate.cli.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        "supgate.cli.RunStore",
        lambda: __import__("supgate.store", fromlist=["RunStore"]).RunStore(tmp_path / "h.db"),
    )
    result = runner.invoke(
        app,
        ["run", "--base-url", "https://x.example/v1", "--key-env", "SUPGATE_KEY",
         "--model", "gpt-4o", "--out", str(tmp_path / "runs")],
    )
    assert result.exit_code == 0
    assert "1/2 p0.echo pass" in result.stdout
    assert "2/2 p0.models warn" in result.stdout


def test_unimplemented_m4_commands_fail_loudly():
    report = runner.invoke(app, ["report", "runs/x.json"])
    export = runner.invoke(app, ["export-qa", "runs/x.json"])
    assert report.exit_code == 3
    assert export.exit_code == 3
    assert "not implemented" in report.output
    assert "not implemented" in export.output


def test_baseline_is_subapp_with_help():
    result = runner.invoke(app, ["baseline"])
    assert result.exit_code == 2  # bare sub-app invocation is a usage error (help shown)
    assert "record" in result.output
