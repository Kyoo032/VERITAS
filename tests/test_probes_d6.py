"""D6 protocol probes (§10.2): custom runners + manifest-driven generic ones."""

from __future__ import annotations

import httpx
import pytest

from supgate.models import Verdict
from supgate.probes.d6_protocol import (
    ResponsesApiProbe,
    SseProbe,
    UsageFieldsProbe,
    VisionProbe,
    parse_sse,
)
from supgate.registry import ManifestProbe
from tests.fake_server import _respond


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)

MANIFEST_SAMPLES: dict[str, tuple[dict, str]] = {
    "d6.chat.basic": (
        {
            "id": "d6.chat.basic", "domain": "D6", "samples": 3,
            "request": {"messages": [{"role": "user", "content": "Say hello in exactly one short sentence."}], "max_tokens": 32},
            "pass": "status == 200 and finish_reason('stop') and content_contains('hello')",
        },
        Verdict.PASS,
    ),
    "d6.json_mode": (
        {
            "id": "d6.json_mode", "domain": "D6", "samples": 2,
            "request": {"messages": [{"role": "user", "content": 'Return JSON with keys "name" and "value".'}], "max_tokens": 64, "response_format": {"type": "json_object"}},
            "pass": "status == 200 and json_parses and has_keys(['name', 'value'])",
        },
        Verdict.PASS,
    ),
    "d6.idempotency": (
        {
            "id": "d6.idempotency", "domain": "D6", "samples": 3,
            "request": {"messages": [{"role": "user", "content": "Count from 1 to 5."}], "max_tokens": 32},
            "pass": "status == 200 and content_contains('1')",
        },
        Verdict.PASS,
    ),
}


