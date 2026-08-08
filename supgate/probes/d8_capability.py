"""D8 tool & capability contract probes (build plan §10.6; weekend plan U2).

Ten probes, one manifest-addressable class per exact id, all Domain.D8:

- ``d8.tools.auto``        ToolAutoProbe         — tool_choice auto
- ``d8.tools.forced``      ToolForcedProbe       — tool_choice {type: function}
- ``d8.tools.required``    ToolRequiredProbe     — tool_choice required
- ``d8.tools.parallel``    ToolParallelProbe     — two calls in one turn
- ``d8.tools.multiturn``   ToolMultiturnProbe    — tool result → final answer
- ``d8.tools.stream``      ToolStreamProbe       — streamed tool_call deltas
- ``d8.structured_strict`` StructuredStrictProbe — json_object + strict json_schema
- ``d8.reasoning``         ReasoningProbe        — reasoning family multistep + accounting
- ``d8.cutoff_battery``    CutoffBatteryProbe    — fact battery vs baseline pattern
- ``d8.prompt_caching``    PromptCachingProbe    — shared-prefix cache fields + TTFT

Verdict policy (shared with the D4/D6 probes, §10): transport errors FAIL with
their evidence saved; a persistent 429/5xx after one retry WARNs; a clean 4xx
SKIPs only when the claimed model family (or a matched baseline) does not
claim the capability, and FAILs when it does; a 200 that silently omits the
required capability is a hard FAIL. All HTTP flows through
``RunContext.request``/``stream`` so redaction and the reproducible curl stay
at the single evidence choke point.

Optional baseline fingerprints consumed here (recorded on official
endpoints; absent fingerprints fall back to the static family tables):

- ``tools``                {"supported": bool}      tool-call claim override
- ``structured_output``    {"supported": bool}      structured-output claim override
- ``reasoning``            {"supported": bool}      reasoning-family claim override
- ``cutoff_battery``       {"expected": [bool,...]} per-fact answer pattern, or
                           {"pattern": "YYYYYYNNNN"} regex over the observed
                           yes/no sequence
- ``usage_schema``         {"cached_tokens": bool}  caching claim override
                           (docs/06 §5.4 shape)
"""

from __future__ import annotations

import json
import re
from typing import Any

from supgate.models import Domain, ProbeResult, SurfaceMap, Verdict
from supgate.probes.base import (
    RateLimitError,
    RunContext,
    ServerError,
    probe_result,
    probe_result_with_warn,
    request_with_retry,
    warn_result,
)
from supgate.probes.d4_billing import usage_schema_for

# Static claim tables: which model families claim which capability. A clean
# 4xx SKIPs only for families that do not claim the capability (§10). gpt-5*
# is a reasoning family here; the D4 usage-schema table stays the single
# authority for usage-details claims (unknown families there SKIP caching),
# so the gpt-5 reasoning claim never contradicts D4 (consistency, docs/06 §5.4).
_TOOL_PREFIXES: tuple[str, ...] = ("gpt-", "o1", "o3", "o4")
_STRUCTURED_PREFIXES: tuple[str, ...] = ("gpt-4", "gpt-5", "o1", "o3", "o4")
_REASONING_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5")

_CLEAN_4XX = (400, 404, 405, 422)

_WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

# d8.prompt_caching shared prefix: ~600 words, well past the >= 300-token bar
# (docs/06 §5.4 request shapes 1-3 use the same style of prefix).
_CACHE_PREFIX = " ".join(f"reference token {i}" for i in range(300))

# d8.prompt_caching TTFT rule: a repeat must be strictly faster than the cold
# call — an equal or slower repeat means the cache was not honored (no loose
# jitter tolerance; cache claims are cost claims).

# d8.cutoff_battery canonical compact fact battery. The verdict compares the
# observed yes/no answers against the baseline's recorded pattern only; the
# probe never infers identity from prose.
_CUTOFF_FACTS: tuple[tuple[str, str], ...] = (
    ("us_independence_1776", "Was the United States Declaration of Independence adopted in 1776?"),
    ("moon_landing_1969", "Did Apollo 11 land humans on the Moon in 1969?"),
    ("berlin_wall_1989", "Did the Berlin Wall fall in 1989?"),
    ("covid_pandemic_2020", "Did the WHO declare COVID-19 a pandemic in March 2020?"),
    ("tokyo_olympics_2021", "Were the 2020 Summer Olympics held in 2021 in Tokyo?"),
    ("chatgpt_nov_2022", "Was ChatGPT released to the public in November 2022?"),
    ("eclipse_apr_2024", "Did a total solar eclipse cross North America on 8 April 2024?"),
    ("synthetic_moon_orbit", "Does the Moon orbit the Earth twice every day?"),
    ("synthetic_largest_planet", "Is Earth the largest planet in the solar system?"),
    ("synthetic_horse_flight", "Was the first powered flight made by a horse?"),
)

_CUTOFF_WARN_RATIO = 0.25


# ---------- claim helpers ----------


def _prefix_in(name: str, prefixes: tuple[str, ...]) -> bool:
    lowered = (name or "").strip().lower()
    return any(lowered.startswith(prefix) for prefix in prefixes)


