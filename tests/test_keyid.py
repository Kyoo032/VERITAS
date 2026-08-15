"""One-way API-key fingerprint (security hardening)."""

from supgate.keyid import key_fingerprint


def test_fingerprint_is_sha256_prefixed_and_deterministic():
    fp = key_fingerprint("sk-test-1234567890abcdef")
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 64
    assert fp == key_fingerprint("sk-test-1234567890abcdef")


def test_fingerprint_differs_between_keys():
    assert key_fingerprint("sk-key-a-111111111111") != key_fingerprint("sk-key-b-222222222222")


def test_fingerprint_empty_key_is_empty():
    assert key_fingerprint("") == ""
