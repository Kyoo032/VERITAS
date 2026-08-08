"""D4 billing forensics probes (docs/06-m2-probe-spec.md §5.1-§5.4).

d4.usage_presence, d4.recount_deviation, d4.wrap_offset,
d4.reasoning_cache_fields: presence/arithmetic, tiktoken recount with
inflation gates, constant hidden-wrapper offset, and cache/reasoning
usage-details fields. Verdict routing follows docs/06 §1.3: persistent
429/5xx WARNs after one retry; transport errors stay FAIL.
"""

from __future__ import annotations

import json

import httpx
import pytest

from supgate.baselines import BaselineRecord
from supgate.models import Domain, SurfaceMap, Verdict
from supgate.probes.base import RateLimitError
from supgate.probes.d4_billing import (
    ReasoningCacheFieldsProbe,
    RecountDeviationProbe,
    UsagePresenceProbe,
    WrapOffsetProbe,
    usage_schema_for,
)
from supgate.registry import CUSTOM_RUNNERS, load_probes
from supgate.tokenizers import count

_SENTENCE = "The quick brown fox jumps over the lazy dog and circles the fence."
_SHORT_TEXT = "Say ping."
# Must byte-match supgate.probes.d4_billing._RECOUNT_PROMPTS (trailing space
# included) so recounted token counts agree exactly.
_MEDIUM_TEXT = (_SENTENCE + " ") * 3
_LONG_TEXT = (_SENTENCE + " ") * 16
_REPLY_TEXT = "This is a fake completion reply."


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)


def _chat_posts(fake_server) -> list[dict]:
    return [
        e for e in fake_server.requests_log
        if e["method"] == "POST" and e["path"] == "/v1/chat/completions"
    ]


def _read_evidence_doc(ctx, probe_id: str, index: int = 0) -> dict:
    ref = ctx.evidence.refs_for(probe_id)[index]
    return json.loads((ctx.evidence.dir.parent / ref).read_text(encoding="utf-8"))


def _assert_evidence_redacted(ctx, probe_id: str) -> None:
    curl = ctx.evidence.curl_for(probe_id)
    assert curl is not None
    assert "$SUPGATE_KEY" in curl
    assert "sk-test" not in curl
    doc = _read_evidence_doc(ctx, probe_id)
    assert "sk-test" not in json.dumps(doc)


def _expected_recount(messages: list[dict], offset_pct: float = 0.0, offset_tokens: int = 0) -> tuple[int, int]:
    """(reported, recounted) prompt tokens the fake server + probe should agree on."""
    recounted = count(json.dumps(messages), "o200k_base")
    reported = recounted + offset_tokens
    if offset_pct:
        reported = round(reported * (1.0 + offset_pct / 100.0))
    return reported, recounted


# --- class contract / registry ----------------------------------------------


def test_billing_probe_class_contract(manifest):
    by_id = {p.id: p for p in load_probes(manifest)}
    specs = {
        "d4.usage_presence": (UsagePresenceProbe, 1.0, 3),
        "d4.recount_deviation": (RecountDeviationProbe, 2.0, 3),
        "d4.wrap_offset": (WrapOffsetProbe, 1.0, 4),
        "d4.reasoning_cache_fields": (ReasoningCacheFieldsProbe, 1.0, 3),
    }
    for probe_id, (cls, weight, samples) in specs.items():
        probe = by_id[probe_id]
        assert isinstance(probe, cls)
        assert probe.id == probe_id
        assert probe.domain == Domain.D4
        assert probe.weight == weight
        assert probe.samples == samples
        assert probe.skip_reason(SurfaceMap()) is None
        assert CUSTOM_RUNNERS[probe_id] is cls


def test_usage_schema_for_known_and_unknown_families():
    assert usage_schema_for("gpt-4o") == {"cached_tokens": True, "reasoning_tokens": False}
    assert usage_schema_for("gpt-4o-2024-08-06") == {"cached_tokens": True, "reasoning_tokens": False}
    assert usage_schema_for("o1") == {"cached_tokens": True, "reasoning_tokens": True}
    assert usage_schema_for("o3-mini") == {"cached_tokens": True, "reasoning_tokens": True}
    assert usage_schema_for("text-davinci-003") == {"cached_tokens": False, "reasoning_tokens": False}
    assert usage_schema_for("mystery-model") is None