def _claims_tool_calls(ctx: RunContext) -> bool:
    baseline = ctx.selected_baseline
    if baseline is not None:
        fp = baseline.fingerprints.get("tools")
        if isinstance(fp, dict) and "supported" in fp:
            return bool(fp["supported"])
    return _prefix_in(ctx.model, _TOOL_PREFIXES)


def _claims_structured(ctx: RunContext) -> bool:
    baseline = ctx.selected_baseline
    if baseline is not None:
        fp = baseline.fingerprints.get("structured_output")
        if isinstance(fp, dict) and "supported" in fp:
            return bool(fp["supported"])
    return _prefix_in(ctx.model, _STRUCTURED_PREFIXES)


def _claims_reasoning(ctx: RunContext) -> bool:
    if _prefix_in(ctx.model, _REASONING_PREFIXES):
        return True
    baseline = ctx.selected_baseline
    if baseline is not None:
        if _prefix_in(baseline.model, _REASONING_PREFIXES):
            return True
        fp = baseline.fingerprints.get("reasoning")
        if isinstance(fp, dict) and "supported" in fp:
            return bool(fp["supported"])
    return False


def _caching_claim(ctx: RunContext) -> bool | None:
    """True = family claims prompt caching, False = known non-caching family,
    None = unknown family (skip). Baseline ``usage_schema`` overrides the
    static table (docs/06 §5.4)."""
    baseline = ctx.selected_baseline
    if baseline is not None:
        fp = baseline.fingerprints.get("usage_schema")
        if isinstance(fp, dict) and "cached_tokens" in fp:
            return bool(fp["cached_tokens"])
    schema = usage_schema_for(ctx.model)
    if schema is None:
        return None
    return bool(schema["cached_tokens"])


def _clean_4xx_result(probe_id: str, status: int, claiming: bool, capability: str) -> ProbeResult:
    """§10: clean 4xx FAILs a claiming family, SKIPs a non-claiming one."""
    if claiming:
        return probe_result(
            probe_id, Domain.D8, successes=0, attempts=1,
            notes=[f"claims {capability} support but returned clean status {status} — capability FAIL"],
        )
    return ProbeResult(
        probe_id=probe_id, domain=Domain.D8, verdict=Verdict.SKIP, score=0.0,
        successes=0, attempts=0,
        notes=[f"{capability} unsupported (status {status}) for a model that does not claim it — skipped"],
    )


# ---------- response helpers ----------


def _json(response: Any) -> dict | None:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - any parse failure means no usable body
        return None


def _content_of(body: dict | None) -> str:
    if not body:
        return ""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def _tool_calls(body: dict | None) -> list[Any]:
    if not body:
        return []
    try:
        calls = body["choices"][0]["message"]["tool_calls"]
    except (KeyError, IndexError, TypeError):
        return []
    return calls if isinstance(calls, list) else []


def _call_city(call: Any) -> Any:
    if not isinstance(call, dict):
        return None
    try:
        return json.loads(call["function"]["arguments"]).get("city")
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def _validate_call(call: Any, cities: tuple[str, ...]) -> tuple[bool, str]:
    """Shape + name + JSON-parseable arguments with the expected city."""
    if not isinstance(call, dict):
        return False, "tool_call is not an object"
    if not isinstance(call.get("id"), str) or not call["id"]:
        return False, "tool_call missing id"
    if call.get("type") != "function":
        return False, f"tool_call type {call.get('type')!r} != 'function'"
    fn = call.get("function")
    if not isinstance(fn, dict):
        return False, "tool_call function is not an object"
    if fn.get("name") != "get_weather":
        return False, f"tool name {fn.get('name')!r} != 'get_weather'"
    raw_args = fn.get("arguments")
    if not isinstance(raw_args, str):
        return False, "tool arguments are not a JSON string"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return False, "tool arguments are not valid JSON"
    if not isinstance(args, dict):
        return False, "tool arguments did not parse to an object"
    if args.get("city") not in cities:
        return False, f"tool argument city {args.get('city')!r} not in {cities}"
    return True, "tool call shape, name, and arguments valid"


