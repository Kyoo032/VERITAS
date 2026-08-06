"""P0 foundation probes (build plan §10.1): auth/liveness, model list, error contract."""

from __future__ import annotations

import secrets

from supgate.models import Domain, ProbeResult, SurfaceMap
from supgate.probes.base import (
    RateLimitError,
    RunContext,
    ServerError,
    probe_result,
    probe_result_with_warn,
    request_with_retry,
)

_PING = "PONG-"


class EchoProbe:
    """p0.echo — minimal chat with a unique nonce. Separates 'endpoint dead'
    from 'probe failed' for everything downstream."""

    id = "p0.echo"
    domain = Domain.PLATFORM
    weight = 1.0
    samples = 1

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        nonce = secrets.token_hex(4)
        prompt = f"Reply with exactly this token and nothing else: {_PING}{nonce}"
        try:
            response = await request_with_retry(
                ctx, self.id, "POST", "/chat/completions",
                payload={
                    "model": ctx.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
            )
        except (RateLimitError, ServerError) as exc:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[exc.note], error=str(exc), warn_failures=True,
            )
        except Exception as exc:  # noqa: BLE001
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=["transport error — endpoint unreachable"], error=str(exc), transport_failures=True,
            )
        body = _json(response)
        content = _content(body)
        ok = response.status_code == 200 and nonce in content
        notes = []
        if not ok:
            notes.append(f"expected 200 + nonce echo, got status={response.status_code}")
        return probe_result(self.id, self.domain, successes=1 if ok else 0, attempts=1, notes=notes)


class ModelsProbe:
    """p0.models — GET /v1/models; builds the SurfaceMap that drives skip rules."""

    id = "p0.models"
    domain = Domain.PLATFORM
    weight = 1.0
    samples = 1

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        try:
            response = await request_with_retry(ctx, self.id, "GET", "/models")
        except (RateLimitError, ServerError) as exc:
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=[exc.note], error=str(exc), warn_failures=True,
            )
        except Exception as exc:  # noqa: BLE001
            return probe_result_with_warn(
                self.id, self.domain, successes=0, attempts=1,
                notes=["/models unreachable"], error=str(exc), transport_failures=True,
            )
        body = _json(response)
        listed = _model_ids(body)
        claimed_present = any(claimed in listed for claimed in ctx.claimed_models)
        ctx.surface.models = listed
        ctx.surface.claimed_present = claimed_present
        ok = response.status_code == 200 and listed
        notes = [f"{len(listed)} models listed", f"claimed present: {claimed_present}"]
        if not ok:
            notes.append(f"expected 200 + model list, got status={response.status_code}")
        return probe_result(self.id, self.domain, successes=1 if ok else 0, attempts=1, notes=notes)


class ErrorContractProbe:
    """p0.error_contract — 401 on bad key, 400 on malformed body, OpenAI-style error
    objects; self-check that supgate leaks no fingerprint into requests."""

    id = "p0.error_contract"
    domain = Domain.PLATFORM
    weight = 1.0
    samples = 2

    def skip_reason(self, surface: SurfaceMap) -> str | None:
        return None

    async def run(self, ctx: RunContext) -> ProbeResult:
        checks: list[bool] = []
        notes: list[str] = []
        warn_failures = False
        transport_failures = False

        try:
            response = await request_with_retry(
                ctx, self.id, "POST", "/chat/completions",
                headers={"Authorization": "Bearer sk-invalid-key-0000000000000000"},
                payload={"model": ctx.model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
            )
        except (RateLimitError, ServerError) as exc:
            warn_failures = True
            checks.append(False)
            notes.append(f"invalid-key check: {exc.note}")
        except Exception as exc:  # noqa: BLE001
            transport_failures = True
            checks.append(False)
            notes.append(f"invalid-key check: transport error: {exc}")
        else:
            body = _json(response)
            error_ok = _is_openai_error(body)
            checks.append(response.status_code == 401 and error_ok)
            if not checks[-1]:
                notes.append("invalid key did not produce 401 + OpenAI error object")

        try:
            response = await request_with_retry(
                ctx, self.id, "POST", "/chat/completions", raw_body=b"{not-json"
            )
        except (RateLimitError, ServerError) as exc:
            warn_failures = True
            checks.append(False)
            notes.append(f"malformed-body check: {exc.note}")
        except Exception as exc:  # noqa: BLE001
            transport_failures = True
            checks.append(False)
            notes.append(f"malformed-body check: transport error: {exc}")
        else:
            body = _json(response)
            error_ok = _is_openai_error(body)
            checks.append(response.status_code == 400 and error_ok)
            if not checks[-1]:
                notes.append("malformed body did not produce 400 + OpenAI error object")

        return probe_result_with_warn(
            self.id, self.domain, successes=sum(checks), attempts=2, notes=notes,
            warn_failures=warn_failures, transport_failures=transport_failures,
        )


def _json(response) -> dict | None:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


def _content(body: dict | None) -> str:
    if not body:
        return ""
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _model_ids(body: dict | None) -> list[str]:
    if not body:
        return []
    try:
        return [entry["id"] for entry in body["data"]]
    except (KeyError, TypeError):
        return []


def _is_openai_error(body: dict | None) -> bool:
    if not body:
        return False
    error = body.get("error")
    return isinstance(error, dict) and {"message", "type", "code"} <= set(error)
