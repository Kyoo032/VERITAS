"""Deterministic baseline-record planning estimates (docs/12 §8 P1).

Pure domain API: no endpoint requests are made and no user files are created
(tiktoken may fetch/cache its BPE encoding data on first use on a cold
machine). The estimates describe nominal logical requests and a retry-aware
worst-case ceiling under the per-stage assumptions; capped stages pin their
max_tokens/max_output_tokens, so completion ceilings are provider-enforced
where noted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from supgate.probes.d4_billing import (
    _RECOUNT_PROMPTS,
    _WRAP_PROMPTS,
    RecountDeviationProbe,
    WrapOffsetProbe,
)
from supgate.probes.p0 import EchoProbe, ModelsProbe
from supgate.tokenizers import (
    DEFAULT_MODEL,
    FALLBACK_ENCODING,
    FALLBACK_RATES,
    ModelRates,
    TokenizerService,
    resolve_rates,
)

# Must match baseline_recorder capture prompts / payload limits (prevent drift).
_CHAT_CAPTURE_PROMPT = "Reply with the single word ping."
_STREAM_CAPTURE_PROMPT = "Count from 1 to 50."
_SELF_REPORT_CAPTURE_PROMPT = (
    "Describe in one sentence which hosting platform or provider API is serving this request."
)

# POST /responses surface probe pins its completion ceiling so the plan's
# response-token assumption is a provider-enforced upper bound. The recorder
# imports this constant to build its payload — never change one side alone.
_RESPONSES_MAX_OUTPUT_TOKENS = 16

# Finite response-token planning assumptions. Capped stages use their actual
# max_tokens/max_output_tokens. Only p0.models (GET /models) has no provider
# response-token ceiling; its value is an estimate called out in assumptions.
_RESPONSE_TOKEN_ASSUMPTIONS: dict[str, int] = {
    "p0.echo": 64,
    "p0.models": 4096,
    "baseline.capture.responses": _RESPONSES_MAX_OUTPUT_TOKENS,
    "baseline.capture.chat": 8,
    "baseline.capture.stream": 64,
    "baseline.capture.self_report": 48,
    "d4.recount_deviation": 64,
    "d4.wrap_offset": 16,
}

# One retry on 429/5xx is implemented by request_with_retry/RunContext.stream.
# /responses capture deliberately uses one direct request_or_none call.
_RETRYABLE_STAGES = frozenset(
    {
        "p0.echo",
        "p0.models",
        "baseline.capture.chat",
        "baseline.capture.stream",
        "baseline.capture.self_report",
        "d4.recount_deviation",
        "d4.wrap_offset",
    }
)


@dataclass(frozen=True)
class StagePlan:
    """One ordered stage's nominal and retry-aware planning estimates."""

    id: str
    requests: int
    max_requests: int
    estimated_prompt_tokens: int
    estimated_completion_tokens: int
    estimated_usd: float
    estimated_max_prompt_tokens: int
    estimated_max_completion_tokens: int
    estimated_max_usd: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineRecordPlan:
    """Full baseline-record plan (JSON-serializable via :meth:`to_dict`)."""

    model: str
    samples: int
    streams: int
    run_p0_gate: bool
    billing_included: bool
    encoding: str | None
    rates: dict[str, float]
    stages: list[StagePlan]
    requests: int
    max_requests: int
    estimated_prompt_tokens: int
    estimated_completion_tokens: int
    estimated_usd: float
    estimated_max_prompt_tokens: int
    estimated_max_completion_tokens: int
    estimated_max_usd: float
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_baseline_record(
    *,
    model: str,
    samples: int = 3,
    streams: int = 1,
    run_p0_gate: bool = True,
    tokenizer: TokenizerService | None = None,
) -> BaselineRecordPlan:
    """Build a deterministic baseline-record estimate.

    Makes no endpoint requests and creates no user files; tiktoken encoding
    data may be fetched/cached on first use on a cold machine.
    """

    if not model.strip():
        raise ValueError("model must not be empty")
    if samples < 1 or streams < 1:
        raise ValueError("samples/streams must be >= 1")

    tok = tokenizer or TokenizerService()
    encoding = tok.resolve_encoding(model)
    count_encoding = encoding or FALLBACK_ENCODING
    resolved_rates = resolve_rates(model)
    rates_obj = resolved_rates or FALLBACK_RATES
    rates = {
        "input_per_1k": rates_obj.input_per_1k,
        "output_per_1k": rates_obj.output_per_1k,
        "unit": "per-1K tokens",
    }
    billing_included = encoding is not None
    stages: list[StagePlan] = []

    if run_p0_gate:
        echo_prompt = "Reply with exactly this token and nothing else: PONG-deadbeef"
        stages.append(
            _stage(
                "p0.echo",
                requests=EchoProbe.samples,
                prompt_parts=[echo_prompt],
                completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["p0.echo"],
                tok=tok,
                encoding=count_encoding,
                rates=rates_obj,
                notes=["p0.echo gate; max_tokens=64"],
            )
        )

    stages.append(
        _stage(
            "p0.models",
            requests=ModelsProbe.samples,
            prompt_parts=[""],
            completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["p0.models"],
            tok=tok,
            encoding=count_encoding,
            rates=rates_obj,
            notes=[
                "GET /models catalog",
                "4096 response tokens is a finite planning assumption, not a provider limit",
            ],
        )
    )
    stages.append(
        _stage(
            "baseline.capture.responses",
            requests=1,
            prompt_parts=[
                json.dumps(
                    {
                        "model": model,
                        "input": "ping",
                        "max_output_tokens": _RESPONSES_MAX_OUTPUT_TOKENS,
                    }
                )
            ],
            completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["baseline.capture.responses"],
            tok=tok,
            encoding=count_encoding,
            rates=rates_obj,
            notes=[
                "POST /responses surface probe; direct request with no retry",
                "max_output_tokens=16 pins the provider-enforced completion ceiling",
            ],
        )
    )

    chat_messages = json.dumps([{"role": "user", "content": _CHAT_CAPTURE_PROMPT}])
    stages.append(
        _stage(
            "baseline.capture.chat",
            requests=samples,
            prompt_parts=[chat_messages] * samples,
            completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["baseline.capture.chat"],
            tok=tok,
            encoding=count_encoding,
            rates=rates_obj,
            notes=[f"{samples} chat fingerprint samples; max_tokens=8"],
        )
    )
    stream_payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": _STREAM_CAPTURE_PROMPT}],
            "max_tokens": 64,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
        }
    )
    stages.append(
        _stage(
            "baseline.capture.stream",
            requests=streams,
            prompt_parts=[stream_payload] * streams,
            completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["baseline.capture.stream"],
            tok=tok,
            encoding=count_encoding,
            rates=rates_obj,
            notes=[f"{streams} stream fingerprint samples; max_tokens=64"],
        )
    )
    self_report_messages = json.dumps(
        [{"role": "user", "content": _SELF_REPORT_CAPTURE_PROMPT}]
    )
    stages.append(
        _stage(
            "baseline.capture.self_report",
            requests=samples,
            prompt_parts=[self_report_messages] * samples,
            completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["baseline.capture.self_report"],
            tok=tok,
            encoding=count_encoding,
            rates=rates_obj,
            notes=[f"{samples} self-report samples; max_tokens=48"],
        )
    )

    if billing_included:
        recount_prompts = [
            json.dumps([{"role": "user", "content": text}]) for _, text in _RECOUNT_PROMPTS
        ]
        stages.append(
            _stage(
                "d4.recount_deviation",
                requests=RecountDeviationProbe.samples,
                prompt_parts=recount_prompts,
                completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["d4.recount_deviation"],
                tok=tok,
                encoding=count_encoding,
                rates=rates_obj,
                notes=[
                    f"billing recount ×{RecountDeviationProbe.samples}; max_tokens=64"
                ],
            )
        )
        wrap_prompts = [
            json.dumps([{"role": "user", "content": text}]) for text in _WRAP_PROMPTS
        ]
        stages.append(
            _stage(
                "d4.wrap_offset",
                requests=WrapOffsetProbe.samples,
                prompt_parts=wrap_prompts,
                completion_per_request=_RESPONSE_TOKEN_ASSUMPTIONS["d4.wrap_offset"],
                tok=tok,
                encoding=count_encoding,
                rates=rates_obj,
                notes=[f"billing wrap ×{WrapOffsetProbe.samples}; max_tokens=16"],
            )
        )

    return BaselineRecordPlan(
        model=model,
        samples=samples,
        streams=streams,
        run_p0_gate=run_p0_gate,
        billing_included=billing_included,
        encoding=encoding,
        rates=rates,
        stages=stages,
        requests=sum(stage.requests for stage in stages),
        max_requests=sum(stage.max_requests for stage in stages),
        estimated_prompt_tokens=sum(stage.estimated_prompt_tokens for stage in stages),
        estimated_completion_tokens=sum(stage.estimated_completion_tokens for stage in stages),
        estimated_usd=round(sum(stage.estimated_usd for stage in stages), 10),
        estimated_max_prompt_tokens=sum(
            stage.estimated_max_prompt_tokens for stage in stages
        ),
        estimated_max_completion_tokens=sum(
            stage.estimated_max_completion_tokens for stage in stages
        ),
        estimated_max_usd=round(sum(stage.estimated_max_usd for stage in stages), 10),
        assumptions={
            "response_tokens_per_request": dict(_RESPONSE_TOKEN_ASSUMPTIONS),
            "unbounded_provider_responses": {
                "p0.models": (
                    "4096 response tokens is a finite planning assumption; "
                    "GET /models has no provider-enforced response-token limit"
                ),
            },
            "retry_policy": (
                "retryable stages may make one retry on HTTP 429/5xx; "
                "baseline.capture.responses is one direct request"
            ),
            "retryable_stages": sorted(_RETRYABLE_STAGES),
            "count_encoding": count_encoding,
            "pricing_fallback": encoding is None or resolved_rates is None,
            "default_model_for_unknown_rates": DEFAULT_MODEL,
            "billing_omitted_reason": (
                None
                if billing_included
                else f"unknown tokenizer encoding for {model!r}; D4 uses static gates"
            ),
            "request_math": (
                "echo(1 if gate) + models(1) + responses(1) + chat(samples) + "
                "stream(streams) + self_report(samples) + "
                f"recount({RecountDeviationProbe.samples} if encoding) + "
                f"wrap({WrapOffsetProbe.samples} if encoding); worst case is nominal x2 "
                "because every retryable stage may retry once on HTTP 429/5xx "
                "(baseline.capture.responses is one direct request)"
            ),
            "budget_granularity": (
                "baseline capture loops check BudgetTracker.blocked before every request; "
                "d4.recount_deviation and d4.wrap_offset are stage-granular (checked before "
                "each probe stage) and bounded by fixed prompt lists of "
                f"{len(_RECOUNT_PROMPTS)} and {len(_WRAP_PROMPTS)} requests"
            ),
            "cost_status": (
                "nominal and retry-aware planning estimates only; BudgetTracker is the runtime cap"
            ),
        },
    )


