"""D2 load performance probes (build plan §10.5; docs/05 §6 U1).

Two probes:

- :class:`LoadMatrixProbe` (``d2.load_matrix``): three input bands (<8K,
  8-16K, 16-32K token targets), 20 streamed requests per band at concurrency
  10. An internal semaphore caps each band's in-flight requests and timing
  starts only after the semaphore is acquired, so load-host queueing is
  excluded from TTFT/E2E/TPOT (docs/05 §6 U1: "timers start after semaphore
  (no load-host queueing)"). Per-request TTFT/E2E/ITL/TPOT feed P50/P90;
  goodput is the share of requests meeting TTFT<=5s + TPOT<=500ms +
  E2E<=60s (client SLA overrides via :meth:`LoadMatrixProbe.apply_sla` —
  RunContext does not carry the SLA yet, so the probe owns the thresholds).
  Pass bar: goodput >= 80% per band (proposal, §10.5). Persistent 429/5xx
  after one backoff retry -> WARN (§10); transport errors and invalid
  streams (HTTP 200 without SSE events/content) -> FAIL.

- :class:`NeedleRecallProbe` (``d2.needle_recall``): deterministic ~30K-token
  context with a unique needle planted at 20% depth; the endpoint must echo
  the needle verbatim. Altered/missing needle on HTTP 200 -> FAIL; a clean
  context-length/unsupported 4xx -> WARN (explicit contract-compatible
  refusal); any other non-200 -> FAIL.

Prompt sizing is deterministic token-target construction rather than
enormous literal prompts: filler units are repeated until
``chars / APPROX_CHARS_PER_TOKEN`` reaches the target (chars-per-token
heuristic). When the claimed model's tiktoken encoding resolves,
:class:`~supgate.tokenizers.TokenizerService` recounts the built prompt
exactly for metrics (best-effort: a missing or unresolvable encoding just
leaves ``counted_*`` as None, never breaks the probe).

Class constants (``CALLS_PER_BAND``, ``CONCURRENCY``, ``BANDS``, SLA
thresholds, ``CONTEXT_TOKENS``, ``NEEDLE_DEPTH``, ...) are the docs-derived
defaults and can be overridden per instance via the constructor (tests run
tiny, bounded variants). weight/samples follow the manifest convention
(weight 1.0 like most probes; samples 60 = 3 bands x 20 calls).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

import httpx

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
    request_with_retry,
    text_content,
    warn_result,
)
from supgate.tokenizers import TokenizerService

#: Deterministic filler unit repeated to reach a token target. Kept short so
#: any band/context size is reachable without literal prompts; the needle is
#: unique per run and can never collide with this text.
_FILLER_UNIT = (
    "The quick brown fox measures the width of the harbour in steady increments. "
    "Every ledger entry records the exact position of the pendulum at the moment "
    "the gatekeeper signals the change of watch."
)


def _band_label(token_target: int) -> str:
    """docs/05 §6 U1 / build plan §10.5 band label for a token target."""
    if token_target < 8000:
        return "<8K"
    if token_target < 16000:
        return "8-16K"
    return "16-32K"


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy ``percentile`` default).

    ``sorted_values`` must already be sorted ascending. ``pct`` in [0, 100].
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    pos = pct / 100.0 * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _p50p90(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    ordered = sorted(values)
    return round(_percentile(ordered, 50), 1), round(_percentile(ordered, 90), 1)


def _count_tokens(tokenizer: TokenizerService, model: str, text: str) -> int | None:
    """Best-effort tiktoken recount; None when the encoding is unresolvable.

    Counting is never allowed to break a probe: an unknown model or a
    tokenizer hiccup leaves the metric as None and the chars-per-token
    approximation stands.
    """
    try:
        encoding = tokenizer.resolve_encoding(model)
        if encoding is None:
            return None
        return tokenizer.count(text, encoding)
    except Exception:  # noqa: BLE001 - best-effort metric, never fatal
        return None


def _content_of(response: httpx.Response) -> str:
    """Best-effort choices[0].message.content extraction from a response."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return response.text
    try:
        return text_content(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return ""


class LoadMatrixProbe:
    """d2.load_matrix — 3 input bands x 20 streamed requests at concurrency 10.

    Per band: TTFT/TPOT/ITL/E2E P50+P90, success rate, and goodput = % of
    requests meeting TTFT<=5s + TPOT<=500ms + E2E<=60s (docs/05 §6 U1; the
    client SLA overrides the docs defaults via :meth:`apply_sla`). Pass bar:
    goodput >= 80% per band (proposal, build plan §10.5). Verdict routing
    (§10): a persistent 429/5xx after one retry WARNs; a transport error or
    an invalid stream (200 without SSE events/content) FAILs.
    """

    id = "d2.load_matrix"
    domain = Domain.D2
    weight = 1.0
    samples = 60  # 3 bands x 20 calls (build plan §10.5; docs/05 §6 U1)
    #: Client-SLA-equivalent docs defaults (docs/05 §6 U1); overrideable via
    #: :meth:`apply_sla` once the run bundle carries a client SLA.
    SLA_TTFT_S = 5.0
    SLA_TPOT_MS = 500.0
    SLA_E2E_S = 60.0
    #: Pass bar per band (proposal, §10.5).
    GOODPUT_BAR_PCT = 80.0
    #: In-flight cap per band (build plan §10.5).
    CONCURRENCY = 10
    #: Streamed requests per band (build plan §10.5).
    CALLS_PER_BAND = 20
    #: Token targets: midpoints of the <8K / 8-16K / 16-32K bands (§10.5).
    BANDS: tuple[int, ...] = (4000, 12000, 24000)
    MAX_TOKENS = 64
    #: chars-per-token heuristic for deterministic prompt construction.
    APPROX_CHARS_PER_TOKEN = 4.0
    tokenizer: TokenizerService = TokenizerService()

    def __init__(
        self,
        *,
        calls_per_band: int | None = None,
        concurrency: int | None = None,
        bands: tuple[int, ...] | None = None,
        max_tokens: int | None = None,
        tokenizer: TokenizerService | None = None,
    ) -> None:
        self.calls_per_band = self.CALLS_PER_BAND if calls_per_band is None else int(calls_per_band)
        self.concurrency = self.CONCURRENCY if concurrency is None else int(concurrency)
        self.bands = self.BANDS if bands is None else tuple(int(b) for b in bands)
        self.max_tokens = self.MAX_TOKENS if max_tokens is None else int(max_tokens)
        if tokenizer is not None:
            self.tokenizer = tokenizer
        self.sla = {
            "ttft_s": self.SLA_TTFT_S,
            "tpot_ms": self.SLA_TPOT_MS,
            "e2e_s": self.SLA_E2E_S,
        }

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    def apply_sla(self, sla: Any) -> None:
        """Adopt client SLA thresholds before run() (docs/05 §6 U1).

        Accepts the :class:`~supgate.models.SLA` model or any mapping with
        ``ttft_s``/``tpot_ms``/``e2e_s`` keys. Minimal public hook: RunContext
        does not carry the SLA yet, so the orchestrator calls this later once
        the run bundle's client SLA is available.
        """
        data = dict(sla)
        self.sla = {
            "ttft_s": float(data.get("ttft_s", self.sla["ttft_s"])),
            "tpot_ms": float(data.get("tpot_ms", self.sla["tpot_ms"])),
            "e2e_s": float(data.get("e2e_s", self.sla["e2e_s"])),
        }

    def build_prompt(self, target_tokens: int) -> str:
        """Deterministic prompt of approximately ``target_tokens`` tokens.

        No enormous literal prompts: a fixed filler unit is repeated until
        ``chars / APPROX_CHARS_PER_TOKEN`` reaches the target (chars-per-token
        heuristic, §10.5). :meth:`count_prompt_tokens` recounts the result
        exactly via TokenizerService when the claimed model's encoding
        resolves.
        """
        unit = _FILLER_UNIT
        units = max(1, int(target_tokens * self.APPROX_CHARS_PER_TOKEN / len(unit)))
        return f"Below is a document. {unit * units} Reply with the word PONG."

    def count_prompt_tokens(self, text: str, model: str) -> int | None:
        """Best-effort tiktoken recount of a built prompt; None on any failure."""
        return _count_tokens(self.tokenizer, model, text)

    async def run(self, ctx: RunContext) -> ProbeResult:
        bands: dict[str, dict[str, Any]] = {}
        notes: list[str] = []
        totals = {
            "attempts": 0,
            "successes": 0,
            "good": 0,
            "retry_warns": 0,
            "transport_failures": 0,
            "invalid_streams": 0,
        }
        queue_waits: list[float] = []
        pooled: dict[str, list[float]] = {
            "ttft_ms": [],
            "tpot_ms": [],
            "itl_ms": [],
            "e2e_ms": [],
        }
        samples: list[TimingSample] = []
        for target in self.bands:
            band = await self._run_band(ctx, target)
            label = band["label"]
            bands[label] = band["metrics"]
            notes.extend(band["notes"])
            for key in totals:
                totals[key] += band["totals"][key]
            queue_waits.extend(band["queue_waits"])
            for kind in pooled:
                pooled[kind].extend(band["pooled"][kind])
            for request in band["metrics"]["requests"]:
                if request["outcome"] != "success":
                    continue
                samples.append(TimingSample(kind="ttft", ms=request["ttft_ms"]))
                samples.append(TimingSample(kind="e2e", ms=request["e2e_ms"]))
                if request["tpot_ms"] is not None:
                    samples.append(TimingSample(kind="tpot", ms=request["tpot_ms"]))

        overall_goodput = round(totals["good"] / totals["attempts"] * 100.0, 1) if totals["attempts"] else 0.0
        metrics = {
            "load_matrix": {
                "sla": {**self.sla, "goodput_bar_pct": self.GOODPUT_BAR_PCT},
                "bands": bands,
                "overall": {
                    "attempts": totals["attempts"],
                    "successes": totals["successes"],
                    "good": totals["good"],
                    "goodput_pct": overall_goodput,
                },
                "retry_warns": totals["retry_warns"],
                "transport_failures": totals["transport_failures"],
                "invalid_streams": totals["invalid_streams"],
                "queue_wait_ms": {
                    "count": len(queue_waits),
                    "p50": round(_percentile(sorted(queue_waits), 50), 1),
                    "max": round(max(queue_waits), 1) if queue_waits else None,
                },
            }
        }

        bar = self.GOODPUT_BAR_PCT
        bands_ok = [label for label, band in bands.items() if band["goodput_pct"] >= bar]
        if totals["transport_failures"] or totals["invalid_streams"]:
            verdict, score = Verdict.FAIL, 0.0
            notes.append(
                f"transport ({totals['transport_failures']}) or invalid-stream "
                f"({totals['invalid_streams']}) failures — FAIL per §10"
            )
        elif totals["successes"] == 0:
            # Nothing usable: persistent 429/5xx-only degradation WARNs (§10),
            # any other total failure stays FAIL.
            if totals["retry_warns"]:
                verdict, score = Verdict.WARN, 50.0
            else:
                verdict, score = Verdict.FAIL, 0.0
        elif len(bands_ok) == len(bands):
            verdict, score = Verdict.PASS, 100.0
            notes.append(f"goodput >= {bar}% in every band")
        elif bands_ok:
            verdict, score = Verdict.WARN, overall_goodput
            notes.append(f"goodput >= {bar}% only in bands: {', '.join(bands_ok)}")
        elif totals["retry_warns"]:
            verdict, score = Verdict.WARN, overall_goodput
            notes.append("no band reached the goodput bar and the run was degraded by retries")
        else:
            verdict, score = Verdict.FAIL, 0.0
            notes.append(f"no band reached the {bar}% goodput bar")

        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=verdict,
            score=score,
            weight=self.weight,
            successes=totals["successes"],
            attempts=totals["attempts"],
            notes=notes,
            metrics=metrics,
            samples=samples,
        )

    async def _run_band(self, ctx: RunContext, target_tokens: int) -> dict[str, Any]:
        """One band: ``calls_per_band`` streamed requests under a per-band
        semaphore of ``concurrency``; returns metrics + run accumulators."""
        prompt = self.build_prompt(target_tokens)
        counted = self.count_prompt_tokens(prompt, ctx.model)
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(index: int) -> dict[str, Any]:
            queued_at = time.perf_counter()
            async with semaphore:
                acquired_at = time.perf_counter()
                queued_ms = (acquired_at - queued_at) * 1000.0
                # Timing starts post-semaphore: ctx.stream clocks TTFT/E2E
                # from dispatch, so load-host queueing is excluded (docs/05
                # §6 U1 "timers start after semaphore").
                try:
                    result = await ctx.stream(self.id, "/chat/completions", payload=payload)
                except (RateLimitError, ServerError) as exc:
                    return {"outcome": "retry_warn", "queued_ms": queued_ms, "note": exc.note}
                except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                    return {
                        "outcome": "transport",
                        "queued_ms": queued_ms,
                        "note": f"transport error: {exc}",
                    }
                if result.status != 200 or not result.events or not result.events[-1].delta.strip():
                    return {
                        "outcome": "invalid",
                        "queued_ms": queued_ms,
                        "status": result.status,
                        "note": f"invalid stream: status={result.status} events={len(result.events)}",
                    }
                ttft_ms = result.ttft_ms if result.ttft_ms is not None else 0.0
                e2e_ms = result.e2e_ms
                tokens = _output_tokens(result)
                tpot_ms: float | None = None
                if tokens is not None and tokens > 1 and e2e_ms >= ttft_ms:
                    # TPOT excludes the first output token: generation time
                    # (E2E - TTFT) spans tokens 2..N.
                    tpot_ms = (e2e_ms - ttft_ms) / (tokens - 1)
                return {
                    "outcome": "success",
                    "queued_ms": queued_ms,
                    "ttft_ms": ttft_ms,
                    "e2e_ms": e2e_ms,
                    "tpot_ms": tpot_ms,
                    "itl_ms": list(result.inter_event_ms),
                    "note": None,
                }

        outcomes = await asyncio.gather(*(one(i) for i in range(self.calls_per_band)))

        successes = sum(1 for o in outcomes if o["outcome"] == "success")
        good = 0
        notes: list[str] = []
        for o in outcomes:
            if o["outcome"] == "success":
                good += 1 if self._meets_sla(o) else 0
            else:
                notes.append(f"{_band_label(target_tokens)}: {o['note']}")
        attempts = len(outcomes)
        itl_values = [v for o in outcomes if o["outcome"] == "success" for v in o["itl_ms"]]
        p50: dict[str, float | None] = {}
        p90: dict[str, float | None] = {}
        for kind, key in (
            ("ttft_ms", "ttft_ms"),
            ("tpot_ms", "tpot_ms"),
            ("e2e_ms", "e2e_ms"),
        ):
            values = [o[key] for o in outcomes if o["outcome"] == "success" and o[key] is not None]
            p50[kind], p90[kind] = _p50p90(values)
        p50["itl_ms"], p90["itl_ms"] = _p50p90(itl_values)
        label = _band_label(target_tokens)
        metrics = {
            "token_target": target_tokens,
            "counted_prompt_tokens": counted,
            "attempts": attempts,
            "successes": successes,
            "good": good,
            "goodput_pct": round(good / attempts * 100.0, 1) if attempts else 0.0,
            "success_rate_pct": round(successes / attempts * 100.0, 1) if attempts else 0.0,
            "p50": p50,
            "p90": p90,
            "requests": [
                {
                    "outcome": o["outcome"],
                    "queued_ms": round(o["queued_ms"], 1),
                    "ttft_ms": round(o["ttft_ms"], 1) if o.get("ttft_ms") is not None else None,
                    "e2e_ms": round(o["e2e_ms"], 1) if o.get("e2e_ms") is not None else None,
                    "tpot_ms": round(o["tpot_ms"], 1) if o.get("tpot_ms") is not None else None,
                    "itl_mean_ms": (round(sum(o["itl_ms"]) / len(o["itl_ms"]), 1) if o.get("itl_ms") else None),
                    "good": self._meets_sla(o) if o["outcome"] == "success" else None,
                    "note": o["note"],
                }
                for o in outcomes
            ],
        }
        return {
            "label": label,
            "metrics": metrics,
            "notes": notes,
            "totals": {
                "attempts": attempts,
                "successes": successes,
                "good": good,
                "retry_warns": sum(1 for o in outcomes if o["outcome"] == "retry_warn"),
                "transport_failures": sum(1 for o in outcomes if o["outcome"] == "transport"),
                "invalid_streams": sum(1 for o in outcomes if o["outcome"] == "invalid"),
            },
            "queue_waits": [o["queued_ms"] for o in outcomes],
            "pooled": {
                "ttft_ms": [o["ttft_ms"] for o in outcomes if o["outcome"] == "success"],
                "tpot_ms": [o["tpot_ms"] for o in outcomes if o["outcome"] == "success" and o["tpot_ms"] is not None],
                "itl_ms": itl_values,
                "e2e_ms": [o["e2e_ms"] for o in outcomes if o["outcome"] == "success"],
            },
        }

    def _meets_sla(self, request: dict[str, Any]) -> bool:
        """docs/05 §6 U1 goodput clause: TTFT<=5s + TPOT<=500ms + E2E<=60s.

        ``sla`` thresholds apply when :meth:`apply_sla` was called. A request
        whose TPOT is not computable (single-token completion or no usage
        block) cannot violate the TPOT clause and counts as meeting it.
        """
        ttft_ok = request["ttft_ms"] <= self.sla["ttft_s"] * 1000.0
        e2e_ok = request["e2e_ms"] <= self.sla["e2e_s"] * 1000.0
        tpot = request["tpot_ms"]
        tpot_ok = tpot is None or tpot <= self.sla["tpot_ms"]
        return ttft_ok and e2e_ok and tpot_ok


