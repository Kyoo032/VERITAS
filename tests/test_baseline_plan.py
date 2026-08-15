"""Baseline-record cost planner: deterministic plan + endpoint-free dry-run.

docs/12-operator-test-plan.md §8 P1 — baseline-record cost preview/cap and a
dry-run planner that makes no endpoint requests. Pure domain layer; no CLI
wiring here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from supgate.baseline_plan import (
    _RESPONSES_MAX_OUTPUT_TOKENS,
    plan_baseline_record,
    plan_baseline_record_dict,
)
from supgate.probes.d4_billing import RecountDeviationProbe, WrapOffsetProbe


def test_plan_default_gpt4o_includes_billing_and_p0() -> None:
    plan = plan_baseline_record(model="gpt-4o", samples=3, streams=1, run_p0_gate=True)

    assert plan.model == "gpt-4o"
    assert plan.samples == 3
    assert plan.streams == 1
    assert plan.run_p0_gate is True
    assert plan.billing_included is True
    assert plan.encoding is not None
    assert plan.rates is not None

    stage_ids = [s.id for s in plan.stages]
    assert stage_ids == [
        "p0.echo",
        "p0.models",
        "baseline.capture.responses",
        "baseline.capture.chat",
        "baseline.capture.stream",
        "baseline.capture.self_report",
        "d4.recount_deviation",
        "d4.wrap_offset",
    ]

    by_id = {s.id: s for s in plan.stages}
    assert by_id["p0.echo"].requests == 1
    assert by_id["p0.models"].requests == 1
    assert by_id["baseline.capture.responses"].requests == 1
    assert by_id["baseline.capture.chat"].requests == 3
    assert by_id["baseline.capture.stream"].requests == 1
    assert by_id["baseline.capture.self_report"].requests == 3
    assert by_id["d4.recount_deviation"].requests == RecountDeviationProbe.samples
    assert by_id["d4.wrap_offset"].requests == WrapOffsetProbe.samples

    expected = 1 + 1 + 1 + 3 + 1 + 3 + RecountDeviationProbe.samples + WrapOffsetProbe.samples
    assert expected == 17
    assert plan.requests == expected
    assert plan.max_requests == 33  # every logical request except /responses may retry once
    assert plan.estimated_usd > 0.0
    assert plan.estimated_max_usd > plan.estimated_usd
    assert plan.estimated_max_prompt_tokens >= plan.estimated_prompt_tokens
    assert plan.estimated_max_completion_tokens > plan.estimated_completion_tokens

    assert by_id["baseline.capture.responses"].max_requests == 1
    for stage_id, stage in by_id.items():
        if stage_id != "baseline.capture.responses":
            assert stage.max_requests == stage.requests * 2
            assert stage.estimated_max_usd == pytest.approx(stage.estimated_usd * 2)
    assert "provider-enforced" in plan.assumptions["unbounded_provider_responses"]["p0.models"]
    # F1: the /responses probe pins max_output_tokens=16, so its completion
    # assumption is a provider-enforced upper bound, not an unbounded estimate.
    assert "baseline.capture.responses" not in plan.assumptions["unbounded_provider_responses"]
    assert (
        plan.assumptions["response_tokens_per_request"]["baseline.capture.responses"]
        == _RESPONSES_MAX_OUTPUT_TOKENS
        == 16
    )
    responses_stage = by_id["baseline.capture.responses"]
    assert responses_stage.estimated_completion_tokens == 16
    assert responses_stage.estimated_max_completion_tokens == 16
    assert any("max_output_tokens=16" in note for note in responses_stage.notes)
    assert "worst case is nominal x2" in plan.assumptions["request_math"]
    assert "stage-granular" in plan.assumptions["budget_granularity"]


def test_plan_omits_billing_when_encoding_unknown() -> None:
    plan = plan_baseline_record(
        model="claude-3-5-sonnet-20241022", samples=3, streams=1, run_p0_gate=True
    )
    assert plan.billing_included is False
    assert plan.encoding is None
    stage_ids = [s.id for s in plan.stages]
    assert "d4.recount_deviation" not in stage_ids
    assert "d4.wrap_offset" not in stage_ids
    assert plan.requests == 1 + 1 + 1 + 3 + 1 + 3  # no billing


def test_plan_omits_p0_when_gate_disabled() -> None:
    plan = plan_baseline_record(model="gpt-4o", samples=2, streams=2, run_p0_gate=False)
    assert "p0.echo" not in [s.id for s in plan.stages]
    assert plan.requests == (
        1 + 1 + 2 + 2 + 2 + RecountDeviationProbe.samples + WrapOffsetProbe.samples
    )


def test_plan_request_math_scales_with_samples_streams() -> None:
    a = plan_baseline_record(model="gpt-4o", samples=1, streams=1, run_p0_gate=True)
    b = plan_baseline_record(model="gpt-4o", samples=5, streams=3, run_p0_gate=True)
    # delta: chat +4, stream +2, self_report +4 = +10
    assert b.requests - a.requests == 10
    assert b.max_requests - a.max_requests == 20


def test_plan_dict_is_json_serializable() -> None:
    payload = plan_baseline_record_dict(model="gpt-4o", samples=3, streams=1)
    dumped = json.dumps(payload)
    loaded = json.loads(dumped)
    assert loaded["requests"] == payload["requests"]
    assert loaded["estimated_max_usd"] == payload["estimated_max_usd"]
    assert "estimated_upper_bound_usd" not in loaded
    assert isinstance(loaded["stages"], list)
    assert loaded["stages"][0]["id"] == "p0.echo"


def test_plan_rejects_invalid_args() -> None:
    with pytest.raises(ValueError, match="model must not be empty"):
        plan_baseline_record(model="  ", samples=1, streams=1)
    with pytest.raises(ValueError, match="samples/streams"):
        plan_baseline_record(model="gpt-4o", samples=0, streams=1)
    with pytest.raises(ValueError, match="samples/streams"):
        plan_baseline_record(model="gpt-4o", samples=1, streams=0)


def test_plan_makes_zero_http_requests_and_no_artifacts(tmp_path: Path, monkeypatch) -> None:
    """Dry-run planner: exactly zero HTTP and no output/evidence dirs or files."""
    monkeypatch.chdir(tmp_path)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p != tmp_path}

    def guarded_init(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("planner must not construct httpx.AsyncClient")

    with (
        patch.object(httpx.AsyncClient, "__init__", guarded_init),
        patch("httpx.AsyncClient.request", side_effect=AssertionError("no request")),
        patch("httpx.AsyncClient.stream", side_effect=AssertionError("no stream")),
    ):
        plan = plan_baseline_record(model="gpt-4o", samples=3, streams=1)
        payload = plan_baseline_record_dict(model="gpt-4o", samples=3, streams=1)

    assert plan.requests > 0
    assert payload["requests"] == plan.requests

    after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p != tmp_path}
    assert after == before
    for name in ("baselines", "runs", "evidence", "output"):
        assert not (tmp_path / name).exists()

    assert httpx.AsyncClient.__init__ is not guarded_init