def _parse_stream_tool_calls(text: str) -> list[dict[str, Any]]:
    """Reassemble index-keyed tool_call deltas from a buffered SSE body.

    Returns one entry per tool index with the accumulated id/type/name and
    the concatenated arguments; an empty list when the stream carried no
    tool_call deltas at all.
    """

    by_index: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        parts = delta.get("tool_calls")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("index"), int):
                continue
            index = part["index"]
            if index not in by_index:
                by_index[index] = {"index": index, "id": None, "type": None, "name": None, "arguments": ""}
                order.append(index)
            entry = by_index[index]
            if isinstance(part.get("id"), str) and part["id"]:
                entry["id"] = part["id"]
            if isinstance(part.get("type"), str) and part["type"]:
                entry["type"] = part["type"]
            fn = part.get("function")
            if isinstance(fn, dict):
                if isinstance(fn.get("name"), str) and fn["name"]:
                    entry["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    entry["arguments"] += fn["arguments"]
    return [by_index[i] for i in order]


def _schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    """Validate a parsed value against a small JSON-schema subset (object,
    string, integer, number, boolean, array, enum) — no external dependency.
    ``additionalProperties: false`` rejects extra keys exactly like strict
    json_schema mode does."""

    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return [f"expected object, got {type(value).__name__}"]
        errors: list[str] = []
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"missing required key {name!r}")
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties") is False:
                    errors.append(f"unexpected key {key!r} (additionalProperties=false)")
                continue
            errors.extend(f"{key}.{error}" for error in _schema_errors(item, properties[key]))
        return errors
    if kind == "string":
        return [] if isinstance(value, str) else [f"expected string, got {type(value).__name__}"]
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"expected integer, got {type(value).__name__}"]
        return []
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"expected number, got {type(value).__name__}"]
        return []
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"expected boolean, got {type(value).__name__}"]
    if kind == "array":
        if not isinstance(value, list):
            return [f"expected array, got {type(value).__name__}"]
        items = schema.get("items", {})
        errors = []
        for i, item in enumerate(value):
            errors.extend(f"[{i}].{error}" for error in _schema_errors(item, items))
        return errors
    enum = schema.get("enum")
    if isinstance(enum, list):
        return [] if value in enum else [f"{value!r} not in enum {enum!r}"]
    return [f"unsupported schema type {kind!r}"]


def _extract_final_number(text: str) -> int | None:
    numbers = re.findall(r"\d+", text or "")
    if not numbers:
        return None
    try:
        return int(numbers[-1])
    except ValueError:
        return None


def _reasoning_tokens(usage: Any) -> Any:
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        return None
    return details.get("reasoning_tokens")


def _parse_yes_no(text: str) -> bool | None:
    lowered = (text or "").lower()
    if re.search(r"\byes\b", lowered):
        return True
    if re.search(r"\bno\b", lowered):
        return False
    return None


# ---------- tool probes (d8.tools.*) ----------


class _ToolProbeBase:
    """Shared single-turn tool probe: request once, validate call shape/name/JSON args."""

    domain = Domain.D8
    weight = 1.0
    samples = 1
    _mode = "tool"
    _prompt = "Use the get_weather tool to get the current weather for Jakarta."
    _choice: str | dict[str, Any] = "auto"
    _cities: tuple[str, ...] = ("Jakarta",)

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        try:
            response = await request_with_retry(
                ctx, self.id, "POST", "/chat/completions", payload=self._payload(ctx)
            )
        except (RateLimitError, ServerError) as exc:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[exc.note], error=str(exc), warn_failures=True,
            )
        except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL with evidence
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"transport error: {exc}"], transport_failures=True,
            )
        body = _json(response)
        if response.status_code == 200:
            ok, note = self._validate(ctx, body)
            result = probe_result(self.id, self.domain, successes=1 if ok else 0, attempts=1, notes=[note])
            result.metrics = {"tool_call": {**self._metrics(ctx, body), "ok": ok}}
            return result
        if response.status_code in _CLEAN_4XX:
            result = _clean_4xx_result(self.id, response.status_code, _claims_tool_calls(ctx), "tool calling")
            result.metrics = {"tool_call": self._metrics(ctx, body)}
            return result
        result = probe_result(
            self.id, self.domain, successes=0, attempts=1,
            notes=[f"unexpected status {response.status_code} for tool request"],
        )
        result.metrics = {"tool_call": self._metrics(ctx, body)}
        return result

    def _payload(self, ctx: RunContext) -> dict[str, Any]:
        return {
            "model": ctx.model,
            "messages": [{"role": "user", "content": self._prompt}],
            "tools": [_WEATHER_TOOL],
            "tool_choice": self._choice,
            "max_tokens": 64,
            "temperature": 0,
        }

    def _validate(self, ctx: RunContext, body: dict | None) -> tuple[bool, str]:
        calls = _tool_calls(body)
        if not calls:
            return False, "200 without the required tool call — silent stripping"
        for call in calls:
            ok, note = _validate_call(call, self._cities)
            if not ok:
                return False, note
        return True, f"tool call valid: name get_weather, arguments JSON with city in {self._cities}"

    def _metrics(self, ctx: RunContext, body: dict | None) -> dict[str, Any]:
        calls = _tool_calls(body)
        cities = sorted({_call_city(call) for call in calls if isinstance(call, dict)} - {None})
        return {
            "mode": self._mode,
            "claiming": _claims_tool_calls(ctx),
            "calls": len(calls),
            "names": [call.get("function", {}).get("name") for call in calls if isinstance(call, dict)],
            "cities": cities,
        }


class ToolAutoProbe(_ToolProbeBase):
    """d8.tools.auto — tool_choice auto must still produce a schema-valid call."""

    id = "d8.tools.auto"
    _mode = "auto"
    _choice = "auto"


class ToolForcedProbe(_ToolProbeBase):
    """d8.tools.forced — a forced function choice must be honored exactly."""

    id = "d8.tools.forced"
    _mode = "forced"
    _choice = {"type": "function", "function": {"name": "get_weather"}}