# --- d4.usage_presence ------------------------------------------------------


async def test_usage_presence_passes_default(ctx, fake_server):
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 2
    forms = result.metrics["usage_presence"]["forms"]
    assert forms["non_stream"]["usage_present"] is True
    assert forms["non_stream"]["arithmetic_ok"] is True
    assert forms["stream_include_usage"]["usage_present"] is True
    assert forms["stream_include_usage"]["arithmetic_ok"] is True
    assert forms["stream_no_include_usage"]["usage_present"] is False
    assert len(ctx.evidence.refs_for("d4.usage_presence")) == 3
    _assert_evidence_redacted(ctx, "d4.usage_presence")

    posts = _chat_posts(fake_server)
    assert len(posts) == 3
    non_stream = json.loads(posts[0]["body"])
    assert "stream" not in non_stream
    assert non_stream["max_tokens"] == 32
    assert non_stream["messages"] == [{"role": "user", "content": "What is 2+2?"}]
    stream_include = json.loads(posts[1]["body"])
    assert stream_include["stream"] is True
    assert stream_include["stream_options"] == {"include_usage": True}
    stream_plain = json.loads(posts[2]["body"])
    assert stream_plain["stream"] is True
    assert "stream_options" not in stream_plain


async def test_usage_presence_omit_nonstream_fails(ctx, fake_server):
    fake_server.omit_usage_nonstream = True
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert result.metrics["usage_presence"]["forms"]["non_stream"]["usage_present"] is False
    assert any("usage absent on HTTP 200" in note for note in result.notes)


async def test_usage_presence_omit_stream_fails(ctx, fake_server):
    fake_server.omit_usage_stream = True
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    forms = result.metrics["usage_presence"]["forms"]
    assert forms["non_stream"]["usage_present"] is True
    assert forms["stream_include_usage"]["usage_present"] is False
    assert any("stream (include_usage)" in note and "usage absent" in note for note in result.notes)


async def test_usage_presence_bad_arithmetic_warns(ctx, fake_server):
    fake_server.bad_usage_arithmetic = True
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    usage = result.metrics["usage_presence"]["forms"]["non_stream"]["usage"]
    assert usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
    assert result.metrics["usage_presence"]["forms"]["non_stream"]["arithmetic_ok"] is False
    assert any("arithmetically inconsistent" in note for note in result.notes)


async def test_usage_presence_form3_usage_warns(ctx, fake_server):
    fake_server.usage_without_include_usage = True
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.metrics["usage_presence"]["forms"]["stream_no_include_usage"]["usage_present"] is True
    assert any("without include_usage" in note for note in result.notes)


async def test_usage_presence_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    # non-stream: 2 evidence docs (one retry); streams: 1 each (no bytes read).
    assert len(ctx.evidence.refs_for("d4.usage_presence")) == 4


async def test_usage_presence_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await UsagePresenceProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_usage_presence_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await UsagePresenceProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert all("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.usage_presence")) == 3


class _BodyStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):
        yield self.body


def _sse_chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class _MultiUsageTransport(httpx.AsyncBaseTransport):
    """Call 0: JSON completion with clean usage. Call 1: SSE stream whose
    FIRST usage chunk is arithmetically bad and whose LAST usage chunk is
    clean (the authoritative final block). Call 2: plain stream without
    ``include_usage`` — no usage chunk at all."""

    _ROLE = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    _CLEAN = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    _BAD = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 999}

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request):
        call = self.calls
        self.calls += 1
        if call == 0:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-x",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": dict(self._CLEAN),
                },
                request=request,
            )
        if call == 1:
            body = b"".join(
                [
                    _sse_chunk(self._ROLE),
                    _sse_chunk({"choices": [], "usage": dict(self._BAD)}),
                    _sse_chunk({"choices": [], "usage": dict(self._CLEAN)}),
                    b"data: [DONE]\n\n",
                ]
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_BodyStream(body),
                request=request,
            )
        body = b"".join([_sse_chunk(self._ROLE), b"data: [DONE]\n\n"])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_BodyStream(body),
            request=request,
        )


