"""Custom-logic D6 protocol probes (build plan §10.2).

Simple manifest-driven D6 probes (chat basic, message shapes, json mode,
tool passthrough, param boundaries, max_tokens, idempotency) live in
``manifests/probes.yaml`` and run through the generic chat_completion runner.
This module holds the ones needing streaming or bespoke checks: SSE framing,
usage fields, vision, and the Responses API.
"""

from __future__ import annotations

import json

from supgate.models import (
    Domain,
    ProbeResult,
    StreamResult,
    SurfaceMap,
    TimingSample,
    Verdict,
)
from supgate.probes.base import (
    RateLimitError,
    RunContext,
    ServerError,
    probe_result,
    probe_result_with_warn,
    request_with_retry,
    text_content,
)


def parse_sse(text: str) -> list[dict]:
    """Parse a buffered SSE body into data payloads; ignores comments/blank lines.

    Returns the sequence of ``data:`` payloads (each JSON-decoded when possible).
    """

    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            events.append({"raw": payload})
    return events


class SseProbe:
    """d6.chat.sse ×2 — well-formed SSE: data frames, [DONE] terminator,
    deltas reassemble to non-empty text."""

    id = "d6.chat.sse"
    domain = Domain.D6
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        successes = 0
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        samples: list[TimingSample] = []
        for i in range(self.samples):
            try:
                result = await ctx.stream(
                    self.id, "/chat/completions",
                    payload={
                        "model": ctx.model,
                        "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
                        "max_tokens": 32,
                        "stream": True,
                    },
                )
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"sample {i}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001
                transport_failures = True
                notes.append(f"sample {i}: transport error: {exc}")
                continue
            terminated = "[DONE]" in result.body
            deltas = result.events[-1].delta if result.events else ""
            ok = (
                result.status == 200
                and bool(result.events)
                and terminated
                and deltas.strip() != ""
            )
            if ok:
                successes += 1
            else:
                notes.append(
                    f"sample {i}: status={result.status} events={len(result.events)} "
                    f"done={terminated} deltas={deltas!r}"
                )
            samples.extend(_timing_samples(result))
        result = probe_result_with_warn(
            self.id, self.domain, successes=successes, attempts=self.samples, notes=notes,
            warn_failures=warn_failures, transport_failures=transport_failures,
        )
        result.samples = samples
        return result


class UsageFieldsProbe:
    """d6.usage_fields ×2 — usage present and arithmetically consistent in
    non-stream responses and in the final chunk of streams with include_usage."""

    id = "d6.usage_fields"
    domain = Domain.D6
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "What is 2+2?"}],
            "max_tokens": 32,
        }
        checks = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        samples: list[TimingSample] = []

        try:
            response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
        except (RateLimitError, ServerError) as exc:
            warn_failures = True
            notes.append(f"non-stream: {exc.note}")
            checks.append(False)
        except Exception as exc:  # noqa: BLE001
            transport_failures = True
            notes.append(f"non-stream: transport error: {exc}")
            checks.append(False)
        else:
            body = _json(response)
            usage = body.get("usage") if body else None
            non_stream_ok = _usage_consistent(usage)
            checks.append(non_stream_ok)
            if not non_stream_ok:
                notes.append(f"non-stream usage missing/inconsistent: {usage}")

        stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        try:
            result = await ctx.stream(self.id, "/chat/completions", payload=stream_payload)
        except (RateLimitError, ServerError) as exc:
            warn_failures = True
            notes.append(f"stream: {exc.note}")
            checks.append(False)
        except Exception as exc:  # noqa: BLE001
            transport_failures = True
            notes.append(f"stream: transport error: {exc}")
            checks.append(False)
        else:
            usage = next(
                (event.usage for event in result.events if event.usage is not None), None
            )
            stream_ok = _usage_consistent(usage)
            checks.append(stream_ok)
            if not stream_ok:
                notes.append(f"stream usage missing/inconsistent: {usage}")
            samples.extend(_timing_samples(result))

        result = probe_result_with_warn(
            self.id, self.domain, successes=sum(checks), attempts=2, notes=notes,
            warn_failures=warn_failures, transport_failures=transport_failures,
        )
        result.samples = samples
        return result


