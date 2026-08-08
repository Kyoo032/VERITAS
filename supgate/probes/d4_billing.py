"""Custom-logic D4 billing forensics probes (docs/06-m2-probe-spec.md §5.1-§5.4).

Four probes: d4.usage_presence, d4.recount_deviation, d4.wrap_offset,
d4.reasoning_cache_fields. Together they form the billing_transparency
signal family and are the primary source of ``billing_inflation`` veto
evidence (docs/06 §1.5).

Tokenizer discipline (docs/06 §3.5): recount/wrap count the request/response
*text* under the encoding resolved for the claimed model. An unknown
encoding SKIPs the probe with "unknown encoding for model" — the budget
tracker's ``FALLBACK_ENCODING`` is deliberately never used here, because
recounting under the wrong encoding would manufacture inflation evidence.

Verdict routing (docs/06 §1.3): persistent 429/5xx after exactly one local
backoff retry maps to WARN (:class:`RateLimitError`/:class:`ServerError`);
transport errors (no HTTP response) stay FAIL with the failed attempt's
evidence saved; non-retryable bad statuses are hard FAILs. All HTTP flows
through ``RunContext.request``/``stream`` so redaction and curl capture stay
at the single choke point. Metrics dicts are veto-ready for the orchestrator
(docs/06 §3.6, §5.2-5.3).
"""

from __future__ import annotations

import json
from statistics import stdev
from typing import Any

from supgate.models import Domain, ProbeResult, SurfaceMap, Verdict
from supgate.probes.base import (
    RateLimitError,
    RunContext,
    ServerError,
    request_with_retry,
    text_content,
)
from supgate.probes.d4_fingerprint import probe_result_capped
from supgate.probes.d6_protocol import _usage_consistent
from supgate.tokenizers import TokenizerService

# d4.recount_deviation reference prompts (docs/06 §5.2): same prompt family,
# three lengths — short / ~40-word / ~200-word paragraphs.
_SENTENCE = "The quick brown fox jumps over the lazy dog and circles the fence."
_RECOUNT_PROMPTS: tuple[tuple[str, str], ...] = (
    ("short", "Say ping."),
    ("medium", (_SENTENCE + " ") * 3),
    ("long", (_SENTENCE + " ") * 16),
)

# d4.wrap_offset monotonic lengths (docs/06 §5.3): ~5 / ~50 / ~150 / ~400 words.
_WRAP_PROMPTS: tuple[str, ...] = (
    "Say ping.",
    " ".join(["billing"] * 50),
    " ".join(["billing"] * 150),
    " ".join(["billing"] * 400),
)

# d4.reasoning_cache_fields shared prefix: >= 300 tokens to exercise caching
# (docs/06 §5.4 request shapes 1-3).
_CACHE_PREFIX = " ".join(f"reference token {i}" for i in range(300))

# Static PASS/WARN/FAIL gates (docs/06 §5.2): mean over-report gates, replaced
# by the baseline-calibrated gates when a matched baseline carries
# ``recount_deviation_pct`` (warn = mean + 4*std; fail = max(15, mean + 8*std)).
_STATIC_WARN_GATE_PCT = 5.0
_STATIC_FAIL_GATE_PCT = 15.0

# Static wrap_offset gates (docs/06 §5.3): +-4 neutral band and 32-token fail
# gate. A baseline ``wrap_offset_tokens`` mean+6*std replaces the fail gate
# but never loosens it below the static expectation (min(32, mean + 6*std)).
_STATIC_WRAP_NEUTRAL_BAND = 4.0
_STATIC_WRAP_FAIL_GATE = 32.0

# Usage-details expectation table (docs/06 §5.4): claimed family ->
# {cached_tokens, reasoning_tokens}. A matched baseline ``usage_schema``
# overrides the table entirely.
_USAGE_SCHEMA_TABLE: tuple[tuple[tuple[str, ...], tuple[bool, bool]], ...] = (
    (("o1", "o3", "o4"), (True, True)),
    (("gpt-4o", "gpt-4", "gpt-3.5"), (True, False)),
    (("text-", "davinci", "curie"), (False, False)),
)