async def test_parse_sse_handles_frames():
    text = (
        'data: {"id":"x","choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    events = parse_sse(text)
    assert len(events) == 1
    assert events[0]["choices"][0]["delta"]["content"] == "hi"


async def test_sse_probe_passes(ctx):
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.successes == 2


async def test_sse_probe_tolerates_role_only_chunks(ctx, fake_server):
    """Real-world SSE streams emit delta chunks with only 'role' set (no content)."""

    original = fake_server._sse_body

    def with_role_chunk(payload):
        return (
            b'data: {"id":"x","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            + original(payload)
        )

    fake_server._sse_body = with_role_chunk
    try:
        result = await SseProbe().run(ctx)
    finally:
        fake_server._sse_body = original
    assert result.verdict == Verdict.PASS


async def test_sse_probe_fails_on_broken_stream(ctx, fake_server):
    original = fake_server._sse_body
    fake_server._sse_body = lambda payload: b"not sse at all"
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    fake_server._sse_body = original


async def test_sse_probe_uses_runcontext_stream(ctx, monkeypatch):
    calls = []
    original = ctx.stream

    async def spy_stream(probe_id, path, *, payload, **kwargs):
        calls.append((probe_id, path, payload))
        return await original(probe_id, path, payload=payload, **kwargs)

    monkeypatch.setattr(ctx, "stream", spy_stream)
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(calls) == 2
    for probe_id, path, payload in calls:
        assert probe_id == "d6.chat.sse"
        assert path == "/chat/completions"
        assert payload["stream"] is True
        assert payload["max_tokens"] == 32


async def test_sse_probe_surfaces_timing_samples(ctx):
    result = await SseProbe().run(ctx)
    by_kind: dict[str, list[float]] = {}
    for sample in result.samples:
        by_kind.setdefault(sample.kind, []).append(sample.ms)
    assert len(by_kind["ttft"]) == 2
    assert len(by_kind["e2e"]) == 2
    assert all(0 <= ttft <= e2e for ttft, e2e in zip(by_kind["ttft"], by_kind["e2e"], strict=True))
    assert by_kind["itl"]
    assert all(delay >= 0 for delay in by_kind["itl"])


async def test_sse_probe_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert len(ctx.evidence.refs_for("d6.chat.sse")) == 2


async def test_sse_probe_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.WARN


async def test_sse_probe_transport_error_fails(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await SseProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert len(ctx.evidence.refs_for("d6.chat.sse")) == 2


async def test_sse_probe_midstream_failure_fails(ctx):
    reads = {"n": 0}

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads["n"] += 1
            yield b'data: {"choices": [{"index": 0, "delta": {"content": "hi"}}]}\n\n'
            raise httpx.ReadError("mid-stream disconnect")

    class HalfStreamTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=FailingStream(),
                request=request,
            )

    half_client = httpx.AsyncClient(transport=HalfStreamTransport(), timeout=10)
    ctx.client = half_client
    try:
        result = await SseProbe().run(ctx)
    finally:
        await half_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert reads["n"] == 2  # one per sample, no mid-stream retry
    assert len(ctx.evidence.refs_for("d6.chat.sse")) == 2


async def test_sse_probe_evidence_not_duplicated_on_retry(ctx, fake_server, no_sleep):
    original = fake_server._handle_chat
    calls = {"n": 0}

    async def handler(send, body, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return await _respond(
                send,
                429,
                {"error": {"message": "Rate limit", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            )
        return await original(send, body, headers)

    fake_server._handle_chat = handler
    try:
        result = await SseProbe().run(ctx)
    finally:
        fake_server._handle_chat = original
    assert result.verdict == Verdict.PASS
    assert calls["n"] == 3  # sample 0 retried (429 -> 200), sample 1 clean
    refs = ctx.evidence.refs_for("d6.chat.sse")
    assert len(refs) == 2  # one evidence doc per sample, none for the retried attempt


async def test_usage_fields_passes(ctx):
    result = await UsageFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS


async def test_usage_fields_streams_via_runcontext_stream(ctx, monkeypatch):
    calls = []
    original = ctx.stream

    async def spy_stream(probe_id, path, *, payload, **kwargs):
        calls.append((probe_id, path, payload))
        return await original(probe_id, path, payload=payload, **kwargs)

    monkeypatch.setattr(ctx, "stream", spy_stream)
    result = await UsageFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(calls) == 1
    probe_id, path, payload = calls[0]
    assert probe_id == "d6.usage_fields"
    assert path == "/chat/completions"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


async def test_usage_fields_surfaces_stream_timing(ctx):
    result = await UsageFieldsProbe().run(ctx)
    kinds = {sample.kind for sample in result.samples}
    assert {"ttft", "e2e", "itl"} <= kinds


async def test_usage_fields_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await UsageFieldsProbe().run(ctx)
    assert result.verdict == Verdict.WARN


async def test_usage_fields_evidence_no_duplication(ctx):
    result = await UsageFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(ctx.evidence.refs_for("d6.usage_fields")) == 2  # non-stream + stream


async def test_vision_passes(ctx):
    result = await VisionProbe().run(ctx)
    assert result.verdict == Verdict.PASS


async def test_vision_skips_when_unsupported(ctx, fake_server):
    fake_server.vision_enabled = False
    result = await VisionProbe().run(ctx)
    assert result.verdict == Verdict.SKIP


async def test_responses_api_passes(ctx):
    result = await ResponsesApiProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert ctx.surface.responses_api is True


async def test_responses_api_skips_when_unsupported(ctx, fake_server):
    fake_server.responses_api_enabled = False
    result = await ResponsesApiProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert ctx.surface.responses_api is False


async def test_manifest_chat_basic_passes(ctx):
    spec, expected = MANIFEST_SAMPLES["d6.chat.basic"]
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == expected
    assert result.successes == 3


async def test_manifest_json_mode_passes(ctx):
    spec, expected = MANIFEST_SAMPLES["d6.json_mode"]
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == expected


async def test_manifest_idempotency_passes(ctx):
    spec, expected = MANIFEST_SAMPLES["d6.idempotency"]
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == expected


async def test_manifest_param_boundaries_all_clean(ctx):
    spec = {
        "id": "d6.param_boundaries", "domain": "D6", "samples": 5,
        "cases": [
            {"name": "t0", "samples": 1, "request": {"temperature": 0}, "pass": "status == 200"},
            {"name": "t2", "samples": 1, "request": {"temperature": 2}, "pass": "status == 200 or (status == 400 and error_object)"},
            {"name": "top_p", "samples": 1, "request": {"top_p": 0.01}, "pass": "status == 200 or (status == 400 and error_object)"},
            {"name": "n2", "samples": 1, "request": {"n": 2}, "pass": "choices == 2 or (status == 400 and error_object)"},
            {"name": "stop", "samples": 1, "request": {"stop": ["STOP"]}, "pass": "status == 200 and finish_reason('stop') or (status == 400 and error_object)"},
        ],
    }
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.successes == 5


async def test_manifest_max_tokens_cases(ctx):
    spec = {
        "id": "d6.max_tokens", "domain": "D6", "samples": 2,
        "cases": [
            {"name": "one", "samples": 1, "request": {"messages": [{"role": "user", "content": "long essay"}], "max_tokens": 1}, "pass": "status == 200 and finish_reason('length')"},
            {"name": "absurd", "samples": 1, "request": {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 999999}, "pass": "status == 200 or (status == 400 and error_object)"},
        ],
    }
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == Verdict.PASS


async def test_manifest_tool_passthrough_none_case(ctx):
    spec = {
        "id": "d6.tool_passthrough", "domain": "D6", "samples": 2,
        "cases": [
            {"name": "auto", "samples": 1, "request": {"messages": [{"role": "user", "content": "use a tool"}], "max_tokens": 64, "tools": [{"type": "function", "function": {"name": "get_weather", "description": "w", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}], "tool_choice": "auto"}, "pass": "status == 200"},
            {"name": "none", "samples": 1, "request": {"messages": [{"role": "user", "content": "use a tool"}], "max_tokens": 64, "tools": [{"type": "function", "function": {"name": "get_weather", "description": "w", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}], "tool_choice": "none"}, "pass": "status == 200 and no_tool_calls"},
        ],
    }
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.successes == 2


async def test_manifest_warns_on_partial_success(ctx, fake_server):
    calls = {"n": 0}

    def flaky_choices(payload, n):
        calls["n"] += 1
        if calls["n"] % 2:
            return []
        return [{"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}]

    original = fake_server._choices
    fake_server._choices = flaky_choices
    spec, _ = MANIFEST_SAMPLES["d6.chat.basic"]
    result = await ManifestProbe(spec).run(ctx)
    assert result.verdict == Verdict.WARN
    fake_server._choices = original
