"""d6.idempotency custom runner: stable pass, mixed-length detection, 429/5xx WARN."""

from __future__ import annotations

from supgate.models import Verdict
from supgate.probes.idempotency import LENGTH_SPREAD_BOUND, IdempotencyProbe


async def test_idempotency_stable_passes(ctx):
    result = await IdempotencyProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 3
    joined = " ".join(result.notes)
    assert "lengths=[13, 13, 13]" in joined
    assert f"bound={LENGTH_SPREAD_BOUND}" in joined


async def test_idempotency_mixed_length_warns(ctx, fake_server):
    seq = ["1, 2, 3, 4, 5", "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12", "1, 2, 3, 4, 5"]
    state = {"i": 0}

    def mixed_choices(payload, n):
        content = seq[state["i"] % len(seq)]
        state["i"] += 1
        return [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]

    original = fake_server._choices
    fake_server._choices = mixed_choices
    try:
        result = await IdempotencyProbe().run(ctx)
    finally:
        fake_server._choices = original

    assert result.verdict == Verdict.WARN
    joined = " ".join(result.notes)
    assert "lengths=[13, 37, 13]" in joined
    assert f"bound={LENGTH_SPREAD_BOUND}" in joined
    assert "exceeds bound" in joined


async def test_idempotency_structure_mismatch_fails(ctx, fake_server):
    seq = ["1, 2, 3, 4, 5", "1, 2, 3, 4, 5", "1, 2, 3, 4, 5"]
    state = {"i": 0}

    def mixed_choices(payload, n):
        content = seq[state["i"] % len(seq)]
        message = {"role": "assistant", "content": content}
        if state["i"] % len(seq) == 1:
            message["refusal"] = None
        state["i"] += 1
        return [{"index": 0, "message": message, "finish_reason": "stop"}]

    original = fake_server._choices
    fake_server._choices = mixed_choices
    try:
        result = await IdempotencyProbe().run(ctx)
    finally:
        fake_server._choices = original

    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("structure differs" in note for note in result.notes)


async def test_idempotency_persistent_429_warns(ctx, fake_server):
    fake_server.force_429 = True
    result = await IdempotencyProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.successes == 0
    assert any("429" in note for note in result.notes)


async def test_idempotency_persistent_5xx_warns(ctx, fake_server):
    fake_server.force_5xx = True
    result = await IdempotencyProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.successes == 0
    assert any("500" in note for note in result.notes)
