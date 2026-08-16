"""Evidence redaction choke point + reproducible-curl builder (§5, §10).

All requests/responses pass through here. Keys are redacted everywhere;
curl lines reference ``$SUPGATE_KEY`` so they stay reproducible without
printing the secret.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_KEY = re.compile(r"(sk-[A-Za-z0-9_-]{4,})")
_BEARER = re.compile(r"(Bearer\s+)([A-Za-z0-9._-]{8,})")
# Non-OpenAI credential shapes that can appear verbatim in endpoint-controlled
# response bodies: Google API keys and GitHub tokens (classic + fine-grained).
_GOOGLE_KEY = re.compile(r"(AIza[0-9A-Za-z_\-]{35})")
_GITHUB_TOKEN = re.compile(r"(gh[pousr]_[A-Za-z0-9]{36,255})")
_URL = re.compile(r"https?://[^\s\"'<>]+")
# Custom auth header names: x-api-key, api-key, access-token, authorization, ...
_SENSITIVE_HEADER = re.compile(
    r"^(?:(?:x[_-]?)?(?:api[_-]?key|api[_-]?token|access[_-]?token|auth(?:orization)?(?:[_-]token|[_-]key)?|token)|proxy[_-]?auth(?:orization)?(?:[_-](?:token|key))?)$",
    re.IGNORECASE,
)
# Sensitive URL query parameter names: api_key, key, token, access_token, ...
_SENSITIVE_PARAM = re.compile(
    r"^(?:x[_-]?)?(?:api[_-]?key|api[_-]?token|access[_-]?token|auth(?:[_-]token|[_-]key)?|token|key|secret)$",
    re.IGNORECASE,
)

_REDACTED = "$SUPGATE_KEY"

# httpx transport headers that curl manages itself or that fingerprint the
# client. Skipped in the reproducible curl so replays are clean and do not
# leak ``python-httpx/...`` (build plan §10.1 self-check). ``content-type`` is
# deliberately kept: curl's ``-d`` default differs from ``application/json``.
_CURL_SKIP_HEADERS = frozenset(
    {"host", "content-length", "accept-encoding", "connection", "user-agent"}
)


def _key_stub(token: str) -> str:
    """Recognizable but non-reconstructable stub: ``prefix****suffix``."""

    if len(token) > 10:
        return token[:6] + "****" + token[-4:]
    return token[:5] + "****"


def redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Redact sk- keys and bearer tokens, keeping a recognizable stub.

    ``sk-abc...WXYZ`` becomes ``sk-abc****WXYZ`` (plan: ``sk_l****5765`` style).
    Short keys are redacted to a non-reconstructable prefix stub.
    """

    def _key_repl(m: re.Match[str]) -> str:
        return _key_stub(m.group(1))

    def _bearer_repl(m: re.Match[str]) -> str:
        tok = m.group(2)
        if len(tok) <= 8:
            return m.group(1) + tok[:4] + "****"
        return m.group(1) + tok[:4] + "****" + tok[-4:]

    for secret in sorted({value for value in extra_secrets if len(value) >= 4}, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    text = _KEY.sub(_key_repl, _BEARER.sub(_bearer_repl, text))
    text = _GOOGLE_KEY.sub(_key_repl, text)
    return _GITHUB_TOKEN.sub(_key_repl, text)


def redact_payload(payload: Any, extra_secrets: tuple[str, ...] = ()) -> Any:
    """Recursively redact string values inside a JSON payload (req or resp)."""

    if isinstance(payload, str):
        return redact_text(payload, extra_secrets)
    if isinstance(payload, list):
        return [redact_payload(item, extra_secrets) for item in payload]
    if isinstance(payload, dict):
        redacted: dict[Any, Any] = {}
        for key, value in payload.items():
            name = str(key)
            if _SENSITIVE_HEADER.fullmatch(name) or _SENSITIVE_PARAM.fullmatch(name):
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_payload(value, extra_secrets)
        return redacted
    return payload


def redact_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Redact exact known secrets, key patterns, and sensitive URL values in text."""

    text = redact_secrets(text, extra_secrets)
    return _URL.sub(lambda match: redact_url(match.group(0), extra_secrets), text)


def redact_url(url: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Redact sensitive query-parameter values and any embedded key/token."""

    if "?" not in url:
        return redact_secrets(url, extra_secrets)
    base, _, query = url.partition("?")
    pairs: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        if sep and _SENSITIVE_PARAM.fullmatch(name):
            pairs.append(f"{name}={_REDACTED}")
        else:
            pairs.append(redact_secrets(pair, extra_secrets))
    return f"{redact_secrets(base, extra_secrets)}?{'&'.join(pairs)}"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact Authorization + custom auth headers; scrub secrets from the rest."""

    out = dict(headers)
    for k in list(out):
        if k.lower() == "authorization":
            out[k] = f"Bearer {_REDACTED}"
        elif _SENSITIVE_HEADER.fullmatch(k):
            out[k] = _REDACTED
        elif isinstance(out[k], str):
            out[k] = redact_text(out[k])
    return out


def secure_write(path: Path, text: str) -> None:
    """Write UTF-8 text, then restrict POSIX permissions to owner-only (0600).

    Windows has no POSIX mode bits (only a read-only flag), so the chmod is
    skipped there; evidence inherits the user-profile ACLs instead.
    """

    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)


def build_curl(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    """Redacted reproducible curl for a failed probe (evidence-first, §2)."""

    parts = [f"curl -sS -X {method} '{redact_url(url)}'"]
    for key, value in (headers or {}).items():
        if key.lower() in _CURL_SKIP_HEADERS:
            continue
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

    def __init__(self, root: Path, run_id: str, key_fingerprint: str | None = None) -> None:
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._refs: dict[str, list[str]] = {}
        self._curls: dict[str, list[str]] = {}
        self.key_fingerprint = key_fingerprint

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
        secrets = _request_secrets(request_headers or {})
        safe_curl = redact_text(curl, secrets)
        doc = {
            "probe": probe_id,
            "key_fingerprint": self.key_fingerprint,
            "request": {
                "method": method,
                "url": redact_url(url, secrets),
                "headers": redact_headers(request_headers or {}),
                "body": redact_payload(request_body, secrets),
                "curl": safe_curl,
            },
            "response": {
                "status": status,
                "headers": redact_payload(redact_headers(response_headers or {}), secrets),
                "body": redact_payload(response_body, secrets),
            },
            "captured_at": datetime.now(UTC).isoformat(),
        }
        secure_write(self.dir / name, json.dumps(doc, indent=2, default=str))
        ref = f"{self.dir.name}/{name}"
        self._refs.setdefault(probe_id, []).append(ref)
        self._curls.setdefault(probe_id, []).append(safe_curl)
        return ref


def _request_secrets(headers: dict[str, str]) -> tuple[str, ...]:
    secrets: list[str] = []
    for name, value in headers.items():
        if not isinstance(value, str):
            continue
        if name.lower() == "authorization" and value.lower().startswith("bearer "):
            secrets.append(value[7:].strip())
        elif _SENSITIVE_HEADER.fullmatch(name):
            secrets.append(value)
    return tuple(secrets)
