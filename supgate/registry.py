"""Manifest-driven probe registry (build plan §11.3).

``manifests/probes.yaml`` owns probe ids, domains, weights, samples,
request specs, pass criteria, and 429 policy. Probes with bespoke logic
(p0.*, d6.chat.sse, d6.usage_fields, d6.vision, d6.responses_api,
d6.idempotency, later D4/D2/D8) register named runner classes; everything else
runs through the generic ``chat_completion`` runner here.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import yaml

from supgate.models import Domain, ProbeResult, SurfaceMap, Verdict
from supgate.passdsl import eval_pass
from supgate.probes.base import RunContext, probe_result
from supgate.probes.d6_protocol import ResponsesApiProbe, SseProbe, UsageFieldsProbe, VisionProbe
from supgate.probes.idempotency import IdempotencyProbe
from supgate.probes.p0 import EchoProbe, ErrorContractProbe, ModelsProbe

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_PASS = "status == 200"


class ProbeSpecError(ValueError):
    pass


class ManifestProbe:
    """A probe declared in YAML, executed by a generic runner."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.id = spec["id"]
        self.domain = Domain(spec["domain"])
        self.weight = float(spec.get("weight", 1.0))
        self.samples = int(spec.get("samples", 1))
        self.runner = spec.get("runner", "chat_completion")
        self.request = spec.get("request", {})
        self.pass_expr = spec.get("pass", DEFAULT_PASS)
        self.on_429 = spec.get("on_429", "warn_then_retry")
        self.timeout_s = float(spec.get("timeout_s", DEFAULT_TIMEOUT_S))
        self.skip_if = list(spec.get("skip_if", []))
        self.cases = list(spec.get("cases", []))
        self._validate()

    def _validate(self) -> None:
        if self.runner != "chat_completion":
            raise ProbeSpecError(f"probe {self.id}: unknown runner {self.runner!r}")
        if self.cases:
            total = sum(int(case.get("samples", 1)) for case in self.cases)
            if self.samples != total:
                raise ProbeSpecError(
                    f"probe {self.id}: samples={self.samples} but cases total {total}"
                )

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        for condition in self.skip_if:
            if condition == "no_claimed_model" and not surface.claimed_present:
                return "claimed model absent from /models catalog"
            if condition == "no_responses_api" and not surface.responses_api:
                return "Responses API not exposed"
            if condition == "no_messages_api" and not surface.messages_api:
                return "Messages API not exposed"
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        successes = 0
        rate_limited = 0
        server_errors = 0
        notes: list[str] = []
        case_specs = self.cases or [
            {"request": self.request, "pass": self.pass_expr, "samples": self.samples}
        ]
        for case in case_specs:
            case_pass = case.get("pass", self.pass_expr)
            for i in range(int(case.get("samples", 1))):
                payload = self._payload(ctx, case.get("request", self.request), i)
                outcome, sample_notes = await self._sample(ctx, payload, i, case_pass)
                notes.extend(sample_notes)
                if outcome == "pass":
                    successes += 1
                elif outcome == "rate_limited":
                    rate_limited += 1
                elif outcome == "server_error":
                    server_errors += 1
        attempts = self.samples
        # §10: persistent 429/5xx after one retry is an explicit WARN, never a
        # silent FAIL — matching the custom P0/D6 probes. A single transport or
        # pass-criteria failure keeps the FAIL so real defects still surface.
        if attempts > 0 and successes == 0 and rate_limited + server_errors == attempts:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.WARN, score=50.0,
                successes=0, attempts=attempts,
                notes=notes + [_retry_summary(rate_limited, server_errors)],
            )
        return probe_result(self.id, self.domain, successes=successes, attempts=attempts, notes=notes, weight=self.weight)

    def _payload(self, ctx: RunContext, request_spec: dict[str, Any], i: int) -> dict[str, Any]:
        def fill(value: Any) -> Any:
            if isinstance(value, str):
                return PLACEHOLDER.sub(
                    lambda m: str({
                        "model": ctx.model, "i": i, "nonce": "supgate" + str(i),
                    }[m.group(1)]),
                    value,
                )
            if isinstance(value, list):
                return [fill(item) for item in value]
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            return value

        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
            "temperature": 0,
        }
        payload.update(fill(request_spec))
        return payload

    async def _sample(
        self, ctx: RunContext, payload: dict[str, Any], i: int, pass_expr: str
    ) -> tuple[str, list[str]]:
        """Run one sample with one backoff retry on 429/5xx (§10 global defaults)."""

        for attempt in range(2):
            try:
                response = await ctx.request(
                    self.id, "POST", "/chat/completions", payload=payload, timeout_s=self.timeout_s
                )
            except Exception as exc:  # noqa: BLE001
                return "fail", [f"sample {i}: transport error: {exc}"]
            if response.status_code == 429:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                return "rate_limited", [f"sample {i}: 429 after retry"]
            if response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                return "server_error", [
                    f"sample {i}: server error (status {response.status_code}) after retry — Warn per §10"
                ]
            env = _env_for(response, payload)
            passed = eval_pass(pass_expr, env)
            if not passed:
                return "fail", [f"sample {i}: pass criteria not met ({pass_expr})"]
            return "pass", []
        return "fail", [f"sample {i}: exhausted retries"]


def _retry_summary(rate_limited: int, server_errors: int) -> str:
    """§10 summary note when every sample hit a persistent 429/5xx after retry."""

    if rate_limited == 0:
        return "all samples returned server error (5xx) after retry — Warn per §10, endpoint degraded"
    if server_errors == 0:
        return "all samples rate-limited (429) — Warn per §2, rerun with backoff or higher-quota key"
    return "all samples rate-limited (429) or server error (5xx) after retry — Warn per §10"


def _env_for(response, payload: dict[str, Any]) -> dict[str, Any]:
    env: dict[str, Any] = {"status": response.status_code}
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    env["error"] = error
    if payload.get("stream"):
        env["raw_content"] = response.text
        return env
    choices = body.get("choices") if isinstance(body, dict) else None
    env["choices"] = len(choices) if choices else None
    usage = body.get("usage") if isinstance(body, dict) else None
    env["usage"] = usage
    if not choices:
        return env
    first = choices[0]
    env["finish_reason"] = first.get("finish_reason")
    message = first.get("message", {}) if isinstance(first, dict) else {}
    env["content"] = message.get("content") or ""
    env["tool_calls"] = message.get("tool_calls")
    return env


def load_probes(path: str | Any) -> list[Any]:
    """Load a YAML manifest into a list of probe objects (manifest + custom)."""

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or "probes" not in doc:
        raise ProbeSpecError(f"manifest {path}: missing 'probes' list")
    probes: list[Any] = []
    for spec in doc["probes"]:
        runner = spec.get("runner", "chat_completion")
        custom = CUSTOM_RUNNERS.get(spec["id"])
        if custom is not None:
            probes.append(custom())
        elif runner == "chat_completion":
            probes.append(ManifestProbe(spec))
        else:
            raise ProbeSpecError(f"probe {spec['id']}: unknown runner {runner!r} and no custom class")
    return probes


def load_manifest_version(path: str | Any) -> str:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return str(doc.get("version", "0"))


CUSTOM_RUNNERS: dict[str, type] = {
    "p0.echo": EchoProbe,
    "p0.models": ModelsProbe,
    "p0.error_contract": ErrorContractProbe,
    "d6.chat.sse": SseProbe,
    "d6.usage_fields": UsageFieldsProbe,
    "d6.vision": VisionProbe,
    "d6.responses_api": ResponsesApiProbe,
    "d6.idempotency": IdempotencyProbe,
}
