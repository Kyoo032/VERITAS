"""Phase 2A: RunContext.stream typed streaming foundation (§3.3).

Real httpx streamed SSE consumption against the fake ASGI server: a typed
StreamedEvent collection with status/headers/raw body, TTFT/E2E/inter-event
timing, budget accounting, evidence/redacted curl exactly once, and the
request_with_retry policy — one backoff retry on 429/5xx before any bytes
are consumed, persistent statuses raise RateLimitError/ServerError, and
transport errors propagate with no mid-stream retry.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from supgate.probes.base import RateLimitError, ServerError
from supgate.tokenizers import count, resolve_encoding
from tests.fake_server import _respond


def _chat_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Say hello"}],
        "stream": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)


async def test_stream_returns_completed_result(ctx, tmp_path):
    result = await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())

    assert result.status == 200
    assert result.attempts == 1
    assert result.ttft_ms is not None and result.ttft_ms >= 0
    assert result.e2e_ms >= result.ttft_ms
    assert len(result.events) >= 3
    assert len(result.inter_event_ms) == len(result.events) - 1
    assert all(delay >= 0 for delay in result.inter_event_ms)
    assert result.events[0].delta == ""
    assert result.events[-1].delta.rstrip() == "Hello! This is a fake completion reply."
    assert all(event.arrived_ms >= 0 for event in result.events)
    assert "data:" in result.body
    assert "[DONE]" in result.body

    assert ctx.budget.requests == 1
    encoding = resolve_encoding("gpt-4o")
    assert encoding is not None
    assert ctx.budget.prompt_tokens == count(json.dumps(_chat_payload()), encoding)
    assert ctx.budget.completion_tokens == count(result.body, encoding)

    refs = ctx.evidence.refs_for("d6.chat.sse")
    assert len(refs) == 1
    assert result.evidence_ref == refs[0]
    assert "$SUPGATE_KEY" in result.curl
    assert "sk-test" not in result.curl
    doc = (ctx.evidence.dir.parent / refs[0]).read_text(encoding="utf-8")
    assert "sk-test" not in doc
    assert "data:" in doc


async def test_stream_captures_usage_event(ctx):
    result = await ctx.stream(
        "d6.usage_fields",
        "/chat/completions",
        payload=_chat_payload(stream_options={"include_usage": True}),
    )
    usage_events = [event for event in result.events if event.usage is not None]
    assert len(usage_events) == 1
    usage = usage_events[0].usage
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert result.events[-1].usage is usage


async def test_stream_retries_once_on_429_then_succeeds(ctx, fake_server, no_sleep):
    original = fake_server._handle_chat
    calls = {"n": 0}

    async def handler(send, body: bytes, headers: dict[str, str]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            return await _respond(
                send,
                429,
                {"error": {"message": "Rate limit", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            )
        return await original(send, body, headers)

    fake_server._handle_chat = handler
    result = await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())
    assert calls["n"] == 2
    assert result.attempts == 2
    assert result.status == 200
    assert result.events[-1].delta.rstrip().endswith("reply.")
    assert len(ctx.evidence.refs_for("d6.chat.sse")) == 1


async def test_stream_persistent_429_raises_rate_limit_error(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    with pytest.raises(RateLimitError) as excinfo:
        await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())
    assert excinfo.value.status == 429
    refs = ctx.evidence.refs_for("d6.chat.sse")
    assert len(refs) == 1
    doc = json.loads((ctx.evidence.dir.parent / refs[0]).read_text(encoding="utf-8"))
    assert doc["response"]["status"] == 429


async def test_stream_persistent_5xx_raises_server_error(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    with pytest.raises(ServerError) as excinfo:
        await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())
    assert excinfo.value.status == 500
    assert len(ctx.evidence.refs_for("d6.chat.sse")) == 1


async def test_stream_does_not_retry_non_retryable_status(ctx, fake_server):
    ctx.api_key = "sk-wrong-key-00000000"
    result = await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())
    assert result.status == 401
    assert result.events == []
    assert result.ttft_ms is None
    assert "invalid_api_key" in result.body
    assert len(fake_server.requests_log) == 1
    assert len(ctx.evidence.refs_for("d6.chat.sse")) == 1


async def test_stream_transport_error_propagates_and_records_evidence(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        with pytest.raises(httpx.ConnectError):
            await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())
    finally:
        await crash_client.aclose()
    refs = ctx.evidence.refs_for("d6.chat.sse")
    assert len(refs) == 1
    doc = json.loads((ctx.evidence.dir.parent / refs[0]).read_text(encoding="utf-8"))
    assert doc["response"]["status"] == 0
    assert "transport error" in doc["response"]["body"]


async def test_stream_midstream_error_propagates_no_retry_partial_evidence(ctx):
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
        with pytest.raises(httpx.TransportError):
            await ctx.stream("d6.chat.sse", "/chat/completions", payload=_chat_payload())
    finally:
        await half_client.aclose()

    assert reads["n"] == 1  # no retry after bytes/events were emitted
    refs = ctx.evidence.refs_for("d6.chat.sse")
    assert len(refs) == 1
    doc = json.loads((ctx.evidence.dir.parent / refs[0]).read_text(encoding="utf-8"))
    assert doc["response"]["status"] == 200
    assert "hi" in doc["response"]["body"]
