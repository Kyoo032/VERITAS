"""Probe contract (build plan §11.2), RunContext, and shared HTTP helpers.

Every probe gets a :class:`RunContext` (endpoint config, shared httpx client,
budget, evidence writer) and returns a :class:`ProbeResult`. All HTTP goes
through :func:`request` or :meth:`RunContext.stream` so redaction, evidence
capture, and budget counting happen at one choke point.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol, runtime_checkable

import httpx

from supgate.baselines import BaselineRecord
from supgate.evidence import EvidenceWriter, build_curl
from supgate.models import (
    BudgetTracker,
    Domain,
    ProbeResult,
    StreamedEvent,
    StreamResult,
    SurfaceMap,
    Verdict,
)


@runtime_checkable
class Probe(Protocol):
    id: str
    domain: Domain
    weight: float
    samples: int

    def skip_reason(self, surface: SurfaceMap) -> str | None: ...

    async def run(self, ctx: RunContext) -> ProbeResult: ...


def text_content(value: Any) -> str:
    """Normalize string or OpenAI-style text-part content to plain text."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text_content(part) for part in value)
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


class RunContext:
    """Everything a probe needs to talk to the endpoint under test."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        claimed_models: list[str],
        surface: SurfaceMap,
        client: httpx.AsyncClient,
        evidence: EvidenceWriter,
        budget: BudgetTracker,
        selected_baseline: BaselineRecord | None = None,
        p0_verdicts: dict[str, str] | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.claimed_models = claimed_models
        self.surface = surface
        self.client = client
        self.evidence = evidence
        self.budget = budget
        self.selected_baseline = selected_baseline
        # P0 verdict map (probe_id -> verdict value) maintained by the
        # orchestrator as P0 probes complete; drives D4 prerequisite gating
        # (§10.3). Populated before any non-P0 probe runs (P0 sorted first,
        # sequential loop), so reads here are deterministic.
        self.p0_verdicts = p0_verdicts if p0_verdicts is not None else {}

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        headers.update(extra or {})
        return headers

    async def request(
        self,
        probe_id: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float = 60.0,
    ) -> httpx.Response:
        """One instrumented request: timing, evidence capture, budget accounting.

        ``raw_body`` sends an unencoded body (for malformed-body probes).
        Raises the original transport error so callers can mark the probe
        fail/skip; evidence for the failed attempt is still recorded.
        """

        url = f"{self.endpoint}{path}"
        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {}
            if raw_body is not None:
                kwargs["content"] = raw_body
            else:
                kwargs["json"] = payload
            response = await self.client.request(
                method, url, headers=self.headers(headers), timeout=timeout_s, **kwargs
            )
            duration_ms = (time.perf_counter() - started) * 1000
        except Exception as exc:  # noqa: BLE001 - transport errors are per-probe evidence
            duration_ms = (time.perf_counter() - started) * 1000
            curl = build_curl(method, url, self.headers(headers), payload)
            self.evidence.save(
                probe_id,
                method=method,
                url=url,
                request_headers=self.headers(headers),
                request_body=payload,
                status=0,
                response_headers=None,
                response_body=f"transport error: {type(exc).__name__}: {exc}",
                curl=curl,
            )
            raise

        body = _decode_body(response, payload)
        self.budget.add(json.dumps(payload or {}), _text_of(body))
        curl = build_curl(method, url, response.request.headers, payload)
        self.evidence.save(
            probe_id,
            method=method,
            url=url,
            request_headers=self.headers(headers),
            request_body=payload,
            status=response.status_code,
            response_headers=dict(response.headers),
            response_body=body,
            curl=curl,
        )
        response._supgate_ms = duration_ms  # type: ignore[attr-defined]
        return response

    async def stream(
        self,
        probe_id: str,
        path: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_s: float = 60.0,
        backoff_s: float = 0.5,
    ) -> StreamResult:
        """Stream one SSE exchange and return the completed typed result (§3.3).

        Retry policy matches :func:`request_with_retry`: one backoff retry on
        429/5xx before any stream bytes are consumed; a persistent 429/5xx
        raises :class:`RateLimitError`/:class:`ServerError`. Transport errors
        before any bytes (status-0 evidence) or mid-stream (partial-body
        evidence) propagate unchanged and are never retried. Evidence and the
        redacted curl are captured exactly once per call, on every outcome.
        ``[DONE]`` is not yielded; exhaustion implies it.
        """

        url = f"{self.endpoint}{path}"
        req_headers = self.headers(headers)
        attempts = 0
        response: httpx.Response | None = None
        events: list[StreamedEvent] = []
        lines: list[str] = []
        first_ms: float | None = None
        inter: list[float] = []
        try:
            for attempt in range(2):
                response = None
                # Timers re-anchor per attempt so a retried exchange measures
                # the successful dispatch only; the client backoff must never
                # inflate TTFT/E2E latency evidence (docs/05 §6 U1).
                started = time.perf_counter()
                async with self.client.stream(
                    "POST", url, headers=req_headers, json=payload, timeout=timeout_s
                ) as resp:
                    attempts += 1
                    response = resp
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if attempt == 0:
                            await asyncio.sleep(backoff_s)
                            continue
                        raw = await _stream_text(resp)
                        self._save_stream_evidence(probe_id, url, req_headers, payload, resp, raw)
                        if resp.status_code == 429:
                            raise RateLimitError(
                                resp.status_code,
                                f"{probe_id}: rate-limited (429) after retry — Warn per §10, rerun with backoff",
                            )
                        raise ServerError(
                            resp.status_code,
                            f"{probe_id}: server error (status {resp.status_code}) after retry — Warn per §10",
                        )
                    running = ""
                    last_ms = started
                    try:
                        async for line in resp.aiter_lines():
                            lines.append(line)
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                continue
                            arrived = time.perf_counter()
                            if first_ms is None:
                                first_ms = arrived
                            else:
                                inter.append((arrived - last_ms) * 1000)
                            last_ms = arrived
                            delta, usage = _parse_sse_chunk(data)
                            running += delta
                            events.append(StreamedEvent(delta=running, arrived_ms=arrived, usage=usage))
                    except Exception:  # noqa: BLE001 - mid-stream transport error: partial evidence, no retry
                        raw = "\n".join(lines)
                        self._save_stream_evidence(probe_id, url, req_headers, payload, resp, raw)
                        raise
                    break
        except (RateLimitError, ServerError):
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are per-probe evidence
            if response is None:
                curl = build_curl("POST", url, req_headers, payload)
                self.evidence.save(
                    probe_id,
                    method="POST",
                    url=url,
                    request_headers=req_headers,
                    request_body=payload,
                    status=0,
                    response_headers=None,
                    response_body=f"transport error: {type(exc).__name__}: {exc}",
                    curl=curl,
                )
            raise

        if response is None:
            raise AssertionError("unreachable")  # pragma: no cover
        raw = "\n".join(lines)
        e2e_ms = (time.perf_counter() - started) * 1000
        self.budget.add(json.dumps(payload or {}), raw)
        ref, curl = self._save_stream_evidence(probe_id, url, req_headers, payload, response, raw)
        return StreamResult(
            status=response.status_code,
            headers=dict(response.headers),
            body=raw,
            events=events,
            ttft_ms=None if first_ms is None else (first_ms - started) * 1000,
            e2e_ms=e2e_ms,
            inter_event_ms=inter,
            curl=curl,
            evidence_ref=ref,
            attempts=attempts,
        )

    def _save_stream_evidence(
        self,
        probe_id: str,
        url: str,
        request_headers: dict[str, str],
        payload: dict[str, Any],
        response: httpx.Response,
        raw: str,
    ) -> tuple[str, str]:
        """One evidence doc + redacted curl for a streamed exchange; returns (ref, curl)."""

        curl = build_curl("POST", url, response.request.headers, payload)
        ref = self.evidence.save(
            probe_id,
            method="POST",
            url=url,
            request_headers=request_headers,
            request_body=payload,
            status=response.status_code,
            response_headers=dict(response.headers),
            response_body=raw,
            curl=curl,
        )
        return ref, curl


def _decode_body(response: httpx.Response, payload: dict[str, Any] | None) -> Any:
    """Best-effort JSON decode; falls back to text (e.g. SSE bodies)."""

    text = response.text
    if payload and payload.get("stream"):
        return text
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return text


def _text_of(body: Any) -> str:
    if isinstance(body, str):
        return body
    return json.dumps(body or {})


async def _stream_text(response: httpx.Response) -> str:
    """Read a streamed response's full body as text (status/error bodies)."""

    await response.aread()
    return response.text


