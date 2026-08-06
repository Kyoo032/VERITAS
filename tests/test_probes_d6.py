"""D6 protocol probes (§10.2): custom runners + manifest-driven generic ones."""

from __future__ import annotations

from supgate.models import Verdict
from supgate.probes.d6_protocol import (
    ResponsesApiProbe,
    SseProbe,
    UsageFieldsProbe,
    VisionProbe,
    parse_sse,
)
from supgate.registry import ManifestProbe

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


async def test_sse_probe_fails_on_broken_stream(ctx, fake_server):
    original = fake_server._sse_body
    fake_server._sse_body = lambda payload: b"not sse at all"
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    fake_server._sse_body = original


async def test_usage_fields_passes(ctx):
    result = await UsageFieldsProbe().run(ctx)
    assert result.verdict == Verdict.PASS


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