def plan_baseline_record_dict(
    *,
    model: str,
    samples: int = 3,
    streams: int = 1,
    run_p0_gate: bool = True,
    tokenizer: TokenizerService | None = None,
) -> dict[str, Any]:
    """JSON-serializable plan dict for CLI dry-run / dump helpers."""

    return plan_baseline_record(
        model=model,
        samples=samples,
        streams=streams,
        run_p0_gate=run_p0_gate,
        tokenizer=tokenizer,
    ).to_dict()


def _stage(
    stage_id: str,
    *,
    requests: int,
    prompt_parts: list[str],
    completion_per_request: int,
    tok: TokenizerService,
    encoding: str,
    rates: ModelRates,
    notes: list[str] | None = None,
) -> StagePlan:
    if requests < 1:
        raise ValueError(f"stage {stage_id} requests must be >= 1")
    parts = list(prompt_parts)
    if len(parts) < requests:
        parts = (parts * requests)[:requests]
    elif len(parts) > requests:
        parts = parts[:requests]
    prompt_tokens = sum(tok.count(part, encoding) for part in parts)
    completion_tokens = completion_per_request * requests
    retry_multiplier = 2 if stage_id in _RETRYABLE_STAGES else 1
    max_requests = requests * retry_multiplier
    max_prompt_tokens = prompt_tokens * retry_multiplier
    max_completion_tokens = completion_tokens * retry_multiplier

    def cost(prompt: int, completion: int) -> float:
        return round(
            (prompt * rates.input_per_1k + completion * rates.output_per_1k) / 1000.0,
            10,
        )

    return StagePlan(
        id=stage_id,
        requests=requests,
        max_requests=max_requests,
        estimated_prompt_tokens=prompt_tokens,
        estimated_completion_tokens=completion_tokens,
        estimated_usd=cost(prompt_tokens, completion_tokens),
        estimated_max_prompt_tokens=max_prompt_tokens,
        estimated_max_completion_tokens=max_completion_tokens,
        estimated_max_usd=cost(max_prompt_tokens, max_completion_tokens),
        notes=list(notes or []),
    )
