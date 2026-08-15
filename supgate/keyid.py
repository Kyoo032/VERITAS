"""One-way API-key identity (security hardening).

The raw API key is never persisted — not in run bundles, baseline records,
history rows, or evidence exchanges. Every artifact carries only a SHA-256
fingerprint (``sha256:<64 hex>``) of the key that produced it, so an
operator can verify which key an artifact belongs to by recomputing the
fingerprint from a candidate key, without ever storing the secret.
"""

from __future__ import annotations

import hashlib


def key_fingerprint(api_key: str) -> str:
    """Return the one-way fingerprint of a raw API key.

    ``sha256:<64 hex chars>`` for a non-empty key, ``""`` for an empty one.
    The fingerprint is safe to persist and share: SHA-256 is one-way, so the
    key cannot be recovered from it.
    """
    if not api_key:
        return ""
    return "sha256:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()