def _parse_sse_chunk(data: str) -> tuple[str, dict[str, Any] | None]:
    """Extract (content delta, usage block) from one SSE ``data:`` payload.

    Malformed or non-dict payloads contribute an empty delta and no usage.
    """

    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return "", None
    if not isinstance(chunk, dict):
        return "", None
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        usage = None
    delta = ""
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            choice_delta = choice.get("delta")
            if isinstance(choice_delta, dict):
                content = choice_delta.get("content")
                if isinstance(content, str):
                    delta = content
    return delta, usage


async def request_or_none(
    ctx: RunContext,
    probe_id: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """ctx.request that never raises; returns (response, error) instead.

    Evidence is still recorded inside ctx.request on failure.
    """

    try:
        return await ctx.request(probe_id, method, path, **kwargs), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def probe_result(
    probe_id: str,
    domain: Domain,
    *,
    successes: int,
    attempts: int,
    notes: list[str] | None = None,
    error: str | None = None,
    weight: float = 1.0,
) -> ProbeResult:
    """Verdict from a pass/fail count: all pass -> pass, partial -> warn, none -> fail."""

    notes = notes or []
    if attempts == 0:
        verdict, score = "fail", 0.0
    elif successes == attempts:
        verdict, score = "pass", 100.0
    elif successes > 0:
        verdict, score = "warn", round(successes / attempts * 100, 1)
    else:
        verdict, score = "fail", 0.0
    return ProbeResult(
        probe_id=probe_id,
        domain=domain,
        verdict=verdict,
        score=score,
        weight=weight,
        successes=successes,
        attempts=attempts,
        notes=notes,
        error=error,
    )


class RateLimitError(Exception):
    """Persistent 429 after one backoff retry — §10 says WARN, never FAIL."""

    def __init__(self, status: int, note: str) -> None:
        self.status = status
        self.note = note
        super().__init__(note)


class ServerError(Exception):
    """Persistent 5xx after one backoff retry — §10 says WARN, never FAIL."""

    def __init__(self, status: int, note: str) -> None:
        self.status = status
        self.note = note
        super().__init__(note)


async def request_with_retry(
    ctx: RunContext,
    probe_id: str,
    method: str,
    path: str,
    *,
    backoff_s: float = 0.5,
    **kwargs: Any,
) -> httpx.Response:
    """One instrumented request with one backoff retry on 429/5xx (§10).

    Single home for the global retry policy used by every custom P0/D6
    probe. Returns the response once a status outside 429/5xx is seen
    (success, or a 401/400 the probe needs to inspect). A transport error
    propagates unchanged so callers keep it FAIL. A persistent 429/5xx
    raises :class:`RateLimitError`/:class:`ServerError`; callers convert
    those to an explicit WARN using the carried ``note``.
    """

    for attempt in range(2):
        response = await ctx.request(probe_id, method, path, **kwargs)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 0:
                await asyncio.sleep(backoff_s)
                continue
            if response.status_code == 429:
                raise RateLimitError(
                    response.status_code,
                    f"{probe_id}: rate-limited (429) after retry — Warn per §10, rerun with backoff",
                )
            raise ServerError(
                response.status_code,
                f"{probe_id}: server error (status {response.status_code}) after retry — Warn per §10",
            )
        return response
    raise AssertionError("unreachable")  # pragma: no cover


def warn_result(
    probe_id: str,
    domain: Domain,
    *,
    notes: list[str],
    error: str | None = None,
    weight: float = 1.0,
    attempts: int = 1,
    score: float = 50.0,
) -> ProbeResult:
    """§10: persistent 429/5xx is an explicit WARN, never a silent FAIL."""

    return ProbeResult(
        probe_id=probe_id,
        domain=domain,
        verdict=Verdict.WARN,
        score=score,
        weight=weight,
        successes=0,
        attempts=attempts,
        notes=notes,
        error=error,
    )


def probe_result_with_warn(
    probe_id: str,
    domain: Domain,
    *,
    successes: int,
    attempts: int,
    notes: list[str] | None = None,
    error: str | None = None,
    weight: float = 1.0,
    warn_failures: bool = False,
    transport_failures: bool = False,
) -> ProbeResult:
    """probe_result, except §10 converts a total 429/5xx failure to WARN.

    When every failure came from persistent 429/5xx (and none from a
    transport error) the probe WARNs instead of FAILing; a transport error
    keeps the FAIL so ``endpoint_dead`` still triggers on a dead endpoint.
    """

    if successes == 0 and attempts > 0 and warn_failures and not transport_failures:
        return warn_result(
            probe_id, domain, notes=notes or [], error=error, weight=weight, attempts=attempts
        )
    return probe_result(
        probe_id, domain, successes=successes, attempts=attempts,
        notes=notes, error=error, weight=weight,
    )