class ToolRequiredProbe(_ToolProbeBase):
    """d8.tools.required — tool_choice required must yield at least one call."""

    id = "d8.tools.required"
    _mode = "required"
    _choice = "required"


class ToolParallelProbe(_ToolProbeBase):
    """d8.tools.parallel — two schema-valid calls in one turn, both cities."""

    id = "d8.tools.parallel"
    _mode = "parallel"
    _prompt = "Use the get_weather tool to get the current weather for Jakarta and for Tokyo."
    _cities = ("Jakarta", "Tokyo")

    def _validate(self, ctx: RunContext, body: dict | None) -> tuple[bool, str]:
        calls = _tool_calls(body)
        if not calls:
            return False, "200 without parallel tool calls — silent stripping"
        if len(calls) < 2:
            return False, f"expected 2 parallel tool calls in one turn, got {len(calls)}"
        seen: set[str] = set()
        for call in calls:
            ok, note = _validate_call(call, self._cities)
            if not ok:
                return False, note
            seen.add(_call_city(call))
        if seen != set(self._cities):
            return False, f"parallel calls covered {sorted(seen)} but expected {sorted(self._cities)}"
        return True, f"{len(calls)} parallel tool calls covering {sorted(seen)}"


class ToolMultiturnProbe(_ToolProbeBase):
    """d8.tools.multiturn — tool call, then the tool result fed back; the
    final answer must reference the result (the agent loop works end to end)."""

    id = "d8.tools.multiturn"
    _mode = "multiturn"
    _prompt = "Use the get_weather tool to get the current weather for Jakarta, then report it."

    async def run(self, ctx: RunContext) -> ProbeResult:
        turn1_payload = self._payload(ctx)
        try:
            turn1 = await request_with_retry(
                ctx, self.id, "POST", "/chat/completions", payload=turn1_payload
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
        body1 = _json(turn1)
        if turn1.status_code != 200:
            if turn1.status_code in _CLEAN_4XX:
                return _clean_4xx_result(self.id, turn1.status_code, _claims_tool_calls(ctx), "tool calling")
            return probe_result(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"unexpected status {turn1.status_code} for tool request"],
            )
        calls = _tool_calls(body1)
        if not calls:
            result = probe_result(
                self.id, self.domain, successes=0, attempts=1,
                notes=["200 without the required tool call — silent stripping"],
            )
            result.metrics = {"tool_call": {"mode": self._mode, "turn": 1, "ok": False}}
            return result
        ok, note = _validate_call(calls[0], self._cities)
        if not ok:
            result = probe_result(self.id, self.domain, successes=0, attempts=1, notes=[note])
            result.metrics = {"tool_call": {"mode": self._mode, "turn": 1, "ok": False}}
            return result

        body2, status2, error2, warn2, transport2 = await self._second_turn(ctx, turn1_payload, calls[0])
        if transport2:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[error2 or "transport error"], transport_failures=True,
            )
        if warn2:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[error2 or "retryable failure"], warn_failures=True,
            )
        if status2 != 200:
            if status2 in _CLEAN_4XX:
                return _clean_4xx_result(self.id, status2, _claims_tool_calls(ctx), "tool calling")
            return probe_result(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"unexpected status {status2} for tool-result request"],
            )
        final_text = _content_of(body2)
        result_values = ["32", "sunny"]
        referenced = bool(final_text.strip()) and any(v in final_text for v in result_values)
        ok2 = referenced
        result = probe_result(
            self.id, self.domain, successes=1 if ok2 else 0, attempts=1,
            notes=[(
                "final answer references the tool result" if ok2
                else f"final answer did not reference the tool result: {final_text[:200]!r}"
            )],
        )
        result.metrics = {
            "tool_call": {
                "mode": self._mode, "turn": 1, "ok": True,
                "calls": len(calls), "final_referenced": referenced,
                "final_text": final_text[:200],
            }
        }
        return result

    async def _second_turn(
        self, ctx: RunContext, turn1_payload: dict[str, Any], call: dict[str, Any]
    ) -> tuple[dict | None, int, str | None, bool, bool]:
        """Feed the tool result back; returns (body, status, note, warn, transport)."""

        result_json = json.dumps({"temperature": 32, "condition": "sunny"}, separators=(",", ":"))
        payload = {
            "model": ctx.model,
            "messages": [
                *turn1_payload["messages"],
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": result_json},
            ],
            "tools": turn1_payload["tools"],
            "tool_choice": "none",
            "max_tokens": 64,
            "temperature": 0,
        }
        try:
            response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
        except (RateLimitError, ServerError) as exc:
            return None, 0, exc.note, True, False
        except Exception as exc:  # noqa: BLE001
            return None, 0, f"transport error: {exc}", False, True
        return _json(response), response.status_code, None, False, False