def usage_schema_for(model: str) -> dict[str, bool] | None:
    """Usage-details schema the claimed family should emit, or None.

    ``{cached_tokens, reasoning_tokens}`` booleans for the family (docs/06
    §5.4). ``None`` when the family is unknown — d4.reasoning_cache_fields
    then SKIPs unless a baseline ``usage_schema`` provides the expectation.
    Shared with ``tests/fake_server.py`` so the fixture emits the schema the
    probe expects.
    """

    name = model.strip().lower()
    for prefixes, (cached, reasoning) in _USAGE_SCHEMA_TABLE:
        if any(name.startswith(prefix) for prefix in prefixes):
            return {"cached_tokens": cached, "reasoning_tokens": reasoning}
    return None


def _json(response: Any) -> dict | None:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - any parse failure means no usable body
        return None


def _content_of(body: dict | None) -> str:
    if not body:
        return ""
    try:
        return text_content(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return ""


def _deviation_pct(reported: int, recounted: int) -> float:
    """(reported - recounted) / recounted as a percentage; 0 when empty text."""
    if recounted <= 0:
        return 0.0
    return (reported - recounted) / recounted * 100.0


def _recount_baseline(ctx: RunContext) -> dict[str, Any]:
    """Baseline-calibrated recount gates (docs/06 §5.2): warn = mean + 4*std,
    fail = max(15, mean + 8*std). Static gates when no usable fingerprint."""

    baseline = ctx.selected_baseline
    if baseline is not None:
        fp = baseline.fingerprints.get("recount_deviation_pct")
        mean = fp.get("mean") if isinstance(fp, dict) else None
        std = fp.get("std") if isinstance(fp, dict) else None
        if isinstance(mean, (int, float)) and isinstance(std, (int, float)):
            return {
                "present": True,
                "mean": mean,
                "std": std,
                "warn_gate_pct": mean + 4 * std,
                "fail_gate_pct": max(_STATIC_FAIL_GATE_PCT, mean + 8 * std),
            }
    return {
        "present": False,
        "mean": None,
        "std": None,
        "warn_gate_pct": _STATIC_WARN_GATE_PCT,
        "fail_gate_pct": _STATIC_FAIL_GATE_PCT,
    }


def _wrap_baseline_gate(ctx: RunContext) -> dict[str, Any]:
    """Baseline-calibrated wrap fail gate (docs/06 §5.3): mean + 6*std, never
    looser than the static 32-token expectation (min(32, mean + 6*std))."""

    baseline = ctx.selected_baseline
    if baseline is not None:
        fp = baseline.fingerprints.get("wrap_offset_tokens")
        mean = fp.get("mean") if isinstance(fp, dict) else None
        std = fp.get("std") if isinstance(fp, dict) else None
        if isinstance(mean, (int, float)) and isinstance(std, (int, float)):
            gate = min(_STATIC_WRAP_FAIL_GATE, mean + 6 * std)
            return {"present": True, "mean": mean, "std": std, "fail_gate_tokens": gate}
    return {
        "present": False,
        "mean": None,
        "std": None,
        "fail_gate_tokens": _STATIC_WRAP_FAIL_GATE,
    }


def _baseline_usage_schema(ctx: RunContext) -> dict[str, bool] | None:
    """Baseline ``usage_schema`` fingerprint as an expectation dict, or None."""

    baseline = ctx.selected_baseline
    if baseline is None:
        return None
    fp = baseline.fingerprints.get("usage_schema")
    if not isinstance(fp, dict) or "cached_tokens" not in fp or "reasoning_tokens" not in fp:
        return None
    return {
        "cached_tokens": bool(fp["cached_tokens"]),
        "reasoning_tokens": bool(fp["reasoning_tokens"]),
    }


class UsagePresenceProbe:
    """d4.usage_presence ×3 forms — usage present and arithmetically sane in
    every form where the contract requires it (docs/06 §5.1)."""

    id = "d4.usage_presence"
    domain = Domain.D4
    weight = 1.0
    samples = 3

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "What is 2+2?"}],
            "max_tokens": 32,
        }
        forms = {
            "non_stream": await _usage_form(ctx, self.id, payload, "non-stream"),
            "stream_include_usage": await _usage_form(
                ctx, self.id,
                {**payload, "stream": True, "stream_options": {"include_usage": True}},
                "stream (include_usage)",
            ),
            "stream_no_include_usage": await _usage_form(
                ctx, self.id, {**payload, "stream": True}, "stream (no include_usage)"
            ),
        }

        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        ok_count = 0
        required_missing = False
        bad_arithmetic = False
        for name, label in (("non_stream", "non-stream"), ("stream_include_usage", "stream (include_usage)")):
            form = forms[name]
            if form["outcome"] != "ok":
                notes.append(form["note"])
                if form["outcome"] == "retryable":
                    warn_failures = True
                else:
                    transport_failures = True
                continue
            if form["status"] != 200:
                hard_failures += 1
                notes.append(f"{label}: unexpected status {form['status']}")
                continue
            if not form["usage_present"]:
                required_missing = True
                notes.append(f"{label}: usage absent on HTTP 200 (required for billing transparency)")
                continue
            if not _usage_consistent(form["usage"]):
                bad_arithmetic = True
                notes.append(f"{label}: usage arithmetically inconsistent: {form['usage']}")
                continue
            ok_count += 1

        form3 = forms["stream_no_include_usage"]
        if form3["outcome"] == "ok":
            if form3["status"] != 200:
                hard_failures += 1
                notes.append(f"stream (no include_usage): unexpected status {form3['status']}")
        elif form3["outcome"] == "retryable":
            warn_failures = True
            notes.append(form3["note"])
        else:
            transport_failures = True
            notes.append(form3["note"])
        form3_present = form3["outcome"] == "ok" and form3["status"] == 200 and form3["usage_present"]

        metrics = {
            "usage_presence": {
                "forms": {
                    name: {
                        "outcome": form["outcome"],
                        "status": form.get("status"),
                        "usage_present": form.get("usage_present", False),
                        "usage": form.get("usage"),
                        "arithmetic_ok": (
                            _usage_consistent(form["usage"]) if form.get("usage_present") else None
                        ),
                    }
                    for name, form in forms.items()
                }
            }
        }

        if required_missing:
            notes.append("required usage missing on HTTP 200 — billing transparency defect")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
                successes=ok_count, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if transport_failures or hard_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=ok_count, attempts=self.samples, notes=notes,
                warn_failures=warn_failures, transport_failures=transport_failures,
            )
            result.metrics = metrics
            return result
        if warn_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=ok_count, attempts=self.samples, notes=notes,
                warn_failures=True,
            )
            result.metrics = metrics
            return result
        if bad_arithmetic or form3_present:
            if form3_present:
                notes.append("usage emitted on stream without include_usage — WARN (nonstandard but harmless)")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=ok_count, attempts=self.samples, notes=notes, metrics=metrics,
            )
        notes.append("usage present and arithmetically consistent in all required forms")
        return ProbeResult(
            probe_id=self.id, domain=self.domain, verdict=Verdict.PASS, score=100.0,
            successes=ok_count, attempts=self.samples, notes=notes, metrics=metrics,
        )