async def test_usage_presence_uses_last_sse_usage_event(ctx):
    """docs/06 §5.1: the authoritative usage block is the LAST SSE event that
    carries one. A stream emitting an early stale/bad usage chunk then the
    final clean block must PASS — the first event would have WARNed."""

    transport = _MultiUsageTransport()
    client = httpx.AsyncClient(transport=transport, timeout=10)
    ctx.client = client
    try:
        result = await UsagePresenceProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    forms = result.metrics["usage_presence"]["forms"]
    stream_usage = forms["stream_include_usage"]["usage"]
    assert stream_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert forms["stream_include_usage"]["arithmetic_ok"] is True
    assert forms["non_stream"]["usage"] == dict(_MultiUsageTransport._CLEAN)
    assert forms["stream_no_include_usage"]["usage_present"] is False


# --- d4.recount_deviation ---------------------------------------------------


async def test_recount_passes_at_zero_offset(ctx, fake_server):
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["recount_deviation"]
    assert metrics["encoding"] == "o200k_base"
    assert metrics["mean_deviation_pct"] == 0.0
    assert metrics["per_size_deviation_pct"] == [0.0, 0.0, 0.0]
    assert metrics["warn_gate_pct"] == 5.0
    assert metrics["fail_gate_pct"] == 15.0
    assert metrics["all_sizes_above_fail_gate"] is False
    assert metrics["excluded_cached_samples"] == []
    assert metrics["baseline"]["present"] is False
    assert [s["size"] for s in metrics["per_sample"]] == ["short", "medium", "long"]
    assert all(s["deviation_pct"] == 0.0 for s in metrics["per_sample"])
    assert all(
        s["reported_prompt_tokens"] == s["recounted_prompt_tokens"]
        and s["reported_completion_tokens"] == s["recounted_completion_tokens"]
        for s in metrics["per_sample"]
    )
    assert len(ctx.evidence.refs_for("d4.recount_deviation")) == 3
    _assert_evidence_redacted(ctx, "d4.recount_deviation")
    posts = _chat_posts(fake_server)
    assert len(posts) == 3
    assert json.loads(posts[0]["body"])["max_tokens"] == 64
    assert json.loads(posts[0]["body"])["temperature"] == 0


async def test_recount_warns_at_8_percent(ctx, fake_server):
    fake_server.usage_offset_pct = 8.0
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["recount_deviation"]
    assert 5.0 < metrics["mean_deviation_pct"] <= 15.0
    # Deterministic per-sample values under the real tokenizer.
    for sample, text in zip(metrics["per_sample"], (_SHORT_TEXT, _MEDIUM_TEXT, _LONG_TEXT), strict=False):
        reported, recounted = _expected_recount([{"role": "user", "content": text}], offset_pct=8.0)
        assert sample["recounted_prompt_tokens"] == recounted
        assert sample["reported_prompt_tokens"] == reported
        assert sample["deviation_pct"] == round((reported - recounted) / recounted * 100.0, 2)
    assert any("WARN band" in note for note in result.notes)


async def test_recount_fails_at_30_percent_all_sizes_confirmed(ctx, fake_server):
    fake_server.usage_offset_pct = 30.0
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["recount_deviation"]
    assert metrics["mean_deviation_pct"] > 15.0
    assert metrics["all_sizes_above_fail_gate"] is True
    assert metrics["excluded_cached_samples"] == []
    assert all(dev > 15.0 for dev in metrics["per_size_deviation_pct"])
    assert any("billing_inflation veto-ready" in note for note in result.notes)


async def test_recount_fails_at_88_percent(ctx, fake_server):
    fake_server.usage_offset_pct = 88.0
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    metrics = result.metrics["recount_deviation"]
    assert metrics["mean_deviation_pct"] > 80.0
    assert metrics["all_sizes_above_fail_gate"] is True
    assert all(dev > 15.0 for dev in metrics["per_size_deviation_pct"])
    for sample, text in zip(metrics["per_sample"], (_SHORT_TEXT, _MEDIUM_TEXT, _LONG_TEXT), strict=False):
        reported, recounted = _expected_recount([{"role": "user", "content": text}], offset_pct=88.0)
        assert sample["reported_prompt_tokens"] == reported
        assert sample["recounted_prompt_tokens"] == recounted


