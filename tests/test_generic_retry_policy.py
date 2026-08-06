"""§10 global retry policy for the generic ManifestProbe runner.

The manifest-driven chat_completion runner must match the custom P0/D6
probes: one backoff retry on 429/5xx, then an explicit WARN (never a silent
FAIL). Real protocol/pass-criteria failures and transport errors stay FAIL
so they still surface as defects. Backoff is monkeypatched to keep the suite
fast.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from supgate.models import Verdict
from supgate.registry import ManifestProbe
from tests.fake_server import _respond


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.registry.asyncio.sleep", _no_sleep)


def _flaky_handle_chat(original: Callable, fail_first: int = 1, status: int = 500):
    """Server that answers the first ``fail_first`` chat requests with ``status``."""

    calls = {"n": 0}

    async def handler(send, body: bytes, headers: dict[str, str]) -> None:
        calls["n"] += 1
        if calls["n"] <= fail_first:
            return await _respond(
                send,
                status,
                {"error": {"message": "boom", "type": "server_error", "code": "internal_error"}},
            )
        return await original(send, body, headers)

    return handler, calls


async def test_all_persistent_5xx_warns(ctx, fake_server, no_sleep):
    probe = ManifestProbe({"id": "g.5xx", "domain": "D6", "samples": 1})
    fake_server.force_5xx = True
    result = await probe.run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("server error" in note for note in result.notes)


async def test_mixed_success_and_5xx_partial_warn(ctx, fake_server, no_sleep):
    probe = ManifestProbe({"id": "g.mixed", "domain": "D6", "samples": 2})
    original = fake_server._handle_chat
    handler, calls = _flaky_handle_chat(original, fail_first=2, status=500)
    fake_server._handle_chat = handler
    result = await probe.run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.successes == 1
    assert result.attempts == 2
    assert result.score == 50.0
    assert calls["n"] == 3  # 2x500 (retried) then one pass


async def test_all_persistent_429_warns(ctx, fake_server, no_sleep):
    probe = ManifestProbe({"id": "g.429", "domain": "D6", "samples": 1})
    fake_server.force_429 = True
    result = await probe.run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)


async def test_mixed_429_and_5xx_all_retryable_warns(ctx, fake_server, no_sleep):
    probe = ManifestProbe({"id": "g.retry", "domain": "D6", "samples": 2})
    calls = {"n": 0}
    statuses = {1: 429, 2: 429, 3: 500, 4: 500}

    async def handler(send, body: bytes, headers: dict[str, str]) -> None:
        calls["n"] += 1
        return await _respond(
            send,
            statuses.get(calls["n"], 500),
            {"error": {"message": "e", "type": "server_error", "code": "internal_error"}},
        )

    fake_server._handle_chat = handler
    result = await probe.run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.successes == 0


async def test_transport_error_stays_fail(ctx):
    probe = ManifestProbe({"id": "g.transport", "domain": "D6", "samples": 1})

    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await probe.run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)


async def test_pass_criteria_failure_stays_fail(ctx, fake_server):
    probe = ManifestProbe(
        {
            "id": "g.passfail",
            "domain": "D6",
            "samples": 1,
            "pass": "status == 200 and content_contains('goodbye')",
        }
    )
    result = await probe.run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("pass criteria not met" in note for note in result.notes)


async def test_5xx_mixed_with_pass_criteria_failure_stays_fail(ctx, fake_server, no_sleep):
    probe = ManifestProbe(
        {
            "id": "g.mixedhard",
            "domain": "D6",
            "samples": 2,
            "pass": "status == 200 and content_contains('goodbye')",
        }
    )
    original = fake_server._handle_chat
    handler, _ = _flaky_handle_chat(original, fail_first=2, status=500)
    fake_server._handle_chat = handler
    result = await probe.run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.successes == 0