class ToolStreamProbe(_ToolProbeBase):
    """d8.tools.stream — streamed tool_call deltas reassembled and validated."""

    id = "d8.tools.stream"
    _mode = "stream"

    def _payload(self, ctx: RunContext) -> dict[str, Any]:
        return {**super()._payload(ctx), "stream": True}

    async def run(self, ctx: RunContext) -> ProbeResult:
        try:
            result = await ctx.stream(
                self.id, "/chat/completions", payload=self._payload(ctx)
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
        if result.status != 200:
            if result.status in _CLEAN_4XX:
                return _clean_4xx_result(self.id, result.status, _claims_tool_calls(ctx), "tool calling")
            return probe_result(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"unexpected status {result.status} for tool stream"],
            )
        entries = _parse_stream_tool_calls(result.body)
        if not entries:
            return probe_result(
                self.id, self.domain, successes=0, attempts=1,
                notes=["200 stream carried no tool_call deltas — silent stripping"],
            )
        for entry in entries:
            call = {
                "id": entry["id"],
                "type": entry["type"],
                "function": {"name": entry["name"], "arguments": entry["arguments"]},
            }
            ok, note = _validate_call(call, self._cities)
            if not ok:
                result_out = probe_result(
                    self.id, self.domain, successes=0, attempts=1,
                    notes=[f"streamed tool call invalid: {note}"],
                )
                result_out.metrics = {"tool_call": {"mode": self._mode, "ok": False, "reassembled": entries}}
                return result_out
        result_out = probe_result(
            self.id, self.domain, successes=1, attempts=1,
            notes=[f"streamed tool_call deltas reassembled and valid ({len(entries)} call)"],
        )
        result_out.metrics = {"tool_call": {"mode": self._mode, "ok": True, "reassembled": entries}}
        return result_out


# ---------- d8.structured_strict ----------

_JSON_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "value": {"type": "integer"},
    },
    "required": ["name", "value"],
    "additionalProperties": False,
}

_STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "report": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "rating": {"type": "integer"},
                "ok": {"type": "boolean"},
            },
            "required": ["title", "rating", "ok"],
            "additionalProperties": False,
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["report", "tags"],
    "additionalProperties": False,
}


class StructuredStrictProbe:
    """d8.structured_strict ×2 — json_object, then json_schema strict; the
    output must validate exactly (keys, types, additionalProperties=false)."""

    id = "d8.structured_strict"
    domain = Domain.D8
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        cases: tuple[tuple[str, dict[str, Any], str, dict[str, Any]], ...] = (
            (
                "json_object",
                {"type": "json_object"},
                'Return JSON with exactly the keys "name" (string) and "value" (integer).',
                _JSON_OBJECT_SCHEMA,
            ),
            (
                "json_schema",
                {
                    "type": "json_schema",
                    "json_schema": {"name": "quality_report", "strict": True, "schema": _STRICT_SCHEMA},
                },
                "Return a quality report matching the provided JSON schema exactly.",
                _STRICT_SCHEMA,
            ),
        )
        notes: list[str] = []
        case_metrics: list[dict[str, Any]] = []
        warn_failures = False
        transport_failures = False
        hard_failure = False
        skipped: tuple[str, int] | None = None
        for name, response_format, prompt, schema in cases:
            payload = {
                "model": ctx.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": response_format,
                "max_tokens": 128,
                "temperature": 0,
            }
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"{name}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001
                transport_failures = True
                notes.append(f"{name}: transport error: {exc}")
                continue
            if response.status_code in _CLEAN_4XX:
                if _claims_structured(ctx):
                    hard_failure = True
                    notes.append(f"{name}: claims structured output but clean status {response.status_code} — capability FAIL")
                elif skipped is None:
                    skipped = (name, response.status_code)
                    notes.append(
                        f"{name}: structured output unsupported (status {response.status_code}) "
                        "for a model that does not claim it — skipped"
                    )
                continue
            if response.status_code != 200:
                hard_failure = True
                notes.append(f"{name}: unexpected status {response.status_code}")
                continue
            text = _content_of(_json(response))
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            errors = ["content is not valid JSON"] if parsed is None else _schema_errors(parsed, schema)
            ok = not errors
            case_metrics.append({"name": name, "parsed": parsed, "errors": errors, "ok": ok})
            if ok:
                notes.append(f"{name}: output validates exactly against the schema")
            else:
                notes.append(f"{name}: output failed validation: {'; '.join(errors)}")

        metrics = {
            "structured": {
                "claimed": _claims_structured(ctx),
                "cases": case_metrics,
            }
        }
        # A 200 with invalid/non-schema output is always a hard FAIL, even
        # when the other case was skipped: SKIP only when no case produced a
        # hard failure (a skipped case never masks a real capability defect).
        if hard_failure or any(not case["ok"] for case in case_metrics):
            result = ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
                successes=sum(1 for case in case_metrics if case["ok"]),
                attempts=self.samples, notes=notes,
            )
            result.metrics = metrics
            return result
        if skipped is not None:
            result = ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0, notes=notes,
            )
            result.metrics = metrics
            return result
        if transport_failures:
            result = probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=self.samples,
                notes=notes, transport_failures=True,
            )
            result.metrics = metrics
            return result
        if warn_failures:
            result = probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=self.samples,
                notes=notes, warn_failures=True,
            )
            result.metrics = metrics
            return result
        result = probe_result(
            self.id, self.domain, successes=self.samples, attempts=self.samples,
            notes=notes,
        )
        result.metrics = metrics
        return result