async def test_recount_constant_offset_fails_without_all_size_confirmation(ctx, fake_server):
    # A constant 20-token offset overwhelms the short prompt but stays under
    # the fail gate on the long prompt: FAIL on the mean, but the veto-ready
    # multi-size confirmation must NOT fire.
    fake_server.usage_offset_tokens = 20
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    metrics = result.metrics["recount_deviation"]
    assert metrics["mean_deviation_pct"] > 15.0
    assert metrics["all_sizes_above_fail_gate"] is False
    assert any("NOT confirmed across all prompt sizes" in note for note in result.notes)


async def test_recount_unknown_encoding_skips(ctx, fake_server):
    ctx.model = "mystery-model"
    ctx.claimed_models = ["mystery-model"]
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert result.score == 0.0
    assert result.attempts == 0
    assert any("unknown encoding for model" in note for note in result.notes)
    assert _chat_posts(fake_server) == []
    assert ctx.evidence.refs_for("d4.recount_deviation") == []


async def test_recount_excludes_cached_samples_from_confirmation(ctx, fake_server):
    # Pre-seed the fake's cache counter so the short prompt's first probe call
    # looks like a cached repeat: that sample is excluded from the mean. One
    # excluded size means only 2 of the 3 measurements are non-cached, so the
    # veto confirmation must NOT fire (docs/06 §1.5: all short/medium/long
    # sizes above the fail gate are required, and exclusion removes a size).
    fake_server.usage_offset_pct = 30.0
    fake_server.usage_schema_flags = {"caching_delta": True}
    fake_server._cache_counts[json.dumps([{"role": "user", "content": _SHORT_TEXT}])] = 1
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    metrics = result.metrics["recount_deviation"]
    assert metrics["excluded_cached_samples"] == [0]
    assert metrics["per_sample"][0]["cached_sample"] is True
    assert metrics["per_sample"][0]["cached_tokens"] > 0
    assert metrics["per_size_deviation_pct"] == [
        s["deviation_pct"] for s in metrics["per_sample"][1:]
    ]
    assert metrics["all_sizes_above_fail_gate"] is False
    assert metrics["mean_deviation_pct"] > 15.0
    assert any("NOT confirmed across all prompt sizes" in note for note in result.notes)


async def test_recount_two_cached_one_included_cannot_confirm_veto(ctx, fake_server):
    # Two of the three sizes are cached repeats and excluded: the single
    # remaining (long) measurement is above the fail gate and the mean FAILs,
    # but the veto-ready confirmation flag must stay False — one or two
    # included samples may inform FAIL metrics but never confirm the
    # billing_inflation veto (docs/06 §1.5, §9.1.3).
    fake_server.usage_offset_pct = 30.0
    fake_server.usage_schema_flags = {"caching_delta": True}
    fake_server._cache_counts[json.dumps([{"role": "user", "content": _SHORT_TEXT}])] = 1
    fake_server._cache_counts[json.dumps([{"role": "user", "content": _MEDIUM_TEXT}])] = 1
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    metrics = result.metrics["recount_deviation"]
    assert metrics["excluded_cached_samples"] == [0, 1]
    assert len(metrics["per_size_deviation_pct"]) == 1
    assert metrics["per_size_deviation_pct"] == [metrics["per_sample"][2]["deviation_pct"]]
    assert metrics["all_sizes_above_fail_gate"] is False
    assert metrics["mean_deviation_pct"] > 15.0
    assert any("NOT confirmed across all prompt sizes" in note for note in result.notes)
    # The orchestrator refuses to confirm the veto on this metric set.
    from supgate.orchestrator import _vetoes

    assert _vetoes([result], ["gpt-4o"]) == []


async def test_recount_all_samples_cached_warns_not_fails(ctx, fake_server):
    fake_server.usage_offset_pct = 30.0
    fake_server.usage_schema_flags = {"cached_every_call": True}
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["recount_deviation"]
    assert metrics["excluded_cached_samples"] == [0, 1, 2]
    assert metrics["mean_deviation_pct"] is None
    assert metrics["all_sizes_above_fail_gate"] is False
    assert any("all samples excluded" in note for note in result.notes)