async def _usage_form(
    ctx: RunContext, probe_id: str, payload: dict[str, Any], name: str
) -> dict[str, Any]:
    """One usage-presence form exchange; never raises (docs/06 §1.3 routing
    is applied by the caller from the outcome)."""

    try:
        if payload.get("stream"):
            result = await ctx.stream(probe_id, "/chat/completions", payload=payload)
            # docs/06 §5.1: the authoritative usage block is the LAST SSE
            # event that carries one (OpenAI appends a final usage-only chunk
            # when ``include_usage`` is set); an earlier chunk with a usage
            # block would be a malformed/stale emission.
            usage = next(
                (event.usage for event in reversed(result.events) if event.usage is not None),
                None,
            )
            return {
                "name": name,
                "outcome": "ok",
                "status": result.status,
                "usage": usage,
                "usage_present": usage is not None,
            }
        response = await request_with_retry(ctx, probe_id, "POST", "/chat/completions", payload=payload)
        body = _json(response)
        usage = body.get("usage") if body else None
        return {
            "name": name,
            "outcome": "ok",
            "status": response.status_code,
            "usage": usage,
            "usage_present": isinstance(usage, dict),
        }
    except (RateLimitError, ServerError) as exc:
        return {"name": name, "outcome": "retryable", "note": f"{name}: {exc.note}"}
    except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
        return {"name": name, "outcome": "transport", "note": f"{name}: transport error: {exc}"}


