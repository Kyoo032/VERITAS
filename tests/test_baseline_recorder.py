"""Baseline recording against the in-memory fake server (no live network):
contract fields, deterministic ids, evidence redaction, env-only keys,
p0.echo gate, and failure aborts (docs/08 §10, docs/06 §2.3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from supgate.baseline_recorder import record_baseline
from supgate.baselines import BaselineRecordingError, BaselineStore


async def test_record_writes_contract_file(fake_server, transport, tmp_path: Path):
    out = tmp_path / "baselines"
    record = await record_baseline(
        vendor="openai",
        model="gpt-4o",
        api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1",
        out=out,
        evidence_root=tmp_path / "runs",
        transport=transport,
        captured_at="2026-08-06T00:00:00+00:00",
    )

    assert record.schema == 2
    assert record.baseline_id == "BL-OPENAI-GPT-4O-0001"
    assert record.vendor == "openai"
    assert record.model == "gpt-4o"
    assert record.model_version is None
    assert record.endpoint == "https://fake.example/v1"
    assert record.captured_at == "2026-08-06T00:00:00+00:00"
    assert record.provider_label == "openai"
    assert record.claimed_models == ["gpt-4o"]

    assert record.surface.models_catalog == 3  # fake server catalog
    assert record.surface.claimed_present is True
    assert record.surface.responses_api is True
    assert record.surface.messages_api is True

    assert record.fingerprints["id_prefix"] == {
        "family": "chatcmpl-", "samples": 3, "consistent": True,
    }
    assert record.fingerprints["chat_object"] == {
        "object": "chat.completion", "samples": 3,
    }
    assert record.fingerprints["model_echo"] == {
        "model": "gpt-4o", "samples": 3, "consistent": True,
    }
    assert "content-type" in record.fingerprints["headers_stable_set"]
    assert "x-fake-server" in record.fingerprints["headers_stable_set"]
    assert record.fingerprints["sse_ttft_ms"]["n"] == 1
    assert record.fingerprints["sse_ttft_ms"]["median"] is not None
    assert record.fingerprints["sse_inter_chunk_ms"]["n"] >= 1
    assert record.fingerprints["sse_chunks"]["n"] == 1
    assert record.fingerprints["sse_e2e_ms"]["n"] == 1
    assert record.fingerprints["usage_schema"] == {
        "cached_tokens": True, "reasoning_tokens": False,
    }
    # D4 reference fingerprints consumed directly by the probes (docs/06
    # §4.4/§4.7): self_report.terms from label/model + observations, and
    # the observed rotation family count.
    assert record.fingerprints["self_report"]["samples"] == 3
    assert "openai" in record.fingerprints["self_report"]["terms"]  # provider label
    assert "gpt-4o" in record.fingerprints["self_report"]["terms"]  # claimed model
    assert record.fingerprints["rotation_families"] == 1
    assert record.fingerprints["rotation_families_n"] == 3
    assert record.fingerprints["recount_deviation_pct"] == {
        "mean": 0.0, "std": 0.0, "n": 3,
    }
    assert record.fingerprints["wrap_offset_tokens"] == {
        "mean": 0.0, "std": 0.0, "n": 4,
    }

    assert (out / "BL-OPENAI-GPT-4O-0001.json").exists()
    assert BaselineStore(out).get("BL-OPENAI-GPT-4O-0001") == record


async def test_record_second_run_gets_next_sequence(fake_server, transport, tmp_path: Path):
    out = tmp_path / "baselines"
    first = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=out, evidence_root=tmp_path,
        transport=transport,
    )
    second = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=out, evidence_root=tmp_path,
        transport=transport,
    )
    assert first.baseline_id == "BL-OPENAI-GPT-4O-0001"
    assert second.baseline_id == "BL-OPENAI-GPT-4O-0002"
    assert len(list(out.glob("*.json"))) == 2


async def test_record_captures_versioned_model_identity(fake_server, transport, tmp_path: Path):
    out = tmp_path / "baselines"
    record = await record_baseline(
        vendor="openai", model="gpt-4o", model_version="gpt-4o-2024-08-06",
        label="official", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=out, evidence_root=tmp_path,
        transport=transport,
    )
    assert record.baseline_id == "BL-OFFICIAL-GPT-4O-0001"
    assert record.model_version == "gpt-4o-2024-08-06"
    assert record.provider_label == "official"
    assert "operator confirmed official endpoint" not in " ".join(record.notes)


async def test_record_confirm_official_lands_in_notes(fake_server, transport, tmp_path: Path):
    record = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path, evidence_root=tmp_path,
        transport=transport, confirmed_official=True,
    )
    assert any("confirmed official endpoint" in note for note in record.notes)


async def test_record_self_report_terms_include_official_observation(fake_server, transport, tmp_path: Path):
    """Terms come from the provider label, the claimed model, AND the official
    self-report observation — never a static provider table (docs/06 §4.4)."""
    fake_server.self_report_text = "This endpoint is served by OpenAI Azure."
    record = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path / "b",
        evidence_root=tmp_path / "runs", transport=transport,
    )
    terms = record.fingerprints["self_report"]["terms"]
    assert "openai" in terms     # operator label
    assert "gpt-4o" in terms     # claimed model
    assert "azure" in terms      # observed in the official self-report answer
    assert "endpoint" not in terms  # generic prose is never a term
    assert any("self-report observation" in note for note in record.notes)
    # The observation exchange is evidence like any other capture.
    evidence_dir = tmp_path / "runs" / "evidence"
    probe_ids = {
        json.loads(f.read_text(encoding="utf-8"))["probe"]
        for run_dir in evidence_dir.iterdir()
        for f in run_dir.glob("*.json")
    }
    assert "baseline.capture.self_report" in probe_ids


async def test_record_billing_calibration_reflects_inflation(fake_server, transport, tmp_path: Path):
    fake_server.usage_offset_pct = 88.0
    record = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path / "b",
        evidence_root=tmp_path / "runs", transport=transport,
    )
    recount = record.fingerprints["recount_deviation_pct"]
    assert recount["n"] == 3
    assert recount["mean"] >= 85.0


async def test_record_billing_calibration_reflects_wrap_offset(fake_server, transport, tmp_path: Path):
    fake_server.hidden_wrapper_tokens = 11
    record = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path / "b",
        evidence_root=tmp_path / "runs", transport=transport,
    )
    wrap = record.fingerprints["wrap_offset_tokens"]
    assert wrap == {"mean": 11.0, "std": 0.0, "n": 4}


async def test_record_unknown_encoding_omits_billing_calibration(fake_server, transport, tmp_path: Path):
    out = tmp_path / "b"
    record = await record_baseline(
        vendor="anthropic", model="claude-3-5-sonnet-20241022", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=out,
        evidence_root=tmp_path / "runs", transport=transport,
    )
    assert "recount_deviation_pct" not in record.fingerprints
    assert "wrap_offset_tokens" not in record.fingerprints
    assert any("billing calibration omitted" in note for note in record.notes)
    assert (out / record.baseline_id).with_suffix(".json").exists()


async def test_record_rotation_families_reflect_observed_routing(fake_server, transport, tmp_path: Path):
    """The recorded family count is observed from the chat samples, not a
    hard-coded 1 (docs/06 §4.7 F_expected)."""
    fake_server.rotation_families = 2
    record = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path / "b",
        evidence_root=tmp_path / "runs", transport=transport,
    )
    assert record.fingerprints["rotation_families"] == 2
    assert record.fingerprints["rotation_families_n"] == 3
    assert record.fingerprints["id_prefix"]["consistent"] is False  # both families seen


async def test_record_never_persists_key(fake_server, transport, tmp_path: Path):
    out = tmp_path / "baselines"
    await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=out, evidence_root=tmp_path / "runs",
        transport=transport,
    )
    for path in list(out.glob("*.json")) + list((tmp_path / "runs").rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert fake_server.valid_key not in text, path
        assert "sk-test-valid-key" not in text, path
    baseline_text = (out / "BL-OPENAI-GPT-4O-0001.json").read_text(encoding="utf-8")
    assert "$SUPGATE_KEY" not in baseline_text  # reference values, not auth headers
    for evidence in (tmp_path / "runs").rglob("*.json"):
        assert "$SUPGATE_KEY" in evidence.read_text(encoding="utf-8")  # R2/R5: curls carry the marker


async def test_record_evidence_refs_and_curl_redacted(fake_server, transport, tmp_path: Path):
    await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path / "b", evidence_root=tmp_path / "runs",
        transport=transport,
    )
    evidence_dir = tmp_path / "runs" / "evidence"
    run_dirs = list(evidence_dir.iterdir())
    assert len(run_dirs) == 1
    files = sorted(run_dirs[0].glob("*.json"))
    probe_ids = {json.loads(f.read_text(encoding="utf-8"))["probe"] for f in files}
    assert {
        "p0.echo", "p0.models", "baseline.capture.chat", "baseline.capture.stream",
        "d4.recount_deviation", "d4.wrap_offset",
    } <= probe_ids
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert fake_server.valid_key not in json.dumps(doc)
        assert "$SUPGATE_KEY" in doc["request"]["curl"]


async def test_record_p0_echo_gate_rejects_bad_auth(fake_server, transport, tmp_path: Path):
    with pytest.raises(BaselineRecordingError, match="p0.echo gate failed"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key="sk-wrong-key-0000000000000000",
            endpoint="https://fake.example/v1", out=tmp_path / "b", evidence_root=tmp_path,
            transport=transport,
        )
    assert list((tmp_path / "b").glob("*.json")) == []  # nothing written


async def test_record_p0_echo_gate_rejects_transport_error(tmp_path: Path):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(BaselineRecordingError, match="p0.echo gate failed"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key="sk-test-valid-key-0000000000",
            endpoint="https://fake.example/v1", out=tmp_path / "b", evidence_root=tmp_path,
            transport=CrashTransport(),
        )


async def test_record_gate_disabled_notes_fallback_and_transport_aborts(tmp_path: Path):
    class CrashTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(BaselineRecordingError, match="chat capture failed"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key="sk-x",
            endpoint="https://fake.example/v1", out=tmp_path / "b", evidence_root=tmp_path,
            transport=CrashTransport(), run_p0_gate=False,
        )
    assert list((tmp_path / "b").glob("*.json")) == []


async def test_record_gate_disabled_succeeds_with_note(fake_server, transport, tmp_path: Path):
    record = await record_baseline(
        vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
        endpoint="https://fake.example/v1", out=tmp_path / "b", evidence_root=tmp_path,
        transport=transport, run_p0_gate=False,
    )
    assert any("p0.echo gate: not run" in note for note in record.notes)


async def test_record_aborts_on_non_contract_chat_body(fake_server, transport, tmp_path: Path):
    original = fake_server._handle_chat

    async def handler(send, body, headers) -> None:
        payload = json.loads(body.decode("utf-8"))
        if b"PONG-" in body or payload.get("stream"):
            return await original(send, body, headers)
        return await _respond_text(send, 200, "not-json")

    fake_server._handle_chat = handler
    with pytest.raises(BaselineRecordingError, match="chat capture failed"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
            endpoint="https://fake.example/v1", out=tmp_path / "b", evidence_root=tmp_path,
            transport=transport,
        )
    assert list((tmp_path / "b").glob("*.json")) == []


async def test_record_rejects_endpoint_with_query_or_fragment(fake_server, transport, tmp_path: Path):
    with pytest.raises(ValueError, match="base URL without query"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
            endpoint="https://fake.example/v1?api_key=sk-leakme1234567890",
            out=tmp_path / "b", evidence_root=tmp_path,
            transport=transport,
        )
    with pytest.raises(ValueError, match="base URL without query"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key=fake_server.valid_key,
            endpoint="https://fake.example/v1#fragment",
            out=tmp_path / "b", evidence_root=tmp_path,
            transport=transport,
        )


async def test_record_rejects_invalid_args(fake_server, transport, tmp_path: Path):
    with pytest.raises(ValueError, match="model must not be empty"):
        await record_baseline(
            vendor="openai", model="  ", api_key="sk-x", endpoint="https://x/v1",
            out=tmp_path, transport=transport,
        )
    with pytest.raises(ValueError, match="endpoint must not be empty"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key="sk-x", endpoint="",
            out=tmp_path, transport=transport,
        )
    with pytest.raises(ValueError, match="samples/streams"):
        await record_baseline(
            vendor="openai", model="gpt-4o", api_key="sk-x", endpoint="https://x/v1",
            out=tmp_path, transport=transport, samples=0,
        )


async def _respond_text(send, status: int, text: str) -> None:
    from tests.fake_server import _respond

    await _respond(send, status, text)