async def test_recount_baseline_calibrated_gates(ctx, fake_server):
    # docs/06 §5.2: baseline recalibrates WARN to mean+4*std and FAIL to
    # max(15, mean+8*std); the static floor keeps the FAIL gate at >= 15.
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT4O-0001",
        fingerprints={"recount_deviation_pct": {"mean": 2.0, "std": 2.0, "n": 9}},
    )
    fake_server.usage_offset_pct = 12.0
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["recount_deviation"]
    assert metrics["warn_gate_pct"] == 10.0
    assert metrics["fail_gate_pct"] == 18.0
    assert metrics["baseline"] == {
        "present": True, "mean": 2.0, "std": 2.0, "warn_gate_pct": 10.0, "fail_gate_pct": 18.0,
    }
    assert 10.0 < metrics["mean_deviation_pct"] <= 18.0

    fake_server.usage_offset_pct = 5.0
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.metrics["recount_deviation"]["mean_deviation_pct"] <= 10.0


async def test_recount_uses_injected_tokenizer_double(ctx, fake_server):
    # docs/06 §9.6: tests may inject a deterministic TokenizerService double.
    short_json = json.dumps([{"role": "user", "content": _SHORT_TEXT}])
    medium_json = json.dumps([{"role": "user", "content": _MEDIUM_TEXT}])
    long_json = json.dumps([{"role": "user", "content": _LONG_TEXT}])
    fixed = {short_json: 10, medium_json: 50, long_json: 200, _REPLY_TEXT: 5}

    class FixedTokenizer:
        def resolve_encoding(self, model: str) -> str | None:
            return "o200k_base" if model == "gpt-4o" else None

        def count(self, text: str, encoding: str) -> int:
            return fixed[text]

    fake_server.usage_offset_pct = 20.0
    probe = RecountDeviationProbe()
    probe.tokenizer = FixedTokenizer()  # type: ignore[assignment]
    result = await probe.run(ctx)
    assert result.verdict == Verdict.FAIL
    metrics = result.metrics["recount_deviation"]
    assert [s["recounted_prompt_tokens"] for s in metrics["per_sample"]] == [10, 50, 200]
    assert metrics["mean_deviation_pct"] > 15.0
    assert metrics["all_sizes_above_fail_gate"] is True


async def test_recount_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.recount_deviation")) == 6  # 3 samples x one retry


async def test_recount_partial_retry_failure_caps_at_warn(ctx, fake_server, monkeypatch):
    """§1.3: a persistent 429 on one of three sizes caps d4.recount_deviation
    at WARN (never PASS) while the two clean samples' per-sample metrics and
    evidence are preserved."""

    import supgate.probes.d4_billing as d4_billing_module

    real_request = d4_billing_module.request_with_retry
    calls = {"n": 0}

    async def _flaky_first(ctx_, probe_id, method, path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError(
                429,
                "d4.recount_deviation: rate-limited (429) after retry — Warn per §10, rerun with backoff",
            )
        return await real_request(ctx_, probe_id, method, path, **kwargs)

    monkeypatch.setattr(d4_billing_module, "request_with_retry", _flaky_first)
    result = await RecountDeviationProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score < 100.0
    assert result.successes == 2
    metrics = result.metrics["recount_deviation"]
    assert [s["size"] for s in metrics["per_sample"]] == ["medium", "long"]
    assert metrics["all_sizes_above_fail_gate"] is False
    assert any("rate-limited" in note for note in result.notes)
    # The monkeypatched failure is raised before transport, so only the two
    # clean requests produce evidence; request_with_retry evidence is covered
    # separately by the real 429 fixture tests.
    assert len(ctx.evidence.refs_for("d4.recount_deviation")) == 2


async def test_recount_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await RecountDeviationProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert all("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.recount_deviation")) == 3


# --- d4.wrap_offset ---------------------------------------------------------