class RecountDeviationProbe:
    """d4.recount_deviation ×3 prompt sizes — independently token-count the
    prompt/completion text vs reported usage (docs/06 §5.2). The strongest
    M2 veto signal; metrics are veto-ready for ``billing_inflation``."""

    id = "d4.recount_deviation"
    domain = Domain.D4
    weight = 2.0
    samples = 3
    tokenizer: TokenizerService = TokenizerService()

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        encoding = self.tokenizer.resolve_encoding(ctx.model)
        if encoding is None:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=[f"skipped: unknown encoding for model {ctx.model!r}"],
            )
        per_sample: list[dict[str, Any]] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for size, text in _RECOUNT_PROMPTS:
            payload = {
                "model": ctx.model,
                "messages": [{"role": "user", "content": text}],
                "max_tokens": 64,
                "temperature": 0,
            }
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"{size}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"{size}: transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"{size}: non-retryable bad response (status {response.status_code})")
                continue
            body = _json(response)
            usage = body.get("usage") if body else None
            if not isinstance(usage, dict):
                hard_failures += 1
                notes.append(f"{size}: usage absent on HTTP 200 (recount impossible)")
                continue
            recounted_prompt = self.tokenizer.count(json.dumps(payload["messages"]), encoding)
            recounted_completion = self.tokenizer.count(_content_of(body), encoding)
            reported_prompt = usage.get("prompt_tokens")
            reported_completion = usage.get("completion_tokens")
            if not isinstance(reported_prompt, int) or not isinstance(reported_completion, int):
                hard_failures += 1
                notes.append(f"{size}: reported usage tokens missing or non-integer")
                continue
            details = usage.get("prompt_tokens_details")
            cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
            per_sample.append(
                {
                    "size": size,
                    "reported_prompt_tokens": reported_prompt,
                    "recounted_prompt_tokens": recounted_prompt,
                    "deviation_pct": round(_deviation_pct(reported_prompt, recounted_prompt), 2),
                    "reported_completion_tokens": reported_completion,
                    "recounted_completion_tokens": recounted_completion,
                    "dev_completion_pct": round(
                        _deviation_pct(reported_completion, recounted_completion), 2
                    ),
                    "cached_tokens": cached,
                    "cached_sample": isinstance(cached, int) and cached > 0,
                }
            )

        baseline_info = _recount_baseline(ctx)
        warn_gate = baseline_info["warn_gate_pct"]
        fail_gate = baseline_info["fail_gate_pct"]
        # docs/06 §9.1.3: samples with cached-token inflation are excluded from
        # the deviation mean (and cannot confirm a billing_inflation veto).
        excluded = [i for i, sample in enumerate(per_sample) if sample["cached_sample"]]
        included = [sample for sample in per_sample if not sample["cached_sample"]]
        mean_dev = sum(sample["deviation_pct"] for sample in included) / len(included) if included else None
        # docs/06 §1.5: the billing_inflation veto needs the deviation above
        # the FAIL gate across ALL short/medium/long measurements — exactly
        # all three non-cached sizes must be measured and over the gate. One
        # or two included samples may still inform WARN/FAIL metrics, but
        # ``all_sizes_above_fail_gate`` stays False and cannot veto.
        all_sizes_above_fail_gate = len(included) == self.samples and all(
            sample["deviation_pct"] > fail_gate for sample in included
        )

        metrics = {
            "recount_deviation": {
                "encoding": encoding,
                "per_sample": per_sample,
                "per_size_deviation_pct": [sample["deviation_pct"] for sample in included],
                "mean_deviation_pct": round(mean_dev, 2) if mean_dev is not None else None,
                "warn_gate_pct": round(warn_gate, 2),
                "fail_gate_pct": round(fail_gate, 2),
                "all_sizes_above_fail_gate": all_sizes_above_fail_gate,
                "excluded_cached_samples": excluded,
                "baseline": baseline_info,
            }
        }

        if transport_failures or hard_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=len(per_sample), attempts=self.samples,
                notes=notes, warn_failures=warn_failures, transport_failures=transport_failures,
            )
            result.metrics = metrics
            return result
        if warn_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=len(per_sample), attempts=self.samples,
                notes=notes, warn_failures=True,
            )
            result.metrics = metrics
            return result
        if mean_dev is None:
            notes.append(
                "all samples excluded from recount (cached-token inflation) — no deviation confirmation"
            )
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if mean_dev <= warn_gate:
            notes.append(f"mean recount deviation {mean_dev:.2f}% within WARN gate {warn_gate:.2f}%")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.PASS, score=100.0,
                successes=self.samples, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if mean_dev <= fail_gate:
            notes.append(f"mean recount deviation {mean_dev:.2f}% in WARN band ({warn_gate:.2f}%..{fail_gate:.2f}%)")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        notes.append(f"mean recount deviation {mean_dev:.2f}% exceeds FAIL gate {fail_gate:.2f}%")
        if all_sizes_above_fail_gate:
            notes.append("over-reporting confirmed across all prompt sizes — billing_inflation veto-ready")
        else:
            notes.append("over-reporting NOT confirmed across all prompt sizes (some sizes missing or below the fail gate)")
        return ProbeResult(
            probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
            successes=0, attempts=self.samples, notes=notes, metrics=metrics,
        )


