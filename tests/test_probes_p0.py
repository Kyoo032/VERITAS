"""P0 foundation probes (§10.1) against the fake server."""

from __future__ import annotations

from supgate.models import Verdict
from supgate.probes.p0 import EchoProbe, ErrorContractProbe, ModelsProbe


async def test_echo_passes_with_valid_key(ctx):
    result = await EchoProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.successes == 1


async def test_echo_passes_when_reasoning_model_consumes_budget(ctx, fake_server):
    """Reasoning models (e.g. deepseek-v4-flash) return empty content and put
    the thinking in reasoning_content — that is still 'alive' (§10.1)."""

    def reasoning_choices(payload, n):
        return [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "We need to reply exactly with PONG-…",
                },
                "finish_reason": "length",
            }
        ]

    original = fake_server._choices
    fake_server._choices = reasoning_choices
    try:
        result = await EchoProbe().run(ctx)
    finally:
        fake_server._choices = original
    assert result.verdict == Verdict.PASS
    assert result.successes == 1


async def test_echo_fails_when_200_but_entirely_empty(ctx, fake_server):
    """A 200 with neither content nor reasoning_content is not alive."""

    def empty_choices(payload, n):
        return [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]

    original = fake_server._choices
    fake_server._choices = empty_choices
    try:
        result = await EchoProbe().run(ctx)
    finally:
        fake_server._choices = original
    assert result.verdict == Verdict.FAIL
    assert result.successes == 0


async def test_echo_accepts_openai_text_part_content(ctx, fake_server):
    def text_part_choices(payload, n):
        token = payload["messages"][0]["content"].rsplit(": ", 1)[-1]
        return [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": token}],
                },
                "finish_reason": "stop",
            }
        ]

    original = fake_server._choices
    fake_server._choices = text_part_choices
    try:
        result = await EchoProbe().run(ctx)
    finally:
        fake_server._choices = original
    assert result.verdict == Verdict.PASS
    assert any("nonce echo verified" in note for note in result.notes)


async def test_echo_fails_with_bad_key(ctx, fake_server):
    ctx.api_key = "sk-wrong-key-000000000000000"
    result = await EchoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL


async def test_echo_warns_on_persistent_5xx(ctx, fake_server, monkeypatch):
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)
    fake_server.force_5xx = True
    result = await EchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_models_builds_surface(ctx, fake_server):
    result = await ModelsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert ctx.surface.models == fake_server.models
    assert ctx.surface.claimed_present is True


async def test_models_flags_absent_claimed_model(ctx):
    ctx.claimed_models = ["gpt-4o-custom"]
    result = await ModelsProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert ctx.surface.claimed_present is False


async def test_error_contract_passes_on_clean_server(ctx):
    result = await ErrorContractProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.successes == 2


async def test_error_contract_fails_when_wrong_key_accepted(ctx, fake_server):
    fake_server.reject_unknown_keys = False
    result = await ErrorContractProbe().run(ctx)
    assert result.successes == 1
    assert result.verdict == Verdict.WARN


async def test_error_contract_evidence_is_redacted(ctx):
    result = await ErrorContractProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    refs = ctx.evidence.refs_for("p0.error_contract")
    assert len(refs) == 2
