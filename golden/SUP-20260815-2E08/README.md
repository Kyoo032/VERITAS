# Golden bundle — SUP-20260815-2E08

Official field test of the the supplier gateway (2026-08-15).

- **Endpoint:** `https://api.supplier.example/v1`
- **Model:** `gpt-5.4`
- **Mode:** adhoc · supgate 0.2.0 · manifest 3 · schema 2
- **Result:** overall 82.0 · assurance C · 16 pass / 5 warn / 1 fail / 15 skip · $0.36
- **Provenance:** captured live by `supgate run` with the operator's
  **official base API key** against **official OpenAI models**; committed as
  the official baseline reference for this release (docs/13 §5).
- **Anonymization:** the supplier name and endpoint host were removed per
  owner request (2026-08-15) and replaced with `api.supplier.example`;
  request/response exchange data is otherwise unmodified.
- **Redaction:** verified at copy time — no key material anywhere in this
  directory (byte-level scan across all files).
- **Contents:** `SUP-20260815-2E08.json` (bundle) + `evidence/` (79 redacted
  probe exchange records).
- **Replay:** golden replay of recorded exchanges is a P2 backlog item
  (docs/12 §8); do not treat these files as a live baseline record — the
  hash-pinned `supgate baseline record` format is distinct and must be
  captured from the live surface.
