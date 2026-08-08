"""A hand-rolled OpenAI-compatible ASGI server for tests — no live calls (§11.2).

Behaviors are mutable per test: wrong key, force 429/5xx, disabled
Responses API, disabled vision, etc. Every request is logged so tests can
assert on what the client sent.
"""

from __future__ import annotations

import json
import re
from typing import Any

from supgate.probes.d4_billing import usage_schema_for
from supgate.tokenizers import FALLBACK_ENCODING, count, resolve_encoding

_JSON_ERROR_401 = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}}
_JSON_ERROR_BAD_BODY = {"error": {"message": "Invalid JSON body", "type": "invalid_request_error", "code": "invalid_json"}}
_JSON_ERROR_UNSUPPORTED = {"error": {"message": "Unsupported parameter or content", "type": "invalid_request_error", "code": "unsupported_parameter"}}

# d4.canary_echo §4.5: the prompt embeds exactly one VERITAS-<hex16> token.
_CANARY_RE = re.compile(r"VERITAS-[0-9a-f]{16}")

# d4.rotation §4.7: distinct (id_prefix, content) buckets to rotate through;
# ``rotation_formatting_variants`` swaps the contents for formatting variants
# of the same text (same bucket) so clustering tolerance is exercisable.
_ROTATION_CONTENTS = ("forty two", "the answer is forty two", "forty two in words")
_ROTATION_FORMATTED = ("forty two", "Forty two.", "forty two!")
_ROTATION_PREFIXES = ("chatcmpl-", "msg_", "gen-")


