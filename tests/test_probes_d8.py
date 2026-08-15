"""U2 D8 GPT capability suite (docs/05 weekend plan U2; build plan §10.6).

Offline and bounded: every probe runs against the purpose-built
:class:`D8CapabilityServer` ASGI app defined in this file — no shared
``fake_server`` edits and no live calls. Covers all six tool modes
independently, structured-strict pass/fail, reasoning pass/skip/bad
accounting, cutoff pass/mismatch/no-baseline, caching pass/warn/fail,
retry WARN, transport FAIL, redacted evidence/curl, and the class
id/weight/samples contract.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from supgate.baselines import BaselineRecord
from supgate.evidence import EvidenceWriter
from supgate.models import BudgetTracker, Domain, StreamedEvent, StreamResult, SurfaceMap, Verdict
from supgate.probes.base import RateLimitError, RunContext
from supgate.probes.d8_capability import (
    _CUTOFF_FACTS,
    CutoffBatteryProbe,
    PromptCachingProbe,
    ReasoningProbe,
    StructuredStrictProbe,
    ToolAutoProbe,
    ToolForcedProbe,
    ToolMultiturnProbe,
    ToolParallelProbe,
    ToolRequiredProbe,
    ToolStreamProbe,
)

# Question text -> battery fact id, so the fixture can answer per fact.
_FACTS_BY_QUESTION = {question: fact_id for fact_id, question in _CUTOFF_FACTS}


class D8CapabilityServer:
    """Purpose-built OpenAI-compatible ASGI app for the D8 suite.

    Behaviors are mutable per test: tool mode fixtures, clean-4xx capability
    rejection, 429/5xx, structured-output fixtures, reasoning accounting,
    cutoff answers, and prompt caching (cached_tokens sequences plus
    per-call TTFT delays). Every request is logged.
    """

    valid_key = "sk-test-valid-key-0000000000"
    models = ["gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-3.5-turbo", "text-davinci-003"]

    def __init__(self) -> None:
        self.tool_behavior = "good"  # good | stripped | bad_city | bad_args_json | single_call
        self.tool_4xx: int | None = None
        self.stream_behavior = "deltas"  # deltas | content_only | parallel_deltas
        self.multiturn_final = "good"  # good | empty
        self.multiturn_turn2_status: int | None = None  # force turn-2 status (429/5xx)
        self.force_429 = False
        self.force_5xx = False
        self.reject_unknown_keys = True
        self.structured_behavior = "good"  # good | extra_key | schema_extra | wrong_type | invalid_json
        self.structured_4xx: int | None = None
        self.structured_4xx_by_kind: dict[str, int] = {}  # response_format type -> status
        self.reasoning_behavior = "good"  # good | wrong_answer | bad_accounting | missing_reasoning
        self.cutoff_answers: dict[str, bool] = {}
        self.cutoff_unparseable: set[str] = set()
        self.cutoff_status: dict[str, int] = {}  # fact_id -> non-200 status (incomplete observations)
        # pass | regress_cache | regress_ttft | missing_fields | contradictory | no_usage
        # | drop_ttft | no_drop
        self.caching_mode = "pass"
        self.requests_log: list[dict[str, Any]] = []
        self._cache_counts: dict[str, int] = {}

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            return
        body = await _read_body(receive)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        self.requests_log.append(
            {"method": scope["method"], "path": scope["path"], "headers": headers, "body": body}
        )
        if scope["method"] == "POST" and scope["path"] == "/v1/chat/completions":
            return await self._handle_chat(send, body, headers)
        await _respond(send, 404, {"error": {"message": "Not found"}})

    async def _handle_chat(self, send, body: bytes, headers: dict[str, str]) -> None:
        auth = headers.get("authorization", "")
        if self.reject_unknown_keys and auth != f"Bearer {self.valid_key}":
            return await _respond(send, 401, {"error": {"message": "bad key"}})
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return await _respond(send, 400, {"error": {"message": "bad json"}})
        if self.force_429:
            return await _respond(send, 429, {"error": {"message": "Rate limit exceeded"}})
        if self.force_5xx:
            return await _respond(send, 500, {"error": {"message": "boom"}})
        if payload.get("tools") and self.tool_4xx is not None:
            return await _respond(send, self.tool_4xx, {"error": {"message": "tools unsupported"}})
        response_format = payload.get("response_format") or {}
        if response_format and self.structured_4xx is not None:
            return await _respond(
                send, self.structured_4xx, {"error": {"message": "structured output unsupported"}}
            )
        if response_format:
            kind = response_format.get("type")
            if kind in self.structured_4xx_by_kind:
                return await _respond(
                    send, self.structured_4xx_by_kind[kind],
                    {"error": {"message": "structured output unsupported"}},
                )
        if payload.get("stream"):
            return await self._handle_stream(send, payload)
        last = payload["messages"][-1]
        content = last.get("content") if isinstance(last.get("content"), str) else ""
        if payload.get("tools"):
            if last.get("role") == "tool":
                if self.multiturn_turn2_status is not None:
                    return await _respond(
                        send, self.multiturn_turn2_status, {"error": {"message": "turn 2 failure"}}
                    )
                return await _respond(send, 200, self._multiturn_final(payload))
            return await _respond(send, 200, self._tool_response(payload))
        if response_format.get("type") in ("json_object", "json_schema"):
            return await _respond(send, 200, self._structured_response(payload))
        if "Start with 7" in content:
            return await _respond(send, 200, self._reasoning_response(payload))
        if "yes' or 'no'" in content:
            question = content.split(" Reply with exactly 'yes' or 'no'.")[0]
            fact_id = _FACTS_BY_QUESTION.get(question)
            if fact_id is not None and fact_id in self.cutoff_status:
                return await _respond(
                    send, self.cutoff_status[fact_id], {"error": {"message": "cutoff fact failed"}}
                )
            return await _respond(send, 200, self._cutoff_response(payload))
        await _respond(send, 200, self._completion("This is a fake completion reply."))

    # ---------- non-stream responses ----------

    def _completion(self, content: str, message: dict[str, Any] | None = None) -> dict[str, Any]:
        message = message if message is not None else {"role": "assistant", "content": content}
        return {
            "id": "chatcmpl-d8",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def _tool_call(self, city: str) -> dict[str, Any]:
        return {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": city})},
        }

    def _tool_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.tool_behavior == "stripped":
            return self._completion("I will not call any tool.")
        prompt = payload["messages"][-1].get("content") or ""
        calls = [self._tool_call(city) for city in ("Jakarta", "Tokyo") if city in prompt]
        if self.tool_behavior == "single_call":
            calls = calls[:1]
        elif self.tool_behavior == "bad_city":
            calls = [self._tool_call("Sydney")]
        elif self.tool_behavior == "bad_args_json":
            calls = [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{not json"},
                }
            ]
        message = {"role": "assistant", "content": None, "tool_calls": calls}
        return {
            "id": "chatcmpl-d8",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def _multiturn_final(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = (
            "" if self.multiturn_final == "empty" else "The weather in Jakarta is 32 degrees and sunny."
        )
        return self._completion(content)

    def _structured_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = (payload.get("response_format") or {}).get("type")
        if kind == "json_object":
            table = {
                "good": '{"name": "supgate", "value": 42}',
                "extra_key": '{"name": "supgate", "value": 42, "extra": 1}',
                "wrong_type": '{"name": "supgate", "value": "forty two"}',
                "invalid_json": '{"name": "supgate",',
            }
        else:
            table = {
                "good": '{"report": {"title": "ok", "rating": 4, "ok": true}, "tags": ["a"]}',
                "extra_key": '{"report": {"title": "ok", "rating": 4, "ok": true}, "tags": ["a"], "extra": 1}',
                "schema_extra": '{"report": {"title": "ok", "rating": 4, "ok": true, "extra": 1}, "tags": ["a"]}',
                "wrong_type": '{"report": {"title": "ok", "rating": "4", "ok": true}, "tags": ["a"]}',
                "invalid_json": '{"report": {"title": "ok",',
            }
        return self._completion(table[self.structured_behavior])

    def _reasoning_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.reasoning_behavior == "wrong_answer":
            content, reasoning = "42", 10
        elif self.reasoning_behavior == "bad_accounting":
            content, reasoning = "6", 200
        elif self.reasoning_behavior == "missing_reasoning":
            content, reasoning = "6", None
        else:
            content, reasoning = "6", 10
        completion = 12
        usage: dict[str, Any] = {
            "prompt_tokens": 40, "completion_tokens": completion, "total_tokens": 52,
        }
        if reasoning is not None:
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
        return {
            "id": "chatcmpl-d8",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": usage,
        }

    def _cutoff_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = payload["messages"][-1].get("content") or ""
        question = text.split(" Reply with exactly 'yes' or 'no'.")[0]
        fact_id = _FACTS_BY_QUESTION.get(question)
        if fact_id is None:
            content = "unknown"
        elif fact_id in self.cutoff_unparseable:
            content = "maybe"
        elif fact_id in self.cutoff_answers:
            content = "yes" if self.cutoff_answers[fact_id] else "no"
        else:
            raise AssertionError(f"no cutoff answer configured for {fact_id}")
        return self._completion(content)

    # ---------- streaming responses ----------

    async def _handle_stream(self, send, payload: dict[str, Any]) -> None:
        headers = [(b"content-type", b"text/event-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        if payload.get("tools"):
            chunks = self._tool_stream_chunks()
        else:
            chunks, delay = self._caching_stream(payload)
            await asyncio.sleep(delay)
        for chunk in chunks:
            raw = b"data: [DONE]\n\n" if chunk is None else f"data: {json.dumps(chunk)}\n\n".encode()
            await send({"type": "http.response.body", "body": raw, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    def _tool_stream_chunks(self) -> list[dict[str, Any] | None]:
        if self.stream_behavior == "parallel_deltas":
            # Two tool indices interleaved across chunks; index 1's argument
            # arrives SPLIT around index 0's, so reassembly must accumulate
            # per index independent of arrival order.
            return [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {"index": 0, "id": "call_abc", "type": "function",
                                     "function": {"name": "get_weather", "arguments": ""}},
                                    {"index": 1, "id": "call_def", "type": "function",
                                     "function": {"name": "get_weather", "arguments": ""}},
                                ],
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 1, "function": {"arguments": '{"city": "Jakar'}},
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '{"city": "Jakarta"}'}},
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 1, "function": {"arguments": 'ta"}'}},
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                None,
            ]
        if self.stream_behavior == "content_only":
            return [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "The weather is fine."}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                None,
            ]
        return [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {"index": 0, "id": "call_abc", "type": "function",
                                 "function": {"name": "get_weather", "arguments": ""}}
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": "Jakarta"}'}}]},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            None,
        ]

    def _caching_stream(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any] | None], float]:
        key = json.dumps(payload.get("messages", []))
        n = self._cache_counts.get(key, 0)
        self._cache_counts[key] = n + 1
        prompt = 1200
        if self.caching_mode == "regress_cache":
            cached = 0 if n == 2 else (prompt if n > 0 else 0)
        elif self.caching_mode == "contradictory":
            cached = prompt + 100
        else:
            cached = prompt if n > 0 else 0
        if self.caching_mode == "regress_ttft":
            delay = (0.005, 1.0, 0.005)[n]
        elif self.caching_mode == "drop_ttft":
            # Cold call dominates; repeats are separated by >100ms so event-loop
            # scheduling noise (~45ms worst observed) can never flip the strict
            # ordering assertions (loaded-machine flake, fixed 2026-08-15).
            delay = (0.6, 0.15, 0.03)[n]
        elif self.caching_mode == "no_drop":
            # repeat delays nominally equal-to/larger-than the cold call —
            # clearly NOT faster even under measurement jitter
            delay = (0.25, 0.4, 0.4)[n]
        elif self.caching_mode == "pass":
            delay = (0.25, 0.05, 0.03)[n]
        else:
            delay = 0.02
        chunks: list[dict[str, Any] | None] = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "4"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if self.caching_mode != "no_usage":
            usage: dict[str, Any] = {
                "prompt_tokens": prompt, "completion_tokens": 2, "total_tokens": prompt + 2,
            }
            if self.caching_mode != "missing_fields":
                usage["prompt_tokens_details"] = {
                    "cached_tokens": cached, "text_tokens": prompt - cached,
                }
            chunks.append({"choices": [], "usage": usage})
        chunks.append(None)
        return chunks, delay


async def _read_body(receive) -> bytes:  # type: ignore[no-untyped-def]
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def _respond(send, status: int, body: Any) -> None:  # type: ignore[no-untyped-def]
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": data})


@pytest.fixture
def d8_server() -> D8CapabilityServer:
    return D8CapabilityServer()


@pytest.fixture
def ctx(d8_server: D8CapabilityServer, tmp_path: Path) -> RunContext:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=d8_server), timeout=10)
    return RunContext(
        endpoint="https://fake.example/v1",
        api_key=d8_server.valid_key,
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=SurfaceMap(models=list(d8_server.models), claimed_present=True),
        client=client,
        evidence=EvidenceWriter(tmp_path / "evidence", "TEST-RUN"),
        budget=BudgetTracker(),
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("supgate.probes.base.asyncio.sleep", _no_sleep)


def _posts(server: D8CapabilityServer) -> list[dict[str, Any]]:
    return [e for e in server.requests_log if e["method"] == "POST"]


# ---------- class contract ----------

_EXPECTED_PROBES: dict[str, tuple[type, int]] = {
    "d8.tools.auto": (ToolAutoProbe, 1),
    "d8.tools.forced": (ToolForcedProbe, 1),
    "d8.tools.required": (ToolRequiredProbe, 1),
    "d8.tools.parallel": (ToolParallelProbe, 1),
    "d8.tools.multiturn": (ToolMultiturnProbe, 1),
    "d8.tools.stream": (ToolStreamProbe, 1),
    "d8.structured_strict": (StructuredStrictProbe, 2),
    "d8.reasoning": (ReasoningProbe, 1),
    "d8.cutoff_battery": (CutoffBatteryProbe, 10),
    "d8.prompt_caching": (PromptCachingProbe, 3),
}


def test_d8_probe_classes_contract():
    assert set(_EXPECTED_PROBES) == {
        "d8.tools.auto", "d8.tools.forced", "d8.tools.required",
        "d8.tools.parallel", "d8.tools.multiturn", "d8.tools.stream",
        "d8.structured_strict", "d8.reasoning", "d8.cutoff_battery",
        "d8.prompt_caching",
    }
    for probe_id, (cls, samples) in _EXPECTED_PROBES.items():
        probe = cls()
        assert probe.id == probe_id
        assert probe.domain == Domain.D8
        assert probe.weight == 1.0
        assert probe.samples == samples
        assert probe.skip_reason(SurfaceMap()) is None


# ---------- d8.tools.auto ----------


async def test_tool_auto_passes(ctx, d8_server):
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 1
    metrics = result.metrics["tool_call"]
    assert metrics["mode"] == "auto"
    assert metrics["calls"] == 1
    assert metrics["names"] == ["get_weather"]
    assert metrics["cities"] == ["Jakarta"]
    assert metrics["claiming"] is True

    posts = _posts(d8_server)
    assert len(posts) == 1
    payload = json.loads(posts[0]["body"])
    assert payload["model"] == "gpt-4o"
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "get_weather"
    assert payload["tools"][0]["function"]["parameters"]["required"] == ["city"]
    assert payload["max_tokens"] == 64

    assert len(ctx.evidence.refs_for("d8.tools.auto")) == 1
    curl = ctx.evidence.curl_for("d8.tools.auto")
    assert curl is not None and "$SUPGATE_KEY" in curl
    assert d8_server.valid_key not in curl
    ref = ctx.evidence.refs_for("d8.tools.auto")[0]
    doc = json.loads((ctx.evidence.dir / Path(ref).name).read_text(encoding="utf-8"))
    assert d8_server.valid_key not in json.dumps(doc)
    assert doc["request"]["headers"]["Authorization"] == "Bearer $SUPGATE_KEY"


async def test_tool_auto_silent_stripping_fails(ctx, d8_server):
    d8_server.tool_behavior = "stripped"
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("silent stripping" in note for note in result.notes)
    assert result.metrics["tool_call"]["calls"] == 0


async def test_tool_auto_wrong_city_fails(ctx, d8_server):
    d8_server.tool_behavior = "bad_city"
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("city" in note and "Jakarta" in note for note in result.notes)


async def test_tool_auto_bad_arguments_json_fails(ctx, d8_server):
    d8_server.tool_behavior = "bad_args_json"
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("not valid JSON" in note for note in result.notes)


# ---------- d8.tools.forced / required / parallel ----------


async def test_tool_forced_passes(ctx, d8_server):
    result = await ToolForcedProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    payload = json.loads(_posts(d8_server)[0]["body"])
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    assert result.metrics["tool_call"]["mode"] == "forced"


async def test_tool_required_passes(ctx, d8_server):
    result = await ToolRequiredProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    payload = json.loads(_posts(d8_server)[0]["body"])
    assert payload["tool_choice"] == "required"


async def test_tool_parallel_passes(ctx, d8_server):
    result = await ToolParallelProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    metrics = result.metrics["tool_call"]
    assert metrics["calls"] == 2
    assert metrics["cities"] == ["Jakarta", "Tokyo"]
    assert len(ctx.evidence.refs_for("d8.tools.parallel")) == 1
    # both calls arrive in one turn/response
    posts = _posts(d8_server)
    assert len(posts) == 1
    response = posts[0]
    assert response["method"] == "POST"


async def test_tool_parallel_single_call_fails(ctx, d8_server):
    d8_server.tool_behavior = "single_call"
    result = await ToolParallelProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("expected 2 parallel" in note for note in result.notes)


async def test_tool_parallel_bad_city_arguments_fails(ctx, d8_server):
    d8_server.tool_behavior = "bad_city"
    result = await ToolParallelProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("expected 2 parallel" in note for note in result.notes)


# ---------- d8.tools.multiturn ----------


async def test_tool_multiturn_passes(ctx, d8_server):
    result = await ToolMultiturnProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["tool_call"]
    assert metrics["final_referenced"] is True
    assert "32" in metrics["final_text"]

    posts = _posts(d8_server)
    assert len(posts) == 2
    payload1 = json.loads(posts[0]["body"])
    payload2 = json.loads(posts[1]["body"])
    assert payload1["messages"][-1]["content"] == (
        "Use the get_weather tool to get the current weather for Jakarta, then report it."
    )
    messages = payload2["messages"]
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["tool_calls"][0]["id"] == "call_abc"
    assert messages[-2]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_abc"
    assert "temperature" in messages[-1]["content"]
    assert len(ctx.evidence.refs_for("d8.tools.multiturn")) == 2


async def test_tool_multiturn_final_answer_missing_fails(ctx, d8_server):
    d8_server.multiturn_final = "empty"
    result = await ToolMultiturnProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("did not reference the tool result" in note for note in result.notes)
    assert result.metrics["tool_call"]["final_referenced"] is False


async def test_tool_multiturn_stripped_turn1_fails(ctx, d8_server):
    d8_server.tool_behavior = "stripped"
    result = await ToolMultiturnProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("silent stripping" in note for note in result.notes)
    assert len(_posts(d8_server)) == 1  # never got to the tool-result turn


async def test_tool_multiturn_turn2_429_warns(ctx, d8_server, no_sleep):
    d8_server.multiturn_turn2_status = 429
    result = await ToolMultiturnProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)
    assert len(_posts(d8_server)) == 3  # turn 1 + turn 2 attempt + turn 2 retry


async def test_tool_multiturn_turn2_5xx_warns(ctx, d8_server, no_sleep):
    d8_server.multiturn_turn2_status = 500
    result = await ToolMultiturnProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


async def test_tool_multiturn_turn2_transport_fails(ctx, d8_server):
    client = httpx.AsyncClient(transport=_Turn2CrashTransport(d8_server), timeout=10)
    ctx.client = client
    try:
        result = await ToolMultiturnProbe().run(ctx)
    finally:
        await client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d8.tools.multiturn")) == 2  # turn 1 + failed turn 2


# ---------- d8.tools.stream ----------


async def test_tool_stream_passes(ctx, d8_server):
    result = await ToolStreamProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["tool_call"]
    assert metrics["ok"] is True
    reassembled = metrics["reassembled"]
    assert len(reassembled) == 1
    assert reassembled[0]["name"] == "get_weather"
    assert reassembled[0]["id"] == "call_abc"
    assert reassembled[0]["type"] == "function"
    assert reassembled[0]["arguments"] == '{"city": "Jakarta"}'
    assert len(ctx.evidence.refs_for("d8.tools.stream")) == 1
    payload = json.loads(_posts(d8_server)[0]["body"])
    assert payload["stream"] is True
    assert payload["tool_choice"] == "auto"


async def test_tool_stream_no_deltas_fails(ctx, d8_server):
    d8_server.stream_behavior = "content_only"
    result = await ToolStreamProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("no tool_call deltas" in note for note in result.notes)


async def test_tool_stream_parallel_deltas_passes(ctx, d8_server):
    d8_server.stream_behavior = "parallel_deltas"
    result = await ToolStreamProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    reassembled = result.metrics["tool_call"]["reassembled"]
    assert len(reassembled) == 2
    # first-seen order, with each index's argument accumulated independently
    assert [entry["id"] for entry in reassembled] == ["call_abc", "call_def"]
    assert [entry["name"] for entry in reassembled] == ["get_weather", "get_weather"]
    assert [entry["type"] for entry in reassembled] == ["function", "function"]
    assert [entry["arguments"] for entry in reassembled] == ['{"city": "Jakarta"}'] * 2
    assert result.metrics["tool_call"]["ok"] is True
    assert len(ctx.evidence.refs_for("d8.tools.stream")) == 1


# ---------- tool verdict routing: 4xx / 429 / 5xx / transport ----------


async def test_tool_4xx_claiming_family_fails(ctx, d8_server):
    d8_server.tool_4xx = 400
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("claims tool" in note for note in result.notes)


async def test_tool_4xx_not_claiming_family_skips(ctx, d8_server):
    d8_server.tool_4xx = 404
    ctx.model = "mystery-model"
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert result.score == 0.0
    assert any("does not claim" in note for note in result.notes)


async def test_tool_4xx_not_claiming_but_baseline_claims_fails(ctx, d8_server):
    d8_server.tool_4xx = 422
    ctx.model = "mystery-model"
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"tools": {"supported": True}},
    )
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("claims tool" in note for note in result.notes)


async def test_tool_stream_4xx_claiming_family_fails(ctx, d8_server):
    d8_server.tool_4xx = 400
    result = await ToolStreamProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("claims tool" in note for note in result.notes)


@pytest.mark.parametrize(
    "probe_cls,probe_id",
    [(ToolAutoProbe, "d8.tools.auto"), (ToolStreamProbe, "d8.tools.stream")],
)
async def test_tool_persistent_429_warns(ctx, d8_server, no_sleep, probe_cls, probe_id):
    d8_server.force_429 = True
    result = await probe_cls().run(ctx)
    assert result.verdict == Verdict.WARN
    assert result.score == 50.0
    assert any("rate-limited" in note for note in result.notes)
    # the non-stream path saves evidence per retry attempt; ctx.stream saves
    # one evidence doc for the whole (retried) exchange
    expected_refs = 2 if probe_id == "d8.tools.auto" else 1
    assert len(ctx.evidence.refs_for(probe_id)) == expected_refs


@pytest.mark.parametrize("probe_cls", [ToolAutoProbe, ToolStreamProbe])
async def test_tool_persistent_5xx_warns(ctx, d8_server, no_sleep, probe_cls):
    d8_server.force_5xx = True
    result = await probe_cls().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("server error" in note for note in result.notes)


class _CrashTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused", request=request)


class _Turn2CrashTransport(httpx.AsyncBaseTransport):
    """Serves the first request via the ASGI app, then drops the connection:
    turn 1 succeeds, turn 2 raises a transport error (never retried)."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self._inner = httpx.ASGITransport(app=app)
        self._calls = 0

    async def handle_async_request(self, request):
        self._calls += 1
        if self._calls > 1:
            raise httpx.ConnectError("connection refused on turn 2", request=request)
        return await self._inner.handle_async_request(request)