# ---------- d8.reasoning ----------


class ReasoningProbe:
    """d8.reasoning — reasoning-family gated (o1/o3/o4/gpt-5 or baseline
    claim); accepts reasoning params, solves a deterministic multistep task,
    and reports sane reasoning-token accounting. A family that legitimately
    does not claim reasoning SKIPs before any request, so the probe never
    emits FAIL/WARN evidence that would contradict the D4 usage-schema table
    for that family; when the probe does run, bad accounting (missing or
    out-of-range ``reasoning_tokens``) stays an explicit FAIL."""

    id = "d8.reasoning"
    domain = Domain.D8
    weight = 1.0
    samples = 1
    _ANSWER = 6
    _PROMPT = (
        "Solve this step by step. Start with 7. Add 5. Multiply the result by 2. "
        "Subtract 6. Divide the result by 3. What is the final number? "
        "Reply with only the number."
    )

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        if not _claims_reasoning(ctx):
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=["reasoning family not claimed (o1/o3/o4/gpt-5 model or baseline reasoning claim) — skipped"],
            )
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": self._PROMPT}],
            "max_tokens": 1024,
            "temperature": 0,
            "reasoning_effort": "low",
        }
        try:
            response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
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
        if response.status_code != 200:
            if response.status_code in _CLEAN_4XX:
                return _clean_4xx_result(self.id, response.status_code, True, "reasoning")
            return probe_result(
                self.id, self.domain, successes=0, attempts=1,
                notes=[f"unexpected status {response.status_code} for reasoning request"],
            )
        content = _content_of(body)
        answer = _extract_final_number(content)
        answer_ok = answer == self._ANSWER
        usage = body.get("usage") if body else None
        reasoning = _reasoning_tokens(usage)
        completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
        accounting_ok = (
            isinstance(reasoning, int)
            and isinstance(completion, int)
            and 0 <= reasoning <= completion
        )
        notes: list[str] = []
        if not answer_ok:
            notes.append(f"reasoning answer {answer!r} != expected {self._ANSWER} (content {content[:120]!r})")
        if not accounting_ok:
            notes.append(f"reasoning token accounting unsane: reasoning_tokens={reasoning!r} completion_tokens={completion!r}")
        ok = answer_ok and accounting_ok
        if ok:
            notes.append(
                "reasoning params accepted; deterministic multistep answer correct; "
                "reasoning_tokens sane (0 <= reasoning <= completion)"
            )
        result = probe_result(self.id, self.domain, successes=1 if ok else 0, attempts=1, notes=notes)
        result.metrics = {
            "reasoning": {
                "claimed": True,
                "answer": answer,
                "expected": self._ANSWER,
                "answer_ok": answer_ok,
                "reasoning_tokens": reasoning,
                "completion_tokens": completion,
                "accounting_ok": accounting_ok,
            }
        }
        return result


# ---------- d8.cutoff_battery ----------


def _flag_char(char: str) -> bool | None:
    mapping = {"y": True, "t": True, "1": True, "n": False, "f": False, "0": False}
    return mapping.get(char.strip().lower())


def _coerce_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip():
        lowered = value.strip().lower()
        if lowered in ("yes", "true", "y", "t", "1"):
            return True
        if lowered in ("no", "false", "n", "f", "0"):
            return False
    return None


def _cutoff_expected(ctx: RunContext) -> tuple[list[bool] | None, re.Pattern | None]:
    """Baseline cutoff_battery expectation: (per-fact flags, or a pattern)."""

    baseline = ctx.selected_baseline
    if baseline is None:
        return None, None
    fp = baseline.fingerprints.get("cutoff_battery")
    if not isinstance(fp, dict):
        return None, None
    raw = fp.get("expected")
    if isinstance(raw, list):
        flags = [_coerce_flag(value) for value in raw[: len(_CUTOFF_FACTS)]]
        if flags and all(flag is not None for flag in flags):
            return flags, None
    if isinstance(raw, str) and raw:
        flags = [_flag_char(char) for char in raw[: len(_CUTOFF_FACTS)]]
        if flags and all(flag is not None for flag in flags):
            return flags, None
    raw_pattern = fp.get("pattern")
    if isinstance(raw_pattern, str) and raw_pattern:
        try:
            return None, re.compile(raw_pattern)
        except re.error:
            return None, None
    return None, None


