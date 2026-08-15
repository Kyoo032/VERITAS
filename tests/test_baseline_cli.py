"""CLI surface for the baseline sub-app: env-only keys, option wiring,
list/show/select behavior, and malformed-file handling (docs/02 §9)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from supgate.baselines import BaselineRecord, BaselineStore
from supgate.cli import app

runner = CliRunner()


def _seed_baseline(tmp_path: Path, **kwargs) -> None:
    BaselineStore(tmp_path).save(
        BaselineRecord(
            baseline_id="BL-OPENAI-GPT4O-0001",
            provider_label="openai",
            vendor="openai",
            model="gpt-4o",
            endpoint="https://api.openai.com/v1",
            captured_at="2026-08-06T00:00:00+00:00",
            claimed_models=["gpt-4o"],
            fingerprints={"id_prefix": {"family": "chatcmpl-", "samples": 3, "consistent": True}},
            **kwargs,
        )
    )


def test_baseline_no_args_prints_help():
    result = runner.invoke(app, ["baseline"])
    assert result.exit_code == 2  # typer: bare sub-app invocation shows help as a usage error
    assert "record" in result.output and "list" in result.output


# --- record: env-only keys and explicit per-run endpoint ---------------------


def test_baseline_record_aborts_without_key_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPGATE_KEY", raising=False)
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://api.openai.com/v1", "--key-env", "SUPGATE_KEY",
         "--out", str(tmp_path / "b"), "--evidence-out", str(tmp_path / "r")],
    )
    assert result.exit_code == 3
    assert "SUPGATE_KEY" in result.output
    assert not (tmp_path / "b").exists() or list((tmp_path / "b").glob("*.json")) == []


def test_baseline_record_aborts_without_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test-0000000000000000")
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--key-env", "SUPGATE_KEY", "--out", str(tmp_path / "b")],
    )
    assert result.exit_code == 2  # typer: missing required option
    assert "--endpoint" in result.output


def test_baseline_record_aborts_without_key_env_option(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPGATE_OPENAI_OFFICIAL_KEY", "sk-test-0000000000000000")
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://api.openai.com/v1", "--out", str(tmp_path / "b")],
    )
    assert result.exit_code == 2  # typer: missing required option
    assert "--key-env" in result.output


def test_baseline_record_aborts_on_unknown_vendor(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test-0000000000000000")
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "gemini", "--model", "gpt-4o",
         "--endpoint", "https://api.example/v1", "--key-env", "SUPGATE_KEY",
         "--out", str(tmp_path / "b")],
    )
    assert result.exit_code == 3
    assert "unknown vendor 'gemini'" in result.output


def test_baseline_record_wires_env_key_and_options(monkeypatch, tmp_path):
    captured = {}

    async def fake_record_baseline(**kwargs):
        captured.update(kwargs)
        return BaselineRecord(
            baseline_id="BL-OPENAI-GPT4O-0001",
            provider_label="openai",
            vendor="openai",
            model="gpt-4o",
            endpoint="https://api.openai.com/v1",
            captured_at="2026-08-06T00:00:00+00:00",
            claimed_models=["gpt-4o"],
        )

    monkeypatch.setenv("SUPGATE_OPENAI_OFFICIAL_KEY", "sk-official-secret1234567890")
    monkeypatch.setattr("supgate.cli.record_baseline", fake_record_baseline)
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://api.openai.com/v1", "--key-env", "SUPGATE_OPENAI_OFFICIAL_KEY",
         "--label", "official", "--model-version", "gpt-4o-2024-08-06",
         "--samples", "5", "--streams", "2", "--confirm-official",
         "--budget-usd", "5", "--timeout-s", "15",
         "--out", str(tmp_path / "b"), "--evidence-out", str(tmp_path / "r")],
    )
    assert result.exit_code == 0
    assert captured["vendor"] == "openai"
    assert captured["model"] == "gpt-4o"
    assert captured["api_key"] == "sk-official-secret1234567890"
    assert captured["label"] == "official"
    assert captured["model_version"] == "gpt-4o-2024-08-06"
    assert captured["samples"] == 5 and captured["streams"] == 2
    assert captured["confirmed_official"] is True
    assert captured["endpoint"] == "https://api.openai.com/v1"
    assert captured["budget_usd"] == 5.0
    assert captured["timeout_s"] == 15.0
    assert "sk-official-secret1234567890" not in result.output
    assert "recorded baseline BL-OPENAI-GPT4O-0001" in result.output


def test_baseline_record_ignores_official_base_url_env(monkeypatch, tmp_path):
    """No shared endpoint default: SUPGATE_OFFICIAL_BASE_URL must NOT be
    consulted; the endpoint is required explicitly on every run."""
    captured = {}

    async def fake_record_baseline(**kwargs):
        captured.update(kwargs)
        return BaselineRecord(
            baseline_id="BL-OPENAI-GPT4O-0001", vendor="openai", model="gpt-4o",
            endpoint=kwargs["endpoint"], captured_at="2026-08-06T00:00:00+00:00",
            claimed_models=["gpt-4o"], provider_label="openai",
        )

    monkeypatch.setenv("SUPGATE_OPENAI_OFFICIAL_KEY", "sk-test-0000000000000000")
    monkeypatch.setenv("SUPGATE_OFFICIAL_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setattr("supgate.cli.record_baseline", fake_record_baseline)
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://explicit.example/v1", "--key-env", "SUPGATE_OPENAI_OFFICIAL_KEY",
         "--out", str(tmp_path / "b")],
    )
    assert result.exit_code == 0
    assert captured["endpoint"] == "https://explicit.example/v1"


def test_baseline_record_aborts_on_out_of_range_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPGATE_KEY", "sk-test-0000000000000000")
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://api.openai.com/v1", "--key-env", "SUPGATE_KEY",
         "--samples", "99", "--out", str(tmp_path / "b")],
    )
    assert result.exit_code == 3
    assert "--samples" in result.output


def test_baseline_record_dry_run_does_not_resolve_key_create_dirs_or_request(
    monkeypatch, tmp_path
):
    out = tmp_path / "b"
    evidence = tmp_path / "r"
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.setattr(
        "supgate.cli._resolve_key",
        lambda _: (_ for _ in ()).throw(AssertionError("must not resolve key")),
    )

    async def forbidden_record(**kwargs):
        raise AssertionError("must not record")

    monkeypatch.setattr("supgate.cli.record_baseline", forbidden_record)
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://api.openai.com/v1", "--key-env", "MISSING_KEY",
         "--out", str(out), "--evidence-out", str(evidence), "--dry-run", "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["key_env"] == "MISSING_KEY"
    assert payload["plan"]["requests"] == 17
    assert payload["plan"]["max_requests"] == 33
    assert not out.exists()
    assert not evidence.exists()


def test_baseline_record_json_actual_is_one_object_and_preview_is_stderr(
    monkeypatch, tmp_path
):
    async def fake_record_baseline(**kwargs):
        return BaselineRecord(
            baseline_id="BL-OPENAI-GPT4O-0001", provider_label="openai",
            vendor="openai", model="gpt-4o", endpoint=kwargs["endpoint"],
            captured_at="2026-08-06T00:00:00+00:00", claimed_models=["gpt-4o"],
        )

    monkeypatch.setenv("SUPGATE_JSON_KEY", "raw-json-secret")
    monkeypatch.setattr("supgate.cli.record_baseline", fake_record_baseline)
    result = runner.invoke(
        app,
        ["baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
         "--endpoint", "https://api.openai.com/v1", "--key-env", "SUPGATE_JSON_KEY",
         "--budget-usd", "10", "--timeout-s", "8", "--json",
         "--out", str(tmp_path / "b")],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["baseline"]["baseline_id"] == "BL-OPENAI-GPT4O-0001"
    assert "raw-json-secret" not in result.stdout
    assert "baseline request/cost preview" in result.stderr


def test_baseline_record_rejects_non_positive_cap_and_timeout(tmp_path):
    common = [
        "baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
        "--endpoint", "https://api.openai.com/v1", "--key-env", "NO_KEY",
        "--dry-run", "--out", str(tmp_path / "b"),
    ]
    cap = runner.invoke(app, [*common, "--budget-usd", "0"])
    timeout = runner.invoke(app, [*common, "--timeout-s", "-1"])
    assert cap.exit_code == 3 and "--budget-usd must be positive" in cap.output
    assert timeout.exit_code == 3 and "--timeout-s must be positive" in timeout.output
    assert not (tmp_path / "b").exists()


def test_baseline_record_dry_run_rejects_secret_bearing_endpoints_without_leak_or_dirs(
    tmp_path,
):
    secret = "never-print-this-token"
    endpoints = [
        f"https://user:{secret}@api.example/v1",
        f"https://api.example/v1?api_key={secret}",
        f"https://api.example/v1#{secret}",
    ]
    for index, endpoint in enumerate(endpoints):
        out = tmp_path / f"b{index}"
        evidence = tmp_path / f"r{index}"
        result = runner.invoke(
            app,
            [
                "baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
                "--endpoint", endpoint, "--key-env", "MISSING", "--dry-run", "--json",
                "--out", str(out), "--evidence-out", str(evidence),
            ],
        )
        assert result.exit_code == 3
        assert secret not in result.output
        assert not out.exists()
        assert not evidence.exists()


def test_baseline_record_dry_run_budget_and_preview_use_retry_aware_estimate(tmp_path):
    common = [
        "baseline", "record", "--vendor", "openai", "--model", "gpt-4o",
        "--endpoint", "https://api.example/v1", "--key-env", "MISSING", "--dry-run",
        "--out", str(tmp_path / "b"),
    ]
    preview = runner.invoke(app, common)
    assert preview.exit_code == 0
    assert "requests=17 max_requests=33" in preview.output
    assert "estimated_usd=" in preview.output and "estimated_max_usd=" in preview.output

    planned = runner.invoke(app, [*common, "--json"])
    payload = json.loads(planned.stdout)
    max_estimate = payload["plan"]["estimated_max_usd"]
    nominal = payload["plan"]["estimated_usd"]
    midpoint = (nominal + max_estimate) / 2
    capped = runner.invoke(app, [*common, "--json", "--budget-usd", str(midpoint)])
    assert capped.exit_code == 0
    assert json.loads(capped.stdout)["within_budget"] is False


# --- list / show / select ---------------------------------------------------


def test_baseline_list_empty_dir(tmp_path):
    result = runner.invoke(app, ["baseline", "list", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "no baselines recorded" in result.output


def test_baseline_list_shows_records(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "list", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "BL-OPENAI-GPT4O-0001" in result.output
    assert "vendor=openai" in result.output


def test_baseline_list_json(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "list", "--out", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["baselines"][0]["baseline_id"] == "BL-OPENAI-GPT4O-0001"
    assert payload["baselines"][0]["schema"] == 2


def test_baseline_list_filters(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "list", "--out", str(tmp_path), "--vendor", "anthropic"])
    assert "no baselines recorded" in result.output
    result = runner.invoke(app, ["baseline", "list", "--out", str(tmp_path), "--model", "gpt-4o"])
    assert "BL-OPENAI-GPT4O-0001" in result.output


def test_baseline_list_skips_malformed_with_warning(tmp_path):
    _seed_baseline(tmp_path)
    (tmp_path / "BL-BROKEN-0001.json").write_text("{nope", encoding="utf-8")
    result = runner.invoke(app, ["baseline", "list", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "BL-OPENAI-GPT4O-0001" in result.output
    assert "warning: skipped BL-BROKEN-0001.json" in result.output


def test_baseline_show_prints_full_record(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "show", "BL-OPENAI-GPT4O-0001", "--out", str(tmp_path)])
    assert result.exit_code == 0
    record = json.loads(result.output)
    assert record["baseline_id"] == "BL-OPENAI-GPT4O-0001"
    assert record["fingerprints"]["id_prefix"]["family"] == "chatcmpl-"


def test_baseline_show_missing_aborts(tmp_path):
    result = runner.invoke(app, ["baseline", "show", "BL-NOPE-0001", "--out", str(tmp_path)])
    assert result.exit_code == 3
    assert "no baseline 'BL-NOPE-0001'" in result.output


def test_baseline_show_malformed_aborts(tmp_path):
    (tmp_path / "BL-BROKEN-0001.json").write_text('{"schema": 2, "baseline_id": "other"}', encoding="utf-8")
    result = runner.invoke(app, ["baseline", "show", "BL-BROKEN-0001", "--out", str(tmp_path)])
    assert result.exit_code == 3
    assert "does not match file name" in result.output


def test_baseline_show_unsafe_id_aborts(tmp_path):
    result = runner.invoke(app, ["baseline", "show", "../escape", "--out", str(tmp_path)])
    assert result.exit_code == 3
    assert "unsafe baseline id" in result.output


def test_baseline_select_no_match_exits_zero(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "select", "--model", "claude-3-5-sonnet-20241022", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "no matching baseline" in result.output


def test_baseline_select_exact(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "select", "--model", "gpt-4o", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "BL-OPENAI-GPT4O-0001" in result.output
    assert "kind=exact" in result.output
    assert "matched_on=gpt-4o" in result.output


def test_baseline_select_family_requires_flag(tmp_path):
    _seed_baseline(tmp_path)
    result = runner.invoke(app, ["baseline", "select", "--model", "gpt-4o-2024-08-06", "--out", str(tmp_path)])
    assert "no matching baseline" in result.output
    result = runner.invoke(
        app,
        ["baseline", "select", "--model", "gpt-4o-2024-08-06", "--allow-family", "--out", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "kind=family" in result.output


def test_baseline_select_requires_model(tmp_path):
    result = runner.invoke(app, ["baseline", "select", "--out", str(tmp_path)])
    assert result.exit_code == 3
    assert "--model" in result.output


def test_baseline_select_ignores_malformed(tmp_path):
    _seed_baseline(tmp_path)
    (tmp_path / "BL-BROKEN-0001.json").write_text('{"schema": 3}', encoding="utf-8")
    result = runner.invoke(app, ["baseline", "select", "--model", "gpt-4o", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "BL-OPENAI-GPT4O-0001" in result.output
    assert "warning: skipped" in result.output
