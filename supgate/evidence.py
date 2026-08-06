"""Evidence redaction choke point + reproducible-curl builder (§5, §10).

All requests/responses pass through here. Keys are redacted everywhere;
curl lines reference ``$SUPGATE_KEY`` so they stay reproducible without
printing the secret.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_KEY = re.compile(r"(sk-[A-Za-z0-9_-]{4,})")
_BEARER = re.compile(r"(Bearer\s+)([A-Za-z0-9._-]{8,})")
# Custom auth header names: x-api-key, api-key, access-token, authorization, ...
_SENSITIVE_HEADER = re.compile(
    r"^(?:x[_-]?)?(?:api[_-]?key|api[_-]?token|access[_-]?token|auth(?:orization)?(?:[_-]token|[_-]key)?|token)$",
    re.IGNORECASE,
)
# Sensitive URL query parameter names: api_key, key, token, access_token, ...
_SENSITIVE_PARAM = re.compile(
    r"^(?:x[_-]?)?(?:api[_-]?key|api[_-]?token|access[_-]?token|auth(?:[_-]token|[_-]key)?|token|key|secret)$",
    re.IGNORECASE,
)

_REDACTED = "$SUPGATE_KEY"


def redact_secrets(text: str) -> str:
    """Redact sk- keys and bearer tokens, keeping a recognizable stub.

    ``sk-abc...WXYZ`` becomes ``sk-abc****WXYZ`` (plan: ``sk_l****5765`` style).
    Short keys are redacted to a non-reconstructable prefix stub.
    """

    def _key_repl(m: re.Match[str]) -> str:
        tok = m.group(1)
        if len(tok) > 10:
            return tok[:6] + "****" + tok[-4:]
        return tok[:5] + "****"

    def _bearer_repl(m: re.Match[str]) -> str:
        tok = m.group(2)
        if len(tok) <= 8:
            return m.group(1) + tok[:4] + "****"
        return m.group(1) + tok[:4] + "****" + tok[-4:]

    return _KEY.sub(_key_repl, _BEARER.sub(_bearer_repl, text))


def redact_payload(payload: Any) -> Any:
    """Recursively redact string values inside a JSON payload (req or resp)."""

    if isinstance(payload, str):
        return redact_secrets(payload)
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, dict):
        return {k: redact_payload(v) for k, v in payload.items()}
    return payload


def redact_url(url: str) -> str:
    """Redact sensitive query-parameter values and any embedded key/token."""

    if "?" not in url:
        return redact_secrets(url)
    base, _, query = url.partition("?")
    pairs: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        if sep and _SENSITIVE_PARAM.fullmatch(name):
            pairs.append(f"{name}={_REDACTED}")
        else:
            pairs.append(redact_secrets(pair))
    return f"{redact_secrets(base)}?{'&'.join(pairs)}"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact Authorization + custom auth headers; scrub secrets from the rest."""

    out = dict(headers)
    for k in list(out):
        if k.lower() == "authorization":
            out[k] = f"Bearer {_REDACTED}"
        elif _SENSITIVE_HEADER.fullmatch(k):
            out[k] = _REDACTED
        elif isinstance(out[k], str):
            out[k] = redact_secrets(out[k])
    return out


def build_curl(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    """Redacted reproducible curl for a failed probe (evidence-first, §2)."""

    parts = [f"curl -sS -X {method} '{redact_url(url)}'"]
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            parts.append("-H 'Authorization: Bearer $SUPGATE_KEY'")
        elif _SENSITIVE_HEADER.fullmatch(key):
            parts.append(f"-H '{key}: $SUPGATE_KEY'")
        else:
            parts.append(f"-H '{key}: {redact_secrets(value) if isinstance(value, str) else value}'")
    if body:
        parts.append(f"-d '{json.dumps(redact_payload(body), separators=(',', ':'))}'")
    return " \\\n  ".join(parts)


class EvidenceWriter:
    """Writes redacted request/response pairs to disk per probe sample."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._refs: dict[str, list[str]] = {}
        self._curls: dict[str, list[str]] = {}

    def refs_for(self, probe_id: str) -> list[str]:
        return list(self._refs.get(probe_id, []))

    def curl_for(self, probe_id: str) -> str | None:
        """Last redacted curl captured for a probe (reproducible, secret-free)."""

        curls = self._curls.get(probe_id, [])
        return curls[-1] if curls else None

    def save(
        self,
        probe_id: str,
        *,
        method: str,
        url: str,
        request_headers: dict[str, str] | None,
        request_body: Any,
        status: int,
        response_headers: dict[str, str] | None,
        response_body: Any,
        curl: str,
    ) -> str:
        """Persist one redacted exchange; returns the relative evidence ref."""

        self._counter += 1
        name = f"{probe_id}_{self._counter:03d}.json"
        safe_curl = redact_secrets(curl)
        doc = {
            "probe": probe_id,
            "request": {
                "method": method,
                "url": redact_url(url),
                "headers": redact_headers(request_headers or {}),
                "body": redact_payload(request_body),
                "curl": safe_curl,
            },
            "response": {
                "status": status,
                "headers": redact_headers(response_headers or {}),
                "body": redact_payload(response_body),
            },
            "captured_at": datetime.now(UTC).isoformat(),
        }
        (self.dir / name).write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        ref = f"{self.dir.name}/{name}"
        self._refs.setdefault(probe_id, []).append(ref)
        self._curls.setdefault(probe_id, []).append(safe_curl)
        return ref
