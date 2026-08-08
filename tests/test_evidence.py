"""Evidence redaction + reproducible-curl builder (§5, §10)."""

from __future__ import annotations

from pathlib import Path

from supgate.evidence import (
    EvidenceWriter,
    build_curl,
    redact_headers,
    redact_payload,
    redact_secrets,
    redact_text,
    redact_url,
)


def test_redact_sk_key_keeps_stub():
    assert redact_secrets("key is sk-abcdef1234567890 end") == "key is sk-abc****7890 end"


def test_redact_long_key():
    assert redact_secrets("sk-lmnopqrstuvwxyz0123456789") == "sk-lmn****6789"


def test_redact_bearer_token():
    assert redact_secrets("Authorization: Bearer abcdefghijklmnop") == "Authorization: Bearer abcd****mnop"


def test_redact_leaves_plain_text_alone():
    assert redact_secrets("no secrets here 1234") == "no secrets here 1234"


def test_redact_payload_recursive():
    payload = {"key": "sk-abcdef1234567890", "nested": {"token": "Bearer xyzabc1234567890"}, "items": ["sk-1111222233334444"]}
    out = redact_payload(payload)
    assert "sk-abcdef1234567890" not in str(out)
    assert "sk-111****4444" in str(out)


def test_redact_payload_removes_exact_secret_and_embedded_url_query():
    secret = "plain-runtime-secret-1234"
    out = redact_payload(
        {
            "text": f"echo {secret} from https://api.example/v1?api_key={secret}&model=gpt-4o",
            "proxy-authorization": "Basic opaque-value",
        },
        (secret,),
    )
    assert secret not in str(out)
    assert "api_key=$SUPGATE_KEY" in out["text"]
    assert out["proxy-authorization"] == "$SUPGATE_KEY"


def test_redact_text_scrubs_sensitive_query_in_exception_text():
    text = "ConnectError for https://api.example/v1?token=opaque-secret&model=gpt-4o"
    assert redact_text(text) == (
        "ConnectError for https://api.example/v1?token=$SUPGATE_KEY&model=gpt-4o"
    )


def test_redact_headers_replaces_auth():
    out = redact_headers({"Authorization": "Bearer sk-secret1234567890", "x-custom": "1"})
    assert out["Authorization"] == "Bearer $SUPGATE_KEY"
    assert out["x-custom"] == "1"


def test_build_curl_redacts_auth_and_body():
    curl = build_curl(
        "POST",
        "https://api.example/v1/chat/completions",
        {"Authorization": "Bearer sk-secret1234567890"},
        {"model": "gpt-4o", "key": "sk-leakme1234567890"},
    )
    assert "$SUPGATE_KEY" in curl
    assert "sk-secret1234567890" not in curl
    assert "sk-leakme1234567890" not in curl
    assert '"key":"$SUPGATE_KEY"' in curl
    assert curl.startswith("curl -sS -X POST")


def test_build_curl_strips_httpx_transport_headers():
    """Wire headers must not leak the httpx fingerprint or transport noise."""
    curl = build_curl(
        "POST",
        "https://api.example/v1/chat/completions",
        {
            "host": "api.example",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
            "user-agent": "python-httpx/0.28.1",
            "content-length": "18",
            "content-type": "application/json",
            "authorization": "Bearer sk-secret1234567890",
        },
        {"model": "gpt-4o"},
    )
    assert "user-agent" not in curl
    assert "python-httpx" not in curl
    assert "accept-encoding" not in curl
    assert "connection" not in curl
    assert "content-length" not in curl
    assert "-H 'content-type: application/json'" in curl
    assert "-H 'host:" not in curl


def test_evidence_writer_saves_redacted_doc(tmp_path: Path):
    writer = EvidenceWriter(tmp_path, "SUP-TEST")
    ref = writer.save(
        "d6.chat.basic",
        method="POST",
        url="https://api.example/v1/chat/completions",
        request_headers={"Authorization": "Bearer sk-secret1234567890"},
        request_body={"model": "gpt-4o", "api_key": "sk-secret1234567890"},
        status=200,
        response_headers={"content-type": "application/json"},
        response_body={"id": "chatcmpl-1", "usage": "sk-inresponse1234567890"},
        curl="curl -sS ...",
    )
    path = tmp_path / "SUP-TEST" / ref.split("/")[-1]
    text = path.read_text(encoding="utf-8")
    assert "sk-secret1234567890" not in text
    assert "sk-inresponse1234567890" not in text
    assert "chatcmpl-1" in text


