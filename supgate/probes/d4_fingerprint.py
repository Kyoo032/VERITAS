"""Custom-logic D4 relay fingerprint probes (docs/06-m2-probe-spec.md §4.1-§4.7).

Seven probes: d4.headers_diff, d4.id_prefix, d4.model_echo, d4.self_report,
d4.canary_echo, d4.sse_timing, d4.rotation. All produce *consistency*
statements relative to the claimed model list and any matched baseline —
never identity claims (docs/06 §1.2: "consistent with", never "is").

Verdict routing (docs/06 §1.3): persistent 429/5xx after exactly one local
backoff retry maps to WARN (:class:`RateLimitError`/:class:`ServerError`
converted via the shared retry policy in ``base.py``); transport errors
(no HTTP response) stay FAIL with the failed attempt's evidence saved;
non-retryable bad statuses are hard FAILs. Metrics dicts feed baseline
capture and QA exports (docs/06 §3.6).

Shared helpers here are used by ``baseline_recorder.py``
(:func:`id_prefix_family`) and by d4.rotation clustering
(:func:`content_bucket`, :func:`normalize_content`, :func:`cluster_families`).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from typing import Any

from supgate.baselines import percentile
from supgate.models import Domain, ProbeResult, SurfaceMap, TimingSample, Verdict
from supgate.probes.base import (
    RateLimitError,
    RunContext,
    ServerError,
    probe_result_with_warn,
    request_with_retry,
    text_content,
    warn_result,
)
from supgate.probes.idempotency import _shape

# Volatile response headers dropped before the stable header set is computed
# (docs/06 §4.1): date/content-length/ids/ratelimit/connection plumbing.
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

# Hop markers: a gateway/relay inserts or rewrites these (docs/06 §4.1).
_HOP_MARKERS = frozenset(
    {
        "via",
        "x-served-by",
        "x-upstream",
        "x-cache",
        "x-cache-status",
        "x-proxy",
        "x-forwarded-for",
        "x-real-ip",
        "cf-ray",
        "cf-cache-status",
        "server",
        "x-powered-by",
    }
)

# Known id-family tokens (docs/06 §4.2). Longest tokens first so
# ``chatcmpl-`` wins over ``chatcmpl``. Static, short, explicitly NOT
# identity proof: gateways often imitate official prefixes.
_ID_FAMILY_TOKENS: tuple[str, ...] = (
    "chatcmpl-",
    "resp_",
    "msg_",
    "gen-",
    "cmpl-",
    "anthropic",
    "chatcmpl",
)

# Claimed-model prefix -> implied contract family (docs/06 §4.2). The
# comparison is startswith-tolerant so ``chatcmpl``/``chatcmpl-`` agree.
_CLAIMED_FAMILY_RULES: tuple[tuple[str, str], ...] = (
    ("gpt-4o", "chatcmpl-"),
    ("gpt-4", "chatcmpl-"),
    ("gpt-3.5", "chatcmpl-"),
    ("o1", "chatcmpl-"),
    ("o3", "chatcmpl-"),
    ("o4", "chatcmpl-"),
    ("text-", "chatcmpl-"),
    ("davinci", "chatcmpl-"),
    ("curie", "chatcmpl-"),
    ("claude", "msg_"),
    ("gemini", "gen-"),
)

# Known model families used by the d4.model_echo contradiction check
# (docs/06 §4.3). Deliberately excludes "gpt" so a matching echo never
# self-contradicts via a broad substring.
_ALL_KNOWN_FAMILIES: tuple[str, ...] = (
    "claude",
    "gemini",
    "mistral",
    "mixtral",
    "llama",
    "deepseek",
    "qwen",
    "glm",
    "kimi",
    "grok",
    "command",
    "phi",
    "falcon",
    "nemotron",
    "jamba",
    "aya",
    "granite",
    "olmo",
)

# Explicit id-family token -> provider mapping (docs/06 §1.2, §4.2). The
# mapping is deliberately conservative: only explicit known tokens count, so
# generic prose ("openai-style", "hosted on azure") never maps. "openai" is
# NOT a token — prose naming OpenAI is not an id-family signal.
_PROVIDER_FAMILY_TOKENS: tuple[tuple[str, str], ...] = (
    ("chatcmpl-", "openai"),
    ("resp_", "openai"),
    ("msg_", "anthropic"),
    ("anthropic", "anthropic"),
    ("gen-", "gemini"),
    ("gemini", "gemini"),
)

_ID_CHARS = re.compile(r"^[A-Za-z0-9_-]+")
_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def provider_of_family(family: str) -> str | None:
    """Official provider mapped from an id-family token, or None.

    ``chatcmpl-``/``resp_`` -> openai, ``msg_``/``anthropic`` -> anthropic,
    ``gen-``/``gemini`` -> gemini; custom/unknown prefixes stay
    unclassified (None). The mapping is a reference list, never identity
    proof (docs/06 §1.2).
    """

    if not family:
        return None
    for token, provider in _PROVIDER_FAMILY_TOKENS:
        if family.startswith(token):
            return provider
    return None


def provider_family_hits(text: str) -> list[str]:
    """Official providers explicitly named in free text, sorted unique.

    Only explicit known family tokens count (id prefixes plus the brand
    names ``anthropic``/``gemini``); generic provider prose is never a
    family signal (docs/06 §1.2: do not infer from prose).
    """

    lowered = text.lower()
    return sorted({provider for token, provider in _PROVIDER_FAMILY_TOKENS if token in lowered})


def claimed_family_of(claimed_models: list[str]) -> str | None:
    """Family token implied by the claimed model contract, or None (docs/06 §4.2)."""

    return _claimed_family(claimed_models)


def id_prefix_family(value: str) -> str:
    """Leading id-family token of a response id (docs/06 §4.2, shared with the
    baseline recorder).

    Known tokens win by longest-prefix match (``chatcmpl-fake123`` ->
    ``chatcmpl-``); otherwise the leading ``[A-Za-z0-9_-]+`` run is returned
    (``acme.fake123`` -> ``acme``). Empty input yields "".
    """

    if not value:
        return ""
    for token in _ID_FAMILY_TOKENS:
        if value.startswith(token):
            return token
    match = _ID_CHARS.match(value)
    return match.group(0) if match else ""


def _claimed_family(claimed_models: list[str]) -> str | None:
    """First family implied by the claimed model contract, or None when unknown."""

    families = claimed_families_of(claimed_models)
    return families[0] if families else None


def claimed_families_of(claimed_models: list[str]) -> list[str]:
    """All family tokens implied by the claimed model contract, rule order,
    unique (docs/06 §4.2).

    A mixed claim such as ``['gpt-4o', 'claude-3-5-sonnet']`` yields
    ``['chatcmpl-', 'msg_']`` so inconsistency checks (d4.id_prefix,
    reverse_identity veto) compare the observed family against *every*
    claimed family, never just the first rule match.
    """

    families: list[str] = []
    for model in claimed_models:
        lowered = model.lower()
        for prefix, family in _CLAIMED_FAMILY_RULES:
            if lowered.startswith(prefix) and family not in families:
                families.append(family)
    return families


def claimed_providers_of(claimed_models: list[str]) -> set[str]:
    """Official providers implied by every claimed family that maps to one.

    Families without an explicit provider mapping stay unclassified (None)
    and never count; an empty result means the claim cannot establish an
    official-provider expectation at all.
    """

    return {
        provider
        for family in claimed_families_of(claimed_models)
        if (provider := provider_of_family(family)) is not None
    }


def _family_consistent(family: str, claimed: str | None) -> bool:
    if claimed is None:
        return True
    return family == claimed or family.startswith(claimed) or claimed.startswith(family)


def normalize_content(text: str) -> str:
    """Lowercase, collapse whitespace, strip edge punctuation (d4.rotation)."""

    collapsed = " ".join(text.lower().split())
    return collapsed.strip(".,!?;:'\"()[]{}")


def content_bucket(content: str | None) -> str | None:
    """3-gram shingle hash of normalized content; None for empty/absent.

    Formatting variants of the same text ("Forty two." / "  FORTY  two! ")
    normalize to one bucket; different texts hash differently.
    """

    if not content:
        return None
    text = normalize_content(content)
    if not text:
        return None
    grams = {text[i : i + 3] for i in range(len(text) - 2)}
    digest = hashlib.sha1("|".join(sorted(grams)).encode("utf-8")).hexdigest()
    return digest[:16]


def cluster_families(features: list[dict[str, Any]]) -> tuple[list[int], list[dict[str, Any]]]:
    """Greedy tolerant-union clustering of rotation feature tuples (docs/06 §4.7).

    Two responses are the same family when they share an id family AND agree
    on content bucket OR usage ratio. Comparison is against each family's
    first member only (non-transitive on purpose: a middle sample that agrees
    with a later member but not the first starts a new family). Returns
    ``(assignments, representatives)``.
    """

    assignments: list[int] = []
    reps: list[dict[str, Any]] = []
    for feature in features:
        assigned = next(
            (i for i, rep in enumerate(reps) if _same_family(feature, rep)),
            None,
        )
        if assigned is None:
            assigned = len(reps)
            reps.append(feature)
        assignments.append(assigned)
    return assignments, reps


def _same_family(a: dict[str, Any], b: dict[str, Any]) -> bool:
    id_a, id_b = a.get("id_family"), b.get("id_family")
    if id_a is None or id_b is None or id_a != id_b:
        return False
    if a.get("content_bucket") is not None and a.get("content_bucket") == b.get("content_bucket"):
        return True
    ratio_a, ratio_b = a.get("usage_ratio"), b.get("usage_ratio")
    return ratio_a is not None and ratio_a == ratio_b


def _fresh_canary() -> str:
    """One high-entropy ``VERITAS-<hex16>`` canary token (docs/06 §4.5)."""

    return f"VERITAS-{secrets.token_hex(8)}"


def _is_buffered(ttft_ms: float, total_ms: float, inter_chunk_ms: list[float], n_chunks: int) -> bool:
    """Classic relay-buffering test (docs/06 §4.6): all content chunks arrive
    in one burst after a long stall.

    True when 1-2 content chunks arrive near the end of a >= 500 ms exchange
    (content span tiny relative to total). A single early chunk (small TTFT)
    or chunks spread across the stream are not buffering.
    """

    if n_chunks == 0 or n_chunks > 2:
        return False
    if total_ms < 500:
        return False
    span_ms = sum(inter_chunk_ms)
    return span_ms <= 0.5 * total_ms and ttft_ms >= 0.5 * total_ms


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


def _usage_ratio(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int) or completion == 0:
        return None
    return round(prompt / completion, 2)


def probe_result_capped(
    probe_id: str,
    domain: Domain,
    *,
    successes: int,
    attempts: int,
    notes: list[str] | None = None,
    warn_failures: bool = False,
    transport_failures: bool = False,
) -> ProbeResult:
    """``probe_result_with_warn`` plus the hard docs/06 §1.3 cap.

    Any persistent 429/5xx sample must cap the verdict at WARN, never PASS:
    a partial retry failure is still a degraded measurement and cannot read
    PASS even when every recorded sample succeeded (defensive — callers may
    count samples that themselves survived the retry). Transport failures
    still dominate and keep the FAIL so ``endpoint_dead`` keeps working.
    The partial success score, notes, and caller-attached metrics/evidence
    are preserved. Shared by all 11 D4 probes so the partial-retry routing
    is proven once and reused everywhere.
    """

    result = probe_result_with_warn(
        probe_id,
        domain,
        successes=successes,
        attempts=attempts,
        notes=notes,
        warn_failures=warn_failures,
        transport_failures=transport_failures,
    )
    if warn_failures and not transport_failures and result.verdict == Verdict.PASS:
        result.verdict = Verdict.WARN
        result.score = min(result.score, 50.0)
    return result


class HeadersDiffProbe:
    """d4.headers_diff ×2 logical pairs (4 physical calls) — stable header
    surface and hop markers (docs/06 §4.1)."""

    id = "d4.headers_diff"
    domain = Domain.D4
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "Reply with the single word ping."}],
            "max_tokens": 8,
            "temperature": 0,
        }
        per_response: list[dict[str, Any]] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for _ in range(self.samples * 2):
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(exc.note)
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"unexpected status {response.status_code}")
                continue
            per_response.append(_response_headers_metrics(response.headers))

        stable = [p["stable_set"] for p in per_response]
        pairs_stable = [
            len(stable) >= 2 and stable[0] == stable[1],
            len(stable) >= 4 and stable[2] == stable[3],
        ]
        unstable_pairs = sum(0 if pair else 1 for pair in pairs_stable)
        hop_present = any(p["hop_markers"] for p in per_response)

        metrics = {
            "headers": {
                "pairs_stable": pairs_stable,
                "hop_present": hop_present,
                "unstable_pairs": unstable_pairs,
                "per_response": per_response,
            }
        }
        if transport_failures or hard_failures or warn_failures:
            return probe_result_capped(
                self.id,
                self.domain,
                successes=sum(pairs_stable),
                attempts=self.samples,
                notes=notes,
                warn_failures=warn_failures,
                transport_failures=transport_failures,
            )

        if unstable_pairs > 1:
            notes.append("header set differs across identical requests in more than one pair")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=sum(pairs_stable),
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if hop_present or unstable_pairs == 1:
            if hop_present:
                notes.append("hop markers present but consistent (visible proxy)")
            else:
                notes.append("header set differs in one pair")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=sum(pairs_stable),
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        notes.append("stable header surface, no hop markers")
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=self.samples,
            attempts=self.samples,
            notes=notes,
            metrics=metrics,
        )


def _response_headers_metrics(headers: Any) -> dict[str, Any]:
    stable_set = {name for name in headers if name not in _VOLATILE_HEADERS and not name.startswith("x-ratelimit-")}
    markers = {name: headers[name] for name in _HOP_MARKERS if name in headers}
    return {"stable_set": sorted(stable_set), "hop_markers": markers}


class IdPrefixProbe:
    """d4.id_prefix ×10 — response id families self-consistent and consistent
    with the claimed contract and any baseline (docs/06 §4.2)."""

    id = "d4.id_prefix"
    domain = Domain.D4
    weight = 1.0
    samples = 10

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "Say the word ping."}],
            "max_tokens": 8,
            "temperature": 0,
        }
        prefixes: list[str] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        invalid_ids = 0
        for _ in range(self.samples):
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(exc.note)
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"non-retryable bad response (status {response.status_code})")
                continue
            body = _json(response)
            response_id = body.get("id") if body else None
            if not isinstance(response_id, str) or not response_id:
                invalid_ids += 1
                notes.append("missing or invalid response id")
                continue
            prefixes.append(id_prefix_family(response_id))

        # Metrics are computed from the partial prefix set even when retry
        # failures or invalid samples occurred, so evidence survives a
        # degraded run (docs/06 §4.2: prefixes list, families, baseline
        # match are all retained for QA/reverse-identity corroboration).
        baseline = ctx.selected_baseline
        baseline_family: str | None = None
        family_match: bool | None = None
        if baseline is not None:
            fingerprint = baseline.fingerprints.get("id_prefix")
            if isinstance(fingerprint, dict):
                baseline_family = fingerprint.get("family")
            if baseline_family:
                family_match = any(p == baseline_family for p in prefixes)

        families = sorted(set(prefixes))
        claimed = _claimed_family(ctx.claimed_models)
        metrics = {
            "id_prefix": {
                "prefixes": prefixes,
                "families": families,
                "baseline_family": baseline_family,
                "family_match": family_match,
            }
        }

        if transport_failures or hard_failures or warn_failures or invalid_ids:
            result = probe_result_capped(
                self.id,
                self.domain,
                successes=len(prefixes),
                attempts=self.samples,
                notes=notes,
                warn_failures=warn_failures,
                transport_failures=transport_failures,
            )
            result.metrics = metrics
            return result

        if len(families) > 1:
            notes.append("id family rotates across identical requests")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        family = families[0]
        if baseline_family and not family_match:
            notes.append("id family does not match baseline")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if family not in _ID_FAMILY_TOKENS:
            notes.append("custom id prefix (not an official family)")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        # docs/06 §4.2: a matched selected baseline family overrides the
        # static claimed-family mapping (fixture 4: server ``gen-`` with a
        # matching baseline ``gen-`` PASSes for a claimed ``gpt-4o``). The
        # baseline is a recorded measurement of this endpoint; the claim
        # mapping is a static reference list.
        if claimed is not None and not _family_consistent(family, claimed) and not (
            baseline_family and family_match
        ):
            notes.append(f"id family {family!r} differs from claimed family {claimed!r}")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        notes.append("id family stable and consistent")
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=self.samples,
            attempts=self.samples,
            notes=notes,
            metrics=metrics,
        )


class ModelEchoProbe:
    """d4.model_echo ×2 — model's self-reported identity string. Weak,
    corroborating-only signal; structurally capped at WARN (docs/06 §4.3)."""

    id = "d4.model_echo"
    domain = Domain.D4
    weight = 0.5
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly the model identifier that is running "
                    "this conversation. Nothing else. No punctuation.",
                }
            ],
            "max_tokens": 24,
            "temperature": 0,
        }
        echoes: list[str] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for _ in range(self.samples):
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(exc.note)
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"non-retryable bad response (status {response.status_code})")
                continue
            echoes.append(_content_of(_json(response)))

        if transport_failures or hard_failures or warn_failures:
            return probe_result_capped(
                self.id,
                self.domain,
                successes=len(echoes),
                attempts=self.samples,
                notes=notes,
                warn_failures=warn_failures,
                transport_failures=transport_failures,
            )

        claimed = _normalized_claimed(ctx.claimed_models)
        normalized_echoes = [echo.strip().lower().rstrip(".,!?;:'\"") for echo in echoes]
        match_flags = [_echo_matches(echo, claimed) for echo in normalized_echoes]
        contradiction_flags = [_echo_contradicts(echo, claimed) for echo in normalized_echoes]
        empty = any(echo == "" for echo in normalized_echoes)

        metrics = {
            "model_echo": {
                "raw_echoes": echoes,
                "claimed": claimed,
                "match_flags": match_flags,
                "contradiction_flags": contradiction_flags,
            }
        }
        matched = any(match_flags)
        contradicts = any(contradiction_flags)
        if empty:
            notes.append("empty model echo in at least one sample")
        if contradicts:
            notes.append("echo names a known family outside the claim")
        elif not matched:
            notes.append("echo matches no claimed model")
        if empty or contradicts or not matched:
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        notes.append("self-reported model consistent with the claim")
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=self.samples,
            attempts=self.samples,
            notes=notes,
            metrics=metrics,
        )


def _normalized_claimed(claimed_models: list[str]) -> list[str]:
    normalized: list[str] = []
    for model in claimed_models:
        lowered = model.lower()
        for candidate in (lowered, _DATE_SUFFIX.sub("", lowered)):
            if candidate and candidate not in normalized:
                normalized.append(candidate)
    return normalized


def _echo_matches(echo: str, claimed: list[str]) -> bool:
    if not echo:
        return False
    return any(c == echo or c in echo or echo in c for c in claimed)


def _echo_contradicts(echo: str, claimed: list[str]) -> bool:
    """True when the echo names a known model family outside the claim.

    A family token contradicts only when it belongs to none of the claimed
    models: a claimed ``claude-3-5-sonnet`` that echoes ``claude-3-5-sonnet``
    must not self-contradict through the shared ``claude`` token, and the
    same holds for gemini/deepseek/etc. families (docs/06 §4.3).
    """

    claimed_families = {family for model in claimed for family in _ALL_KNOWN_FAMILIES if family in model}
    return any(family in echo and family not in claimed_families for family in _ALL_KNOWN_FAMILIES)


class SelfReportProbe:
    """d4.self_report ×2 — platform self-report vs baseline claim terms plus
    /models surface drift vs baseline surface (docs/06 §4.4). Reuses the
    SurfaceMap from p0.models; never calls /models itself."""

    id = "d4.self_report"
    domain = Domain.D4
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Describe in one sentence which hosting platform or "
                    "provider API is serving this request.",
                }
            ],
            "max_tokens": 48,
            "temperature": 0,
        }
        texts: list[str] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for _ in range(self.samples):
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(exc.note)
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"non-retryable bad response (status {response.status_code})")
                continue
            texts.append(_content_of(_json(response)).strip().lower())

        if transport_failures or hard_failures or warn_failures:
            return probe_result_capped(
                self.id,
                self.domain,
                successes=len(texts),
                attempts=self.samples,
                notes=notes,
                warn_failures=warn_failures,
                transport_failures=transport_failures,
            )

        baseline = ctx.selected_baseline
        baseline_present = baseline is not None
        terms: list[str] = []
        if baseline is not None:
            fingerprint = baseline.fingerprints.get("self_report")
            if isinstance(fingerprint, dict):
                raw_terms = fingerprint.get("terms")
                if isinstance(raw_terms, list):
                    terms = [str(term).lower() for term in raw_terms]

        if baseline_present and terms:
            match_flags = [any(term in text for term in terms) for text in texts]
            contradiction_flags = [not flag for flag in match_flags]
            report_match = any(match_flags)
            contradiction_both = all(not flag for flag in match_flags)
        else:
            # Missing/empty baseline terms are *unknown*, never a
            # contradiction: the report cannot be judged against an empty
            # reference, so all flags stay None and contradiction_both stays
            # False. Empty terms must never feed a hidden_origin veto or a
            # FAIL (docs/06 §4.4: baseline token overlap is the tolerance).
            match_flags = [None, None]
            contradiction_flags = [None, None]
            report_match = None
            contradiction_both = False

        drift = _surface_drift(ctx, baseline)
        family_hits = [provider_family_hits(text) for text in texts]
        family_hits_union = sorted({provider for hits in family_hits for provider in hits})
        metrics = {
            "self_report": {
                "texts": texts,
                "stable": len(texts) == 2 and texts[0] == texts[1],
                "report_match": report_match,
                "match_flags": match_flags,
                "contradiction_flags": contradiction_flags,
                "contradiction_both": contradiction_both,
                "surface_drift": drift,
                "family_hits": family_hits,
                "family_hits_union": family_hits_union,
            }
        }

        if not baseline_present:
            notes.append("no baseline — structural run cannot PASS or FAIL")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if not terms:
            notes.append("baseline carries no self-report terms — report unknown, not a contradiction")
        if contradiction_both and drift["hard"]:
            notes.append(
                "self-report contradicts the claim and /models surface drifted hard (claimed_present flipped) — FAIL"
            )
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if drift["hard"]:
            notes.append("hard surface drift (claimed_present flipped) but report consistent")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if drift["mild"]:
            notes.append(f"mild drift: catalog count delta {drift['count_delta_pct']:.1f}%")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if not terms:
            # Missing/empty baseline terms make the report unjudgeable:
            # unknown, never a contradiction (metrics already carry the None
            # flags), so this is a structural WARN and can never FAIL.
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if not report_match:
            notes.append("self-report matches no baseline claim terms")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        notes.append("self-report consistent with baseline claim terms")
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=self.samples,
            attempts=self.samples,
            notes=notes,
            metrics=metrics,
        )


def _surface_drift(ctx: RunContext, baseline: Any) -> dict[str, Any]:
    if baseline is None:
        return {
            "baseline_present": False,
            "mild": False,
            "hard": False,
            "count_delta_pct": 0.0,
            "claimed_present_baseline": None,
            "claimed_present_now": None,
            "models_catalog_baseline": None,
            "models_catalog_now": None,
        }
    baseline_catalog = baseline.surface.models_catalog
    now_catalog = len(ctx.surface.models)
    baseline_claimed = baseline.surface.claimed_present
    now_claimed = ctx.surface.claimed_present
    count_delta_pct = abs(now_catalog - baseline_catalog) / baseline_catalog * 100.0 if baseline_catalog > 0 else 0.0
    return {
        "baseline_present": True,
        "mild": count_delta_pct > 30.0,
        "hard": bool(baseline_claimed) and not now_claimed,
        "count_delta_pct": round(count_delta_pct, 2),
        "claimed_present_baseline": baseline_claimed,
        "claimed_present_now": now_claimed,
        "models_catalog_baseline": baseline_catalog,
        "models_catalog_now": now_catalog,
    }


class CanaryEchoProbe:
    """d4.canary_echo ×2 pairs (4 calls) — exact canary echo, cross-request
    contamination, templating, and p0 asymmetry (docs/06 §4.5)."""

    id = "d4.canary_echo"
    domain = Domain.D4
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        canaries = [_fresh_canary(), _fresh_canary()]
        # Call order: pair 0 (canary A), pair 1 (canary B), pair 0, pair 1.
        call_canaries = [canaries[0], canaries[1], canaries[0], canaries[1]]
        outputs: list[str] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for canary in call_canaries:
            payload = {
                "model": ctx.model,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Reply with exactly this token and nothing else: {canary}",
                    }
                ],
                "max_tokens": 24,
                "temperature": 0,
            }
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(exc.note)
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"non-retryable bad response (status {response.status_code})")
                continue
            outputs.append(_content_of(_json(response)))

        echo_ok = [canary in content for canary, content in zip(call_canaries, outputs, strict=False)]
        echo_exact = [content.strip() == canary for canary, content in zip(call_canaries, outputs, strict=False)]
        pairs_complete = len(outputs) == 4
        contamination_pairs = (
            [
                any(call_canaries[1 - j] in outputs[i] for i in range(4) if call_canaries[i] == call_canaries[j])
                for j in range(2)
            ]
            if pairs_complete
            else [False, False]
        )
        contamination = any(contamination_pairs)
        template = pairs_complete and any(outputs[2 * p].strip() == outputs[2 * p + 1].strip() for p in range(2))
        p0_echo = ctx.p0_verdicts.get("p0.echo")
        asymmetry = pairs_complete and bool(p0_echo == Verdict.PASS.value) and all(not f for f in echo_exact)

        metrics = {
            "canary_echo": {
                "canaries": canaries,
                "outputs": outputs,
                "echo_ok": echo_ok,
                "echo_exact": echo_exact,
                "contamination": contamination,
                "contamination_pairs": contamination_pairs,
                "template": template,
                "asymmetry": asymmetry,
                "p0_echo": p0_echo,
            }
        }
        if transport_failures or hard_failures or warn_failures:
            return probe_result_capped(
                self.id,
                self.domain,
                successes=0,
                attempts=self.samples,
                notes=notes,
                warn_failures=warn_failures,
                transport_failures=transport_failures,
            )
        if template:
            notes.append("template detected: identical reply across a pair despite different canaries")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if asymmetry:
            notes.append(f"asymmetry: p0.echo passed but every canary echo failed (p0_echo={p0_echo})")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if contamination and all(contamination_pairs):
            notes.append("cross-request contamination in both pairs")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if all(echo_exact):
            notes.append("all canaries echoed exactly")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.PASS,
                score=100.0,
                successes=self.samples,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if contamination:
            notes.append("single pair contamination")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if any(echo_ok):
            notes.append("relaxed echo: canary present but not exact")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=0,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        notes.append("canary not echoed")
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.WARN,
            score=50.0,
            successes=0,
            attempts=self.samples,
            notes=notes,
            metrics=metrics,
        )


class SseTimingProbe:
    """d4.sse_timing ×3 streams — TTFT/inter-chunk envelope and relay
    buffering detection (docs/06 §4.6). Baseline-gated for percentile
    ratios; buffering detection works without a baseline."""

    id = "d4.sse_timing"
    domain = Domain.D4
    weight = 1.0
    samples = 3

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "Count from 1 to 50."}],
            "max_tokens": 64,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
        }
        per_stream: list[dict[str, Any]] = []
        ttfts: list[float] = []
        totals: list[float] = []
        e2es: list[float] = []
        inters: list[float] = []
        buffered_flags: list[bool] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        samples: list[TimingSample] = []
        for i in range(self.samples):
            try:
                result = await ctx.stream(self.id, "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(f"stream {i}: {exc.note}")
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"stream {i}: transport error: {exc}")
                continue
            if result.status != 200:
                hard_failures += 1
                notes.append(f"stream {i}: non-retryable bad response (status {result.status})")
                continue
            ttft, total, stream_inters, n_chunks, text = _analyze_stream(result)
            if n_chunks == 0:
                hard_failures += 1
                notes.append(f"stream {i}: no content events")
                continue
            buffered = _is_buffered(ttft, total, stream_inters, n_chunks)
            ttfts.append(ttft)
            totals.append(total)
            e2es.append(result.e2e_ms)
            inters.extend(stream_inters)
            buffered_flags.append(buffered)
            per_stream.append(
                {
                    "status": result.status,
                    "n_chunks": n_chunks,
                    "text": text,
                    "est_tokens": len(text) // 4,
                    "ttft_ms": ttft,
                    "total_ms": total,
                    "buffered": buffered,
                }
            )
            samples.append(TimingSample(kind="ttft", ms=ttft))
            samples.append(TimingSample(kind="e2e", ms=result.e2e_ms))
            samples.extend(TimingSample(kind="itl", ms=gap) for gap in stream_inters)

        baseline_metrics = _sse_baseline_metrics(ctx, ttfts, inters)
        metrics = {
            "sse_timing": {
                "buffered": buffered_flags,
                "buffered_count": sum(buffered_flags),
                "ttft_ms": ttfts,
                "total_ms": totals,
                "inter_chunk_ms": inters,
                "ttft_median_ms": percentile(ttfts, 50),
                "ttft_p90_ms": percentile(ttfts, 90),
                "inter_chunk_median_ms": percentile(inters, 50),
                "inter_chunk_p90_ms": percentile(inters, 90),
                "e2e_median_ms": percentile(e2es, 50),
                "baseline": baseline_metrics,
                "per_stream": per_stream,
            }
        }
        if transport_failures or hard_failures or warn_failures:
            result = probe_result_capped(
                self.id,
                self.domain,
                successes=len(per_stream),
                attempts=self.samples,
                notes=notes,
                warn_failures=warn_failures,
                transport_failures=transport_failures,
            )
            result.samples = samples
            result.metrics = metrics
            return result

        buffered_count = sum(buffered_flags)
        if buffered_count >= 2:
            notes.append("relay buffering: >= 2 of 3 streams burst after a stall")
            return _sse_result(
                verdict=Verdict.FAIL, score=0.0, successes=0, notes=notes, metrics=metrics, samples=samples
            )
        if buffered_count == 1:
            notes.append("exactly one buffered stream (WARN band)")
            return _sse_result(
                verdict=Verdict.WARN, score=50.0, successes=2, notes=notes, metrics=metrics, samples=samples
            )
        usable = (
            baseline_metrics["sse_ttft_p90_ms"] is not None or baseline_metrics["sse_inter_chunk_p90_ms"] is not None
        )
        if not baseline_metrics["present"] or not usable:
            if baseline_metrics["present"]:
                notes.append("baseline present but no usable sse_timing p90 — structural checks only")
            else:
                notes.append("no baseline — structural checks only")
            return _sse_result(
                verdict=Verdict.PASS, score=100.0, successes=self.samples, notes=notes, metrics=metrics, samples=samples
            )
        worst = max(
            ratio
            for ratio in (baseline_metrics["ttft_ratio"], baseline_metrics["inter_chunk_ratio"])
            if ratio is not None
        )
        if worst <= 2.0:
            notes.append("timing ratios <= 2x baseline p90")
            return _sse_result(
                verdict=Verdict.PASS, score=100.0, successes=self.samples, notes=notes, metrics=metrics, samples=samples
            )
        if worst <= 5.0:
            notes.append(f"timing ratios {worst:.2f}x baseline p90 — WARN band (> 2x, <= 5x)")
            return _sse_result(
                verdict=Verdict.WARN, score=50.0, successes=0, notes=notes, metrics=metrics, samples=samples
            )
        notes.append(f"timing ratios {worst:.2f}x baseline p90 — exceeds the documented bound (> 5x)")
        return _sse_result(verdict=Verdict.WARN, score=50.0, successes=0, notes=notes, metrics=metrics, samples=samples)


def _sse_result(
    *,
    verdict: Verdict,
    score: float,
    successes: int,
    notes: list[str],
    metrics: dict[str, Any],
    samples: list[TimingSample],
) -> ProbeResult:
    result = ProbeResult(
        probe_id="d4.sse_timing",
        domain=Domain.D4,
        verdict=verdict,
        score=score,
        successes=successes,
        attempts=3,
        notes=notes,
        metrics=metrics,
    )
    result.samples = samples
    return result


def _analyze_stream(result: Any) -> tuple[float, float, list[float], int, str]:
    """Per-stream envelope: (ttft_ms, total_ms, content inter gaps, n_chunks, text).

    Content events are events whose running delta advanced past the previous
    event; the first event (often a role-only chunk) is not content.
    ``StreamedEvent.arrived_ms`` carries the raw ``time.perf_counter()``
    second-based timestamp, so gaps are converted to milliseconds exactly as
    ``RunContext.stream`` converts ``inter_event_ms``.
    """

    text = result.events[-1].delta if result.events else ""
    ttft = result.ttft_ms if result.ttft_ms is not None else 0.0
    total = result.e2e_ms
    content_events: list[Any] = []
    last_delta = ""
    for event in result.events:
        if event.delta != last_delta:
            content_events.append(event)
            last_delta = event.delta
    inters = [
        (content_events[i].arrived_ms - content_events[i - 1].arrived_ms) * 1000 for i in range(1, len(content_events))
    ]
    return ttft, total, inters, len(content_events), text


def _sse_baseline_metrics(ctx: RunContext, ttfts: list[float], inters: list[float]) -> dict[str, Any]:
    baseline = ctx.selected_baseline
    if baseline is None:
        return {
            "present": False,
            "sse_ttft_p90_ms": None,
            "sse_inter_chunk_p90_ms": None,
            "ttft_ratio": None,
            "inter_chunk_ratio": None,
        }
    ttft_p90 = _fingerprint_p90(baseline, "sse_ttft_ms")
    inter_p90 = _fingerprint_p90(baseline, "sse_inter_chunk_ms")
    ttft_median = percentile(ttfts, 50)
    inter_median = percentile(inters, 50)
    ttft_ratio = ttft_median / ttft_p90 if ttft_median is not None and ttft_p90 else None
    inter_ratio = inter_median / inter_p90 if inter_median is not None and inter_p90 else None
    return {
        "present": True,
        "sse_ttft_p90_ms": ttft_p90,
        "sse_inter_chunk_p90_ms": inter_p90,
        "ttft_ratio": ttft_ratio,
        "inter_chunk_ratio": inter_ratio,
    }


def _fingerprint_p90(baseline: Any, key: str) -> float | None:
    fingerprint = baseline.fingerprints.get(key)
    if not isinstance(fingerprint, dict):
        return None
    p90 = fingerprint.get("p90")
    return float(p90) if isinstance(p90, (int, float)) else None


class RotationProbe:
    """d4.rotation ×20 spaced calls — load-balanced routing across distinct
    upstream families (docs/06 §4.7). WARN at F==2, FAIL at F>=3."""

    id = "d4.rotation"
    domain = Domain.D4
    weight = 1.5
    samples = 20
    spacing_s = 1.0

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        payload = {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "Write the number 42 in words and stop."}],
            "max_tokens": 32,
            "temperature": 0,
        }
        features: list[dict[str, Any]] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False
        hard_failures = 0
        for i in range(self.samples):
            if i > 0:
                await asyncio.sleep(self.spacing_s)
            try:
                response = await request_with_retry(ctx, self.id, "POST", "/chat/completions", payload=payload)
            except (RateLimitError, ServerError) as exc:
                warn_failures = True
                notes.append(exc.note)
                continue
            except Exception as exc:  # noqa: BLE001 - transport errors stay FAIL
                transport_failures = True
                notes.append(f"transport error: {exc}")
                continue
            if response.status_code != 200:
                hard_failures += 1
                notes.append(f"non-retryable bad response (status {response.status_code})")
                continue
            body = _json(response)
            if body is None:
                hard_failures += 1
                notes.append("response body not valid JSON")
                continue
            features.append(_rotation_feature(body, response.headers))

        rotation_metrics = _rotation_metrics(ctx, features)
        metrics = {"rotation": rotation_metrics}
        successes = len(features)
        if transport_failures or hard_failures:
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=successes,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if warn_failures:
            notes.append("degraded by persistent 429/5xx — WARN per §1.3")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=successes,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if not features:
            return warn_result(self.id, self.domain, notes=notes + ["no usable responses"], attempts=self.samples)
        family_count = rotation_metrics["F"]
        if family_count >= 3:
            notes.append(f"multiple upstream backends: {family_count} distinct families in one run")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.FAIL,
                score=0.0,
                successes=successes,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        if family_count == 2:
            notes.append("two response families — evidence toward suspected substitution, not a verdict")
            return ProbeResult(
                probe_id=self.id,
                domain=self.domain,
                verdict=Verdict.WARN,
                score=50.0,
                successes=successes,
                attempts=self.samples,
                notes=notes,
                metrics=metrics,
            )
        notes.append("single response family across all calls")
        return ProbeResult(
            probe_id=self.id,
            domain=self.domain,
            verdict=Verdict.PASS,
            score=100.0,
            successes=successes,
            attempts=self.samples,
            notes=notes,
            metrics=metrics,
        )


def _rotation_feature(body: dict, headers: Any) -> dict[str, Any]:
    response_id = body.get("id")
    served_by = headers.get("x-served-by") or headers.get("server")
    return {
        "id_family": id_prefix_family(str(response_id)) if response_id else None,
        "content_bucket": content_bucket(_content_of(body)),
        "usage_ratio": _usage_ratio(body.get("usage")),
        "served_by": served_by,
        "shape": str(_shape(body)),
    }


def _rotation_metrics(ctx: RunContext, features: list[dict[str, Any]]) -> dict[str, Any]:
    assignments, reps = cluster_families(features)
    f_expected: int | None = None
    baseline = ctx.selected_baseline
    if baseline is not None:
        expected = baseline.fingerprints.get("rotation_families")
        if isinstance(expected, (int, float)):
            f_expected = int(expected)
    # Distinct official providers among the cluster representatives (docs/06
    # §1.5: substitution needs >= 2 mapped official providers). Custom or
    # unknown id families are unclassified and never count.
    providers = sorted(
        {provider for rep in reps if (provider := provider_of_family(rep.get("id_family"))) is not None}
    )
    return {
        "F": len(reps),
        "F_expected": f_expected,
        "features": features,
        "assignments": assignments,
        "families": reps,
        "providers": providers,
        "distinct_official_providers": len(providers),
    }