async def test_tool_auto_transport_error_fails(ctx):
    crash_client = httpx.AsyncClient(transport=_CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await ToolAutoProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d8.tools.auto")) == 1


async def test_tool_stream_transport_error_fails(ctx):
    crash_client = httpx.AsyncClient(transport=_CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await ToolStreamProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d8.tools.stream")) == 1


# ---------- gpt-5* claim tables ----------


async def test_gpt5_claims_tool_calling(ctx, d8_server):
    ctx.model = "gpt-5.2"
    d8_server.tool_4xx = 400
    result = await ToolAutoProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("claims tool" in note for note in result.notes)
    assert result.metrics["tool_call"]["claiming"] is True


async def test_gpt5_claims_structured_output(ctx, d8_server):
    ctx.model = "gpt-5.2"
    d8_server.structured_4xx = 400
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("claims structured output" in note for note in result.notes)
    assert result.metrics["structured"]["claimed"] is True


# ---------- d8.structured_strict ----------


async def test_structured_strict_passes(ctx, d8_server):
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 2
    metrics = result.metrics["structured"]
    assert metrics["claimed"] is True
    assert [case["ok"] for case in metrics["cases"]] == [True, True]
    assert len(ctx.evidence.refs_for("d8.structured_strict")) == 2

    posts = _posts(d8_server)
    assert len(posts) == 2
    rf1 = json.loads(posts[0]["body"])["response_format"]
    assert rf1 == {"type": "json_object"}
    rf2 = json.loads(posts[1]["body"])["response_format"]
    assert rf2["type"] == "json_schema"
    assert rf2["json_schema"]["name"] == "quality_report"
    assert rf2["json_schema"]["strict"] is True
    assert rf2["json_schema"]["schema"]["additionalProperties"] is False


async def test_structured_json_object_extra_key_fails(ctx, d8_server):
    d8_server.structured_behavior = "extra_key"
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("unexpected key" in note for note in result.notes)
    cases = result.metrics["structured"]["cases"]
    # the extra top-level key breaks json_object AND the strict schema case
    assert [case["ok"] for case in cases] == [False, False]


async def test_structured_json_object_wrong_type_fails(ctx, d8_server):
    d8_server.structured_behavior = "wrong_type"
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("expected integer" in note for note in result.notes)


async def test_structured_json_schema_extra_property_fails(ctx, d8_server):
    d8_server.structured_behavior = "schema_extra"
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("additionalProperties=false" in note for note in result.notes)


async def test_structured_invalid_json_fails(ctx, d8_server):
    d8_server.structured_behavior = "invalid_json"
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("not valid JSON" in note for note in result.notes)


async def test_structured_4xx_claiming_family_fails(ctx, d8_server):
    d8_server.structured_4xx = 400
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("claims structured output" in note for note in result.notes)


async def test_structured_4xx_not_claiming_family_skips(ctx, d8_server):
    d8_server.structured_4xx = 422
    ctx.model = "gpt-3.5-turbo"
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert any("does not claim" in note for note in result.notes)


async def test_structured_invalid_output_fails_even_when_other_case_skipped(ctx, d8_server):
    # json_object returns a 200 with invalid JSON (hard failure) while the
    # json_schema case is cleanly skipped — the skip must not mask the FAIL.
    ctx.model = "gpt-3.5-turbo"  # does not claim structured output
    d8_server.structured_behavior = "invalid_json"
    d8_server.structured_4xx_by_kind = {"json_schema": 422}
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    assert any("not valid JSON" in note for note in result.notes)
    assert any("json_schema" in note and "422" in note and "does not claim" in note for note in result.notes)
    cases = result.metrics["structured"]["cases"]
    assert [case["ok"] for case in cases] == [False]


async def test_structured_skips_only_when_no_case_hard_failed(ctx, d8_server):
    # one case passes, the other is cleanly skipped: no hard failure -> SKIP.
    ctx.model = "gpt-3.5-turbo"
    d8_server.structured_4xx_by_kind = {"json_schema": 422}
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert any("does not claim" in note for note in result.notes)
    assert [case["ok"] for case in result.metrics["structured"]["cases"]] == [True]


async def test_structured_persistent_429_warns(ctx, d8_server, no_sleep):
    d8_server.force_429 = True
    result = await StructuredStrictProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d8.structured_strict")) == 4


async def test_structured_transport_error_fails(ctx):
    crash_client = httpx.AsyncClient(transport=_CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await StructuredStrictProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)


async def test_structured_metrics_serialize(ctx):
    result = await StructuredStrictProbe().run(ctx)
    dumped = result.model_dump()
    assert dumped["metrics"]["structured"]["cases"][0]["name"] == "json_object"


# ---------- d8.reasoning ----------


async def test_reasoning_passes_on_reasoning_family(ctx, d8_server):
    ctx.model = "o3-mini"
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["reasoning"]
    assert metrics["answer"] == 6
    assert metrics["expected"] == 6
    assert metrics["answer_ok"] is True
    assert metrics["reasoning_tokens"] == 10
    assert metrics["completion_tokens"] == 12
    assert metrics["accounting_ok"] is True

    payload = json.loads(_posts(d8_server)[0]["body"])
    assert payload["model"] == "o3-mini"
    assert payload["reasoning_effort"] == "low"
    assert len(ctx.evidence.refs_for("d8.reasoning")) == 1


async def test_reasoning_skips_non_reasoning_family(ctx, d8_server):
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert result.score == 0.0
    assert any("reasoning family not claimed" in note for note in result.notes)
    assert _posts(d8_server) == []  # no request made


async def test_reasoning_absent_reasoning_skips_when_baseline_denies(ctx, d8_server):
    # absent-reasoning SKIP regression: a baseline that explicitly denies
    # reasoning must SKIP with zero attempts and no requests — the probe
    # never FAILs/WARNs a family that legitimately does not claim reasoning
    # (no contradiction with the D4 usage-schema table).
    ctx.model = "mystery-model"
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"reasoning": {"supported": False}},
    )
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert result.attempts == 0
    assert any("reasoning family not claimed" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_reasoning_gpt5_family_runs(ctx, d8_server):
    ctx.model = "gpt-5.2"
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.metrics["reasoning"]["answer_ok"] is True
    assert result.metrics["reasoning"]["accounting_ok"] is True
    payload = json.loads(_posts(d8_server)[0]["body"])
    assert payload["reasoning_effort"] == "low"


async def test_reasoning_gpt5_bad_accounting_fails(ctx, d8_server):
    ctx.model = "gpt-5.2"
    d8_server.reasoning_behavior = "bad_accounting"
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("accounting unsane" in note for note in result.notes)


async def test_reasoning_gpt5_persistent_429_warns(ctx, d8_server, no_sleep):
    ctx.model = "gpt-5.2"
    d8_server.force_429 = True
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_reasoning_runs_when_baseline_claims(ctx, d8_server):
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"reasoning": {"supported": True}},
    )
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.metrics["reasoning"]["claimed"] is True


