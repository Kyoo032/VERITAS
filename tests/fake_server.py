"""A hand-rolled OpenAI-compatible ASGI server for tests — no live calls (§11.2).

Behaviors are mutable per test: wrong key, force 429/5xx, disabled
Responses API, disabled vision, etc. Every request is logged so tests can
assert on what the client sent.
"""

from __future__ import annotations

import json
from typing import Any

_JSON_ERROR_401 = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}}
_JSON_ERROR_BAD_BODY = {"error": {"message": "Invalid JSON body", "type": "invalid_request_error", "code": "invalid_json"}}
_JSON_ERROR_UNSUPPORTED = {"error": {"message": "Unsupported parameter or content", "type": "invalid_request_error", "code": "unsupported_parameter"}}


class FakeOpenAI:
    def __init__(self) -> None:
        self.valid_key = "sk-test-valid-key-0000000000"
        self.models = ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet-20241022"]
        self.id_prefix = "chatcmpl-"
        self.responses_api_enabled = True
        self.vision_enabled = True
        self.force_429 = False
        self.force_5xx = False
        self.reject_unknown_keys = True
        self.requests_log: list[dict[str, Any]] = []

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
            return await _respond(send, 200, {"object": "list", "data": [{"id": m} for m in self.models]})

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
            return await _respond(send, 200, self._sse_body(payload), content_type="text/event-stream")

        choices = self._choices(payload, payload.get("n", 1))
        usage = self._usage(payload)
        return await _respond(
            send,
            200,
            {
                "id": f"{self.id_prefix}fake123",
                "object": "chat.completion",
                "model": payload.get("model", "gpt-4o"),
                "choices": choices,
                "usage": usage,
            },
        )

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
        if payload.get("stream_options", {}).get("include_usage"):
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
        if "json_object" in str(payload.get("response_format", {})) or "Return JSON" in content:
            return '{"name": "supgate", "value": 42}'
        if "hello" in content.lower() or "Say hello" in content:
            return "Hello! This is a fake completion reply."
        if "Count from 1 to 5" in content:
            return "1, 2, 3, 4, 5"
        return "This is a fake completion reply."

    def _tool_calls(self, payload: dict[str, Any], content: str) -> list[dict[str, Any]] | None:
        if self._has_tool_calls(payload) and payload.get("tool_choice") != "none":
            tool = payload["tools"][0]
            return [{"id": "call_fake1", "type": "function",
                     "function": {"name": tool["function"]["name"], "arguments": '{"city": "Jakarta"}'}}]
        return None

    def _has_tool_calls(self, payload: dict[str, Any]) -> bool:
        tools = payload.get("tools")
        return bool(tools and tools[0].get("type") == "function")

    def _usage(self, payload: dict[str, Any]) -> dict[str, int]:
        prompt_text = json.dumps(payload.get("messages", []))
        content = self._reply_text(payload)
        prompt = max(1, len(prompt_text) // 4)
        completion = max(1, len(content) // 4)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


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


async def _respond(send, status: int, body: Any, content_type: str = "application/json") -> None:  # type: ignore[no-untyped-def]
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode()),
                (b"x-fake-server", b"supgate-test"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})
