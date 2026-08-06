"""d6.idempotency — custom runner: cross-sample stability at temperature 0.

Three sequential, identical ``temperature=0`` chat completions must agree on
response structure/object and ``finish_reason``, and keep completion-length
spread inside a calibrated bound. Without a tokenizer (M1) length is measured
in characters, matching the budget tracker's naive char heuristic (§11.3).

Calibration: temperature 0 is deterministic, so identical prompts should yield
near-identical completions. ``LENGTH_SPREAD_BOUND = 0.20`` is the maximum
relative spread ``(max - min) / mean`` across the three lengths — a small
default that tolerates minor formatting drift but flags mixed-length output.

Verdicts never falsely pass (§10): non-200 status, empty content, unstable
response structure, or mixed ``finish_reason`` → FAIL; completion-length
variance beyond the bound → WARN with evidence; persistent 429/5xx or
transport errors after one local retry → WARN (infra, not protocol evidence).
"""

from __future__ import annotations

import asyncio
from typing import Any

from supgate.models import Domain, ProbeResult, SurfaceMap, Verdict
from supgate.probes.base import RunContext, request_or_none

LENGTH_SPREAD_BOUND = 0.20
RETRY_BACKOFF_S = 0.5
TIMEOUT_S = 60.0

_PROMPT = "Count from 1 to 5."


class IdempotencyProbe:
    """d6.idempotency — 3 sequential temperature-0 requests, stability checks."""

    id = "d6.idempotency"
    domain = Domain.D6
    weight = 1.0
    samples = 3

    def __init__(self, *, length_spread_bound: float = LENGTH_SPREAD_BOUND) -> None:
        self.length_spread_bound = length_spread_bound

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        samples = [await self._sample(ctx, i) for i in range(self.samples)]
        notes = [s["note"] for s in samples if s["note"]]
        usable = [s for s in samples if s["usable"]]

        if not usable:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=0.0,
                successes=0, attempts=self.samples,
                notes=notes + ["no usable responses — persistent 429/5xx or transport errors after retry"],
            )

        statuses = [s["status"] for s in usable]
        lengths = [s["length"] for s in usable]
        reasons = [s["finish_reason"] for s in usable]
        shapes = [s["shape"] for s in usable]

        hard_fail = []
        if any(status != 200 for status in statuses):
            hard_fail.append(f"statuses={statuses} — expected all 200")
        if any(not s["content"] for s in usable):
            hard_fail.append("empty completion content in at least one sample")
        if len(set(shapes)) > 1:
            hard_fail.append("response structure differs across samples (non-idempotent object shape)")
        if len(set(reasons)) > 1:
            hard_fail.append(f"finish_reason differs across samples: {reasons}")

        successes = sum(1 for s in usable if s["status"] == 200 and s["content"])
        mean = sum(lengths) / max(len(lengths), 1)
        spread = (max(lengths) - min(lengths)) / max(mean, 1.0)
        length_note = f"lengths={lengths} spread={spread:.3f} bound={self.length_spread_bound}"

        if hard_fail:
            notes.extend(hard_fail)
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
                successes=successes, attempts=self.samples, notes=notes,
            )

        notes.append(length_note + (" → stable (PASS)" if spread <= self.length_spread_bound else " → length variance exceeds bound"))
        if spread > self.length_spread_bound:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=successes, attempts=self.samples,
                notes=notes + ["mixed determinism — completions not length-stable at temperature 0"],
            )
        if any(s["infra"] for s in samples):
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=successes, attempts=self.samples,
                notes=notes + ["degraded by persistent 429/5xx or transport errors — WARN per §2"],
            )
        return ProbeResult(
            probe_id=self.id, domain=self.domain, verdict=Verdict.PASS, score=100.0,
            successes=successes, attempts=self.samples, notes=notes,
        )

    async def _sample(self, ctx: RunContext, i: int) -> dict[str, Any]:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": _PROMPT}],
            "max_tokens": 32,
            "temperature": 0,
        }
        for attempt in range(2):
            response, error = await request_or_none(
                ctx, self.id, "POST", "/chat/completions", payload=payload, timeout_s=TIMEOUT_S
            )
            if error is not None:
                if attempt == 0:
                    await asyncio.sleep(RETRY_BACKOFF_S)
                    continue
                return {"usable": False, "infra": True, "note": f"sample {i}: transport error after retry: {error}"}
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(RETRY_BACKOFF_S)
                    continue
                return {"usable": False, "infra": True, "note": f"sample {i}: persistent status {response.status_code} after retry"}
            body = _json(response)
            content, reason = _content_and_reason(body)
            return {
                "usable": True,
                "infra": False,
                "status": response.status_code,
                "content": content,
                "length": len(content),
                "finish_reason": reason,
                "shape": _shape(body),
                "note": "",
            }
        return {"usable": False, "infra": True, "note": f"sample {i}: exhausted retries"}


def _json(response) -> dict | None:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


def _content_and_reason(body: dict | None) -> tuple[str, str | None]:
    if not body:
        return "", None
    try:
        first = body["choices"][0]
    except (KeyError, IndexError, TypeError):
        return "", None
    message = first.get("message") or {}
    return message.get("content") or "", first.get("finish_reason")


def _shape(value: Any) -> tuple[Any, ...]:
    """Structural fingerprint: keys and value types, never literal content.

    Two responses with the same object shape (same keys, same value types) get
    the same fingerprint even when completion text differs; extra/missing keys
    or changed types produce different fingerprints.
    """

    if isinstance(value, dict):
        return ("dict", tuple(sorted((key, _shape(item)) for key, item in value.items())))
    if isinstance(value, list):
        element_shapes = {_shape(item) for item in value}
        return ("list", len(value), tuple(sorted(element_shapes, key=repr)))
    return type(value).__name__
