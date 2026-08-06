"""§10 global retry/backoff policy for custom P0/D6 probes.

One backoff retry on 429/5xx, then an explicit WARN (never FAIL); transport
errors stay FAIL so ``endpoint_dead`` still triggers on a dead endpoint.
Backoff is monkeypatched to keep the suite fast.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from supgate.models import Verdict
from supgate.orchestrator import endpoint_dead
from supgate.probes.d6_protocol import SseProbe
from supgate.probes.p0 import EchoProbe, ErrorContractProbe, ModelsProbe
from tests.fake_server import _respond


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("supgate.registry.asyncio.sleep", _no_sleep)


def _flaky_handle_chat(original: Callable, fail_first: int = 1, status: int = 429):
    """Server that answers the first ``fail_first`` chat requests with ``status``."""

    calls = {"n": 0}

    async def handler(send, body: bytes, headers: dict[str, str]) -> None:
        calls["n"] += 1
        if calls["n"] <= fail_first:
            return await _respond(
                send,
                status,
                {"error": {"message": "Rate limit", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            )
        return await original(send, body, headers)

    return handler, calls


async def test_echo_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await EchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_echo_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await EchoProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_echo_transport_error_stays_fail(ctx):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    crash_client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await EchoProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("unreachable" in note for note in result.notes)


async def test_echo_retries_429_then_passes(ctx, fake_server, no_sleep):
    original = fake_server._handle_chat
    handler, calls = _flaky_handle_chat(original, fail_first=1, status=429)
    fake_server._handle_chat = handler
    result = await EchoProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert calls["n"] == 2


async def test_models_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await ModelsProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_error_contract_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await ErrorContractProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_error_contract_retries_429_then_asserts_401_400(ctx, fake_server, no_sleep):
    original = fake_server._handle_chat
    handler, calls = _flaky_handle_chat(original, fail_first=1, status=429)
    fake_server._handle_chat = handler
    result = await ErrorContractProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.successes == 2
    assert calls["n"] == 3  # 1×429 (retried) + 401-check + 400-check


async def test_sse_persistent_429_warns(ctx, fake_server, no_sleep):
    fake_server.force_429 = True
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_sse_persistent_5xx_warns(ctx, fake_server, no_sleep):
    fake_server.force_5xx = True
    result = await SseProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_echo_429_does_not_trigger_endpoint_dead(orchestrator, fake_server, manifest, tmp_path, no_sleep):
    fake_server.force_429 = True
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.WARN
    assert not endpoint_dead(bundle)


async def test_echo_5xx_does_not_trigger_endpoint_dead(orchestrator, fake_server, manifest, tmp_path, no_sleep):
    fake_server.force_5xx = True
    bundle = await orchestrator.run(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        claimed_models=["gpt-4o"],
        manifest_path=manifest,
        mode="full",
        out_dir=tmp_path,
    )
    echo = next(p for p in bundle.probes if p.probe_id == "p0.echo")
    assert echo.verdict == Verdict.WARN
    assert not endpoint_dead(bundle)
