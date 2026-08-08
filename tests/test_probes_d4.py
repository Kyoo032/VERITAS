"""D4 relay fingerprint probes (docs/06-m2-probe-spec.md §4.1-§4.7): d4.headers_diff, d4.id_prefix, d4.model_echo, d4.self_report, d4.canary_echo, d4.sse_timing, d4.rotation."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

import supgate.probes.d4_fingerprint as d4_fingerprint_module
from supgate.baselines import BaselineRecord, BaselineSurface
from supgate.models import Domain, ProbeResult, SurfaceMap, Verdict
from supgate.probes.base import RateLimitError, RunContext
from supgate.probes.d4_fingerprint import (
    CanaryEchoProbe,
    HeadersDiffProbe,
    IdPrefixProbe,
    ModelEchoProbe,
    RotationProbe,
    SelfReportProbe,
    SseTimingProbe,
    _echo_contradicts,
    _fresh_canary,
    _is_buffered,
    claimed_families_of,
    claimed_family_of,
    claimed_providers_of,
    cluster_families,
    content_bucket,
    normalize_content,
    probe_result_capped,
    provider_family_hits,
    provider_of_family,
)
from supgate.registry import CUSTOM_RUNNERS, load_probes


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)


async def test_headers_diff_passes_on_stable_clean_surface(ctx, fake_server):
    result = await HeadersDiffProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 2
    metrics = result.metrics["headers"]
    assert metrics["pairs_stable"] == [True, True]
    assert metrics["hop_present"] is False
    assert metrics["unstable_pairs"] == 0
    assert len(metrics["per_response"]) == 4
    assert all(entry["hop_markers"] == {} for entry in metrics["per_response"])
    assert len(ctx.evidence.refs_for("d4.headers_diff")) == 4

    posts = [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]
    assert len(posts) == 4
    payload = json.loads(posts[0]["body"])
    assert payload["max_tokens"] == 8
    assert payload["temperature"] == 0
    assert payload["messages"][0]["content"] == "Reply with the single word ping."


async def test_headers_diff_warns_on_consistent_hop_markers(ctx, fake_server):
    fake_server.hop_headers = ["via", "x-served-by"]
    result = await HeadersDiffProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("hop marker" in note for note in result.notes)
    metrics = result.metrics["headers"]
    assert metrics["hop_present"] is True
    assert metrics["unstable_pairs"] == 0
    assert metrics["per_response"][0]["hop_markers"] == {
        "via": "fake-via",
        "x-served-by": "fake-x-served-by",
    }


async def test_headers_diff_warns_on_single_unstable_pair(ctx, fake_server):
    original = fake_server._extra_headers
    calls = {"n": 0}

    def jitter_third_call() -> list[tuple[bytes, bytes]]:
        calls["n"] += 1
        if calls["n"] == 3:
            return [(b"x-extra", b"jit-3")]
        return []

    fake_server._extra_headers = jitter_third_call
    try:
        result = await HeadersDiffProbe().run(ctx)
    finally:
        fake_server._extra_headers = original
    assert result.verdict == Verdict.WARN
    assert result.metrics["headers"]["pairs_stable"] == [True, False]
    assert result.metrics["headers"]["unstable_pairs"] == 1


async def test_headers_diff_fails_on_header_jitter(ctx, fake_server):
    fake_server.header_jitter = True
    result = await HeadersDiffProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert result.metrics["headers"]["unstable_pairs"] == 2
    assert any("header set differs" in note for note in result.notes)


async def test_headers_diff_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await HeadersDiffProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.headers_diff")) == 8  # 4 calls x one retry


async def test_headers_diff_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await HeadersDiffProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_headers_diff_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await HeadersDiffProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.headers_diff")) == 4


async def test_headers_diff_non_200_status_fails(ctx):
    ctx.api_key = "sk-wrong"
    result = await HeadersDiffProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("unexpected status 401" in note for note in result.notes)


async def test_headers_diff_metrics_serialize(ctx):
    result = await HeadersDiffProbe().run(ctx)
    dumped = result.model_dump()
    assert dumped["metrics"]["headers"]["pairs_stable"] == [True, True]


def test_probe_result_metrics_is_optional():
    result = ProbeResult(probe_id="x", domain=Domain.D4, verdict=Verdict.PASS)
    assert result.metrics == {}
    assert result.model_dump()["metrics"] == {}


def test_headers_diff_registered_in_manifest(manifest):
    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.headers_diff"]
    assert isinstance(probe, HeadersDiffProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 1.0
    assert probe.samples == 2
    assert probe.skip_reason(SurfaceMap()) is None


def _baseline(family: str) -> BaselineRecord:
    return BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"id_prefix": {"family": family, "samples": 10, "consistent": True}},
    )


async def test_id_prefix_passes_with_matching_baseline(ctx, fake_server):
    ctx.selected_baseline = _baseline("chatcmpl-")
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["id_prefix"]
    assert metrics["baseline_family"] == "chatcmpl-"
    assert metrics["family_match"] is True
    assert metrics["families"] == ["chatcmpl-"]
    assert len(metrics["prefixes"]) == 10
    assert all(p == "chatcmpl-" for p in metrics["prefixes"])

    posts = [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]
    assert len(posts) == 10
    payload = json.loads(posts[0]["body"])
    assert payload["model"] == "gpt-4o"
    assert payload["max_tokens"] == 8
    assert payload["temperature"] == 0
    assert payload["messages"][0]["content"] == "Say the word ping."


async def test_id_prefix_self_consistent_without_baseline(ctx, fake_server):
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["id_prefix"]
    assert metrics["baseline_family"] is None
    assert metrics["family_match"] is None
    assert metrics["families"] == ["chatcmpl-"]
    assert len(metrics["prefixes"]) == 10


async def test_id_prefix_matching_baseline_overrides_claimed_family_passes(ctx, fake_server):
    # docs/06 §4.2 fixture 4: a matched selected baseline family overrides the
    # static claimed-family mapping — stable official ``gen-`` with a
    # ``gen-`` baseline PASSes for a claimed ``gpt-4o`` (the static mapping
    # alone would say chatcmpl-). Metrics keep the families/baseline_family/
    # family_match evidence so reverse_identity corroboration still works.
    fake_server.id_prefix = "gen-"
    ctx.selected_baseline = _baseline("gen-")
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["id_prefix"]
    assert metrics["families"] == ["gen-"]
    assert metrics["baseline_family"] == "gen-"
    assert metrics["family_match"] is True
    assert len(metrics["prefixes"]) == 10
    assert all(p == "gen-" for p in metrics["prefixes"])
    assert any("stable and consistent" in note for note in result.notes)


async def test_id_prefix_custom_family_warns(ctx, fake_server):
    fake_server.id_prefix = "acme."
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.metrics["id_prefix"]["families"] == ["acme"]
    assert any("custom id prefix" in note for note in result.notes)


async def test_id_prefix_different_official_family_warns_without_baseline(ctx, fake_server):
    fake_server.id_prefix = "gen-"
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.metrics["id_prefix"]["families"] == ["gen-"]
    assert any("claimed family" in note for note in result.notes)


async def test_id_prefix_baseline_mismatch_warns(ctx, fake_server):
    ctx.selected_baseline = _baseline("gen-")
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["id_prefix"]
    assert metrics["family_match"] is False
    assert any("does not match baseline" in note for note in result.notes)


async def test_id_prefix_mixed_families_fails(ctx, fake_server):
    fake_server.id_prefix_jitter = True
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["id_prefix"]
    assert metrics["families"] == ["chatcmpl-", "gen-"]
    assert any("rotates" in note for note in result.notes)


async def test_id_prefix_missing_id_fails(ctx, fake_server):
    fake_server.missing_id = True
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("missing or invalid response id" in note for note in result.notes)


async def test_id_prefix_non_retryable_bad_response_fails(ctx):
    ctx.api_key = "sk-wrong"
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("non-retryable bad response (status 401)" in note for note in result.notes)


async def test_id_prefix_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.id_prefix")) == 20  # 10 calls x one retry


async def test_id_prefix_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_id_prefix_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await IdPrefixProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.id_prefix")) == 10


async def test_id_prefix_evidence_and_registry(ctx, manifest):
    result = await IdPrefixProbe().run(ctx)
    assert len(ctx.evidence.refs_for("d4.id_prefix")) == 10
    dumped = result.model_dump()
    assert dumped["metrics"]["id_prefix"]["families"] == ["chatcmpl-"]
    assert dumped["metrics"]["id_prefix"]["family_match"] is None

    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.id_prefix"]
    assert isinstance(probe, IdPrefixProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 1.0
    assert probe.samples == 10
    assert probe.skip_reason(SurfaceMap()) is None


async def test_id_prefix_partial_retry_failure_caps_at_warn_with_metrics(ctx, fake_server, monkeypatch):
    """§1.3: a persistent 429 on one of 10 calls caps d4.id_prefix at WARN
    (never PASS) while the partial prefix metrics (9 samples, baseline family
    match) are retained for QA and reverse-identity corroboration."""

    real_request = d4_fingerprint_module.request_with_retry
    calls = {"n": 0}

    async def _flaky_first(ctx_, probe_id, method, path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError(
                429, "d4.id_prefix: rate-limited (429) after retry — Warn per §10, rerun with backoff"
            )
        return await real_request(ctx_, probe_id, method, path, **kwargs)

    ctx.selected_baseline = _baseline("chatcmpl-")
    monkeypatch.setattr(d4_fingerprint_module, "request_with_retry", _flaky_first)
    result = await IdPrefixProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 90.0  # partial success score preserved
    assert result.successes == 9
    metrics = result.metrics["id_prefix"]
    assert len(metrics["prefixes"]) == 9
    assert metrics["families"] == ["chatcmpl-"]
    assert metrics["baseline_family"] == "chatcmpl-"
    assert metrics["family_match"] is True
    assert any("rate-limited" in note for note in result.notes)


def test_probe_result_capped_never_passes_after_retry_failure():
    """Shared §1.3 routing used by all 11 D4 probes: a persistent 429/5xx
    sample caps the verdict at WARN, never PASS, while transport failures
    keep the FAIL and clean runs are untouched."""

    # Defensive cap: even when successes == attempts (a caller counting
    # samples that survived their own retry), the cap forces WARN.
    result = probe_result_capped(
        "d4.id_prefix", Domain.D4, successes=10, attempts=10,
        warn_failures=True, notes=["rate-limited (429) after retry"],
    )
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    # Partial success keeps the partial score but the verdict stays WARN.
    result = probe_result_capped(
        "d4.id_prefix", Domain.D4, successes=9, attempts=10,
        warn_failures=True, notes=["rate-limited (429) after retry"],
    )
    assert result.verdict == Verdict.WARN
    assert result.score == 90.0
    assert result.successes == 9
    # 5xx warn failures behave identically to 429.
    result = probe_result_capped(
        "d4.id_prefix", Domain.D4, successes=1, attempts=10,
        warn_failures=True, notes=["server error (status 500) after retry"],
    )
    assert result.verdict == Verdict.WARN
    # Transport failures still dominate: FAIL (endpoint_dead keeps working).
    result = probe_result_capped(
        "d4.id_prefix", Domain.D4, successes=0, attempts=10,
        transport_failures=True, notes=["transport error: connection refused"],
    )
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    # No retry failure -> routing untouched.
    result = probe_result_capped("d4.id_prefix", Domain.D4, successes=10, attempts=10)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0


def test_claimed_families_of_returns_all_matching_families():
    assert claimed_families_of(["gpt-4o"]) == ["chatcmpl-"]
    assert claimed_families_of(["claude-3-5-sonnet-20241022"]) == ["msg_"]
    assert claimed_families_of(["gemini-2.0-flash"]) == ["gen-"]
    assert claimed_families_of(["gpt-4o", "claude-3-5-sonnet-20241022"]) == ["chatcmpl-", "msg_"]
    assert claimed_families_of(["claude-3-5-sonnet-20241022", "gemini-2.0-flash"]) == ["msg_", "gen-"]
    # duplicate family tokens collapse to one entry
    assert claimed_families_of(["gpt-4o", "gpt-4o-2024-08-06"]) == ["chatcmpl-"]
    assert claimed_families_of(["mystery-model"]) == []
    assert claimed_families_of([]) == []
    # first-family compatibility is preserved
    assert claimed_family_of(["gpt-4o", "claude-3-5-sonnet"]) == "chatcmpl-"


def test_claimed_providers_of_maps_every_claimed_family():
    assert claimed_providers_of(["gpt-4o"]) == {"openai"}
    assert claimed_providers_of(["claude-3-5-sonnet-20241022"]) == {"anthropic"}
    assert claimed_providers_of(["gpt-4o", "claude-3-5-sonnet-20241022"]) == {"openai", "anthropic"}
    assert claimed_providers_of(["claude-3-5-sonnet", "gemini-2.0-flash"]) == {"anthropic", "gemini"}
    assert claimed_providers_of(["mystery-model"]) == set()


# ---------- d4.model_echo (§4.3) ----------


async def _run_model_echo(ctx) -> ProbeResult:
    return await ModelEchoProbe().run(ctx)


def _echo_metrics(result: ProbeResult) -> dict:
    return result.metrics["model_echo"]


async def test_model_echo_passes_on_match(ctx, fake_server):
    fake_server.model_echo = "gpt-4o"
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 2
    metrics = _echo_metrics(result)
    assert metrics["raw_echoes"] == ["gpt-4o", "gpt-4o"]
    assert metrics["claimed"] == ["gpt-4o"]
    assert metrics["match_flags"] == [True, True]
    assert metrics["contradiction_flags"] == [False, False]

    posts = [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]
    assert len(posts) == 2
    payload = json.loads(posts[0]["body"])
    assert payload["model"] == "gpt-4o"
    assert payload["max_tokens"] == 24
    assert payload["temperature"] == 0
    assert payload["messages"][0]["content"] == (
        "Reply with exactly the model identifier that is running "
        "this conversation. Nothing else. No punctuation."
    )
    assert len(ctx.evidence.refs_for("d4.model_echo")) == 2


async def test_model_echo_alias_match(ctx, fake_server):
    fake_server.model_echo = "gpt-4o-2024-08-06"
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.PASS
    assert _echo_metrics(result)["match_flags"] == [True, True]
    assert _echo_metrics(result)["contradiction_flags"] == [False, False]


async def test_model_echo_claimed_alias_normalization(ctx, fake_server):
    ctx.claimed_models = ["gpt-4o-2024-08-06"]
    fake_server.model_echo = "gpt-4o"
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.PASS
    assert _echo_metrics(result)["claimed"] == ["gpt-4o-2024-08-06", "gpt-4o"]


async def test_model_echo_normalizes_punctuation_and_case(ctx, fake_server):
    fake_server.model_echo = "GPT-4O."
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.PASS
    assert _echo_metrics(result)["match_flags"] == [True, True]


async def test_model_echo_unknown_custom_warns(ctx, fake_server):
    fake_server.model_echo = "some-other-model"
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = _echo_metrics(result)
    assert metrics["match_flags"] == [False, False]
    assert metrics["contradiction_flags"] == [False, False]
    assert any("matches no claimed model" in note for note in result.notes)


async def test_model_echo_contradictory_warns_not_fails(ctx, fake_server):
    fake_server.model_echo = "claude-3-5-sonnet"
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = _echo_metrics(result)
    assert metrics["match_flags"] == [False, False]
    assert metrics["contradiction_flags"] == [True, True]
    assert any("known family outside the claim" in note for note in result.notes)


@pytest.mark.parametrize(
    "claimed,echo",
    [
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20241022"),
        ("gemini-2.0-flash", "gemini-2.0-flash"),
        ("deepseek-r1", "deepseek-r1"),
        ("claude-3-5-sonnet", "claude-3-5-sonnet-20241022"),
    ],
)
async def test_model_echo_matching_non_gpt_family_passes(ctx, fake_server, claimed, echo):
    """A matching claude/gemini/deepseek family echo must never self-contradict
    through its own family token (docs/06 §4.3)."""
    ctx.claimed_models = [claimed]
    fake_server.model_echo = echo
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert _echo_metrics(result)["match_flags"] == [True, True]
    assert _echo_metrics(result)["contradiction_flags"] == [False, False]


def test_echo_contradicts_family_tokens_unit():
    claimed = ["claude-3-5-sonnet-20241022"]
    assert _echo_contradicts("claude-3-5-sonnet-20241022", claimed) is False
    assert _echo_contradicts("claude-3-5-sonnet", claimed) is False
    assert _echo_contradicts("gemini-2.0-flash", claimed) is True
    assert _echo_contradicts("deepseek-r1", ["deepseek-r1"]) is False
    assert _echo_contradicts("qwen2.5", ["gpt-4o"]) is True


def test_provider_mapping_is_conservative():
    # Explicit id-family tokens map to official providers (docs/06 §4.2).
    assert provider_of_family("chatcmpl-") == "openai"
    assert provider_of_family("resp_") == "openai"
    assert provider_of_family("msg_") == "anthropic"
    assert provider_of_family("anthropic") == "anthropic"
    assert provider_of_family("gen-") == "gemini"
    assert provider_of_family("gemini") == "gemini"
    # Custom/unknown prefixes stay unclassified — never inferred.
    assert provider_of_family("acme") is None
    assert provider_of_family("") is None
    assert provider_of_family("chatcmpl") is None  # bare token, not an explicit mapping
    # Generic prose is not a family signal ("openai" is deliberately absent).
    assert provider_family_hits("served by openai-style infrastructure") == []
    assert provider_family_hits("powered by azure") == []
    assert provider_family_hits("the anthropic api (msg_)") == ["anthropic"]
    assert provider_family_hits("gemini 2.0 flash") == ["gemini"]
    # Claimed model contract maps through the family rules.
    assert claimed_family_of(["gpt-4o"]) == "chatcmpl-"
    assert claimed_family_of(["claude-3-5-sonnet-20241022"]) == "msg_"
    assert claimed_family_of(["gemini-2.0-flash"]) == "gen-"
    assert claimed_family_of(["mystery-model"]) is None


async def test_model_echo_empty_warns(ctx, fake_server):
    fake_server.model_echo = ""
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.WARN
    assert any("empty model echo" in note for note in result.notes)
    assert _echo_metrics(result)["match_flags"] == [False, False]


async def test_model_echo_generic_reply_warns(ctx, fake_server):
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert _echo_metrics(result)["raw_echoes"] == [
        "This is a fake completion reply.",
        "This is a fake completion reply.",
    ]


async def test_model_echo_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.model_echo")) == 4  # 2 calls x one retry


async def test_model_echo_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_model_echo_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await _run_model_echo(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.model_echo")) == 2


async def test_model_echo_non_200_status_fails(ctx):
    ctx.api_key = "sk-wrong"
    result = await _run_model_echo(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("non-retryable bad response (status 401)" in note for note in result.notes)


async def test_model_echo_evidence_and_registry(ctx, manifest, fake_server):
    fake_server.model_echo = "gpt-4o"
    result = await _run_model_echo(ctx)
    assert len(ctx.evidence.refs_for("d4.model_echo")) == 2
    dumped = result.model_dump()
    assert dumped["metrics"]["model_echo"]["match_flags"] == [True, True]
    assert dumped["metrics"]["model_echo"]["contradiction_flags"] == [False, False]

    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.model_echo"]
    assert isinstance(probe, ModelEchoProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 0.5
    assert probe.samples == 2
    assert probe.skip_reason(SurfaceMap()) is None


# ---------- d4.self_report (§4.4) ----------


def _self_report_baseline(
    *,
    claimed_present: bool = True,
    models_catalog: int = 3,
    terms: list[str] | None = None,
) -> BaselineRecord:
    return BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        provider_label="OpenAI",
        vendor="openai",
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=BaselineSurface(models_catalog=models_catalog, claimed_present=claimed_present),
        fingerprints={"self_report": {"terms": terms if terms is not None else ["openai", "gpt-4o"]}},
    )


async def test_self_report_passes_with_matching_baseline(ctx, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    ctx.selected_baseline = _self_report_baseline()
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 2
    metrics = result.metrics["self_report"]
    assert metrics["texts"] == [
        "this endpoint is served by openai.",
        "this endpoint is served by openai.",
    ]
    assert metrics["stable"] is True
    assert metrics["report_match"] is True
    assert metrics["match_flags"] == [True, True]
    assert metrics["contradiction_flags"] == [False, False]
    assert metrics["contradiction_both"] is False
    assert metrics["surface_drift"]["mild"] is False
    assert metrics["surface_drift"]["hard"] is False

    posts = [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]
    assert len(posts) == 2
    payload = json.loads(posts[0]["body"])
    assert payload["model"] == "gpt-4o"
    assert payload["max_tokens"] == 48
    assert payload["temperature"] == 0
    assert payload["messages"][0]["content"] == (
        "Describe in one sentence which hosting platform or provider API is serving this request."
    )
    assert len(ctx.evidence.refs_for("d4.self_report")) == 2


async def test_self_report_no_baseline_stable_warns(ctx, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["self_report"]
    assert metrics["stable"] is True
    assert metrics["report_match"] is None
    assert metrics["match_flags"] == [None, None]
    assert metrics["contradiction_flags"] == [None, None]
    assert metrics["contradiction_both"] is False
    drift = metrics["surface_drift"]
    assert drift["baseline_present"] is False
    assert drift["mild"] is False and drift["hard"] is False
    assert any("no baseline" in note for note in result.notes)


async def test_self_report_text_mismatch_warns_with_baseline(ctx, fake_server):
    fake_server.self_report_text = "other platform"
    ctx.selected_baseline = _self_report_baseline()
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["self_report"]
    assert metrics["report_match"] is False
    assert metrics["match_flags"] == [False, False]
    assert metrics["contradiction_flags"] == [True, True]
    assert metrics["contradiction_both"] is True
    assert metrics["surface_drift"]["hard"] is False
    assert any("matches no baseline claim terms" in note for note in result.notes)


async def test_self_report_hard_drift_and_contradiction_fails(ctx, fake_server):
    fake_server.self_report_text = "other platform"
    ctx.selected_baseline = _self_report_baseline(claimed_present=True)
    ctx.surface = SurfaceMap(
        models=[m for m in fake_server.models if m != "gpt-4o"], claimed_present=False
    )
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["self_report"]
    assert metrics["contradiction_both"] is True
    drift = metrics["surface_drift"]
    assert drift["hard"] is True
    assert drift["claimed_present_baseline"] is True
    assert drift["claimed_present_now"] is False
    assert any("FAIL" in note for note in result.notes)


async def test_self_report_hard_drift_without_contradiction_warns(ctx, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    ctx.selected_baseline = _self_report_baseline(claimed_present=True)
    ctx.surface = SurfaceMap(
        models=[m for m in fake_server.models if m != "gpt-4o"], claimed_present=False
    )
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["self_report"]
    assert metrics["report_match"] is True
    assert metrics["contradiction_both"] is False
    assert metrics["surface_drift"]["hard"] is True
    assert any("hard surface drift" in note for note in result.notes)


async def test_self_report_empty_baseline_terms_unknown_not_contradiction(ctx, fake_server):
    # Missing/empty baseline terms are unknown, never a contradiction: the
    # flags stay None and contradiction_both stays False, so even combined
    # with a hard surface drift the probe must WARN — never FAIL, and the
    # empty terms can never feed a hidden_origin veto.
    fake_server.self_report_text = "other platform"
    ctx.selected_baseline = _self_report_baseline(terms=[])
    ctx.surface = SurfaceMap(
        models=[m for m in fake_server.models if m != "gpt-4o"], claimed_present=False
    )
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["self_report"]
    assert metrics["report_match"] is None
    assert metrics["match_flags"] == [None, None]
    assert metrics["contradiction_flags"] == [None, None]
    assert metrics["contradiction_both"] is False
    assert metrics["surface_drift"]["hard"] is True
    assert any("no self-report terms" in note for note in result.notes)
    assert not any("FAIL" in note for note in result.notes)


async def test_self_report_mild_catalog_drift_warns(ctx, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    ctx.selected_baseline = _self_report_baseline(models_catalog=3, claimed_present=True)
    ctx.surface = SurfaceMap(
        models=list(fake_server.models) + ["extra-model-1", "extra-model-2"],
        claimed_present=True,
    )
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["self_report"]
    assert metrics["report_match"] is True
    drift = metrics["surface_drift"]
    assert drift["mild"] is True
    assert drift["hard"] is False
    assert drift["count_delta_pct"] > 30.0
    assert any("mild drift" in note for note in result.notes)


async def test_self_report_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.self_report")) == 4  # 2 calls x one retry


async def test_self_report_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_self_report_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await SelfReportProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.self_report")) == 2


async def test_self_report_reuses_surface_without_models_call(ctx, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    ctx.selected_baseline = _self_report_baseline()
    result = await SelfReportProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    gets = [e for e in fake_server.requests_log if e["method"] == "GET"]
    assert gets == []
    posts = [e for e in fake_server.requests_log if e["method"] == "POST"]
    assert len(posts) == 2


async def test_self_report_family_hits_metrics(ctx, fake_server):
    fake_server.self_report_text = "Served by the Anthropic API (msg_ style)."
    ctx.selected_baseline = _self_report_baseline()
    result = await SelfReportProbe().run(ctx)
    metrics = result.metrics["self_report"]
    assert metrics["family_hits"] == [["anthropic"], ["anthropic"]]
    assert metrics["family_hits_union"] == ["anthropic"]


async def test_self_report_generic_prose_no_family_hits(ctx, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI-style infrastructure."
    ctx.selected_baseline = _self_report_baseline()
    result = await SelfReportProbe().run(ctx)
    metrics = result.metrics["self_report"]
    assert metrics["family_hits"] == [[], []]
    assert metrics["family_hits_union"] == []


async def test_self_report_evidence_and_registry(ctx, manifest, fake_server):
    fake_server.self_report_text = "This endpoint is served by OpenAI."
    ctx.selected_baseline = _self_report_baseline()
    result = await SelfReportProbe().run(ctx)
    assert len(ctx.evidence.refs_for("d4.self_report")) == 2
    dumped = result.model_dump()
    assert dumped["metrics"]["self_report"]["report_match"] is True
    assert dumped["metrics"]["self_report"]["surface_drift"]["hard"] is False

    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.self_report"]
    assert isinstance(probe, SelfReportProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 1.0
    assert probe.samples == 2
    assert probe.skip_reason(SurfaceMap()) is None
    assert CUSTOM_RUNNERS["d4.self_report"] is SelfReportProbe


# ---------- d4.canary_echo (§4.5) ----------

_CANARY_RE = re.compile(r"^VERITAS-[0-9a-f]{16}$")


def _chat_posts(fake_server):
    return [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]


def test_canary_echo_randomness_shape():
    canaries = [_fresh_canary() for _ in range(64)]
    assert len(set(canaries)) == len(canaries), "canaries must be unique draws"
    for canary in canaries:
        assert _CANARY_RE.match(canary) is not None
        assert len(canary) == len("VERITAS-") + 16
        assert canary.count("-") == 1


async def test_canary_echo_passes_on_exact_echo(ctx, fake_server):
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 2
    metrics = result.metrics["canary_echo"]
    canaries = metrics["canaries"]
    assert len(canaries) == 2 and canaries[0] != canaries[1]
    assert all(_CANARY_RE.match(c) for c in canaries)
    assert metrics["outputs"] == [canaries[0], canaries[1], canaries[0], canaries[1]]
    assert metrics["echo_ok"] == [True, True, True, True]
    assert metrics["echo_exact"] == [True, True, True, True]
    assert metrics["contamination"] is False
    assert metrics["contamination_pairs"] == [False, False]
    assert metrics["template"] is False
    assert metrics["asymmetry"] is False
    assert len(ctx.evidence.refs_for("d4.canary_echo")) == 4

    posts = _chat_posts(fake_server)
    assert len(posts) == 4
    for call_no, pair_no in enumerate([0, 1, 0, 1]):
        payload = json.loads(posts[call_no]["body"])
        assert payload["model"] == "gpt-4o"
        assert payload["max_tokens"] == 24
        assert payload["temperature"] == 0
        assert payload["messages"][0]["content"] == (
            "Reply with exactly this token and nothing else: " + canaries[pair_no]
        )


async def test_canary_echo_tolerates_whitespace_drift(ctx, fake_server):
    fake_server.canary_relaxed_pairs = 2
    fake_server.canary_echo_suffix = " "
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.metrics["canary_echo"]["echo_exact"] == [True, True, True, True]


async def test_canary_echo_template_mode_fails(ctx, fake_server):
    fake_server.template_mode = True
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["canary_echo"]
    assert metrics["template"] is True
    assert metrics["echo_exact"] == [False, False, False, False]
    assert metrics["contamination"] is False
    assert any("template" in note for note in result.notes)


async def test_canary_echo_cross_contamination_fails(ctx, fake_server):
    fake_server.contaminate_cross_request = True
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["canary_echo"]
    assert metrics["contamination"] is True
    assert metrics["contamination_pairs"] == [True, True]
    assert metrics["template"] is False
    assert metrics["asymmetry"] is False
    assert any("contamination" in note for note in result.notes)


async def test_canary_echo_single_pair_contamination_warns(ctx, fake_server):
    fake_server.contaminate_single_pair = True
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["canary_echo"]
    assert metrics["contamination"] is True
    assert metrics["contamination_pairs"] == [False, True]
    assert metrics["template"] is False
    assert any("single pair" in note for note in result.notes)


async def test_canary_echo_relaxed_echo_warns(ctx, fake_server):
    fake_server.canary_relaxed_pairs = 1
    fake_server.canary_echo_prefix = "token: "
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["canary_echo"]
    assert metrics["echo_ok"] == [True, True, True, True]
    assert metrics["echo_exact"] == [False, True, False, True]
    assert metrics["contamination"] is False
    assert metrics["template"] is False
    assert metrics["asymmetry"] is False
    assert any("relaxed echo" in note for note in result.notes)


async def test_canary_echo_asymmetry_fails_when_p0_passed(ctx, fake_server):
    ctx.p0_verdicts["p0.echo"] = "pass"
    fake_server.canary_relaxed_pairs = 2
    fake_server.canary_echo_prefix = "token: "
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["canary_echo"]
    assert metrics["echo_ok"] == [True, True, True, True]
    assert metrics["echo_exact"] == [False, False, False, False]
    assert metrics["template"] is False
    assert metrics["contamination"] is False
    assert metrics["asymmetry"] is True
    assert metrics["p0_echo"] == "pass"
    assert any("asymmetry" in note for note in result.notes)


async def test_canary_echo_all_relaxed_without_p0_verdict_warns(ctx, fake_server):
    fake_server.canary_relaxed_pairs = 2
    fake_server.canary_echo_prefix = "token: "
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.metrics["canary_echo"]["asymmetry"] is False


async def test_canary_echo_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.canary_echo")) == 8  # 4 calls x one retry


async def test_canary_echo_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_canary_echo_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await CanaryEchoProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.canary_echo")) == 4


async def test_canary_echo_non_200_status_fails(ctx):
    ctx.api_key = "sk-wrong"
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("non-retryable bad response (status 401)" in note for note in result.notes)


async def test_canary_echo_evidence_and_registry(ctx, manifest, fake_server):
    result = await CanaryEchoProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(ctx.evidence.refs_for("d4.canary_echo")) == 4
    dumped = result.model_dump()
    metrics = dumped["metrics"]["canary_echo"]
    assert metrics["contamination"] is False
    assert metrics["template"] is False
    assert metrics["asymmetry"] is False
    assert len(metrics["outputs"]) == 4

    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.canary_echo"]
    assert isinstance(probe, CanaryEchoProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 1.0
    assert probe.samples == 2
    assert probe.skip_reason(SurfaceMap()) is None
    assert CUSTOM_RUNNERS["d4.canary_echo"] is CanaryEchoProbe


# ---------- d4.sse_timing (§4.6) ----------

_CHUNK = {"id": "chatcmpl-fake123", "object": "chat.completion.chunk"}
_FINISH_CHUNK = {**_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
_USAGE_CHUNK = {
    "choices": [],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
_DONE_LINE = b"data: [DONE]\n\n"


def _sse_data(chunk: dict) -> bytes:
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _content_chunk(text: str) -> dict:
    return {**_CHUNK, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def _sse_body(content: list[bytes]) -> bytes:
    return b"".join(
        [*content, _sse_data(_FINISH_CHUNK), _sse_data(_USAGE_CHUNK), _DONE_LINE]
    )


class _EagerStream(httpx.AsyncByteStream):
    """Ordinary eager response: whole body at once; n_chunks = ``words`` (> 2)."""

    def __init__(self, words: int = 5) -> None:
        self.body = _sse_body([_sse_data(_content_chunk(f"w{i} ")) for i in range(words)])

    async def __aiter__(self):
        yield self.body


class _BufferedStream(httpx.AsyncByteStream):
    """Classic relay buffering: 2 content chunks in a burst after a long stall."""

    def __init__(self, delay_s: float = 1.0) -> None:
        self.delay_s = delay_s
        self.body = _sse_body([_sse_data(_content_chunk("one ")), _sse_data(_content_chunk("two "))])

    async def __aiter__(self):
        await asyncio.sleep(self.delay_s)
        yield self.body


class _PacedStream(httpx.AsyncByteStream):
    """Honest slow streaming: many chunks (never buffered) with stable cadence."""

    def __init__(self, words: int = 5, gap_s: float = 0.05) -> None:
        self.chunks = [_sse_data(_content_chunk(f"w{i} ")) for i in range(words)]
        self.gap_s = gap_s

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.gap_s)
            yield chunk
        await asyncio.sleep(self.gap_s)
        yield _sse_data(_FINISH_CHUNK)
        yield _DONE_LINE


class _MidstreamFailStream(httpx.AsyncByteStream):
    def __init__(self, reads: dict[str, int]) -> None:
        self.reads = reads

    async def __aiter__(self):
        self.reads["n"] += 1
        yield _sse_data(_content_chunk("hi "))
        raise httpx.ReadError("mid-stream disconnect")


class _ModeTransport(httpx.AsyncBaseTransport):
    """One deterministic stream mode per call: buffered/eager/paced/empty/midstream."""

    def __init__(self, modes: list[str]) -> None:
        self.modes = modes
        self.calls = 0
        self.reads = {"n": 0}

    def _stream_for(self, mode: str) -> httpx.AsyncByteStream:
        if mode == "buffered":
            return _BufferedStream()
        if mode == "eager":
            return _EagerStream()
        if mode == "paced":
            return _PacedStream()
        if mode == "empty":
            return _EagerStream(words=0)
        if mode == "midstream":
            return _MidstreamFailStream(self.reads)
        raise AssertionError(f"unknown mode {mode!r}")

    async def handle_async_request(self, request):
        mode = self.modes[min(self.calls, len(self.modes) - 1)]
        self.calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=self._stream_for(mode),
            request=request,
        )


def _with_transport(ctx, transport: httpx.AsyncBaseTransport) -> tuple[RunContext, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=transport, timeout=10)
    ctx.client = client
    return ctx, client


def _sse_baseline(*, ttft_p90: float | None = None, inter_p90: float | None = None) -> BaselineRecord:
    fingerprints: dict = {}
    if ttft_p90 is not None:
        fingerprints["sse_ttft_ms"] = {"median": ttft_p90 * 0.9, "p90": ttft_p90, "n": 3}
    if inter_p90 is not None:
        fingerprints["sse_inter_chunk_ms"] = {"median": inter_p90 * 0.9, "p90": inter_p90, "n": 3}
    return BaselineRecord(baseline_id="BL-OPENAI-GPT-4O-0001", fingerprints=fingerprints)


def test_sse_timing_buffered_classifier_is_conservative():
    assert _is_buffered(1.0, 2.0, [0.1, 0.1], 5) is False  # many chunks
    assert _is_buffered(1.0, 400.0, [1.0], 2) is False  # window too short
    assert _is_buffered(50.0, 1100.0, [1000.0], 2) is False  # gap spread, not a burst
    assert _is_buffered(1000.0, 1002.0, [0.5], 2) is True  # burst at end of long stall
    assert _is_buffered(1000.0, 1001.0, [], 1) is True  # single late chunk
    assert _is_buffered(1000.0, 1001.0, [], 0) is False  # no content


async def test_sse_timing_passes_on_paced_stream_without_baseline(ctx, fake_server):
    result = await SseTimingProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 3
    metrics = result.metrics["sse_timing"]
    assert metrics["buffered"] == [False, False, False]
    assert metrics["buffered_count"] == 0
    assert len(metrics["ttft_ms"]) == 3
    assert len(metrics["total_ms"]) == 3
    assert metrics["ttft_median_ms"] is not None
    assert metrics["inter_chunk_p90_ms"] is not None
    assert metrics["baseline"]["present"] is False
    assert metrics["baseline"]["ttft_ratio"] is None
    assert metrics["baseline"]["inter_chunk_ratio"] is None
    per_stream = metrics["per_stream"]
    assert len(per_stream) == 3
    assert all(entry["status"] == 200 for entry in per_stream)
    assert all(entry["n_chunks"] >= 2 for entry in per_stream)
    assert all(entry["text"] for entry in per_stream)
    assert all(entry["est_tokens"] > 0 for entry in per_stream)
    assert len(ctx.evidence.refs_for("d4.sse_timing")) == 3

    posts = [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]
    assert len(posts) == 3
    payload = json.loads(posts[0]["body"])
    assert payload["model"] == "gpt-4o"
    assert payload["messages"][0]["content"] == "Count from 1 to 50."
    assert payload["max_tokens"] == 64
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["temperature"] == 0


async def test_sse_timing_buffered_once_warns(ctx):
    ctx, client = _with_transport(ctx, _ModeTransport(["buffered", "eager", "eager"]))
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["sse_timing"]
    assert metrics["buffered"] == [True, False, False]
    assert metrics["buffered_count"] == 1
    assert any("exactly one buffered stream" in note for note in result.notes)


async def test_sse_timing_buffered_twice_fails(ctx):
    ctx, client = _with_transport(ctx, _ModeTransport(["buffered", "buffered", "eager"]))
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["sse_timing"]
    assert metrics["buffered"] == [True, True, False]
    assert metrics["buffered_count"] == 2
    assert any("relay buffering" in note for note in result.notes)


async def test_sse_timing_baseline_pass_band(ctx):
    ctx.selected_baseline = _sse_baseline(ttft_p90=5000.0, inter_p90=5000.0)
    ctx, client = _with_transport(ctx, _ModeTransport(["paced", "paced", "paced"]))
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["sse_timing"]
    assert metrics["buffered"] == [False, False, False]
    assert metrics["baseline"]["present"] is True
    assert metrics["baseline"]["sse_ttft_p90_ms"] == 5000.0
    assert metrics["baseline"]["ttft_ratio"] < 2.0
    assert metrics["baseline"]["inter_chunk_ratio"] < 2.0
    assert any("<= 2x baseline p90" in note for note in result.notes)


async def test_sse_timing_baseline_warn_band(ctx):
    ctx.selected_baseline = _sse_baseline(ttft_p90=20.0, inter_p90=20.0)
    ctx, client = _with_transport(ctx, _ModeTransport(["paced", "paced", "paced"]))
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["sse_timing"]
    assert metrics["buffered"] == [False, False, False]
    assert metrics["baseline"]["ttft_ratio"] > 2.0
    assert metrics["baseline"]["inter_chunk_ratio"] > 2.0
    assert any("WARN band" in note for note in result.notes)


async def test_sse_timing_baseline_over_five_x_still_warns(ctx):
    ctx.selected_baseline = _sse_baseline(ttft_p90=5.0, inter_p90=5.0)
    ctx, client = _with_transport(ctx, _ModeTransport(["paced", "paced", "paced"]))
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["sse_timing"]
    assert metrics["baseline"]["ttft_ratio"] > 5.0
    assert metrics["baseline"]["inter_chunk_ratio"] > 5.0
    assert any("documented bound" in note for note in result.notes)


async def test_sse_timing_baseline_without_fingerprint_passes(ctx):
    ctx.selected_baseline = BaselineRecord(baseline_id="BL-OPENAI-GPT-4O-0001")
    result = await SseTimingProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["sse_timing"]
    assert metrics["baseline"]["present"] is True
    assert metrics["baseline"]["sse_ttft_p90_ms"] is None
    assert metrics["baseline"]["ttft_ratio"] is None
    assert any("no usable sse_timing p90" in note for note in result.notes)


async def test_sse_timing_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await SseTimingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.sse_timing")) == 3  # persistent-attempt doc per stream


async def test_sse_timing_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await SseTimingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.sse_timing")) == 3


async def test_sse_timing_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.sse_timing")) == 3


async def test_sse_timing_midstream_error_fails_no_retry(ctx):
    transport = _ModeTransport(["midstream", "midstream", "midstream"])
    ctx, client = _with_transport(ctx, transport)
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert transport.reads["n"] == 3  # one read per stream, no retry after bytes
    assert any("transport error" in note for note in result.notes)
    refs = ctx.evidence.refs_for("d4.sse_timing")
    assert len(refs) == 3
    doc = json.loads((ctx.evidence.dir.parent / refs[0]).read_text(encoding="utf-8"))
    assert doc["response"]["status"] == 200
    assert "hi" in doc["response"]["body"]


async def test_sse_timing_empty_stream_fails(ctx):
    ctx, client = _with_transport(ctx, _ModeTransport(["empty", "empty", "empty"]))
    try:
        result = await SseTimingProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("no content events" in note for note in result.notes)


async def test_sse_timing_non_200_status_fails(ctx):
    ctx.api_key = "sk-wrong"
    result = await SseTimingProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("non-retryable bad response (status 401)" in note for note in result.notes)


async def test_sse_timing_metrics_evidence_and_registry(ctx, fake_server, manifest):
    result = await SseTimingProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(ctx.evidence.refs_for("d4.sse_timing")) == 3
    dumped = result.model_dump()
    sse = dumped["metrics"]["sse_timing"]
    assert len(sse["ttft_ms"]) == 3
    assert len(sse["inter_chunk_ms"]) >= 3  # pooled across streams
    assert len(sse["total_ms"]) == 3
    assert sse["buffered"] == [False, False, False]
    assert sse["buffered_count"] == 0
    assert sse["ttft_median_ms"] is not None
    assert sse["ttft_p90_ms"] is not None
    assert sse["inter_chunk_median_ms"] is not None
    assert sse["inter_chunk_p90_ms"] is not None
    assert sse["e2e_median_ms"] is not None
    assert {sample["kind"] for sample in dumped["samples"]} <= {"ttft", "itl", "e2e"}

    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.sse_timing"]
    assert isinstance(probe, SseTimingProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 1.0
    assert probe.samples == 3
    assert probe.skip_reason(SurfaceMap()) is None
    assert CUSTOM_RUNNERS["d4.sse_timing"] is SseTimingProbe


# ---------- d4.rotation (§4.7) ----------


def _rotation_metrics(result: ProbeResult) -> dict:
    return result.metrics["rotation"]


async def test_rotation_passes_on_single_family(ctx, fake_server, no_sleep):
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 20
    metrics = _rotation_metrics(result)
    assert metrics["F"] == 1
    assert metrics["F_expected"] is None
    assert len(metrics["features"]) == 20
    assert len(metrics["assignments"]) == 20
    assert all(a == 0 for a in metrics["assignments"])
    assert len(metrics["families"]) == 1
    for feature in metrics["features"]:
        assert feature["id_family"] == "chatcmpl-"
        assert feature["content_bucket"] is not None
        assert feature["usage_ratio"] is not None
        assert feature["served_by"] is None
        assert isinstance(feature["shape"], str)
    assert any("single response family" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.rotation")) == 20

    posts = _chat_posts(fake_server)
    assert len(posts) == 20
    payload = json.loads(posts[0]["body"])
    assert payload["model"] == "gpt-4o"
    assert payload["max_tokens"] == 32
    assert payload["temperature"] == 0
    assert payload["messages"][0]["content"] == "Write the number 42 in words and stop."
    assert all(json.loads(p["body"])["messages"] == payload["messages"] for p in posts)


async def test_rotation_two_families_warns(ctx, fake_server, no_sleep):
    fake_server.rotation_families = 2
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = _rotation_metrics(result)
    assert metrics["F"] == 2
    assert len(metrics["families"]) == 2
    assert set(metrics["assignments"]) == {0, 1}
    id_families = {f["id_family"] for f in metrics["features"]}
    assert id_families == {"chatcmpl-", "msg_"}
    assert any("evidence toward suspected substitution" in note for note in result.notes)


async def test_rotation_three_families_fails(ctx, fake_server, no_sleep):
    fake_server.rotation_families = 3
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = _rotation_metrics(result)
    assert metrics["F"] == 3
    assert set(metrics["assignments"]) == {0, 1, 2}
    assert {f["id_family"] for f in metrics["features"]} == {"chatcmpl-", "msg_", "gen-"}
    assert any("multiple upstream backends" in note for note in result.notes)


def test_rotation_content_bucket_formatting_tolerance():
    assert content_bucket("forty two") == content_bucket("Forty two.")
    assert content_bucket("forty two") == content_bucket("  FORTY  two! ")
    assert content_bucket("forty two") == content_bucket("forty two")
    assert content_bucket("the answer is forty two") == content_bucket("The answer is forty two,")
    assert content_bucket("forty two") != content_bucket("forty three")
    assert content_bucket("forty two") != content_bucket("the answer is forty two")
    assert content_bucket(None) is None
    assert content_bucket("") is None
    assert normalize_content("  Forty   TWO. ") == "forty two"


async def test_rotation_formatting_variants_are_one_family(ctx, fake_server, no_sleep):
    fake_server.rotation_families = 3
    fake_server.rotation_formatting_variants = True
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = _rotation_metrics(result)
    assert metrics["F"] == 1
    assert all(a == 0 for a in metrics["assignments"])
    assert {f["content_bucket"] for f in metrics["features"]} == {content_bucket("forty two")}
    assert {f["id_family"] for f in metrics["features"]} == {"chatcmpl-"}


def test_rotation_clustering_tolerant_union():
    same_bucket = {"id_family": "chatcmpl-", "content_bucket": "a", "usage_ratio": 8.0}
    same_ratio_diff_bucket = {"id_family": "chatcmpl-", "content_bucket": "b", "usage_ratio": 8.0}
    other_ratio_diff_bucket = {"id_family": "chatcmpl-", "content_bucket": "b", "usage_ratio": 3.2}
    other_id = {"id_family": "msg_", "content_bucket": "a", "usage_ratio": 8.0}
    no_id = {"id_family": None, "content_bucket": "a", "usage_ratio": 8.0}

    assignments, reps = cluster_families(
        [same_bucket, same_ratio_diff_bucket, other_ratio_diff_bucket, other_id, no_id]
    )
    assert assignments == [0, 0, 1, 2, 3]
    assert len(reps) == 4


async def test_rotation_variant_contents_still_one_family(ctx, fake_server, no_sleep):
    fake_server.rotation_families = 2
    fake_server.rotation_formatting_variants = True
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.metrics["rotation"]["F"] == 1


async def test_rotation_optional_baseline_f_expected(ctx, fake_server, no_sleep):
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"rotation_families": 2},
    )
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert _rotation_metrics(result)["F_expected"] == 2
    assert _rotation_metrics(result)["F"] == 1
    dumped = result.model_dump()
    assert dumped["metrics"]["rotation"]["F_expected"] == 2


async def test_rotation_f_expected_absent_without_baseline(ctx, fake_server, no_sleep):
    result = await RotationProbe().run(ctx)
    assert _rotation_metrics(result)["F_expected"] is None


async def test_rotation_exact_request_count_and_spacing(ctx, fake_server, monkeypatch):
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(sleeps) == 19  # 20 calls, spaced between them
    assert all(delay == RotationProbe.spacing_s for delay in sleeps)
    assert len(_chat_posts(fake_server)) == 20


async def test_rotation_spacing_configurable(ctx, fake_server, monkeypatch):
    sleeps: list[float] = []

    async def _record(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record)
    monkeypatch.setattr(RotationProbe, "spacing_s", 0.25)
    await RotationProbe().run(ctx)
    assert all(delay == 0.25 for delay in sleeps)


async def test_rotation_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.rotation")) == 40  # 20 calls x one retry


async def test_rotation_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("server error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.rotation")) == 40


async def test_rotation_mixed_429_with_successes_warns(ctx, fake_server, no_sleep, monkeypatch):
    """A persistent 429 on any call caps the verdict at WARN even when F == 1 (§1.3, §4.7)."""

    real_request = d4_fingerprint_module.request_with_retry
    calls = {"n": 0}

    async def _flaky_first(ctx_, probe_id, method, path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError(
                429, "d4.rotation: rate-limited (429) after retry — Warn per §10, rerun with backoff"
            )
        return await real_request(ctx_, probe_id, method, path, **kwargs)

    monkeypatch.setattr(d4_fingerprint_module, "request_with_retry", _flaky_first)
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.successes == 19
    assert _rotation_metrics(result)["F"] == 1
    assert any("rate-limited" in note for note in result.notes)


async def test_rotation_transport_error_fails(ctx, no_sleep):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await RotationProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.rotation")) == 20


async def test_rotation_non_200_status_fails(ctx, no_sleep):
    ctx.api_key = "sk-wrong"
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("non-retryable bad response (status 401)" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.rotation")) == 20


async def test_rotation_malformed_body_fails(ctx, fake_server, no_sleep):
    fake_server.rotation_bad_body = True
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("not valid JSON" in note for note in result.notes)
    assert _rotation_metrics(result)["F"] == 0


async def test_rotation_served_by_header_extracted(ctx, fake_server, no_sleep):
    fake_server.hop_headers = ["x-served-by"]
    result = await RotationProbe().run(ctx)
    metrics = _rotation_metrics(result)
    assert all(f["served_by"] == "fake-x-served-by" for f in metrics["features"])
    assert metrics["F"] == 1


async def test_rotation_provider_mapping_metrics(ctx, fake_server, no_sleep):
    fake_server.rotation_families = 3
    result = await RotationProbe().run(ctx)
    metrics = _rotation_metrics(result)
    assert metrics["F"] == 3
    assert metrics["providers"] == ["anthropic", "gemini", "openai"]
    assert metrics["distinct_official_providers"] == 3


def test_rotation_custom_families_unclassified(ctx):
    features = [
        {"id_family": "acme.", "content_bucket": "a", "usage_ratio": 1.0},
        {"id_family": "acme.", "content_bucket": "b", "usage_ratio": 2.0},
        {"id_family": "acme.", "content_bucket": "c", "usage_ratio": 3.0},
    ]
    metrics = d4_fingerprint_module._rotation_metrics(ctx, features)
    assert metrics["F"] == 3
    assert metrics["providers"] == []
    assert metrics["distinct_official_providers"] == 0


async def test_rotation_evidence_and_registry(ctx, fake_server, manifest, no_sleep):
    result = await RotationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(ctx.evidence.refs_for("d4.rotation")) == 20
    dumped = result.model_dump()
    rotation = dumped["metrics"]["rotation"]
    assert rotation["F"] == 1
    assert len(rotation["features"]) == 20
    assert len(rotation["assignments"]) == 20

    by_id = {p.id: p for p in load_probes(manifest)}
    probe = by_id["d4.rotation"]
    assert isinstance(probe, RotationProbe)
    assert probe.domain == Domain.D4
    assert probe.weight == 1.5
    assert probe.samples == 20
    assert probe.skip_reason(SurfaceMap()) is None
    assert CUSTOM_RUNNERS["d4.rotation"] is RotationProbe
