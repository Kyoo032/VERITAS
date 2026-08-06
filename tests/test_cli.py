"""CLI behavior (§12): key handling, exit codes, stubs."""

from __future__ import annotations

from typer.testing import CliRunner

from supgate.cli import _parse_sla, app
from supgate.models import SLA

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


def test_stubs_exist():
    assert runner.invoke(app, ["report", "runs/x.json"]).exit_code == 0
    assert runner.invoke(app, ["baseline"]).exit_code == 0
    assert runner.invoke(app, ["export-qa", "runs/x.json"]).exit_code == 0