class WrapOffsetProbe:
    """d4.wrap_offset ×4 monotonic lengths — isolate the *constant* component
    of prompt-token deviation (hidden wrapper; docs/06 §5.3)."""

    id = "d4.wrap_offset"
    domain = Domain.D4
    weight = 1.0
    samples = 4
    tokenizer: TokenizerService = TokenizerService()

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        encoding = self.tokenizer.resolve_encoding(ctx.model)
        if encoding is None:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=[f"skipped: unknown encoding for model {ctx.model!r}"],
            )
        per_sample: list[dict[str, Any]] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for text in _WRAP_PROMPTS:
            payload = {
                "model": ctx.model,
                "messages": [{"role": "user", "content": text}],
                "max_tokens": 16,
                "temperature": 0,
            }
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"{len(text.split())} words: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"{len(text.split())} words: transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"{len(text.split())} words: non-retryable bad response (status {response.status_code})")
                continue
            body = _json(response)
            usage = body.get("usage") if body else None
            if not isinstance(usage, dict):
                hard_failures += 1
                notes.append(f"{len(text.split())} words: usage absent on HTTP 200 (offset impossible)")
                continue
            reported_prompt = usage.get("prompt_tokens")
            if not isinstance(reported_prompt, int):
                hard_failures += 1
                notes.append(f"{len(text.split())} words: reported prompt_tokens missing or non-integer")
                continue
            recounted_prompt = self.tokenizer.count(json.dumps(payload["messages"]), encoding)
            per_sample.append(
                {
                    "words": len(text.split()),
                    "reported_prompt_tokens": reported_prompt,
                    "recounted_prompt_tokens": recounted_prompt,
                    "offset_tokens": reported_prompt - recounted_prompt,
                }
            )

        offsets = [sample["offset_tokens"] for sample in per_sample]
        mean_offset = sum(offsets) / len(offsets) if offsets else 0.0
        std_offset = stdev(offsets) if len(offsets) > 1 else 0.0
        offset_stable = std_offset <= 0.25 * max(abs(mean_offset), 1.0)
        gate_info = _wrap_baseline_gate(ctx)
        fail_gate = gate_info["fail_gate_tokens"]

        metrics = {
            "wrap_offset": {
                "encoding": encoding,
                "per_sample": per_sample,
                "mean_offset_tokens": round(mean_offset, 2),
                "std_offset_tokens": round(std_offset, 2),
                "offset_stable": offset_stable,
                "fail_gate_tokens": fail_gate,
                "baseline": gate_info,
            }
        }

        if transport_failures or hard_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=len(per_sample), attempts=self.samples,
                notes=notes, warn_failures=warn_failures, transport_failures=transport_failures,
            )
            result.metrics = metrics
            return result
        if warn_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=len(per_sample), attempts=self.samples,
                notes=notes, warn_failures=True,
            )
            result.metrics = metrics
            return result
        if not offset_stable:
            notes.append("offset varies with prompt length — no constant wrapper (tokenizer/overhead noise)")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.PASS, score=100.0,
                successes=self.samples, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if abs(mean_offset) <= _STATIC_WRAP_NEUTRAL_BAND:
            notes.append(f"mean offset {mean_offset:.2f} tokens within the +-4 neutral band")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.PASS, score=100.0,
                successes=self.samples, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if mean_offset < 0:
            notes.append(f"stable under-reporting offset {mean_offset:.2f} tokens — anomaly, not a veto signal")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if mean_offset <= fail_gate:
            notes.append(f"stable constant offset {mean_offset:.2f} tokens — hidden wrapper band (WARN, <= {fail_gate:.2f})")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        notes.append(
            f"stable constant offset {mean_offset:.2f} tokens exceeds fail gate {fail_gate:.2f} — "
            "large hidden wrapper (hidden_origin corroboration signal)"
        )
        return ProbeResult(
            probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
            successes=0, attempts=self.samples, notes=notes, metrics=metrics,
        )