async def test_reasoning_bad_accounting_fails(ctx, d8_server):
    ctx.model = "o3-mini"
    d8_server.reasoning_behavior = "bad_accounting"
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("accounting unsane" in note for note in result.notes)
    assert result.metrics["reasoning"]["accounting_ok"] is False


async def test_reasoning_missing_accounting_fails(ctx, d8_server):
    ctx.model = "o3-mini"
    d8_server.reasoning_behavior = "missing_reasoning"
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("accounting unsane" in note for note in result.notes)
    assert result.metrics["reasoning"]["reasoning_tokens"] is None


async def test_reasoning_wrong_answer_fails(ctx, d8_server):
    ctx.model = "o3-mini"
    d8_server.reasoning_behavior = "wrong_answer"
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("reasoning answer" in note for note in result.notes)
    assert result.metrics["reasoning"]["answer_ok"] is False


async def test_reasoning_persistent_429_warns(ctx, d8_server, no_sleep):
    ctx.model = "o3-mini"
    d8_server.force_429 = True
    result = await ReasoningProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_reasoning_transport_error_fails(ctx):
    ctx.model = "o3-mini"
    crash_client = httpx.AsyncClient(transport=_CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await ReasoningProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)


# ---------- d8.cutoff_battery ----------


def _cutoff_baseline(*, cutoff: dict[str, Any] | None = None, model: str = "") -> BaselineRecord:
    fingerprints: dict[str, Any] = {}
    if cutoff is not None:
        fingerprints["cutoff_battery"] = cutoff
    return BaselineRecord(baseline_id="BL-OPENAI-GPT-4O-0001", model=model, fingerprints=fingerprints)