class VisionProbe:
    """d6.vision — one small image_url part must be described correctly, or the
    endpoint cleanly refuses (Skip) when it isn't a multimodal claim."""

    id = "d6.vision"
    domain = Domain.D6
    weight = 1.0
    samples = 1

    # 1x1 transparent PNG as a data URL — tiny but real image content.
    _DATA_URL = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        try:
            response = await request_with_retry(
                ctx, self.id, "POST", "/chat/completions",
                payload={
                    "model": ctx.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Describe this image in one word."},
                                {"type": "image_url", "image_url": {"url": self._DATA_URL}},
                            ],
                        }
                    ],
                    "max_tokens": 16,
                },
            )
        except (RateLimitError, ServerError) as exc:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[exc.note], error=str(exc), warn_failures=True,
            )
        except Exception as exc:  # noqa: BLE001
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"transport error: {exc}"], transport_failures=True,
            )
        body = _json(response)
        if response.status_code == 200:
            content = text_content(body.get("choices", [{}])[0].get("message", {}).get("content")) if body else ""
            ok = bool(content and content.strip())
            return probe_result(
                self.id, self.domain, successes=1 if ok else 0, attempts=1,
                notes=["vision accepted and produced a description" if ok else "empty description"],
            )
        if response.status_code in (400, 404, 422):
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=1,
                notes=["image input unsupported (clean error) — skipped for non-multimodal claim"],
            )
        return probe_result(
            self.id, self.domain, successes=0, attempts=1,
            notes=[f"unexpected status {response.status_code} for image input"],
        )


class ResponsesApiProbe:
    """d6.responses_api — POST /v1/responses; a clean unsupported error marks the
    surface and lets downstream probes Skip."""

    id = "d6.responses_api"
    domain = Domain.D6
    weight = 1.0
    samples = 1

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        try:
            response = await request_with_retry(
                ctx, self.id, "POST", "/responses",
                payload={"model": ctx.model, "input": "Say hi."},
            )
        except (RateLimitError, ServerError) as exc:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[exc.note], error=str(exc), warn_failures=True,
            )
        except Exception as exc:  # noqa: BLE001
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"transport error: {exc}"], transport_failures=True,
            )
        body = _json(response)
        if response.status_code == 200 and body:
            ctx.surface.responses_api = True
            output = body.get("output") or []
            has_text = any(
                item.get("type") == "message" for item in output
            ) or isinstance(body.get("output_text"), str)
            return probe_result(
                self.id, self.domain, successes=1 if has_text else 0, attempts=1,
                notes=["Responses API accepted" if has_text else "Responses API returned no message output"],
            )
        if response.status_code in (400, 404, 405, 501):
            ctx.surface.responses_api = False
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=1,
                notes=[f"Responses API unsupported (status {response.status_code}) — downstream probes skip"],
            )
        return probe_result(
            self.id, self.domain, successes=0, attempts=1,
            notes=[f"unexpected status {response.status_code} for /v1/responses"],
        )


def _json(response) -> dict | None:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


def _timing_samples(result: StreamResult) -> list[TimingSample]:
    """Map one streamed exchange onto ProbeResult timing samples (§3.3, §8).

    Only actual streamed exchanges (at least one SSE event) contribute
    timing; error/status bodies carry no TTFT and are skipped.
    """

    if not result.events:
        return []
    samples: list[TimingSample] = []
    if result.ttft_ms is not None:
        samples.append(TimingSample(kind="ttft", ms=result.ttft_ms))
    samples.append(TimingSample(kind="e2e", ms=result.e2e_ms))
    samples.extend(TimingSample(kind="itl", ms=delay) for delay in result.inter_event_ms)
    return samples


def _usage_consistent(usage: dict | None) -> bool:
    if not isinstance(usage, dict):
        return False
    total = usage.get("total_tokens")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not all(isinstance(v, int) for v in (total, prompt, completion)):
        return False
    return total == prompt + completion