async def test_wrap_passes_at_zero_offset(ctx, fake_server):
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["wrap_offset"]
    assert metrics["mean_offset_tokens"] == 0.0
    assert metrics["std_offset_tokens"] == 0.0
    assert metrics["offset_stable"] is True
    assert metrics["fail_gate_tokens"] == 32.0
    assert metrics["baseline"]["present"] is False
    assert len(metrics["per_sample"]) == 4
    assert all(s["offset_tokens"] == 0 for s in metrics["per_sample"])
    assert len(ctx.evidence.refs_for("d4.wrap_offset")) == 4
    _assert_evidence_redacted(ctx, "d4.wrap_offset")
    posts = _chat_posts(fake_server)
    assert len(posts) == 4
    assert json.loads(posts[0]["body"])["max_tokens"] == 16
    assert json.loads(posts[0]["body"])["temperature"] == 0


async def test_wrap_warns_at_11_tokens(ctx, fake_server):
    fake_server.hidden_wrapper_tokens = 11
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["wrap_offset"]
    assert metrics["mean_offset_tokens"] == 11.0
    assert metrics["offset_stable"] is True
    assert any("hidden wrapper band" in note for note in result.notes)


async def test_wrap_fails_at_64_tokens(ctx, fake_server):
    fake_server.hidden_wrapper_tokens = 64
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["wrap_offset"]
    assert metrics["mean_offset_tokens"] == 64.0
    assert metrics["offset_stable"] is True
    assert any("exceeds fail gate" in note for note in result.notes)


async def test_wrap_unstable_offset_passes(ctx, fake_server):
    # Percentage inflation makes the offset grow with prompt length: no
    # constant wrapper, so the deviation is tokenizer/overhead noise -> PASS.
    fake_server.usage_offset_pct = 10.0
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    metrics = result.metrics["wrap_offset"]
    assert metrics["offset_stable"] is False
    assert any("no constant wrapper" in note for note in result.notes)


async def test_wrap_unknown_encoding_skips(ctx, fake_server):
    ctx.model = "mystery-model"
    ctx.claimed_models = ["mystery-model"]
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert result.attempts == 0
    assert any("unknown encoding for model" in note for note in result.notes)
    assert _chat_posts(fake_server) == []
    assert ctx.evidence.refs_for("d4.wrap_offset") == []


async def test_wrap_baseline_gate_tightens(ctx, fake_server):
    # docs/06 §5.3: baseline mean+6*std replaces the fail gate when it is
    # stricter than the static 32-token expectation.
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT4O-0001",
        fingerprints={"wrap_offset_tokens": {"mean": 2.0, "std": 1.0, "n": 12}},
    )
    fake_server.hidden_wrapper_tokens = 6
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.metrics["wrap_offset"]["fail_gate_tokens"] == 8.0
    assert result.metrics["wrap_offset"]["baseline"]["present"] is True

    fake_server.hidden_wrapper_tokens = 11
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.metrics["wrap_offset"]["fail_gate_tokens"] == 8.0


async def test_wrap_baseline_gate_never_looser_than_static(ctx, fake_server):
    # A loose baseline gate (mean+6*std = 40) must not relax the static 32.
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT4O-0001",
        fingerprints={"wrap_offset_tokens": {"mean": 10.0, "std": 5.0, "n": 12}},
    )
    fake_server.hidden_wrapper_tokens = 40
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.metrics["wrap_offset"]["fail_gate_tokens"] == 32.0


async def test_wrap_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await WrapOffsetProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.wrap_offset")) == 8  # 4 samples x one retry


# --- d4.reasoning_cache_fields ----------------------------------------------


async def test_reasoning_cache_passes_default_gpt4o(ctx, fake_server):
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["expected"] == {"cached_tokens": True, "reasoning_tokens": False}
    assert metrics["baseline_usage_schema"] is None
    assert metrics["cached_seen"] is True
    assert metrics["reasoning_seen"] is False
    assert metrics["missing_expected"] is False
    assert metrics["contradictory"] is False
    assert metrics["cache_delta_ok"] is True
    assert len(metrics["per_call"]) == 3
    assert all(call["cached_ok"] for call in metrics["per_call"])
    assert all(call["reasoning_ok"] for call in metrics["per_call"])
    assert len(ctx.evidence.refs_for("d4.reasoning_cache_fields")) == 3
    _assert_evidence_redacted(ctx, "d4.reasoning_cache_fields")

    posts = _chat_posts(fake_server)
    assert len(posts) == 3
    bodies = [json.loads(p["body"]) for p in posts]
    assert all(b["messages"] == bodies[0]["messages"] for b in bodies)
    assert count(json.dumps(bodies[0]["messages"]), "o200k_base") >= 300
    assert bodies[0]["max_tokens"] == 24