def test_evidence_writer_redacts_exact_arbitrary_request_secret_from_response(tmp_path: Path):
    secret = "plain-runtime-secret-1234"
    writer = EvidenceWriter(tmp_path, "SUP-TEST")
    ref = writer.save(
        "d4.self_report",
        method="POST",
        url="https://api.example/v1/chat/completions",
        request_headers={"Authorization": f"Bearer {secret}"},
        request_body={"model": "gpt-4o"},
        status=200,
        response_headers={"x-debug": secret},
        response_body={"content": f"echoed {secret}"},
        curl=f"curl -H 'Authorization: Bearer {secret}'",
    )
    text = (tmp_path / "SUP-TEST" / ref.split("/")[-1]).read_text(encoding="utf-8")
    assert secret not in text
    assert "$SUPGATE_KEY" in text


def test_redact_short_sk_token():
    assert redact_secrets("dev key sk-test") == "dev key sk-te****"
    assert redact_secrets("dev key sk-abc123") == "dev key sk-ab****"


def test_redact_url_query_secrets():
    url = "https://api.example/v1/chat?api_key=sk-secret1234567890&access_token=abc&model=gpt-4o&token=xyz&key=kkk&apikey=zz"
    assert redact_url(url) == (
        "https://api.example/v1/chat?api_key=$SUPGATE_KEY&access_token=$SUPGATE_KEY"
        "&model=gpt-4o&token=$SUPGATE_KEY&key=$SUPGATE_KEY&apikey=$SUPGATE_KEY"
    )


def test_redact_url_redacts_sk_in_path_and_fragment():
    assert "sk-secret1234567890" not in redact_url("https://api.example/v1/sk-secret1234567890?plain=1")


def test_redact_headers_covers_custom_auth_headers():
    out = redact_headers(
        {
            "X-Api-Key": "sk-secret1234567890",
            "Authorization": "Bearer sk-secret1234567890",
            "x-auth-token": "abc123",
            "access_token": "tok",
            "x-custom": "sk-leak1234567890",
        }
    )
    assert out["X-Api-Key"] == "$SUPGATE_KEY"
    assert out["Authorization"] == "Bearer $SUPGATE_KEY"
    assert out["x-auth-token"] == "$SUPGATE_KEY"
    assert out["access_token"] == "$SUPGATE_KEY"
    assert "sk-leak1234567890" not in out["x-custom"]
    assert out["x-custom"] == "sk-lea****7890"


def test_build_curl_redacts_custom_auth_headers():
    curl = build_curl(
        "GET",
        "https://api.example/v1/models",
        {"X-Api-Key": "sk-secret1234567890", "Accept": "application/json"},
    )
    assert "sk-secret1234567890" not in curl
    assert "-H 'X-Api-Key: $SUPGATE_KEY'" in curl
    assert "Accept: application/json" in curl


def test_proxy_authorization_is_redacted_in_headers_and_curl():
    headers = {"Proxy-Authorization": "Basic opaque-secret"}
    assert redact_headers(headers)["Proxy-Authorization"] == "$SUPGATE_KEY"
    curl = build_curl("GET", "https://api.example/v1/models", headers)
    assert "opaque-secret" not in curl
    assert "Proxy-Authorization: $SUPGATE_KEY" in curl


def test_build_curl_redacts_url_query():
    curl = build_curl("GET", "https://api.example/v1/models?api_key=sk-secret1234567890")
    assert "sk-secret1234567890" not in curl
    assert "api_key=$SUPGATE_KEY" in curl


def test_evidence_writer_captures_redacted_curl(tmp_path: Path):
    url = "https://api.example/v1/chat/completions?api_key=sk-leakme1234567890"
    headers = {"X-Api-Key": "sk-leakme1234567890"}
    curl = build_curl("POST", url, headers, {"model": "gpt-4o"})
    writer = EvidenceWriter(tmp_path, "SUP-TEST")
    writer.save(
        "d6.chat.basic",
        method="POST",
        url=url,
        request_headers=headers,
        request_body={"model": "gpt-4o"},
        status=200,
        response_headers={},
        response_body={},
        curl=curl,
    )
    captured = writer.curl_for("d6.chat.basic")
    assert captured is not None
    assert "sk-leakme1234567890" not in captured
    assert "api_key=$SUPGATE_KEY" in captured
    assert "X-Api-Key: $SUPGATE_KEY" in captured


def test_evidence_writer_curl_for_missing_probe_is_none(tmp_path: Path):
    assert EvidenceWriter(tmp_path, "SUP-TEST").curl_for("nope") is None
