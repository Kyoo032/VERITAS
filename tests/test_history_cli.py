"""Detailed and JSON history CLI behavior (docs/12 §8 P1)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from supgate.cli import app
from supgate.models import (
    AssuranceVerdict,
    Domain,
    ProbeResult,
    RunBundle,
    Verdict,
)
from supgate.store import RunStore

runner = CliRunner()


def _install_store(monkeypatch, tmp_path) -> RunStore:
    store = RunStore(tmp_path / "history.db")
    monkeypatch.setattr("supgate.cli.RunStore", lambda: store)
    return store


def _bundle() -> RunBundle:
    return RunBundle(
        run_id="SUP-HISTORY-1",
        endpoint="https://supplier.example/v1",
        claimed_models=["gpt-4o"],
        mode="full",
        started_at="2026-08-15T01:00:00+00:00",
        finished_at="2026-08-15T01:01:00+00:00",
        versions={"python": "3.14.2", "schema": 2},
        overall_score=92.0,
        assurance=AssuranceVerdict(level="B"),
        probes=[
            ProbeResult(
                probe_id="p0.echo",
                domain=Domain.PLATFORM,
                verdict=Verdict.PASS,
                score=100.0,
                attempts=1,
                successes=1,
                metrics={"latency_ms": 12},
                notes=["ok"],
                evidence_ref=["evidence/echo.json"],
            )
        ],
    )


def test_history_summary_json_is_exactly_one_parseable_list(monkeypatch, tmp_path):
    store = _install_store(monkeypatch, tmp_path)
    store.record_run(_bundle())
    result = runner.invoke(app, ["history", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["run_id"] == "SUP-HISTORY-1"


def test_history_detail_text_and_exact_json(monkeypatch, tmp_path):
    store = _install_store(monkeypatch, tmp_path)
    store.record_run(_bundle())
    text = runner.invoke(app, ["history", "--run-id", "SUP-HISTORY-1"])
    assert text.exit_code == 0
    assert "run_id: SUP-HISTORY-1" in text.stdout
    assert "p0.echo" in text.stdout
    exact = runner.invoke(app, ["history", "--run-id", "SUP-HISTORY-1", "--json"])
    assert exact.exit_code == 0
    payload = json.loads(exact.stdout)
    assert payload["run_id"] == "SUP-HISTORY-1"
    assert payload["probes"][0]["metrics"] == {"latency_ms": 12}


def test_history_limit_zero_is_empty_and_negative_aborts(monkeypatch, tmp_path):
    store = _install_store(monkeypatch, tmp_path)
    store.record_run(_bundle())
    zero = runner.invoke(app, ["history", "--limit", "0", "--json"])
    assert zero.exit_code == 0
    assert json.loads(zero.stdout) == []
    negative = runner.invoke(app, ["history", "--limit", "-1"])
    assert negative.exit_code == 3
    assert "--limit must not be negative" in negative.output


def test_history_missing_run_is_explicit_exit_3(monkeypatch, tmp_path):
    _install_store(monkeypatch, tmp_path)
    result = runner.invoke(app, ["history", "--run-id", "SUP-MISSING", "--json"])
    assert result.exit_code == 3
    assert "run id 'SUP-MISSING' not found" in result.output
