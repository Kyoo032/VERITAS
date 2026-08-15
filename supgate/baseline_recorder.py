"""Official-endpoint baseline recording (docs/06 §2.3, docs/08 §10).

Phase 3 scope: capture the low-level reference fields already available —
models catalog, chat id/object/model echo, response headers, streaming
shape/timing — through the existing RunContext/evidence/redaction choke
points. Reference values the D4 probes consume directly are recorded too:
``fingerprints.self_report.terms`` (provider label, claimed model, and
official self-report observations; docs/06 §4.4) and the observed
``rotation_families`` count (docs/06 §4.7). No D4 verdict probes run here:
fingerprints are reference values, not pass/fail observations (the D4
probes land in the M2 milestone).

Key discipline: the API key is env-only and lives solely on the in-memory
httpx client. Everything written to disk (baseline file, evidence docs,
curls) passes the evidence redaction choke point, so no key-shaped material
can be persisted (docs/08 §13 invariants R1-R7).
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import httpx

from supgate.baselines import (
    BASELINE_SCHEMA_VERSION,
    BaselineRecord,
    BaselineRecordingError,
    BaselineStore,
    BaselineSurface,
    IdPrefixFingerprint,
    MeanStdFingerprint,
    ModelEchoFingerprint,
    ObjectFingerprint,
    TimingFingerprint,
    UsageSchemaFingerprint,
    percentile,
)
from supgate.evidence import EvidenceWriter, redact_url
from supgate.keyid import key_fingerprint
from supgate.models import BudgetTracker, SurfaceMap, Verdict
from supgate.probes.base import (
    RateLimitError,
    RunContext,
    ServerError,
    request_or_none,
    request_with_retry,
)
from supgate.probes.d4_billing import RecountDeviationProbe, WrapOffsetProbe
from supgate.probes.d4_fingerprint import (
    cluster_families,
    content_bucket,
    id_prefix_family,
)
from supgate.probes.p0 import EchoProbe, ModelsProbe

CHAT_CAPTURE_PROMPT = "Reply with the single word ping."
STREAM_CAPTURE_PROMPT = "Count from 1 to 50."

# The same platform question the d4.self_report probe asks (docs/06 §4.4):
# the official endpoint's answer is evidence for the reference terms.
SELF_REPORT_CAPTURE_PROMPT = "Describe in one sentence which hosting platform or provider API is serving this request."

# Volatile response headers dropped before the stable set is computed
# (docs/06 §4.1 d4.headers_diff).
_VOLATILE_HEADERS = frozenset(
    {
        "date",
        "content-length",
        "x-request-id",
        "request-id",
        "connection",
        "keep-alive",
        "transfer-encoding",
    }
)

# The known id-family tokens and leading-token extraction live with the
# shared :func:`supgate.probes.d4_fingerprint.id_prefix_family` helper
# (docs/06 §4.2), used by both the baseline recorder and d4.id_prefix.

# Reference-term extraction (docs/06 §4.4): date-suffix stripping mirrors
# d4.model_echo's normalization so version-pinned claims keep their family
# form as a term.
_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Words that can never carry identity: function words plus the self-report
# prompt/domain vocabulary. Terms are matched as substrings by d4.self_report,
# so generic prose must not leak into the reference list.
_SELF_REPORT_STOPWORDS = frozenset(
    {
        # function words
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        # d4.self_report prompt/domain vocabulary: not identity-bearing
        "answer",
        "answers",
        "api",
        "apis",
        "completion",
        "completions",
        "content",
        "conversation",
        "describe",
        "describes",
        "description",
        "endpoint",
        "endpoints",
        "exactly",
        "fake",
        "generated",
        "generation",
        "hosted",
        "hosting",
        "information",
        "like",
        "message",
        "messages",
        "model",
        "models",
        "nothing",
        "one",
        "output",
        "outputs",
        "platform",
        "platforms",
        "please",
        "provider",
        "providers",
        "punctuation",
        "reply",
        "replies",
        "request",
        "requests",
        "respond",
        "response",
        "responses",
        "result",
        "results",
        "running",
        "say",
        "says",
        "sentence",
        "served",
        "server",
        "servers",
        "service",
        "services",
        "serving",
        "system",
        "test",
        "testing",
        "text",
        "user",
        "word",
        "words",
    }
)


def _chat_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": CHAT_CAPTURE_PROMPT}],
        "max_tokens": 8,
        "temperature": 0,
    }


def _stream_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": STREAM_CAPTURE_PROMPT}],
        "max_tokens": 64,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0,
    }


def _self_report_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": SELF_REPORT_CAPTURE_PROMPT}],
        "max_tokens": 48,
        "temperature": 0,
    }


def _id_prefix(value: str) -> str:
    # Shared with d4.id_prefix (docs/06 §4.2) so the known-family table
    # lives in exactly one place.
    return id_prefix_family(value)


def _usage_schema(result: Any) -> dict[str, bool]:
    cached = False
    reasoning = False
    for event in result.events:
        usage = event.usage
        if not isinstance(usage, dict):
            continue
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            cached = True
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict) and "reasoning_tokens" in completion_details:
            reasoning = True
    return {"cached_tokens": cached, "reasoning_tokens": reasoning}


def _timing(values: list[float]) -> dict[str, Any]:
    return TimingFingerprint(
        median=percentile(values, 50), p90=percentile(values, 90), n=len(values)
    ).model_dump()


async def _capture_chats(ctx: RunContext, model: str, samples: int) -> dict[str, Any]:
    ids: list[str] = []
    objects: list[str] = []
    models: list[str] = []
    statuses: list[int] = []
    header_sets: list[set[str]] = []
    contents: list[str] = []
    usages: list[dict[str, Any] | None] = []
    for _ in range(samples):
        try:
            response = await request_with_retry(
                ctx, "baseline.capture.chat", "POST", "/chat/completions",
                payload=_chat_payload(model),
            )
            body = response.json()
        except BaselineRecordingError:
            raise
        except (RateLimitError, ServerError) as exc:
            raise BaselineRecordingError(f"chat capture failed: {exc.note}") from exc
        except Exception as exc:  # noqa: BLE001 - transport errors abort the recording
            raise BaselineRecordingError(f"chat capture failed: {exc}") from exc
        if response.status_code != 200 or not isinstance(body, dict):
            raise BaselineRecordingError(
                f"chat capture failed: status {response.status_code} with non-contract body"
            )
        ids.append(str(body.get("id") or ""))
        objects.append(str(body.get("object") or ""))
        models.append(str(body.get("model") or ""))
        statuses.append(response.status_code)
        contents.append(_content_of(body))
        usage = body.get("usage")
        usages.append(usage if isinstance(usage, dict) else None)
        header_sets.append(
            {
                name
                for name in response.headers
                if name not in _VOLATILE_HEADERS and not name.startswith("x-ratelimit-")
            }
        )
    return {
        "ids": ids,
        "objects": objects,
        "models": models,
        "statuses": statuses,
        "header_sets": header_sets,
        "contents": contents,
        "usages": usages,
    }


async def _capture_streams(ctx: RunContext, model: str, streams: int) -> dict[str, Any]:
    ttfts: list[float] = []
    inters: list[float] = []
    e2es: list[float] = []
    chunks: list[int] = []
    usage_schemas: list[dict[str, bool]] = []
    for _ in range(streams):
        try:
            result = await ctx.stream(
                "baseline.capture.stream", "/chat/completions", payload=_stream_payload(model)
            )
        except BaselineRecordingError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors abort the recording
            raise BaselineRecordingError(f"stream capture failed: {exc}") from exc
        if result.status != 200 or not result.events:
            raise BaselineRecordingError(
                f"stream capture failed: status {result.status}, {len(result.events)} events"
            )
        ttfts.append(result.ttft_ms)
        inters.extend(result.inter_event_ms)
        e2es.append(result.e2e_ms)
        chunks.append(len(result.events))
        usage_schemas.append(_usage_schema(result))
    return {"ttfts": ttfts, "inters": inters, "e2es": e2es, "chunks": chunks, "usage_schemas": usage_schemas}


async def _capture_self_reports(ctx: RunContext, model: str, samples: int) -> list[str]:
    """Official-endpoint self-report observations (docs/06 §4.4).

    One chat exchange per sample with the same platform question the
    d4.self_report probe asks. Responses stay in the evidence stream
    through the RunContext choke point and are tokenized into reference
    terms by :func:`_reference_terms` — an observation, never an inference.
    """

    observations: list[str] = []
    for _ in range(samples):
        try:
            response = await request_with_retry(
                ctx, "baseline.capture.self_report", "POST", "/chat/completions",
                payload=_self_report_payload(model),
            )
            body = response.json()
        except BaselineRecordingError:
            raise
        except (RateLimitError, ServerError) as exc:
            raise BaselineRecordingError(f"self-report capture failed: {exc.note}") from exc
        except Exception as exc:  # noqa: BLE001 - transport errors abort the recording
            raise BaselineRecordingError(f"self-report capture failed: {exc}") from exc
        if response.status_code != 200 or not isinstance(body, dict):
            raise BaselineRecordingError(
                f"self-report capture failed: status {response.status_code} with non-contract body"
            )
        observations.append(_content_of(body))
    return observations


def _content_of(body: Any) -> str:
    """First choice message content; mirrors d4's private helper so the
    recorder stays on the public d4 surface."""

    if not isinstance(body, dict):
        return ""
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _usage_ratio(usage: dict[str, Any] | None) -> float | None:
    """prompt/completion token ratio rounded to 2 dp; mirrors d4.rotation's
    private helper (kept local: the probe module is out of scope)."""

    if not usage:
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int) or completion == 0:
        return None
    return round(prompt / completion, 2)


def _reference_terms(
    *,
    label: str,
    model: str,
    claimed_models: list[str],
    observations: list[str],
) -> list[str]:
    """d4.self_report reference terms (docs/06 §4.4).

    Sources are strictly the operator-supplied provider label, the claimed
    model names (date suffix stripped like d4.model_echo), and tokens
    observed in the official self-report answers — never a static provider
    table. Stopwords keep generic prose out; d4.self_report matches terms as
    substrings, so only identity-bearing tokens belong here.
    """

    terms: list[str] = []
    for source in (label, model, *claimed_models):
        lowered = (source or "").lower()
        for candidate in (lowered, _DATE_SUFFIX_RE.sub("", lowered)):
            for token in _TOKEN_RE.findall(candidate):
                token = token.rstrip("._-")
                if len(token) >= 2 and token not in terms:
                    terms.append(token)
    for observation in observations:
        for token in _TOKEN_RE.findall((observation or "").lower()):
            token = token.rstrip("._-")
            if len(token) >= 3 and token not in _SELF_REPORT_STOPWORDS and token not in terms:
                terms.append(token)
    return sorted(terms)


def _rotation_families(chats: dict[str, Any]) -> tuple[int, int]:
    """Observed distinct response families at baseline time (docs/06 §4.7).

    Rotation features are built from the recorded chat samples with the same
    tolerant-union clustering the d4.rotation probe applies, so the recorded
    count is the family count the probe would see on the official endpoint.
    Returns ``(families, samples)``.
    """

    features: list[dict[str, Any]] = []
    for i, response_id in enumerate(chats["ids"]):
        features.append(
            {
                "id_family": _id_prefix(response_id),
                "content_bucket": content_bucket(chats["contents"][i]),
                "usage_ratio": _usage_ratio(chats["usages"][i]),
            }
        )
    _, representatives = cluster_families(features)
    return len(representatives), len(features)


async def _capture_billing(ctx: RunContext) -> dict[str, Any]:
    """Capture tokenizer recount and constant-offset reference distributions."""

    recount = await RecountDeviationProbe().run(ctx)
    recount_metrics = recount.metrics.get("recount_deviation", {})
    recount_samples = recount_metrics.get("per_sample")
    if not isinstance(recount_samples, list) or len(recount_samples) != RecountDeviationProbe.samples:
        raise BaselineRecordingError("billing recount calibration did not produce all prompt-size samples")
    deviations = [
        float(sample["deviation_pct"])
        for sample in recount_samples
        if isinstance(sample, dict)
        and not sample.get("cached_sample")
        and isinstance(sample.get("deviation_pct"), (int, float))
    ]
    if not deviations:
        raise BaselineRecordingError("billing recount calibration has no non-cached samples")

    wrap = await WrapOffsetProbe().run(ctx)
    wrap_metrics = wrap.metrics.get("wrap_offset", {})
    wrap_samples = wrap_metrics.get("per_sample")
    if not isinstance(wrap_samples, list) or len(wrap_samples) != WrapOffsetProbe.samples:
        raise BaselineRecordingError("billing wrap calibration did not produce all prompt-size samples")
    offsets = [
        float(sample["offset_tokens"])
        for sample in wrap_samples
        if isinstance(sample, dict) and isinstance(sample.get("offset_tokens"), (int, float))
    ]
    if len(offsets) != WrapOffsetProbe.samples:
        raise BaselineRecordingError("billing wrap calibration contains incomplete offset measurements")

    return {
        "recount_deviation_pct": _mean_std(deviations),
        "wrap_offset_tokens": _mean_std(offsets),
    }


def _mean_std(values: list[float]) -> dict[str, Any]:
    return MeanStdFingerprint(
        mean=mean(values),
        std=stdev(values) if len(values) > 1 else 0.0,
        n=len(values),
    ).model_dump()


def _build_fingerprints(
    *,
    chats: dict[str, Any],
    streams: dict[str, Any],
    label: str,
    model: str,
    claimed_models: list[str],
    self_reports: list[str],
    billing: dict[str, Any],
) -> dict[str, Any]:
    ids = chats["ids"]
    objects = chats["objects"]
    models = chats["models"]
    families = {_id_prefix(value) for value in ids}
    header_sets = chats["header_sets"]
    usage_flags = {
        key: any(schema[key] for schema in streams["usage_schemas"])
        for key in ("cached_tokens", "reasoning_tokens")
    }
    rotation_families, rotation_samples = _rotation_families(chats)
    return {
        "id_prefix": IdPrefixFingerprint(
            family=sorted(families)[0] if len(families) == 1 else "",
            samples=len(ids),
            consistent=len(families) == 1,
        ).model_dump(),
        "chat_object": ObjectFingerprint(
            object=sorted(set(objects))[0] if len(set(objects)) == 1 else "",
            samples=len(objects),
        ).model_dump(),
        "model_echo": ModelEchoFingerprint(
            model=sorted(set(models))[0] if len(set(models)) == 1 else "",
            samples=len(models),
            consistent=len(set(models)) == 1,
        ).model_dump(),
        "headers_stable_set": sorted(set.intersection(*[set(h) for h in header_sets])),
        "sse_ttft_ms": _timing(streams["ttfts"]),
        "sse_inter_chunk_ms": _timing(streams["inters"]),
        "sse_e2e_ms": _timing(streams["e2es"]),
        "sse_chunks": _timing([float(count) for count in streams["chunks"]]),
        "usage_schema": UsageSchemaFingerprint(**usage_flags).model_dump(),
        "self_report": {
            "terms": _reference_terms(
                label=label,
                model=model,
                claimed_models=claimed_models,
                observations=self_reports,
            ),
            "samples": len(self_reports),
        },
        # d4.rotation consumes the family count directly as F_expected
        # (docs/06 §4.7); the observation count rides alongside so the
        # entry still carries its own n (docs/08 §10.1).
        "rotation_families": rotation_families,
        "rotation_families_n": rotation_samples,
        **billing,
    }


async def record_baseline(
    *,
    vendor: str,
    model: str,
    api_key: str,
    key_env: str | None = None,
    endpoint: str,
    out: Path,
    label: str | None = None,
    model_version: str | None = None,
    claimed_models: list[str] | None = None,
    samples: int = 3,
    streams: int = 1,
    evidence_root: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    run_p0_gate: bool = True,
    confirmed_official: bool = False,
    captured_at: str | None = None,
) -> BaselineRecord:
    """Capture official-endpoint fingerprints and write one baseline file.

    The p0.echo gate (docs/08 §10.1: baselines require ``p0.echo == pass``
    on the recording run) runs first unless ``run_p0_gate`` is False. All
    HTTP flows through ``RunContext.request``/``stream`` so evidence,
    redaction, and curls stay at the single choke point. Raises
    :class:`BaselineRecordingError` on any capture failure; nothing is
    written in that case.

    Besides the low-level fingerprints, the record carries the two D4
    reference values: ``self_report.terms`` (provider label, claimed model,
    and official self-report observations — docs/06 §4.4) and
    ``rotation_families`` (families observed across the chat samples —
    docs/06 §4.7).
    """

    if not model.strip():
        raise ValueError("model must not be empty")
    if not endpoint.strip():
        raise ValueError("endpoint must not be empty")
    if "?" in endpoint or "#" in endpoint:
        raise ValueError("endpoint must be a base URL without query/fragment")
    if samples < 1 or streams < 1:
        raise ValueError("samples/streams must be >= 1")
    out_root = Path(out)
    out_root.mkdir(parents=True, exist_ok=True)
    evidence_root = Path(evidence_root) if evidence_root else Path("runs")
    evidence_root.mkdir(parents=True, exist_ok=True)

    run_id = f"BASELINE-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(2).upper()}"
    evidence = EvidenceWriter(
        evidence_root / "evidence", run_id, key_fingerprint=key_fingerprint(api_key)
    )
    surface = SurfaceMap()
    client = httpx.AsyncClient(transport=transport, timeout=60.0)
    ctx = RunContext(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        claimed_models=claimed_models or [model],
        surface=surface,
        client=client,
        evidence=evidence,
        budget=BudgetTracker(model=model),
    )
    try:
        if run_p0_gate:
            echo = await EchoProbe().run(ctx)
            if echo.verdict != Verdict.PASS:
                detail = "; ".join(echo.notes) or "no notes"
                raise BaselineRecordingError(
                    f"p0.echo gate failed on the recording run (docs/08 §10.1): {detail}"
                )
        await ModelsProbe().run(ctx)  # fills ctx.surface: catalog + claimed_present

        responses, _ = await request_or_none(
            ctx, "baseline.capture.responses", "POST", "/responses",
            payload={"model": model, "input": "ping"},
        )
        surface.responses_api = responses is not None and responses.status_code == 200

        chats = await _capture_chats(ctx, model, samples)
        surface.messages_api = any(status == 200 for status in chats["statuses"])
        streams_result = await _capture_streams(ctx, model, streams)
        self_reports = await _capture_self_reports(ctx, model, samples)
        billing_encoding = RecountDeviationProbe.tokenizer.resolve_encoding(model)
        billing = await _capture_billing(ctx) if billing_encoding is not None else {}

        notes = [
            "recorded against an official endpoint (operator-managed reference)",
            f"evidence run: {run_id}",
            f"p0.echo gate: {'passed' if run_p0_gate else 'not run'}",
            (
                "operator confirmed official endpoint"
                if confirmed_official
                else "official-endpoint confirmation not given (--confirm-official)"
            ),
            f"reference terms from provider label/model and {len(self_reports)} official self-report observation(s)",
            (
                "billing recount and wrap-offset calibration captured with the evaluation probes"
                if billing_encoding is not None
                else f"billing calibration omitted: unknown tokenizer encoding for {model!r}; D4 uses static gates"
            ),
        ]
        if not surface.claimed_present:
            notes.append("claimed model absent from /models catalog at capture time")

        record = BaselineRecord(
            schema=BASELINE_SCHEMA_VERSION,
            baseline_id=BaselineStore(out_root).next_id(label=label or vendor, model=model),
            provider_label=label or vendor,
            vendor=vendor,
            model=model,
            model_version=model_version,
            endpoint=redact_url(endpoint),
            key_env=key_env or "",
            key_fingerprint=key_fingerprint(api_key),
            captured_at=captured_at or datetime.now(UTC).isoformat(),
            claimed_models=claimed_models or [model],
            surface=BaselineSurface(
                models_catalog=len(surface.models),
                responses_api=surface.responses_api,
                messages_api=surface.messages_api,
                claimed_present=surface.claimed_present,
            ),
            fingerprints=_build_fingerprints(
                chats=chats,
                streams=streams_result,
                label=label or vendor,
                model=model,
                claimed_models=claimed_models or [model],
                self_reports=self_reports,
                billing=billing,
            ),
            notes=notes,
        )
        BaselineStore(out_root).save(record)
        return record
    finally:
        await client.aclose()