class CutoffBatteryProbe:
    """d8.cutoff_battery — deterministic compact fact battery scored against
    the selected baseline's ``cutoff_battery`` pattern. No baseline, or a
    baseline without the fingerprint, SKIPs; the mismatch ratio is exposed in
    metrics. Identity is never inferred from prose — only the pattern
    comparison decides. Incomplete observations (facts that never returned
    200) are counted only at their own position against the truncated
    expected, so flags and pattern modes always agree."""

    id = "d8.cutoff_battery"
    domain = Domain.D8
    weight = 1.0
    samples = len(_CUTOFF_FACTS)

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        baseline = ctx.selected_baseline
        if baseline is None:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=["no selected baseline — cutoff battery cannot be scored"],
            )
        expected, pattern = _cutoff_expected(ctx)
        if expected is None and pattern is None:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=["baseline carries no cutoff_battery fingerprint (expected flags or pattern) — skipped"],
            )
        facts = _CUTOFF_FACTS[: len(expected)] if expected is not None else _CUTOFF_FACTS
        # per_position tracks WHICH fact each answer belongs to, so a fact
        # that never returned 200 is simply not counted (incomplete
        # observations never shift the comparison against the expected
        # pattern — flags and pattern modes agree by construction).
        per_position: dict[int, bool | None] = {}
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        for i, (fact_id, question) in enumerate(facts):
            payload = {
                "model": ctx.model,
                "messages": [
                    {"role": "user", "content": f"{question} Reply with exactly 'yes' or 'no'."}
                ],
                "max_tokens": 8,
                "temperature": 0,
            }
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"{fact_id}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001
                transport_failures = True
                notes.append(f"{fact_id}: transport error: {exc}")
                continue
            if response.status_code != 200:
                notes.append(f"{fact_id}: unexpected status {response.status_code}")
                continue
            per_position[i] = _parse_yes_no(_content_of(_json(response)))

        if transport_failures:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=len(per_position),
                notes=notes, transport_failures=True,
            )
        if warn_failures:
            return warn_result(self.id, self.domain, notes=notes, attempts=len(per_position))

        observed = [per_position.get(i) for i in range(len(facts))]
        observed_positions = sorted(per_position)

        flags: list[bool] | None = expected
        mode = "expected"
        if flags is None and pattern is not None:
            plain = len(pattern.pattern) == len(facts) and all(c in "YN" for c in pattern.pattern)
            if plain:
                flags = [c == "Y" for c in pattern.pattern]
                mode = "pattern"
        if flags is None and pattern is not None:
            sequence = "".join(
                "Y" if per_position.get(i) is True else ("N" if per_position.get(i) is False else "?")
                for i in range(len(facts))
            )
            matched = pattern.fullmatch(sequence) is not None
            mismatches = 0 if matched else len(observed_positions)
            ratio = 0.0 if matched else 1.0
            per_item = [
                {"fact": fid, "observed": per_position.get(i), "expected": None}
                for i, (fid, _) in enumerate(facts)
            ]
            return self._verdict(
                ctx, baseline, facts, observed, None, mismatches, ratio, mode, per_item,
                len(observed_positions), notes,
            )

        # flags mode: count only positions actually observed, each against
        # the expected flag at that same fact position.
        mismatches = sum(
            1 for i in observed_positions if per_position[i] is None or per_position[i] != flags[i]
        )
        ratio = mismatches / len(observed_positions) if observed_positions else 0.0
        per_item = [
            {"fact": fid, "observed": per_position.get(i), "expected": flags[i]}
            for i, (fid, _) in enumerate(facts)
        ]
        return self._verdict(
            ctx, baseline, facts, observed, flags, mismatches, ratio, mode, per_item,
            len(observed_positions), notes,
        )

    def _verdict(
        self,
        ctx: RunContext,
        baseline: Any,
        facts: list[tuple[str, str]],
        observed: list[bool | None],
        flags: list[bool] | None,
        mismatches: int,
        ratio: float,
        mode: str,
        per_item: list[dict[str, Any]],
        observed_count: int,
        notes: list[str],
    ) -> ProbeResult:
        metrics = {
            "cutoff": {
                "baseline_id": baseline.baseline_id,
                "mode": mode,
                "battery": [fact_id for fact_id, _ in facts],
                "expected": flags,
                "observed": observed,
                "mismatch_ratio": round(ratio, 3),
                "mismatches": mismatches,
                "per_item": per_item,
            }
        }
        if ratio == 0.0:
            notes.append(
                f"cutoff battery matches the baseline pattern (ratio 0.0) — {observed_count} facts observed"
            )
            result = probe_result(
                self.id, self.domain, successes=observed_count, attempts=observed_count, notes=notes
            )
        elif ratio <= _CUTOFF_WARN_RATIO:
            notes.append(
                f"cutoff battery diverges from the baseline pattern (ratio {ratio:.2f} <= {_CUTOFF_WARN_RATIO}) — WARN band"
            )
            result = probe_result(
                self.id, self.domain, successes=observed_count - mismatches,
                attempts=observed_count, notes=notes,
            )
        else:
            # A divergence past the warn band is an explicit capability FAIL
            # at score 0 — never a partial WARN carrying a "capability FAIL"
            # note (the verdict must match the note).
            notes.append(
                f"cutoff battery diverges from the baseline pattern (ratio {ratio:.2f} > {_CUTOFF_WARN_RATIO}) — capability FAIL"
            )
            result = ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.FAIL, score=0.0,
                successes=observed_count - mismatches,
                attempts=observed_count, notes=notes,
            )
        result.metrics = metrics
        return result


# ---------- d8.prompt_caching ----------


