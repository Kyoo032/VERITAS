"""D2 load probes (build plan §10.5; docs/05 §6 U1): d2.load_matrix +
d2.needle_recall.

Runtime is bounded: every probe instance in these tests overrides the
class constants (calls per band, concurrency, band token targets, context
tokens) with tiny values. Deterministic math tests (percentiles, TPOT
first-token exclusion, goodput, SLA overrides) drive the probes through a
stubbed ``ctx.stream`` returning exact :class:`StreamResult` values; the
purpose-built ASGI apps below cover behavior (post-semaphore timing,
semaphore-bound concurrency, retry WARN, invalid stream, transport) without
touching the shared ``tests/fake_server.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from supgate.evidence import EvidenceWriter
from supgate.models import (
    SLA,
    BudgetTracker,
    Domain,
    StreamedEvent,
    StreamResult,
    SurfaceMap,
    Verdict,
)
from supgate.probes.base import Probe, RunContext
from supgate.probes.d2_load import (
    LoadMatrixProbe,
    NeedleRecallProbe,
    _band_label,
    _percentile,
)

API_KEY = "sk-test-d2-secret-key"


# ---------------------------------------------------------------------------
# Purpose-built ASGI apps (shared fake_server stays untouched)
# ---------------------------------------------------------------------------


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(body).encode(), "more_body": False})


async def _send_raw(send: Any, status: int, raw: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/event-stream")],
        }
    )
    await send({"type": "http.response.body", "body": raw, "more_body": False})


class D2StreamApp:
    """SSE chat-completions app for d2.load_matrix with controllable latency."""

    def __init__(self) -> None:
        self.status = 200
        self.first_token_delay_ms = 0
        self.chunk_gap_ms = 0
        self.chunk_count = 3
        self.content_delta = "t"
        self.completion_tokens = 5
        self.include_usage = True
        self.force_429 = False
        self.force_5xx = False
        self.raw_body: bytes | None = None  # sent verbatim when set (invalid-stream fixture)
        self.request_count = 0
        self.max_inflight = 0
        self._inflight = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        await _read_body(receive)
        self.request_count += 1
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.force_429:
                await _send_json(
                    send,
                    429,
                    {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                )
                return
            if self.force_5xx:
                await _send_json(send, 500, {"error": {"message": "boom", "type": "server_error"}})
                return
            if self.raw_body is not None:
                await _send_raw(send, self.status, self.raw_body)
                return
            await self._stream(send)
        finally:
            self._inflight -= 1

    async def _stream(self, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        if self.first_token_delay_ms:
            await asyncio.sleep(self.first_token_delay_ms / 1000.0)
        for i in range(self.chunk_count):
            event: dict[str, Any] = {
                "id": f"chatcmpl-d2-{i}",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": self.content_delta},
                        "finish_reason": "stop" if i == self.chunk_count - 1 else None,
                    }
                ],
            }
            if self.include_usage and i == self.chunk_count - 1:
                event["usage"] = {
                    "prompt_tokens": 10,
                    "completion_tokens": self.completion_tokens,
                }
            line = f"data: {json.dumps(event)}\n\n".encode()
            await send({"type": "http.response.body", "body": line, "more_body": True})
            if self.chunk_gap_ms and i < self.chunk_count - 1:
                await asyncio.sleep(self.chunk_gap_ms / 1000.0)
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})


class D2NeedleApp:
    """Chat-completions app for d2.needle_recall; locates the unique needle in
    the request and echoes/mutates/omits it per ``mode``."""

    def __init__(self) -> None:
        self.status = 200
        self.mode = "echo"  # echo | altered | missing | unrelated
        self.error = {
            "message": "This model's maximum context length is 8192 tokens. However, you requested 32000 tokens.",
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
        self.force_429 = False
        self.force_5xx = False
        self.request_count = 0
        self.last_needle = ""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        raw = await _read_body(receive)
        self.request_count += 1
        if self.force_429:
            await _send_json(send, 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}})
            return
        if self.force_5xx:
            await _send_json(send, 500, {"error": {"message": "boom", "type": "server_error"}})
            return
        if self.status != 200:
            await _send_json(send, self.status, {"error": self.error})
            return
        match = re.search(rb"VERITAS-NEEDLE-[A-Z0-9]+", raw)
        needle = match.group(0).decode() if match else ""
        self.last_needle = needle
        if self.mode == "echo":
            content = needle
        elif self.mode == "altered":
            content = needle[:-1] + "X" if needle else "altered"
        elif self.mode == "missing":
            content = "I searched the document and found no verification token."
        else:
            content = "The document is about maritime logistics and pendulum records."
        await _send_json(
            send,
            200,
            {
                "id": "chatcmpl-d2n",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)


def _ctx_for(app: Any, tmp_path: Path) -> RunContext:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=10)
    return RunContext(
        endpoint="https://d2.example/v1",
        api_key=API_KEY,
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=SurfaceMap(models=["gpt-4o"], claimed_present=True),
        client=client,
        evidence=EvidenceWriter(tmp_path / "evidence", "TEST-D2"),
        budget=BudgetTracker(),
    )


async def _run_with_app(probe: Any, app: Any, tmp_path: Path) -> tuple[Any, RunContext]:
    ctx = _ctx_for(app, tmp_path)
    try:
        return await probe.run(ctx), ctx
    finally:
        await ctx.client.aclose()


def _stream_result(
    *,
    ttft_ms: float,
    e2e_ms: float,
    inter_event_ms: list[float],
    tokens: int,
    status: int = 200,
) -> StreamResult:
    """Exact StreamResult with a final usage-bearing event (include_usage)."""
    chunks = max(1, len(inter_event_ms) + 1)
    events = []
    running = ""
    for i in range(chunks):
        running += "t"
        events.append(StreamedEvent(delta=running, arrived_ms=100.0 + i * 10.0))
    events[-1].usage = {"prompt_tokens": 10, "completion_tokens": tokens}
    return StreamResult(
        status=status,
        headers={},
        body="data: x\n\n",
        events=events,
        ttft_ms=ttft_ms,
        e2e_ms=e2e_ms,
        inter_event_ms=list(inter_event_ms),
        curl=None,
        evidence_ref=None,
        attempts=1,
    )


def _stub_stream_factory(results: list[StreamResult]):
    state = {"i": 0}

    async def stub(probe_id: str, path: str, *, payload: dict[str, Any], **kwargs: Any):
        result = results[state["i"] % len(results)]
        state["i"] += 1
        return result

    return stub


class _FixedTokenizer:
    """Deterministic TokenizerService double: exact counts, no tiktoken."""

    def __init__(self, count_value: int = 123, encoding: str | None = "fake-encoding") -> None:
        self.count_value = count_value
        self.encoding = encoding

    def resolve_encoding(self, model: str) -> str | None:
        return self.encoding if model == "gpt-4o" else None

    def count(self, text: str, encoding: str) -> int:
        assert encoding == self.encoding
        return self.count_value


# ---------------------------------------------------------------------------
# LoadMatrixProbe: class contract, defaults, SLA override
# ---------------------------------------------------------------------------


def test_load_matrix_class_contract():
    probe = LoadMatrixProbe()
    assert probe.id == "d2.load_matrix"
    assert probe.domain == Domain.D2
    assert probe.weight == 1.0
    assert probe.samples == 60  # 3 bands x 20 calls (build plan §10.5)
    assert probe.calls_per_band == 20
    assert probe.concurrency == 10
    assert probe.bands == (4000, 12000, 24000)
    assert probe.skip_reason(SurfaceMap()) is None
    assert isinstance(probe, Probe)
    assert probe.sla == {"ttft_s": 5.0, "tpot_ms": 500.0, "e2e_s": 60.0}  # docs/05 §6 U1


def test_load_matrix_constructor_overrides():
    probe = LoadMatrixProbe(calls_per_band=3, concurrency=2, bands=(40, 120))
    assert probe.calls_per_band == 3
    assert probe.concurrency == 2
    assert probe.bands == (40, 120)


def test_band_labels():
    assert _band_label(4000) == "<8K"
    assert _band_label(12000) == "8-16K"
    assert _band_label(24000) == "16-32K"
    assert _band_label(0) == "<8K"
    assert _band_label(8000) == "8-16K"
    assert _band_label(16000) == "16-32K"


def test_percentile_exact_math():
    assert _percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert _percentile([10.0, 20.0, 30.0], 90) == 28.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert _percentile([1.0, 2.0, 3.0, 4.0], 90) == 3.7
    assert _percentile([5.0], 50) == 5.0
    assert _percentile([], 50) == 0.0


def test_load_prompt_deterministic_and_approximates_target():
    probe = LoadMatrixProbe()
    a, b = probe.build_prompt(4000), probe.build_prompt(4000)
    assert a == b
    approx = len(a) / probe.APPROX_CHARS_PER_TOKEN
    assert abs(approx - 4000) / 4000 < 0.05  # chars-per-token approximation (§10.5)
    assert "Reply with the word PONG." in a


def test_load_count_prompt_tokens_best_effort():
    probe = LoadMatrixProbe(tokenizer=_FixedTokenizer(count_value=777))
    assert probe.count_prompt_tokens("hello", "gpt-4o") == 777
    assert probe.count_prompt_tokens("hello", "unknown-model") is None


async def test_load_sla_defaults_and_apply_sla(ctx):
    """apply_sla(SLA) and apply_sla(dict) both retarget goodput thresholds."""
    good = _stream_result(ttft_ms=1000.0, e2e_ms=2000.0, inter_event_ms=[], tokens=1)
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=2, bands=(40,))
    ctx.stream = _stub_stream_factory([good])
    result = await probe.run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.metrics["load_matrix"]["sla"] == {
        "ttft_s": 5.0,
        "tpot_ms": 500.0,
        "e2e_s": 60.0,
        "goodput_bar_pct": 80.0,
    }

    probe.apply_sla(SLA(ttft_s=0.5, tpot_ms=100.0, e2e_s=1.0))
    result = await probe.run(ctx)
    assert result.verdict == Verdict.FAIL  # 1000ms TTFT now violates the 0.5s SLA
    assert result.metrics["load_matrix"]["sla"] == {
        "ttft_s": 0.5,
        "tpot_ms": 100.0,
        "e2e_s": 1.0,
        "goodput_bar_pct": 80.0,
    }

    probe.apply_sla({"ttft_s": 2.0, "tpot_ms": 50.0, "e2e_s": 0.5})
    result = await probe.run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.metrics["load_matrix"]["sla"]["ttft_s"] == 2.0
    assert result.metrics["load_matrix"]["sla"]["e2e_s"] == 0.5


# ---------------------------------------------------------------------------
# LoadMatrixProbe: P50/P90, TPOT/ITL first-token exclusion, exact goodput
# ---------------------------------------------------------------------------


async def test_load_p50_p90_and_tpot_excludes_first_token(ctx):
    """TPOT = (E2E - TTFT) / (tokens - 1): the first output token is excluded."""
    probe = LoadMatrixProbe(calls_per_band=3, concurrency=3, bands=(40,))
    results = [
        _stream_result(ttft_ms=10.0, e2e_ms=50.0, inter_event_ms=[8.0] * 5, tokens=6),
        _stream_result(ttft_ms=20.0, e2e_ms=100.0, inter_event_ms=[8.0] * 10, tokens=11),
        _stream_result(ttft_ms=30.0, e2e_ms=60.0, inter_event_ms=[], tokens=1),
    ]
    ctx.stream = _stub_stream_factory(results)
    result = await probe.run(ctx)
    assert result.verdict == Verdict.PASS
    band = result.metrics["load_matrix"]["bands"]["<8K"]
    requests = band["requests"]
    assert requests[0]["tpot_ms"] == 8.0  # (50-10)/(6-1); 50/6 ~ 8.33 would include token 1
    assert requests[1]["tpot_ms"] == 8.0  # (100-20)/(11-1)
    assert requests[2]["tpot_ms"] is None  # single-token completion: no TPOT
    assert band["p50"]["tpot_ms"] == 8.0
    assert band["p90"]["tpot_ms"] == 8.0
    assert band["p50"]["ttft_ms"] == 20.0
    assert band["p90"]["ttft_ms"] == 28.0
    # ITL comes from inter-event pairs only — the first event of each stream
    # contributes no inter-arrival delay.
    assert band["p50"]["itl_ms"] == 8.0
    assert band["p90"]["itl_ms"] == 8.0
    assert requests[2]["itl_mean_ms"] is None
    # ProbeResult.samples: ttft + e2e per request, tpot only when computable.
    kinds = [sample.kind for sample in result.samples]
    assert kinds.count("ttft") == 3
    assert kinds.count("e2e") == 3
    assert kinds.count("tpot") == 2


async def test_load_goodput_exact_math_and_verdict_warn(ctx):
    """Exact goodput math per band and overall; partial pass -> WARN."""
    probe = LoadMatrixProbe(calls_per_band=4, concurrency=4, bands=(4000, 12000))
    band1 = [
        _stream_result(ttft_ms=1000.0, e2e_ms=1100.0, inter_event_ms=[], tokens=1),  # good
        _stream_result(ttft_ms=6000.0, e2e_ms=6100.0, inter_event_ms=[], tokens=1),  # TTFT > 5s
        _stream_result(
            ttft_ms=1000.0, e2e_ms=3000.0, inter_event_ms=[10.0, 20.0, 30.0, 40.0], tokens=5
        ),  # tpot exactly 500 -> good
        _stream_result(ttft_ms=1000.0, e2e_ms=3000.0, inter_event_ms=[5.0, 15.0], tokens=4),  # tpot ~666.7 > 500
    ]
    band2 = [_stream_result(ttft_ms=500.0, e2e_ms=600.0, inter_event_ms=[], tokens=1) for _ in range(4)]
    ctx.stream = _stub_stream_factory(band1 + band2)
    result = await probe.run(ctx)
    lm = result.metrics["load_matrix"]
    low, high = lm["bands"]["<8K"], lm["bands"]["8-16K"]

    assert low["goodput_pct"] == 50.0  # 2/4
    assert high["goodput_pct"] == 100.0  # 4/4
    assert lm["overall"] == {"attempts": 8, "successes": 8, "good": 6, "goodput_pct": 75.0}
    assert low["success_rate_pct"] == 100.0
    assert [r["good"] for r in low["requests"]] == [True, False, True, False]
    assert [r["tpot_ms"] for r in low["requests"]] == [None, None, 500.0, 666.7]
    assert low["requests"][2]["itl_mean_ms"] == 25.0

    # Exact P50/P90 (linear interpolation) over successful requests.
    assert low["p50"]["ttft_ms"] == 1000.0
    assert low["p90"]["ttft_ms"] == 4500.0
    assert low["p50"]["tpot_ms"] == 583.3
    assert low["p90"]["tpot_ms"] == 650.0
    assert low["p50"]["itl_ms"] == 17.5
    assert low["p90"]["itl_ms"] == 35.0
    assert low["p50"]["e2e_ms"] == 3000.0
    assert low["p90"]["e2e_ms"] == 5170.0
    assert high["p50"]["ttft_ms"] == 500.0

    assert result.verdict == Verdict.WARN
    assert result.score == 75.0
    assert result.successes == 8 and result.attempts == 8
    assert any("8-16K" in note for note in result.notes)


async def test_load_goodput_all_pass_and_all_fail(ctx):
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=2, bands=(4000, 12000))
    good = _stream_result(ttft_ms=100.0, e2e_ms=200.0, inter_event_ms=[], tokens=1)
    ctx.stream = _stub_stream_factory([good])
    result = await probe.run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.metrics["load_matrix"]["overall"]["goodput_pct"] == 100.0

    bad = _stream_result(ttft_ms=9000.0, e2e_ms=9500.0, inter_event_ms=[], tokens=1)
    ctx.stream = _stub_stream_factory([bad])
    result = await probe.run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert result.metrics["load_matrix"]["overall"]["goodput_pct"] == 0.0
    assert any("goodput" in note for note in result.notes)


# ---------------------------------------------------------------------------
# LoadMatrixProbe: post-semaphore timing, concurrency bound
# ---------------------------------------------------------------------------


async def test_load_timing_starts_after_semaphore(tmp_path):
    """Queue wait is excluded from TTFT: a request that queued behind another
    still measures only server latency (~equal to the unqueued request),
    while the wall clock covers wait + processing."""
    app = D2StreamApp()
    app.first_token_delay_ms = 40
    app.chunk_gap_ms = 0
    app.chunk_count = 1
    app.completion_tokens = 3
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=1, bands=(4000,))
    started = time.perf_counter()
    result, ctx = await _run_with_app(probe, app, tmp_path)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert result.verdict == Verdict.PASS

    band = result.metrics["load_matrix"]["bands"]["<8K"]
    r0, r1 = band["requests"]
    assert r0["outcome"] == "success" and r1["outcome"] == "success"
    assert r0["queued_ms"] <= 5.0  # the first request did not queue
    assert r1["queued_ms"] >= 25.0  # the second request really queued behind the first
    # If TTFT included queue wait, r1's would be ~2x r0's (~wait + latency).
    assert abs(r1["ttft_ms"] - r0["ttft_ms"]) <= 15.0
    assert r1["ttft_ms"] <= 60.0  # just server latency, not wait + latency
    # The wall clock covers the queue wait AND the post-semaphore exchange.
    assert elapsed_ms >= r1["queued_ms"] + r1["ttft_ms"] - 5.0
    assert (lm := result.metrics["load_matrix"]["queue_wait_ms"])
    assert lm["count"] == 2
    assert lm["max"] >= 25.0


async def test_load_concurrency_bounded_by_internal_semaphore(tmp_path):
    app = D2StreamApp()
    app.first_token_delay_ms = 10
    app.chunk_count = 2
    probe = LoadMatrixProbe(calls_per_band=9, concurrency=3, bands=(4000,))
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.PASS
    assert app.request_count == 9
    assert app.max_inflight == 3  # never more than CONCURRENCY inside the endpoint


# ---------------------------------------------------------------------------
# LoadMatrixProbe: retry WARN, transport/invalid-stream FAIL, evidence
# ---------------------------------------------------------------------------


async def test_load_persistent_429_warns(tmp_path, no_sleep):
    app = D2StreamApp()
    app.force_429 = True
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=1, bands=(4000,))
    result, ctx = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.successes == 0 and result.attempts == 2
    assert result.metrics["load_matrix"]["retry_warns"] == 2
    assert any("rate-limited" in note for note in result.notes)


async def test_load_persistent_5xx_warns(tmp_path, no_sleep):
    app = D2StreamApp()
    app.force_5xx = True
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=1, bands=(4000,))
    result, ctx = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.metrics["load_matrix"]["retry_warns"] == 2
    assert any("server error" in note for note in result.notes)


async def test_load_transport_fails(tmp_path):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx = RunContext(
        endpoint="https://d2.example/v1",
        api_key=API_KEY,
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=SurfaceMap(models=["gpt-4o"], claimed_present=True),
        client=client,
        evidence=EvidenceWriter(tmp_path / "evidence", "TEST-D2"),
        budget=BudgetTracker(),
    )
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=1, bands=(4000,))
    try:
        result = await probe.run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert result.metrics["load_matrix"]["transport_failures"] == 2
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d2.load_matrix")) == 2  # one failed attempt each


async def test_load_invalid_stream_fails(tmp_path):
    app = D2StreamApp()
    app.raw_body = b"not sse at all"
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=1, bands=(4000,))
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert result.metrics["load_matrix"]["invalid_streams"] == 2
    assert any("invalid stream" in note for note in result.notes)


async def test_load_metrics_and_evidence(tmp_path):
    app = D2StreamApp()
    probe = LoadMatrixProbe(calls_per_band=2, concurrency=2, bands=(4000, 12000))
    result, ctx = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.PASS

    lm = result.metrics["load_matrix"]
    assert set(lm) == {
        "sla",
        "bands",
        "overall",
        "retry_warns",
        "transport_failures",
        "invalid_streams",
        "queue_wait_ms",
    }
    assert set(lm["bands"]) == {"<8K", "8-16K"}
    band = lm["bands"]["<8K"]
    assert set(band) == {
        "token_target",
        "counted_prompt_tokens",
        "attempts",
        "successes",
        "good",
        "goodput_pct",
        "success_rate_pct",
        "p50",
        "p90",
        "requests",
    }
    assert band["token_target"] == 4000
    assert isinstance(band["counted_prompt_tokens"], int)  # TokenizerService recount
    assert band["attempts"] == 2 and band["successes"] == 2
    assert set(band["p50"]) == {"ttft_ms", "tpot_ms", "itl_ms", "e2e_ms"}
    assert len(band["requests"]) == 2
    assert all(r["outcome"] == "success" for r in band["requests"])
    assert lm["overall"]["goodput_pct"] == 100.0

    assert {sample.kind for sample in result.samples} == {"ttft", "tpot", "e2e"}

    refs = ctx.evidence.refs_for("d2.load_matrix")
    assert len(refs) == 4  # one evidence doc per streamed exchange
    doc = json.loads((ctx.evidence.dir / Path(refs[0]).name).read_text(encoding="utf-8"))
    assert doc["response"]["status"] == 200
    assert "curl -sS -X POST" in doc["request"]["curl"]
    auth = [v for k, v in doc["request"]["headers"].items() if k.lower() == "authorization"]
    assert auth and auth[0] == "Bearer $SUPGATE_KEY"
    assert API_KEY not in json.dumps(doc)


# ---------------------------------------------------------------------------
# NeedleRecallProbe
# ---------------------------------------------------------------------------


def test_needle_class_contract():
    probe = NeedleRecallProbe()
    assert probe.id == "d2.needle_recall"
    assert probe.domain == Domain.D2
    assert probe.weight == 1.0
    assert probe.samples == 1
    assert probe.context_tokens == 30000  # build plan §10.5
    assert probe.needle_depth == 0.2
    assert probe.skip_reason(SurfaceMap()) is None
    assert isinstance(probe, Probe)


def test_needle_context_deterministic_at_20_percent_depth():
    probe = NeedleRecallProbe(context_tokens=4000)
    needle = "VERITAS-NEEDLE-ABCD1234"
    a = probe.build_context(needle)
    b = probe.build_context(needle)
    assert a == b  # deterministic: same needle -> identical context
    assert needle in a
    position = a.index(needle) / len(a)
    assert 0.15 <= position <= 0.3  # planted at ~20% depth
    approx = len(a) / probe.APPROX_CHARS_PER_TOKEN
    assert abs(approx - 4000) / 4000 < 0.1  # chars-per-token approximation


def test_needle_tokenizer_recount_injected():
    """NeedleRecallProbe accepts an injected TokenizerService for exact counts."""
    probe = NeedleRecallProbe(context_tokens=200, tokenizer=_FixedTokenizer(count_value=999))
    needle = "VERITAS-NEEDLE-ABCD1234"
    context = probe.build_context(needle)
    assert needle in context


async def test_needle_verbatim_echo_passes(tmp_path):
    app = D2NeedleApp()
    probe = NeedleRecallProbe(context_tokens=200)
    result, ctx = await _run_with_app(probe, app, tmp_path)
    assert app.request_count == 1
    assert app.last_needle.startswith("VERITAS-NEEDLE-")
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 1 and result.attempts == 1
    metrics = result.metrics["needle_recall"]
    assert metrics["response_contains_needle"] is True
    assert metrics["needle"] == app.last_needle
    assert metrics["needle_depth"] == 0.2
    # chars-per-token approximation; header/footer text skews tiny contexts,
    # so tolerate relative slack here (the exact recount is asserted below).
    assert abs(metrics["approx_context_tokens"] - 200) / 200 < 0.35
    assert isinstance(metrics["counted_context_tokens"], int)  # tiktoken recount

    refs = ctx.evidence.refs_for("d2.needle_recall")
    assert len(refs) == 1
    doc = json.loads((ctx.evidence.dir / Path(refs[0]).name).read_text(encoding="utf-8"))
    assert doc["response"]["status"] == 200
    assert "curl -sS -X POST" in doc["request"]["curl"]
    auth = [v for k, v in doc["request"]["headers"].items() if k.lower() == "authorization"]
    assert auth and auth[0] == "Bearer $SUPGATE_KEY"
    assert API_KEY not in json.dumps(doc)


async def test_needle_counted_context_tokens_uses_injected_tokenizer(tmp_path):
    app = D2NeedleApp()
    probe = NeedleRecallProbe(context_tokens=200, tokenizer=_FixedTokenizer(count_value=999))
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.PASS
    assert result.metrics["needle_recall"]["counted_context_tokens"] == 999


async def test_needle_altered_on_200_fails(tmp_path):
    app = D2NeedleApp()
    app.mode = "altered"
    probe = NeedleRecallProbe(context_tokens=200)
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert result.successes == 0 and result.attempts == 1
    assert result.metrics["needle_recall"]["response_contains_needle"] is False
    assert any("missing or altered" in note for note in result.notes)


async def test_needle_missing_on_200_fails(tmp_path):
    app = D2NeedleApp()
    app.mode = "missing"
    probe = NeedleRecallProbe(context_tokens=200)
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.FAIL
    assert result.metrics["needle_recall"]["response_contains_needle"] is False
    assert any("missing or altered" in note for note in result.notes)


async def test_needle_context_limit_4xx_warns(tmp_path):
    app = D2NeedleApp()
    app.status = 400  # context_length_exceeded error body by default
    probe = NeedleRecallProbe(context_tokens=200)
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert result.metrics["needle_recall"]["status"] == 400
    assert any("clean refusal" in note for note in result.notes)
    assert any("context" in note for note in result.notes)


async def test_needle_non_contract_4xx_fails(tmp_path):
    app = D2NeedleApp()
    app.status = 400
    app.error = {"message": "bad request", "type": "invalid_request_error", "code": None}
    probe = NeedleRecallProbe(context_tokens=200)
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.FAIL
    assert any("unexpected status 400" in note for note in result.notes)


async def test_needle_persistent_429_warns(tmp_path, no_sleep):
    app = D2NeedleApp()
    app.force_429 = True
    probe = NeedleRecallProbe(context_tokens=200)
    result, _ = await _run_with_app(probe, app, tmp_path)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)


async def test_needle_transport_fails(tmp_path):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=CrashTransport(), timeout=10)
    ctx = RunContext(
        endpoint="https://d2.example/v1",
        api_key=API_KEY,
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=SurfaceMap(models=["gpt-4o"], claimed_present=True),
        client=client,
        evidence=EvidenceWriter(tmp_path / "evidence", "TEST-D2"),
        budget=BudgetTracker(),
    )
    probe = NeedleRecallProbe(context_tokens=200)
    try:
        result = await probe.run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d2.needle_recall")) == 1  # failed attempt saved