def _answer_all(server: D8CapabilityServer, expected: list[bool]) -> None:
    server.cutoff_answers = {
        fact_id: flag for (fact_id, _), flag in zip(_CUTOFF_FACTS, expected, strict=False)
    }


_MATCHING = [True] * 7 + [False] * 3


async def test_cutoff_battery_passes_on_matching_pattern(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    _answer_all(d8_server, _MATCHING)
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 10
    metrics = result.metrics["cutoff"]
    assert metrics["baseline_id"] == "BL-OPENAI-GPT-4O-0001"
    assert metrics["expected"] == _MATCHING
    assert metrics["observed"] == _MATCHING
    assert metrics["mismatch_ratio"] == 0.0
    assert metrics["mismatches"] == 0
    assert len(ctx.evidence.refs_for("d8.cutoff_battery")) == 10
    assert any("matches the baseline pattern" in note for note in result.notes)

    posts = _posts(d8_server)
    assert len(posts) == 10
    payload = json.loads(posts[0]["body"])
    assert payload["max_tokens"] == 8
    assert payload["temperature"] == 0
    assert "Reply with exactly 'yes' or 'no'." in payload["messages"][0]["content"]


async def test_cutoff_battery_mild_mismatch_warns(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    flipped = list(_MATCHING)
    flipped[3] = not flipped[3]  # one divergence -> ratio 0.1
    _answer_all(d8_server, flipped)
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["cutoff"]
    assert metrics["mismatch_ratio"] == 0.1
    assert metrics["mismatches"] == 1
    assert any("WARN band" in note for note in result.notes)
    assert any("never infers" not in note for note in result.notes)  # identity never named


async def test_cutoff_battery_severe_mismatch_fails(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    flipped = [not flag for flag in _MATCHING]  # 10/10 divergence
    _answer_all(d8_server, flipped)
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["cutoff"]
    assert metrics["mismatch_ratio"] == 1.0
    assert any("capability FAIL" in note for note in result.notes)


async def test_cutoff_battery_ratio_0_3_fails(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    flipped = list(_MATCHING)
    for i in (2, 5, 8):  # 3/10 divergences -> ratio 0.3
        flipped[i] = not flipped[i]
    _answer_all(d8_server, flipped)
    result = await CutoffBatteryProbe().run(ctx)
    # a divergence past the warn band is an explicit FAIL at score 0, never
    # a partial WARN carrying a "capability FAIL" note
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["cutoff"]
    assert metrics["mismatch_ratio"] == 0.3
    assert metrics["mismatches"] == 3
    assert any("capability FAIL" in note for note in result.notes)


async def test_cutoff_battery_ratio_0_6_fails(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    flipped = list(_MATCHING)
    for i in (1, 2, 4, 5, 7, 9):  # 6/10 divergences -> ratio 0.6
        flipped[i] = not flipped[i]
    _answer_all(d8_server, flipped)
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.score == 0.0
    metrics = result.metrics["cutoff"]
    assert metrics["mismatch_ratio"] == 0.6
    assert metrics["mismatches"] == 6
    assert any("capability FAIL" in note for note in result.notes)


async def test_cutoff_battery_no_baseline_skips(ctx, d8_server):
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert any("no selected baseline" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_cutoff_battery_baseline_without_fingerprint_skips(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline()
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert any("no cutoff_battery fingerprint" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_cutoff_battery_plain_pattern_mode_passes(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"pattern": "YYYYYYYNNN"})
    _answer_all(d8_server, _MATCHING)
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    metrics = result.metrics["cutoff"]
    assert metrics["mode"] == "pattern"
    assert metrics["expected"] == _MATCHING
    assert metrics["mismatch_ratio"] == 0.0


async def test_cutoff_battery_regex_pattern_mismatch_fails(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"pattern": "N{10}"})
    _answer_all(d8_server, _MATCHING)
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert result.metrics["cutoff"]["mismatch_ratio"] == 1.0


async def test_cutoff_battery_unparseable_counts_as_mismatch(ctx, d8_server):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    _answer_all(d8_server, _MATCHING)
    d8_server.cutoff_unparseable = {"moon_landing_1969"}
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["cutoff"]
    assert metrics["mismatch_ratio"] == 0.1
    assert metrics["per_item"][1]["observed"] is None


async def test_cutoff_battery_partial_observation_flags_and_pattern_agree(ctx, d8_server):
    # moon_landing never returns 200: only the 9 observed positions count,
    # each against the expected flag at ITS OWN fact position. Flags and
    # plain-pattern modes must agree on the incomplete observation.
    _answer_all(d8_server, _MATCHING)
    d8_server.cutoff_status = {"moon_landing_1969": 400}

    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 9
    assert result.attempts == 9
    metrics = result.metrics["cutoff"]
    assert metrics["mismatch_ratio"] == 0.0
    assert metrics["mismatches"] == 0
    assert metrics["observed"] == [True, None, True, True, True, True, True, False, False, False]
    assert metrics["per_item"][1]["observed"] is None
    assert any("9 facts observed" in note for note in result.notes)

    ctx.selected_baseline = _cutoff_baseline(cutoff={"pattern": "YYYYYYYNNN"})
    pattern_result = await CutoffBatteryProbe().run(ctx)
    assert pattern_result.verdict == Verdict.PASS
    assert pattern_result.score == 100.0
    assert pattern_result.successes == 9
    pattern_metrics = pattern_result.metrics["cutoff"]
    assert pattern_metrics["mode"] == "pattern"
    assert pattern_metrics["mismatch_ratio"] == 0.0
    assert pattern_metrics["mismatches"] == 0
    assert pattern_metrics["observed"] == metrics["observed"]


async def test_cutoff_battery_persistent_429_warns(ctx, d8_server, no_sleep):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    _answer_all(d8_server, _MATCHING)
    d8_server.force_429 = True
    result = await CutoffBatteryProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_cutoff_battery_transport_error_fails(ctx):
    ctx.selected_baseline = _cutoff_baseline(cutoff={"expected": _MATCHING})
    crash_client = httpx.AsyncClient(transport=_CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await CutoffBatteryProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)


# ---------- d8.prompt_caching ----------


async def test_prompt_caching_passes(ctx, d8_server):
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    assert result.successes == 3
    metrics = result.metrics["caching"]
    per_call = metrics["per_call"]
    assert len(per_call) == 3
    assert [call["cached_tokens"] for call in per_call] == [0, 1200, 1200]
    assert all(call["prompt_tokens"] == 1200 for call in per_call)
    assert metrics["nondecreasing"] is True
    assert metrics["ttft_ok"] is True
    assert len(ctx.evidence.refs_for("d8.prompt_caching")) == 3

    posts = _posts(d8_server)
    assert len(posts) == 3
    payload = json.loads(posts[0]["body"])
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    content = payload["messages"][0]["content"]
    assert content.startswith("reference token 0")
    assert len(content.split()) >= 300
    assert any("sane and nondecreasing" in note for note in result.notes)


async def test_prompt_caching_cache_regression_warns(ctx, d8_server):
    d8_server.caching_mode = "regress_cache"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["caching"]
    assert metrics["nondecreasing"] is False
    assert any("regressed between identical calls" in note for note in result.notes)


async def test_prompt_caching_ttft_regression_warns(ctx, d8_server):
    d8_server.caching_mode = "regress_ttft"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["caching"]
    assert metrics["ttft_ok"] is False
    assert any("TTFT regressed" in note for note in result.notes)


async def test_prompt_caching_ttft_drop_passes(ctx, d8_server):
    d8_server.caching_mode = "drop_ttft"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert result.score == 100.0
    metrics = result.metrics["caching"]
    assert metrics["ttft_ok"] is True
    ttfts = [call["ttft_ms"] for call in metrics["per_call"]]
    assert ttfts[0] > ttfts[1] > ttfts[2]  # every repeat strictly faster
    assert any("TTFT dropped on every repeat" in note for note in result.notes)


async def test_prompt_caching_ttft_no_drop_warns(ctx, d8_server):
    d8_server.caching_mode = "no_drop"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    metrics = result.metrics["caching"]
    assert metrics["ttft_ok"] is False
    ttfts = [call["ttft_ms"] for call in metrics["per_call"]]
    assert ttfts[1] >= ttfts[0]  # repeat never dropped below the cold call
    assert any("TTFT regressed" in note for note in result.notes)


async def test_prompt_caching_missing_fields_fails(ctx, d8_server):
    d8_server.caching_mode = "missing_fields"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("cached_tokens absent on 200" in note for note in result.notes)


async def test_prompt_caching_hard_failure_dominates_retry_warn(ctx, monkeypatch):
    calls = 0

    async def mixed_stream(*args, **kwargs):
        nonlocal calls
        call = calls
        calls += 1
        if call == 0:
            raise RateLimitError(429, "rate-limited (429) after retry")
        usage = None if call == 1 else {
            "prompt_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 1200},
        }
        return StreamResult(
            status=200,
            events=[StreamedEvent(delta="4", arrived_ms=float(call), usage=usage)],
            ttft_ms=10.0 - call,
        )

    monkeypatch.setattr(ctx, "stream", mixed_stream)
    result = await PromptCachingProbe().run(ctx)

    assert result.verdict == Verdict.FAIL
    assert any("rate-limited" in note for note in result.notes)
    assert any("cached_tokens absent" in note for note in result.notes)


async def test_prompt_caching_transport_failure_dominates_partial_success(ctx, monkeypatch):
    calls = 0

    async def mixed_stream(*args, **kwargs):
        nonlocal calls
        call = calls
        calls += 1
        if call == 0:
            raise httpx.ConnectError("connection dropped")
        return StreamResult(
            status=200,
            events=[
                StreamedEvent(
                    delta="4",
                    arrived_ms=float(call),
                    usage={
                        "prompt_tokens": 1200,
                        "prompt_tokens_details": {"cached_tokens": 1200},
                    },
                )
            ],
            ttft_ms=10.0 - call,
        )

    monkeypatch.setattr(ctx, "stream", mixed_stream)
    result = await PromptCachingProbe().run(ctx)

    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)


async def test_prompt_caching_contradictory_fields_fails(ctx, d8_server):
    d8_server.caching_mode = "contradictory"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("out of range" in note for note in result.notes)


async def test_prompt_caching_no_usage_fails(ctx, d8_server):
    d8_server.caching_mode = "no_usage"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.FAIL
    assert any("cached_tokens absent" in note for note in result.notes)


async def test_prompt_caching_unsupported_family_warns(ctx, d8_server):
    ctx.model = "text-davinci-003"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("does not claim prompt caching" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_prompt_caching_unknown_family_skips(ctx, d8_server):
    ctx.model = "mystery-model"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert any("unknown model family" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_prompt_caching_baseline_claim_overrides_table(ctx, d8_server):
    ctx.model = "mystery-model"
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"usage_schema": {"cached_tokens": True}},
    )
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.PASS
    assert len(_posts(d8_server)) == 3


async def test_prompt_caching_baseline_override_false_warns(ctx, d8_server):
    # usage_schema override denies caching for a family the static table
    # claims: WARN before any request, exactly like a known non-caching family.
    ctx.selected_baseline = BaselineRecord(
        baseline_id="BL-OPENAI-GPT-4O-0001",
        fingerprints={"usage_schema": {"cached_tokens": False}},
    )
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("does not claim prompt caching" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_prompt_caching_gpt5_unknown_family_skips(ctx, d8_server):
    # gpt-5* claims reasoning in D8, but the D4 usage-schema table (the
    # single authority for usage-details claims) does not know the family:
    # prompt caching SKIPs instead of contradicting D4.
    ctx.model = "gpt-5.2"
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.SKIP
    assert any("unknown model family" in note for note in result.notes)
    assert _posts(d8_server) == []


async def test_prompt_caching_persistent_429_warns(ctx, d8_server, no_sleep):
    d8_server.force_429 = True
    result = await PromptCachingProbe().run(ctx)
    assert result.verdict == Verdict.WARN
    assert any("rate-limited" in note for note in result.notes)


async def test_prompt_caching_transport_error_fails(ctx):
    crash_client = httpx.AsyncClient(transport=_CrashTransport(), timeout=10)
    ctx.client = crash_client
    try:
        result = await PromptCachingProbe().run(ctx)
    finally:
        await crash_client.aclose()
    assert result.verdict == Verdict.FAIL
    assert any("transport error" in note for note in result.notes)
    assert len(ctx.evidence.refs_for("d8.prompt_caching")) == 3  # one evidence doc per call