def _output_tokens(result: StreamResult) -> int | None:
    """completion_tokens from the last event carrying a usage block.

    With ``stream_options.include_usage`` OpenAI appends a final usage-only
    chunk; the last usage-bearing event is authoritative (§10.5 TPOT math).
    Falls back to None when the stream carries no usage block.
    """
    usage = next((event.usage for event in reversed(result.events) if event.usage is not None), None)
    if isinstance(usage, dict):
        tokens = usage.get("completion_tokens")
        if isinstance(tokens, int) and tokens >= 0:
            return tokens
    return None


class NeedleRecallProbe:
    """d2.needle_recall — unique needle at 20% depth of a ~30K-token context.

    The context is deterministic filler (no literal 30K-token prompt); the
    needle is a fresh random token per run. The endpoint must echo the needle
    verbatim on HTTP 200: an altered or missing needle is a silent context
    truncation/compression signal and FAILs (§10.5). A clean
    context-length/unsupported 4xx is an explicit contract-compatible refusal
    and WARNs; any other non-200 FAILs. Persistent 429/5xx after one backoff
    retry WARNs (§10); transport errors FAIL.
    """

    id = "d2.needle_recall"
    domain = Domain.D2
    weight = 1.0
    samples = 1
    #: ~30K-token context (build plan §10.5).
    CONTEXT_TOKENS = 30000
    #: Needle planted at 20% depth (§10.5).
    NEEDLE_DEPTH = 0.2
    APPROX_CHARS_PER_TOKEN = 4.0
    MAX_TOKENS = 64
    #: Distinctive prefix so fixtures/endpoints can locate the unique needle.
    NEEDLE_PREFIX = "VERITAS-NEEDLE-"
    tokenizer: TokenizerService = TokenizerService()
    #: Error text markers that make a 4xx an explicit context/unsupported
    #: refusal (WARN) rather than a generic failure (FAIL).
    _CONTEXT_ERROR_MARKERS = (
        "context_length",
        "context length",
        "maximum context",
        "context window",
        "too many tokens",
        "token limit",
        "unsupported",
    )

    def __init__(
        self,
        *,
        context_tokens: int | None = None,
        needle_depth: float | None = None,
        max_tokens: int | None = None,
        tokenizer: TokenizerService | None = None,
    ) -> None:
        self.context_tokens = self.CONTEXT_TOKENS if context_tokens is None else int(context_tokens)
        self.needle_depth = self.NEEDLE_DEPTH if needle_depth is None else float(needle_depth)
        self.max_tokens = self.MAX_TOKENS if max_tokens is None else int(max_tokens)
        if tokenizer is not None:
            self.tokenizer = tokenizer

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    def build_context(self, needle: str) -> str:
        """Deterministic filler context with the needle planted at
        ``needle_depth`` (20% default). Same needle -> identical context."""
        unit = _FILLER_UNIT
        units = max(2, int(self.context_tokens * self.APPROX_CHARS_PER_TOKEN / len(unit)))
        lines = [f"{unit} section {i + 1}" for i in range(units)]
        at = min(max(1, int(units * self.needle_depth)), len(lines) - 1)
        lines.insert(at, f"[VERIFICATION] {needle}")
        return (
            "You are an audit assistant. Read the document below and, at the end, "
            "repeat the bracketed verification token exactly as written, nothing else.\n\n"
            "DOCUMENT START\n" + "\n".join(lines) + "\nDOCUMENT END"
        )

    async def run(self, ctx: RunContext) -> ProbeResult:
        needle = f"{self.NEEDLE_PREFIX}{secrets.token_hex(4).upper()}"
        context = self.build_context(needle)
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": context}],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        metrics: dict[str, Any] = {
            "needle_recall": {
                "needle": needle,
                "needle_depth": self.needle_depth,
                "approx_context_tokens": round(len(context) / self.APPROX_CHARS_PER_TOKEN),
                "counted_context_tokens": _count_tokens(self.tokenizer, ctx.model, context),
            }
        }
        try:
            response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
        except (RateLimitError, ServerError) as exc:
            result = warn_result(self.id, self.domain, notes=[exc.note], attempts=self.samples)
            result.metrics = metrics
            return result
        except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                weight=self.weight,
                successes=0,
                attempts=self.samples,
                notes=[f"transport error: {exc}"],
                metrics=metrics,
            )
        if response.status_code != 200:
            return self._non_200(response, metrics)
        content = _content_of(response)
        metrics["needle_recall"]["response_contains_needle"] = needle in content
        if needle in content:
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.PASS,
                score=100.0,
                weight=self.weight,
                successes=1,
                attempts=self.samples,
                notes=[f"needle {needle} recalled verbatim at {self.needle_depth:.0%} depth"],
                metrics=metrics,
            )
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.FAIL,
            score=0.0,
            weight=self.weight,
            successes=0,
            attempts=self.samples,
            notes=[
                "needle missing or altered in an HTTP 200 response — silent context "
                "truncation/compression signal (build plan §10.5)"
            ],
            metrics=metrics,
        )

    def _non_200(self, response: httpx.Response, metrics: dict[str, Any]) -> ProbeResult:
        status = response.status_code
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None
        error_text = ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            err = body["error"]
            error_text = " ".join(str(err.get(k, "")) for k in ("message", "type", "code"))
        metrics["needle_recall"]["status"] = status
        if 400 <= status < 500 and any(marker in error_text.lower() for marker in self._CONTEXT_ERROR_MARKERS):
            # Explicit contract-compatible refusal: the endpoint admits it
            # cannot hold the context it claims — honest, so WARN not FAIL.
            result = warn_result(
                self.id,
                self.domain,
                attempts=self.samples,
                notes=[
                    f"clean refusal (status {status}): endpoint rejects the "
                    f"~{self.context_tokens}-token context: {error_text.strip() or body}"
                ],
            )
            result.metrics = metrics
            return result
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.FAIL,
            score=0.0,
            weight=self.weight,
            successes=0,
            attempts=self.samples,
            notes=[f"unexpected status {status} — not a contract-compatible context refusal"],
            metrics=metrics,
        )