class FakeOpenAI:
    def __init__(self) -> None:
        self.valid_key = "sk-test-valid-key-0000000000"
        self.models = ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet-20241022"]
        self.id_prefix = "chatcmpl-"
        self.id_prefix_jitter = False
        self.jitter_prefix = "gen-"
        self.missing_id = False
        self._id_jitter_n = 0
        self.responses_api_enabled = True
        self.vision_enabled = True
        self.force_429 = False
        self.force_5xx = False
        self.reject_unknown_keys = True
        self.requests_log: list[dict[str, Any]] = []
        self.hop_headers: list[str] = []
        self.header_jitter = False
        self._jitter_n = 0
        self.model_echo: str | None = None
        self.self_report_text: str | None = None
        self.models_metadata_drift = False
        # d4.canary_echo fixture controls (§4.5): exact echo by default.
        self.template_mode = False
        self.contaminate_cross_request = False
        self.contaminate_single_pair = False
        self.canary_echo_prefix = ""
        self.canary_echo_suffix = ""
        self.canary_relaxed_pairs = 0
        self._canary_pairs: dict[str, int] = {}
        # d4.rotation fixture controls (§4.7): number of distinct
        # (id_prefix, content) buckets to rotate through per call.
        self.rotation_families = 1
        self.rotation_formatting_variants = False
        self.rotation_bad_body = False
        self._rotation_n = 0
        self._current_rotation_family = 0
        # d4 billing fixture controls (docs/06 §5.1-§5.4). Defaults are the
        # clean contract: usage always present with tiktoken-accurate counts
        # (so a recount under the same encoding sees 0% deviation) and the
        # family-appropriate details schema (cached_tokens=0 stable, no
        # reasoning fields for non-reasoning families).
        self.omit_usage_nonstream = False
        self.omit_usage_stream = False
        self.usage_without_include_usage = False
        self.bad_usage_arithmetic = False
        self.usage_offset_tokens = 0
        self.usage_offset_pct = 0.0
        self.hidden_wrapper_tokens = 0
        # usage_schema_flags keys: cached_tokens / reasoning_tokens (emit the
        # field; default follows the claimed family's schema), cached_tokens_bad
        # / reasoning_tokens_bad (contradictory overflow), caching_delta (cache
        # grows between identical calls), caching_regress (second call loses the
        # cache), cached_every_call (fully cached on every call).
        self.usage_schema_flags: dict[str, Any] = {}
        self._cache_counts: dict[str, int] = {}

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            return
        body = await _read_body(receive)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        entry = {"method": scope["method"], "path": scope["path"], "headers": headers, "body": body}
        self.requests_log.append(entry)

        if self.force_429:
            return await _respond(send, 429, {"error": {"message": "Rate limit", "type": "rate_limit_error", "code": "rate_limit_exceeded"}})
        if self.force_5xx:
            return await _respond(send, 500, {"error": {"message": "boom", "type": "server_error", "code": "internal_error"}})

        if scope["method"] == "GET" and scope["path"] == "/v1/models":
            # §4.4 fixture control: models_metadata_drift drops the first
            # catalog entry (the claimed model in the default fixture list),
            # which flips claimed_present on the p0.models SurfaceMap.
            models = self.models[1:] if self.models_metadata_drift else self.models
            return await _respond(send, 200, {"object": "list", "data": [{"id": m} for m in models]})

        if scope["method"] == "POST" and scope["path"] == "/v1/responses":
            return await self._handle_responses(send, body)

        if scope["method"] == "POST" and scope["path"] == "/v1/chat/completions":
            return await self._handle_chat(send, body, headers)

        return await _respond(send, 404, {"error": {"message": "Not found", "type": "invalid_request_error", "code": "not_found"}})

    async def _handle_responses(self, send, body: bytes) -> None:
        if not self.responses_api_enabled:
            return await _respond(send, 404, {"error": {"message": "Unsupported endpoint", "type": "invalid_request_error", "code": "unsupported_endpoint"}})
        return await _respond(
            send,
            200,
            {
                "id": "resp_fake123",
                "object": "response",
                "model": "gpt-4o",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hi there"}],
                    }
                ],
            },
        )

    async def _handle_chat(self, send, body: bytes, headers: dict[str, str]) -> None:
        auth = headers.get("authorization", "")
        if self.reject_unknown_keys and auth != f"Bearer {self.valid_key}":
            return await _respond(send, 401, _JSON_ERROR_401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return await _respond(send, 400, _JSON_ERROR_BAD_BODY)

        if not isinstance(payload, dict) or "messages" not in payload:
            return await _respond(send, 400, _JSON_ERROR_BAD_BODY)

        if any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for message in payload.get("messages", [])
            for part in (message.get("content", []) if isinstance(message.get("content"), list) else [])
        ) and not self.vision_enabled:
            return await _respond(send, 400, _JSON_ERROR_UNSUPPORTED)

        if payload.get("stream"):
            return await _respond(
                send, 200, self._sse_body(payload),
                content_type="text/event-stream", extra_headers=self._extra_headers(),
            )

        if self.rotation_bad_body:
            return await _respond(send, 200, b'{"id": "chatcmpl-rot", "choices": [')

        self._current_rotation_family = self._rotation_family()
        choices = self._choices(payload, payload.get("n", 1))
        usage = self._usage(payload)
        response = {
            "id": self._chat_id(),
            "object": "chat.completion",
            "model": payload.get("model", "gpt-4o"),
            "choices": choices,
            "usage": usage,
        }
        if self.missing_id:
            response.pop("id", None)
        if self.omit_usage_nonstream:
            response.pop("usage", None)
        return await _respond(
            send,
            200,
            response,
            extra_headers=self._extra_headers(),
        )

    def _rotation_family(self) -> int:
        """§4.7: next ``(id_prefix, content)`` bucket for this request.

        Stable at 0 when ``rotation_families <= 1``; otherwise advances once
        per request and wraps, so call n of a 2-family fixture alternates
        deterministically.
        """

        if self.rotation_families <= 1:
            return 0
        self._rotation_n += 1
        return self._rotation_n % self.rotation_families

    def _chat_id(self) -> str:
        """Chat completion id; ``id_prefix_jitter`` alternates families per call (§4.2).

        §4.7 rotation takes precedence when ``rotation_families > 1``: the id
        prefix follows the current rotation bucket (unchanged when
        ``rotation_formatting_variants`` keeps the prefix stable).
        """

        prefix = self.id_prefix
        if self.rotation_families > 1 and not self.rotation_formatting_variants:
            prefix = _ROTATION_PREFIXES[self._current_rotation_family]
        elif self.id_prefix_jitter:
            self._id_jitter_n += 1
            if self._id_jitter_n % 2 == 0:
                prefix = self.jitter_prefix
        return f"{prefix}fake123"

    def _extra_headers(self) -> list[tuple[bytes, bytes]]:
        """Fixture toggles for d4.headers_diff (§4.1): hop markers + header jitter."""

        extra: list[tuple[bytes, bytes]] = [
            (name.lower().encode(), f"fake-{name.lower()}".encode()) for name in self.hop_headers
        ]
        if self.header_jitter:
            self._jitter_n += 1
            if self._jitter_n % 2 == 0:
                extra.append((b"x-extra", f"jit-{self._jitter_n}".encode()))
        return extra

    def _sse_body(self, payload: dict[str, Any]) -> bytes:
        content = self._reply_text(payload)
        words = content.split(" ")
        chunks: list[bytes] = [
            b'data: {"id": "chatcmpl-fake123", "object": "chat.completion.chunk", '
            b'"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}\n\n'
        ]
        for word in words:
            chunk = {"id": "chatcmpl-fake123", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}
            chunks.append(f"data: {json.dumps(chunk)}\n\n".encode())
        chunks.append(
            b'data: {"id": "chatcmpl-fake123", "object": "chat.completion.chunk", '
            b'"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
        )
        include = bool(payload.get("stream_options", {}).get("include_usage"))
        if (include and not self.omit_usage_stream) or (not include and self.usage_without_include_usage):
            usage = self._usage(payload)
            chunks.append(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode())
        chunks.append(b"data: [DONE]\n\n")
        return b"".join(chunks)

    def _choices(self, payload: dict[str, Any], n: int) -> list[dict[str, Any]]:
        content, reason = self._reply(payload)
        return [
            {
                "index": i,
                "message": {"role": "assistant", "content": content, "tool_calls": self._tool_calls(payload, content)},
                "finish_reason": reason,
            }
            for i in range(n)
        ]

    def _reply(self, payload: dict[str, Any]) -> tuple[str, str]:
        content = self._reply_text(payload)
        if content.startswith("PONG-"):
            return content, "stop"
        stop = payload.get("stop")
        if stop and content.upper().find(stop[0].upper()) >= 0:
            idx = content.upper().find(stop[0].upper())
            return content[:idx], "stop"
        if payload.get("max_tokens") == 1:
            return "H", "length"
        if self._has_tool_calls(payload) and payload.get("tool_choice") != "none":
            return content, "tool_calls"
        return content, "stop"

    def _reply_text(self, payload: dict[str, Any]) -> str:
        messages = payload.get("messages", [])
        last = messages[-1] if messages else {}
        content = last.get("content", "")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    return "pixel" if self.vision_enabled else "I cannot process images."
            return "pixel"
        if "PONG-" in content:
            return "PONG-" + content.split("PONG-", 1)[1][:8]
        if self.model_echo is not None and "model identifier" in content:
            return self.model_echo
        if self.self_report_text is not None and "hosting platform or provider API" in content:
            return self.self_report_text
        if "42 in words" in content:
            variants = _ROTATION_FORMATTED if self.rotation_formatting_variants else _ROTATION_CONTENTS
            return variants[self._current_rotation_family % len(variants)]
        match = _CANARY_RE.search(content)
        if match:
            return self._canary_reply(match.group(0))
        if "json_object" in str(payload.get("response_format", {})) or "Return JSON" in content:
            return '{"name": "supgate", "value": 42}'
        if "hello" in content.lower() or "Say hello" in content:
            return "Hello! This is a fake completion reply."
        if "Count from 1 to 5" in content:
            return "1, 2, 3, 4, 5"
        return "This is a fake completion reply."

    def _canary_reply(self, canary: str) -> str:
        """d4.canary_echo fixture control (§4.5); exact echo by default.

        ``template_mode`` returns one canned reply regardless of canary;
        ``contaminate_cross_request`` leaks the other pair's canary into
        every response (both-direction contamination), while
        ``contaminate_single_pair`` leaks only into the second pair's
        responses; ``canary_relaxed_pairs`` wraps the canary with
        prefix/suffix so the echo is present but not exact. Pair identity is
        first-seen order.
        """

        if canary not in self._canary_pairs:
            self._canary_pairs[canary] = len(self._canary_pairs)
        pair = self._canary_pairs[canary]
        if self.template_mode:
            return "ok"
        other = next((c for c, p in self._canary_pairs.items() if p != pair), None)
        if other is not None and (
            self.contaminate_cross_request
            or (self.contaminate_single_pair and pair == 1)
        ):
            return f"{other} {canary}"
        if pair < self.canary_relaxed_pairs:
            return f"{self.canary_echo_prefix}{canary}{self.canary_echo_suffix}"
        return canary

    def _tool_calls(self, payload: dict[str, Any], content: str) -> list[dict[str, Any]] | None:
        if self._has_tool_calls(payload) and payload.get("tool_choice") != "none":
            tool = payload["tools"][0]
            return [{"id": "call_fake1", "type": "function",
                     "function": {"name": tool["function"]["name"], "arguments": '{"city": "Jakarta"}'}}]
        return None

    def _has_tool_calls(self, payload: dict[str, Any]) -> bool:
        tools = payload.get("tools")
        return bool(tools and tools[0].get("type") == "function")

    def _usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Usage block with tiktoken-accurate counts (docs/06 §9.6).

        Prompt/completion tokens are counted under the claimed model's
        encoding with the same ``supgate.tokenizers`` service the recount and
        wrap probes use, so the default fixture is clean under a tiktoken
        recount (0% deviation, 0-token offset). Inflation controls
        (``usage_offset_tokens`` / ``usage_offset_pct`` /
        ``hidden_wrapper_tokens``), arithmetic corruption, and the
        family-appropriate details schema are layered on top.
        """

        prompt_text = json.dumps(payload.get("messages", []))
        content = self._reply_text(payload)
        encoding = resolve_encoding(payload.get("model") or "") or FALLBACK_ENCODING
        prompt = count(prompt_text, encoding)
        completion = count(content, encoding)
        if self.usage_offset_pct:
            prompt = int(round(prompt * (1.0 + self.usage_offset_pct / 100.0)))
        prompt += self.usage_offset_tokens + self.hidden_wrapper_tokens
        usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
        usage["total_tokens"] = (
            prompt + completion + 7 if self.bad_usage_arithmetic else prompt + completion
        )
        schema = usage_schema_for(payload.get("model") or "")
        flags = self.usage_schema_flags
        if flags.get("cached_tokens", bool(schema and schema["cached_tokens"])):
            cached = self._cached_tokens(payload, prompt)
            if flags.get("cached_tokens_bad"):
                cached = prompt + 100
            usage["prompt_tokens_details"] = {"cached_tokens": cached, "text_tokens": prompt - cached}
        if flags.get("reasoning_tokens", bool(schema and schema["reasoning_tokens"])):
            reasoning = completion + 100 if flags.get("reasoning_tokens_bad") else 0
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
        return usage

    def _cached_tokens(self, payload: dict[str, Any], prompt: int) -> int:
        """cached_tokens for this request; per-message-hash call tracking so
        identical requests can exercise cache growth/regression (docs/06 §5.4)."""

        key = json.dumps(payload.get("messages", []))
        n = self._cache_counts.get(key, 0)
        self._cache_counts[key] = n + 1
        flags = self.usage_schema_flags
        if flags.get("cached_every_call"):
            return prompt
        if flags.get("caching_regress"):
            return prompt if n == 0 else 0
        if flags.get("caching_delta"):
            return prompt if n > 0 else 0
        return 0


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


async def _respond(
    send, status: int, body: Any, content_type: str = "application/json",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:  # type: ignore[no-untyped-def]
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", content_type.encode()),
        (b"x-fake-server", b"supgate-test"),
    ]
    headers.extend(extra_headers or [])
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": data})