async def test_reasoning_cache_passes_o1_full_schema(ctx, fake_server):
    ctx.model = "o1"
    ctx.claimed_models = ["o1"]
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["expected"] == {"cached_tokens": True, "reasoning_tokens": True}
    assert metrics["cached_seen"] is True
    assert metrics["reasoning_seen"] is True


async def test_reasoning_cache_passes_caching_delta_growth(ctx, fake_server):
    fake_server.usage_schema_flags = {"caching_delta": True}
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["per_call"][0]["cached_tokens"] == 0
    assert metrics["per_call"][1]["cached_tokens"] == metrics["per_call"][1]["prompt_tokens"]
    assert metrics["cache_deltas"][0] > 0
    assert metrics["cache_delta_ok"] is True


async def test_reasoning_cache_missing_expected_fields_warns(ctx, fake_server):
    ctx.model = "o1"
    ctx.claimed_models = ["o1"]
    fake_server.usage_schema_flags = {"cached_tokens": False}
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["expected"] == {"cached_tokens": True, "reasoning_tokens": True}
    assert metrics["cached_seen"] is False
    assert metrics["missing_expected"] is True
    assert any("expected usage details fields missing" in note for note in result.notes)


async def test_reasoning_cache_cached_overflow_fails(ctx, fake_server):
    fake_server.usage_schema_flags = {"cached_tokens_bad": True}
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["contradictory"] is True
    assert any(call["cached_tokens"] > call["prompt_tokens"] for call in metrics["per_call"])
    assert any("contradictory" in note for note in result.notes)


async def test_reasoning_cache_reasoning_overflow_fails(ctx, fake_server):
    fake_server.usage_schema_flags = {"reasoning_tokens": True, "reasoning_tokens_bad": True}
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["contradictory"] is True
    assert any(call["reasoning_tokens"] > call["completion_tokens"] for call in metrics["per_call"])


async def test_reasoning_cache_cache_regress_warns(ctx, fake_server):
    fake_server.usage_schema_flags = {"caching_regress": True}
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["cache_delta_ok"] is False
    assert metrics["cache_deltas"][0] < 0
    assert any("regressed" in note for note in result.notes)


async def test_reasoning_cache_unknown_family_skips(ctx, fake_server):
    ctx.model = "mystery-model"
    ctx.claimed_models = ["mystery-model"]
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert result.attempts == 0
    assert any("unknown to the usage schema table" in note for note in result.notes)
    assert _chat_posts(fake_server) == []
    assert ctx.evidence.refs_for("d4.reasoning_cache_fields") == []


async def test_reasoning_cache_baseline_usage_schema_overrides(ctx, fake_server):
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT4O-0001",
        fingerprints={"usage_schema": {"cached_tokens": True, "reasoning_tokens": True}},
    )
    result = await ReasoningCacheFieldsProbe().run(ctx)
    # Baseline expects reasoning_tokens for gpt-4o, but the default fixture
    # emits only the family-appropriate schema -> missing expected field.
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["baseline_usage_schema"] == {"cached_tokens": True, "reasoning_tokens": True}
    assert metrics["expected"] == {"cached_tokens": True, "reasoning_tokens": True}
    assert metrics["reasoning_seen"] is False
    assert metrics["missing_expected"] is True


async def test_reasoning_cache_baseline_enables_unknown_family(ctx, fake_server):
    ctx.model = "claude-3-5-sonnet-20241022"
    ctx.claimed_models = ["claude-3-5-sonnet-20241022"]
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-CLAUDE-0001",
        fingerprints={"usage_schema": {"cached_tokens": True, "reasoning_tokens": False}},
    )
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict != Verdict.SKIP
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["reasoning_cache_fields"]
    assert metrics["expected"] == {"cached_tokens": True, "reasoning_tokens": False}
    assert metrics["cached_seen"] is False
    assert metrics["missing_expected"] is True


async def test_reasoning_cache_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await ReasoningCacheFieldsProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d4.reasoning_cache_fields")) == 6  # 3 calls x one retry