class ReasoningCacheFieldsProbe:
    """d4.reasoning_cache_fields ×3 identical calls (large shared prefix) —
    cache/reasoning usage-details fields present and internally consistent
    with the claimed family's expectation (docs/06 §5.4)."""

    id = "d4.reasoning_cache_fields"
    domain = Domain.D4
    weight = 1.0
    samples = 3

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        baseline_usage = _baseline_usage_schema(ctx)
        expected = baseline_usage if baseline_usage is not None else usage_schema_for(ctx.model)
        if expected is None:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=[
                    "skipped: claimed model family unknown to the usage schema table "
                    "and no baseline usage_schema"
                ],
            )
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": f"{_CACHE_PREFIX} What is 2+2?"}],
            "max_tokens": 24,
            "temperature": 0,
        }
        per_call: list[dict[str, Any]] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for i in range(self.samples):
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"call {i}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"call {i}: transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"call {i}: non-retryable bad response (status {response.status_code})")
                continue
            body = _json(response)
            usage = body.get("usage") if body else None
            if not isinstance(usage, dict):
                hard_failures += 1
                notes.append(f"call {i}: usage absent on HTTP 200 (cache fields unreadable)")
                continue
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            pdetails = usage.get("prompt_tokens_details")
            cdetails = usage.get("completion_tokens_details")
            pdetails = pdetails if isinstance(pdetails, dict) else None
            cdetails = cdetails if isinstance(cdetails, dict) else None
            cached = pdetails.get("cached_tokens") if pdetails else None
            text_tokens = pdetails.get("text_tokens") if pdetails else None
            reasoning = cdetails.get("reasoning_tokens") if cdetails else None
            # docs/06 §5.4: 0 <= cached <= prompt; text_tokens == prompt - cached
            # when both are present; 0 <= reasoning <= completion.
            cached_ok = True
            if cached is not None:
                cached_ok = (
                    isinstance(cached, int) and isinstance(prompt, int) and 0 <= cached <= prompt
                )
                if cached_ok and text_tokens is not None:
                    cached_ok = isinstance(text_tokens, int) and text_tokens == prompt - cached
            reasoning_ok = True
            if reasoning is not None:
                reasoning_ok = (
                    isinstance(reasoning, int)
                    and isinstance(completion, int)
                    and 0 <= reasoning <= completion
                )
            per_call.append(
                {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "cached_tokens": cached,
                    "text_tokens": text_tokens,
                    "reasoning_tokens": reasoning,
                    "cached_ok": cached_ok,
                    "reasoning_ok": reasoning_ok,
                }
            )

        caches = [call["cached_tokens"] for call in per_call if call["cached_tokens"] is not None]
        cached_seen = bool(caches)
        reasoning_seen = any(call["reasoning_tokens"] is not None for call in per_call)
        cache_deltas = [caches[i] - caches[i - 1] for i in range(1, len(caches))]
        cache_delta_ok = all(delta >= 0 for delta in cache_deltas)
        contradictory = any(not call["cached_ok"] or not call["reasoning_ok"] for call in per_call)
        missing_expected = (expected["cached_tokens"] and not cached_seen) or (
            expected["reasoning_tokens"] and not reasoning_seen
        )

        metrics = {
            "reasoning_cache_fields": {
                "expected": expected,
                "baseline_usage_schema": baseline_usage,
                "per_call": per_call,
                "cached_seen": cached_seen,
                "reasoning_seen": reasoning_seen,
                "cache_deltas": cache_deltas,
                "cache_delta_ok": cache_delta_ok,
                "missing_expected": missing_expected,
                "contradictory": contradictory,
            }
        }

        if transport_failures or hard_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=len(per_call), attempts=self.samples,
                notes=notes, warn_failures=warn_failures, transport_failures=transport_failures,
            )
            result.metrics = metrics
            return result
        if warn_failures:
            result = probe_result_capped(
                self.id, self.domain, successes=len(per_call), attempts=self.samples,
                notes=notes, warn_failures=True,
            )
            result.metrics = metrics
            return result
        if contradictory:
            notes.append("usage details fields present but contradictory (cached/reasoning out of range)")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if missing_expected:
            notes.append(f"expected usage details fields missing for family {ctx.model!r}: {expected}")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        if not cache_delta_ok:
            notes.append("cache delta regressed between identical calls (second call no longer cached)")
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=self.samples, notes=notes, metrics=metrics,
            )
        notes.append(f"cache/reasoning fields consistent with expected schema {expected}")
        return ProbeResult(
            probe_id=self.id, domain=self.domain, verdict=Verdict.PASS, score=100.0,
            successes=self.samples, attempts=self.samples, notes=notes, metrics=metrics,
        )