class PromptCachingProbe:
    """d8.prompt_caching ×3 — repeated >= 300-token prefix; cache fields sane
    and nondecreasing, and every repeat's TTFT strictly below the cold call
    (equal or slower repeat WARNs — PASS requires the cache benefit).
    Known non-caching families WARN; unknown families SKIP."""

    id = "d8.prompt_caching"
    domain = Domain.D8
    weight = 1.0
    samples = 3

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        claim = _caching_claim(ctx)
        if claim is None:
            return ProbeResult(
                probe_id=self.id, domain=self.domain, verdict=Verdict.SKIP, score=0.0,
                successes=0, attempts=0,
                notes=["unknown model family for prompt caching and no baseline usage_schema claim — skipped"],
            )
        if not claim:
            return warn_result(
                self.id, self.domain, notes=[
                    f"family {ctx.model!r} does not claim prompt caching — cache fields cannot be verified"
                ],
            )
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": f"{_CACHE_PREFIX} What is 2+2?"}],
            "max_tokens": 24,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        per_call: list[dict[str, Any]] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failure = False
        for i in range(self.samples):
            try:
                result = await ctx.stream(self.id, "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"call {i}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001
                transport_failures = True
                notes.append(f"call {i}: transport error: {exc}")
                continue
            if result.status != 200:
                if result.status in _CLEAN_4XX:
                    hard_failure = True
                    notes.append(f"call {i}: claims caching but clean status {result.status} — capability FAIL")
                else:
                    hard_failure = True
                    notes.append(f"call {i}: unexpected status {result.status}")
                continue
            if not result.events:
                hard_failure = True
                notes.append(f"call {i}: stream carried no events")
                continue
            usage = next(
                (event.usage for event in reversed(result.events) if event.usage is not None), None
            )
            cached: Any = None
            prompt: Any = None
            if isinstance(usage, dict):
                prompt = usage.get("prompt_tokens")
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    cached = details.get("cached_tokens")
            if cached is None:
                hard_failure = True
                notes.append(f"call {i}: cached_tokens absent on 200 — cache claim not honored")
                continue
            if not (isinstance(cached, int) and isinstance(prompt, int) and 0 <= cached <= prompt):
                hard_failure = True
                notes.append(f"call {i}: cached_tokens {cached!r} out of range of prompt_tokens {prompt!r}")
                continue
            per_call.append({"cached_tokens": cached, "prompt_tokens": prompt, "ttft_ms": result.ttft_ms})

        metrics = {"caching": {"per_call": per_call, "family_claim": True}}
        if transport_failures:
            result = ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=len(per_call),
                attempts=self.samples,
                notes=notes,
            )
            result.metrics = metrics
            return result
        if hard_failure:
            result = ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=len(per_call),
                attempts=self.samples,
                notes=notes,
            )
            result.metrics = metrics
            return result
        if warn_failures:
            result = probe_result_with_warn(
                self.id, self.domain, successes=len(per_call), attempts=self.samples,
                notes=notes, warn_failures=True,
            )
            result.metrics = metrics
            return result
        if len(per_call) != self.samples:
            result = probe_result(
                self.id, self.domain, successes=len(per_call), attempts=self.samples, notes=notes,
            )
            result.metrics = metrics
            return result

        caches = [call["cached_tokens"] for call in per_call]
        deltas = [caches[i] - caches[i - 1] for i in range(1, len(caches))]
        nondecreasing = all(delta >= 0 for delta in deltas)
        first_ttft = per_call[0]["ttft_ms"]
        ttft_ok = first_ttft is not None
        if ttft_ok and first_ttft is not None:
            # PASS requires a real TTFT drop on every repeat: an equal or
            # slower repeat means the cache was not honored (no jitter
            # tolerance — cache claims are cost claims).
            for call in per_call[1:]:
                repeat_ms = call["ttft_ms"]
                if repeat_ms is None or repeat_ms >= first_ttft:
                    ttft_ok = False
                    notes.append(
                        f"TTFT regressed (repeat not below the cold call): first {first_ttft:.1f}ms, "
                        f"repeat {repeat_ms!r}ms — cache not honored"
                    )
                    break
        metrics["caching"]["cache_deltas"] = deltas
        metrics["caching"]["nondecreasing"] = nondecreasing
        metrics["caching"]["ttft_ok"] = ttft_ok
        if not nondecreasing:
            notes.append(f"cached_tokens regressed between identical calls: {caches}")
            result = warn_result(self.id, self.domain, notes=notes, attempts=self.samples)
            result.metrics = metrics
            return result
        if not ttft_ok:
            notes.append("TTFT regressed (equal or slower repeat) — cache not honored")
            result = warn_result(self.id, self.domain, notes=notes, attempts=self.samples)
            result.metrics = metrics
            return result
        notes.append(
            f"cache fields sane and nondecreasing ({caches}); TTFT dropped on every repeat "
            f"({per_call[0]['ttft_ms']:.1f}ms -> {per_call[-1]['ttft_ms']:.1f}ms)"
        )
        result = probe_result(
            self.id, self.domain, successes=self.samples, attempts=self.samples, notes=notes,
        )
        result.metrics = metrics
        return result
